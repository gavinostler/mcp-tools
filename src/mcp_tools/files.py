"""A local HTTP server for moving files between this machine and Roblox Studio.

serve(paths, minutes) -> {path: url}: GET URLs for local files, e.g. for the Roblox Studio MCP's
upload_image, which takes http URLs (the mcp-tools `serve_files` tool).
receive(folder, minutes) -> url: files PUT to <url>/<relative path> are written under `folder`,
e.g. by a Studio script with HttpService (the `receive_files` tool). The body is the file, or its
base64 with the header "Content-Encoding: base64" (Studio strings carry text more safely).

One server per process, on 127.0.0.1 only, started on first use; each serve / receive gets a
random token in its URLs and stops working after `minutes`.

    python -m mcp_tools.files   # self-check
"""
import base64
import http.server
import os
import secrets
import threading
import time
import urllib.parse
from pathlib import Path

PORT = 34880  # tried first, then any free port
RECEIVE_TYPES = {".rbxm", ".rbxmx", ".txt", ".json", ".png"}
MAX_UPLOAD = 64 * 1024 * 1024

_lock = threading.Lock()
_server: http.server.ThreadingHTTPServer | None = None
_served: dict[str, tuple[Path, float]] = {}  # token/name -> file, expiry
_receivers: dict[str, tuple[Path, float]] = {}  # token -> folder, expiry


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):  # quiet: stdio is the MCP channel
        pass

    def _parts(self):
        return [urllib.parse.unquote(p) for p in urllib.parse.urlsplit(self.path).path.split("/") if p]

    def do_GET(self):
        parts = self._parts()
        entry = _served.get("/".join(parts[1:3])) if len(parts) == 3 and parts[0] == "f" else None
        if not entry or entry[1] < time.time() or not entry[0].is_file():
            return self.send_error(404)
        data = entry[0].read_bytes()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_PUT(self):
        parts = self._parts()
        recv = _receivers.get(parts[1]) if len(parts) >= 3 and parts[0] == "r" else None
        if not recv or recv[1] < time.time():
            return self.send_error(404)
        folder = recv[0]
        target = folder.joinpath(*parts[2:]).resolve()
        if folder not in target.parents or target.suffix.lower() not in RECEIVE_TYPES:
            return self.send_error(403, "outside the folder, or not an allowed file type")
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            return self.send_error(413)
        body = self.rfile.read(length)
        if (self.headers.get("Content-Encoding") or "").lower() == "base64":
            body = base64.b64decode(body)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")


def _ensure() -> int:
    global _server
    with _lock:
        if _server is None:
            for port in (PORT, 0):
                try:
                    _server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
                    break
                except OSError:
                    continue
            threading.Thread(target=_server.serve_forever, daemon=True).start()
        return _server.server_address[1]


def serve(paths: list[str], minutes: float = 10) -> dict[str, str]:
    files = [Path(p).expanduser().resolve() for p in paths]
    missing = [str(f) for f in files if not f.is_file()]
    if missing:
        raise FileNotFoundError("not files: " + ", ".join(missing))
    port, token, until = _ensure(), secrets.token_urlsafe(8), time.time() + minutes * 60
    out = {}
    for f in files:
        _served[f"{token}/{f.name}"] = (f, until)
        out[str(f)] = f"http://127.0.0.1:{port}/f/{token}/{urllib.parse.quote(f.name)}"
    return out


def receive(folder: str, minutes: float = 10) -> str:
    root = Path(folder).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"not a folder: {root}")
    port, token = _ensure(), secrets.token_urlsafe(8)
    _receivers[token] = (root, time.time() + minutes * 60)
    return f"http://127.0.0.1:{port}/r/{token}"


if __name__ == "__main__":  # self-check: serve a file, receive one (plain and base64), refuse escapes
    import tempfile
    import urllib.request

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp, "a.png")
        src.write_bytes(b"\x89PNGdata")
        url = serve([str(src)])[str(src.resolve())]
        assert urllib.request.urlopen(url).read() == b"\x89PNGdata"
        dest = Path(tmp, "in")
        dest.mkdir()
        base = receive(str(dest))

        def put(path, body, b64=False):
            req = urllib.request.Request(base + "/" + path, data=body, method="PUT",
                                         headers={"Content-Encoding": "base64"} if b64 else {})
            try:
                return urllib.request.urlopen(req).status
            except urllib.error.HTTPError as e:
                return e.code

        assert put("x/kit.rbxm", b"\x00\x01binary") == 200
        assert (dest / "x" / "kit.rbxm").read_bytes() == b"\x00\x01binary"
        assert put("kit.txt", base64.b64encode(b"hello"), b64=True) == 200
        assert (dest / "kit.txt").read_bytes() == b"hello"
        assert put("..%2F..%2Fescape.txt", b"no") == 403
        assert put("evil.exe", b"no") == 403
        bad = urllib.request.Request(url.replace("/f/", "/f/x"), method="GET")
        try:
            urllib.request.urlopen(bad)
            raise AssertionError("served an unknown token")
        except urllib.error.HTTPError as e:
            assert e.code == 404
    print("files: ok")
