"""Type into the focused real Chrome window. Never agent-side pyautogui."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Any, Dict, List

user32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

VK = {
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "shift": 0x10,
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
    "lwin": 0x5B,
    "win": 0x5B,
    "backspace": 0x08,
    "delete": 0x2E,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "l": 0x4C,
    "t": 0x54,
    "w": 0x57,
    "a": 0x41,
    "c": 0x43,
    "v": 0x56,
    "x": 0x58,
    "f": 0x46,
    "r": 0x52,
}

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class INPUT(ctypes.Structure):
    class _I(ctypes.Union):
        _fields_ = (("ki", KEYBDINPUT),)
    _anonymous_ = ("i",)
    _fields_ = (("type", wintypes.DWORD), ("i", _I))


def _send(inp: INPUT) -> None:
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def _vk(vk: int, up: bool = False) -> None:
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, 0)
    _send(inp)


def _unicode_char(ch: str) -> None:
    code = ord(ch)
    down = INPUT(type=INPUT_KEYBOARD)
    down.ki = KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, 0)
    _send(down)
    up = INPUT(type=INPUT_KEYBOARD)
    up.ki = KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0)
    _send(up)


def type_text(text: str, submit: bool = False) -> Dict[str, Any]:
    for ch in text or "":
        if ch == "\n":
            _vk(VK["enter"])
            _vk(VK["enter"], up=True)
        else:
            _unicode_char(ch)
    if submit:
        _vk(VK["enter"])
        _vk(VK["enter"], up=True)
    return {"status": "ok", "typed": text, "submitted": bool(submit)}


def send_keys(keys: str) -> Dict[str, Any]:
    raw = (keys or "").strip()
    if not raw:
        return {"status": "error", "error": "keys is empty"}
    parts = [p.strip().lower() for p in raw.replace("-", "+").split("+") if p.strip()]
    vks: List[int] = []
    for p in parts:
        if p not in VK:
            if len(p) == 1 and p.isalpha():
                vks.append(ord(p.upper()))
            else:
                return {"status": "error", "error": f"unknown key '{p}'"}
        else:
            vks.append(VK[p])
    for vk in vks:
        _vk(vk)
    for vk in reversed(vks):
        _vk(vk, up=True)
    return {"status": "ok", "keys": raw, "vks": vks}
