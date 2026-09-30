"""Deterministic spatial geometry. No I/O, no platform calls.

Conventions (all integers are PHYSICAL screen pixels):
  * Rect is half-open: x in [x, x+w), y in [y, y+h).
  * A Grid partitions a surface rect into cols x rows cells with integer
    edges floor(i * w / cols) - no gaps, no overlaps, any resolution.
  * Cell ids are chess-like: column letter + 1-based row -> A1 (top-left)
    ... P8 (bottom-right) for the default 16x8 grid.
  * Local coordinates inside any region run 0..LOCAL_MAX on both axes:
    0 = first pixel, LOCAL_MAX = last pixel, 32 = centre.
  * Refinement: "G4/B3" = sub-cell B3 of a SUB_COLS x SUB_ROWS split of G4.
    Levels nest ("G4/B3/A1"). Local coordinates apply to the innermost region.
"""
from __future__ import annotations

import bisect
import math
import re
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple

DEFAULT_COLS = 16
DEFAULT_ROWS = 8
LOCAL_MAX = 64
SUB_COLS = 4
SUB_ROWS = 4


def _round(v: float) -> int:
    """Round half up - deterministic, unlike Python's banker's round()."""
    return int(math.floor(v + 0.5))


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    @property
    def center(self) -> Tuple[int, int]:
        return (self.x + (self.w - 1) // 2 if self.w else self.x,
                self.y + (self.h - 1) // 2 if self.h else self.y)

    @property
    def area(self) -> int:
        return max(0, self.w) * max(0, self.h)

    def contains(self, px: float, py: float) -> bool:
        return self.x <= px < self.right and self.y <= py < self.bottom

    def intersect(self, other: "Rect") -> Optional["Rect"]:
        x0, y0 = max(self.x, other.x), max(self.y, other.y)
        x1, y1 = min(self.right, other.right), min(self.bottom, other.bottom)
        if x1 <= x0 or y1 <= y0:
            return None
        return Rect(x0, y0, x1 - x0, y1 - y0)

    def inflate(self, dx: int, dy: Optional[int] = None) -> "Rect":
        dy = dx if dy is None else dy
        return Rect(self.x - dx, self.y - dy, max(0, self.w + 2 * dx), max(0, self.h + 2 * dy))

    def clamp_point(self, px: int, py: int) -> Tuple[int, int]:
        return (min(max(px, self.x), self.right - 1), min(max(py, self.y), self.bottom - 1))

    def to_dict(self) -> Dict[str, int]:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}

    @staticmethod
    def from_any(obj) -> "Rect":
        if isinstance(obj, Rect):
            return obj
        if isinstance(obj, dict):
            return Rect(int(obj["x"]), int(obj["y"]), int(obj["w"]), int(obj["h"]))
        x, y, w, h = obj
        return Rect(int(x), int(y), int(w), int(h))


def local_to_point(region: Rect, lx: float, ly: float) -> Tuple[int, int]:
    """Map local (0..LOCAL_MAX) coordinates inside `region` to an exact pixel.
    Out-of-range values are clamped - the result is ALWAYS inside region."""
    if region.w <= 0 or region.h <= 0:
        raise ValueError(f"empty region {region}")
    lx = min(max(float(lx), 0.0), float(LOCAL_MAX))
    ly = min(max(float(ly), 0.0), float(LOCAL_MAX))
    px = region.x + _round(lx * (region.w - 1) / LOCAL_MAX)
    py = region.y + _round(ly * (region.h - 1) / LOCAL_MAX)
    return px, py


def point_to_local(region: Rect, px: float, py: float) -> Tuple[float, float]:
    """Inverse of local_to_point (unclamped; may fall outside 0..64)."""
    lx = (px - region.x) * LOCAL_MAX / max(1, region.w - 1)
    ly = (py - region.y) * LOCAL_MAX / max(1, region.h - 1)
    return round(lx, 2), round(ly, 2)


def col_letter(i: int) -> str:
    if not 0 <= i < 26:
        raise ValueError("grid supports at most 26 columns")
    return chr(ord("A") + i)


_CELL_RE = re.compile(r"^([A-Za-z])(\d{1,2})$")


def parse_cell_id(cid: str) -> Tuple[int, int]:
    """'G4' -> (col=6, row=3) zero-based."""
    m = _CELL_RE.match((cid or "").strip())
    if not m:
        raise ValueError(f"bad cell id {cid!r} (expected e.g. G4)")
    return ord(m.group(1).upper()) - ord("A"), int(m.group(2)) - 1


