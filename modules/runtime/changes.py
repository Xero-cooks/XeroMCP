"""Screen-change intelligence: classify WHAT changed so the runtime knows how
much to distrust (keep lock / update tracking / OCR / UIA / deep / interrupt)."""
from __future__ import annotations

from collections import deque
from typing import Any, Deque, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

SIG_COLS, SIG_ROWS = 16 * 8, 8 * 8       # 8x8 signature pixels per grid cell
PIXEL_DELTA = 32
CELL_CHANGED_FRACTION = 0.04

POLICY: Dict[str, Dict[str, Any]] = {
    "none":          dict(keep_locks=True,  update_tracking=False, ocr=False, uia=False, deep=False, interrupt=False),
    "cursor_only":   dict(keep_locks=True,  update_tracking=False, ocr=False, uia=False, deep=False, interrupt=False),
    "animation":     dict(keep_locks=True,  update_tracking=False, ocr=False, uia=False, deep=False, interrupt=False),
    "small_region":  dict(keep_locks=True,  update_tracking=True,  ocr=True,  uia=False, deep=False, interrupt=False),
    "target_region": dict(keep_locks=False, update_tracking=True,  ocr=True,  uia=True,  deep=False, interrupt=True),
    "major_layout":  dict(keep_locks=False, update_tracking=True,  ocr=True,  uia=True,  deep=True,  interrupt=True),
    "window":        dict(keep_locks=False, update_tracking=True,  ocr=True,  uia=True,  deep=True,  interrupt=True),
    "navigation":    dict(keep_locks=False, update_tracking=True,  ocr=True,  uia=True,  deep=True,  interrupt=True),
}


def signature_gray(img) -> bytes:
    from PIL import Image
    return img.convert("L").resize((SIG_COLS, SIG_ROWS), Image.Resampling.BILINEAR).tobytes()


def cell_id(c: int, r: int) -> str:
    return f"{chr(65 + c)}{r + 1}"


def changed_cells(a: bytes, b: bytes) -> Dict[str, float]:
    """cell id -> fraction of strongly-changed signature pixels (only cells above threshold)."""
    out: Dict[str, float] = {}
    if not a or not b or len(a) != len(b) or len(a) != SIG_COLS * SIG_ROWS:
        return out
    for r in range(8):
        for c in range(16):
            n = ch = 0
            for j in range(8):
                base = (r * 8 + j) * SIG_COLS + c * 8
                for i in range(8):
                    n += 1
                    if abs(a[base + i] - b[base + i]) > PIXEL_DELTA:
                        ch += 1
            f = ch / n
            if f >= CELL_CHANGED_FRACTION:
                out[cell_id(c, r)] = round(f, 3)
    return out


def cells_for_point(x: float, y: float, bounds: Tuple[int, int, int, int], pad: int = 1) -> Set[str]:
    bx, by, bw, bh = bounds
    if bw <= 0 or bh <= 0:
        return set()
    c = int((x - bx) / bw * 16)
    r = int((y - by) / bh * 8)
    return {cell_id(cc, rr) for cc in range(c - pad, c + pad + 1) for rr in range(r - pad, r + pad + 1)
            if 0 <= cc < 16 and 0 <= rr < 8}


def cells_for_rect(rect: Tuple[int, int, int, int], bounds: Tuple[int, int, int, int]) -> Set[str]:
    bx, by, bw, bh = bounds
    if bw <= 0 or bh <= 0:
        return set()
    x0, y0 = (rect[0] - bx) / bw * 16, (rect[1] - by) / bh * 8
    x1, y1 = (rect[0] + rect[2] - bx) / bw * 16, (rect[1] + rect[3] - by) / bh * 8
    out = set()
    for c in range(max(0, int(x0)), min(15, int(x1 - 1e-9)) + 1):
        for r in range(max(0, int(y0)), min(7, int(y1 - 1e-9)) + 1):
            out.add(cell_id(c, r))
    return out


class ChangeClassifier:
    def __init__(self, history: int = 4) -> None:
        self._hist: Deque[FrozenSet[str]] = deque(maxlen=history)

    def reset(self) -> None:
        self._hist.clear()

    def classify(self, prev: Optional[bytes], new: Optional[bytes], *, window_changed: bool = False,
                 url_changed: bool = False, target_cells: Iterable[str] = (),
                 cursor_cells: Iterable[str] = ()) -> Dict[str, Any]:
        if url_changed:
            kind, cells = "navigation", changed_cells(prev, new) if prev and new else {}
        elif window_changed:
            kind, cells = "window", changed_cells(prev, new) if prev and new else {}
        elif prev is None or new is None:
            kind, cells = "major_layout", {}
        else:
            cells = changed_cells(prev, new)
            cs = frozenset(cells)
            tgt, cur = set(target_cells), set(cursor_cells)
            if not cs:
                kind = "none"
            elif len(cs) >= 64:
                kind = "major_layout"
            elif cs <= cur and len(cs) <= 4:
                kind = "cursor_only"
            elif tgt & (cs - cur):
                kind = "target_region"
            elif len(cs) <= 6 and sum(1 for h in self._hist if h == cs) >= 2:
                kind = "animation"
            else:
                kind = "small_region" if len(cs) <= 12 else "major_layout"
            self._hist.append(cs)
        if kind in ("window", "navigation", "major_layout"):
            self._hist.clear()
        return {"class": kind, "cells": sorted(cells)[:24], "n_cells": len(cells), **POLICY[kind]}
