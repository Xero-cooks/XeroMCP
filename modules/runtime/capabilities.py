"""Capability cache: probe once, remember, re-probe only when conditions
materially change (epoch) or on explicit reset. Kills the per-action CDP probe."""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Optional

AVAILABLE, UNAVAILABLE, UNKNOWN = "available", "unavailable", "unknown"


class CapabilityRegistry:
    def __init__(self, on_change: Optional[Callable[[str, str, str], None]] = None,
                 clock=time.monotonic) -> None:
        self._caps: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._on_change = on_change
        self._clock = clock
        self.stats = {"probes": 0, "skipped": 0}

    def state(self, name: str, epoch: Any = None) -> str:
        """Cached state; a different epoch means 'conditions changed' -> unknown."""
        with self._lock:
            c = self._caps.get(name)
            if c is None or c["epoch"] != epoch:
                return UNKNOWN
            return c["state"]

    def set(self, name: str, state: str, epoch: Any = None, reason: str = "") -> None:
        with self._lock:
            old = self._caps.get(name, {}).get("state", UNKNOWN)
            self._caps[name] = {"state": state, "epoch": epoch, "reason": reason, "since": self._clock()}
        if old != state and self._on_change:
            self._on_change(name, old, state)

    def reset(self, name: Optional[str] = None) -> None:
        with self._lock:
            if name:
                self._caps.pop(name, None)
            else:
                self._caps.clear()

    def probe(self, name: str, fn: Callable[[], Any], epoch: Any = None):
        """Run fn() unless the cache already says unavailable for this epoch.
        Returns (state, value). Exceptions mark the capability unavailable."""
        st = self.state(name, epoch)
        if st == UNAVAILABLE:
            self.stats["skipped"] += 1
            return UNAVAILABLE, None
        self.stats["probes"] += 1
        try:
            val = fn()
        except Exception as e:  # noqa: BLE001
            self.set(name, UNAVAILABLE, epoch, f"{type(e).__name__}")
            return UNAVAILABLE, None
        if val is None or val is False:
            self.set(name, UNAVAILABLE, epoch, "empty")
            return UNAVAILABLE, None
        self.set(name, AVAILABLE, epoch)
        return AVAILABLE, val

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {k: {"state": v["state"], "reason": v["reason"],
                        "age_s": round(self._clock() - v["since"], 1)} for k, v in self._caps.items()}
