"""Import files through Roblox Studio's own importer (File > Import), as a person would: meshes
(FBX/OBJ/GLB/GLTF), images, audio and video.

import_many(paths) / import_model(path) (the mcp-tools `studio_import` tool), or:
    python -m mcp_tools.studio_import <file or folder>... [--window W] [--no-input]
Windows only. `window` picks the Studio window (see studio.pick; needed when several are open),
which must be in Edit mode (not playtesting: the caller checks).

It leaves the real mouse and keyboard alone where Studio lets it: the menu and file dialog go
through UI Automation, and Import is clicked by posting mouse messages to the Import Preview.
Only if Studio ignores those does it use the real mouse / keyboard (unless allow_input=False).
It still brings the Studio window to the front, as File > Import works on the active window.

Drives: File > Import -> Windows file dialog -> "Import Preview" window -> Import. A mesh always
gets the Import Preview; another kind of file may go straight from the file dialog to Studio's
Import Queue, and then there's nothing more to click. The importer's settings are left exactly as
they are. Returning means the importer accepted the file; the Import Queue finishes it a few
seconds later, so confirm it arrived (e.g. with the Roblox Studio MCP: workspace:WaitForChild(name,
30)) and check Studio's log for errors.
"""
import argparse
import ctypes
import os
import sys
import time

from pywinauto import findwindows

from .studio import StudioError, StudioWindow, activate, close, find, owner, pick, post_click, uia

# The file dialog's "All Supported Files"
MESHES = {".fbx", ".obj", ".gltf", ".glb"}
MEDIA = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tga",  # images
         ".ogg", ".mp3", ".flac", ".wav",  # audio
         ".mp4", ".mov"}  # video
EXTENSIONS = MESHES | MEDIA
NO_PREVIEW_AFTER = 15  # s after the file dialog closes: a non-mesh file got no Import Preview
IGNORED_AFTER = 5  # s after a background click on Import with nothing happening: Studio ignored it
BETWEEN = 3  # s between imports, so the Import Queue keeps up
IMPORT_ACTION_ID = "RBXMainWindow.meshImporterAction"  # File > "Import"
FILE_DLG_CLASS = "#32770"
FILE_EDIT_ID, FILE_OPEN_ID = "1148", "1"  # standard Win32 file dialog ids
PREVIEW_TITLE = "Import Preview"
# The Import Preview contents are drawn by Roblox (a QEngineWidget), invisible to UI Automation.
# So the Import button is located relative to the window's bottom-right corner and only clicked
# once the pixel there is blue (enabled: ~(51,95,255), lighter under the mouse; disabled is grey).
# ponytail: breaks if Roblox moves the button; re-measure.
IMPORT_BTN_FROM_BR = (85, 40)

STRAY_MENU, STRAY = "RBX::Studio::detail::Menu", {"RBX::Studio::detail::Menu", "SysShadow"}
# ^ a menu left open by the last import (SysShadow is its drop shadow)


class ImportFailed(StudioError):
    pass


def fail(msg):
    raise ImportFailed(msg)


def child(el, **want):
    for c in el.children():
        ei = c.element_info
        if all(getattr(ei, k) == v for k, v in want.items()):
            return c
    fail(f"control not found: {want} under {el.element_info.name!r}")


def wait_for(what, timeout, fn):
    end = time.time() + timeout
    while time.time() < end:
        r = fn()
        if r:
            return r
        time.sleep(0.25)
    fail(f"timed out after {timeout}s waiting for {what}")


def settles(timeout, fn):
    """Whether fn() turns true within `timeout` s (wait_for without failing)."""
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.25)
    return False


def windows(pid):
    return findwindows.find_windows(process=pid, backend="win32", top_level_only=True, visible_only=True)


def titles(pid, skip):
    infos = [uia(h).element_info for h in windows(pid) if h not in skip]
    return [ei.name or ei.class_name for ei in infos]


