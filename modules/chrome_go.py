"""Real-Chrome go/type/keys. Never kill Chrome, never picker, never debug profile."""
from __future__ import annotations

import subprocess
import time
from ctypes import wintypes
import ctypes
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from . import chrome_profiles, keys, until_proof

user32 = ctypes.WinDLL("user32", use_last_error=True)

EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.BringWindowToTop.restype = wintypes.BOOL

SW_RESTORE = 9
SW_SHOW = 5


def _class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _title(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def list_chrome_windows() -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []

    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        cls = _class_name(hwnd)
        if cls not in ("Chrome_WidgetWin_1", "Chrome_WidgetWin_0"):
            return True
        title = _title(hwnd)
        if not title:
            return True
        found.append({"hwnd": int(hwnd), "title": title, "class": cls})
        return True

    user32.EnumWindows(EnumWindowsProc(_cb), 0)
    return found


def focus_hwnd(hwnd: int) -> bool:
    """Focus and PROVE it (GetForegroundWindow() == hwnd). Only restores
    minimized windows - SW_RESTORE on a maximized window un-maximizes it."""
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    try:
        from . import desktop_native
        return desktop_native._force_foreground(hwnd)
    except Exception:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        return int(user32.GetForegroundWindow() or 0) == int(hwnd)


def _host(url: str) -> str:
    if not url:
        return ""
    raw = url if "://" in url else "https://" + url
    try:
        return (urlparse(raw).hostname or "").lower()
    except Exception:
        return url.lower()


def _title_matches_legacy(title: str, url: str) -> bool:
    t = (title or "").lower()
    host = _host(url)
    if not host:
        return False
    short = host.replace("www.", "")
    first = short.split(".")[0]
    aliases = {
        "notebook": ("notebooklm", "notebook"),
        "mail": ("gmail", "mail.google"),
        "docs": ("google docs", "docs.google"),
        "drive": ("google drive", "drive.google"),
    }
    if first in aliases and any(a in t for a in aliases[first]):
        return True
    return first in t or short in t


def _focus_matching(url: str) -> Optional[Dict[str, Any]]:
    for w in list_chrome_windows():
        if _title_matches(w["title"], url):
            focus_hwnd(w["hwnd"])
            return w
    return None


def _focus_any_chrome() -> Optional[Dict[str, Any]]:
    """Topmost Chrome window (EnumWindows is z-ordered), focused WITH proof.
    Returns the window dict with 'focused': bool."""
    wins = list_chrome_windows()
    if not wins:
        return None
    fg = int(user32.GetForegroundWindow() or 0)
    w = next((x for x in wins if x["hwnd"] == fg), wins[0])
    w = dict(w)
    w["focused"] = (w["hwnd"] == fg) or focus_hwnd(w["hwnd"])
    return w


def _refuse_unfocused(w: Dict[str, Any], what: str) -> Optional[Dict[str, Any]]:
    # Typing into whatever else has focus (a terminal, a chat box) is the
    # worst possible failure mode - refuse instead.
    if not w.get("focused"):
        return {"status": "focus_failed", "error": f"Chrome window {w.get('title')!r} did not take "
                f"the foreground; refusing to send {what} into another window"}
    return None


_REGISTRY = None


def get_registry():
    """Process-wide Chrome profile registry wired to the real Win32 providers."""
    global _REGISTRY
    if _REGISTRY is None:
        from .runtime.chrome_registry import ChromeRegistry, ProfileInfo
        seed = [ProfileInfo(directory=chrome_profiles.KARTIK["directory"], name=chrome_profiles.KARTIK["gaia"],
                            email=chrome_profiles.KARTIK["email"], gaia_name=chrome_profiles.KARTIK["gaia"],
                            aliases=["Kartik"])]
        _REGISTRY = ChromeRegistry(windows_provider=list_chrome_windows,
                                   foreground_provider=lambda: int(user32.GetForegroundWindow() or 0), seed=seed)
    return _REGISTRY


def _launch(argv: List[str]) -> bool:
    try:
        subprocess.Popen(argv, close_fds=False)
        return True
    except (FileNotFoundError, OSError):
        return False


def op_go(url: str = "", profile: str = "Kartik", until: str = "") -> Dict[str, Any]:
    """Open `url` in the REQUESTED Chrome profile and verify it stage by stage.
    already_open=True only when that profile's own foreground window shows the
    destination (see runtime/chrome_flow.py for the state machine)."""
    guard = chrome_profiles.refuse_debug_identity("go", profile, url)
    if guard:
        return guard
    from .runtime import chrome_flow, get_runtime
    rt = get_runtime()
    out = chrome_flow.go(get_registry(), (url or "").strip() or "https://notebook.google.com", profile or "Kartik", until,
                         focus=focus_hwnd, launch=_launch, exe=chrome_profiles.CHROME_EXE,
                         emit=lambda kind, **d: rt.events.emit(kind, **d))
    try:
        fg = (out.get("observed_profile") or {})
        rt.update_browser({"profile": fg.get("name"), "directory": fg.get("directory"),
                           "profile_verified": fg.get("verified"), "page_title": out.get("title"),
                           "url": out.get("url") if out.get("verified") else None})
    except Exception:
        pass
    # legacy keys older clients read
    rp = out.get("requested_profile") or {}
    out.setdefault("profile", rp.get("name"))
    out.setdefault("directory", rp.get("directory"))
    return out


def op_profiles() -> Dict[str, Any]:
    return {"status": "ok", "profiles": get_registry().registry_view()}


def op_profile() -> Dict[str, Any]:
    """Which profile is ACTUALLY in the foreground Chrome window right now."""
    obs = get_registry().observe()
    return {"status": "ok", "foreground": obs["foreground"], "windows": obs["windows"]}


def op_type(text: str = "", submit: bool = False) -> Dict[str, Any]:
    w = _focus_any_chrome()
    if not w:
        return {"status": "error", "error": "no Chrome window to type into"}
    refused = _refuse_unfocused(w, 'text')
    if refused:
        return refused
    typed = keys.type_text(text, submit=submit)
    typed["window"] = w["title"]
    return typed


def op_keys(combo: str = "") -> Dict[str, Any]:
    w = _focus_any_chrome()
    if not w:
        return {"status": "error", "error": "no Chrome window for keys"}
    refused = _refuse_unfocused(w, 'keys')
    if refused:
        return refused
    sent = keys.send_keys(combo)
    sent["window"] = w["title"]
    return sent


def op_urlbar() -> Dict[str, Any]:
    w = _focus_any_chrome()
    if not w:
        return {"status": "error", "error": "no Chrome window for urlbar"}
    refused = _refuse_unfocused(w, 'urlbar')
    if refused:
        return refused
    sent = keys.send_keys("ctrl+l")
    sent["op"] = "urlbar"
    sent["window"] = w["title"]
    return sent
