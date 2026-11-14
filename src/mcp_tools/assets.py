"""Upload files as Roblox assets through Open Cloud's Assets API: no Studio window, focus or mouse
involved. The mcp-tools `upload_assets` tool, or:
    python -m mcp_tools.assets <file or folder>... [--creator ID | --creator group:ID]
Needs an Open Cloud API key with the Assets API's read and write permissions in ROBLOX_API_KEY.
The creator defaults to ROBLOX_CREATOR, else the key's owner. Images upload as Image assets (ids
usable as textures). It lists what it will upload and asks for "y" at the terminal. An OBJ is converted to GLB (split to fit 20 MB) and comes back with Luau that
puts all of it in one Model; insert other models with the Roblox Studio MCP's insert_asset.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

API = "https://apis.roblox.com"
# extension -> (assetType, content type), from the Assets API's supported file types
TYPES = {
    ".fbx": ("Model", "model/fbx"), ".glb": ("Model", "model/gltf-binary"),
    ".gltf": ("Model", "model/gltf+json"), ".rbxm": ("Model", "model/x-rbxm"),
    ".rbxmx": ("Model", "model/x-rbxm"),
    ".png": ("Image", "image/png"), ".jpg": ("Image", "image/jpeg"), ".jpeg": ("Image", "image/jpeg"),
    ".bmp": ("Image", "image/bmp"), ".tga": ("Image", "image/tga"),
    ".mp3": ("Audio", "audio/mpeg"), ".ogg": ("Audio", "audio/ogg"), ".wav": ("Audio", "audio/wav"),
    ".flac": ("Audio", "audio/flac"),
    ".mp4": ("Video", "video/mp4"), ".mov": ("Video", "video/mov"),
    ".obj": ("Model", "model/gltf-binary"),  # converted to GLB first (obj.py)
}
MESHES = {".fbx", ".obj", ".gltf", ".glb"}
IMAGES = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tga"}
MAX_BYTES = 20 * 1024 * 1024  # per file, except video
POLL_FOR = 120  # s to wait for Roblox to finish processing an upload
WORKERS = 4


class UploadError(Exception):
    pass


def ext(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def expand(paths: list[str], extensions: set[str]) -> list[str]:
    """Files to use: each path that's a file, and every file with one of `extensions` directly in
    each folder, in order and once each. A folder holding a mesh keeps its images out: they're the
    mesh's textures. Raises ValueError for a missing or unsupported file, or nothing at all."""
    files = []
    for p in paths:
        full = os.path.abspath(os.path.expanduser(p))
        if os.path.isdir(full):
            found = sorted(os.path.join(full, f) for f in os.listdir(full)
                           if ext(f) in extensions and os.path.isfile(os.path.join(full, f)))
            if any(ext(f) in MESHES for f in found):
                found = [f for f in found if ext(f) not in IMAGES]
            files += found
        elif not os.path.isfile(full):
            raise ValueError(f"no such file: {full}")
        elif ext(full) not in extensions:
            raise ValueError(f"takes {' '.join(sorted(extensions))} files, not {full}")
        else:
            files.append(full)
    files = list(dict.fromkeys(files))
    if not files:
        raise ValueError(f"nothing to use in {paths}")
    return files


def _call(method: str, path: str, key: str, body: bytes | None = None, ctype: str = "") -> dict:
    req = urllib.request.Request(API + path, data=body, method=method, headers={"x-api-key": key})
    if ctype:
        req.add_header("Content-Type", ctype)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            if e.code == 429 and attempt < 3:  # rate limited
                time.sleep(float(e.headers.get("Retry-After") or 2 ** (attempt + 1)))
                continue
            try:
                text = json.loads(text).get("message") or text
            except (ValueError, AttributeError):
                pass
            if e.code in (401, 403):
                text += " (check ROBLOX_API_KEY and its Assets API read/write permissions)"
            raise UploadError(f"HTTP {e.code}: {text.strip()}") from None
        except urllib.error.URLError as e:
            raise UploadError(f"couldn't reach {API}: {e.reason}") from None
    raise AssertionError("unreachable")


def key_from_env() -> str:
    key = os.environ.get("ROBLOX_API_KEY", "").strip()
    if not key:
        raise UploadError("set ROBLOX_API_KEY to an Open Cloud API key with the Assets API's read "
                          "and write permissions (create.roblox.com > Open Cloud > API Keys)")
    return key


