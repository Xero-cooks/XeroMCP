"""TARGET LOCK: short-lived memory of 'label -> exact box' with revalidation.

A lock is only reused when ALL of these still hold:
  * younger than ttl
  * same window hwnd and same window rect (no move/resize/switch)
  * the pixels inside the locked box still match (cheap crop signature)
Verification failure, disappearance or a material screen change drops it.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .frame import sig_diff
from .geometry import Rect

LOCK_TTL = 10.0
LOCK_PIXEL_TOLERANCE = 0.06   # mean abs gray diff (0..1) still considered "same"


def norm_label(label: str) -> str:
    return " ".join((label or "").lower().split())


@dataclass
class TargetLock:
    label: str
    rect: Rect
    cell: str
    confidence: float
    source: str
    frame_id: str
    hwnd: Optional[int]
    win_rect: Optional[Dict[str, int]]
    region_sig: Optional[bytes]
    ctype: str = ""
    invoke: bool = False
    ts: float = field(default_factory=time.monotonic)
    uses: int = 0

    @property
    def age(self) -> float:
        return time.monotonic() - self.ts

    def to_dict(self) -> Dict[str, object]:
        return {"label": self.label, "cell": self.cell, "box": self.rect.to_dict(),
                "confidence": round(self.confidence, 3), "source": self.source,
                "frame_id": self.frame_id, "age_ms": int(self.age * 1000), "uses": self.uses}


class LockStore:
    def __init__(self, ttl: float = LOCK_TTL) -> None:
        self.ttl = ttl
        self._locks: Dict[str, TargetLock] = {}
        self._mu = threading.Lock()
        self.events: List[str] = []   # recent invalidation reasons (bounded)

    def stats(self) -> Dict[str, Any]:
        with self._mu:
            live = [l for l in self._locks.values() if l.age <= self.ttl]
            return {"live": len(live), "labels": [l.label for l in live][:10],
                    "recent_invalidations": list(self.events[-5:])}

    def _note(self, msg: str) -> None:
        self.events.append(msg)
        del self.events[:-20]

    def put(self, lock: TargetLock) -> None:
        with self._mu:
            self._locks[norm_label(lock.label)] = lock

    def get(self, label: str, hwnd: Optional[int], win_rect: Optional[Dict[str, int]]) -> Optional[TargetLock]:
        key = norm_label(label)
        with self._mu:
            lk = self._locks.get(key)
            if lk is None:
                return None
            reason = ""
            if lk.age > self.ttl:
                reason = "timeout"
            elif hwnd is not None and lk.hwnd is not None and hwnd != lk.hwnd:
                reason = "window_changed"
            elif win_rect is not None and lk.win_rect is not None and win_rect != lk.win_rect:
                reason = "window_moved"
            if reason:
                self._locks.pop(key, None)
                self._note(f"{key}:{reason}")
                return None
            return lk

    def revalidate(self, lock: TargetLock, current_sig: Optional[bytes]) -> Dict[str, object]:
        """Compare the locked region's pixels now vs at lock time."""
        if lock.region_sig is None or current_sig is None:
            return {"valid": False, "reason": "no_signature"}
        d = sig_diff(lock.region_sig, current_sig)
        ok = d <= LOCK_PIXEL_TOLERANCE
        if not ok:
            self.invalidate(lock.label, f"pixels_changed:{d:.3f}")
        return {"valid": ok, "diff": round(d, 4)}

    def invalidate(self, label: str, reason: str) -> None:
        with self._mu:
            if self._locks.pop(norm_label(label), None) is not None:
                self._note(f"{norm_label(label)}:{reason}")

    def invalidate_window(self, hwnd: Optional[int], reason: str) -> int:
        with self._mu:
            keys = [k for k, v in self._locks.items() if hwnd is None or v.hwnd == hwnd]
            for k in keys:
                self._locks.pop(k, None)
            if keys:
                self._note(f"*{len(keys)}:{reason}")
            return len(keys)

    def clear(self) -> None:
        with self._mu:
            self._locks.clear()

    def snapshot(self) -> List[Dict[str, object]]:
        with self._mu:
            return [v.to_dict() for v in self._locks.values()]
