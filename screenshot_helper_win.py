#!/usr/bin/env python3
"""screenshot_helper for Windows: the same command line and stdout protocol as
screenshot_helper.swift (macOS), so main.py and server.py drive both alike.

Modes:
  screenshot_helper_win.py <output-dir> [capture-config.json]
      Daemon: global hotkeys (Ctrl+Shift+S screenshot of the capture area, Ctrl+Shift+M
      screenshot of the whole primary monitor, Ctrl+Shift+W selection).
      Also accepts "SNAP" (capture area) and "SNAP FULL" lines on stdin. The capture config is re-read on every
      screenshot, so the capture area can be changed while recording.
      "AUTO <seconds>" starts automatic frames: every <seconds> a JPEG of the capture
      area goes to <output-dir>/.frames/ and "FRAME:<path>" is printed (no flash).
      "AUTO OFF" stops them. Exits when stdin (a pipe from main.py) closes.
  screenshot_helper_win.py --select-region     Drag-select overlay → prints REGION:{json}
  screenshot_helper_win.py --pick-window       Click-a-window overlay → prints WINDOW:{json}
  screenshot_helper_win.py --list-windows      Prints on-screen windows as a JSON array
  screenshot_helper_win.py --capture <config.json> <out.png>   One-shot capture (preview)

Capture config (JSON), coordinates in physical pixels of the virtual screen:
  {"mode": "full"}                                         the primary monitor
  {"mode": "region", "rect": {"x":..,"y":..,"w":..,"h":..}}
  {"mode": "window", "windowId": <HWND>, "owner": "Google Chrome", "title": "..."}
  Optional "frames_crop": {"x":..,"y":..,"w":..,"h":..} in fractions of the captured image:
  automatic frames and Ctrl+Shift+S keep only that part.

A window nothing covers is grabbed from the screen; a covered one is rendered with
PrintWindow(PW_RENDERFULLCONTENT), which works behind other windows (not while minimized)
but leaves hardware-decoded video (YouTube in a browser) blank. Needs: mss, pillow; tkinter for the
overlays and the capture flash.
"""

import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import mss
from PIL import Image, ImageEnhance

import native_helpers

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")

# ── Win32 prototypes (handles are pointer-sized: declare them, or they get truncated) ──

WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
for fn, args, res in [
    (user32.EnumWindows, [WNDENUMPROC, wt.LPARAM], wt.BOOL),
    (user32.IsWindow, [wt.HWND], wt.BOOL),
    (user32.IsWindowVisible, [wt.HWND], wt.BOOL),
    (user32.IsIconic, [wt.HWND], wt.BOOL),
    (user32.GetWindowTextLengthW, [wt.HWND], ctypes.c_int),
    (user32.GetWindowTextW, [wt.HWND, wt.LPWSTR, ctypes.c_int], ctypes.c_int),
    (user32.GetClassNameW, [wt.HWND, wt.LPWSTR, ctypes.c_int], ctypes.c_int),
    (user32.GetWindowThreadProcessId, [wt.HWND, ctypes.POINTER(wt.DWORD)], wt.DWORD),
    (user32.GetWindowLongW, [wt.HWND, ctypes.c_int], ctypes.c_long),
    (user32.SetWindowLongW, [wt.HWND, ctypes.c_int, ctypes.c_long], ctypes.c_long),
    (user32.GetWindow, [wt.HWND, wt.UINT], wt.HWND),
    (user32.GetParent, [wt.HWND], wt.HWND),
    (user32.GetWindowRect, [wt.HWND, ctypes.POINTER(wt.RECT)], wt.BOOL),
    (user32.GetDC, [wt.HWND], wt.HDC),
    (user32.ReleaseDC, [wt.HWND, wt.HDC], ctypes.c_int),
    (user32.PrintWindow, [wt.HWND, wt.HDC, wt.UINT], wt.BOOL),
    (user32.ShowWindow, [wt.HWND, ctypes.c_int], wt.BOOL),
    (user32.SetWindowPos, [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT],
     wt.BOOL),
    (user32.SetForegroundWindow, [wt.HWND], wt.BOOL),
    (user32.RegisterHotKey, [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT], wt.BOOL),
    (user32.UnregisterHotKey, [wt.HWND, ctypes.c_int], wt.BOOL),
    (user32.GetMessageW, [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT], wt.BOOL),
    (user32.GetAsyncKeyState, [ctypes.c_int], ctypes.c_short),
    (user32.keybd_event, [wt.BYTE, wt.BYTE, wt.DWORD, ctypes.c_void_p], None),
    (user32.GetClipboardSequenceNumber, [], wt.DWORD),
    (user32.OpenClipboard, [wt.HWND], wt.BOOL),
    (user32.CloseClipboard, [], wt.BOOL),
    (user32.GetClipboardData, [wt.UINT], wt.HANDLE),
    (user32.GetSystemMetrics, [ctypes.c_int], ctypes.c_int),
    (gdi32.CreateCompatibleDC, [wt.HDC], wt.HDC),
    (gdi32.CreateCompatibleBitmap, [wt.HDC, ctypes.c_int, ctypes.c_int], wt.HBITMAP),
    (gdi32.SelectObject, [wt.HDC, wt.HGDIOBJ], wt.HGDIOBJ),
    (gdi32.DeleteObject, [wt.HGDIOBJ], wt.BOOL),
    (gdi32.DeleteDC, [wt.HDC], wt.BOOL),
    (gdi32.GetDIBits, [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p, ctypes.c_void_p, wt.UINT],
     ctypes.c_int),
    (kernel32.OpenProcess, [wt.DWORD, wt.BOOL, wt.DWORD], wt.HANDLE),
    (kernel32.CloseHandle, [wt.HANDLE], wt.BOOL),
    (kernel32.QueryFullProcessImageNameW, [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)], wt.BOOL),
    (kernel32.GlobalLock, [wt.HGLOBAL], ctypes.c_void_p),
    (kernel32.GlobalUnlock, [wt.HGLOBAL], wt.BOOL),
    (dwmapi.DwmGetWindowAttribute, [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD], ctypes.c_long),
]:
    fn.argtypes, fn.restype = args, res

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = 0x80, 0x40000
WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_NOACTIVATE, WS_EX_TOPMOST = 0x80000, 0x20, 0x8000000, 0x8
GW_OWNER = 4
DWMWA_EXTENDED_FRAME_BOUNDS, DWMWA_CLOAKED = 9, 14
PW_RENDERFULLCONTENT = 2
MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x2, 0x4, 0x4000
VK_SHIFT, VK_CONTROL, VK_MENU, VK_ESCAPE = 0x10, 0x11, 0x12, 0x1B
WM_HOTKEY = 0x0312
CF_UNICODETEXT = 13
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
HWND_TOPMOST = wt.HWND(-1)
SWP_NOACTIVATE = 0x10
SKIP_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}

ACCENT = "#7d9cff"


def set_dpi_aware():
    """All coordinates are physical pixels; otherwise Windows scales them per monitor."""
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):   # PER_MONITOR_AWARE_V2
            return
    except AttributeError:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        user32.SetProcessDPIAware()


def log(msg: str):
    print(f"[screenshot-helper] {msg}", file=sys.stderr, flush=True)


def emit(line: str):
    print(line, flush=True)


# ── Capture config ───────────────────────────────────────────────────────────

def load_config(path: str | None) -> dict:
    if not path:
        return {"mode": "full"}
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")) or {"mode": "full"}
    except (OSError, ValueError):
        return {"mode": "full"}


def _rect(d) -> tuple[int, int, int, int] | None:
    try:
        return int(round(float(d["x"]))), int(round(float(d["y"]))), int(round(float(d["w"]))), int(round(float(d["h"])))
    except (KeyError, TypeError, ValueError):
        return None


