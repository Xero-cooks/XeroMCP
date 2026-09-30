"""Deterministic cursor trajectories (no fake human jitter). Perception and
interruption checks run between waypoints; injection stays on the input thread."""
from __future__ import annotations

import math
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

Pt = Tuple[int, int]


def plan_path(start: Pt, end: Pt, max_step_px: int = 160, max_steps: int = 16, ease: bool = True) -> List[Pt]:
    """Waypoints from start (excluded) to end (included). Smoothstep easing."""
    dist = math.hypot(end[0] - start[0], end[1] - start[1])
    n = max(1, min(max_steps, int(math.ceil(dist / max(8, max_step_px)))))
    pts: List[Pt] = []
    for i in range(1, n + 1):
        t = i / n
        if ease:
            t = t * t * (3 - 2 * t)
        p = (int(round(start[0] + (end[0] - start[0]) * t)), int(round(start[1] + (end[1] - start[1]) * t)))
        if not pts or p != pts[-1]:
            pts.append(p)
    if not pts or pts[-1] != (int(end[0]), int(end[1])):
        pts.append((int(end[0]), int(end[1])))
    return pts


def run_trajectory(path: List[Pt], move_fn: Callable[[Pt], Any],
                   check_fn: Optional[Callable[[int, Pt], Optional[str]]] = None,
                   retarget_fn: Optional[Callable[[], Optional[Pt]]] = None,
                   step_delay: float = 0.0, sleep=time.sleep, max_retargets: int = 2) -> Dict[str, Any]:
    """Walk the path. check_fn(step, point) -> reason string to abort. retarget_fn()
    returns a NEW end point if the tracked target shifted (path is re-planned from
    the current position) or None to keep going."""
    done = 0
    cur: Optional[Pt] = None
    retargets = 0
    i = 0
    path = list(path)
    while i < len(path):
        p = path[i]
        if check_fn:
            reason = check_fn(i, p)
            if reason:
                return {"status": "interrupted", "reason": reason, "steps_done": done, "at": cur,
                        "steps_planned": len(path)}
        if retarget_fn and i < len(path) - 1:
            new_end = retarget_fn()
            if new_end and tuple(new_end) != path[-1]:
                if retargets >= max_retargets:
                    return {"status": "interrupted", "reason": "target_kept_moving", "steps_done": done,
                            "at": cur, "steps_planned": len(path)}
                retargets += 1
                start = cur if cur else p
                path = plan_path(start, tuple(new_end)); i = 0
                continue
        r = move_fn(p)
        if isinstance(r, dict) and r.get("ok") is False:
            return {"status": "input_failed", "reason": r.get("error", "move failed"), "steps_done": done,
                    "at": cur, "steps_planned": len(path)}
        cur = p
        done += 1
        i += 1
        if step_delay and i < len(path):
            sleep(step_delay)
    return {"status": "reached", "steps_done": done, "at": cur, "steps_planned": len(path), "retargets": retargets}
