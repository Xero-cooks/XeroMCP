"""Short-lived target tracking: identity + geometry + confidence + velocity.
A button that shifts a little is UPDATED, not rediscovered; a vanished target
is predicted only briefly and only when safe, else invalidated."""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .changes import cells_for_rect

Rect4 = Tuple[int, int, int, int]      # x, y, w, h physical px


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _center(r: Rect4) -> Tuple[float, float]:
    return r[0] + r[2] / 2.0, r[1] + r[3] / 2.0


@dataclass
class Track:
    target_id: str
    label: str
    bbox: Rect4
    confidence: float
    source: str
    last_seen_frame: int
    last_seen_t: float
    velocity: Tuple[float, float] = (0.0, 0.0)      # px / s
    state: str = "visible"                          # visible | predicted | lost
    reacquired: int = 0
    surface_id: str = ""

    def to_dict(self, bounds: Optional[Rect4] = None) -> Dict[str, Any]:
        d = {"target_id": self.target_id, "label": self.label, "bbox": list(self.bbox),
             "confidence": round(self.confidence, 2), "source": self.source,
             "last_seen_frame": self.last_seen_frame, "velocity": [round(v, 1) for v in self.velocity],
             "state": self.state}
        if bounds:
            d["cells"] = sorted(cells_for_rect(self.bbox, bounds))    # multi-cell targets are first-class
        return d


class TargetTracker:
    def __init__(self, max_tracks: int = 24, max_shift_frac: float = 0.25, predict_ttl: float = 0.6,
                 lost_ttl: float = 3.0, on_event=None, clock=time.monotonic) -> None:
        self._tracks: Dict[str, Track] = {}
        self._lock = threading.Lock()
        self.max_tracks = max_tracks
        self.max_shift_frac = max_shift_frac
        self.predict_ttl = predict_ttl
        self.lost_ttl = lost_ttl
        self._emit = on_event or (lambda *a, **k: None)
        self._clock = clock
        self.reacquisitions = 0

    def track(self, label: str, bbox: Rect4, frame: int, confidence: float = 1.0, source: str = "ocr",
              target_id: str = "", surface_id: str = "") -> Track:
        tid = target_id or re.sub(r"[^a-z0-9]+", "_", norm(label)).strip("_") or "target"
        with self._lock:
            if tid not in self._tracks and len(self._tracks) >= self.max_tracks:
                oldest = min(self._tracks.values(), key=lambda t: t.last_seen_t)
                self._tracks.pop(oldest.target_id)
            tr = Track(tid, label, tuple(int(v) for v in bbox), confidence, source, frame, self._clock(),
                       surface_id=surface_id)
            self._tracks[tid] = tr
        self._emit("TARGET_APPEARED", target_id=tid, label=label, bbox=list(tr.bbox))
        return tr

    def get(self, target_id: str) -> Optional[Track]:
        with self._lock:
            return self._tracks.get(target_id)

    def find_label(self, label: str) -> Optional[Track]:
        n = norm(label)
        with self._lock:
            return next((t for t in self._tracks.values() if norm(t.label) == n and t.state != "lost"), None)

    def usable(self, target_id: str) -> Optional[Track]:
        """A track safe to aim at: visible, or briefly predicted."""
        t = self.get(target_id)
        return t if t and t.state in ("visible", "predicted") else None

    def all(self) -> List[Track]:
        with self._lock:
            return list(self._tracks.values())

    def invalidate(self, target_id: Optional[str] = None, reason: str = "") -> int:
        with self._lock:
            ids = [target_id] if target_id else list(self._tracks)
            n = 0
            for i in ids:
                t = self._tracks.pop(i, None)
                if t:
                    n += 1
        if n:
            self._emit("STATE_INVALIDATED", scope="targets", n=n, reason=reason)
        return n

    def update(self, elements: Iterable[Dict[str, Any]], frame: int, surface: Rect4, surface_id: str = "") -> Dict[str, List[str]]:
        """elements: dicts with text + bbox [x,y,w,h] (physical). Match each track
        to the same-label element nearest to its predicted position."""
        now = self._clock()
        max_shift = self.max_shift_frac * max(surface[2], surface[3])
        els = [(norm(e.get("text", "")), tuple(e["bbox"]), float(e.get("confidence", 1.0)), e.get("source", "ocr"))
               for e in elements if e.get("text")]
        moved: List[str] = []
        gone: List[str] = []
        with self._lock:
            tracks = list(self._tracks.values())
        for tr in tracks:
            if tr.surface_id and surface_id and tr.surface_id != surface_id:
                self._mark_missing(tr, now, gone, "surface changed")
                continue
            px, py = self._predict(tr, now)
            cands = [(abs(_center(b)[0] - px) + abs(_center(b)[1] - py), b, c, s)
                     for (t, b, c, s) in els if t == norm(tr.label)]
            cands.sort(key=lambda x: x[0])
            if not cands or cands[0][0] > max_shift:
                self._mark_missing(tr, now, gone, "not found")
                continue
            dist, b, c, s = cands[0]
            dt = max(1e-3, now - tr.last_seen_t)
            ox, oy = _center(tr.bbox)
            nx, ny = _center(b)
            reacq = tr.state != "visible"
            if abs(nx - ox) + abs(ny - oy) > 3:
                moved.append(tr.target_id)
                tr.velocity = ((nx - ox) / dt, (ny - oy) / dt) if dt < 2.0 else (0.0, 0.0)
                self._emit("TARGET_MOVED", target_id=tr.target_id, bbox=list(b))
            else:
                tr.velocity = (0.0, 0.0)
            if reacq:
                tr.reacquired += 1
                self.reacquisitions += 1
                self._emit("TARGET_APPEARED", target_id=tr.target_id, label=tr.label, bbox=list(b))
            tr.bbox, tr.confidence, tr.source = tuple(int(v) for v in b), c, s
            tr.last_seen_frame, tr.last_seen_t, tr.state = frame, now, "visible"
        return {"moved": moved, "gone": gone}

    def _predict(self, tr: Track, now: float) -> Tuple[float, float]:
        cx, cy = _center(tr.bbox)
        dt = min(now - tr.last_seen_t, self.predict_ttl)
        return cx + tr.velocity[0] * dt, cy + tr.velocity[1] * dt

    def _mark_missing(self, tr: Track, now: float, gone: List[str], why: str) -> None:
        age = now - tr.last_seen_t
        if tr.state == "visible":
            self._emit("TARGET_DISAPPEARED", target_id=tr.target_id, why=why)
            gone.append(tr.target_id)
        moving = abs(tr.velocity[0]) + abs(tr.velocity[1]) > 5
        if age <= self.predict_ttl and moving:
            tr.state = "predicted"
            px, py = self._predict(tr, now)
            tr.bbox = (int(px - tr.bbox[2] / 2), int(py - tr.bbox[3] / 2), tr.bbox[2], tr.bbox[3])
            tr.confidence *= 0.7
        elif age > self.lost_ttl:
            with self._lock:
                self._tracks.pop(tr.target_id, None)
        else:
            tr.state = "lost"
