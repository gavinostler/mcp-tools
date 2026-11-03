# mcp-tools

Small Windows tools that let an AI coding assistant (Claude Code, alongside the Roblox Studio
MCP server) work on a Roblox game without a person clicking things in Studio. They're plain
command-line tools: the assistant runs them from its shell.

| Tool | What it does |
|---|---|
| [`rojo-autoconnect`](rojo-autoconnect) | Starts `rojo serve` as a detached process that outlives the calling shell, and a small patch to the Rojo Studio plugin so it connects by itself in Edit mode. |
| [`studio-import`](studio-import) | Imports an FBX / OBJ / GLB / GLTF through Roblox Studio's own 3D Importer, by driving its UI. |

Both need Windows and Roblox Studio.

## rojo-autoconnect

**Server.** Run from your Rojo project's folder (or pass `-Project path\to\default.project.json`):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File rojo.ps1 start    # -Port N for another port (default 34872)
powershell -NoProfile -ExecutionPolicy Bypass -File rojo.ps1 status   # exit 0: ours; 1: nothing there; 2: another project
powershell -NoProfile -ExecutionPolicy Bypass -File rojo.ps1 stop     # only a server this script started
```

`start` launches `rojo serve` through WMI (`Win32_Process.Create`), so the server isn't in the
caller's process tree and keeps running when the shell or agent session that started it ends.
It does nothing if your project is already being served on the port, and refuses if another
project is. A pid file and the server's log are written next to the script.

**Plugin.** Stock Rojo has no Edit-mode auto-connect: "Auto Reconnect" only tries once, when the
plugin loads. `autoconnect.patch` adds a loop to the plugin: every 5 seconds in Edit mode, if Auto
Reconnect is on and nothing is connected, it connects to a server on the configured host/port,
but only if that server's project lists the open place in `servePlaceIds`. Disconnect or Abort
pauses it until you next connect by hand.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File rojo.ps1 build    # -> out\Rojo.rbxm
```

`build` reads the installed CLI's version (`rojo --version`), clones that tag of
[rojo-rbx/rojo](https://github.com/rojo-rbx/rojo), applies the patch and builds the plugin, so the
plugin always matches your server. It stops with an error if a newer Rojo no longer takes the patch.
Tested on Rojo 7.7.1 (and 7.7.0).

To install: close Studio, move your current `%LOCALAPPDATA%\Roblox\Plugins\Rojo.rbxm` out (keep only
one Rojo plugin), copy `out\Rojo.rbxm` in, reopen Studio, then in Rojo's settings turn on
**Auto Reconnect** and set **Confirmation Behavior** to **Unlisted PlaceId** (otherwise each
connection still asks for a click). Add your place to the project file:

```json
"servePlaceIds": [123456789],
```

## studio-import

```powershell
python -m pip install -r requirements.txt
python import.py C:\path\to\Model.fbx
```

With one Studio window open in Edit mode, it opens File > Import, enters the file in the Windows
dialog, waits for the Import Preview and presses Import, leaving your importer settings exactly as
they are (so "Upload to Roblox" etc. behave as when you import by hand). Exit 0 means the importer
accepted the file; Studio's Import Queue finishes it a few seconds later. It takes the mouse and
keyboard for about 15 seconds.

Limits, because Studio draws the inside of the Import Preview itself (invisible to UI Automation):

- The Import button is found by its position from the window's bottom-right corner and clicked
  only once that pixel is blue (enabled). If a Studio update moves it, the script times out rather
  than clicking anything else: update `IMPORT_BTN_FROM_BR` at the top.
- It can't read importer warnings: with the button enabled it imports anyway.
- One Studio window only; it doesn't check for playtest mode (check before calling).
- Leave a few seconds between imports, or Studio's File menu can stay stuck open.

The menus and the file dialog are found by their UI Automation ids; see the constants at the top
of `import.py` if a Studio update renames them.

## Licence

MIT (see [LICENSE](LICENSE)), except `rojo-autoconnect/autoconnect.patch`, which modifies Rojo's
plugin source and is under Rojo's licence, the
[Mozilla Public License 2.0](https://github.com/rojo-rbx/rojo/blob/master/LICENSE.txt).
