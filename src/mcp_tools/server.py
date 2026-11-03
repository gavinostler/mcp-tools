"""mcp-tools: an MCP server (stdio) for working on a Roblox game from an AI assistant.

Rojo: serve a project in a process that outlives the assistant's session, and build / install a
Rojo plugin that auto-connects in Edit mode. Studio: import a model through Studio's own 3D
Importer. Windows only. Working files (pid files, logs, the plugin build) go in
%LOCALAPPDATA%\\mcp-tools.
"""
import datetime
import json
import os
import shutil
import subprocess
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

HERE = Path(__file__).parent
ROJO_PS1 = HERE / "rojo.ps1"
LOCAL = Path(os.environ.get("LOCALAPPDATA") or Path.home())
STATE = LOCAL / "mcp-tools" / "rojo"
ROBLOX = LOCAL / "Roblox"
DEFAULT_PORT = 34872

mcp = MCPServer("mcp-tools")


def _rojo(command: str, *args: str) -> tuple[int, str]:
    run = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROJO_PS1), command,
         "-StateDir", str(STATE), *args],
        capture_output=True, text=True, timeout=900,
    )
    return run.returncode, (run.stdout + run.stderr).strip()


def _project(path: str) -> str:
    p = Path(path).expanduser()
    if p.is_dir():
        p = p / "default.project.json"
    if not p.is_file():
        raise ToolError(f"no Rojo project file at {p}")
    return str(p.resolve())


def _studio_running() -> bool:
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq RobloxStudioBeta.exe", "/NH"],
                         capture_output=True, text=True).stdout
    return "RobloxStudioBeta.exe" in out


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
        shutil.copy2(target, backups / f"Rojo-{stamp}.rbxm")
        lines.append(f"backed up the old plugin to {backups / f'Rojo-{stamp}.rbxm'}")
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
        data = json.loads(settings.read_text(encoding="utf-8"))
        if not any(k.startswith("Rojo_") for k in data):
            continue  # an account that's never run Rojo
        backups.mkdir(parents=True, exist_ok=True)
        shutil.copy2(settings, backups / f"settings-{settings.parents[2].name}-{stamp}.json")
        data["Rojo_autoReconnect"] = True
        data["Rojo_confirmationBehavior"] = "Unlisted PlaceId"
        settings.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        changed += 1
    lines.append(f"turned on Auto Reconnect and \"Unlisted PlaceId\" for {changed} Studio account(s)"
                 if changed else "no Studio account has Rojo settings yet: open Studio with the plugin once, "
                 "then run this again with Studio closed (or set them in Rojo's settings)")
    return "\n".join(lines)


@mcp.tool()
def studio_import(path: str) -> str:
    """Import a model file (FBX, OBJ, GLB or GLTF) through Roblox Studio's own 3D Importer, as a
    person would (File > Import, then Import), leaving the importer's settings as they are. Needs
    exactly one Studio window, in Edit mode (not playtesting), visible on screen; it takes the
    mouse and keyboard for about 15 s. Returning means the importer accepted the file: Studio's
    Import Queue finishes it a few seconds later, so check the model arrived before using it.
    Leave a few seconds between imports."""
    from .studio_import import ImportFailed, import_model

    try:
        return import_model(path)
    except ImportFailed as e:
        raise ToolError(f"import failed: {e}") from None


def main():
    mcp.run()


if __name__ == "__main__":
    main()
