"""ComputerRuntime: ONE shared, persistent world model for every tool.

  sampler (fast/medium/deep) ─┐
  see / spatial_point / chrome ─┴─> ComputerRuntime ─> XeroState + EventBuffer
                                        │  TargetTracker · CapabilityRegistry
                                        └─> observe() (compact) · assume()/check() (reactive interruption)

The runtime NEVER injects input. Physical mouse/keyboard stays on the single
input thread (mouse_runtime); this module only reads, remembers and judges.
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from .capabilities import CapabilityRegistry
from .changes import ChangeClassifier, cells_for_point, cells_for_rect, signature_gray
from .events import EventBuffer
from .tracker import TargetTracker

STALE_AFTER_S = 6.0
SESSION_TTL_S = 300.0
HARD_REASONS = {"modal_appeared", "focus_lost", "profile_changed"}


def _rect_t(r: Any) -> Optional[Tuple[int, int, int, int]]:
    if not r:
        return None
    if isinstance(r, dict):
        return int(r.get("x", 0)), int(r.get("y", 0)), int(r.get("w", 0)), int(r.get("h", 0))
    return tuple(int(v) for v in r)   # type: ignore[return-value]


def _inside(inner, outer) -> bool:
    return (inner[0] >= outer[0] - 2 and inner[1] >= outer[1] - 2 and
            inner[0] + inner[2] <= outer[0] + outer[2] + 2 and inner[1] + inner[3] <= outer[1] + outer[3] + 2)


class Session:
    def __init__(self, sid: str, now: float) -> None:
        self.id = sid
        self.created = now
        self.last_touch = now
        self.event_cursor = 0
        self.current_action: Optional[Dict[str, Any]] = None
        self.last_action: Optional[Dict[str, Any]] = None
        self.active_app = ""
        self.chrome_profile: Optional[str] = None


class ComputerRuntime:
    def __init__(self, sampler: Any = None, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time, on_invalidate: Optional[Callable[[str], None]] = None) -> None:
        self._clock, self._wall = clock, wall
        self._lock = threading.RLock()
        self.sampler = sampler
        self.events = EventBuffer(clock=wall)
        self.caps = CapabilityRegistry(on_change=lambda n, o, s: self.events.emit("CAPABILITY_CHANGED", name=n, old=o, new=s),
                                       clock=clock)
        self.tracker = TargetTracker(on_event=lambda kind, **k: self.events.emit(kind, **k), clock=clock)
        self.classifier = ChangeClassifier()
        self.on_invalidate = on_invalidate          # e.g. engine.locks / cache invalidation
        self.sessions: Dict[str, Session] = {}
        self.frame_id = 0
        self.updated_at = clock()
        self.window: Dict[str, Any] = {}
        self.browser: Dict[str, Any] = {}
        self.cursor: Optional[Tuple[int, int]] = None
        self.visual: Dict[str, Any] = {"change": {"class": "none", "cells": []}, "sig": None, "bounds": None,
                                       "texts": [], "elements": 0, "surface_id": "", "spatial_frame_id": None,
                                       "coord": None}
        self.interaction: Dict[str, Any] = {"blocked": None, "action_status": "idle", "verification": None,
                                            "expected": None}
        self.space: Optional[Dict[str, Any]] = None
        self._deep_reason: Optional[str] = None
        self._last_deep = 0.0
        self.stats = {"fast": 0, "medium": 0, "deep": 0, "errors": 0, "screenshots": 0, "reacquisitions": 0}
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.intervals = {"fast": 0.25, "medium": 1.0, "deep_min": 1.5}

    # ---- sessions ---------------------------------------------------------------
    def session(self, sid: Optional[str] = None) -> Session:
        sid = sid or "default"
        with self._lock:
            s = self.sessions.get(sid)
            now = self._clock()
            if s is None:
                for k in [k for k, v in self.sessions.items() if now - v.last_touch > SESSION_TTL_S]:
                    self.sessions.pop(k)
                s = self.sessions[sid] = Session(sid, now)
                s.event_cursor = self.events.last_seq
            s.last_touch = now
            return s

    def _active(self) -> bool:
        now = self._clock()
        return any(now - s.last_touch < SESSION_TTL_S for s in self.sessions.values())

    # ---- state mutation ------------------------------------------------------------
    def _bump(self) -> int:
        self.frame_id += 1
        self.updated_at = self._clock()
        return self.frame_id

    def request_deep(self, reason: str) -> None:
        self._deep_reason = self._deep_reason or reason

    def invalidate(self, reason: str, tracks: bool = True, locks: bool = True) -> None:
        if tracks:
            self.tracker.invalidate(reason=reason)
        if locks and self.on_invalidate:
            try:
                self.on_invalidate(reason)
            except Exception:
                pass
        self.events.emit("STATE_INVALIDATED", scope="all", reason=reason)
        self._bump()

    def update_window(self, info: Dict[str, Any]) -> List[str]:
        """Feed foreground-window info (hwnd,title,rect,exe,pid,monitor,dpi)."""
        info = {k: info.get(k) for k in ("hwnd", "title", "rect", "exe", "pid", "monitor", "dpi", "class") if k in info}
        changes: List[str] = []
        with self._lock:
            old = self.window
            if old.get("hwnd") != info.get("hwnd"):
                changes.append("hwnd")
                self.events.emit("FOCUS_CHANGED", hwnd=info.get("hwnd"), title=(info.get("title") or "")[:80])
                a, b = _rect_t(old.get("rect")), _rect_t(info.get("rect"))
                if old and a and b and _inside(b, a) and b[2] * b[3] < 0.8 * a[2] * a[3]:
                    self.events.emit("MODAL_APPEARED", hwnd=info.get("hwnd"), title=(info.get("title") or "")[:80])
                self.events.emit("WINDOW_CHANGED", change="foreground", title=(info.get("title") or "")[:80],
                                 exe=info.get("exe"))
            elif old.get("title") != info.get("title"):
                changes.append("title")
                self.events.emit("WINDOW_CHANGED", change="title", title=(info.get("title") or "")[:80])
            elif old.get("rect") != info.get("rect") and info.get("rect"):
                changes.append("rect")
                self.events.emit("WINDOW_CHANGED", change="geometry")
            self.window = info
            if changes:
                self._bump()
                if "hwnd" in changes:
                    self.request_deep("foreground window changed")
                for s in self.sessions.values():
                    s.active_app = str(info.get("exe") or "")
        return changes

    def update_browser(self, b: Dict[str, Any]) -> List[str]:
        keys = ("profile", "directory", "profile_verified", "url", "page_title", "loading", "cdp", "hwnd")
        b = {k: b.get(k) for k in keys if k in b}
        changes: List[str] = []
        with self._lock:
            old = self.browser
            if old.get("directory") != b.get("directory") or old.get("profile") != b.get("profile"):
                if old:
                    changes.append("profile")
                    self.events.emit("PROFILE_CHANGED", old=old.get("profile"), new=b.get("profile"))
            if old.get("url") != b.get("url") and b.get("url"):
                changes.append("url")
                self.events.emit("URL_CHANGED", url=str(b.get("url"))[:200])
            elif old.get("page_title") != b.get("page_title") and old:
                changes.append("url")           # no URL source (real Chrome has no CDP): a page-title change is the signal
                self.events.emit("URL_CHANGED", page_title=str(b.get("page_title"))[:120], via="title")
            self.browser = {**b, "cdp": self.caps.state("cdp", b.get("hwnd"))}
            if changes:
                self._bump()
                if "url" in changes or "profile" in changes:
                    self.request_deep("browser navigation")
        return changes

    def set_space(self, space: Dict[str, Any]) -> None:
        with self._lock:
            if self.space and self.space["space_id"] != space["space_id"]:
                self.events.emit("STATE_INVALIDATED", scope="coordinate_space", reason="monitor/DPI layout changed")
                self.tracker.invalidate(reason="coordinate space changed")
                if self.on_invalidate:
                    self.on_invalidate("coordinate space changed")
            self.space = space

    # ---- perception ticks -------------------------------------------------------------
    def tick_fast(self) -> Dict[str, Any]:
        self.stats["fast"] += 1
        s = self.sampler.fast() if self.sampler else {}
        if s.get("cursor"):
            self.cursor = tuple(s["cursor"])
        ch = self.update_window(s["fg"]) if s.get("fg") else []
        return {"window_changes": ch}

    def tick_medium(self) -> Dict[str, Any]:
        self.stats["medium"] += 1
        if not self.sampler:
            return {}
        out: Dict[str, Any] = {}
        m = self.sampler.medium() or {}
        if m.get("space"):
            self.set_space(m["space"])
        if m.get("browser"):
            out["browser_changes"] = self.update_browser(m["browser"])
        sig, bounds = m.get("sig"), _rect_t(m.get("bounds"))
        if sig is not None and bounds:
            self.stats["screenshots"] += 1
            out["change"] = self._ingest_sig(sig, bounds, window_changed=False,
                                             url_changed="url" in out.get("browser_changes", []))
        return out

    def tick_deep(self, reason: str = "explicit") -> Dict[str, Any]:
        self.stats["deep"] += 1
        self._last_deep = self._clock()
        self._deep_reason = None
        if not self.sampler:
            return {}
        d = self.sampler.deep() or {}
        if d.get("ingested"):                   # sampler already fed a full frame via ingest_frame()
            return {"reason": reason, "ingested": True}
        self.stats["screenshots"] += 1
        return self._ingest_elements(d.get("elements", []), _rect_t(d.get("bounds")), d.get("sig"),
                                     surface_id=d.get("surface_id", ""), reason=reason)

    def _ingest_sig(self, sig: bytes, bounds, window_changed: bool, url_changed: bool) -> Dict[str, Any]:
        with self._lock:
            prev = self.visual["sig"]
            tcells = set()
            for t in self.tracker.all():
                tcells |= cells_for_rect(t.bbox, bounds)
            ccells = cells_for_point(*self.cursor, bounds) if self.cursor else set()
            if prev is None and not (window_changed or url_changed):
                # first sight of the screen is a baseline, not a "change"
                from .changes import POLICY
                ch = {"class": "none", "cells": [], "n_cells": 0, **POLICY["none"]}
            else:
                ch = self.classifier.classify(prev, sig, window_changed=window_changed, url_changed=url_changed,
                                              target_cells=tcells, cursor_cells=ccells)
            self.visual.update(sig=sig, bounds=bounds, change=ch)
            if ch["class"] not in ("none",):
                self.events.emit("SCREEN_CHANGED", **{"class": ch["class"], "cells": ch["cells"][:8]})
            if ch["class"] not in ("none", "cursor_only", "animation"):
                self._bump()
            if not ch["keep_locks"]:
                self.tracker.invalidate(reason=f"screen {ch['class']}") if ch["class"] in ("major_layout", "window", "navigation") else None
                if self.on_invalidate:
                    try:
                        self.on_invalidate(f"screen {ch['class']}")
                    except Exception:
                        pass
            if ch["deep"]:
                self.request_deep(f"screen {ch['class']}")
            elif ch["ocr"] and self.tracker.all():
                self.request_deep(f"tracked region {ch['class']}")
            return ch

    def _ingest_elements(self, elements: List[Dict[str, Any]], bounds, sig, surface_id: str, reason: str) -> Dict[str, Any]:
        with self._lock:
            if sig is not None and bounds:
                if self.visual["sig"] is not None and self.visual["bounds"] == bounds:
                    self._ingest_sig(sig, bounds, False, False)
                else:
                    self.visual.update(sig=sig, bounds=bounds)
            b = bounds or self.visual["bounds"] or (0, 0, 1, 1)
            res = self.tracker.update(elements, self.frame_id + 1, b, surface_id=surface_id)
            self.stats["reacquisitions"] = self.tracker.reacquisitions
            self.visual["elements"] = len(elements)
            self.visual["texts"] = [str(e.get("text"))[:40] for e in elements if e.get("text")][:40]
            self.visual["surface_id"] = surface_id
            self._bump()
            return {"reason": reason, **res}

    def ingest_frame(self, frame: Any, space: Optional[Dict[str, Any]] = None) -> int:
        """Called by see(grid)/engine.observe: the frame becomes the current visual state."""
        from .coords import frame_meta
        els = [{"text": e.text, "bbox": (e.rect.x, e.rect.y, e.rect.w, e.rect.h),
                "confidence": getattr(e, "confidence", 1.0), "source": getattr(e, "source", "ocr")}
               for e in frame.elements]
        sig = signature_gray(frame.image) if frame.image is not None else None
        s = frame.surface
        self.stats["screenshots"] += 1
        if space:
            self.set_space(space)
        self._ingest_elements(els, (s.x, s.y, s.w, s.h), sig, surface_id=f"{s.x},{s.y},{s.w},{s.h}", reason="frame")
        with self._lock:
            self.visual["spatial_frame_id"] = frame.frame_id
            self.visual["coord"] = frame_meta(frame, self.space)
            self.window = {**self.window, **{k: v for k, v in (frame.window or {}).items() if k in ("hwnd", "title", "rect")}}
        return self.frame_id

    # ---- background loop --------------------------------------------------------------
    def start(self) -> bool:
        if not self.sampler or (self._thread and self._thread.is_alive()):
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="xero-perception", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        next_medium = 0.0
        while not self._stop.is_set():
            try:
                if not self._active():
                    self._stop.wait(1.0)
                    continue
                now = self._clock()
                self.tick_fast()
                if now >= next_medium:
                    self.tick_medium()
                    next_medium = now + self.intervals["medium"]
                if self._deep_reason and now - self._last_deep >= self.intervals["deep_min"]:
                    self.tick_deep(self._deep_reason)
            except Exception:
                self.stats["errors"] += 1
                self._stop.wait(1.0)
            self._stop.wait(self.intervals["fast"])

    # ---- reactive interruption ----------------------------------------------------------
    def assume(self) -> Dict[str, Any]:
        """Snapshot of the assumptions an action is about to rely on."""
        with self._lock:
            return {"seq": self.events.last_seq, "hwnd": self.window.get("hwnd"), "title": self.window.get("title"),
                    "url": self.browser.get("url"), "profile": self.browser.get("directory"),
                    "t": self._clock()}

    def check(self, token: Dict[str, Any], max_age: float = STALE_AFTER_S) -> Optional[str]:
        """None if the assumptions still hold, else the reason to interrupt."""
        with self._lock:
            evs = self.events.since(token["seq"], limit=200)["events"]
        kinds = {e["kind"] for e in evs}
        if "MODAL_APPEARED" in kinds:
            return "modal_appeared"
        if "PROFILE_CHANGED" in kinds:
            return "profile_changed"
        if token.get("hwnd") is not None and self.window.get("hwnd") != token["hwnd"]:
            return "focus_lost"
        if "URL_CHANGED" in kinds:
            return "navigation"
        if any(e["kind"] == "SCREEN_CHANGED" and e.get("class") in ("major_layout", "window", "navigation") for e in evs):
            return "screen_changed"
        if "TARGET_DISAPPEARED" in kinds:
            return "target_disappeared"
        if "STATE_INVALIDATED" in kinds:
            return "state_invalidated"
        if self._clock() - self.updated_at > max_age and self.sampler:
            return "stale_state"
        return None

    def target_moved(self, target_id: str, since_seq: int) -> bool:
        return any(e["kind"] == "TARGET_MOVED" and e.get("target_id") == target_id
                   for e in self.events.since(since_seq, limit=200)["events"])

    def blocked(self, reason: Optional[str]) -> None:
        self.interaction["blocked"] = reason

    # ---- action lifecycle ---------------------------------------------------------------
    def action_started(self, step: Dict[str, Any], expect: Optional[str] = None, sid: Optional[str] = None) -> None:
        s = self.session(sid)
        brief = {k: v for k, v in step.items() if k in ("type", "action", "target", "cell", "text", "keys", "to", "until", "command")}
        if "text" in brief and isinstance(brief["text"], str) and len(brief["text"]) > 24:
            brief["text"] = brief["text"][:8] + "…"      # never echo long typed content
        s.current_action = brief
        self.interaction.update(action_status="running", expected=expect or step.get("until"), verification=None)
        self.events.emit("ACTION_STARTED", action=brief.get("type") or brief.get("action"), expect=expect or step.get("until"))

    def action_finished(self, result: Dict[str, Any], sid: Optional[str] = None) -> None:
        s = self.session(sid)
        status = str(result.get("status"))
        ok = status in ("hit", "ok", "dry_run", "verified")
        proof = result.get("proof") or {}
        with self._lock:
            s.last_action = {**(s.current_action or {}), "status": status}
            s.current_action = None
            verified = proof.get("until_ok")
            self.interaction.update(action_status=status, verification=(
                {"ok": verified, "how": proof.get("how")} if proof else None))
        if status == "interrupted" or status in ("stale_frame", "occluded", "focus_failed", "input_blocked"):
            self.events.emit("ACTION_INTERRUPTED", status=status, reason=result.get("reason") or result.get("error", "")[:80])
            if status == "input_blocked":
                self.blocked("input_blocked")
        else:
            self.events.emit("ACTION_COMPLETED", status=status)
        if proof.get("until_ok") is True:
            self.events.emit("VERIFICATION_PASSED", how=proof.get("how"))
        elif proof.get("until_ok") is False:
            self.events.emit("VERIFICATION_FAILED", how=proof.get("how"))
        self._bump()

    # ---- observation ----------------------------------------------------------------------
    def observe(self, sid: Optional[str] = None, refresh: bool = False, deep: bool = False,
                since: Optional[int] = None, max_targets: int = 12, max_text: int = 20) -> Dict[str, Any]:
        s = self.session(sid)
        if refresh or deep:
            if self.sampler:
                self.tick_fast()
                self.tick_medium()
            if deep:
                self.tick_deep("explicit")
        with self._lock:
            age = self._clock() - self.updated_at
            bounds = self.visual["bounds"]
            targets = [t.to_dict(bounds) for t in self.tracker.all()][:max_targets]
            cursor_since = s.event_cursor if since is None else since
            ev = self.events.since(cursor_since, limit=40)
            s.event_cursor = ev["last_seq"]
            ch = self.visual["change"]
            snap = {
                "frame_id": self.frame_id, "ts": round(self._wall(), 3), "age_ms": int(age * 1000),
                "session": {"id": s.id, "app": s.active_app or None, "profile": self.browser.get("profile")},
                "window": self.window or None,
                "browser": self.browser or None,
                "visual": {"change": {"class": ch["class"], "cells": ch["cells"][:8]},
                           "targets": targets, "text": self.visual["texts"][:max_text],
                           "spatial_frame_id": self.visual["spatial_frame_id"], "coord": self.visual["coord"]},
                "grid": (self.space or {}).get("grid"), "space_id": (self.space or {}).get("space_id"),
                "interaction": {**self.interaction, "current_action": s.current_action, "last_action": s.last_action,
                                "stale": bool(self.sampler) and age > STALE_AFTER_S,
                                "ambiguous": any(t["state"] != "visible" for t in targets)},
                "capabilities": self.caps.snapshot(),
                "events": ev["events"], "last_seq": ev["last_seq"], "events_dropped": ev["dropped"],
            }
        return snap


_RUNTIME: Optional[ComputerRuntime] = None
_RT_LOCK = threading.Lock()


def get_runtime() -> ComputerRuntime:
    global _RUNTIME
    with _RT_LOCK:
        if _RUNTIME is None:
            _RUNTIME = ComputerRuntime()
        return _RUNTIME


def set_runtime(rt: Optional[ComputerRuntime]) -> None:
    global _RUNTIME
    with _RT_LOCK:
        _RUNTIME = rt
