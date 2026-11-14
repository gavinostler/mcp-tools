"""python tests/test_upload_confirm.py (or pytest): upload_assets uploads nothing unless the user
accepts the confirmation dialog. Nothing here reaches Roblox: upload() is replaced."""
import os
import tempfile

import anyio
from mcp import types
from mcp.client.client import Client

from mcp_tools import assets, server

sent = []


def fake_upload(name, data, ctype, asset_type, key, creator):
    sent.append(name)
    return {"assetId": "1", "assetType": asset_type, "name": name, "moderation": "approved"}


def folder():
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "car.mtl"), "w") as f:
        f.write("newmtl paint\nKd 1 0 0\nmap_Kd paint.png\n")
    with open(os.path.join(d, "car.obj"), "w") as f:
        f.write("mtllib car.mtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\nvt 0 0\nvt 1 0\nvt 0 1\n"
                "usemtl paint\nf 1/1 2/2 3/3\n")
    from PIL import Image
    Image.new("RGB", (4, 4), "red").save(os.path.join(d, "paint.png"))
    return d


CHECK = "car.obj: my own mesh. paint.png: plain red, no text, links or logos."


async def call(d, answer, mode, check=CHECK):
    async def elicit(context, params):
        if answer is None:
            return types.ErrorData(code=-32601, message="elicitation not supported")
        call.message = params.message
        return types.ElicitResult(action=answer[0], content=answer[1])

    async with Client(server.mcp, mode=mode, elicitation_callback=elicit) as c:
        try:
            r = await c.call_tool("upload_assets", {"paths": [d], "tos_check": check, "creator": "123"})
        except Exception as e:  # a client that can't ask gets an error, not an upload
            return True, f"Not uploaded ({e})"
        return r.is_error, r.content[0].text


def test_confirm():
    os.environ["ROBLOX_API_KEY"] = "test"
    assets.upload = fake_upload
    d = folder()
    for mode in ["legacy", "auto"]:  # elicitation/create, and 2026-07-28's InputRequiredResult
        for answer in [None, ("decline", None), ("cancel", None), ("accept", {"upload": False})]:
            err, text = anyio.run(call, d, answer, mode)
            assert err and "Not uploaded" in text, (mode, answer, text)
            assert sent == [], (mode, answer)
        err, text = anyio.run(call, d, ("accept", {"upload": True}), mode)
        assert not err and sent == ["car"], (mode, text)
        sent.clear()
        call.message = None  # a check that skips a texture is refused before the dialog
        err, text = anyio.run(call, d, ("accept", {"upload": True}), mode, "car.obj: my own mesh.")
        assert err and "paint.png" in text and call.message is None and sent == [], (mode, text)
    anyio.run(call, d, ("decline", None), "legacy")
    assert "car.obj (Model)" in call.message and "plain red" in call.message, call.message


if __name__ == "__main__":
    test_confirm()
    print("ok")
