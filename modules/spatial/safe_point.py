"""Safe interior click points + confidence.

Never click a target's edge: pick the lattice point inside the box that
maximises distance to the box border while staying out of overlapping
controls, window resize borders and scrollbar gutters.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .geometry import Rect

RESIZE_BORDER_DIP = 8
SCROLLBAR_DIP = 17


@dataclass
class SafePoint:
    x: int
    y: int
    margin: float          # px distance to the nearest box edge
    quality: float         # 0..1 - how safe the chosen point is
    reasons: List[str]

    def to_dict(self) -> Dict[str, object]:
        return {"x": self.x, "y": self.y, "margin_px": round(self.margin, 1),
                "quality": round(self.quality, 3), "notes": self.reasons}


def hazard_zones(win_rect: Optional[Dict[str, int]], scale: float = 1.0,
                 maximized: bool = False) -> List[Tuple[str, Rect]]:
    """Window resize borders and scrollbar gutters (physical px)."""
    if not win_rect:
        return []
    w = Rect.from_any(win_rect)
    b = max(2, int(RESIZE_BORDER_DIP * scale))
    s = max(8, int(SCROLLBAR_DIP * scale))
    zones: List[Tuple[str, Rect]] = []
    if not maximized:
        zones += [("resize", Rect(w.x, w.y, w.w, b)), ("resize", Rect(w.x, w.bottom - b, w.w, b)),
                  ("resize", Rect(w.x, w.y, b, w.h)), ("resize", Rect(w.right - b, w.y, b, w.h))]
    zones += [("scrollbar", Rect(w.right - b - s, w.y + int(w.h * 0.1), s, int(w.h * 0.85)))]
    return zones


def _contains_rect(outer: Rect, inner: Rect) -> bool:
    return outer.x <= inner.x and outer.y <= inner.y and outer.right >= inner.right and outer.bottom >= inner.bottom


def safe_point(box: Rect, obstacles: Sequence[Rect] = (), hazards: Sequence[Tuple[str, Rect]] = (),
               preferred: Optional[Tuple[int, int]] = None, lattice: int = 7) -> SafePoint:
    """Choose the safest pixel inside `box`.

    obstacles: other controls' rects; any that CONTAIN the box are treated as
      parents (ignored), any overlapping it are avoided.
    hazards:  (kind, rect) zones to avoid when possible.
    preferred: optional point (e.g. an explicit local coordinate) - kept if safe.
    """
    notes: List[str] = []
    if box.w <= 0 or box.h <= 0:
        raise ValueError("empty target box")
    blockers = [o for o in obstacles if o != box and o.intersect(box) and not _contains_rect(o, box)]
    half = max(0.5, min(box.w, box.h) / 2.0)

    def score(px: int, py: int) -> Tuple[float, float, List[str]]:
        m = min(px - box.x, box.right - 1 - px, py - box.y, box.bottom - 1 - py)
        s = m / half                      # 1.0 at centre of the short axis
        why: List[str] = []
        for o in blockers:
            if o.contains(px, py):
                s -= 2.0
                why.append("overlap")
                break
        for kind, z in hazards:
            if z.contains(px, py):
                s -= 1.0
                why.append(kind)
        return s, float(m), why

    cands: List[Tuple[int, int]] = []
    if preferred is not None and box.contains(*preferred):
        cands.append((int(preferred[0]), int(preferred[1])))
    n = max(3, lattice)
    for j in range(n):
        for i in range(n):
            px = box.x + int(round((i + 0.5) * box.w / n - 0.5))
            py = box.y + int(round((j + 0.5) * box.h / n - 0.5))
            cands.append(box.clamp_point(px, py))
    cands.append(box.center)

    best = None
    for idx, (px, py) in enumerate(cands):
        s, m, why = score(px, py)
        # tie-breaker: prefer the explicit point, then the centre
        cx, cy = box.center
        s2 = s - 0.001 * (abs(px - cx) + abs(py - cy)) / max(1.0, half)
        if preferred is not None and idx == 0 and s >= 0.25 and not why:
            s2 += 0.5
        if best is None or s2 > best[0]:
            best = (s2, px, py, m, why, s)
    assert best is not None
    _, px, py, m, why, raw = best
    if min(box.w, box.h) < 6:
        notes.append("tiny_target")
    notes += why
    quality = max(0.0, min(1.0, raw))
    if min(box.w, box.h) < 6:
        quality = min(quality, 0.5)
    return SafePoint(px, py, m, quality, notes)
