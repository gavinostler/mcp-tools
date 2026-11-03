"""Import an FBX/OBJ/GLB/GLTF through Roblox Studio's own 3D Importer, as a person would.

Usage: python import.py <file>   (Windows; Roblox Studio open with one place, in Edit mode)

Drives: File > Import -> Windows "Open 3D File" dialog -> "Import Preview" window -> Import.
The importer's settings are left exactly as the owner set them.
Caller must make sure Studio is in Edit mode (not playtesting) first.
Exit 0 means the importer accepted the file. Studio's Import Queue then finishes it a few seconds
later, so confirm the model arrived (e.g. Studio MCP: workspace:WaitForChild(name, 30)).
Setup: python -m pip install pywinauto
"""
import ctypes
import os
import sys
import time

from pywinauto import findwindows
from pywinauto.controls.uiawrapper import UIAWrapper
from pywinauto.uia_element_info import UIAElementInfo

STUDIO_TITLE = r".* - Roblox Studio$"
IMPORT_ACTION_ID = "RBXMainWindow.meshImporterAction"  # File > "Import"
FILE_DLG_CLASS = "#32770"  # titled "Open 3D File (FBX, OBJ, GLB, GLTF)"
FILE_EDIT_ID, FILE_OPEN_ID = "1148", "1"  # standard Win32 file dialog ids
PREVIEW_TITLE = "Import Preview"
# The Import Preview contents are drawn by Roblox (a QEngineWidget), invisible to UI Automation.
# So the Import button is located relative to the window's bottom-right corner and only clicked
# once the pixel there is blue (enabled: ~(51,95,255), lighter under the mouse; disabled is grey).
# ponytail: breaks if Roblox moves the button; re-measure.
IMPORT_BTN_FROM_BR = (85, 40)


def fail(msg):
    print("import failed:", msg, file=sys.stderr)
    sys.exit(1)


def uia(h):
    return UIAWrapper(UIAElementInfo(h))


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
    return [uia(h).element_info.name or uia(h).element_info.class_name for h in windows(pid) if h not in skip]


def pixel(x, y):
    dc = ctypes.windll.user32.GetDC(0)
    c = ctypes.windll.gdi32.GetPixel(dc, x, y)
    ctypes.windll.user32.ReleaseDC(0, dc)
    return c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF


def blue(c):
    r, g, b = c
    return b > 200 and b - r > 90 and b - g > 50


def main():
    if len(sys.argv) != 2:
        fail("usage: import.py <path-to.fbx>")
    path = os.path.abspath(sys.argv[1])
    if not os.path.isfile(path):
        fail(f"no such file: {path}")
    t0 = time.time()

    mains = findwindows.find_windows(title_re=STUDIO_TITLE, backend="win32", visible_only=True)
    if len(mains) != 1:
        fail(f"expected exactly one Roblox Studio window, found {len(mains)}")
    main_h = mains[0]
    studio = uia(main_h)
    pid = studio.process_id()
    extra = titles(pid, {main_h})
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

    dlg_h = wait_for("the Open 3D File dialog", 15, lambda: findwindows.find_windows(
        process=pid, backend="win32", class_name=FILE_DLG_CLASS, visible_only=True))[0]
    dlg = uia(dlg_h)
    edit = child(child(dlg, automation_id=FILE_EDIT_ID, control_type="ComboBox"), control_type="Edit")
    edit.set_edit_text(path)
    child(dlg, automation_id=FILE_OPEN_ID).invoke()

    def preview_or_error():
        hs = [h for h in windows(pid) if h != main_h]
        for h in hs:
            if uia(h).element_info.name == PREVIEW_TITLE:
                return h
        if hs and dlg_h not in hs:  # dialog gone, something other than the importer came up
            fail(f"Studio showed {titles(pid, {main_h})} instead of the Import Preview")
        return None

    prev_h = wait_for("the Import Preview window", 60, preview_or_error)
    prev = uia(prev_h)
    prev.set_focus()
    r = prev.rectangle()
    bx, by = r.right - IMPORT_BTN_FROM_BR[0], r.bottom - IMPORT_BTN_FROM_BR[1]
    # The button greys out while the file loads (and stays grey if it can't be imported).
    wait_for("the Import button to be enabled", 60, lambda: blue(pixel(bx, by)))
    t_click = time.time()
    prev.click_input(coords=(bx - r.left, by - r.top))

    def closed():
        hs = [h for h in windows(pid) if h != main_h]
        if prev_h in hs:
            return False
        if hs:
            fail(f"importer closed but Studio showed: {titles(pid, {main_h})}")
        return True

    wait_for("the import to finish (Import Preview to close)", 180, closed)
    print(f"imported {path} in {time.time() - t0:.1f}s (import itself {time.time() - t_click:.1f}s)")


if __name__ == "__main__":
    main()
