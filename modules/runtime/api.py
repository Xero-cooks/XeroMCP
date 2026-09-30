"""Platform-free implementation of the `xero` runtime tool (thin wrapper in main.py)."""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from .. import until_proof
from .stream import StreamRunner

STEP_KEYS = ("target", "cell", "x", "y", "target2", "to_cell", "to_x", "to_y", "near", "text", "keys",
             "amount", "display", "ms", "until", "window", "scope", "min_confidence", "motion",
             "timeout_ms", "frame_id", "retries", "dry_run", "command")
STEP_TYPES = {"move", "hover", "click", "double_click", "right_click", "drag", "scroll", "type", "key",
              "wait", "wait_until", "observe"}


def normalize_steps(steps: Any) -> List[Dict[str, Any]]:
    if not isinstance(steps, list) or not steps:
        raise ValueError("steps must be a non-empty list of {type, target|cell|..., until?}")
    out = []
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            raise ValueError(f"step {i} must be an object")
        t = str(s.get("type") or s.get("action") or "").lower()
        if t not in STEP_TYPES:
            raise ValueError(f"step {i}: unknown type {t!r}; valid: {sorted(STEP_TYPES)}")
        if t == "wait_until" and not s.get("condition"):
            raise ValueError(f"step {i}: wait_until needs 'condition'")
        out.append({**s, "type": t})
    return out


def make_condition_checker(rt, eng, sleep=time.sleep) -> Callable[[str, float], Dict[str, Any]]:
    def check(cond: str, timeout_s: float) -> Dict[str, Any]:
        deadline = time.monotonic() + max(0.3, timeout_s)
        proof: Dict[str, Any] = {"until_ok": False, "how": "not_checked"}
        while True:
            if rt.sampler:
                rt.tick_fast()
            kind = until_proof.parse_until(cond)["kind"]
            if kind in ("url_contains", "title_contains"):        # free, screenshot-less evidence first
                p = until_proof.check_until(cond, url=rt.browser.get("url") or "", title=rt.window.get("title") or "")
                if p.get("until_ok"):
                    return {**p, "how": "state:" + p["how"]}
            ctx = eng._context("")
            proof = eng.b.check_until(cond, ctx, deadline)
            if proof.get("until_ok") or time.monotonic() >= deadline:
                return proof
            sleep(0.25)
    return check


def make_act(eng, spatial_run, defaults: Dict[str, Any]) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    def act(step: Dict[str, Any]) -> Dict[str, Any]:
        kw = {k: step[k] for k in STEP_KEYS if k in step and step[k] not in (None, "")}
        kw.setdefault("motion", defaults.get("motion", "sniper"))
        kw.setdefault("timeout_ms", defaults.get("timeout_ms", 2500))
        return spatial_run(eng, action=step["type"], **kw)
    return act


def run_stream(rt, eng, spatial_run, steps: Any, session: str = "default", motion: str = "sniper",
               timeout_ms: int = 2500) -> Dict[str, Any]:
    try:
        steps = normalize_steps(steps)
    except ValueError as e:
        return {"status": "bad_request", "error": str(e)}
    submitted = time.monotonic()
    runner = StreamRunner(rt, make_act(eng, spatial_run, {"motion": motion, "timeout_ms": timeout_ms}),
                          make_condition_checker(rt, eng), cancelled=lambda: eng._cancel_ts >= submitted,
                          emit_lifecycle=False)
    return runner.run(steps, sid=session)


def status_of(rt, eng) -> Dict[str, Any]:
    return {"status": "ok", "runtime": dict(rt.stats), "capabilities": rt.caps.snapshot(),
            "capability_probes": dict(rt.caps.stats), "tracked_targets": len(rt.tracker.all()),
            "reacquisitions": rt.tracker.reacquisitions, "perception_running": bool(rt._thread and rt._thread.is_alive()),
            "sessions": sorted(rt.sessions), "event_buffer": {"last_seq": rt.events.last_seq, "max": rt.events.maxlen}}
