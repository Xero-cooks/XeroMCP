"""Short-lived probabilistic spatial memory (priors, never truth).

Positions are stored WINDOW-RELATIVE and normalised (0..1) so they survive
window moves/resizes. Memory only ever narrows *where to look first*
(e.g. which region to OCR); the resolver still has to see the target.

Privacy rules (enforced in `is_persistable`):
  * only labels of UI chrome controls (button/menu/tab/link/checkbox/...) that
    were resolved AND verified are learned;
  * no digits runs, emails, URLs, paths, long strings or many-word text;
  * never screenshots, OCR dumps, typed text, tokens or window contents.
Persistence is opt-in (XEROSPATIAL_MEMORY_FILE or persist_path) and bounded.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .geometry import Rect

_PERSIST_CTYPES = ("button", "menuitem", "tabitem", "hyperlink", "checkbox",
                   "radiobutton", "combobox", "splitbutton", "menubar", "toolbar")
_BAD = re.compile(r"(@|https?:|www\.|[\\/]|\d{4,}|[A-Fa-f0-9]{12,}|password|token|secret|key=)", re.I)

# Built-in regional priors: app -> [(keywords, normalised window region)]
BUILTIN_PRIORS: Dict[str, List[Tuple[Tuple[str, ...], Tuple[float, float, float, float]]]] = {
    "chrome.exe": [(("address", "search google", "type a url", "omnibox"), (0.0, 0.03, 1.0, 0.14)),
                   (("tab", "new tab"), (0.0, 0.0, 1.0, 0.07)),
                   (("extensions", "bookmark", "profile"), (0.6, 0.03, 1.0, 0.14))],
    "msedge.exe": [(("address", "search", "tab"), (0.0, 0.0, 1.0, 0.14))],
    "code.exe": [(("explorer", "open editors", "outline"), (0.0, 0.0, 0.3, 1.0)),
                 (("terminal", "problems", "output", "debug console"), (0.0, 0.55, 1.0, 1.0))],
    "blender.exe": [(("timeline", "playback", "keying"), (0.0, 0.75, 1.0, 1.0)),
                    (("outliner",), (0.7, 0.0, 1.0, 0.45)),
                    (("properties",), (0.7, 0.4, 1.0, 1.0))],
}


def is_persistable(label: str, ctype: str = "") -> bool:
    lab = (label or "").strip()
    if not (1 <= len(lab) <= 32) or len(lab.split()) > 4:
        return False
    if _BAD.search(lab):
        return False
    ct = (ctype or "").lower()
    return any(c in ct for c in _PERSIST_CTYPES)


class SpatialMemory:
    def __init__(self, persist_path: Optional[str] = None, max_apps: int = 50,
                 max_labels: int = 200, half_life_s: float = 7 * 24 * 3600) -> None:
        env = os.environ.get("XEROSPATIAL_MEMORY_FILE", "")
        self.path = Path(persist_path or env) if (persist_path or env) else None
        self.max_apps, self.max_labels, self.half_life = max_apps, max_labels, half_life_s
        self._mu = threading.Lock()
        # app -> label -> {"nx","ny","w","n","t"}
        self._data: Dict[str, Dict[str, Dict[str, float]]] = {}
        self._load()

    # ---- persistence -------------------------------------------------------
    def _load(self) -> None:
        if not self.path or not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for app, labels in (raw.get("apps") or {}).items():
                clean = {k: v for k, v in labels.items() if is_persistable(k, v.get("ctype", "button"))}
                if clean:
                    self._data[app] = clean
        except Exception:
            self._data = {}

    def _save(self) -> None:
        if not self.path:
            return
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": 1, "apps": self._data}), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception:
            pass

    # ---- learning ----------------------------------------------------------
    def learn(self, app: str, label: str, rect: Rect, win_rect: Optional[Dict[str, int]],
              ctype: str = "", verified: bool = False) -> bool:
        """Record a verified target position. Returns True if stored."""
        if not verified or not win_rect or not app or not is_persistable(label, ctype):
            return False
        ww, wh = max(1, win_rect["w"]), max(1, win_rect["h"])
        cx, cy = rect.center
        nx, ny = (cx - win_rect["x"]) / ww, (cy - win_rect["y"]) / wh
        if not (0 <= nx <= 1 and 0 <= ny <= 1):
            return False
        key = " ".join(label.lower().split())
        with self._mu:
            labels = self._data.setdefault(app.lower(), {})
            e = labels.get(key)
            if e:
                n = e["n"] + 1
                a = 1.0 / min(n, 8)   # running mean, capped memory
                e.update(nx=e["nx"] + (nx - e["nx"]) * a, ny=e["ny"] + (ny - e["ny"]) * a,
                         w=rect.w / ww, h=rect.h / wh, n=n, t=time.time(), ctype=ctype)
            else:
                labels[key] = {"nx": nx, "ny": ny, "w": rect.w / ww, "h": rect.h / wh,
                               "n": 1, "t": time.time(), "ctype": ctype}
            if len(labels) > self.max_labels:
                for k in sorted(labels, key=lambda k: labels[k]["t"])[: len(labels) - self.max_labels]:
                    labels.pop(k, None)
            if len(self._data) > self.max_apps:
                oldest = min(self._data, key=lambda a: max(v["t"] for v in self._data[a].values()))
                self._data.pop(oldest, None)
            self._save()
        return True

    def forget(self, app: str, label: str) -> None:
        with self._mu:
            self._data.get((app or "").lower(), {}).pop(" ".join((label or "").lower().split()), None)
            self._save()

    # ---- priors ------------------------------------------------------------
    def priors(self, app: str, label: str, win_rect: Optional[Dict[str, int]]) -> List[Dict[str, object]]:
        """Candidate search regions (physical) ordered by weight. Never truth."""
        if not win_rect:
            return []
        out: List[Dict[str, object]] = []
        wx, wy, ww, wh = win_rect["x"], win_rect["y"], win_rect["w"], win_rect["h"]
        key = " ".join((label or "").lower().split())
        with self._mu:
            e = self._data.get((app or "").lower(), {}).get(key)
        if e:
            age = max(0.0, time.time() - e["t"])
            weight = min(1.0, 0.4 + 0.1 * e["n"]) * 0.5 ** (age / self.half_life)
            bw = max(e["w"] * ww * 3, 160)
            bh = max(e["h"] * wh * 3, 80)
            cx, cy = wx + e["nx"] * ww, wy + e["ny"] * wh
            out.append({"source": "learned", "weight": round(weight, 3),
                        "rect": Rect(int(cx - bw / 2), int(cy - bh / 2), int(bw), int(bh))})
        for kws, (x0, y0, x1, y1) in BUILTIN_PRIORS.get((app or "").lower(), []):
            if any(k in key for k in kws):
                out.append({"source": "builtin", "weight": 0.3,
                            "rect": Rect(int(wx + x0 * ww), int(wy + y0 * wh),
                                         max(1, int((x1 - x0) * ww)), max(1, int((y1 - y0) * wh)))})
        out.sort(key=lambda d: -float(d["weight"]))  # type: ignore[arg-type]
        return out

    def stats(self) -> Dict[str, int]:
        with self._mu:
            return {"apps": len(self._data), "labels": sum(len(v) for v in self._data.values()),
                    "persistent": int(bool(self.path))}