def pixel(x, y):
    dc = ctypes.windll.user32.GetDC(0)
    c = ctypes.windll.gdi32.GetPixel(dc, x, y)
    ctypes.windll.user32.ReleaseDC(0, dc)
    return c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF


def blue(c):
    r, g, b = c
    return b > 200 and b - r > 90 and b - g > 50


def check_file(path: str) -> str:
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(path):
        fail(f"no such file: {path}")
    if os.path.splitext(path)[1].lower() not in EXTENSIONS:
        fail(f"Studio's importer takes {' '.join(sorted(EXTENSIONS))} files, not {path}")
    return path


def expand(paths: list[str]) -> list[str]:
    """Files to import: each path that's a file, and every importable file directly in each
    folder, in order and once each. Raises ImportFailed for a missing or unimportable file."""
    files = []
    for p in paths:
        full = os.path.abspath(os.path.expanduser(p))
        if os.path.isdir(full):
            files += sorted(os.path.join(full, f) for f in os.listdir(full)
                            if os.path.splitext(f)[1].lower() in EXTENSIONS
                            and os.path.isfile(os.path.join(full, f)))
        else:
            files.append(check_file(full))
    files = list(dict.fromkeys(files))
    if not files:
        fail(f"nothing to import in {paths}")
    return files


def import_many(paths: list[str], window: str = "", allow_input: bool = True, on_file=None) -> str:
    """Import every file `paths` names (see expand) into the Studio window `window` picks, one at
    a time, stopping at the first failure. on_file(index, count, path) is called before each.
    Returns a line per file; raises StudioError, saying what got imported before it."""
    files = expand(paths)  # all checked before Studio is touched
    done = []
    for i, f in enumerate(files):
        if on_file:
            on_file(i, len(files), f)
        if i:
            time.sleep(BETWEEN)
        try:
            done.append(import_model(f, window, allow_input))
        except StudioError as e:
            if not done:
                raise
            left = files[i + 1:]
            msg = f"imported {len(done)} of {len(files)}, then {f} failed: {e}\n" + "\n".join(done)
            if left:
                msg += "\nnot tried: " + ", ".join(left)
            raise ImportFailed(msg) from None
    return "\n".join(done)


