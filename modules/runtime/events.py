"""Bounded, thread-safe change/event buffer. Compact records, no payloads."""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Deque, Dict, Iterable, List, Optional

KINDS = frozenset({
    "WINDOW_CHANGED", "PROFILE_CHANGED", "URL_CHANGED", "TARGET_APPEARED", "TARGET_MOVED",
    "TARGET_DISAPPEARED", "FOCUS_CHANGED", "MODAL_APPEARED", "SCREEN_CHANGED",
    "ACTION_STARTED", "ACTION_COMPLETED", "ACTION_INTERRUPTED", "VERIFICATION_PASSED",
    "VERIFICATION_FAILED", "CAPABILITY_CHANGED", "STATE_INVALIDATED",
})
_COALESCE = {"SCREEN_CHANGED"}     # bursts collapse into one record with a count


class EventBuffer:
    def __init__(self, maxlen: int = 256, clock=time.time) -> None:
        self._buf: Deque[Dict[str, Any]] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._seq = 0
        self._clock = clock
        self.maxlen = maxlen

    @property
    def last_seq(self) -> int:
        return self._seq

    def emit(self, kind: str, **data: Any) -> Dict[str, Any]:
        if kind not in KINDS:
            raise ValueError(f"unknown event kind {kind}")
        now = self._clock()
        with self._lock:
            last = self._buf[-1] if self._buf else None
            if (kind in _COALESCE and last and last["kind"] == kind
                    and now - last["t"] < 0.75 and last.get("class") == data.get("class")):
                last["n"] = last.get("n", 1) + 1
                last["t"] = now
                return last
            self._seq += 1
            ev = {"seq": self._seq, "t": round(now, 3), "kind": kind, **data}
            self._buf.append(ev)
            return ev

    def since(self, seq: int = 0, kinds: Optional[Iterable[str]] = None, limit: int = 50) -> Dict[str, Any]:
        want = set(kinds) if kinds else None
        with self._lock:
            evs = [dict(e) for e in self._buf if e["seq"] > seq and (want is None or e["kind"] in want)]
            oldest = self._buf[0]["seq"] if self._buf else self._seq + 1
        dropped = seq + 1 < oldest and self._seq > seq
        return {"events": evs[-limit:], "last_seq": self._seq, "dropped": bool(dropped)}

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()
