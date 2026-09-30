"""Action stream: several related actions under one runtime session.

Every step is EXPECT -> ACT -> VERIFY. The runner continues only while the
world still matches what the previous step verified; otherwise it invalidates,
re-acquires and either retries the (not yet fired) step once or returns a
structured interruption. It never re-fires an action that already fired.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from .core import HARD_REASONS, ComputerRuntime

OK = {"hit", "ok", "dry_run", "verified"}
WEAK_CHANGE = {"target_region", "major_layout", "window", "navigation", "small_region"}
POINTER = {"move", "hover", "click", "double_click", "right_click", "drag", "scroll"}


def default_expect(step: Dict[str, Any]) -> str:
    t = step.get("type", "")
    if step.get("until"):
        return f"until: {step['until']}"
    if t in ("click", "double_click", "right_click"):
        return "state change after click (window/screen/target region)"
    if t == "wait_until":
        return f"condition: {step.get('condition')}"
    if t == "type":
        return "text entered (focus proven)"
    return f"{t} completes"


class StreamRunner:
    def __init__(self, rt: ComputerRuntime, act: Callable[[Dict[str, Any]], Dict[str, Any]],
                 check_condition: Callable[[str, float], Dict[str, Any]],
                 cancelled: Callable[[], bool] = lambda: False, settle_s: float = 0.6, emit_lifecycle: bool = True,
                 sleep: Callable[[float], None] = time.sleep, clock=time.monotonic) -> None:
        self.rt, self.act, self.check_condition, self.cancelled = rt, act, check_condition, cancelled
        self.settle_s, self.sleep, self.clock = settle_s, sleep, clock
        self.emit_lifecycle = emit_lifecycle

    # weak, honest evidence for steps that carry no `until`
    def _settle(self, token_seq: int, budget: float) -> Optional[Dict[str, Any]]:
        end = self.clock() + budget
        while True:
            self.rt.tick_fast()
            self.rt.tick_medium()
            for e in self.rt.events.since(token_seq, limit=100)["events"]:
                if e["kind"] in ("FOCUS_CHANGED", "URL_CHANGED", "PROFILE_CHANGED", "MODAL_APPEARED"):
                    return {"how": "state:" + e["kind"].lower(), "strength": "weak"}
                if e["kind"] == "SCREEN_CHANGED" and e.get("class") in WEAK_CHANGE:
                    return {"how": "state:screen_" + str(e.get("class")), "strength": "weak"}
            if self.clock() >= end:
                return None
            self.sleep(0.1)

    def run(self, steps: List[Dict[str, Any]], sid: Optional[str] = None, max_steps: int = 25) -> Dict[str, Any]:
        rt = self.rt
        t0 = self.clock()
        rt.session(sid)
        start_seq = rt.events.last_seq
        if rt.sampler:
            rt.tick_fast()
            rt.tick_medium()
        start_seq = rt.events.last_seq
        baseline = rt.assume()
        out_steps: List[Dict[str, Any]] = []
        status, interruption = "completed", None
        steps = steps[:max_steps]
        reacquired = 0
        for i, step in enumerate(steps):
            if self.cancelled():
                status, interruption = "cancelled", {"at_step": i, "reason": "cancelled"}
                break
            typ = (step.get("type") or step.get("action") or "").lower()
            step = {**step, "type": typ}
            expect = step.get("expect") or default_expect(step)
            attempt = 0
            while True:
                attempt += 1
                if rt.sampler:
                    rt.tick_fast()
                    rt.tick_medium()
                reason = None if step.get("expect_window_change") else rt.check(baseline)
                if reason:
                    rt.events.emit("ACTION_INTERRUPTED", reason=reason, step=i)
                    rt.invalidate(f"stream: {reason}")
                    if reason in HARD_REASONS or attempt > 1:
                        status = "interrupted"
                        interruption = {"at_step": i, "reason": reason, "fired": False,
                                        "hint": "re-observe (op=observe), decide, and resubmit the remaining steps"}
                        break
                    reacquired += 1
                    if rt.sampler:
                        rt.tick_deep(f"reacquire after {reason}")
                    baseline = rt.assume()
                    continue        # not fired yet -> safe to re-resolve and try this step once more
                break
            if status == "interrupted":
                break
            rec = self._execute(step, i, typ, expect, baseline)
            out_steps.append(rec)
            if rec["status"] not in OK and not rec.get("verified"):
                status = "failed" if rec["status"] != "unverified" else "unverified"
                interruption = {"at_step": i, "reason": rec["status"], "fired": rec.get("fired", False)}
                break
            baseline = rt.assume()
        done = sum(1 for r in out_steps if r.get("verified") or r["status"] in OK)
        res = {"status": status, "completed": done, "total": len(steps), "steps": out_steps,
               "reacquired": reacquired, "ms": int((self.clock() - t0) * 1000), "model_round_trips": 1,
               "events": rt.events.since(start_seq, limit=30)["events"], "state": rt.observe(sid)}
        if interruption:
            res["interruption"] = interruption
        return res

    def _execute(self, step: Dict[str, Any], i: int, typ: str, expect: str, baseline: Dict[str, Any]) -> Dict[str, Any]:
        rt = self.rt
        rec: Dict[str, Any] = {"i": i, "type": typ, "expect": expect}
        t0 = self.clock()
        if self.emit_lifecycle or typ == "wait_until":
            rt.action_started(step, expect)
        seq = rt.events.last_seq
        if typ == "wait_until":
            proof = self.check_condition(step.get("condition", ""), float(step.get("timeout_ms", 5000)) / 1000.0)
            ok = bool(proof.get("until_ok"))
            rec.update(status="ok" if ok else "timeout", verified=ok, evidence=[proof.get("how")],
                       fired=False)
            rt.action_finished({"status": rec["status"], "proof": proof})
            rec["ms"] = int((self.clock() - t0) * 1000)
            return rec
        r = self.act(step)
        status = str(r.get("status"))
        rec.update(status=status, fired=status in ("hit", "fired_unverified", "ok"))
        if r.get("tries"):
            rec["tries"] = r["tries"]
        proof = r.get("proof") or {}
        evidence: List[str] = []
        verified = False
        if proof.get("until_ok") is True:
            verified, evidence = True, [str(proof.get("how"))]
        elif status == "fired_unverified" or (status == "hit" and not proof):
            # no semantic proof was requested/produced: look for honest state evidence
            if typ in ("click", "double_click", "right_click", "drag"):
                w = self._settle(seq, self.settle_s)
                if w:
                    verified, evidence = True, [w["how"] + " (weak: state changed, content not confirmed)"]
                    rec["evidence_strength"] = "weak"
                else:
                    rec["status"] = "unverified"
            else:
                verified = status in OK      # move/scroll/key: input confirmed by cursor/focus proof only
                evidence = ["input_delivery"]
                if not verified:
                    rec["status"] = "unverified"
        elif status in OK:
            verified, evidence = True, [str(proof.get("how") or "input_delivery")]
        rec.update(verified=verified, evidence=evidence)
        if r.get("resolved"):
            rec["resolved"] = {k: r["resolved"].get(k) for k in ("cell", "source", "confidence", "text") if k in r["resolved"]}
        if not verified:
            rec["detail"] = {k: r.get(k) for k in ("error", "hint", "candidates", "reason") if r.get(k)}
        if self.emit_lifecycle:
            rt.action_finished({**r, "status": rec["status"] if rec["status"] == "unverified" else status})
        rec["ms"] = int((self.clock() - t0) * 1000)
        return rec
