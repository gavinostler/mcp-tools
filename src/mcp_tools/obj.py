"""OBJ (+ MTL and its textures) to GLB, split into files under a size limit: the Assets API takes
GLB but not OBJ, and at most 20 MB per file.

    to_glbs(path, max_bytes) -> [(name, glb bytes), ...]
    python -m mcp_tools.obj <file.obj> [out_dir]
One glTF node per OBJ object (`o`, else `g`), one primitive per material, base colour from Kd and
map_Kd (embedded). Objects are packed into files in order; a file never splits an object.
"""
import io
import json
import os
import struct
import sys
from array import array

LIMIT = 19 * 1024 * 1024  # under the Assets API's 20 MB, leaving room for the JSON


def parse_mtl(path: str) -> dict:
    mats, cur = {}, None
    base = os.path.dirname(path)
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            p = line.split()
            if not p:
                continue
            if p[0] == "newmtl":
                cur = mats.setdefault(" ".join(p[1:]), {})
            elif cur is None:
                continue
            elif p[0] == "Kd" and len(p) >= 4:
                cur["Kd"] = [float(x) for x in p[1:4]]
            elif p[0] == "d" and len(p) >= 2:
                cur["d"] = float(p[1])
            elif p[0] == "map_Kd" and len(p) >= 2:
                cur["map_Kd"] = os.path.join(base, p[-1])  # the last token: options come first
    return mats


def parse(path: str):
    """-> (v, vt, vn, objects, materials); objects is {name: {material: [corner, ...]}} with
    three corners per triangle, each corner (v, vt, vn) as 0-based indices (-1: none)."""
    v, vt, vn = [], [], []
    objects, mats = {}, {}
    obj, mat = os.path.splitext(os.path.basename(path))[0], ""
    seen_o = False
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            p = line.split()
            if not p:
                continue
            k = p[0]
            if k == "v":
                v.append((float(p[1]), float(p[2]), float(p[3])))
            elif k == "vt":
                vt.append((float(p[1]), float(p[2]) if len(p) > 2 else 0.0))
            elif k == "vn":
                vn.append((float(p[1]), float(p[2]), float(p[3])))
            elif k == "f":
                cs = []
                for tok in p[1:]:
                    a = tok.split("/")
                    i = int(a[0])
                    t = int(a[1]) if len(a) > 1 and a[1] else 0
                    n = int(a[2]) if len(a) > 2 and a[2] else 0
                    cs.append((i - 1 if i > 0 else len(v) + i,
                               (t - 1 if t > 0 else len(vt) + t) if t else -1,
                               (n - 1 if n > 0 else len(vn) + n) if n else -1))
                tris = objects.setdefault(obj, {}).setdefault(mat, [])
                for j in range(1, len(cs) - 1):  # fan: faces are convex in practice
                    tris += (cs[0], cs[j], cs[j + 1])
            elif k == "o" or (k == "g" and not seen_o):
                seen_o = seen_o or k == "o"
                obj = " ".join(p[1:]) or obj
            elif k == "usemtl":
                mat = " ".join(p[1:])
            elif k == "mtllib":
                mtl = os.path.join(os.path.dirname(path), " ".join(p[1:]))
                if os.path.isfile(mtl):
                    mats.update(parse_mtl(mtl))
    return v, vt, vn, objects, mats


def primitive(corners, v, vt, vn) -> dict:
    """Packed arrays for one material's triangles: shared corners become one vertex."""
    has_uv = all(c[1] >= 0 for c in corners)
    has_n = all(c[2] >= 0 for c in corners)
    pos, nor, uv, idx = array("f"), array("f"), array("f"), array("I")
    seen = {}
    for c in corners:
        k = seen.get(c)
        if k is None:
            k = seen[c] = len(seen)
            x, y, z = v[c[0]]
            pos.extend((-x, y, -z))  # Roblox turns glTF 180 degrees about Y: undo it, keep OBJ coordinates
            if has_n:
                x, y, z = vn[c[2]]
                nor.extend((-x, y, -z))
            if has_uv:
                u, w = vt[c[1]]
                uv.extend((u, 1.0 - w))  # glTF's v runs top to bottom
        idx.append(k)
    return {"pos": pos, "nor": nor if has_n else None, "uv": uv if has_uv else None, "idx": idx}


