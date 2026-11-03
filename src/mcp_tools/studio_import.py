"""Import a file through Roblox Studio's own importer (File > Import), as a person would: meshes
(FBX/OBJ/GLB/GLTF), images, audio and video.

import_model(path, window="") (the mcp-tools `studio_import` tool), or:
    python -m mcp_tools.studio_import <file> [window]
Windows only. `window` picks the Studio window (see studio.pick; needed when several are open),
which must be in Edit mode (not playtesting: the caller checks).

Drives: File > Import -> Windows file dialog -> "Import Preview" window -> Import. A mesh always
gets the Import Preview; another kind of file may go straight from the file dialog to Studio's
Import Queue, and then there's nothing more to click. The importer's settings are left exactly as
they are. Returning means the importer accepted the file; the Import Queue finishes it a few
seconds later, so confirm it arrived (e.g. with the Roblox Studio MCP: workspace:WaitForChild(name,
30)) and check Studio's log for errors.
"""
import ctypes
import os
import sys
import time

from pywinauto import findwindows

from .studio import StudioError, find, pick, uia

# The file dialog's "All Supported Files"
MESHES = {".fbx", ".obj", ".gltf", ".glb"}
MEDIA = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tga",  # images
         ".ogg", ".mp3", ".flac", ".wav",  # audio
         ".mp4", ".mov"}  # video
EXTENSIONS = MESHES | MEDIA
NO_PREVIEW_AFTER = 15  # s after the file dialog closes: a non-mesh file got no Import Preview
IMPORT_ACTION_ID = "RBXMainWindow.meshImporterAction"  # File > "Import"
FILE_DLG_CLASS = "#32770"
FILE_EDIT_ID, FILE_OPEN_ID = "1148", "1"  # standard Win32 file dialog ids
PREVIEW_TITLE = "Import Preview"
# The Import Preview contents are drawn by Roblox (a QEngineWidget), invisible to UI Automation.
# So the Import button is located relative to the window's bottom-right corner and only clicked
# once the pixel there is blue (enabled: ~(51,95,255), lighter under the mouse; disabled is grey).
# ponytail: breaks if Roblox moves the button; re-measure.
IMPORT_BTN_FROM_BR = (85, 40)

STRAY = {"RBX::Studio::detail::Menu", "SysShadow"}  # a menu left open by the last import


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


def import_model(path: str, window: str = "") -> str:
    """Import `path` through the importer of the Studio window `window` picks; returns a summary,
    raises StudioError (ImportFailed once it's started)."""
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        fail(f"no such file: {path}")
    ext = os.path.splitext(path)[1].lower()
    if ext not in EXTENSIONS:
        fail(f"Studio's importer takes {' '.join(sorted(EXTENSIONS))} files, not {path}")
    t0 = time.time()

    found = find()
    target = pick(window, found)
    main_h, pid = target.handle, target.pid
    # Other places' main windows, should Studio ever put two in one process, aren't in the way.
    mains = {w.handle for w in found if w.pid == pid}
    studio = uia(main_h)
    extra = titles(pid, mains)
    if extra and set(extra) <= STRAY:  # close it and carry on
        from pywinauto.keyboard import send_keys

        studio.set_focus()  # the Esc must go to this window, not whichever Studio has the focus
        send_keys("{ESC}")
        time.sleep(0.5)
        extra = titles(pid, mains)
    if extra:
        fail(f"Studio already has another window open (close it first): {extra}")

    # File > Import. Walk children explicitly: a descendant search crawls the whole Explorer tree.
    topbar = child(studio, automation_id="RBXMainWindow.studioTopBar")
    menubar = child(topbar, control_type="MenuBar")
    file_menu = child(menubar, name="File", control_type="MenuItem")
    studio.set_focus()
    file_menu.expand()
    popup = wait_for("File menu", 5, lambda: file_menu.children())[0]
    child(popup, automation_id=IMPORT_ACTION_ID).invoke()

    dlg_h = wait_for("the importer's file dialog", 15, lambda: findwindows.find_windows(
        process=pid, backend="win32", class_name=FILE_DLG_CLASS, visible_only=True))[0]
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
    prev = uia(prev_h)
    prev.set_focus()
    r = prev.rectangle()
    bx, by = r.right - IMPORT_BTN_FROM_BR[0], r.bottom - IMPORT_BTN_FROM_BR[1]
    # The button greys out while the file loads (and stays grey if it can't be imported).
    wait_for("the Import button to be enabled", 60, lambda: blue(pixel(bx, by)))
    t_click = time.time()
    prev.click_input(coords=(bx - r.left, by - r.top))

    def closed():
        hs = [h for h in windows(pid) if h not in mains]
        if prev_h in hs:
            return False
        if hs:
            fail(f"importer closed but Studio showed: {titles(pid, mains)}")
        return True

    wait_for("the import to finish (Import Preview to close)", 180, closed)
    return (f"imported {path} into {target} in {time.time() - t0:.1f}s "
            f"(import itself {time.time() - t_click:.1f}s)")


if __name__ == "__main__":
    if not 2 <= len(sys.argv) <= 3:
        sys.exit("usage: python -m mcp_tools.studio_import <file> [window]")
    try:
        print(import_model(*sys.argv[1:]))
    except StudioError as e:
        sys.exit(f"import failed: {e}")
