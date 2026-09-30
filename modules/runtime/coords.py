"""ONE canonical physical coordinate space, shared by see(grid), spatial_point
and the runtime state. space_id changes iff monitors/DPI/grid change, so any
consumer can detect that a frame/lock/grid belongs to a different space."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional


def describe_space(displays: List[Any], cols: int = 16, rows: int = 8, local: int = 64) -> Dict[str, Any]:
    mons = []
    for i, d in enumerate(displays):
        mons.append({"id": getattr(d, "id", i), "primary": bool(getattr(d, "primary", False)),
                     "bounds": d.bounds.to_dict(), "work": d.work.to_dict(),
                     "dpi": d.dpi, "scale": round(d.scale, 3)})
    x0 = min(m["bounds"]["x"] for m in mons)
    y0 = min(m["bounds"]["y"] for m in mons)
    x1 = max(m["bounds"]["x"] + m["bounds"]["w"] for m in mons)
    y1 = max(m["bounds"]["y"] + m["bounds"]["h"] for m in mons)
    grid = {"cols": cols, "rows": rows, "local": local, "cells": cols * rows,
            "addressing": "A1..%s%d; cell 'G4' local 0..%d; display prefix '1:G4'" % (chr(64 + cols), rows, local)}
    sig = json.dumps([[m["bounds"], m["dpi"]] for m in mons] + [cols, rows], sort_keys=True)
    return {"space_id": hashlib.sha1(sig.encode()).hexdigest()[:10],
            "units": "physical_px", "virtual_desktop": {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0},
            "monitors": mons, "grid": grid}


def frame_meta(frame: Any, space: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Coordinate metadata for one frame: which surface, how the capture maps to
    physical pixels, which monitor, and the space it was defined in."""
    tr = frame.transform
    s = frame.surface
    disp = frame.display or {}
    img = frame.image
    meta = {"frame_id": frame.frame_id, "surface": s.to_dict(), "monitor": disp.get("id"),
            "dpi": disp.get("dpi"), "scale": disp.get("scale"),
            "capture": {"w": getattr(img, "width", None), "h": getattr(img, "height", None)},
            "capture_to_physical": {"sx": round(tr.scale_x, 4), "sy": round(tr.scale_y, 4), "ox": tr.origin_x, "oy": tr.origin_y},
            "grid": {"cols": frame.grid.cols, "rows": frame.grid.rows, "local": 64}}
    if space:
        meta["space_id"] = space["space_id"]
    if disp.get("scale"):
        meta["logical_surface"] = {"w": round(s.w / disp["scale"]), "h": round(s.h / disp["scale"])}
    return meta
