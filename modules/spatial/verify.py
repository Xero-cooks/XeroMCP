"""BEFORE -> action -> AFTER comparison helpers."""
from __future__ import annotations

import time
from typing import Callable, Dict, Optional

from .frame import sig_diff

CHANGE_THRESHOLD = 0.015     # mean gray diff (0..1) that counts as "pixels changed"


def poll_until(check: Callable[[], Dict[str, object]], deadline: float,
               interval: float = 0.08, sleep: Callable[[float], None] = time.sleep,
               first_delay: float = 0.06, is_cancelled: Callable[[], bool] = lambda: False) -> Dict[str, object]:
    """Poll a proof check until it passes or the deadline expires. Always
    performs at least one check (a result without a check is not a proof)."""
    sleep(first_delay)
    polls = 0
    res: Dict[str, object] = {"until_ok": False, "how": "none"}
    while True:
        polls += 1
        res = dict(check())
        if res.get("until_ok") or time.monotonic() + interval >= deadline or is_cancelled():
            break
        sleep(interval)
    res["polls"] = polls
    return res


def evidence(before_sig: Optional[bytes], after_sig: Optional[bytes],
             fg_before: Optional[int], fg_after: Optional[int],
             cursor: Optional[tuple], point: Optional[tuple]) -> Dict[str, object]:
    delta = sig_diff(before_sig, after_sig) if (before_sig and after_sig) else None
    ev: Dict[str, object] = {
        "visual_delta": round(delta, 4) if delta is not None else None,
        "pixels_changed": bool(delta is not None and delta >= CHANGE_THRESHOLD),
        "foreground_changed": bool(fg_before and fg_after and fg_before != fg_after),
    }
    if cursor and point:
        ev["cursor_on_target"] = abs(cursor[0] - point[0]) <= 1 and abs(cursor[1] - point[1]) <= 1
    ev["changed"] = bool(ev["pixels_changed"] or ev["foreground_changed"])
    return ev
