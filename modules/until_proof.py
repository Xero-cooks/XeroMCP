"""Tiny until-condition parser. Proof or it did not happen."""
from __future__ import annotations

from typing import Any, Dict


def parse_until(until: str) -> Dict[str, Any]:
    text = (until or "").strip()
    if not text:
        return {"kind": "none"}
    low = text.lower()
    if low.startswith("url contains "):
        return {"kind": "url_contains", "needle": text[13:].strip()}
    if low.endswith(" gone"):
        return {"kind": "gone", "needle": text[: -len(" gone")].strip()}
    if low.startswith("title contains "):
        return {"kind": "title_contains", "needle": text[15:].strip()}
    return {"kind": "visible", "needle": text}


def check_until(until: str, *, url: str = "", title: str = "", visible_text: str = "") -> Dict[str, Any]:
    spec = parse_until(until)
    kind = spec.get("kind")
    if kind == "none":
        return {"until_ok": True, "how": "no_until"}
    needle = (spec.get("needle") or "").lower()
    hay_url = (url or "").lower()
    hay_title = (title or "").lower()
    hay_vis = (visible_text or "").lower()
    if kind == "url_contains":
        ok = needle in hay_url or needle in hay_title
        return {"until_ok": ok, "how": "url_contains", "needle": needle}
    if kind == "title_contains":
        ok = needle in hay_title
        return {"until_ok": ok, "how": "title_contains", "needle": needle}
    if kind == "gone":
        ok = needle not in hay_vis and needle not in hay_title
        return {"until_ok": ok, "how": "gone", "needle": needle}
    ok = needle in hay_vis or needle in hay_title
    return {"until_ok": ok, "how": "visible", "needle": needle}