# ── Windows ──────────────────────────────────────────────────────────────────

def _window_text(hwnd) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _class_name(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _pid(hwnd) -> int:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _exe_path(pid: int) -> str:
    h = kernel32.OpenProcess(0x1000, False, pid)    # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        size = wt.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        return buf.value if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) else ""
    finally:
        kernel32.CloseHandle(h)


def owner_name(hwnd) -> str:
    exe = _exe_path(_pid(hwnd))
    return native_helpers.app_display_name(exe) if exe else ""


def frame_rect(hwnd) -> tuple[int, int, int, int]:
    """Visible bounds (without the invisible resize borders of Windows 10/11)."""
    r = wt.RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)) != 0:
        user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def _cloaked(hwnd) -> bool:
    v = ctypes.c_int(0)
    dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(v), ctypes.sizeof(v))
    return v.value != 0


def visible_windows() -> list[int]:
    """Top-level windows of other apps that are on screen, front to back."""
    me = os.getpid()
    found = []

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd) or _cloaked(hwnd):
            return True
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if (ex & WS_EX_TOOLWINDOW) and not (ex & WS_EX_APPWINDOW):
            return True
        if user32.GetWindow(hwnd, GW_OWNER) or _class_name(hwnd) in SKIP_CLASSES:
            return True
        if not user32.GetWindowTextLengthW(hwnd) or _pid(hwnd) == me:
            return True
        _, _, w, h = frame_rect(hwnd)
        if w >= 40 and h >= 40:
            found.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found


def covered(hwnd) -> bool:
    """Whether a visible window above this one in the z-order overlaps it."""
    tx, ty, tw, th = frame_rect(hwnd)
    me, hit = os.getpid(), [False]

    def cb(h, _):
        if h == hwnd:
            return False                     # reached the target: everything above is checked
        if not user32.IsWindowVisible(h) or user32.IsIconic(h) or _cloaked(h) or _pid(h) == me:
            return True
        ex = user32.GetWindowLongW(h, GWL_EXSTYLE)
        if ex & WS_EX_TRANSPARENT and ex & WS_EX_LAYERED:
            return True                      # click-through overlays draw (almost) nothing
        x, y, w, h_ = frame_rect(h)
        if w > 0 and h_ > 0 and x < tx + tw and tx < x + w and y < ty + th and ty < y + h_:
            hit[0] = True
            return False
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return hit[0]


def window_json(hwnd) -> dict:
    x, y, w, h = frame_rect(hwnd)
    return {"windowId": int(hwnd), "owner": owner_name(hwnd), "title": _window_text(hwnd),
            "rect": {"x": x, "y": y, "w": w, "h": h}}


def resolve_window(cfg: dict) -> int | None:
    """The configured window: by id first, then by owner + title, then by owner."""
    wid = int(cfg.get("windowId") or 0)
    if wid and user32.IsWindow(wid) and user32.IsWindowVisible(wid):
        return wid
    owner, title = cfg.get("owner") or "", cfg.get("title") or ""
    cands = [h for h in visible_windows() if owner_name(h) == owner]
    match = next((h for h in cands if _window_text(h) == title), None)
    return match or (cands[0] if cands else None)


# ── Capture ──────────────────────────────────────────────────────────────────

def primary_rect() -> tuple[int, int, int, int]:
    return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)   # SM_CXSCREEN, SM_CYSCREEN


def virtual_rect() -> tuple[int, int, int, int]:
    return tuple(user32.GetSystemMetrics(i) for i in (76, 77, 78, 79))     # SM_*VIRTUALSCREEN


_MSS = getattr(mss, "MSS", None) or mss.mss     # mss 10 renamed the class


def grab(x, y, w, h) -> Image.Image:
    with _MSS() as sct:       # one instance per call: mss handles are per thread
        shot = sct.grab({"left": x, "top": y, "width": w, "height": h})
    return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


