"""XeroSpatial engine: OBSERVE -> TARGET -> SPATIAL RESOLUTION -> ACT -> VERIFY.

Runs synchronously on the hub's single input thread (mouse_runtime._EXEC),
so resolution + firing + proof can never interleave with another click.
All platform access goes through `backends` (see backends.py); tests inject
deterministic fakes.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import protocol
from .displays import Display, display_for_rect, point_on_any_display
from .frame import Element, SpatialFrame, VisionCache, sig_diff
from .geometry import (LOCAL_MAX, FrameTransform, Grid, Rect, address_for_point,
                       local_to_point, resolve_address)
from .locks import LOCK_PIXEL_TOLERANCE, LockStore, TargetLock
from .memory import SpatialMemory
from .protocol import Action, TargetSpec
from .resolver import SOURCE_TRUST, Candidate, ambiguity, match_score, rank
from .safe_point import hazard_zones, safe_point
from .telemetry import TELEMETRY, Stopwatch, Telemetry
from .verify import CHANGE_THRESHOLD, evidence, poll_until
from ..runtime.coords import describe_space

GHOST_CTYPES = ("button", "menuitem", "hyperlink", "tabitem", "splitbutton")
NO_RETRY_CTYPES = ("checkbox", "radiobutton", "toggle", "combobox", "edit", "slider")
OK_STATUSES = ("hit", "fired_unverified", "ok")
DEFAULT_MIN_CONFIDENCE = 0.6
STALE_REGION_TOLERANCE = 0.05


class SpatialError(Exception):
    def __init__(self, status: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


@dataclass
class Resolution:
    ok: bool
    source: str = ""
    rect: Optional[Rect] = None          # target box (if known)
    point: Optional[Tuple[int, int]] = None
    text: str = ""
    ctype: str = ""
    invoke: bool = False
    confidence: float = 0.0
    candidates: List[Candidate] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    refined: bool = False
    status: str = ""
    error: str = ""
    region_sig: Optional[bytes] = None


class SpatialEngine:
    def __init__(self, backends, cache: Optional[VisionCache] = None, locks: Optional[LockStore] = None,
                 memory: Optional[SpatialMemory] = None, telemetry: Optional[Telemetry] = None,
                 cols: int = 16, rows: int = 8, runtime=None) -> None:
        self.b = backends
        self.runtime = runtime
        self.cache = cache or VisionCache()
        self.locks = locks or LockStore()
        self.memory = memory or SpatialMemory()
        self.telemetry = telemetry or TELEMETRY
        self.cols, self.rows = cols, rows
        # cancel() stamps a time; only actions SUBMITTED before that stamp are
        # cancelled (a stale cancel must never kill a later, unrelated action).
        self._cancel_ts = -1.0
        self._submitted = threading.local()

    def _rt(self, method: str, *a: Any, **k: Any) -> None:
        """Best-effort call into the shared ComputerRuntime (never breaks an action)."""
        rt = self.runtime
        if rt is None:
            return
        try:
            getattr(rt, method)(*a, **k)
        except Exception:
            pass

    def invalidate_all(self, reason: str) -> None:
        self.cache.invalidate(reason)
        self.locks.clear()

    # ==========================================================================
    # cancellation
    # ==========================================================================
    def cancel(self) -> None:
        self._cancel_ts = time.monotonic()

    def _is_cancelled(self) -> bool:
        sub = getattr(self._submitted, "t", None)
        return sub is not None and self._cancel_ts >= sub

    def _check_cancel(self) -> None:
        if self._is_cancelled():
            raise SpatialError("cancelled", "cancelled by request before firing")

    # ==========================================================================
    # context + surface
    # ==========================================================================
    def _context(self, window: str) -> Dict[str, Any]:
        info = self.b.focus(window) if window else self.b.foreground()
        if not info or not info.get("ok", True):
            raise SpatialError("focus_failed", (info or {}).get("error") or f"could not focus {window!r}")
        displays = self.b.displays()
        rect = info.get("rect")
        disp = display_for_rect(displays, Rect.from_any(rect)) if rect else \
            next((d for d in displays if d.primary), displays[0])
        info = dict(info)
        info["display_obj"] = disp
        info["displays"] = displays
        return info

    def _surface(self, ctx: Dict[str, Any], scope: str, display: Optional[int]) -> Tuple[Rect, Display]:
        displays: List[Display] = ctx["displays"]
        if display is not None:
            if not 0 <= int(display) < len(displays):
                raise SpatialError("bad_request", f"display {display} not found (have {len(displays)})")
            disp = displays[int(display)]
        else:
            disp = ctx["display_obj"]
        if scope == "window" and ctx.get("rect"):
            inter = Rect.from_any(ctx["rect"]).intersect(disp.bounds)
            if inter and inter.w >= self.cols and inter.h >= self.rows:
                return inter, disp
        return disp.bounds, disp

    def grid_for(self, ctx: Dict[str, Any], scope: str = "screen", display: Optional[int] = None) -> Grid:
        surface, _ = self._surface(ctx, scope, display)
        return Grid(surface, self.cols, self.rows)

    # ==========================================================================
    # OBSERVE
    # ==========================================================================
    def observe(self, ctx: Dict[str, Any], scope: str = "screen", display: Optional[int] = None,
                ocr: bool = True, force: bool = False, sw: Optional[Stopwatch] = None) -> SpatialFrame:
        sw = sw or Stopwatch()
        surface, disp = self._surface(ctx, scope, display)
        if not force:
            f = self.cache.get(ctx.get("hwnd"), ctx.get("rect"))
            if f is not None and f.surface == surface and (f.ocr_ok or not ocr):
                return f
        with sw.span("capture"):
            img = self.b.capture(surface)
        if img is None:
            raise SpatialError("vision_unavailable", "screen capture failed")
        tr = FrameTransform(surface.x, surface.y, surface.w / img.width, surface.h / img.height)
        frame = SpatialFrame(surface=surface, grid=Grid(surface, self.cols, self.rows), transform=tr,
                             image=img, window={k: ctx.get(k) for k in ("hwnd", "title", "rect", "exe")},
                             display=disp.to_dict())
        if ocr:
            region = self._ocr_focus_region(ctx, surface)
            with sw.span("ocr"):
                els, ok, engine = self._ocr_region(frame, region)
            frame.elements, frame.ocr_ok, frame.ocr_engine = els, ok, engine
        self.cache.put(frame)
        self._rt("set_space", describe_space(ctx["displays"], self.cols, self.rows))
        self._rt("ingest_frame", frame)
        return frame

    def _ocr_focus_region(self, ctx: Dict[str, Any], surface: Rect) -> Rect:
        """OCR only the target window when we know it (fewer pixels, fewer
        false positives from other apps); the whole surface otherwise."""
        if ctx.get("rect"):
            inter = Rect.from_any(ctx["rect"]).intersect(surface)
            if inter and inter.area > 0:
                return inter
        return surface

    def _ocr_region(self, frame: SpatialFrame, region: Rect, upscale: int = 1) -> Tuple[List[Element], bool, str]:
        if frame.image is None:
            return [], False, "none"
        ix0, iy0 = frame.transform.to_image(region.x, region.y)
        ix1, iy1 = frame.transform.to_image(region.right, region.bottom)
        ix0, iy0 = max(0, int(ix0)), max(0, int(iy0))
        ix1, iy1 = min(frame.image.width, int(ix1)), min(frame.image.height, int(iy1))
        if ix1 - ix0 < 4 or iy1 - iy0 < 4:
            return [], False, "none"
        crop = frame.image.crop((ix0, iy0, ix1, iy1))
        if upscale > 1:
            from PIL import Image
            crop = crop.resize((crop.width * upscale, crop.height * upscale), Image.Resampling.LANCZOS)
        res = self.b.ocr(crop) or {}
        if not res.get("ok"):
            return [], False, str(res.get("engine", "none"))
        sub = FrameTransform(0, 0, 1.0 / upscale, 1.0 / upscale)
        els = []
        for ln in res.get("lines", []):
            ix, iy = sub.to_physical(ln["x"], ln["y"])
            iw, ih = max(1, int(round(ln["w"] / upscale))), max(1, int(round(ln["h"] / upscale)))
            r = frame.transform.rect_to_physical(Rect(ix0 + ix, iy0 + iy, iw, ih))
            els.append(Element(text=str(ln.get("text", "")), rect=r, source="ocr"))
        return els, True, str(res.get("engine", ""))

    def _region_sig_now(self, rect: Rect) -> Optional[bytes]:
        from .frame import signature
        img = self.b.capture(rect)
        return signature(img, (16, 8)) if img is not None else None

    def _region_unchanged(self, frame: SpatialFrame, rect: Rect, tol: float = STALE_REGION_TOLERANCE) -> Tuple[bool, float]:
        """Has the region changed since `frame` was captured? (cheap crop)"""
        old = frame.region_sig(rect)
        new = self._region_sig_now(rect)
        if old is None or new is None:
            return False, 1.0
        d = sig_diff(old, new)
        return d <= tol, d

    # ==========================================================================
    # TARGET RESOLUTION
    # ==========================================================================
    def resolve(self, spec: TargetSpec, ctx: Dict[str, Any], scope: str = "screen",
                frame_id: str = "", sw: Optional[Stopwatch] = None,
                min_confidence: float = DEFAULT_MIN_CONFIDENCE) -> Resolution:
        sw = sw or Stopwatch()
        grid = self.grid_for(ctx, scope, spec.display)
        if spec.cell:
            return self._resolve_cell(spec, ctx, grid, frame_id, sw, min_confidence, scope)
        if spec.label:
            return self._resolve_label(spec.label, ctx, grid, sw, near=spec.near,
                                       min_confidence=min_confidence, scope=scope, display=spec.display)
        return Resolution(False, status="bad_request", error="target needs a label or a cell")

    # ---- spatial address ------------------------------------------------------
    def _resolve_cell(self, spec: TargetSpec, ctx, grid: Grid, frame_id: str, sw: Stopwatch,
                      min_confidence: float, scope: str = "screen") -> Resolution:
        try:
            addr = resolve_address(grid, spec.cell)
        except ValueError as e:
            return Resolution(False, status="bad_request", error=str(e))
        explicit = spec.x is not None and spec.y is not None
        lx = spec.x if explicit else LOCAL_MAX / 2
        ly = spec.y if explicit else LOCAL_MAX / 2
        pt = local_to_point(addr.region, lx, ly)
        notes = [f"address {addr.text} -> region {addr.region.to_dict()}"]

        # Which observation did the agent reason about?
        frame = self.cache.get_by_id(frame_id) if frame_id else self.cache.get(ctx.get("hwnd"), ctx.get("rect"))
        if frame_id and frame is None:
            return Resolution(False, status="stale_frame",
                              error=f"frame {frame_id} expired or unknown - observe again")
        if frame is not None and frame.surface != grid.bounds:
            if frame_id:
                return Resolution(False, status="stale_frame",
                                  error="the screen surface changed since that frame (resolution/DPI/window)")
            frame = None
        if frame is not None:
            with sw.span("revalidate"):
                same, d = self._region_unchanged(frame, addr.region)
            if not same:
                if frame_id:
                    return Resolution(False, status="stale_frame",
                                      error=f"cell {addr.text} changed since frame {frame_id} (diff {d:.3f})")
                notes.append(f"cached frame stale for {addr.text} (diff {d:.3f}); not snapping")
                frame = None

        # Label inside the cell -> semantic resolution restricted to it
        if spec.label:
            within = addr.region.inflate(max(4, addr.region.w // 4), max(4, addr.region.h // 4))
            res = self._resolve_label(spec.label, ctx, grid, sw, near=spec.near, within=within,
                                      min_confidence=min_confidence, scope=scope, display=spec.display)
            if res.ok:
                res.notes = notes + res.notes
                return res
            if not explicit:
                res.notes = notes + res.notes
                return res
            return Resolution(True, source="cell", point=pt, confidence=0.45,
                              notes=notes + [f"label {spec.label!r} NOT confirmed in {addr.text}"],
                              candidates=res.candidates)

        # Snap to an observed element under / next to the requested point
        if frame is not None and frame.elements:
            near_els = [e for e in frame.elements if e.rect.inflate(4).contains(*pt)]
            if near_els:
                el = min(near_els, key=lambda e: e.rect.area)
                notes.append(f"snapped to observed element {el.text[:24]!r}")
                return Resolution(True, source="cell", rect=el.rect, point=pt if el.rect.contains(*pt) else None,
                                  text=el.text, confidence=0.85, notes=notes)
        conf = SOURCE_TRUST["cell"] if explicit else 0.62
        if not explicit:
            notes.append("no local coordinate given - using cell centre")
        return Resolution(True, source="cell", point=pt, confidence=conf, notes=notes)

    # ---- semantic label -------------------------------------------------------
    def _resolve_label(self, label: str, ctx, grid: Grid, sw: Stopwatch, near: str = "",
                       within: Optional[Rect] = None,
                       min_confidence: float = DEFAULT_MIN_CONFIDENCE, scope: str = "screen",
                       display: Optional[int] = None) -> Resolution:
        notes: List[str] = []
        exclusions = [z for d in ctx["displays"] for z in d.taskbar_rects()]
        hwnd, wrect = ctx.get("hwnd"), ctx.get("rect")

        # 1) TARGET LOCK (validated by pixels, ~1 tiny capture)
        with sw.span("resolve.lock"):
            lk = self.locks.get(label, hwnd, wrect)
            if lk is not None and (within is None or within.contains(*lk.rect.center)):
                v = self.locks.revalidate(lk, self._region_sig_now(lk.rect))
                if v.get("valid"):
                    lk.uses += 1
                    return Resolution(True, source="lock", rect=lk.rect, text=lk.label, ctype=lk.ctype,
                                      invoke=lk.invoke, confidence=min(lk.confidence, SOURCE_TRUST["lock"]),
                                      notes=[f"target lock reused (pixel diff {v.get('diff')})"])
                notes.append(f"lock dropped: {v}")

        # 2) UI Automation - real control bounds
        uia_ranked: List[Candidate] = []
        with sw.span("resolve.uia"):
            raw = self.b.uia_find(label, ctx.get("title", ""), hwnd)
        if raw is None:
            notes.append("uia unavailable")
        else:
            cands = []
            for c in raw:
                r = Rect(int(c["x"]), int(c["y"]), int(c["w"]), int(c["h"]))
                if r.w <= 0 or r.h <= 0 or not point_on_any_display(ctx["displays"], *r.center):
                    continue
                cands.append(Candidate(text=c.get("name", ""), rect=r, source="uia", ctype=c.get("ctype", ""),
                                       invoke=bool(c.get("invoke") or c.get("toggle") or c.get("legacy")),
                                       enabled=c.get("enabled", True) is not False))
            near_pt = self._near_point(near, ctx, grid) if near else None
            uia_ranked = [c for c in rank(cands, label, near_pt, within, exclusions) if c.score >= 0.55]
        if uia_ranked:
            amb = None if near else ambiguity(uia_ranked)
            if amb:
                return Resolution(False, status="ambiguous", candidates=amb,
                                  error="several controls match; pass near= or a cell (e.g. IN G4)", notes=notes)
            best = uia_ranked[0]
            conf = SOURCE_TRUST["uia"] * min(1.0, best.score)
            if conf >= min_confidence:
                return Resolution(True, source="uia", rect=best.rect, text=best.text, ctype=best.ctype,
                                  invoke=best.invoke, confidence=conf, notes=notes, candidates=uia_ranked[:3])
            notes.append(f"uia best {best.text!r} too weak ({conf:.2f})")

        # 3) cached observation (see() already paid for it) - region revalidated
        frame = self.cache.get(hwnd, wrect)
        tried_cached = False
        if frame is not None and frame.ocr_ok and frame.surface == grid.bounds:
            tried_cached = True
            res = self._match_elements(frame, label, near, within, exclusions, ctx, grid, sw, notes, cached=True)
            if res is not None and (res.ok or res.status == "ambiguous"):
                return res

        # 4) fresh observation: ONE capture, OCR prioritised regions first
        try:
            frame = self.observe(ctx, scope=scope, display=display, ocr=False, force=True, sw=sw)
        except SpatialError as e:
            return Resolution(False, status=e.status, error=e.message, notes=notes)
        regions: List[Tuple[str, Rect]] = []
        if within is not None:
            regions.append(("cell", within))
        for p in self.memory.priors(ctx.get("exe", ""), label, wrect)[:2]:
            regions.append((f"prior:{p['source']}", p["rect"]))  # type: ignore[arg-type]
        full = self._ocr_focus_region(ctx, frame.surface)
        if within is None:
            regions.append(("window", full))
        ocr_any = False
        for name, reg in regions:
            reg = reg.intersect(frame.surface)
            if reg is None:
                continue
            with sw.span("resolve.ocr"):
                els, ok, engine = self._ocr_region(frame, reg)
            ocr_any = ocr_any or ok
            if name == "window" and ok:
                frame.elements, frame.ocr_ok, frame.ocr_engine = els, True, engine
                self.cache.put(frame)
            if not ok:
                continue
            tmp = SpatialFrame(surface=frame.surface, grid=frame.grid, transform=frame.transform,
                               image=frame.image, elements=els, window=frame.window, sig=frame.sig)
            tmp.frame_id, tmp.ts = frame.frame_id, frame.ts
            res = self._match_elements(tmp, label, near, within, exclusions, ctx, grid, sw, notes, cached=False)
            if res is not None and (res.ok or res.status == "ambiguous"):
                res.notes.append(f"found via OCR region '{name}'")
                return res
        if not ocr_any:
            notes.append("ocr unavailable")
        return Resolution(False, status="not_found",
                          error=f"{label!r} not found (uia={'n/a' if raw is None else len(raw)}, "
                                f"ocr={'ok' if ocr_any else 'n/a'}{', cached' if tried_cached else ''})",
                          notes=notes)

    def _near_point(self, near: str, ctx, grid: Grid) -> Optional[Tuple[float, float]]:
        frame = self.cache.get(ctx.get("hwnd"), ctx.get("rect"))
        if frame is None:
            return None
        best = max(frame.elements, key=lambda e: match_score(near, e.text), default=None)
        if best is not None and match_score(near, best.text) >= 0.55:
            return best.center
        return None

    def _match_elements(self, frame: SpatialFrame, label: str, near: str, within: Optional[Rect],
                        exclusions, ctx, grid: Grid, sw: Stopwatch, notes: List[str],
                        cached: bool) -> Optional[Resolution]:
        near_pt = None
        if near:
            nb = max(frame.elements, key=lambda e: match_score(near, e.text), default=None)
            if nb is not None and match_score(near, nb.text) >= 0.55:
                near_pt = nb.center
        cands = [Candidate(text=e.text, rect=e.rect, source="ocr") for e in frame.elements]
        ranked = [c for c in rank(cands, label, near_pt, within, exclusions) if c.score >= 0.45]
        if not ranked:
            return None
        best = ranked[0]
        if cached:
            with sw.span("revalidate"):
                same, d = self._region_unchanged(frame, best.rect.inflate(2))
            if not same:
                notes.append(f"cached frame stale at target (diff {d:.3f})")
                self.cache.invalidate("target_region_changed")
                return None
        amb = None if (near_pt or within) else ambiguity(ranked)
        if amb:
            return Resolution(False, status="ambiguous", candidates=amb,
                              error="several texts match; pass near= or a cell (e.g. IN G4)", notes=notes)
        conf = SOURCE_TRUST["ocr"] * min(1.0, best.score + 0.1)
        res = Resolution(True, source="ocr", rect=best.rect, text=best.text, confidence=conf,
                         notes=list(notes) + (["from cached frame"] if cached else []),
                         candidates=ranked[:3])
        # ---- adaptive refinement: only when the coarse answer is weak ----
        scale = float(frame.display.get("scale", 1.0) or 1.0)
        small = min(best.rect.w, best.rect.h) < 14 * scale
        fuzzy = best.score < 0.8
        crowded = len([c for c in ranked[1:] if c.rect.inflate(30).intersect(best.rect)]) > 0
        if (small or fuzzy or crowded) and frame.image is not None:
            with sw.span("resolve.refine"):
                self._refine(frame, label, res, ranked)
        return res

    def _refine(self, frame: SpatialFrame, label: str, res: Resolution, ranked: List[Candidate]) -> None:
        """LEVEL 3: re-OCR a tight upscaled crop around the candidate."""
        assert res.rect is not None
        r = res.rect
        reg = Rect(r.x - max(r.w, 40), r.y - max(r.h, 16), r.w + 2 * max(r.w, 40), r.h + 2 * max(r.h, 16))
        reg = reg.intersect(frame.surface) or r
        els, ok, _ = self._ocr_region(frame, reg, upscale=2)
        if not ok or not els:
            res.notes.append("refinement: no extra detail")
            return
        best = max(els, key=lambda e: match_score(label, e.text))
        s = match_score(label, best.text)
        if s >= max(0.45, ranked[0].score) and best.rect.intersect(r.inflate(max(r.w, r.h))):
            res.rect, res.text, res.source, res.refined = best.rect, best.text, "ocr_refined", True
            res.confidence = SOURCE_TRUST["ocr_refined"] * min(1.0, s + 0.1)
            res.notes.append("refined at 2x (level 3)")
        else:
            res.notes.append("refinement did not improve the match")

    # ==========================================================================
    # ACT + VERIFY
    # ==========================================================================
    def act(self, action: Action, window: str = "", scope: str = "screen", until: str = "",
            min_confidence: float = DEFAULT_MIN_CONFIDENCE, motion: str = "sniper",
            timeout_ms: int = 2500, frame_id: str = "", retries: int = 1,
            dry_run: bool = False, debug: bool = False,
            submitted_at: Optional[float] = None) -> Dict[str, Any]:
        self._submitted.t = submitted_at if submitted_at is not None else time.monotonic()
        sw = Stopwatch()
        deadline = time.monotonic() + max(0.4, timeout_ms / 1000.0)
        until = until or action.until
        out: Dict[str, Any] = {"action": action.verb}
        if action.verb not in ("observe", "wait"):
            self._rt("action_started", {"type": action.verb, "target": (action.target.label or action.target.cell) if action.target else "",
                                        "until": until, "text": action.text})
        try:
            self._check_cancel()
            with sw.span("context"):
                ctx = self._context(window)
            out["window"] = ctx.get("title", "")
            if action.verb == "observe":
                return self._finish(out, sw, self._observe_result(ctx, scope, sw, debug), "observe")
            if action.verb == "wait":
                self.b.sleep(action.ms / 1000.0)
                out.update(status="ok", waited_ms=action.ms)
                return self._finish(out, sw, {}, "wait")
            if action.verb in ("type", "key"):
                return self._finish(out, sw, self._keyboard(action, ctx, until, deadline, sw), action.verb)

            # ---- pointer actions -------------------------------------------
            spec = action.target
            if spec.empty and action.verb == "scroll":
                cur = self.b.cursor_pos()
                spec = TargetSpec()
                res = Resolution(True, source="cursor", point=cur, confidence=1.0)
            else:
                if spec.empty:
                    raise SpatialError("bad_request", f"{action.verb} needs a target (cell or \"label\")")
                res = self.resolve(spec, ctx, scope, frame_id, sw, min_confidence)
            grid = self.grid_for(ctx, scope, spec.display)
            if not res.ok:
                out.update(status=res.status or "not_found", error=res.error, notes=res.notes,
                           candidates=[c.to_brief(grid) for c in res.candidates[:5]])
                out["hint"] = self._hint(res.status)
                return self._finish(out, sw, {}, action.verb, debug=debug, ctx=ctx)

            with sw.span("safe_point"):
                pt, quality, sp_notes = self._aim(res, ctx)
            conf = res.confidence * (0.75 + 0.25 * quality) if res.rect is not None else res.confidence * quality
            out["resolved"] = self._describe_resolution(res, grid, pt, conf, sp_notes)
            if conf < min_confidence:
                out.update(status="refused_low_confidence",
                           error=f"confidence {conf:.2f} < {min_confidence:.2f}; not clicking",
                           hint="observe again, pass a cell/near anchor, or lower min_confidence deliberately")
                return self._finish(out, sw, {}, action.verb, debug=debug, ctx=ctx, res=res, pt=pt)

            pt2 = None
            if action.verb == "drag":
                if action.target2 is None or action.target2.empty:
                    raise SpatialError("bad_request", "drag needs a second target")
                res2 = self.resolve(action.target2, ctx, scope, frame_id, sw, min_confidence)
                if not res2.ok:
                    out.update(status="drag_end_" + (res2.status or "not_found"), error=res2.error)
                    return self._finish(out, sw, {}, action.verb)
                pt2, _, _ = self._aim(res2, ctx)
                out["resolved_end"] = self._describe_resolution(res2, grid, pt2, res2.confidence, [])

            if dry_run:
                out.update(status="dry_run", point={"x": pt[0], "y": pt[1]})
                return self._finish(out, sw, {}, action.verb, debug=debug, ctx=ctx, res=res, pt=pt)

            self._check_cancel()
            if time.monotonic() > deadline:
                raise SpatialError("timeout", "budget exhausted before firing (nothing clicked)")
            fired = self._fire(action, res, ctx, pt, pt2, until, motion, deadline, retries, sw, spec)
            out.update(fired)
            return self._finish(out, sw, {}, action.verb, debug=debug, ctx=ctx, res=res, pt=pt)
        except SpatialError as e:
            out.update(status=e.status, error=e.message, **e.extra)
            out["hint"] = self._hint(e.status)
            return self._finish(out, sw, {}, action.verb)

    # ---- aiming -----------------------------------------------------------------
    def _aim(self, res: Resolution, ctx) -> Tuple[Tuple[int, int], float, List[str]]:
        disp: Display = ctx["display_obj"]
        hz = hazard_zones(ctx.get("rect"), disp.scale, bool(ctx.get("maximized")))
        if res.rect is not None:
            frame = self.cache.peek()
            obstacles = [e.rect for e in (frame.elements if frame else [])
                         if e.rect != res.rect and e.rect.intersect(res.rect)]
            sp = safe_point(res.rect, obstacles, hz, preferred=res.point)
            return (sp.x, sp.y), sp.quality, sp.reasons
        assert res.point is not None
        px, py = res.point
        notes = [k for k, z in hz if z.contains(px, py)]
        quality = 0.85 if notes else 1.0
        return (int(px), int(py)), quality, notes

    def _describe_resolution(self, res: Resolution, grid: Grid, pt, conf, sp_notes) -> Dict[str, Any]:
        d: Dict[str, Any] = {"source": res.source, "confidence": round(conf, 3)}
        if res.text:
            d["text"] = res.text[:60]
        if res.rect is not None:
            d["box"] = res.rect.to_dict()
        a1 = address_for_point(grid, pt[0], pt[1], levels=1)
        a2 = address_for_point(grid, pt[0], pt[1], levels=2)
        if a1:
            d["cell"], d["local"] = a1[0], [a1[1], a1[2]]
        if a2:
            d["address"] = a2[0]
        d["point"] = {"x": pt[0], "y": pt[1]}
        if res.refined:
            d["refined"] = True
        if sp_notes:
            d["safe_point_notes"] = sp_notes
        if res.notes:
            d["notes"] = res.notes[-6:]
        return d

    # ---- firing -----------------------------------------------------------------
    def _fire(self, action: Action, res: Resolution, ctx, pt, pt2, until: str, motion: str,
              deadline: float, retries: int, sw: Stopwatch, spec: TargetSpec) -> Dict[str, Any]:
        verb = action.verb
        # occlusion: a foreign window over the point would receive the click
        with sw.span("act.pre"):
            if not point_on_any_display(ctx["displays"], *pt):
                raise SpatialError("offscreen", f"point {pt} is not on any display")
            u_hwnd, _u_title, u_pid = self.b.window_under_point(*pt)
            if ctx.get("hwnd") and u_hwnd and u_hwnd != ctx["hwnd"] and u_pid and u_pid != ctx.get("pid"):
                raise SpatialError("occluded", f"another window ({_u_title!r}) covers the target point",
                                   point={"x": pt[0], "y": pt[1]})
            probe = Rect(pt[0] - 24, pt[1] - 24, 48, 48)
            before_sig = self._region_sig_now(probe)
            fg_before = self.b.foreground().get("hwnd")

        motion_used = ""
        out_traj = None
        with sw.span("act.fire"):
            ghost_ok = (verb == "click" and motion != "human" and res.invoke and res.source in ("uia", "lock")
                        and any(g in (res.ctype or "").lower() for g in GHOST_CTYPES))
            r = None
            if ghost_ok:
                r = self.b.uia_invoke(res.text, ctx.get("title", ""), ctx.get("hwnd"), res.rect)
                if r and r.get("ok"):
                    motion_used = f"ghost:{r.get('how', 'invoke')}"
            if not motion_used and motion == "trajectory":
                traj = self._trajectory(ctx, res, pt, probe, before_sig, fg_before)
                if traj["status"] != "reached":
                    raise SpatialError("interrupted", f"trajectory interrupted: {traj.get('reason')} (nothing clicked)",
                                       reason=traj.get("reason"), trajectory=traj, point={"x": pt[0], "y": pt[1]})
                out_traj = {k: traj.get(k) for k in ("steps_done", "steps_planned", "retargets")}
                motion = "sniper"        # the final press still goes through the proven pointer path
            if not motion_used:
                r = self.b.pointer(verb, pt[0], pt[1],
                                   x2=pt2[0] if pt2 else None, y2=pt2[1] if pt2 else None,
                                   amount=action.amount, motion=motion)
                if not r or not r.get("ok"):
                    raise SpatialError((r or {}).get("status", "input_failed"),
                                       (r or {}).get("error", "pointer injection failed"),
                                       point={"x": pt[0], "y": pt[1]})
                motion_used = r.get("motion", "warp")

        proof: Dict[str, Any] = {"until_ok": None, "how": "not_requested"}
        tries = 1
        with sw.span("verify"):
            if until:
                check = lambda: self.b.check_until(until, ctx, deadline)
                can_retry = retries > 0 and verb == "click" and \
                    not any(t in (res.ctype or "").lower() for t in NO_RETRY_CTYPES)
                now = time.monotonic()
                first_deadline = max(deadline, now + 0.6)
                if can_retry and deadline - now > 1.2:
                    # keep budget for ONE retry, but give the UI >= 0.6 s (and
                    # half the budget) to react before judging it "untouched"
                    first_deadline = now + max(0.6, (deadline - now) * 0.5)
                proof = poll_until(check, first_deadline, sleep=self.b.sleep,
                                   is_cancelled=self._is_cancelled)
                # ONE safe retry: only if nothing at all happened (click swallowed)
                if not proof.get("until_ok") and can_retry:
                    after = self._region_sig_now(probe)
                    fg_now = self.b.foreground().get("hwnd")
                    untouched = (sig_diff(before_sig, after) < CHANGE_THRESHOLD) and fg_now == fg_before
                    if untouched and time.monotonic() < deadline - 0.3:
                        r2 = self.b.pointer("click", pt[0], pt[1], motion="sniper")
                        tries += 1
                        if r2 and r2.get("ok"):
                            proof = poll_until(check, max(deadline, time.monotonic() + 0.6),
                                               sleep=self.b.sleep, is_cancelled=self._is_cancelled)
                            proof["retried"] = True
            after_sig = self._region_sig_now(probe)
            fg_after = self.b.foreground().get("hwnd")
            cursor = self.b.cursor_pos()
            ev = evidence(before_sig, after_sig, fg_before, fg_after, cursor,
                          pt2 if verb == "drag" else pt)

        # ---- status: success ONLY with a satisfied postcondition ----
        if until:
            status = "hit" if proof.get("until_ok") else "fired_unverified"
        elif verb in ("move", "hover"):
            status = "hit" if ev.get("cursor_on_target") else "fired_unverified"
            proof = {"until_ok": bool(ev.get("cursor_on_target")), "how": "cursor_position"}
        else:
            status = "fired_unverified"
            proof = {"until_ok": None, "how": "not_requested",
                     "note": "no postcondition given; see evidence.changed"}

        # ---- learning / invalidation ----
        label = spec.label or res.text
        if status == "hit" and label and res.rect is not None:
            self.locks.put(TargetLock(label=label, rect=res.rect, cell=self.grid_for(ctx).cell_at(*res.rect.center) or "",
                                      confidence=res.confidence, source=res.source,
                                      frame_id=(self.cache.peek().frame_id if self.cache.peek() else ""),
                                      hwnd=ctx.get("hwnd"), win_rect=ctx.get("rect"),
                                      region_sig=self._region_sig_now(res.rect), ctype=res.ctype, invoke=res.invoke))
            self.memory.learn(ctx.get("exe", ""), label, res.rect, ctx.get("rect"), res.ctype, verified=True)
        elif until and status != "hit":
            if label:
                self.locks.invalidate(label, "verification_failed")
            self.cache.invalidate("verification_failed")
        elif ev.get("changed"):
            # the UI reacted; cached pixels elsewhere may now be wrong
            if label:
                self.locks.invalidate(label, "screen_changed")
            self.cache.invalidate("screen_changed")
        return {"status": status, "point": {"x": pt[0], "y": pt[1]}, "motion": motion_used,
                **({"trajectory": out_traj} if out_traj else {}), "tries": tries, "proof": proof, "evidence": ev,
                **({"to": {"x": pt2[0], "y": pt2[1]}} if pt2 else {})}

    def _trajectory(self, ctx, res: Resolution, pt, probe: Rect, before_sig, fg_before) -> Dict[str, Any]:
        """Walk the cursor to `pt`; between waypoints check cancel, focus, the target
        region and (when tracked) target movement. Injection = the same pointer()
        backend on the same input thread; nothing else moves the mouse."""
        from ..runtime.trajectory import plan_path, run_trajectory
        start = self.b.cursor_pos()
        path = plan_path(start, pt)
        hwnd0 = fg_before

        def check(i, p):
            if self._is_cancelled():
                return "cancelled"
            if self.b.foreground().get("hwnd") != hwnd0:
                return "focus_lost"
            if i and i % 2 == 0:
                cur = self._region_sig_now(probe)
                if before_sig is not None and cur is not None and sig_diff(before_sig, cur) > STALE_REGION_TOLERANCE * 2:
                    return "target_region_changed"
            return None

        def retarget():
            if res.rect is None or self.runtime is None:
                return None
            t = self.runtime.tracker.find_label(res.text or "")
            if t and t.state == "visible" and t.last_seen_frame and abs(t.velocity[0]) + abs(t.velocity[1]) > 20:
                r = Rect(*t.bbox)
                return r.center
            return None
        return run_trajectory(path, lambda p: self.b.pointer("move", p[0], p[1], motion="warp"),
                              check_fn=check, retarget_fn=retarget)

    def _keyboard(self, action: Action, ctx, until: str, deadline: float, sw: Stopwatch) -> Dict[str, Any]:
        fg = self.b.foreground()
        if ctx.get("hwnd") and fg.get("hwnd") != ctx.get("hwnd"):
            raise SpatialError("focus_lost", "target window is not foreground; refusing to type")
        with sw.span("act.fire"):
            r = self.b.type_text(action.text) if action.verb == "type" else self.b.send_keys(action.keys)
        if not r or r.get("status") not in ("ok",):
            raise SpatialError("input_failed", (r or {}).get("error", "keyboard injection failed"))
        out: Dict[str, Any] = {"sent": r.get("sent")}
        if until:
            with sw.span("verify"):
                proof = poll_until(lambda: self.b.check_until(until, ctx, deadline),
                                   max(deadline, time.monotonic() + 0.6), sleep=self.b.sleep)
            out.update(status="hit" if proof.get("until_ok") else "fired_unverified", proof=proof)
        else:
            out.update(status="fired_unverified", proof={"until_ok": None, "how": "not_requested"})
        if self.b.foreground().get("hwnd") != fg.get("hwnd"):
            out["focus_changed_during_input"] = True
        return out

    # ---- observe result -----------------------------------------------------------
    def _observe_result(self, ctx, scope: str, sw: Stopwatch, debug: bool) -> Dict[str, Any]:
        frame = self.observe(ctx, scope=scope, force=True, sw=sw)
        out = {"status": "ok", "frame": frame.describe(), "cells": frame.cell_map()}
        if not frame.ocr_ok:
            out["ocr"] = "unavailable"
        if debug:
            try:
                from .debug_render import render_grid
                out["image"] = render_grid(frame)
            except Exception as e:
                out["debug_error"] = f"{type(e).__name__}: {e}"
        return out

    # ---- finish -------------------------------------------------------------------
    def _finish(self, out: Dict[str, Any], sw: Stopwatch, extra: Dict[str, Any], op: str,
                debug: bool = False, ctx=None, res: Optional[Resolution] = None, pt=None) -> Dict[str, Any]:
        out.update(extra)
        f = self.cache.peek()
        if f is not None:
            out.setdefault("frame", {"frame_id": f.frame_id, "age_ms": int(f.age * 1000)})
        if debug and ctx is not None:
            try:
                from .debug_render import render
                frame = f if (f is not None and f.image is not None) else self.observe(ctx, ocr=False, force=True)
                out["image"] = render(frame, res, pt, out, sw.report())
            except Exception as e:  # debug must never break the action
                out["debug_error"] = f"{type(e).__name__}: {e}"
        out["timing_ms"] = sw.report()
        self.telemetry.record(op, out["timing_ms"])
        if op not in ("observe", "wait"):
            self._rt("action_finished", out)
        return out

    @staticmethod
    def _hint(status: str) -> str:
        return {
            "ambiguous": "add near=\"<anchor text>\" or restrict with a cell (CLICK \"X\" IN G4)",
            "not_found": "call see()/observe, check spelling, or give a cell + local x,y",
            "stale_frame": "screen changed since you looked: observe again, then act on the new frame",
            "focus_failed": "window could not be brought to front; check the title substring",
            "occluded": "a popup/other window covers the point; close it or target it instead",
            "refused_low_confidence": "observe again or give a cell/near anchor",
            "vision_unavailable": "screen capture failed (locked screen / secure desktop?)",
            "interrupted": "the screen/focus changed while the cursor was moving; nothing was clicked - observe and retry",
            "input_blocked": "Windows blocked injected input (elevated target window? run the hub elevated)",
        }.get(status, "")

    # ==========================================================================
    # multi-action commands
    # ==========================================================================
    def run(self, command: str, **kw: Any) -> Dict[str, Any]:
        try:
            actions = protocol.parse(command)
        except protocol.ProtocolError as e:
            return {"status": "bad_request", "error": f"command syntax: {e}",
                    "hint": 'e.g. CLICK G4 32 48 | CLICK "Settings" UNTIL "Settings gone" | KEY CTRL+L'}
        if len(actions) == 1:
            return self.act(actions[0], **kw)
        steps = []
        t0 = time.perf_counter()
        kw.setdefault("submitted_at", time.monotonic())
        for a in actions:
            r = self.act(a, **kw)
            steps.append(r)
            if r.get("status") not in OK_STATUSES:
                break
        last = steps[-1]
        return {"status": last.get("status"), "steps": steps, "completed": sum(1 for s in steps if s.get("status") in OK_STATUSES),
                "total": len(actions), "timing_ms": {"total": round((time.perf_counter() - t0) * 1000, 2)}}
