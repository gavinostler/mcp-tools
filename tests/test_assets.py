"""python tests/test_assets.py (or pytest): folder expansion for upload_assets / studio_import."""
import os
import tempfile

from mcp_tools.assets import TYPES, expand


def test_expand():
    d = tempfile.mkdtemp()
    for n in ["m.fbx", "tex.png", "notes.txt", "song.mp3"]:
        open(os.path.join(d, n), "w").close()
    names = lambda fs: [os.path.basename(f) for f in fs]
    assert names(expand([d], set(TYPES))) == ["m.fbx", "song.mp3"]  # a mesh's textures stay out
    assert names(expand([os.path.join(d, "tex.png")], set(TYPES))) == ["tex.png"]  # unless named
    os.remove(os.path.join(d, "m.fbx"))
    assert names(expand([d, d], set(TYPES))) == ["song.mp3", "tex.png"]
    for bad in ["nope.png", "notes.txt"]:
        try:
            expand([os.path.join(d, bad)], set(TYPES))
            raise AssertionError(f"{bad} should fail")
        except ValueError:
            pass


if __name__ == "__main__":
    test_expand()
    print("ok")
