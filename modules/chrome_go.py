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
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.BringWindowToTop(hwnd)
    return bool(user32.SetForegroundWindow(hwnd))


def _host(url: str) -> str:
    if not url:
        return ""
    raw = url if "://" in url else "https://" + url
    try:
        return (urlparse(raw).hostname or "").lower()
    except Exception:
        return url.lower()


def _title_matches(title: str, url: str) -> bool:
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
    wins = list_chrome_windows()
    if not wins:
        return None
    w = wins[0]
    focus_hwnd(w["hwnd"])
    return w


def op_go(url: str = "", profile: str = "Kartik", until: str = "") -> Dict[str, Any]:
    t0 = time.perf_counter()
    guard = chrome_profiles.refuse_debug_identity("go", profile, url)
    if guard:
        return guard
    resolved = chrome_profiles.resolve_profile(profile)
    if not resolved.get("ok"):
        return resolved
    directory = resolved["directory"]
    target = (url or "").strip()
    if not target:
        target = "https://notebook.google.com"

    existing = _focus_matching(target)
    if existing:
        proof = until_proof.check_until(until, url=target, title=existing["title"])
        return {
            "status": "ok" if proof.get("until_ok", True) else "launched_unverified",
            "already_open": True,
            "profile": resolved["name"],
            "directory": directory,
            "email": resolved["email"],
            "url": target,
            "title": existing["title"],
            "proof": proof,
            "ms": int((time.perf_counter() - t0) * 1000),
        }

    argv = [
        chrome_profiles.CHROME_EXE,
        f"--profile-directory={directory}",
        target,
    ]
    try:
        subprocess.Popen(argv, close_fds=False)
    except FileNotFoundError:
        return {
            "status": "error",
            "error": f"chrome.exe not found at {chrome_profiles.CHROME_EXE}",
        }

    matched = None
    for _ in range(20):
        time.sleep(0.1)
        matched = _focus_matching(target)
        if matched:
            break
    title = matched["title"] if matched else ""
    proof = until_proof.check_until(until, url=target, title=title)
    ok = bool(matched) or not until
    return {
        "status": "ok" if (ok and proof.get("until_ok", True)) else "launched_unverified",
        "already_open": False,
        "launched": True,
        "profile": resolved["name"],
        "directory": directory,
        "email": resolved["email"],
        "url": target,
        "argv": argv,
        "title": title,
        "proof": proof,
        "ms": int((time.perf_counter() - t0) * 1000),
    }


def op_type(text: str = "", submit: bool = False) -> Dict[str, Any]:
    w = _focus_any_chrome()
    if not w:
        return {"status": "error", "error": "no Chrome window to type into"}
    typed = keys.type_text(text, submit=submit)
    typed["window"] = w["title"]
    return typed


def op_keys(combo: str = "") -> Dict[str, Any]:
    w = _focus_any_chrome()
    if not w:
        return {"status": "error", "error": "no Chrome window for keys"}
    sent = keys.send_keys(combo)
    sent["window"] = w["title"]
    return sent


def op_urlbar() -> Dict[str, Any]:
    w = _focus_any_chrome()
    if not w:
        return {"status": "error", "error": "no Chrome window for urlbar"}
    sent = keys.send_keys("ctrl+l")
    sent["op"] = "urlbar"
    sent["window"] = w["title"]
    return sent
