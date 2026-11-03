# mcp-tools

An MCP server for working on a Roblox game from an AI assistant (Claude Code and other MCP
clients), next to Roblox's own Studio MCP. It covers what that one can't: keeping Rojo running
and connected without anyone clicking Connect, importing models, images, sounds and video
through Studio's own importer, and letting the assistant check its work through Rojo's and Studio's logs and a
screenshot of Studio. Windows only.

| Tool | What it does |
|---|---|
| `rojo_start(project, port=34872)` | Serves a Rojo project in a process that keeps running after the assistant's session ends. Does nothing if it's already being served; refuses if another project is on the port. |
| `rojo_status(project, port)` | Is a server for that project answering, and did `rojo_start` start it? |
| `rojo_stop(port)` | Stops the server `rojo_start` started (never one it didn't start). |
| `rojo_logs(port, lines=50)` | The end of that server's output: where Rojo reports files it can't sync. |
| `rojo_build_plugin()` | Builds a Rojo plugin that connects by itself in Edit mode (below), from the installed Rojo's own version. |
| `rojo_install_plugin(configure_settings=True)` | Installs that plugin (backing up the old one) and, with Studio closed, turns on Rojo's Auto Reconnect and "Unlisted PlaceId" confirmation. |
| `studio_windows()` | The open Studio windows (one per open place), with each one's pid and log file. |
| `studio_import(paths, window="", allow_input=True)` | Imports meshes (FBX / OBJ / GLB / GLTF), images, sounds and video through Studio's importer, as a person would: files and/or whole folders, one after another. |
| `studio_logs(lines=50, pattern="", since_minutes=0, window="")` | The end of a Studio window's log (Output window and Studio's own messages), filtered by a regex and/or age. |
| `studio_screenshot(window="")` | A screenshot of a Studio window, returned as an image, even if it's behind other windows. |

Each Studio tool works on one Studio window. With several open (one per place), pass `window`: a place name, part of the window title, or a pid from
`studio_windows`. With just one open it can be left out.

## Install

Needs Windows, [uv](https://docs.astral.sh/uv/), Roblox Studio, and for the Rojo tools `rojo`
(e.g. through aftman or rokit) and `git`.

Claude Code:

```
claude mcp add mcp-tools --scope user -- uvx --from git+https://github.com/gavinostler/mcp-tools mcp-tools
```

Other MCP clients (Claude Desktop, Cursor, ...):

```json
{
  "mcpServers": {
    "mcp-tools": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/gavinostler/mcp-tools", "mcp-tools"]
    }
  }
}
```

Working files (pid files, server logs, the plugin build) go in `%LOCALAPPDATA%\mcp-tools`.

## Rojo that stays up and connects by itself

`rojo_start` launches `rojo serve` through WMI (`Win32_Process.Create`), so the server isn't in the
assistant's process tree and survives its session ending (assistants' background commands are
usually killed after a while, and the sync with them).

