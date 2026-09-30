"""Platform-free chrome_session(go) logic. All OS effects are injected, so the
state machine is unit-testable; chrome_go wires the real Win32 providers.

Stages (never collapsed into 'success'):
  profile_verified profile_mismatch launch_requested launch_started window_found
  tab_found navigation_started navigation_verified destination_visible
already_open=True ONLY when the requested profile's own window is foreground
(proven) AND its active tab matches the destination.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from .. import until_proof
from .chrome_registry import ChromeRegistry


def host_of(url: str) -> str:
    if not url:
        return ""
    raw = url if "://" in url else "https://" + url
    try:
        return (urlparse(raw).hostname or "").lower()
    except Exception:
        return url.lower()


_ALIASES = {"notebook": ("notebooklm", "notebook"), "mail": ("gmail", "mail.google"),
            "docs": ("google docs", "docs.google"), "drive": ("google drive", "drive.google")}


def title_matches(title: str, url: str) -> bool:
    t = (title or "").lower()
    host = host_of(url)
    if not host:
        return False
    short = host.replace("www.", "")
    labels = short.split(".")
    first = labels[0]
    if first in _ALIASES and any(a in t for a in _ALIASES[first]):
        return True
    # registrable label (dashboard.render.com -> "render"): subdomains like
    # dashboard/app/www are generic and don't identify the site in a title
    sld = labels[-2] if len(labels) >= 2 else first
    return bool(sld) and len(sld) >= 3 and (sld in t or short in t)


def go(reg: ChromeRegistry, url: str, profile: str, until: str = "", *,
       focus: Callable[[int], bool], launch: Callable[[List[str]], bool], exe: str,
       sleep: Callable[[float], None] = time.sleep, polls: int = 25, poll_s: float = 0.2,
       url_reader: Optional[Callable[[int], Optional[str]]] = None,
       emit: Optional[Callable[..., Any]] = None) -> Dict[str, Any]:
    t0 = time.perf_counter()
    stages: List[str] = []
    ms = lambda: int((time.perf_counter() - t0) * 1000)  # noqa: E731

    res = reg.resolve(profile)
    if not res["ok"]:
        return {"status": "profile_unknown", "verified": False, "already_open": False, "stages": stages,
                "requested_profile": {"input": profile}, "error": f"cannot resolve profile {profile!r}: {res['reason']}",
                "candidates": res.get("candidates", []), "ms": ms()}
    prof = res["profile"]
    requested = {"input": profile, "name": prof.name, "directory": prof.directory}
    target = (url or "").strip()
    if not target:
        return {"status": "bad_request", "verified": False, "already_open": False, "stages": stages,
                "requested_profile": requested, "error": "url required", "ms": ms()}

    def dest_evidence(w: Dict[str, Any]) -> Optional[str]:
        if url_reader:
            try:
                u = url_reader(w["hwnd"])
            except Exception:
                u = None
            if u:
                return "url" if host_of(u) == host_of(target) or host_of(target) in host_of(u) else None
        return "window_title" if title_matches(w["page_title"], target) else None

    def in_profile(obs) -> List[Dict[str, Any]]:
        return [w for w in obs["windows"] if w["directory"] == prof.directory and w["verified"]]

    def finish(status: str, w: Optional[Dict[str, Any]], **extra) -> Dict[str, Any]:
        obs = reg.observe()
        fg = obs["foreground"]
        out = {"status": status, "verified": status == "ok", "already_open": False, "stages": stages,
               "requested_profile": requested,
               "observed_profile": ({"name": fg["profile"], "directory": fg["directory"], "verified": fg["verified"],
                                     "evidence": fg["evidence"]} if fg else None),
               "url": target, "title": (w or {}).get("page_title", ""), "ms": ms(), **extra}
        if emit:
            emit("PROFILE_CHANGED" if status == "profile_mismatch" else "URL_CHANGED", status=status,
                 profile=requested["name"])
        return out

    obs = reg.observe()
    existing = in_profile(obs)
    hit = next((w for w in existing if dest_evidence(w)), None)
    if hit:
        stages += ["profile_verified", "window_found", "tab_found"]
        if focus(hit["hwnd"]):
            obs2 = reg.observe()
            fg = obs2["foreground"]
            if fg and fg["hwnd"] == hit["hwnd"] and fg["directory"] == prof.directory and fg["verified"]:
                stages.append("destination_visible")
                proof = until_proof.check_until(until, url=target, title=hit["page_title"])
                ok = proof.get("until_ok") is not False
                r = finish("ok" if ok else "launched_unverified", hit, proof=proof,
                           destination_evidence=dest_evidence(hit), url_verified=bool(url_reader))
                r["already_open"] = ok
                return r
        return finish("focus_failed", hit, error="matching window found but did not take the foreground")

    # not open in THIS profile (a same-site tab in another profile does not count)
    other = [w for w in obs["windows"] if w["directory"] != prof.directory and dest_evidence(w)]
    argv = [exe, f"--profile-directory={prof.directory}", target]
    stages.append("launch_requested")
    if not launch(argv):
        return finish("launch_failed", None, error=f"could not start {exe}", argv=argv)
    stages.append("launch_started")
    had_window = bool(existing)
    if had_window:
        stages.append("navigation_started")
    matched = None
    seen_window = None
    for _ in range(polls):
        sleep(poll_s)
        o = reg.observe()
        mine = in_profile(o)
        if mine and not seen_window:
            seen_window = mine[0]
        matched = next((w for w in mine if dest_evidence(w)), None)
        if matched:
            break
    if matched:
        for s in ("profile_verified", "window_found", "tab_found", "navigation_verified"):
            if s not in stages:
                stages.append(s)
        focused = focus(matched["hwnd"])
        fg = reg.observe()["foreground"]
        if focused and fg and fg["hwnd"] == matched["hwnd"] and fg["directory"] == prof.directory:
            stages.append("destination_visible")
            proof = until_proof.check_until(until, url=target, title=matched["page_title"])
            return finish("ok" if proof.get("until_ok") is not False else "launched_unverified", matched,
                          launched=True, argv=[exe, f"--profile-directory={prof.directory}", "<url>"],
                          proof=proof, destination_evidence=dest_evidence(matched), url_verified=bool(url_reader))
        return finish("focus_failed", matched, launched=True, error="destination loaded but window not foreground")
    fg = reg.observe()["foreground"]
    if seen_window:
        stages += [s for s in ("profile_verified", "window_found") if s not in stages]
        return finish("navigation_unverified", seen_window, launched=True,
                      error="profile window found but destination not confirmed (still loading, or title differs)",
                      other_profile_has_destination=bool(other))
    if fg and fg["directory"] and fg["directory"] != prof.directory:
        stages.append("profile_mismatch")
        return finish("profile_mismatch", None, launched=True,
                      error=f"requested profile {prof.name!r} but foreground Chrome is {fg['profile']!r}",
                      other_profile_has_destination=bool(other))
    return finish("launch_unverified", None, launched=True, error="no window of the requested profile appeared",
                  other_profile_has_destination=bool(other))
