# ==============================================================================
# desktop_native.py - Win32 DPI awareness, Alt bypass, mouse/keyboard gestures,
#                     disk-saved inspection screenshots, verified window focus
# ==============================================================================
from __future__ import annotations

import base64
import ctypes
import io
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import pyautogui
from PIL import Image

if sys.platform == "win32":
    from ctypes import wintypes

IS_WINDOWS = sys.platform == "win32"

# --- DPI awareness: force 1:1 mapping with physical 1080p pixels ---------------
_DPI_MODE = "unaware"
if IS_WINDOWS:
    # Per-Monitor-Aware V2 first (correct physical coords on EVERY monitor,
    # incl. mixed-DPI setups), then V1, then system-aware as a last resort.
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            _DPI_MODE = "per_monitor_v2"
    except Exception:
        pass
    if _DPI_MODE == "unaware":
        try:
            if ctypes.windll.shcore.SetProcessDpiAwareness(2) in (0, -2147024891):  # S_OK / E_ACCESSDENIED(already set)
                _DPI_MODE = "per_monitor"
        except Exception:
            pass
    if _DPI_MODE == "unaware":
        try:
            if ctypes.windll.user32.SetProcessDPIAware():
                _DPI_MODE = "system"
        except Exception:
            pass

try:
    # Anchor pyautogui's screenshot root away from CWD when run as a service
    _ANCHOR = Path(__file__).resolve().parent.parent
    if Path.cwd() != _ANCHOR:
        os.chdir(_ANCHOR)
except Exception:
    pass

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.03


def get_inspections_dir() -> Path:
    """Static dump folder for inspection screenshots (served at /inspections)."""
    d = Path(__file__).resolve().parent.parent / ".mcp_inspections"
    d.mkdir(parents=True, exist_ok=True)
    return d


def screen_size() -> tuple:
    w, h = pyautogui.size()
    return int(w), int(h)


def _logical_vs_physical() -> tuple:
    """(logical_w, physical_w) of the primary display. Different values mean
    this process is NOT DPI aware and every coordinate would be scaled."""
    if not IS_WINDOWS:
        w, _ = screen_size()
        return w, w
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    logical = user32.GetSystemMetrics(0)
    hdc = user32.GetDC(None)
    try:
        physical = gdi32.GetDeviceCaps(hdc, 118)  # DESKTOPHORZRES
    finally:
        user32.ReleaseDC(None, hdc)
    return int(logical), int(physical or logical)


def dpi_mode() -> str:
    return _DPI_MODE


def verify_calibration() -> dict:
    """Physical resolution, DPI mode and whether the scaling trap is active -
    for ANY resolution / scale factor (no hardcoded 1080p assumptions)."""
    w, h = screen_size()
    logical, physical = _logical_vs_physical()
    trap_active = logical != physical
    displays = []
    try:
        from modules.spatial.displays import get_displays
        displays = [d.to_dict() for d in get_displays(refresh=True, fallback_size=(w, h))]
    except Exception:
        pass
    return {
        "physical_resolution": f"{w}x{h}",
        "dpi_aware": IS_WINDOWS and not trap_active,
        "dpi_mode": _DPI_MODE,
        "scaling_trap_active": trap_active,
        "displays": displays,
        "note": ("logical != physical width: DPI awareness failed; coordinates would be scaled"
                 if trap_active else "coordinates are physical pixels on every display"),
    }


def virtual_screen() -> Dict[str, int]:
    """Bounding box of ALL monitors in physical px (may start negative)."""
    if not IS_WINDOWS:
        w, h = screen_size()
        return {"x": 0, "y": 0, "w": w, "h": h}
    m = ctypes.windll.user32.GetSystemMetrics
    return {"x": int(m(76)), "y": int(m(77)), "w": int(m(78)), "h": int(m(79))}


# ==============================================================================
# Window management with proof-of-action verification
# ==============================================================================

def _get_active_hwnd() -> Optional[int]:
    if not IS_WINDOWS:
        return None
    return ctypes.windll.user32.GetForegroundWindow()


def _get_window_title(hwnd: int) -> str:
    if not IS_WINDOWS or not hwnd:
        return ""
    length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value.strip()


