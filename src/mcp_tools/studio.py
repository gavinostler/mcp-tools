"""Finding Roblox Studio's windows and the log each one writes. Windows only.

Studio opens each place in its own window. Every studio_* tool picks the window to work on with
pick(). Each RobloxStudioBeta.exe process writes one log, found by pid; places sharing a process
share it. Menu actions such as File > Import apply to the place in Studio's active window, so
activate() brings the right one to the front first.
"""
import ctypes
import os
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

import psutil
from pywinauto import findwindows  # importing pywinauto also makes this process DPI aware
from pywinauto.controls.uiawrapper import UIAWrapper
from pywinauto.uia_element_info import UIAElementInfo

from .logs import studio_log_files

TITLE_SUFFIX = " - Roblox Studio"
STUDIO_TITLE = r".* - Roblox Studio$"
LOG_START = 120  # a Studio process creates its log within this many seconds of starting
GA_ROOTOWNER, SW_RESTORE, WM_CLOSE = 3, 9, 0x0010
CWP_SKIPINVISIBLE, CWP_SKIPTRANSPARENT = 1, 4
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, MK_LBUTTON = 0x0200, 0x0201, 0x0202, 1

# Own handles, so setting argtypes here doesn't change ctypes.windll for anyone else.
user32 = ctypes.WinDLL("user32")
kernel32 = ctypes.WinDLL("kernel32")
H = wintypes.HWND
for name, args, res in [
    ("GetForegroundWindow", [], H),
    ("SetForegroundWindow", [H], wintypes.BOOL),
    ("BringWindowToTop", [H], wintypes.BOOL),
    ("GetWindowThreadProcessId", [H, ctypes.c_void_p], wintypes.DWORD),
    ("AttachThreadInput", [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL], wintypes.BOOL),
    ("IsIconic", [H], wintypes.BOOL),
    ("ShowWindow", [H, ctypes.c_int], wintypes.BOOL),
    ("GetAncestor", [H, wintypes.UINT], H),
    ("PostMessageW", [H, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL),
    ("ScreenToClient", [H, ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
    ("ChildWindowFromPointEx", [H, wintypes.POINT, wintypes.UINT], H),
]:
    getattr(user32, name).argtypes, getattr(user32, name).restype = args, res
kernel32.GetCurrentThreadId.restype = wintypes.DWORD


class StudioError(Exception):
    pass


@dataclass(frozen=True)
class StudioWindow:
    handle: int
    pid: int
    title: str

    @property
    def place(self) -> str:
        return self.title.removesuffix(TITLE_SUFFIX)

    def __str__(self):
        return f"{self.place!r} (pid {self.pid})"


def uia(h):
    return UIAWrapper(UIAElementInfo(h))


def find() -> list[StudioWindow]:
    """The open Studio windows, frontmost first."""
    return [StudioWindow(e.handle, e.process_id, e.name) for e in
            findwindows.find_elements(title_re=STUDIO_TITLE, backend="win32", visible_only=True)]


def pick(window: str = "", found: list[StudioWindow] | None = None) -> StudioWindow:
    """The Studio window `window` names: a pid, a place name (the title without " - Roblox
    Studio"), or part of a title. "" picks the only window open."""
    found = find() if found is None else found
    key = window.strip().lower()
    if key.isdigit():
        hits = [w for w in found if w.pid == int(key)]
    else:  # an exact place name wins over a substring ("Game" vs "Game Test")
        hits = [w for w in found if w.place.lower() == key] or [w for w in found if key in w.title.lower()]
    if len(hits) == 1:
        return hits[0]
    if not found:
        raise StudioError("Roblox Studio isn't open")
    listing = ", ".join(map(str, found))
    if not hits:
        raise StudioError(f"no Studio window matches {window!r}; open: {listing}")
    raise StudioError(f"{len(hits)} Studio windows are open{f' matching {window!r}' if key else ''}: "
                      f"pass `window` (a place name, part of a title, or a pid) to pick one; open: {listing}")


def activate(w: StudioWindow, timeout: float = 5) -> None:
    """Bring `w` to the front, so it's Studio's active window and menu actions apply to its place.
    Raises StudioError if Windows won't allow it."""
    h = w.handle
    if user32.IsIconic(h):
        user32.ShowWindow(h, SW_RESTORE)
    end = time.time() + timeout
    while user32.GetForegroundWindow() != h:
        if time.time() > end:
            raise StudioError(f"couldn't bring {w} to the front; click it once, then try again")
        # Windows only lets the foreground window's thread hand the foreground on, so borrow it.
        fg = user32.GetForegroundWindow()
        fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        me = kernel32.GetCurrentThreadId()
        attached = bool(fg_thread) and fg_thread != me and user32.AttachThreadInput(me, fg_thread, True)
        try:
            user32.BringWindowToTop(h)
            user32.SetForegroundWindow(h)
        finally:
            if attached:
                user32.AttachThreadInput(me, fg_thread, False)
        time.sleep(0.2)


def owner(h: int) -> int | None:
    """The top-level window that owns window `h` (a dialog's owner is the window it's for)."""
    return user32.GetAncestor(h, GA_ROOTOWNER)


def close(h: int) -> None:
    user32.PostMessageW(h, WM_CLOSE, 0, 0)


def post_click(h: int, x: int, y: int) -> None:
    """Click screen point (x, y) in window `h` by posting mouse messages to it: the real mouse
    doesn't move. Studio may ignore it (the caller checks)."""
    while True:  # the innermost child window at the point is the one that gets the mouse
        pt = wintypes.POINT(x, y)
        user32.ScreenToClient(h, ctypes.byref(pt))
        c = user32.ChildWindowFromPointEx(h, pt, CWP_SKIPINVISIBLE | CWP_SKIPTRANSPARENT)
        if not c or c == h:
            break
        h = c
    lparam = (pt.y & 0xFFFF) << 16 | (pt.x & 0xFFFF)
    for msg, wparam in [(WM_MOUSEMOVE, 0), (WM_LBUTTONDOWN, MK_LBUTTON), (WM_LBUTTONUP, 0)]:
        user32.PostMessageW(h, msg, wparam, lparam)
        time.sleep(0.1)  # frames apart: Roblox's UI reads input once a frame


def log_for(pid: int, logs_dir: Path) -> Path | None:
    """The log Studio process `pid` writes: the one it has open, or else the one created as it
    started. None if neither can be found."""
    try:
        proc = psutil.Process(pid)
        opened = [Path(f.path) for f in proc.open_files()
                  if f.path.lower().endswith(".log") and "studio" in os.path.basename(f.path).lower()]
        started = proc.create_time()
    except psutil.Error:
        return None
    if opened:
        return max(opened, key=lambda p: p.stat().st_mtime)

    def after_start(p):
        st = p.stat()
        return getattr(st, "st_birthtime", st.st_ctime) - started  # st_ctime: creation time on Windows

    near = [(after_start(p), p) for p in studio_log_files(logs_dir)]
    near = [(d, p) for d, p in near if -5 <= d <= LOG_START]
    return min(near, key=lambda x: x[0])[1] if near else None
