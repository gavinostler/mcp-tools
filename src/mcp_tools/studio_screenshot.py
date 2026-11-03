"""Capture Roblox Studio's window as an image (the mcp-tools `studio_screenshot` tool).

capture(window="") -> (window, JPEG bytes), or:
    python -m mcp_tools.studio_screenshot [out.jpg] [window]
Windows only. `window` picks the Studio window (see studio.pick; needed when several are open). Uses PrintWindow, so the window can be behind others and keeps the focus where it
is; only if that comes back blank does it bring Studio to the front and copy it off the screen.
"""
import ctypes
import io
import sys
import time
from ctypes import wintypes

from PIL import Image, ImageGrab

from .studio import StudioError, StudioWindow, pick, uia  # (pywinauto makes this process DPI aware)

MAX_EDGE = 1568  # bigger images are scaled down on the model's side anyway
PW_RENDERFULLCONTENT = 2  # also capture what the GPU draws (the 3D viewport)
DWMWA_EXTENDED_FRAME_BOUNDS = 9
SW_SHOWNOACTIVATE = 4

# Own handles, so setting argtypes here doesn't change ctypes.windll for anyone else.
user32 = ctypes.WinDLL("user32")
gdi32 = ctypes.WinDLL("gdi32")
dwmapi = ctypes.WinDLL("dwmapi")
H, HDC, HBM = wintypes.HWND, wintypes.HDC, wintypes.HBITMAP
for dll, name, args, res in [
    (user32, "GetWindowDC", [H], HDC),
    (user32, "ReleaseDC", [H, HDC], ctypes.c_int),
    (user32, "PrintWindow", [H, HDC, wintypes.UINT], wintypes.BOOL),
    (user32, "GetWindowRect", [H, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
    (user32, "IsIconic", [H], wintypes.BOOL),
    (user32, "ShowWindow", [H, ctypes.c_int], wintypes.BOOL),
    (gdi32, "CreateCompatibleDC", [HDC], HDC),
    (gdi32, "CreateCompatibleBitmap", [HDC, ctypes.c_int, ctypes.c_int], HBM),
    (gdi32, "SelectObject", [HDC, wintypes.HGDIOBJ], wintypes.HGDIOBJ),
    (gdi32, "GetDIBits", [HDC, HBM, wintypes.UINT, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p,
                          wintypes.UINT], ctypes.c_int),
    (gdi32, "DeleteObject", [wintypes.HGDIOBJ], wintypes.BOOL),
    (gdi32, "DeleteDC", [HDC], wintypes.BOOL),
    (dwmapi, "DwmGetWindowAttribute", [H, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD], ctypes.c_long),
]:
    getattr(dll, name).argtypes, getattr(dll, name).restype = args, res


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


def _rects(h):
    """The window's rect, and the part of it that's visible (without Windows 10/11's invisible
    resize border), both in screen pixels."""
    r = wintypes.RECT()
    user32.GetWindowRect(h, ctypes.byref(r))
    f = wintypes.RECT()
    if dwmapi.DwmGetWindowAttribute(h, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(f), ctypes.sizeof(f)):
        f = r
    return (r.left, r.top, r.right, r.bottom), (f.left, f.top, f.right, f.bottom)


def _print_window(h, rect):
    w, ht = rect[2] - rect[0], rect[3] - rect[1]
    wdc = user32.GetWindowDC(h)
    mem = gdi32.CreateCompatibleDC(wdc)
    bmp = gdi32.CreateCompatibleBitmap(wdc, w, ht)
    try:
        old = gdi32.SelectObject(mem, bmp)
        ok = user32.PrintWindow(h, mem, PW_RENDERFULLCONTENT)
        gdi32.SelectObject(mem, old)  # GetDIBits wants the bitmap out of every DC
        bi = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=w, biHeight=-ht,
                              biPlanes=1, biBitCount=32)  # negative height: top row first
        buf = ctypes.create_string_buffer(w * ht * 4)
        rows = gdi32.GetDIBits(mem, bmp, 0, ht, buf, ctypes.byref(bi), 0)
    finally:
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(h, wdc)
    if not ok or rows != ht:
        return None
    return Image.frombuffer("RGB", (w, ht), buf.raw, "raw", "BGRX", 0, 1)


def capture(window: str = "") -> tuple[StudioWindow, bytes]:
    """Screenshot the Studio window `window` picks. Returns it and a JPEG no bigger than MAX_EDGE
    on its long side. Raises StudioError."""
    target = pick(window)
    h = target.handle
    if user32.IsIconic(h):
        user32.ShowWindow(h, SW_SHOWNOACTIVATE)  # a minimised window has nothing to print
        time.sleep(0.5)
    rect, frame = _rects(h)
    img = _print_window(h, rect)
    if img is not None:
        img = img.crop((frame[0] - rect[0], frame[1] - rect[1], frame[2] - rect[0], frame[3] - rect[1]))
    if img is None or img.getbbox() is None:  # failed, or all black
        uia(h).set_focus()
        time.sleep(0.3)
        img = ImageGrab.grab(bbox=frame, all_screens=True)
    img.thumbnail((MAX_EDGE, MAX_EDGE))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    return target, out.getvalue()


if __name__ == "__main__":
    dest = sys.argv[1] if len(sys.argv) > 1 else "studio.jpg"
    try:
        target, jpeg = capture(sys.argv[2] if len(sys.argv) > 2 else "")
    except StudioError as e:
        sys.exit(f"screenshot failed: {e}")
    with open(dest, "wb") as f:
        f.write(jpeg)
    print(f"saved {target} to {dest}")