def import_model(path: str, window: str = "", allow_input: bool = True) -> str:
    """Import `path` through the importer of the Studio window `window` picks; returns a summary,
    raises StudioError (ImportFailed once it's started). allow_input: fall back to the real mouse
    and keyboard if Studio ignores the background ones."""
    path = check_file(path)
    ext = os.path.splitext(path)[1].lower()
    t0 = time.time()

    found = find()
    target = pick(window, found)
    main_h, pid = target.handle, target.pid
    # Other places' main windows in the same process aren't in the way.
    mains = {w.handle for w in found if w.pid == pid}
    others = {w.handle: w for w in found if w.handle != main_h}

    def check_owner(h, what):
        """Fail (cancelling it) if Studio opened `h` for another place's window."""
        wrong = others.get(owner(h))
        if wrong:
            close(h)
            fail(f"Studio opened the {what} for {wrong} instead of {target}, so it was cancelled; "
                 "try again")
    studio = uia(main_h)
    extra = titles(pid, mains)
    if extra and set(extra) <= STRAY:  # close it and carry on
        for h in windows(pid):
            ei = uia(h).element_info
            if h not in mains and (ei.name or ei.class_name) == STRAY_MENU:
                close(h)
        if not settles(1, lambda: not titles(pid, mains)) and allow_input:
            from pywinauto.keyboard import send_keys

            activate(target)  # the Esc must go to this window, not whichever Studio has the focus
            send_keys("{ESC}")
            time.sleep(0.5)
        extra = titles(pid, mains)
    if extra:
        fail(f"Studio already has another window open (close it first): {extra}")

    # File > Import. Walk children explicitly: a descendant search crawls the whole Explorer tree.
    topbar = child(studio, automation_id="RBXMainWindow.studioTopBar")
    menubar = child(topbar, control_type="MenuBar")
    file_menu = child(menubar, name="File", control_type="MenuItem")
    activate(target)  # File > Import applies to the place in Studio's active window
    file_menu.expand()
    popup = wait_for("File menu", 5, lambda: file_menu.children())[0]
    child(popup, automation_id=IMPORT_ACTION_ID).invoke()

    dlg_h = wait_for("the importer's file dialog", 15, lambda: findwindows.find_windows(
        process=pid, backend="win32", class_name=FILE_DLG_CLASS, visible_only=True))[0]
    check_owner(dlg_h, "file dialog")
    dlg = uia(dlg_h)
    edit = child(child(dlg, automation_id=FILE_EDIT_ID, control_type="ComboBox"), control_type="Edit")
    edit.set_edit_text(path)
    child(dlg, automation_id=FILE_OPEN_ID).invoke()

    no_preview = object()
    dlg_closed = None

    def preview_or_error():
        nonlocal dlg_closed
        hs = [h for h in windows(pid) if h not in mains]
        for h in hs:
            if uia(h).element_info.name == PREVIEW_TITLE:
                return h
        if dlg_h in hs:
            return None
        if hs:  # dialog gone, something other than the importer came up
            fail(f"Studio showed {titles(pid, mains)} instead of the Import Preview "
                 "(see it with studio_screenshot)")
        if ext in MEDIA:  # may have gone straight to the Import Queue
            dlg_closed = dlg_closed or time.time()
            if time.time() - dlg_closed > NO_PREVIEW_AFTER:
                return no_preview
        return None

    prev_h = wait_for("the Import Preview window", 60, preview_or_error)
    if prev_h is no_preview:
        return (f"handed {path} to {target}'s importer in {time.time() - t0:.1f}s; Studio showed no "
                "Import Preview for it, so its Import Queue takes it from here")
    check_owner(prev_h, "Import Preview")
    prev = uia(prev_h)
    # In front, as the Import button's state is read off the screen (no mouse involved).
    activate(StudioWindow(prev_h, pid, PREVIEW_TITLE))
    r = prev.rectangle()
    bx, by = r.right - IMPORT_BTN_FROM_BR[0], r.bottom - IMPORT_BTN_FROM_BR[1]
    # The button greys out while the file loads (and stays grey if it can't be imported).
    wait_for("the Import button to be enabled", 60, lambda: blue(pixel(bx, by)))
    t_click = time.time()
    post_click(prev_h, bx, by)
    how = "in the background"
    # Taken: the preview closes, or the button greys out. Neither within a few seconds: ignored.
    if not settles(IGNORED_AFTER, lambda: prev_h not in windows(pid) or not blue(pixel(bx, by))):
        if not allow_input:
            close(prev_h)
            fail("Studio ignored the background click on Import, so the import was cancelled; "
                 "allow the real mouse to import this")
        prev.set_focus()
        prev.click_input(coords=(bx - r.left, by - r.top))
        how = "with the mouse (Studio ignored the background click)"

    def closed():
        hs = [h for h in windows(pid) if h not in mains]
        if prev_h in hs:
            return False
        if hs:
            fail(f"importer closed but Studio showed: {titles(pid, mains)}")
        return True

    wait_for("the import to finish (Import Preview to close)", 180, closed)
    return (f"imported {path} into {target} in {time.time() - t0:.1f}s "
            f"(import itself {time.time() - t_click:.1f}s; clicked Import {how})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(prog="python -m mcp_tools.studio_import",
                                 description="Import files through Roblox Studio's importer.")
    ap.add_argument("paths", nargs="+", help="files, or folders to import every supported file in")
    ap.add_argument("--window", default="", help="which Studio window: place name, part of a title, or pid")
    ap.add_argument("--no-input", action="store_true",
                    help="never use the real mouse or keyboard (fail instead)")
    args = ap.parse_args()
    try:
        print(import_many(args.paths, args.window, not args.no_input,
                          lambda i, n, f: print(f"[{i + 1}/{n}] {f}", file=sys.stderr)))
    except StudioError as e:
        sys.exit(f"import failed: {e}")
