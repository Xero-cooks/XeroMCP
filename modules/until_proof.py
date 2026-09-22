# ==============================================================================
# until_proof.py - Shared until-condition parsing + real-Chrome title proof.
#
# Real Chrome (Kartik / default User Data) has NO CDP. "url contains
# notebook.google" must succeed from the window title (NotebookLM), not from
# http://127.0.0.1:9222/json which is the debug twin.
# ==============================================================================
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


def parse_until(until: str) -> Tuple[str, str]:
    u = (until or "").strip()
    if not u:
        return "", ""
    low = u.lower()
    if low.startswith("url contains "):
        return "url", u[13:].strip()
    if low.startswith("title contains "):
        return "title", u[15:].strip()
    if low.endswith(" gone"):
        return "gone", u[:-5].strip()
    if low.startswith("gone:"):
        return "gone", u[5:].strip()
    return "contains", u


def _titles(win_info: Optional[Dict[str, Any]]) -> List[str]:
    titles: List[str] = []
    t = (win_info or {}).get("title") or ""
    if t:
        titles.append(t)
    try:
        from modules.chrome_profiles import chrome_window_titles
        titles.extend(chrome_window_titles())
    except Exception:
        pass
    seen = set()
    out = []
    for x in titles:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def check_url_or_title(kind: str, needle: str, win_info: Dict[str, Any],
                       cdp_fallback=None) -> Dict[str, Any]:
    from modules.chrome_profiles import titles_match_until
    titles = _titles(win_info)
    if titles_match_until(titles, needle):
        return {"until_ok": True, "how": "window_title", "titles": titles[:6]}
    if kind == "url" and cdp_fallback is not None:
        try:
            cdp = cdp_fallback()
            if cdp.get("until_ok"):
                return cdp
        except Exception:
            pass
    return {"until_ok": False, "how": "window_title", "titles": titles[:6],
            "needle": needle}


def install() -> None:
    """Patch mouse_runtime so until=url/title works on real Chrome."""
    from modules import mouse_runtime
    mouse_runtime._parse_until = parse_until
    orig = getattr(mouse_runtime, "_check_until_orig", mouse_runtime._check_until)
    mouse_runtime._check_until_orig = orig

    def wrapped(kind: str, needle: str, win_info: Dict[str, Any],
                pre_ocr_lines=None, deadline=None):
        if kind in ("url", "title"):
            def _cdp():
                return orig(kind, needle, win_info, pre_ocr_lines, deadline)
            return check_url_or_title(kind, needle, win_info,
                                      cdp_fallback=_cdp if kind == "url" else None)
        return orig(kind, needle, win_info, pre_ocr_lines, deadline)

    mouse_runtime._check_until = wrapped
