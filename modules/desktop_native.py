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
if IS_WINDOWS:
    try:
        # Per-monitor DPI awareness v2 (1920x1080 native coordinate mapping)
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
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


def verify_calibration() -> dict:
    """Physical resolution + whether the 125%/150% scaling trap is active."""
    w, h = screen_size()
    trap_active = (w == 1536 and h == 864)  # 1920/1.25 x 1080/1.25
    return {
        "physical_resolution": f"{w}x{h}",
        "dpi_aware": IS_WINDOWS and not trap_active,
        "scaling_trap_active": trap_active,
        "expected": "1920x1080",
        "note": "1536x864 indicates per-monitor DPI awareness failed; coordinates would be off by 25%.",
    }


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


def _force_foreground(hwnd: int) -> bool:
    """
    Win32 Alt-key injection bypass: Windows blocks SetForegroundWindow from
    background processes; pulsing ALT right before the call unlocks it.
    """
    if not IS_WINDOWS:
        return False
    user32 = ctypes.windll.user32
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)   # SW_RESTORE
    else:
        user32.ShowWindow(hwnd, 5)   # SW_SHOW
    user32.keybd_event(0x12, 0, 0, 0)      # ALT down
    user32.SetForegroundWindow(hwnd)
    user32.keybd_event(0x12, 0, 2, 0)      # ALT up
    user32.BringWindowToTop(hwnd)
    return True


def list_open_windows() -> List[Dict[str, Any]]:
    """Enumerate visible top-level windows with HWND, title and window state."""
    windows: List[Dict[str, Any]] = []
    if not IS_WINDOWS:
        return windows
    user32 = ctypes.windll.user32

    def enum_cb(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
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
        for w in list_open_windows():
            if title_or_hwnd.lower() in w["title"].lower():
                target_hwnd = w["hwnd"]
                target_title = w["title"]
                break
    if not target_hwnd:
        return {
            "verified": False,
            "error": f"No open window matching '{title_or_hwnd}' found.",
            "active_window_before": {"hwnd": before_hwnd, "title": before_title},
        }

    _force_foreground(target_hwnd)
    if maximize:
        ctypes.windll.user32.ShowWindow(target_hwnd, 3)  # SW_MAXIMIZE
    time.sleep(0.35)  # allow OS focus transition to settle

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
    sw, sh = screen_size()
    x = max(0, min(x, sw - 1))
    y = max(0, min(y, sh - 1))
    width = max(10, min(width, sw - x))
    height = max(10, min(height, sh - y))
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
# Mouse gestures
# ==============================================================================

def mouse_click(x: int, y: int, button: str = "left", clicks: int = 1, interval: float = 0.05) -> str:
    """Single/double/triple click with left, right or middle button at physical coords."""
    pyautogui.click(x=x, y=y, clicks=clicks, interval=interval, button=button)
    return f"Executed {clicks}x {button} click(s) at ({x}, {y})."


def mouse_drag(start_x: int, start_y: int, end_x: int, end_y: int, button: str = "left", duration: float = 0.3) -> str:
    """Continuous press-and-drag between two coordinate pairs."""
    pyautogui.moveTo(start_x, start_y)
    pyautogui.dragTo(end_x, end_y, duration=duration, button=button)
    return f"Dragged mouse from ({start_x}, {start_y}) to ({end_x}, {end_y})."


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


def keyboard_hotkey(keys: List[str]) -> str:
    """Trigger system hotkeys, e.g. ['ctrl','c'] or ['alt','tab']."""
    pyautogui.hotkey(*keys)
    return f"Triggered hotkey: {' + '.join(keys)}"


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