def print_window(hwnd) -> Image.Image | None:
    """Render the window itself, even when other windows cover it."""
    wr = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(wr))
    w, h = wr.right - wr.left, wr.bottom - wr.top
    if w < 2 or h < 2:
        return None
    screen_dc = user32.GetDC(None)
    dc = gdi32.CreateCompatibleDC(screen_dc)
    bmp = gdi32.CreateCompatibleBitmap(screen_dc, w, h)
    old = gdi32.SelectObject(dc, bmp)
    try:
        if not user32.PrintWindow(hwnd, dc, PW_RENDERFULLCONTENT):
            return None
        bmi = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=w, biHeight=-h, biPlanes=1,
                               biBitCount=32, biCompression=0)
        buf = ctypes.create_string_buffer(w * h * 4)
        if not gdi32.GetDIBits(dc, bmp, 0, h, buf, ctypes.byref(bmi), 0):
            return None
        img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1).copy()
    finally:
        gdi32.SelectObject(dc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(dc)
        user32.ReleaseDC(None, screen_dc)
    fx, fy, fw, fh = frame_rect(hwnd)        # drop the invisible resize borders
    box = (fx - wr.left, fy - wr.top, fx - wr.left + fw, fy - wr.top + fh)
    if 0 <= box[0] < box[2] <= w and 0 <= box[1] < box[3] <= h:
        img = img.crop(box)
    return img


def capture_image(cfg: dict) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """The image plus the captured rect in screen coordinates (for the flash)."""
    mode = cfg.get("mode") or "full"
    if mode == "region":
        r = _rect(cfg.get("rect") or {})
        if r and r[2] >= 4 and r[3] >= 4:
            return grab(*r), r
    elif mode == "window":
        hwnd = resolve_window(cfg)
        if hwnd:
            rect = frame_rect(hwnd)
            # The screen has everything DWM composed, video overlays included; PrintWindow
            # only what the app redraws for it, so it's for windows that are covered.
            img = grab(*rect) if rect[2] > 0 and rect[3] > 0 and not covered(hwnd) else None
            if img is None:
                img = print_window(hwnd)
            # Some GPU-composed content renders black through PrintWindow; the window's
            # on-screen pixels are the next best thing when it isn't covered.
            if img is None or img.convert("L").getextrema()[1] < 8:
                img = grab(*rect) if rect[2] > 0 and rect[3] > 0 else None
            if img is not None:
                return img, rect
        log(f"window '{cfg.get('owner', '')}' not found, capturing full screen")
    r = primary_rect()
    return grab(*r), r


def crop_box(img: Image.Image, f: dict | None) -> tuple[int, int, int, int] | None:
    """frames_crop (fractions of the image) as a pixel box, or None for the whole image."""
    r = None
    try:
        r = [float(f[k]) for k in ("x", "y", "w", "h")] if f else None
    except (KeyError, TypeError, ValueError):
        pass
    if not r or r[2] <= 0.02 or r[3] <= 0.02:
        return None
    w, h = img.size
    box = (max(0, int(r[0] * w)), max(0, int(r[1] * h)), min(w, int((r[0] + r[2]) * w)), min(h, int((r[1] + r[3]) * h)))
    return box if box[2] - box[0] >= 16 and box[3] - box[1] >= 16 else None


def crop_fraction(img: Image.Image, f: dict | None) -> Image.Image:
    box = crop_box(img, f)
    return img.crop(box) if box else img


def stamp() -> str:
    now = datetime.now()
    return now.strftime("%Y%m%d_%H%M%S_") + f"{now.microsecond // 1000:03d}"


# ── Selection (Ctrl+Shift+W) ─────────────────────────────────────────────────

def _read_clipboard_text() -> str | None:
    for _ in range(10):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.03)
    else:
        return None
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = kernel32.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.wstring_at(p)
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def selected_text():
    """Copy the selection of the focused app (Ctrl+C) and print it as SELECTION:<base64>."""
    deadline = time.time() + 1.5     # Ctrl+C while Shift is still held would be Ctrl+Shift+C
    while time.time() < deadline and any(user32.GetAsyncKeyState(k) & 0x8000 for k in (VK_SHIFT, VK_CONTROL, 0x57)):
        time.sleep(0.02)
    seq = user32.GetClipboardSequenceNumber()
    user32.keybd_event(VK_CONTROL, 0, 0, None)
    user32.keybd_event(0x43, 0, 0, None)       # C
    user32.keybd_event(0x43, 0, 2, None)       # KEYEVENTF_KEYUP
    user32.keybd_event(VK_CONTROL, 0, 2, None)
    deadline = time.time() + 0.6
    while time.time() < deadline and user32.GetClipboardSequenceNumber() == seq:
        time.sleep(0.02)
    text = _read_clipboard_text() if user32.GetClipboardSequenceNumber() != seq else None
    if text:
        emit("SELECTION:" + base64.b64encode(text.encode("utf-8")).decode("ascii"))
    else:
        log("no selection found")


