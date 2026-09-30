"""Chrome profile registry: discovered from disk (Local State) and OBSERVED from
live windows. Nothing here is fabricated: `purpose` comes only from an optional
user-written xero_profiles.json; observed profile is verified from evidence."""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

_TITLE = re.compile(r"^(?P<page>.*?)\s+-\s+Google Chrome(?:\s+-\s+(?P<profile>.+))?$", re.S)


def default_user_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Google" / "Chrome" / "User Data"


def parse_window_title(title: str) -> Dict[str, Any]:
    """'Dashboard - Render - Google Chrome - Kartik Raghav' -> page + profile suffix.
    Chrome only shows the profile suffix when more than one profile exists."""
    m = _TITLE.match(title or "")
    if not m:
        return {"page": title or "", "profile_label": None, "is_chrome_title": False}
    return {"page": m.group("page"), "profile_label": (m.group("profile") or "").strip() or None,
            "is_chrome_title": True}


@dataclass
class ProfileInfo:
    directory: str
    name: str
    email: str = ""
    gaia_name: str = ""
    path: str = ""
    purpose: str = ""            # ONLY from user config, never inferred
    aliases: List[str] = field(default_factory=list)

    def labels(self) -> List[str]:
        return [x for x in {self.name, self.gaia_name, *self.aliases} if x]

    def public(self) -> Dict[str, Any]:
        return {"name": self.name, "directory": self.directory, "email": self.email,
                "path": self.path, "purpose": self.purpose or None}


class ChromeRegistry:
    def __init__(self, user_data_dir: Optional[Path] = None, config_path: Optional[Path] = None,
                 windows_provider: Optional[Callable[[], List[Dict[str, Any]]]] = None,
                 foreground_provider: Optional[Callable[[], int]] = None,
                 clock=time.time, seed: Optional[List[ProfileInfo]] = None) -> None:
        self._seed = seed or []
        self.user_data_dir = Path(user_data_dir) if user_data_dir else default_user_data_dir()
        self.config_path = Path(config_path) if config_path else Path(__file__).resolve().parents[2] / "xero_profiles.json"
        self._windows = windows_provider or (lambda: [])
        self._fg = foreground_provider or (lambda: 0)
        self._clock = clock
        self._profiles: Dict[str, ProfileInfo] = {}
        self._loaded_at = 0.0
        self.last_observed: Dict[str, Dict[str, Any]] = {}     # directory -> observation

    # ---- discovery ------------------------------------------------------------
    def refresh(self) -> None:
        profiles: Dict[str, ProfileInfo] = {}
        try:
            state = json.loads((self.user_data_dir / "Local State").read_text(encoding="utf-8"))
            cache = (state.get("profile") or {}).get("info_cache") or {}
            for directory, info in cache.items():
                profiles[directory] = ProfileInfo(
                    directory=directory, name=str(info.get("name") or directory),
                    email=str(info.get("user_name") or ""), gaia_name=str(info.get("gaia_name") or ""),
                    path=str(self.user_data_dir / directory))
        except Exception:
            pass
        try:
            cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
            for key, meta in (cfg.get("profiles") or cfg).items():
                if not isinstance(meta, dict):
                    continue
                p = profiles.get(key) or next((q for q in profiles.values() if q.name.lower() == key.lower()), None)
                if p:
                    p.purpose = str(meta.get("purpose", "") or "")
                    p.aliases = [str(a) for a in meta.get("aliases", [])]
        except Exception:
            pass
        for sd in self._seed:                       # fallback identity ONLY when disk discovery lacks it
            if sd.directory not in profiles and not any(p.name.lower() == sd.name.lower() for p in profiles.values()):
                profiles[sd.directory] = sd
        self._profiles = profiles
        self._loaded_at = self._clock()

    def profiles(self, max_age: float = 30.0) -> List[ProfileInfo]:
        if not self._profiles or self._clock() - self._loaded_at > max_age:
            self.refresh()
        return list(self._profiles.values())

    def resolve(self, who: str) -> Dict[str, Any]:
        """'XeroCore' / 'Profile 11' / an email -> actual directory. Exact matches
        win; a unique case-insensitive prefix is accepted; ambiguity is reported."""
        key = (who or "").strip().lower()
        ps = self.profiles()
        if not key:
            return {"ok": False, "reason": "empty_profile", "candidates": [p.name for p in ps]}
        exact = [p for p in ps if key in (p.directory.lower(), p.email.lower(), *[l.lower() for l in p.labels()])]
        if len(exact) == 1:
            return {"ok": True, "profile": exact[0], "match": "exact"}
        if len(exact) > 1:
            return {"ok": False, "reason": "ambiguous", "candidates": [p.name for p in exact]}
        pre = [p for p in ps if any(l.lower().startswith(key) for l in p.labels())]
        if len(pre) == 1:
            return {"ok": True, "profile": pre[0], "match": "prefix"}
        return {"ok": False, "reason": "ambiguous" if pre else "unknown_profile",
                "candidates": [p.name for p in (pre or ps)]}

    # ---- observation ----------------------------------------------------------
    def identify_window(self, w: Dict[str, Any]) -> Dict[str, Any]:
        """Observed profile for one live Chrome window, with evidence + verified flag."""
        t = parse_window_title(w.get("title", ""))
        out: Dict[str, Any] = {"hwnd": w.get("hwnd"), "page_title": t["page"], "profile": None,
                               "directory": None, "verified": False, "evidence": None}
        label = t["profile_label"]
        if label:
            hits = [p for p in self.profiles() if label.lower() in [l.lower() for l in p.labels()]]
            if len(hits) == 1:
                p = hits[0]
                out.update(profile=p.name, directory=p.directory, verified=True, evidence="window_title_suffix")
            elif len(hits) > 1:
                out.update(profile=label, evidence="window_title_suffix_ambiguous")
            else:
                out.update(profile=label, evidence="window_title_suffix_unregistered")
        elif len(self.profiles()) <= 1 and self.profiles():
            p = self.profiles()[0]
            out.update(profile=p.name, directory=p.directory, verified=True, evidence="single_profile_install")
        else:
            out["evidence"] = "no_profile_marker_in_title"
        return out

    def observe(self) -> Dict[str, Any]:
        fg = int(self._fg() or 0)
        wins = []
        now = self._clock()
        for w in self._windows():
            ident = self.identify_window(w)
            ident["foreground"] = int(w.get("hwnd", -1)) == fg
            wins.append(ident)
            if ident["directory"]:
                self.last_observed[ident["directory"]] = {"hwnd": ident["hwnd"], "page_title": ident["page_title"],
                                                          "t": now, "foreground": ident["foreground"]}
        active = next((w for w in wins if w["foreground"]), None)
        return {"windows": wins, "foreground": active}

    def registry_view(self) -> List[Dict[str, Any]]:
        obs = self.observe()
        by_dir: Dict[str, Dict[str, Any]] = {}
        for w in obs["windows"]:
            if w["directory"]:
                cur = by_dir.get(w["directory"])
                if cur is None or w["foreground"]:
                    by_dir[w["directory"]] = w
        rows = []
        for p in self.profiles():
            w = by_dir.get(p.directory)
            last = self.last_observed.get(p.directory)
            rows.append({**p.public(), "active": bool(w and w["foreground"]), "open": bool(w),
                         "window": w["hwnd"] if w else None, "tab": w["page_title"] if w else None,
                         "last_observed": last["t"] if last else None})
        return rows