def texture(path: str) -> tuple[bytes, str] | None:
    """The image as PNG or JPEG bytes (others converted to PNG), or None if it can't be read."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    ext = os.path.splitext(path)[1].lower()
    if ext == ".png":
        return data, "image/png"
    if ext in (".jpg", ".jpeg"):
        return data, "image/jpeg"
    try:
        from PIL import Image

        out = io.BytesIO()
        Image.open(io.BytesIO(data)).save(out, "PNG")
        return out.getvalue(), "image/png"
    except Exception:
        return None


def glb(root: str, objs: list[tuple[str, list[tuple[str, dict]]]], mats: dict, images: dict) -> bytes:
    """A GLB of `objs` [(name, [(material, primitive), ...])] under one node named `root`;
    images {path: (bytes, mime)}."""
    bin_, views, accessors = bytearray(), [], []
    g = {"asset": {"version": "2.0", "generator": "mcp-tools"}, "scene": 0, "scenes": [{"nodes": [0]}],
         "nodes": [{"name": root, "children": []}], "meshes": [], "materials": [], "bufferViews": views, "accessors": accessors}

    def view(data: bytes, target=None) -> int:
        while len(bin_) % 4:
            bin_.append(0)
        views.append({"buffer": 0, "byteOffset": len(bin_), "byteLength": len(data)})
        if target:
            views[-1]["target"] = target
        bin_.extend(data)
        return len(views) - 1

    def accessor(arr: array, kind: str, n: int, **extra) -> int:
        ctype = 5125 if arr.typecode == "I" else 5126
        accessors.append({"bufferView": view(arr.tobytes(), 34963 if ctype == 5125 else 34962),
                          "componentType": ctype, "count": len(arr) // n, "type": kind, **extra})
        return len(accessors) - 1

    mat_index, img_index = {}, {}
    for name, prims in objs:
        out = []
        for mname, pr in prims:
            if mname not in mat_index:
                m = mats.get(mname, {})
                pbr = {"baseColorFactor": [*m.get("Kd", [1, 1, 1]), m.get("d", 1.0)],
                       "metallicFactor": 0.0, "roughnessFactor": 1.0}
                tex = m.get("map_Kd")
                if tex in images:
                    if tex not in img_index:
                        data, mime = images[tex]
                        g.setdefault("images", []).append({"bufferView": view(data), "mimeType": mime,
                                                           "name": os.path.basename(tex)})
                        g.setdefault("textures", []).append({"source": len(g["images"]) - 1})
                        img_index[tex] = len(g["textures"]) - 1
                    pbr["baseColorTexture"] = {"index": img_index[tex]}
                g["materials"].append({"name": mname or "default", "pbrMetallicRoughness": pbr})
                if m.get("d", 1.0) < 1:
                    g["materials"][-1]["alphaMode"] = "BLEND"
                mat_index[mname] = len(g["materials"]) - 1
            p = pr["pos"]
            xs, ys, zs = p[0::3], p[1::3], p[2::3]
            attrs = {"POSITION": accessor(p, "VEC3", 3, min=[min(xs), min(ys), min(zs)],
                                          max=[max(xs), max(ys), max(zs)])}
            if pr["nor"] is not None:
                attrs["NORMAL"] = accessor(pr["nor"], "VEC3", 3)
            if pr["uv"] is not None:
                attrs["TEXCOORD_0"] = accessor(pr["uv"], "VEC2", 2)
            out.append({"attributes": attrs, "indices": accessor(pr["idx"], "SCALAR", 1),
                        "material": mat_index[mname]})
        g["meshes"].append({"name": name, "primitives": out})
        g["nodes"].append({"name": name, "mesh": len(g["meshes"]) - 1})
        g["nodes"][0]["children"].append(len(g["nodes"]) - 1)
    while len(bin_) % 4:
        bin_.append(0)
    g["buffers"] = [{"byteLength": len(bin_)}]
    js = json.dumps(g, separators=(",", ":")).encode()
    js += b" " * (-len(js) % 4)
    total = 12 + 8 + len(js) + 8 + len(bin_)
    return (struct.pack("<III", 0x46546C67, 2, total) + struct.pack("<II", len(js), 0x4E4F534A) + js
            + struct.pack("<II", len(bin_), 0x004E4942) + bytes(bin_))


def size(pr: dict) -> int:
    return sum(len(a) * 4 for a in (pr["pos"], pr["nor"], pr["uv"], pr["idx"]) if a is not None)


def to_glbs(path: str, max_bytes: int = LIMIT) -> list[tuple[str, bytes]]:
    """The OBJ at `path` as GLBs of at most `max_bytes` each: [(name, bytes)], named after the
    file (with -1, -2, ... when split). Raises ValueError if it has no faces or one object (with
    its textures) is bigger than max_bytes on its own."""
    v, vt, vn, objects, mats = parse(path)
    if not objects:
        raise ValueError(f"no faces in {path}")
    images = {}
    for m in mats.values():
        t = m.get("map_Kd")
        if t and t not in images:
            got = texture(t)
            if got:
                images[t] = got
    built = []  # (name, [(material, primitive)], geometry bytes, textures used)
    for name, by_mat in objects.items():
        prims = [(m, primitive(c, v, vt, vn)) for m, c in by_mat.items() if c]
        texs = {mats.get(m, {}).get("map_Kd") for m, _ in prims} & images.keys()
        built.append((name, prims, sum(size(p) for _, p in prims) + 2048, texs))
    # Fill files in order; an object only adds the bytes of textures the file doesn't have yet.
    files, cur, cur_size, cur_tex = [], [], 0, set()
    for name, prims, geo, texs in built:
        add = geo + sum(len(images[t][0]) for t in texs - cur_tex)
        if cur and cur_size + add > max_bytes:
            files.append(cur)
            cur, cur_size, cur_tex = [], 0, set()
            add = geo + sum(len(images[t][0]) for t in texs)
        if add > max_bytes:
            # ponytail: one object over the limit isn't split; split its triangles if this happens
            raise ValueError(f"object {name!r} in {path} is over {max_bytes // 2**20} MB on its own")
        cur.append((name, prims))
        cur_size += add
        cur_tex |= texs
    files.append(cur)
    stem = os.path.splitext(os.path.basename(path))[0]
    names = [stem] if len(files) == 1 else [f"{stem}-{i + 1}" for i in range(len(files))]
    return [(n, glb(n, f, mats, images)) for n, f in zip(names, files)]


if __name__ == "__main__":
    src = sys.argv[1]
    out_dir = sys.argv[2] if len(sys.argv) > 2 else os.path.dirname(os.path.abspath(src))
    for name, data in to_glbs(src):
        dest = os.path.join(out_dir, name + ".glb")
        with open(dest, "wb") as f:
            f.write(data)
        print(f"{dest}: {len(data) / 2**20:.1f} MB")
