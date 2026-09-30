"""Lightweight per-stage latency telemetry (no payloads, no screen text)."""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional


class Stopwatch:
    def __init__(self) -> None:
        self.t0 = time.perf_counter()
        self._last = self.t0
        self.stages: Dict[str, float] = {}

    def lap(self, stage: str) -> float:
        now = time.perf_counter()
        ms = (now - self._last) * 1000.0
        self.stages[stage] = round(self.stages.get(stage, 0.0) + ms, 2)
        self._last = now
        return ms

    class _Span:
        def __init__(self, sw: "Stopwatch", stage: str) -> None:
            self.sw, self.stage = sw, stage

        def __enter__(self):
            self.start = time.perf_counter()
            return self

        def __exit__(self, *exc):
            ms = (time.perf_counter() - self.start) * 1000.0
            self.sw.stages[self.stage] = round(self.sw.stages.get(self.stage, 0.0) + ms, 2)
            self.sw._last = time.perf_counter()
            return False

    def span(self, stage: str) -> "Stopwatch._Span":
        return Stopwatch._Span(self, stage)

    def total_ms(self) -> float:
        return round((time.perf_counter() - self.t0) * 1000.0, 2)

    def report(self) -> Dict[str, float]:
        out = dict(self.stages)
        out["total"] = self.total_ms()
        return out


class Telemetry:
    """Rolling window of stage timings per operation kind."""

    def __init__(self, window: int = 200) -> None:
        self._data: Dict[str, Dict[str, Deque[float]]] = defaultdict(lambda: defaultdict(lambda: deque(maxlen=window)))
        self._lock = threading.Lock()

    def record(self, op: str, report: Dict[str, float]) -> None:
        with self._lock:
            for stage, ms in report.items():
                self._data[op][stage].append(float(ms))

    def summary(self, op: Optional[str] = None) -> Dict[str, Dict[str, Dict[str, float]]]:
        with self._lock:
            ops = [op] if op else list(self._data)
            out: Dict[str, Dict[str, Dict[str, float]]] = {}
            for o in ops:
                stages = self._data.get(o, {})
                out[o] = {}
                for st, vals in stages.items():
                    if not vals:
                        continue
                    s = sorted(vals)
                    out[o][st] = {"n": len(s), "p50": round(s[len(s) // 2], 2),
                                  "p95": round(s[min(len(s) - 1, int(len(s) * 0.95))], 2),
                                  "max": round(s[-1], 2)}
            return out


TELEMETRY = Telemetry()