# ── Global hotkeys (a thread with its own message loop) ──────────────────────

def hotkey_thread(keys: dict[int, tuple[int, int]], on_hotkey, ready: threading.Event | None = None):
    def run():
        for hid, (mods, vk) in keys.items():
            if not user32.RegisterHotKey(None, hid, mods, vk):
                log(f"hotkey {hid} is taken by another app (error {ctypes.get_last_error()})")
        if ready:
            ready.set()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                on_hotkey(int(msg.wParam))
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def _hwnd_of(tk_widget) -> int:
    return user32.GetParent(tk_widget.winfo_id()) or tk_widget.winfo_id()


# ── Daemon ───────────────────────────────────────────────────────────────────

class Daemon:
    def __init__(self, out_dir: str, config_path: str | None, hotkeys_only: bool = False):
        self.hotkeys_only = hotkeys_only    # Ctrl+Shift+S → HOTKEY:SNAP (main.py grabs a video frame)
        self.out_dir = Path(out_dir)
        self.config_path = config_path
        self.actions: queue.Queue = queue.Queue()
        self.flashes: queue.Queue = queue.Queue()
        self.frame_interval: float | None = None
        self.frame_wake = threading.Event()

    def screenshot(self, full: bool = False):
        """The capture area with its crop; full: the whole primary monitor (Ctrl+Shift+M)."""
        path = self.out_dir / f"screenshot_{stamp()}.png"
        try:
            cfg = {"mode": "full"} if full else load_config(self.config_path)
            img, rect = capture_image(cfg)
            box = crop_box(img, cfg.get("frames_crop"))
            if box:
                sx, sy = rect[2] / img.width, rect[3] / img.height
                rect = (rect[0] + round(box[0] * sx), rect[1] + round(box[1] * sy),
                        round((box[2] - box[0]) * sx), round((box[3] - box[1]) * sy))
                img = img.crop(box)
            img.save(path, "PNG", compress_level=3)
        except Exception as e:
            log(f"capture failed: {e}")
            return
        emit(f"SCREENSHOT:{path}")
        self.flashes.put(rect)

    def frame(self):
        d = self.out_dir / ".frames"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"frame_{stamp()}.jpg"
        try:
            cfg = load_config(self.config_path)
            img, _ = capture_image(cfg)
            crop_fraction(img, cfg.get("frames_crop")).save(path, "JPEG", quality=85)
        except Exception as e:
            log(f"frame capture failed: {e}")
            return
        emit(f"FRAME:{path}")

    def frames_loop(self):
        next_at = time.monotonic()
        while True:
            iv = self.frame_interval
            if not iv:
                self.frame_wake.wait()
                self.frame_wake.clear()
                next_at = time.monotonic()
                continue
            delay = next_at - time.monotonic()
            if delay > 0 and self.frame_wake.wait(delay):
                self.frame_wake.clear()
                continue
            self.frame()
            next_at = max(next_at + iv, time.monotonic())    # skip ticks rather than pile them up

    def set_auto(self, interval: float | None):
        self.frame_interval = interval if interval and interval > 0 else None
        log(f"auto frames every {interval}s" if self.frame_interval else "auto frames off")
        self.frame_wake.set()

    def actions_loop(self):
        while True:
            self.actions.get()()

    def stdin_loop(self):
        for line in sys.stdin:
            cmd = line.strip().upper()
            if cmd == "SNAP":
                self.actions.put(self.screenshot)
            elif cmd == "SNAP FULL":
                self.actions.put(lambda: self.screenshot(full=True))
            elif cmd.startswith("AUTO"):
                arg = cmd[4:].strip()
                try:
                    self.set_auto(None if arg == "OFF" else float(arg or 1.0))
                except ValueError:
                    self.set_auto(1.0)
        self.set_auto(None)
        if not sys.stdin.isatty():
            os._exit(0)       # main.py is gone: don't keep the hotkeys registered

    def on_hotkey(self, hid: int):
        if hid == 1:
            log("hotkey 1 (screenshot) fired")
            if self.hotkeys_only:
                emit("HOTKEY:SNAP")
            else:
                self.actions.put(self.screenshot)
        elif hid == 2:
            log("hotkey 2 (selection) fired")
            self.actions.put(selected_text)
        elif hid == 3:
            log("hotkey 3 (full screen) fired")
            self.actions.put(lambda: self.screenshot(full=True))

    def run(self):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        for target in (self.actions_loop, self.frames_loop, self.stdin_loop):
            threading.Thread(target=target, daemon=True).start()
        hotkey_thread({1: (MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, 0x53),       # S
                       2: (MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, 0x57),       # W
                       3: (MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, 0x4D)},      # M
                      self.on_hotkey)
        log("Helper ready (Ctrl+Shift+S: screenshot, Ctrl+Shift+M: full screen, Ctrl+Shift+W: selection)")
        try:
            self.flash_ui()
        except Exception as e:     # no tkinter / no display: everything but the flash still works
            log(f"capture flash is off: {e}")
            threading.Event().wait()

    def flash_ui(self):
        """Main thread: a click-through, never-activated white flash over what was captured."""
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        win = tk.Toplevel(root, bg="white", highlightthickness=4, highlightbackground="white")
        win.overrideredirect(True)
        win.attributes("-alpha", 0.0)
        win.update_idletasks()
        hwnd = _hwnd_of(win)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, user32.GetWindowLongW(hwnd, GWL_EXSTYLE) | WS_EX_LAYERED
                              | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST)
        user32.ShowWindow(hwnd, SW_HIDE)
        state = {"step": 0}

        def fade():
            state["step"] += 1
            a = 0.30 * (1 - state["step"] / 9)
            if a <= 0:
                user32.ShowWindow(hwnd, SW_HIDE)
                return
            win.attributes("-alpha", a)
            root.after(50, fade)

        def poll():
            try:
                x, y, w, h = self.flashes.get_nowait()
            except queue.Empty:
                root.after(40, poll)
                return
            if w > 0 and h > 0:
                user32.SetWindowPos(hwnd, HWND_TOPMOST, x, y, w, h, SWP_NOACTIVATE)
                user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
                win.attributes("-alpha", 0.30)
                state["step"] = 0
                root.after(50, fade)
            root.after(40, poll)

        root.after(40, poll)
        root.mainloop()