def creator_for(key: str, creator: str = "") -> dict:
    """{"userId": ..} or {"groupId": ..} from `creator` ("123" or "group:123"), else
    ROBLOX_CREATOR, else the user who owns the key."""
    creator = (creator or os.environ.get("ROBLOX_CREATOR", "")).strip().lower()
    if creator.startswith("group:") and creator[6:].isdigit():
        return {"groupId": creator[6:]}
    if creator.isdigit():
        return {"userId": creator}
    if creator:
        raise UploadError(f"creator must be a user id or group:<id>, not {creator!r}")
    info = _call("POST", "/api-keys/v1/introspect", key, json.dumps({"apiKey": key}).encode(),
                 "application/json")
    owner = info.get("authorizedUserId")
    if not owner:
        raise UploadError("couldn't tell who owns this API key: pass creator (or set ROBLOX_CREATOR)")
    return {"userId": str(owner)}


def multipart(fields: dict[str, str], name: str, filename: str, ctype: str, data: bytes) -> tuple[bytes, str]:
    b = uuid.uuid4().hex
    parts = [f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
             for k, v in fields.items()]
    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                 f'Content-Type: {ctype}\r\n\r\n'.encode() + data + f"\r\n--{b}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={b}"


def upload(name: str, data: bytes, ctype: str, asset_type: str, key: str, creator: dict) -> dict:
    """Upload one file's bytes; returns {"assetId", "assetType", "name", "moderation"}."""
    request = {"assetType": asset_type, "displayName": name[:50], "description": "",
               "creationContext": {"creator": creator}}
    body, form = multipart({"request": json.dumps(request)}, "fileContent", name, ctype, data)
    op = _call("POST", "/assets/v1/assets", key, body, form)
    end = time.time() + POLL_FOR
    while not op.get("done"):
        if time.time() > end:
            raise UploadError(f"Roblox still processing after {POLL_FOR}s ({op.get('path')})")
        time.sleep(1)
        op = _call("GET", "/assets/v1/" + op["path"], key)
    if "error" in op:
        raise UploadError(op["error"].get("message") or json.dumps(op["error"]))
    r = op.get("response", {})
    state = r.get("moderationResult", {}).get("moderationState", "")
    return {"assetId": r.get("assetId"), "assetType": asset_type, "name": name[:50],
            "moderation": state.removeprefix("MODERATION_STATE_").lower()}


def insert_luau(name: str, ids: list[str]) -> str:
    """Luau for Studio's Edit DataModel: every part of the uploaded models `ids` in one Model in
    Workspace, where the file put them (insert_asset would move each model on its own)."""
    return f"""local model = Instance.new("Model")
model.Name = {json.dumps(name)}
for _, id in {{{", ".join(ids)}}} do
	local got = game:GetService("InsertService"):LoadAsset(id)
	for _, d in got:GetDescendants() do
		if d:IsA("BasePart") then d.Parent = model end
	end
	got:Destroy()
end
model.Parent = workspace
return #model:GetChildren() .. " parts in " .. model:GetFullName()"""


def jobs_for(path: str) -> list[tuple[str, bytes, str, str]]:
    """(name, bytes, content type, asset type) to upload for `path`: an OBJ becomes one or more
    GLBs under the size limit. Raises UploadError if it's too big or can't be converted."""
    asset_type, ctype = TYPES[ext(path)]
    if ext(path) == ".obj":
        from .obj import to_glbs

        try:
            return [(n, d, ctype, asset_type) for n, d in to_glbs(path)]
        except (ValueError, OSError, IndexError) as e:
            raise UploadError(f"couldn't convert {path} to GLB: {e}") from None
    if asset_type != "Video" and os.path.getsize(path) > MAX_BYTES:
        raise UploadError(f"{path} is over the Assets API's 20 MB limit")
    with open(path, "rb") as f:
        return [(os.path.splitext(os.path.basename(path))[0], f.read(), ctype, asset_type)]


def prepare(paths: list[str], creator: str = "") -> dict:
    """Everything an upload of `paths` needs, checked, with nothing sent yet: {"todo": {path:
    jobs}, "key", "creator"}. Raises UploadError for bad paths, a file too big, or no key."""
    try:
        files = expand(paths, set(TYPES))
    except ValueError as e:
        raise UploadError(str(e)) from None
    todo = {f: jobs_for(f) for f in files}
    key = key_from_env()
    return {"todo": todo, "key": key, "creator": creator_for(key, creator)}