Stock Rojo has no Edit-mode auto-connect: "Auto Reconnect" only tries once, when the plugin loads.
The patch this builds (`src/mcp_tools/autoconnect.patch`) adds a loop: every 5 seconds in Edit mode,
if Auto Reconnect is on and nothing is connected, it connects to a server on the configured
host/port, but only if that server's project lists the open place in `servePlaceIds`.
Disconnect or Abort pauses it until you next connect by hand. The build uses the installed CLI's
version tag of [rojo-rbx/rojo](https://github.com/rojo-rbx/rojo), so plugin and server always
match, and stops with an error if a newer Rojo no longer takes the patch. Tested on 7.7.1.

Once: `rojo_build_plugin`, close Studio, `rojo_install_plugin`, then add your place to the project:

```json
"servePlaceIds": [123456789],
```

(If Studio was open during `rojo_install_plugin`, set Auto Reconnect on and Confirmation Behavior to
"Unlisted PlaceId" in Rojo's settings yourself; Studio rewrites its settings when it closes.)

## Studio import

`studio_import` drives Studio's UI with Windows UI Automation: File > Import, the file dialog, then
Import in the Import Preview, leaving your importer settings as they are (so "Upload to Roblox"
etc. behave as when you import by hand). It takes everything in the dialog's "All Supported Files": meshes (`.fbx .obj .gltf .glb`), images
(`.bmp .gif .jpeg .jpg .png .tga`), audio (`.ogg .mp3 .flac .wav`) and video (`.mp4 .mov`). A mesh
always gets the Import Preview; if another kind of file gets none within 15 seconds of the file
dialog closing, the tool takes it that Studio sent it straight to its Import Queue and returns.
Returning means the importer accepted the file; Studio's Import Queue finishes it a few seconds
later, so check it arrived (with the Studio MCP) and look at `studio_logs` before using it.

**Bulk import.** `paths` is a list of files and/or folders (a folder means every supported file
directly inside it). Everything is checked before Studio is touched, then imported one at a time
(about 15-20 seconds each, with a pause between), reporting progress to clients that ask for it.
It stops at the first failure and says which files were imported and which weren't tried.

**Your mouse and keyboard.** The menu and file dialog go through UI Automation, and Import is
clicked by posting mouse messages to the Import Preview, so the real pointer doesn't move. If
Studio ignores that click (nothing happens within 5 seconds), it clicks with the real mouse; with
`allow_input=False` it cancels that import and fails instead. A menu left open by an earlier
import is closed the same way, with a real Esc only as the fallback. The result says how Import was
clicked. Studio's window (then the Import Preview) does still come to the front, since File >
Import works on the active window.

Limits, because Studio draws the inside of the Import Preview itself (invisible to UI Automation):

- The Import button is found by its position from the window's bottom-right corner and clicked only
  once that pixel is blue (enabled). If a Studio update moves it, the tool times out rather than
  clicking anything else: update `IMPORT_BTN_FROM_BR` in `studio_import.py`.
- It can't read importer warnings: with the button enabled it imports anyway.
- The Studio window must be in Edit mode (check before calling: it doesn't) and visible on screen.
- File > Import works on the place in Studio's active window, so the tool brings the chosen window
  to the front first, and fails if Windows won't let it. If the file dialog or Import Preview still
  turns out to belong to another place's window, it cancels it and fails rather than importing
  into the wrong place.
- Leave a few seconds between separate `studio_import` calls (a bulk import pauses by itself).

It also works without the server:
`python -m mcp_tools.studio_import <file or folder>... [--window W] [--no-input]`.

## Checking the result

- `rojo_logs` reads `serve-<port>.log`, which `rojo_start` writes (and starts over each time). When
  edits stop reaching Studio, this is where Rojo says which file it couldn't read.
- `studio_logs` reads the log of the chosen Studio window from `%LOCALAPPDATA%\Roblox\logs`:
  the one that window's process has open, or else the one created when it started. With Studio
  closed it reads the most recently written log instead. It's noisy, so pass a `pattern` (case-insensitive regex, e.g. `error|warn|fail`) and/or
  `since_minutes`. Times are UTC. After `studio_import`, look here for what the Import Queue said.
- `studio_screenshot` captures Studio with `PrintWindow`, so it works with Studio behind other
  windows and leaves the focus alone. If that comes back blank, it brings Studio to the front and
  copies it off the screen instead. The image is a JPEG at most 1568 px on its long side. It
  also works without the server: `python -m mcp_tools.studio_screenshot [out.jpg] [window]`.

## Licence

MIT (see [LICENSE](LICENSE)), except `src/mcp_tools/autoconnect.patch`, which modifies Rojo's plugin
source and is under Rojo's licence, the
[Mozilla Public License 2.0](https://github.com/rojo-rbx/rojo/blob/master/LICENSE.txt).