def _edges(start: int, length: int, n: int) -> List[int]:
    return [start + (i * length) // n for i in range(n + 1)]


@dataclass(frozen=True)
class Grid:
    """cols x rows partition of `bounds`."""
    bounds: Rect
    cols: int = DEFAULT_COLS
    rows: int = DEFAULT_ROWS

    def __post_init__(self) -> None:
        if not (1 <= self.cols <= 26 and 1 <= self.rows <= 99):
            raise ValueError("grid must be 1..26 cols and 1..99 rows")
        if self.bounds.w < self.cols or self.bounds.h < self.rows:
            raise ValueError(f"surface {self.bounds} too small for {self.cols}x{self.rows} grid")

    def cell_rect_rc(self, c: int, r: int) -> Rect:
        if not (0 <= c < self.cols and 0 <= r < self.rows):
            raise ValueError(f"cell ({c},{r}) outside {self.cols}x{self.rows} grid")
        xe = _edges(self.bounds.x, self.bounds.w, self.cols)
        ye = _edges(self.bounds.y, self.bounds.h, self.rows)
        return Rect(xe[c], ye[r], xe[c + 1] - xe[c], ye[r + 1] - ye[r])

    def cell_rect(self, cid: str) -> Rect:
        c, r = parse_cell_id(cid)
        return self.cell_rect_rc(c, r)

    def cell_id(self, c: int, r: int) -> str:
        return f"{col_letter(c)}{r + 1}"

    def cell_at(self, px: float, py: float) -> Optional[str]:
        """Exact inverse of cell_rect: which cell contains the pixel."""
        if not self.bounds.contains(px, py):
            return None
        xe = _edges(self.bounds.x, self.bounds.w, self.cols)
        ye = _edges(self.bounds.y, self.bounds.h, self.rows)
        c = min(self.cols - 1, bisect.bisect_right(xe, px) - 1)
        r = min(self.rows - 1, bisect.bisect_right(ye, py) - 1)
        return self.cell_id(c, r)

    def cells(self) -> Iterator[Tuple[str, Rect]]:
        for r in range(self.rows):
            for c in range(self.cols):
                yield self.cell_id(c, r), self.cell_rect_rc(c, r)

    def cells_overlapping(self, rect: Rect) -> List[str]:
        return [cid for cid, cr in self.cells() if cr.intersect(rect)]

    def neighbors(self, cid: str, radius: int = 1) -> List[str]:
        c, r = parse_cell_id(cid)
        out = []
        for rr in range(max(0, r - radius), min(self.rows, r + radius + 1)):
            for cc in range(max(0, c - radius), min(self.cols, c + radius + 1)):
                out.append(self.cell_id(cc, rr))
        return out

    def normalized(self, rect: Rect) -> Dict[str, float]:
        b = self.bounds
        return {"x0": round((rect.x - b.x) / b.w, 4), "y0": round((rect.y - b.y) / b.h, 4),
                "x1": round((rect.right - b.x) / b.w, 4), "y1": round((rect.bottom - b.y) / b.h, 4)}

    def describe_cell(self, cid: str) -> Dict[str, object]:
        rect = self.cell_rect(cid)
        return {"cell": cid.upper(), "rect": rect.to_dict(), "w": rect.w, "h": rect.h,
                "center": list(rect.center), "normalized": self.normalized(rect)}

    def to_dict(self) -> Dict[str, object]:
        return {"bounds": self.bounds.to_dict(), "cols": self.cols, "rows": self.rows,
                "first": "A1", "last": self.cell_id(self.cols - 1, self.rows - 1),
                "local_range": [0, LOCAL_MAX]}


@dataclass(frozen=True)
class Address:
    """A resolved spatial address: 'G4' or refined 'G4/B3[/..]'."""
    text: str
    levels: Tuple[str, ...]
    region: Rect


def resolve_address(grid: Grid, address: str, sub_cols: int = SUB_COLS,
                    sub_rows: int = SUB_ROWS) -> Address:
    parts = [p.strip() for p in re.split(r"[/.>]", (address or "").strip()) if p.strip()]
    if not parts:
        raise ValueError("empty cell address")
    region = grid.cell_rect(parts[0])
    for p in parts[1:]:
        if region.w < sub_cols or region.h < sub_rows:
            raise ValueError(f"region {region} too small to refine further")
        region = Grid(region, sub_cols, sub_rows).cell_rect(p)
    return Address(text="/".join(x.upper() for x in parts), levels=tuple(x.upper() for x in parts),
                   region=region)


def address_for_point(grid: Grid, px: int, py: int, levels: int = 1,
                      sub_cols: int = SUB_COLS, sub_rows: int = SUB_ROWS) -> Optional[Tuple[str, float, float]]:
    """Physical point -> ('G4/B3', lx, ly). Used to describe what we clicked."""
    cid = grid.cell_at(px, py)
    if cid is None:
        return None
    parts = [cid]
    region = grid.cell_rect(cid)
    for _ in range(max(0, levels - 1)):
        g = Grid(region, sub_cols, sub_rows)
        sub = g.cell_at(px, py)
        if sub is None:
            break
        parts.append(sub)
        region = g.cell_rect(sub)
    lx, ly = point_to_local(region, px, py)
    return "/".join(parts), lx, ly


@dataclass
class FrameTransform:
    """image pixel <-> physical pixel mapping for one captured frame."""
    origin_x: int = 0
    origin_y: int = 0
    scale_x: float = 1.0   # physical px per image px
    scale_y: float = 1.0

    def to_physical(self, ix: float, iy: float) -> Tuple[int, int]:
        return (self.origin_x + _round(ix * self.scale_x), self.origin_y + _round(iy * self.scale_y))

    def to_image(self, px: float, py: float) -> Tuple[float, float]:
        return ((px - self.origin_x) / self.scale_x, (py - self.origin_y) / self.scale_y)

    def rect_to_physical(self, r: Rect) -> Rect:
        x0, y0 = self.to_physical(r.x, r.y)
        x1, y1 = self.to_physical(r.right, r.bottom)
        return Rect(x0, y0, max(1, x1 - x0), max(1, y1 - y0))

    def to_dict(self) -> Dict[str, float]:
        return {"origin_x": self.origin_x, "origin_y": self.origin_y,
                "scale_x": self.scale_x, "scale_y": self.scale_y}