def textures(path: str) -> list[str]:
    """Images that go up with the model at `path` (Roblox makes each one an asset): an OBJ's
    texture files, or a GLB / GLTF's images by name. [] for anything else."""
    if ext(path) == ".obj":
        from .obj import parse

        mats = parse(path)[4]
        return sorted({m["map_Kd"] for m in mats.values()
                       if m.get("map_Kd") and os.path.isfile(m["map_Kd"])})
    if ext(path) not in (".glb", ".gltf"):
        return []
    try:
        with open(path, "rb") as f:
            data = f.read()
        if ext(path) == ".glb":
            n = int.from_bytes(data[12:16], "little")
            data = data[20:20 + n]
        images = json.loads(data).get("images", [])
    except (OSError, ValueError):
        return []
    return [i.get("name") or i.get("uri", "")[:60] or f"image {n}" for n, i in enumerate(images)]


def unnamed(plan: dict, text: str) -> list[str]:
    """The files and textures in `plan` whose names `text` doesn't mention (case-insensitive)."""
    text = text.lower()
    names = []
    for f in plan["todo"]:
        names.append(os.path.basename(f))
        names += [os.path.basename(t) for t in textures(f)]
    return [n for n in dict.fromkeys(names) if n.lower() not in text]


def summary(plan: dict) -> str:
    """What `plan` uploads, a line per file and each model's textures under it."""
    lines = []
    for f, jobs in plan["todo"].items():
        kind = jobs[0][3] + (f", {len(jobs)} files" if len(jobs) > 1 else "")
        lines.append(f"{os.path.basename(f)} ({kind})")
        tex = textures(f)
        if tex:
            lines.append(f"  + {len(tex)} textures:")
            lines += [f"    {os.path.basename(t)}" for t in tex]
        elif ext(f) == ".fbx":
            lines.append("  + any textures embedded in it")
    who = plan["creator"]
    lines.append(f"To group {who['groupId']}" if "groupId" in who else f"To user {who['userId']}")
    return "\n".join(lines)


def upload_many(plan: dict, on_done=None) -> dict:
    """Upload what prepare() planned, a few at a time. Returns {path: result or {"error": ...}};
    an OBJ split into several GLBs gets "assetIds" and "luau" (insert_luau).
    on_done(done, count, path) is called as each upload finishes."""
    todo, key, who = plan["todo"], plan["key"], plan["creator"]
    count = sum(len(j) for j in todo.values())
    done = {f: [None] * len(j) for f, j in todo.items()}
    finished = 0
    with ThreadPoolExecutor(WORKERS) as pool:
        futures = {pool.submit(upload, *job, key, who): (f, i)
                   for f, jobs in todo.items() for i, job in enumerate(jobs)}
        for fut in as_completed(futures):
            f, i = futures[fut]
            try:
                done[f][i] = fut.result()
            except UploadError as e:
                done[f][i] = {"error": str(e), "name": todo[f][i][0]}
            finished += 1
            if on_done:
                on_done(finished, count, f)
    out = {}
    for f, parts in done.items():
        if ext(f) != ".obj":
            out[f] = parts[0]
            continue
        bad = [f"{p['name']}: {p['error']}" for p in parts if "error" in p]
        name = os.path.splitext(os.path.basename(f))[0]
        ids = [p["assetId"] for p in parts if "error" not in p]
        out[f] = {"error": "; ".join(bad), "uploaded": ids} if bad else {
            "assetIds": ids, "assetType": "Model", "name": name,
            "moderation": ",".join(sorted({p["moderation"] for p in parts})),
            "luau": insert_luau(name, ids)}
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(prog="python -m mcp_tools.assets",
                                 description="Upload files as Roblox assets through Open Cloud.")
    ap.add_argument("paths", nargs="+", help="files, or folders to upload every supported file in")
    ap.add_argument("--creator", default="", help="user id or group:<id> (default: the key's owner)")
    a = ap.parse_args()
    try:
        plan = prepare(a.paths, a.creator)
        print(summary(plan) + "\nRoblox moderates every upload, textures included.", file=sys.stderr)
        # Only a person at a terminal can say yes: piped or scripted input is refused.
        if not sys.stdin.isatty() or input("Upload? [y/N] ").strip().lower() != "y":
            sys.exit("not uploaded")
        print(json.dumps(upload_many(plan, lambda i, n, f: print(f"[{i}/{n}] {f}", file=sys.stderr)),
                         indent=1))
    except UploadError as e:
        sys.exit(f"upload failed: {e}")
