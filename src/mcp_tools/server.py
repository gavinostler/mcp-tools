"""mcp-tools: an MCP server (stdio) for working on a Roblox game from an AI assistant.

Rojo: serve a project in a process that outlives the assistant's session, read its log, and build /
install a Rojo plugin that auto-connects in Edit mode. Studio: import a model through Studio's own
3D Importer, read Studio's log, and take a screenshot of it. Assets: upload files through Open Cloud
with no Studio involved. Files: serve local files to Studio over
http and receive files from it. Windows only (the two log tools and upload_assets work
anywhere). Working files (pid files, logs, the plugin build) go in %LOCALAPPDATA%\\mcp-tools.
"""
import datetime
import functools
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Annotated

import anyio
import anyio.from_thread
import anyio.to_thread
from mcp.server.elicitation import AcceptedElicitation, ElicitationResult
from mcp.server.mcpserver import Context, Elicit, Image, MCPServer, Resolve
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from . import logs

HERE = Path(__file__).parent
ROJO_PS1 = HERE / "rojo.ps1"
LOCAL = Path(os.environ.get("LOCALAPPDATA") or Path.home())
STATE = LOCAL / "mcp-tools" / "rojo"
ROBLOX = LOCAL / "Roblox"
DEFAULT_PORT = 34872

mcp = MCPServer("mcp-tools", instructions="""upload_assets publishes to the user's Roblox account, and Roblox moderates every file, including
the textures inside a model. One flagged image can get the account banned; this has happened.
- Only call upload_assets when the user has asked to upload those specific files. Never to test
  the tool, and never as a step of some other task.
- Look at every image first, textures included, for text, links, logos and watermarks (artist
  credits like "site.com" count as off-platform links). Tell the user what you find.
- Before uploading, prove each file doesn't break Roblox's Terms of Use or Community Standards:
  upload_assets' tos_check must name every file and texture and say what you checked in it.
  If you can't prove it for a file, don't upload that file.
- The user confirms each upload in a dialog, seeing your tos_check. If they decline, don't retry
  or look for another way to upload. For a test or a preview, use studio_import into a local
  place instead.""")


def _need_windows():
    if sys.platform != "win32":
        raise ToolError("this tool needs Windows (only rojo_logs and studio_logs work elsewhere)")


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    # CREATE_NO_WINDOW: a GUI client (Claude Desktop, ...) would otherwise flash a console window
    return subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace",
                          timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW)


def _rojo(command: str, *args: str) -> tuple[int, str]:
    _need_windows()
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROJO_PS1), command,
           "-StateDir", str(STATE), *args]
    try:
        run = _run(cmd, timeout=900)
    except subprocess.TimeoutExpired:
        raise ToolError(f"rojo.ps1 {command} didn't finish within 15 minutes") from None
    return run.returncode, (run.stdout + run.stderr).strip()


def _project(path: str) -> str:
    p = Path(path).expanduser()
    if p.is_dir():
        p = p / "default.project.json"
    if not p.is_file():
        raise ToolError(f"no Rojo project file at {p}")
    return str(p.resolve())


def _studio_running() -> bool:
    return "RobloxStudioBeta.exe" in _run(
        ["tasklist", "/FI", "IMAGENAME eq RobloxStudioBeta.exe", "/NH"], timeout=30).stdout


@mcp.tool()
def rojo_start(project: str, port: int = DEFAULT_PORT) -> str:
    """Serve a Rojo project (`rojo serve`) in a background process that keeps running after this
    session ends. `project`: the project's folder or its .project.json. Does nothing if that
    project is already being served on the port; fails if another project is."""
    code, text = _rojo("start", "-Project", _project(project), "-Port", str(port))
    if code:
        raise ToolError(text)
    return text


@mcp.tool()
def rojo_status(project: str, port: int = DEFAULT_PORT) -> str:
    """Is a Rojo server for this project answering on the port (and did rojo_start start it)?"""
    return _rojo("status", "-Project", _project(project), "-Port", str(port))[1]


