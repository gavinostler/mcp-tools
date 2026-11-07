"""Is Studio running the scripts on disk? Rojo sometimes misses an edit, or crashes, and a playtest
then runs old code. This maps every script in a Rojo project to where it lands in Studio, with its
length and a checksum, and writes the Luau that compares them inside Studio."""
import json
from pathlib import Path

# Rojo's script files, longest suffix first, so "x.server.luau" isn't read as a module "x.server"
SUFFIXES = (".server.luau", ".client.luau", ".server.lua", ".client.lua", ".luau", ".lua")
INITS = tuple("init" + s for s in SUFFIXES)


def checksum(data: bytes) -> int:
    h = 0
    for b in data:
        h = (h * 31 + b) % 4294967296
    return h


def _walk_dir(folder: Path, at: list[str], out: list):
    for init in INITS:
        if (folder / init).is_file():
            out.append((at, folder / init))
            break
    for child in sorted(folder.iterdir()):
        if child.is_dir():
            _walk_dir(child, at + [child.name], out)
        elif child.name not in INITS:
            for suffix in SUFFIXES:
                if child.name.endswith(suffix):
                    out.append((at + [child.name[: -len(suffix)]], child))
                    break


def _walk_node(node: dict, at: list[str], root: Path, out: list):
    if "$path" in node:
        p = root / node["$path"]
        if p.is_dir():
            _walk_dir(p, at, out)
        elif p.is_file() and p.name.endswith(SUFFIXES):
            out.append((at, p))
    for name, child in node.items():
        if not name.startswith("$") and isinstance(child, dict):
            _walk_node(child, at + [name], root, out)


def scripts(project_file: str) -> list[tuple[str, int, int]]:
    """(Studio path like "ReplicatedStorage/Shared/Jobs", byte length, checksum) per script."""
    project = Path(project_file)
    tree = json.loads(project.read_text(encoding="utf-8-sig"))["tree"]
    found: list = []
    _walk_node(tree, [], project.parent, found)
    out = []
    for at, file in found:
        data = file.read_bytes()
        out.append(("/".join(at), len(data), checksum(data)))
    return out


def luau(entries: list[tuple[str, int, int]]) -> str:
    """Luau for Studio's Edit DataModel: returns the scripts that differ from disk, or that all match."""
    rows = ",\n".join(f'\t{{ "{p}", {n}, {h} }}' for p, n, h in entries)
    return f"""local want = {{
{rows}
}}
local bad = {{}}
for _, w in want do
	local inst = game
	for name in string.gmatch(w[1], "[^/]+") do
		inst = inst and inst:FindFirstChild(name)
	end
	if not inst or not inst:IsA("LuaSourceContainer") then
		table.insert(bad, w[1] .. ": not in Studio")
	else
		local s = (inst :: any).Source
		local h = 0
		for i = 1, #s do
			h = (h * 31 + string.byte(s, i)) % 4294967296
		end
		if #s ~= w[2] or h ~= w[3] then
			table.insert(bad, `{{w[1]}}: Studio has {{#s}} bytes, disk {{w[2]}}`)
		end
	end
end
return if #bad == 0 then `all {{#want}} scripts match disk` else `{{#bad}} of {{#want}} differ:\\n` .. table.concat(bad, "\\n")
"""


if __name__ == "__main__":  # self-check: python -m mcp_tools.sync
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "src/server/Dispatch").mkdir(parents=True)
        (root / "src/server/Jobs.server.luau").write_bytes(b"print(1)\n")
        (root / "src/server/Dispatch/init.server.luau").write_bytes(b"x")
        (root / "src/server/Dispatch/Callouts.luau").write_bytes(b"return {}")
        (root / "src/server/notes.txt").write_bytes(b"skip")
        (root / "src/Loading.client.luau").write_bytes(b"y")
        (root / "default.project.json").write_text(json.dumps({"tree": {
            "$className": "DataModel",
            "ServerScriptService": {"Server": {"$path": "src/server"}},
            "ReplicatedFirst": {"Loading": {"$path": "src/Loading.client.luau"}},
        }}))
        got = {p: (n, h) for p, n, h in scripts(str(root / "default.project.json"))}
        assert set(got) == {
            "ServerScriptService/Server/Jobs",
            "ServerScriptService/Server/Dispatch",
            "ServerScriptService/Server/Dispatch/Callouts",
            "ReplicatedFirst/Loading",
        }, got
        assert got["ServerScriptService/Server/Dispatch"] == (1, ord("x"))
        assert checksum(b"ab") == 97 * 31 + 98
        assert '"ReplicatedFirst/Loading", 1, 121' in luau(list(scripts(str(root / "default.project.json"))))
    print("sync: ok")