# ── Overlays (--select-region / --pick-window) ───────────────────────────────

def overlay(mode: str) -> int:
    import tkinter as tk
    from PIL import ImageTk

    vx, vy, vw, vh = virtual_rect()
    shot = grab(vx, vy, vw, vh)
    dim = ImageEnhance.Brightness(shot).enhance(0.62)
    cands = [(h, frame_rect(h)) for h in visible_windows()] if mode == "window" else []

    root = tk.Tk()
    root.overrideredirect(True)
    root.geometry(f"{vw}x{vh}+{vx}+{vy}")
    root.attributes("-topmost", True)
    cv = tk.Canvas(root, width=vw, height=vh, highlightthickness=0, bd=0,
                   cursor="crosshair" if mode == "region" else "hand2")
    cv.pack()
    bg = ImageTk.PhotoImage(dim)
    cv.create_image(0, 0, image=bg, anchor="nw")
    lit = {"img": None, "item": None, "last": 0.0}
    box = cv.create_rectangle(0, 0, 0, 0, outline=ACCENT, width=2, state="hidden")
    pill_bg = cv.create_rectangle(0, 0, 0, 0, fill=ACCENT, outline="", state="hidden")
    pill = cv.create_text(0, 0, text="", fill="white", anchor="nw", font=("Consolas", 10), state="hidden")
    hint_text = ("Drag to select the capture area  ·  Esc to cancel" if mode == "region"
                 else "Click a window to capture it  ·  Esc to cancel")
    px, py, pw, _ = primary_rect()
    hint = cv.create_text(px - vx + pw // 2, py - vy + 70, text=hint_text, fill="white",
                          font=("Segoe UI", 13, "bold"))
    hb = cv.bbox(hint)
    cv.tag_lower(cv.create_rectangle(hb[0] - 18, hb[1] - 8, hb[2] + 18, hb[3] + 8, fill="#141414", outline=""),
                 hint)
    st = {"start": None, "rect": None, "hover": None, "done": False}

    def result(prefix: str, obj: dict | None):
        if st["done"]:
            return
        st["done"] = True
        emit(f"{prefix}:{json.dumps(obj, ensure_ascii=False)}" if obj is not None else prefix)
        root.destroy()

    def show(rect, label, force=False):
        st["rect"] = rect
        if not rect or rect[2] <= 0 or rect[3] <= 0:
            for it in (box, pill, pill_bg):
                cv.itemconfigure(it, state="hidden")
            if lit["item"]:
                cv.itemconfigure(lit["item"], state="hidden")
            return
        x, y, w, h = rect[0] - vx, rect[1] - vy, rect[2], rect[3]
        now = time.monotonic()
        if force or now - lit["last"] > 0.03:     # re-cut the bright patch at most ~30 times a second
            lit["last"] = now
            crop = shot.crop((max(0, x), max(0, y), min(vw, x + w), min(vh, y + h)))
            if crop.width > 0 and crop.height > 0:
                lit["img"] = ImageTk.PhotoImage(crop)
                if lit["item"] is None:
                    lit["item"] = cv.create_image(0, 0, anchor="nw")
                cv.itemconfigure(lit["item"], image=lit["img"], state="normal")
                cv.coords(lit["item"], max(0, x), max(0, y))
        cv.coords(box, x - 1, y - 1, x + w + 1, y + h + 1)
        cv.itemconfigure(pill, text=label, state="normal")
        ty = y + h + 8 if y + h + 40 < vh else max(0, y - 30)
        cv.coords(pill, x + 8, ty + 4)
        b = cv.bbox(pill)
        cv.coords(pill_bg, b[0] - 8, b[1] - 4, b[2] + 8, b[3] + 4)
        for it in (box, pill_bg, pill):
            cv.itemconfigure(it, state="normal")
            cv.tag_raise(it)

    def hover(e):
        if mode != "window":
            return
        gx, gy = e.x + vx, e.y + vy
        hit = next(((h, r) for h, r in cands if r[0] <= gx < r[0] + r[2] and r[1] <= gy < r[1] + r[3]), None)
        if (hit and hit[0]) == (st["hover"] and st["hover"][0]):
            return
        st["hover"] = hit
        if hit:
            owner, title = owner_name(hit[0]), _window_text(hit[0])
            show(hit[1], f"{owner} - {title}" if title else owner, force=True)
        else:
            show(None, "")

    def down(e):
        if mode == "region":
            st["start"] = (e.x + vx, e.y + vy)
        elif st["hover"]:
            result("WINDOW", window_json(st["hover"][0]))

    def drag(e):
        if mode != "region" or not st["start"]:
            return
        sx, sy = st["start"]
        gx, gy = e.x + vx, e.y + vy
        r = (min(sx, gx), min(sy, gy), abs(gx - sx), abs(gy - sy))
        show(r, f"{r[2]} × {r[3]}")

    def up(e):
        if mode != "region" or not st["start"]:
            return
        drag(e)
        r = st["rect"]
        if r and r[2] >= 12 and r[3] >= 12:
            result("REGION", {"rect": {"x": r[0], "y": r[1], "w": r[2], "h": r[3]}})
        else:
            st["start"] = None
            show(None, "")

    cancel = lambda *_: result("CANCEL", None)
    cv.bind("<Motion>", hover)
    cv.bind("<ButtonPress-1>", down)
    cv.bind("<B1-Motion>", drag)
    cv.bind("<ButtonRelease-1>", up)
    cv.bind("<ButtonPress-3>", cancel)
    root.bind("<Escape>", cancel)

    # Esc works even if Windows refuses to give us focus (a background process can't
    # always take the foreground): a global Esc hotkey for the overlay's lifetime.
    esc = threading.Event()
    hotkey_thread({9: (0, VK_ESCAPE)}, lambda _hid: esc.set())

    def poll_esc():
        if esc.is_set():
            cancel()
        elif not st["done"]:
            root.after(50, poll_esc)

    def take_focus():
        hwnd = _hwnd_of(root)
        user32.keybd_event(VK_MENU, 0, 0, None)       # an Alt tap lifts the foreground lock
        user32.keybd_event(VK_MENU, 0, 2, None)
        user32.SetForegroundWindow(hwnd)
        root.focus_force()
        if mode == "window":
            x, y = root.winfo_pointerx() - vx, root.winfo_pointery() - vy

            class E:
                pass
            ev = E()
            ev.x, ev.y = x, y
            hover(ev)

    root.after(30, take_focus)
    root.after(50, poll_esc)
    root.after(90_000, cancel)     # safety net: never leave the screen blocked
    root.mainloop()
    return 0 if st["done"] and st["rect"] is not None else 1


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    set_dpi_aware()
    args = sys.argv[1:]
    hotkeys_only = "--hotkeys-only" in args
    args = [a for a in args if a != "--hotkeys-only"]
    if not args:
        print("Usage: screenshot_helper_win.py <output-dir> [capture-config.json] [--hotkeys-only]\n"
              "       screenshot_helper_win.py --select-region | --pick-window | --list-windows\n"
              "       screenshot_helper_win.py --capture <config.json> <out.png>", file=sys.stderr)
        return 1
    if args[0] == "--list-windows":
        print(json.dumps([window_json(h) for h in visible_windows()], ensure_ascii=False))
        return 0
    if args[0] == "--capture":
        if len(args) < 3:
            print("Usage: --capture <config.json> <out.png>", file=sys.stderr)
            return 1
        try:
            img, _ = capture_image(load_config(args[1]))
            img.save(args[2], "PNG", compress_level=3)
        except Exception as e:
            log(f"capture failed: {e}")
            return 1
        emit(f"OK:{args[2]}")
        return 0
    if args[0] in ("--select-region", "--pick-window"):
        return overlay("region" if args[0] == "--select-region" else "window")
    Daemon(args[0], args[1] if len(args) > 1 else None, hotkeys_only).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