@mcp.tool()
def rojo_stop(port: int = DEFAULT_PORT) -> str:
    """Stop the Rojo server rojo_start started on this port (never one it didn't start)."""
    return _rojo("stop", "-Port", str(port))[1]


@mcp.tool()
def rojo_logs(port: int = DEFAULT_PORT, lines: int = 50) -> str:
    """The last `lines` lines printed by the Rojo server rojo_start started on this port (the log
    starts over with each rojo_start). Look here when changes stop syncing: Rojo reports files and
    project changes it can't read (a bad .model.json, invalid JSON) here, not in Studio."""
    log = STATE / f"serve-{port}.log"
    if not log.is_file():
        raise ToolError(f"no log at {log}: rojo_start hasn't served anything on port {port}")
    got = logs.read_lines(log)[-max(lines, 1):]
    return f"{log}:\n" + "\n".join(got) if got else f"{log} is empty"


@mcp.tool()
def rojo_sync_check(project: str, port: int = DEFAULT_PORT) -> str:
    """Is Studio running the scripts on disk? Rojo sometimes misses an edit or crashes, and a
    playtest then runs old code. Returns whether the Rojo server is up, and Luau to run in Studio's
    Edit DataModel (e.g. the Roblox Studio MCP's execute_luau): it checks every script in the
    project against its file (length and checksum) and returns the ones that differ. One that
    differs: make a real content change to its file (touching it isn't enough) or restart Rojo.
    Playtests copy the Edit DataModel when they start, so restart play after a resync."""
    from .sync import luau, scripts

    project_file = _project(project)
    head = [f"rojo: {rojo_status(project, port)}"]
    log = STATE / f"serve-{port}.log"
    if log.is_file() and any("crashed" in line for line in logs.read_lines(log)[-200:]):
        head.append(f"rojo: its log ({log}) says it crashed: rojo_stop, then rojo_start")
    entries = scripts(project_file)
    if not entries:
        raise ToolError(f"no scripts found in {project_file}")
    head.append(f"Run this in Studio (Edit) to compare its {len(entries)} scripts with disk:")
    return "\n".join(head) + "\n\n" + luau(entries)


@mcp.tool()
def rojo_build_plugin() -> str:
    """Build the auto-connect Rojo plugin: the installed Rojo CLI's own version (from GitHub) plus a
    patch that, in Edit mode with Auto Reconnect on, connects every few seconds to a server whose
    project lists the open place in `servePlaceIds`. Needs git and rojo on this machine."""
    code, text = _rojo("build")
    if code:
        raise ToolError(text)
    return text