def _force_foreground(hwnd: int, timeout: float = 0.5) -> bool:
    """Bring hwnd to the foreground and WAIT until the OS confirms it.

    1. plain SetForegroundWindow (works when we own the foreground lock)
    2. AttachThreadInput to the current foreground thread (no key events)
    3. ALT pulse as last resort (can flash menu bars, so never first)
    Returns True only when GetForegroundWindow() == hwnd.
    """
    if not IS_WINDOWS:
        return False
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)   # SW_RESTORE
    elif not user32.IsWindowVisible(hwnd):
        user32.ShowWindow(hwnd, 5)   # SW_SHOW

    def _wait(t: float) -> bool:
        end = time.monotonic() + t
        while time.monotonic() < end:
            if user32.GetForegroundWindow() == hwnd:
                return True
            time.sleep(0.015)
        return user32.GetForegroundWindow() == hwnd

    if user32.GetForegroundWindow() == hwnd:
        return True
    user32.SetForegroundWindow(hwnd)
    if _wait(0.12):
        return True
    fg = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    me = kernel32.GetCurrentThreadId()
    attached = False
    try:
        if fg_thread and fg_thread != me:
            attached = bool(user32.AttachThreadInput(me, fg_thread, True))
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(me, fg_thread, False)
    if _wait(0.15):
        return True
    from modules.input_lock import INPUT_LOCK
    with INPUT_LOCK:
        user32.keybd_event(0x12, 0, 0, 0)      # ALT down
        user32.SetForegroundWindow(hwnd)
        user32.keybd_event(0x12, 0, 2, 0)      # ALT up
    user32.BringWindowToTop(hwnd)
    return _wait(max(0.05, timeout - 0.27))


def _is_cloaked(hwnd: int) -> bool:
    """UWP/background windows report visible but are cloaked (DWM)."""
    try:
        val = ctypes.c_int(0)
        ctypes.windll.dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), 14, ctypes.byref(val),
                                                   ctypes.sizeof(val))
        return val.value != 0
    except Exception:
        return False


def window_rect(hwnd: int) -> Optional[Dict[str, int]]:
    """Visible window bounds (DWM extended frame - excludes the invisible
    resize borders GetWindowRect includes on Win10/11)."""
    if not IS_WINDOWS or not hwnd:
        return None
    r = wintypes.RECT()
    try:
        if ctypes.windll.dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), 9, ctypes.byref(r),
                                                      ctypes.sizeof(r)) == 0 and r.right > r.left:
            return {"x": int(r.left), "y": int(r.top), "w": int(r.right - r.left), "h": int(r.bottom - r.top)}
    except Exception:
        pass
    if ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return {"x": int(r.left), "y": int(r.top), "w": int(r.right - r.left), "h": int(r.bottom - r.top)}
    return None


def window_pid(hwnd: int) -> int:
    if not IS_WINDOWS or not hwnd:
        return 0
    pid = wintypes.DWORD(0)
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


_EXE_CACHE: Dict[int, str] = {}


def process_exe(pid: int) -> str:
    """Executable base name for a pid (cached; e.g. 'chrome.exe')."""
    if not pid:
        return ""
    if pid in _EXE_CACHE:
        return _EXE_CACHE[pid]
    name = ""
    try:
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            try:
                buf = ctypes.create_unicode_buffer(1024)
                n = wintypes.DWORD(1024)
                if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                    name = os.path.basename(buf.value).lower()
            finally:
                k32.CloseHandle(h)
    except Exception:
        name = ""
    if len(_EXE_CACHE) > 512:
        _EXE_CACHE.clear()
    _EXE_CACHE[pid] = name
    return name


def window_info(hwnd: Optional[int]) -> Dict[str, Any]:
    if not IS_WINDOWS or not hwnd:
        return {"ok": False, "error": "no window"}
    pid = window_pid(hwnd)
    return {"ok": True, "hwnd": int(hwnd), "title": _get_window_title(hwnd), "rect": window_rect(hwnd),
            "pid": pid, "exe": process_exe(pid),
            "maximized": bool(ctypes.windll.user32.IsZoomed(hwnd)),
            "minimized": bool(ctypes.windll.user32.IsIconic(hwnd))}


