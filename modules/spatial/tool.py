# ==============================================================================
# modules/spatial/tool.py - MCP-facing adapter for the `spatial_point` tool.
#
# Turns either a compact command string ("CLICK G4 32 48 UNTIL ...") or
# structured params (action/target/cell/x/y/...) into protocol.Action objects
# and runs them on the SpatialEngine. Pure Python; the MCP layer decides which
# thread this runs on (always the single input thread in production).
# ==============================================================================
from __future__ import annotations

import time
from typing import Any, Dict, Optional

from . import protocol
from .protocol import Action, TargetSpec

_ALIASES = {
    "click": "click", "left": "click", "tap": "click",
    "double": "double_click", "double_click": "double_click", "dblclick": "double_click",
    "right": "right_click", "right_click": "right_click", "context": "right_click",
    "move": "move", "hover": "hover", "drag": "drag", "scroll": "scroll",
    "type": "type", "key": "key", "keys": "key", "press": "key",
    "wait": "wait", "observe": "observe", "see": "observe", "grid": "observe",
}


def _check_address(addr: str, cols: int = 16, rows: int = 8) -> None:
    import re
    from .geometry import SUB_COLS, SUB_ROWS, parse_cell_id
    if not protocol._CELL.match(addr):
        raise protocol.ProtocolError(f"bad cell address {addr!r} (expected e.g. G4 or G4/B3)")
    parts = [p for p in re.split(r"[/.>]", addr.split(":")[-1]) if p]
    for i, p in enumerate(parts):
        c, r = parse_cell_id(p)
        mc, mr = (cols, rows) if i == 0 else (SUB_COLS, SUB_ROWS)
        if not (0 <= c < mc and 0 <= r < mr):
            last = f"{chr(64 + mc)}{mr}"
            raise protocol.ProtocolError(f"cell {p!r} outside the grid (A1..{last})")


def _spec(target: Any = None, cell: str = "", x: Optional[float] = None, y: Optional[float] = None,
          display: Optional[int] = None, near: str = "") -> TargetSpec:
    label = ""
    if isinstance(target, dict):
        cell = cell or str(target.get("cell", "") or "")
        label = str(target.get("text", "") or target.get("label", "") or "")
        x = target.get("x", x)
        y = target.get("y", y)
        near = near or str(target.get("near", "") or "")
        if target.get("display") is not None:
            display = target.get("display")
    elif isinstance(target, str) and target.strip():
        t = target.strip()
        # "G4", "1:G4", "G4/B3" given as target -> spatial address
        m = protocol._CELL.match(t)
        if m and not cell:
            if m.group(1):
                display = int(m.group(1))
            cell = m.group(2)
        else:
            label = t
    for v in (x, y):
        if v is not None and not (0 <= float(v) <= 64):
            raise protocol.ProtocolError(f"local coordinates must be 0..64 (got {v})")
    if (x is None) != (y is None):
        raise protocol.ProtocolError("give both x and y (local 0..64) or neither")
    if cell:
        _check_address(cell)
    return TargetSpec(label=label, cell=cell.upper() if cell else "",
                      x=None if x is None else float(x), y=None if y is None else float(y),
                      display=None if display is None else int(display), near=near)


def build_action(action: str, target: Any = None, cell: str = "", x=None, y=None,
                 target2: Any = None, to_cell: str = "", to_x=None, to_y=None,
                 near: str = "", text: str = "", keys: str = "", amount: int = 0,
                 until: str = "", display: Optional[int] = None, ms: int = 0) -> Action:
    verb = _ALIASES.get((action or "click").strip().lower())
    if not verb:
        raise protocol.ProtocolError(f"unknown action {action!r}; valid: {sorted(set(_ALIASES))} or use command=")
    a = Action(verb=verb, until=until or "", amount=int(amount or 0), text=text or "",
               keys=keys or "", ms=int(ms or 0))
    if verb in protocol.POINTER_VERBS:
        a.target = _spec(target, cell, x, y, display, near)
        if verb == "drag":
            if target2 is None and not to_cell:
                raise protocol.ProtocolError("drag needs target2 or to_cell (+to_x/to_y)")
            a.target2 = _spec(target2, to_cell, to_x, to_y, display, "")
    elif verb == "type" and not a.text:
        raise protocol.ProtocolError("type needs text")
    elif verb == "key" and not a.keys:
        raise protocol.ProtocolError("key needs keys, e.g. ctrl+l")
    elif verb == "wait" and a.ms <= 0:
        a.ms = max(1, int(amount or 250))
    return a


def stats(engine) -> Dict[str, Any]:
    out: Dict[str, Any] = {"status": "ok", "telemetry": engine.telemetry.summary()
                           if hasattr(engine.telemetry, "summary") else {}}
    for name, obj in (("locks", engine.locks), ("memory", engine.memory), ("cache", engine.cache)):
        fn = getattr(obj, "stats", None)
        if callable(fn):
            try:
                out[name] = fn()
            except Exception as e:  # stats must never fail the call
                out[name] = {"error": str(e)}
    return out


def run(engine, *, action: str = "click", command: str = "", submitted_at: Optional[float] = None,
        window: str = "", scope: str = "screen", until: str = "", min_confidence: float = 0.0,
        motion: str = "sniper", timeout_ms: int = 2500, frame_id: str = "", retries: int = 1,
        dry_run: bool = False, debug: bool = False, **spec_kw: Any) -> Dict[str, Any]:
    submitted_at = submitted_at if submitted_at is not None else time.monotonic()
    act = (action or "").strip().lower()
    if act == "stats":
        return stats(engine)
    kw: Dict[str, Any] = dict(window=window, scope=scope if scope in ("screen", "window") else "screen",
                              motion=motion, timeout_ms=int(timeout_ms), frame_id=frame_id,
                              retries=max(0, min(2, int(retries))), dry_run=bool(dry_run),
                              debug=bool(debug), submitted_at=submitted_at)
    if min_confidence:
        kw["min_confidence"] = float(min_confidence)
    if command and command.strip():
        if until:
            kw["until"] = until
        return engine.run(command, **kw)
    try:
        a = build_action(act or "click", until=until, **spec_kw)
    except protocol.ProtocolError as e:
        return {"status": "bad_request", "error": str(e)}
    return engine.act(a, **kw)