@mcp.tool()
def rojo_install_plugin(configure_settings: bool = True) -> str:
    """Install the plugin rojo_build_plugin built as Studio's Rojo plugin (the old one is backed up
    to %LOCALAPPDATA%\\Roblox\\PluginBackups). With Studio closed it also turns on Rojo's Auto
    Reconnect and sets Confirmation Behavior to "Unlisted PlaceId", so listed places connect
    without a click. Studio picks the plugin up when it next opens. The project needs
    "servePlaceIds": [<place id>] for auto-connect to happen."""
    _need_windows()
    built = STATE / "out" / "Rojo.rbxm"
    if not built.is_file():
        raise ToolError("no plugin built yet: run rojo_build_plugin first")
    plugins = ROBLOX / "Plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    backups = ROBLOX / "PluginBackups"
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    lines = []
    target = plugins / "Rojo.rbxm"
    if target.is_file():
        backups.mkdir(parents=True, exist_ok=True)
        backup = backups / f"Rojo-{stamp}.rbxm"
        shutil.copy2(target, backup)
        lines.append(f"backed up the old plugin to {backup}")
    shutil.copy2(built, target)
    lines.append(f"installed {target}")
    if not configure_settings:
        return "\n".join(lines)
    if _studio_running():
        lines.append("Studio is open, so its settings weren't changed (it rewrites them on exit): in "
                     "Rojo's settings turn on Auto Reconnect and set Confirmation Behavior to "
                     "\"Unlisted PlaceId\", or run this again with Studio closed")
        return "\n".join(lines)
    changed = 0
    for settings in ROBLOX.glob("*/InstalledPlugins/0/settings.json"):
        try:
            data = json.loads(settings.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as e:
            lines.append(f"skipped {settings}: couldn't read it ({e})")
            continue
        if not isinstance(data, dict) or not any(k.startswith("Rojo_") for k in data):
            continue  # an account that's never run Rojo
        backups.mkdir(parents=True, exist_ok=True)
        shutil.copy2(settings, backups / f"settings-{settings.parents[2].name}-{stamp}.json")
        data["Rojo_autoReconnect"] = True
        data["Rojo_confirmationBehavior"] = "Unlisted PlaceId"
        settings.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        changed += 1
    if changed:
        lines.append(f"turned on Auto Reconnect and \"Unlisted PlaceId\" for {changed} Studio account(s)")
    else:
        lines.append("no Studio account has Rojo settings yet: open Studio with the plugin once, then "
                     "run this again with Studio closed (or set them in Rojo's settings)")
    return "\n".join(lines)


def _studio(fn, *args):
    """Run a studio.* function, turning StudioError into a ToolError."""
    from .studio import StudioError

    try:
        return fn(*args)
    except StudioError as e:
        raise ToolError(str(e)) from None


@mcp.tool()
def studio_windows() -> str:
    """The open Roblox Studio windows, one per open place, with each one's pid and log file. The
    other studio_* tools take a `window` naming one of these; it's needed when more than one is
    open."""
    _need_windows()
    from .studio import find, log_for

    found = find()
    if not found:
        return "Roblox Studio isn't open"
    out = []
    for w in found:
        log = log_for(w.pid, ROBLOX / "logs")
        line = f"{w.place!r}: pid {w.pid}, log {log.name if log else '(not found)'}"
        sharing = [repr(o.place) for o in found if o.pid == w.pid and o != w]
        if sharing:  # then pass a place name, not the pid, as `window`
            line += f" (shares its process and log with {', '.join(sharing)})"
        out.append(line)
    return "\n".join(out)


@mcp.tool()
async def studio_import(paths: list[str], ctx: Context, window: str = "", allow_input: bool = True) -> str:
    """Import files through Roblox Studio's own importer, as a person would (File > Import, then
    Import in the Import Preview if one comes up), leaving the importer's settings as they are:
    meshes (FBX, OBJ, GLB, GLTF), images (BMP, GIF, JPEG, PNG, TGA), audio (OGG, MP3, FLAC, WAV) and
    video (MP4, MOV). `paths`: files, and/or folders to import every such file directly inside.
    They're imported one at a time (about 15-20 s each), stopping at the first failure; everything
    is checked before Studio is touched. The Studio window must be in Edit mode (not playtesting)
    and visible on screen, and is brought to the front. The mouse and keyboard are left alone
    unless Studio ignores the background clicks; then it uses the real ones, or with
    `allow_input` false cancels that import and fails instead. Returning means the importer
    accepted the files: Studio's Import Queue finishes them a few seconds later, so check they
    arrived before using them, and check studio_logs for import errors.
    `window`: which Studio window, if several are open: a place name, part of a title, or a pid
    (see studio_windows)."""
    _need_windows()
    from .studio_import import import_many

    def on_file(i, n, f):
        try:  # progress is a nicety: never let it stop an import
            anyio.from_thread.run(ctx.report_progress, i, n, f"importing {os.path.basename(f)}")
        except Exception:
            pass

    return await anyio.to_thread.run_sync(
        functools.partial(_studio, import_many, paths, window, allow_input, on_file))


class _Confirm(BaseModel):
    upload: bool = Field(description="Upload these files")


async def _ask_upload(paths: list[str], tos_check: str, creator: str = "") -> Elicit[_Confirm]:
    """The dialog the user answers before upload_assets sends anything. Refused before it's shown
    if tos_check doesn't name every file and texture."""
    from .assets import UploadError, prepare, summary, unnamed

    try:
        plan = await anyio.to_thread.run_sync(prepare, paths, creator)
        what = await anyio.to_thread.run_sync(summary, plan)
        missing = await anyio.to_thread.run_sync(unnamed, plan, tos_check)
    except UploadError as e:
        raise ToolError(str(e)) from None
    if missing:
        raise ToolError("Not uploaded: tos_check must prove every file and texture doesn't break "
                        "Roblox's Terms of Use or Community Standards. It doesn't cover: "
                        + ", ".join(missing))
    return Elicit(f"Upload to Roblox?\n\n{what}\n\nTerms of Use check:\n{tos_check.strip()}\n\n"
                  "Roblox moderates every upload, textures included.", _Confirm)


@mcp.tool()
async def upload_assets(paths: list[str], tos_check: str, ctx: Context,
                        confirm: Annotated[ElicitationResult[_Confirm], Resolve(_ask_upload)],
                        creator: str = "") -> str:
    """Upload files as Roblox assets through Open Cloud. This publishes to the user's account and
    every file, textures included, goes through Roblox moderation: a flagged image can get the
    account banned. Only call it when the user asked to upload these specific files.
    `tos_check` must prove each upload doesn't break Roblox's Terms of Use or Community
    Standards: name every file and every texture (by file name) and say what you checked in it.
    Any text, links, URLs, logos, watermarks or contact info (an artist's "site.com" credit counts
    as an off-platform link); real people, violence, gore, sexual content, drugs, hate symbols. Look at each
    image yourself first. If you can't prove a file is fine, leave it out. It's refused if any
    name is missing, and the user sees it in the confirmation dialog.
    Models (FBX, GLB, GLTF, RBXM, OBJ converted to GLB), images (PNG, JPG, BMP, TGA), audio (MP3,
    OGG, WAV, FLAC) and video (MP4, MOV), 20 MB each (video more). `paths`: files, and/or folders
    to upload every such file directly inside (a folder with a mesh leaves out its images, the
    mesh's textures). Returns JSON {path: {assetId, assetType, name, moderation} or {error}}. Then
    put them in the place with the Roblox Studio MCP's insert_asset (assetId, assetType, assetName).
    `creator`: a user id or "group:<id>"; default ROBLOX_CREATOR, else the API key's owner.
    Needs ROBLOX_API_KEY: an Open Cloud key with the Assets API's read and write permissions."""
    from .assets import UploadError, prepare, upload_many

    if not isinstance(confirm, AcceptedElicitation) or not confirm.data.upload:
        raise ToolError("Not uploaded: the user declined. Don't retry or upload another way.")
    try:
        plan = await anyio.to_thread.run_sync(prepare, paths, creator)
    except UploadError as e:
        raise ToolError(str(e)) from None

    def on_done(i, n, f):
        try:  # progress is a nicety: never let it stop an upload
            anyio.from_thread.run(ctx.report_progress, i, n, f"uploaded {os.path.basename(f)}")
        except Exception:
            pass

    out = await anyio.to_thread.run_sync(upload_many, plan, on_done)
    if all("error" in r for r in out.values()):
        raise ToolError(json.dumps(out, indent=1))
    return json.dumps(out, indent=1)


def _studio_log(window: str) -> tuple[Path, str]:
    """The Studio log for `window`, and whose it is."""
    logs_dir = ROBLOX / "logs"
    newest = logs.newest_studio_log(logs_dir)
    if sys.platform != "win32":
        if window:
            raise ToolError("picking a Studio window needs Windows")
    else:
        from .studio import find, log_for, pick

        found = find()
        if found or window:
            target = _studio(pick, window, found)
            log = log_for(target.pid, logs_dir)
            if log:
                return log, str(target)
            if len(found) > 1 or newest is None:
                raise ToolError(f"couldn't tell which log in {logs_dir} is {target}'s")
            return newest, f"{target}, its newest log"
    if newest is None:
        raise ToolError(f"no Studio log in {logs_dir}")
    return newest, "Studio isn't open: the newest log"


@mcp.tool()
def studio_logs(lines: int = 50, pattern: str = "", since_minutes: float = 0, window: str = "") -> str:
    """The end of a Roblox Studio window's log (in %LOCALAPPDATA%\\Roblox\\logs): the Output
    window's prints, warnings and errors plus Studio's own messages (3D importer, plugins, ...). It's
    noisy, so narrow it down: `pattern` keeps lines matching a case-insensitive regex (e.g.
    "error|warn|fail"), `since_minutes` keeps lines from the last that many minutes. Returns the
    last `lines` lines left, times in UTC. After studio_import, check here for import errors. With
    Studio closed, reads the newest log.
    `window`: which Studio window, if several are open: a place name, part of a title, or a pid
    (see studio_windows)."""
    log, whose = _studio_log(window)
    try:
        rx = re.compile(pattern, re.IGNORECASE) if pattern else None
    except re.error as e:
        raise ToolError(f"bad pattern {pattern!r}: {e}") from None
    since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=since_minutes)
             if since_minutes > 0 else None)
    got = logs.studio_lines(log, rx, since)
    shown = got[-max(lines, 1):]
    written = datetime.datetime.fromtimestamp(log.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    head = f"{log.name} ({whose}; last written {written} local time): {len(got)} matching lines"
    head += f", the last {len(shown)} shown" if len(shown) < len(got) else ""
    return head + ("\n" + "\n".join(shown) if shown else "")


@mcp.tool()
def studio_screenshot(window: str = "") -> list[str | Image]:
    """A screenshot of a Roblox Studio window (the whole window: viewport, Explorer, Output, any
    dialogs), to check what a change or import actually looks like. Works with Studio behind other
    windows without taking the focus, unless the capture comes back blank: then it brings Studio to
    the front.
    `window`: which Studio window, if several are open: a place name, part of a title, or a pid
    (see studio_windows)."""
    _need_windows()
    from .studio_screenshot import capture

    target, jpeg = _studio(capture, window)
    return [str(target), Image(data=jpeg, format="jpeg")]


@mcp.tool()
def serve_files(paths: list[str], minutes: float = 10) -> str:
    """Serve local files over http on 127.0.0.1 for `minutes`, for tools that take URLs rather than
    paths (the Roblox Studio MCP's upload_image). Returns one URL per file, as JSON {path: url}.
    The server lives in this process: nothing to start or stop."""
    from . import files

    try:
        return json.dumps(files.serve(paths, minutes), indent=1)
    except FileNotFoundError as e:
        raise ToolError(str(e)) from None


@mcp.tool()
def receive_files(folder: str, minutes: float = 10) -> str:
    """A URL that writes files into `folder` for `minutes`: an HTTP PUT to <url>/<relative path>
    saves the body there (subfolders made as needed; .rbxm .rbxmx .txt .json .png only; nothing
    outside the folder). Send the header "Content-Encoding: base64" with a base64 body (what a
    Roblox Studio script can send most safely: HttpService and EncodingService:Base64Encode).
    Studio's HttpService must be allowed to reach 127.0.0.1 (the Assistant's HTTP allowlist)."""
    from . import files

    try:
        return files.receive(folder, minutes)
    except NotADirectoryError as e:
        raise ToolError(str(e)) from None


def main():
    mcp.run()


if __name__ == "__main__":
    main()
