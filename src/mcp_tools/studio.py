"""Finding Roblox Studio's windows and the log each one writes. Windows only.

Studio opens each place in its own window and its own process (RobloxStudioBeta.exe), and each
process writes its own log, so a window is identified by its pid. Every studio_* tool picks the
window to work on with pick().
"""
import os
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
