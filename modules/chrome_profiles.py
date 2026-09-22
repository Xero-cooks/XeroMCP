"""Real Chrome identity. Never debug profile, never picker, never kill Chrome."""
from __future__ import annotations

from typing import Any, Dict, Optional

CHROME_EXE = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
DEBUG_USER_DATA = ".chrome_debug_profile"

KARTIK = {
    "name": "Kartik",
    "directory": "Profile 11",
    "email": "kartikraghav1st@gmail.com",
    "gaia": "Kartik Raghav",
}

PROFILES: Dict[str, Dict[str, str]] = {
    "kartik": KARTIK,
    "kartik raghav": KARTIK,
    "profile 11": KARTIK,
    "profile11": KARTIK,
    "me": KARTIK,
    "user": KARTIK,
}

FORBIDDEN = ("default", "ghost", "saurabh", "xero", "vinayak")


def resolve_profile(who: str = "Kartik") -> Dict[str, Any]:
    key = (who or "Kartik").strip().lower()
    if key in PROFILES:
        return {"ok": True, **PROFILES[key]}
    for needle in FORBIDDEN:
        if needle in key:
            return {
                "ok": False,
                "error": (
                    f"refused identity '{who}'. Kartik = Profile 11 only. "
                    "Never Default / Ghost / SAURABH / XERO / Vinayak."
                ),
            }
    if not key:
        return {"ok": True, **KARTIK}
    return {
        "ok": False,
        "error": f"unknown profile '{who}'. Known: Kartik = Profile 11 = kartikraghav1st@gmail.com.",
    }


def refuse_debug_identity(*parts: str) -> Optional[Dict[str, Any]]:
    blob = " ".join(p or "" for p in parts).lower()
    if "profile-picker" in blob:
        return {"ok": False, "error": "never --profile-picker; use --profile-directory"}
    if DEBUG_USER_DATA in blob or "user-data-dir" in blob:
        return {
            "ok": False,
            "error": "debug user-data-dir is forbidden for identity work",
        }
    return None
