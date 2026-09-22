# ==============================================================================
# chrome_control.py - ONE Chrome session engine (no more 4 CDP toys).
#
# Strategy: "twin debug profile" attach. Since Chrome 136, Google BLOCKS
# --remote-debugging-port on the DEFAULT User Data dir (flag silently ignored).
# So instead of ever touching the user's running Chrome (never_kill is law):
#   1. If :9222 already listens -> attach (works for any Chrome that has it).
#   2. Else spawn a DEDICATED debug-profile Chrome with CDP. The user logs into
#      their sites in that profile once; cookies/sessions persist on disk.
#   3. CDP is an accelerator, never a hard dependency - callers fall back to
#      UIA/vision when this module reports cdp_available=False.
# ==============================================================================
from __future__ import annotations

import os
import socket
import subprocess
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

try:
    from .. import config
except ImportError:  # direct script execution
    import config  # type: ignore

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]

# Dedicated debug profile - created once, logged in once, sessions persist.
DEBUG_USER_DATA_DIR = str(config.PROJECT_ROOT / ".chrome_debug_profile")


# ------------------------------------------------------------------------------
# CDP liveness
# ------------------------------------------------------------------------------

def cdp_listening(port: Optional[int] = None, timeout: float = 0.6) -> bool:
    """True if something accepts TCP on the CDP port."""
    p = port or config.CDP_PORT
    try:
        with socket.create_connection(("127.0.0.1", p), timeout=timeout):
            return True
    except Exception:
        return False


def _find_chrome_exe() -> Optional[str]:
    for path in CHROME_CANDIDATES:
        if os.path.isfile(path):
            return path
    return None


def _cdp_json(path: str, timeout: float = 3.0) -> Optional[Any]:
    import urllib.request
    try:
        with urllib.request.urlopen(f"{config.CDP_ENDPOINT}{path}", timeout=timeout) as r:
            import json
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


# ------------------------------------------------------------------------------
# Win32 window helpers (focus the Chrome window without CDP)
# ------------------------------------------------------------------------------

def _win32():
    import ctypes

    class W:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        EnumWindows = user32.EnumWindows
        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        IsWindowVisible = user32.IsWindowVisible
        GetWindowTextW = user32.GetWindowTextW
        GetWindowTextLengthW = user32.GetWindowTextLengthW
        IsIconic = user32.IsIconic
        ShowWindow = user32.ShowWindow
        SetForegroundWindow = user32.SetForegroundWindow
        GetWindowRect = user32.GetWindowRect

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    W.user32.keybd_event(0x12, 0, 0, 0)  # placeholder to keep linters calm
    return W


def _enum_windows():
    import ctypes
    W = _win32()
    hwnds = []

    @W.EnumWindowsProc
    def cb(hwnd, _):
        if W.IsWindowVisible(hwnd):
            length = W.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                W.GetWindowTextW(hwnd, buf, length + 1)
                hwnds.append((hwnd, buf.value))
        return True

    W.EnumWindows(cb, None)
    return hwnds


def focus_chrome_window(title_hint: str = "") -> Dict[str, Any]:
    """
    Bring a Chrome window to the foreground via the Alt-pulse bypass.
    Matches title_hint as a substring; empty hint = first visible Chrome window.
    Returns proof of what is actually foreground now.
    """
    import ctypes
    W = _win32()

    candidates = []
    for hwnd, title in _enum_windows():
        low = title.lower()
        if "chrome" in low or (title_hint and title_hint.lower() in low):
            candidates.append((hwnd, title))
    if not candidates:
        return {"verified": False, "error": f"No Chrome window found (hint={title_hint!r})."}

    hwnd, title = candidates[0]
    if W.IsIconic(hwnd):
        W.ShowWindow(hwnd, 9)  # SW_RESTORE
        time.sleep(0.2)
    # Alt-key pulse defeats Windows foreground locks
    W.user32.keybd_event(0x12, 0, 0, 0)
    W.user32.keybd_event(0x12, 0, 2, 0)
    ok = bool(W.SetForegroundWindow(hwnd))
    time.sleep(0.25)

    fg = W.user32.GetForegroundWindow()
    fg_len = W.GetWindowTextLengthW(fg)
    buf = ctypes.create_unicode_buffer(fg_len + 1) if fg_len else None
    fg_title = ""
    if buf:
        W.GetWindowTextW(fg, buf, fg_len + 1)
        fg_title = buf.value

    return {
        "verified": ok and fg == hwnd,
        "focused_hwnd": int(fg),
        "focused_title": fg_title,
        "target_title": title,
    }


# ------------------------------------------------------------------------------
# Ops
# ------------------------------------------------------------------------------

