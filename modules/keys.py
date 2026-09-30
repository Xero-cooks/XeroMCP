"""Keyboard injection via SendInput (the hub's only keyboard path).

Hardened (v2.4):
  * INPUT struct now includes the full MOUSEINPUT/KEYBDINPUT/HARDWAREINPUT
    union. The old struct only carried KEYBDINPUT, so on 64-bit Python
    sizeof(INPUT) was 32 instead of 40 and Windows REJECTED every event
    (SendInput returned 0) while we reported status "ok".
  * Every SendInput return value is checked; failures are reported
    (UIPI-blocked input to elevated windows, bad struct, secure desktop).
  * Events of one gesture go out in ONE SendInput batch (atomic, cannot
    interleave with other input) under the global INPUT_LOCK.
  * UTF-16 surrogate pairs (emoji etc.), digits, F-keys, navigation keys,
    punctuation; extended-key flag for arrows/nav keys.
  * Modifier keys are always released, even after a partial failure.
  * Imports cleanly on non-Windows (returns status "error" when called).
"""
from __future__ import annotations

import ctypes
import sys
from typing import Any, Dict, List, Tuple

from modules.input_lock import INPUT_LOCK

IS_WINDOWS = sys.platform == "win32"

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

VK: Dict[str, int] = {
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "menu": 0x12, "shift": 0x10,
    "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C, "cmd": 0x5B, "super": 0x5B,
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "spacebar": 0x20, "backspace": 0x08, "bksp": 0x08,
    "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "ins": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "capslock": 0x14, "numlock": 0x90, "scrolllock": 0x91, "printscreen": 0x2C, "prtsc": 0x2C,
    "pause": 0x13, "apps": 0x5D, "contextmenu": 0x5D,
    "plus": 0xBB, "=": 0xBB, "minus": 0xBD, "-": 0xBD, ",": 0xBC, "comma": 0xBC,
    ".": 0xBE, "period": 0xBE, "/": 0xBF, "slash": 0xBF, ";": 0xBA, "`": 0xC0,
    "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
    "volumeup": 0xAF, "volumedown": 0xAE, "volumemute": 0xAD,
}
for _i in range(1, 25):
    VK[f"f{_i}"] = 0x6F + _i
for _c in "abcdefghijklmnopqrstuvwxyz":
    VK[_c] = ord(_c.upper())
for _d in "0123456789":
    VK[_d] = ord(_d)
_EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C, 0x5D, 0x2C}
_MODIFIERS = {0x10, 0x11, 0x12, 0x5B, 0x5C}

if IS_WINDOWS:
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    ULONG_PTR = ctypes.c_size_t

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = (("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR))

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = (("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR))

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = (("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD))

    class _INPUTUNION(ctypes.Union):
        _fields_ = (("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT))

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = (("type", wintypes.DWORD), ("u", _INPUTUNION))

    user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    user32.SendInput.restype = wintypes.UINT
    user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
    user32.MapVirtualKeyW.restype = wintypes.UINT


def _key_event(vk: int, up: bool) -> "INPUT":
    flags = KEYEVENTF_KEYUP if up else 0
    if vk in _EXTENDED:
        flags |= KEYEVENTF_EXTENDEDKEY
    scan = user32.MapVirtualKeyW(vk, 0) & 0xFF
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
    return inp


def _unicode_events(ch: str) -> List["INPUT"]:
    units = ch.encode("utf-16-le")
    out = []
    for i in range(0, len(units), 2):
        code = int.from_bytes(units[i:i + 2], "little")
        for up in (False, True):
            inp = INPUT(type=INPUT_KEYBOARD)
            inp.ki = KEYBDINPUT(0, code, KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0), 0, 0)
            out.append(inp)
    return out


def _send_batch(events: List["INPUT"]) -> Tuple[int, int]:
    """Send events atomically. Returns (sent, win32_error)."""
    if not events:
        return 0, 0
    arr = (INPUT * len(events))(*events)
    sent = user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))
    return int(sent), (ctypes.get_last_error() if sent != len(events) else 0)


def _release_all(vks: List[int]) -> None:
    try:
        _send_batch([_key_event(vk, True) for vk in reversed(vks)])
    except Exception:
        pass


def _blocked_msg(err: int) -> str:
    return (f"SendInput rejected the events (win32 error {err}). Usual causes: the target "
            "window runs elevated (UIPI) while the hub does not, the workstation is locked "
            "(secure desktop), or another process holds exclusive input.")


def type_text(text: str, submit: bool = False) -> Dict[str, Any]:
    if not IS_WINDOWS:
        return {"status": "error", "error": "keyboard injection is Windows-only"}
    events: List[INPUT] = []
    for ch in (text or ""):
        if ch == "\n":
            events += [_key_event(0x0D, False), _key_event(0x0D, True)]
        elif ch == "\r":
            continue
        elif ch == "\t":
            events += [_key_event(0x09, False), _key_event(0x09, True)]
        else:
            events += _unicode_events(ch)
    if submit:
        events += [_key_event(0x0D, False), _key_event(0x0D, True)]
    total, sent = len(events), 0
    with INPUT_LOCK:
        # chunk to keep each SendInput call bounded (very long pastes)
        for i in range(0, total, 400):
            n, err = _send_batch(events[i:i + 400])
            sent += n
            if n != len(events[i:i + 400]):
                return {"status": "error", "error": _blocked_msg(err), "sent": sent, "expected": total,
                        "typed_chars": len(text or ""), "submitted": False}
    return {"status": "ok", "typed": text, "typed_chars": len(text or ""), "submitted": bool(submit),
            "sent": sent}


def parse_chord(keys: str) -> Tuple[List[int], str]:
    """'ctrl+shift+t' / 'CTRL+L' / 'ctrl++' / 'ctrl-l' -> ([vk...], error)."""
    raw = (keys or "").strip()
    if not raw:
        return [], "keys is empty"
    low = raw.lower()
    if "+" in low:
        parts, buf = [], ""
        for i, ch in enumerate(low):
            if ch == "+" and buf:
                parts.append(buf.strip())
                buf = ""
            elif ch == "+" and not buf:
                buf = "+"          # literal '+' key (e.g. 'ctrl++')
            else:
                buf += ch
        if buf:
            parts.append(buf.strip())
        parts = ["plus" if p == "+" else p for p in parts if p]
    elif "-" in low and len(low) > 1 and all(p in VK for p in low.split("-") if p):
        parts = [p for p in low.split("-") if p]      # 'ctrl-l' legacy form
    else:
        parts = [low]
    vks = []
    for p in parts:
        if p not in VK:
            return [], f"unknown key '{p}'"
        vks.append(VK[p])
    return vks, ""


def send_keys(keys: str) -> Dict[str, Any]:
    vks, err = parse_chord(keys)
    if err:
        return {"status": "error", "error": err}
    if not IS_WINDOWS:
        return {"status": "error", "error": "keyboard injection is Windows-only"}
    events = [_key_event(vk, False) for vk in vks] + [_key_event(vk, True) for vk in reversed(vks)]
    with INPUT_LOCK:
        n, e = _send_batch(events)
        if n != len(events):
            _release_all([v for v in vks if v in _MODIFIERS] or vks)
            return {"status": "error", "error": _blocked_msg(e), "keys": keys, "sent": n}
    return {"status": "ok", "keys": keys, "vks": vks, "sent": n}