def foreground_info() -> Dict[str, Any]:
    return window_info(_get_active_hwnd())


def list_open_windows() -> List[Dict[str, Any]]:
    """Enumerate visible top-level windows with HWND, title and window state."""
    windows: List[Dict[str, Any]] = []
    if not IS_WINDOWS:
        return windows
    user32 = ctypes.windll.user32

    def enum_cb(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd) and not _is_cloaked(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value.strip()
                if title and title not in ("Program Manager", "Settings"):
                    if user32.IsIconic(hwnd):
                        state = "minimized"
                    elif user32.IsZoomed(hwnd):
                        state = "maximized"
                    else:
                        state = "normal"
                    windows.append({"hwnd": int(hwnd), "title": title, "state": state})
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(WNDENUMPROC(enum_cb), 0)
    return windows


def bring_window_to_front(title_or_hwnd: str, maximize: bool = False) -> Dict[str, Any]:
    """
    Unminimize + Alt-pulse + foreground a window by title substring or HWND
    string. Returns PROOF: the OS active-window HWND and title before/after.
    """
    if not IS_WINDOWS:
        return {"verified": False, "error": "Only supported on Windows."}

    before_hwnd = _get_active_hwnd()
    before_title = _get_window_title(before_hwnd)

    target_hwnd: Optional[int] = None
    target_title = title_or_hwnd
    if title_or_hwnd.isdigit():
        target_hwnd = int(title_or_hwnd)
        target_title = _get_window_title(target_hwnd) or f"HWND:{target_hwnd}"
    else:
        hint = title_or_hwnd.lower()
        matches = [w for w in list_open_windows() if hint in w["title"].lower()]
        # prefer: already-foreground match > non-minimized (z-order) > minimized
        matches.sort(key=lambda w: (w["hwnd"] != before_hwnd, w["state"] == "minimized"))
        if matches:
            target_hwnd = matches[0]["hwnd"]
            target_title = matches[0]["title"]
    if not target_hwnd:
        return {
            "verified": False,
            "error": f"No open window matching '{title_or_hwnd}' found.",
            "active_window_before": {"hwnd": before_hwnd, "title": before_title},
        }

    _force_foreground(target_hwnd)
    if maximize:
        ctypes.windll.user32.ShowWindow(target_hwnd, 3)  # SW_MAXIMIZE
        time.sleep(0.12)

    after_hwnd = _get_active_hwnd()
    after_title = _get_window_title(after_hwnd)
    verified = (after_hwnd == target_hwnd)

    return {
        "verified": verified,
        "hwnd": target_hwnd,
        "target_title": target_title,
        "active_window_before": {"hwnd": before_hwnd, "title": before_title},
        "active_window_title": after_title,
        "active_window_after": {"hwnd": after_hwnd, "title": after_title},
        "note": "" if verified else
                "OS reports a different foreground window after the Alt-pulse "
                "(focus lock may have won). Try again or use keyboard_hotkey(['alt','tab']).",
    }


def minimize_window(title_keyword: str) -> str:
    """Minimize the first window whose title contains the keyword."""
    if not IS_WINDOWS:
        return "Only supported on Windows."
    for w in list_open_windows():
        if title_keyword.lower() in w["title"].lower():
            ctypes.windll.user32.ShowWindow(w["hwnd"], 6)  # SW_MINIMIZE
            return f"Minimized window '{w['title']}'."
    return f"Window matching '{title_keyword}' not found."


# ==============================================================================
# Screenshots: disk-first artifacts + dual-coordinate scale matrix
# ==============================================================================

def _artifact_paths(name: Optional[str] = None) -> Dict[str, str]:
    d = get_inspections_dir()
    fname = f"{name or 'latest'}.jpg"
    path = d / fname
    return {
        "inspection_image_path": str(path),
        "inspection_image_url": f"{_base_url()}/inspections/{fname}",
    }


def _base_url() -> str:
    try:
        import config
        return config.LOCAL_BASE_URL
    except Exception:
        return "http://127.0.0.1:8000"


_GDI_READY = False


def _gdi_setup() -> None:
    """Declare 64-bit-safe signatures once (default ctypes int restype would
    truncate HANDLEs on x64 and crash intermittently)."""
    global _GDI_READY
    if _GDI_READY:
        return
    u, g = ctypes.windll.user32, ctypes.windll.gdi32
    H = ctypes.c_void_p
    u.GetDC.argtypes, u.GetDC.restype = [H], H
    u.ReleaseDC.argtypes, u.ReleaseDC.restype = [H, H], ctypes.c_int
    g.CreateCompatibleDC.argtypes, g.CreateCompatibleDC.restype = [H], H
    g.CreateCompatibleBitmap.argtypes, g.CreateCompatibleBitmap.restype = [H, ctypes.c_int, ctypes.c_int], H
    g.SelectObject.argtypes, g.SelectObject.restype = [H, H], H
    g.BitBlt.argtypes = [H, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, H,
                         ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    g.BitBlt.restype = wintypes.BOOL
    g.GetDIBits.argtypes = [H, H, wintypes.UINT, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
    g.GetDIBits.restype = ctypes.c_int
    g.DeleteObject.argtypes, g.DeleteObject.restype = [H], wintypes.BOOL
    g.DeleteDC.argtypes, g.DeleteDC.restype = [H], wintypes.BOOL
    _GDI_READY = True


def _gdi_grab(x: int, y: int, w: int, h: int):
    _gdi_setup()

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                    ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                    ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

    u, g = ctypes.windll.user32, ctypes.windll.gdi32
    sdc = u.GetDC(None)
    if not sdc:
        return None
    mdc = bmp = old = None
    try:
        mdc = g.CreateCompatibleDC(sdc)
        bmp = g.CreateCompatibleBitmap(sdc, w, h)
        if not mdc or not bmp:
            return None
        old = g.SelectObject(mdc, bmp)
        if not g.BitBlt(mdc, 0, 0, w, h, sdc, x, y, 0x00CC0020 | 0x40000000):  # SRCCOPY|CAPTUREBLT
            return None
        bih = BITMAPINFOHEADER()
        bih.biSize, bih.biWidth, bih.biHeight = ctypes.sizeof(BITMAPINFOHEADER), w, -h  # top-down
        bih.biPlanes, bih.biBitCount, bih.biCompression = 1, 32, 0
        buf = ctypes.create_string_buffer(w * h * 4)
        if g.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bih), 0) != h:
            return None
        return Image.frombuffer("RGB", (w, h), buf.raw, "raw", "BGRX", 0, 1)
    finally:
        if old:
            g.SelectObject(mdc, old)
        if bmp:
            g.DeleteObject(bmp)
        if mdc:
            g.DeleteDC(mdc)
        u.ReleaseDC(None, sdc)


def grab(x: Optional[int] = None, y: Optional[int] = None, w: Optional[int] = None,
         h: Optional[int] = None):
    """Capture physical pixels INTO MEMORY (never disk). Any monitor, incl.
    negative coordinates. Returns a PIL RGB image or None on failure
    (locked workstation / secure desktop / invalid rect)."""
    vs = virtual_screen()
    if x is None:
        x, y, w, h = vs["x"], vs["y"], vs["w"], vs["h"]
    x0, y0 = max(int(x), vs["x"]), max(int(y), vs["y"])
    x1, y1 = min(int(x) + int(w), vs["x"] + vs["w"]), min(int(y) + int(h), vs["y"] + vs["h"])
    if x1 - x0 < 1 or y1 - y0 < 1:
        return None
    if IS_WINDOWS:
        try:
            img = _gdi_grab(x0, y0, x1 - x0, y1 - y0)
            if img is not None:
                return img
        except Exception:
            pass
    try:
        from PIL import ImageGrab
        return ImageGrab.grab(bbox=(x0, y0, x1, y1), all_screens=True).convert("RGB")
    except Exception:
        return None


def prune_inspections(keep: int = 20) -> int:
    """Old code wrote one region_*.jpg per click forever. Keep the newest few."""
    try:
        files = sorted(get_inspections_dir().glob("region_*.jpg"), key=lambda p: p.stat().st_mtime)
        for p in files[:-keep] if keep else files:
            try:
                p.unlink()
            except Exception:
                pass
        return max(0, len(files) - keep)
    except Exception:
        return 0


def take_screenshot(scaled_width: Optional[int] = 1280, quality: int = 70,
                    return_base64: bool = False) -> Dict[str, Any]:
    """
    Capture the full physical desktop. Writes .mcp_inspections/latest.jpg and
    returns path + URL + the exact coordinate conversion matrix so the agent
    can map image pixels -> physical screen pixels with zero drift.
    Set return_base64=True only when the raw image is genuinely needed.
    """
    shot = pyautogui.screenshot()
    phys_w, phys_h = shot.size
    img_w, img_h = phys_w, phys_h
    if scaled_width and phys_w > scaled_width:
        ratio = scaled_width / float(phys_w)
        shot = shot.resize((scaled_width, int(phys_h * ratio)), Image.Resampling.LANCZOS)
        img_w, img_h = shot.size

    artifacts = _artifact_paths("latest")
    try:
        shot.convert("RGB").save(artifacts["inspection_image_path"], format="JPEG", quality=quality)
        saved = True
    except Exception as e:
        saved = False
        artifacts["save_error"] = str(e)

    result: Dict[str, Any] = {
        "status": "ok" if saved else "save_failed",
        "physical_width": phys_w,
        "physical_height": phys_h,
        "image_width": img_w,
        "image_height": img_h,
        "scale_x": round(phys_w / img_w, 4) if img_w else 1.0,
        "scale_y": round(phys_h / img_h, 4) if img_h else 1.0,
        "coordinate_mapping": (
            f"physical_x = image_x * {round(phys_w / img_w, 4)}; "
            f"physical_y = image_y * {round(phys_h / img_h, 4)}"
        ),
        **artifacts,
    }
    if return_base64:
        buf = io.BytesIO()
        shot.convert("RGB").save(buf, format="JPEG", quality=quality)
        result["base64_jpeg"] = base64.b64encode(buf.getvalue()).decode("utf-8")
    return result


def take_region_screenshot(x: int, y: int, width: int, height: int, quality: int = 80,
                           return_base64: bool = False) -> Dict[str, Any]:
    """
    Unscaled cropped capture of (X, Y, W, H) in PHYSICAL pixels - 1:1 with
    mouse_click coordinates, no conversion needed. Writes an inspection file.
    """
    vs = virtual_screen()
    x = max(vs["x"], min(int(x), vs["x"] + vs["w"] - 1))
    y = max(vs["y"], min(int(y), vs["y"] + vs["h"] - 1))
    width = max(10, min(int(width), vs["x"] + vs["w"] - x))
    height = max(10, min(int(height), vs["y"] + vs["h"] - y))
    shot = grab(x, y, width, height)
    if shot is None:
        shot = pyautogui.screenshot(region=(x, y, width, height))

    artifacts = _artifact_paths(f"region_{uuid.uuid4().hex[:8]}")
    try:
        shot.convert("RGB").save(artifacts["inspection_image_path"], format="JPEG", quality=quality)
        status = "ok"
    except Exception as e:
        status = "save_failed"
        artifacts["save_error"] = str(e)

    result: Dict[str, Any] = {
        "status": status,
        "region": {"x": x, "y": y, "width": width, "height": height},
        "scale_x": 1.0,
        "scale_y": 1.0,
        "image_width": width,
        "image_height": height,
        "coordinate_mapping": "region is unscaled: image pixels == physical screen pixels",
        **artifacts,
    }
    if return_base64:
        buf = io.BytesIO()
        shot.convert("RGB").save(buf, format="JPEG", quality=quality)
        result["base64_jpeg"] = base64.b64encode(buf.getvalue()).decode("utf-8")
    return result


# ==============================================================================
# Mouse gestures (legacy pyautogui paths used by web_task/browser_cdp).
# Every gesture holds the global INPUT_LOCK so it can never interleave with a
# `point` / `spatial_point` / `chrome_session` gesture from another request.
# ==============================================================================
import functools as _functools


def _input_locked(fn):
    @_functools.wraps(fn)
    def _wrap(*a, **kw):
        from modules.input_lock import INPUT_LOCK
        with INPUT_LOCK:
            return fn(*a, **kw)
    return _wrap


@_input_locked
def mouse_click(x: int, y: int, button: str = "left", clicks: int = 1, interval: float = 0.05) -> str:
    """Single/double/triple click with left, right or middle button at physical coords."""
    pyautogui.click(x=x, y=y, clicks=clicks, interval=interval, button=button)
    return f"Executed {clicks}x {button} click(s) at ({x}, {y})."


@_input_locked
def mouse_drag(start_x: int, start_y: int, end_x: int, end_y: int, button: str = "left", duration: float = 0.3) -> str:
    """Continuous press-and-drag between two coordinate pairs."""
    pyautogui.moveTo(start_x, start_y)
    pyautogui.dragTo(end_x, end_y, duration=duration, button=button)
    return f"Dragged mouse from ({start_x}, {start_y}) to ({end_x}, {end_y})."


@_input_locked
def drag_with_modifiers(keys: List[str], start_x: int, start_y: int, end_x: int, end_y: int, duration: float = 0.3) -> str:
    """Hold modifier keys (ctrl/shift/alt) while dragging - area selects, custom snips."""
    for key in keys:
        pyautogui.keyDown(key)
    try:
        pyautogui.moveTo(start_x, start_y)
        pyautogui.dragTo(end_x, end_y, duration=duration, button="left")
    finally:
        for key in reversed(keys):
            pyautogui.keyUp(key)
    return f"Executed drag ({start_x},{start_y})->({end_x},{end_y}) holding {keys}."


@_input_locked
def mouse_scroll(amount: int) -> str:
    """Scroll the wheel; positive = up, negative = down."""
    pyautogui.scroll(amount)
    return f"Scrolled {amount} units."


# ==============================================================================
# Keyboard & clipboard (with focus-losing verification helpers)
# ==============================================================================

def _active_window_title() -> str:
    hwnd = _get_active_hwnd()
    return _get_window_title(hwnd) if hwnd else ""


@_input_locked
def keyboard_type(text: str, press_enter: bool = False) -> Dict[str, Any]:
    """
    Type into the focused element. Captures the focused window title before and
    after so the caller can detect focus loss mid-typing (web-context agents
    should confirm via read_focused_element_value).
    """
    hwnd_before = _get_active_hwnd()
    title_before = _get_window_title(hwnd_before) if hwnd_before else ""

    pyautogui.write(text, interval=0.01)
    if press_enter:
        pyautogui.press("enter")

    hwnd_after = _get_active_hwnd()
    title_after = _get_window_title(hwnd_after) if hwnd_after else ""
    return {
        "typed_chars": len(text),
        "focused_window_before": title_before,
        "active_window_title": title_after,
        "focus_lost_during_typing": hwnd_before != hwnd_after,
    }


@_input_locked
def keyboard_hotkey(keys: List[str]) -> str:
    """Trigger system hotkeys, e.g. ['ctrl','c'] or ['alt','tab']."""
    pyautogui.hotkey(*keys)
    return f"Triggered hotkey: {' + '.join(keys)}"


@_input_locked
def set_clipboard_and_paste(text: str) -> Dict[str, Any]:
    """
    Clipboard-based insertion (no keystroke lag). Captures focused window
    before/after to prove focus was retained during the paste.
    """
    import pyperclip
    hwnd_before = _get_active_hwnd()
    title_before = _get_window_title(hwnd_before) if hwnd_before else ""

    pyperclip.copy(text)
    time.sleep(0.05)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.1)

    hwnd_after = _get_active_hwnd()
    title_after = _get_window_title(hwnd_after) if hwnd_after else ""
    return {
        "pasted_chars": len(text),
        "focused_window_before": title_before,
        "active_window_title": title_after,
        "focus_lost_during_paste": hwnd_before != hwnd_after,
        "hint": "For web contexts, confirm the input landed via read_focused_element_value.",
    }


def read_clipboard() -> str:
    """Read the current OS clipboard string."""
    import pyperclip
    content = pyperclip.paste()
    return content if content else "[Clipboard is empty]"