def op_status() -> Dict[str, Any]:
    chrome = _find_chrome_exe()
    listening = cdp_listening()
    info = _cdp_json("/json/version") if listening else None
    return {
        "cdp_available": listening,
        "cdp_endpoint": config.CDP_ENDPOINT,
        "browser": (info or {}).get("Browser"),
        "debug_profile_exists": os.path.isdir(DEBUG_USER_DATA_DIR),
        "chrome_exe_found": chrome,
        "policy": "never_kill",
    }


def op_ensure_debug(start_url: str = "about:blank") -> Dict[str, Any]:
    """
    Get a usable CDP session without ever killing Chrome.
    Attaches to existing :9222 if present; otherwise spawns the dedicated
    debug-profile Chrome (its own user-data-dir -> the Chrome 136+ block does
    not apply). Safe to call every time; idempotent.
    """
    if cdp_listening():
        info = _cdp_json("/json/version")
        return {"cdp_available": True, "action": "attached_existing",
                "browser": (info or {}).get("Browser")}

    chrome = _find_chrome_exe()
    if not chrome:
        return {"cdp_available": False, "error": "chrome.exe not found in standard locations."}

    os.makedirs(DEBUG_USER_DATA_DIR, exist_ok=True)
    flags = [
        chrome,
        f"--remote-debugging-port={config.CDP_PORT}",
        f"--user-data-dir={DEBUG_USER_DATA_DIR}",
        # Window geometry keeps viewport math deterministic on the 1080p panel
        "--window-position=0,0", "--window-size=1280,900",
        "--no-first-run", "--no-default-browser-check",
        "--disable-features=Translate,MediaRouter",
        # CRITICAL: keep the renderer fully alive when the window is occluded
        # (behind other windows). Without these, Chrome throttles setTimeout
        # in hidden pages and assistant-reply polling silently starves.
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-background-timer-throttling",
        start_url,
    ]
    try:
        subprocess.Popen(flags, creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    except Exception as e:
        return {"cdp_available": False, "error": f"Failed to spawn debug Chrome: {e}"}

    deadline = time.time() + 15
    while time.time() < deadline:
        if cdp_listening():
            return {"cdp_available": True, "action": "spawned_debug_profile",
                    "user_data_dir": DEBUG_USER_DATA_DIR,
                    "note": "Log into your sites inside this window once; sessions persist for all future runs."}
        time.sleep(0.4)
    return {"cdp_available": False, "error": "Debug Chrome spawned but CDP port never opened."}


def op_tabs() -> Dict[str, Any]:
    if not cdp_listening():
        return {"cdp_available": False, "tabs": []}
    raw = _cdp_json("/json/list") or []
    tabs = [
        {"id": t.get("id"), "url": t.get("url"), "title": t.get("title"),
         "type": t.get("type")}
        for t in raw if t.get("type") == "page"
    ]
    return {"cdp_available": True, "tabs": tabs}


def op_focus(url_hint: str = "") -> Dict[str, Any]:
    """Focus a tab by URL hint via CDP activate; falls back to window title."""
    if url_hint and cdp_listening():
        raw = _cdp_json("/json/list") or []
        for t in raw:
            if t.get("type") == "page" and url_hint.lower() in (t.get("url") or "").lower():
                import urllib.request
                try:
                    urllib.request.urlopen(
                        f"{config.CDP_ENDPOINT}/json/activate/{t['id']}", timeout=3)
                    return {"verified": True, "method": "cdp_activate",
                            "tab": {"url": t.get("url"), "title": t.get("title")}}
                except Exception:
                    pass
    return focus_chrome_window(url_hint)


def op_open_tab(url: str) -> Dict[str, Any]:
    """Open a URL as a new tab via CDP /json/new (works even pre-Playwright)."""
    if not cdp_listening():
        return {"opened": False, "error": "CDP not available; call ensure_debug first."}
    import urllib.request
    try:
        quoted = urllib.parse.quote(url, safe=":/?&=%+#@~")
        with urllib.request.urlopen(f"{config.CDP_ENDPOINT}/json/new?{quoted}", timeout=5) as r:
            import json
            data = json.loads(r.read().decode("utf-8", "replace"))
        return {"opened": True, "tab": {"id": data.get("id"), "url": data.get("url")}}
    except Exception as e:
        return {"opened": False, "error": str(e)}


def dispatch(op: str, **kwargs) -> Dict[str, Any]:
    ops = {
        "status": op_status,
        "ensure_debug": op_ensure_debug,
        "tabs": op_tabs,
        "focus": lambda: op_focus(kwargs.get("url", "")),
        "open": lambda: op_open_tab(kwargs.get("url", "")),
    }
    fn = ops.get(op)
    if fn is None:
        return {"error": f"Unknown op '{op}'. Valid: {sorted(ops)}"}
    return fn()
