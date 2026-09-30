"""SpatialFrame + VisionCache.

A frame is ONE observation: the captured surface, its grid, the elements
found in it (UIA/OCR boxes in PHYSICAL px), a pixel signature for change
detection, and the window it belongs to. Frames live in RAM only - pixels are
never written to disk by this module.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .geometry import FrameTransform, Grid, Rect

SIG_SIZE = (64, 32)


@dataclass
class Element:
    text: str
    rect: Rect
    source: str = "ocr"          # ocr | uia
    ctype: str = ""
    invoke: bool = False
    enabled: bool = True

    @property
    def center(self) -> Tuple[int, int]:
        return self.rect.center

    def to_dict(self, grid: Optional[Grid] = None) -> Dict[str, Any]:
        d = {"text": self.text, "box": self.rect.to_dict(), "source": self.source}
        if self.ctype:
            d["ctype"] = self.ctype
        if grid is not None:
            c = grid.cell_at(*self.center)
            if c:
                d["cell"] = c
        return d


# ------------------------------------------------------------------------------
# Pixel signatures (tiny grayscale thumbnails) - the change detector
# ------------------------------------------------------------------------------

def signature(img, size: Tuple[int, int] = SIG_SIZE) -> bytes:
    from PIL import Image
    return img.convert("L").resize(size, Image.Resampling.BILINEAR).tobytes()


SIG_PIXEL_DELTA = 32   # gray levels: a signature pixel counts as "changed"


def sig_diff(a: Optional[bytes], b: Optional[bytes]) -> float:
    """Change score in 0..1; 1.0 when not comparable.

    max(mean absolute difference, fraction of strongly-changed pixels): the
    mean alone dilutes LOCAL changes (a button leaving 13 % of a cell moved
    the mean by only ~0.04), the fraction alone ignores global shifts
    (theme/brightness). Antialiased downscaling keeps caret blinks and
    ClearType noise well under either measure."""
    if not a or not b or len(a) != len(b):
        return 1.0
    n = len(a)
    total = changed = 0
    for x, y in zip(a, b):
        d = abs(x - y)
        total += d
        if d > SIG_PIXEL_DELTA:
            changed += 1
    return max(total / (255.0 * n), changed / n)


def crop_signature(img, transform: FrameTransform, rect: Rect,
                   size: Tuple[int, int] = (16, 8)) -> Optional[bytes]:
    """Signature of a physical rect inside a frame image."""
    x0, y0 = transform.to_image(rect.x, rect.y)
    x1, y1 = transform.to_image(rect.right, rect.bottom)
    x0, y0 = max(0, int(x0)), max(0, int(y0))
    x1, y1 = min(img.width, int(x1)), min(img.height, int(y1))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    return signature(img.crop((x0, y0, x1, y1)), size)


def cell_occupancy(img, grid: Grid, transform: FrameTransform) -> Dict[str, float]:
    """Visual 'busyness' per cell in 0..1 (normalised std-dev of a small
    grayscale block). Empty background ~0, dense UI/text -> high."""
    from PIL import Image
    bw, bh = grid.cols * 8, grid.rows * 8
    small = img.convert("L").resize((bw, bh), Image.Resampling.BILINEAR)
    px = small.load()
    out: Dict[str, float] = {}
    for r in range(grid.rows):
        for c in range(grid.cols):
            vals = [px[c * 8 + i, r * 8 + j] for j in range(8) for i in range(8)]
            m = sum(vals) / 64.0
            var = sum((v - m) ** 2 for v in vals) / 64.0
            out[grid.cell_id(c, r)] = round(min(1.0, (var ** 0.5) / 64.0), 3)
    return out


# ------------------------------------------------------------------------------
# Frame
# ------------------------------------------------------------------------------

@dataclass
class SpatialFrame:
    surface: Rect
    grid: Grid
    transform: FrameTransform
    image: Any = None                      # PIL image, RAM only
    elements: List[Element] = field(default_factory=list)
    window: Dict[str, Any] = field(default_factory=dict)   # hwnd,title,rect
    display: Dict[str, Any] = field(default_factory=dict)
    ocr_engine: str = ""
    ocr_ok: bool = False
    frame_id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    ts: float = field(default_factory=time.monotonic)
    sig: Optional[bytes] = None
    _occupancy: Optional[Dict[str, float]] = None

    def __post_init__(self) -> None:
        if self.sig is None and self.image is not None:
            self.sig = signature(self.image)

    @property
    def age(self) -> float:
        return time.monotonic() - self.ts

    def occupancy(self) -> Dict[str, float]:
        if self._occupancy is None and self.image is not None:
            self._occupancy = cell_occupancy(self.image, self.grid, self.transform)
        return self._occupancy or {}

    def elements_in(self, rect: Rect) -> List[Element]:
        return [e for e in self.elements if rect.contains(*e.center)]

    def region_sig(self, rect: Rect) -> Optional[bytes]:
        if self.image is None:
            return None
        return crop_signature(self.image, self.transform, rect)

    def cell_map(self, max_labels: int = 3, max_len: int = 24) -> Dict[str, Any]:
        """Compact, LLM-friendly map: only non-empty cells, a few labels each."""
        occ = self.occupancy()
        cells: Dict[str, Any] = {}
        for e in self.elements:
            cid = self.grid.cell_at(*e.center)
            if not cid:
                continue
            slot = cells.setdefault(cid, {"labels": []})
            if len(slot["labels"]) < max_labels:
                slot["labels"].append(e.text[:max_len])
        for cid, v in occ.items():
            if v >= 0.08 and cid in cells:
                cells[cid]["occupancy"] = v
        return cells

    def describe(self) -> Dict[str, Any]:
        return {"frame_id": self.frame_id, "age_ms": int(self.age * 1000),
                "surface": self.surface.to_dict(), "grid": self.grid.to_dict(),
                "window": {k: v for k, v in self.window.items() if k in ("hwnd", "title", "rect")},
                "display": self.display, "elements": len(self.elements),
                "ocr_engine": self.ocr_engine}


# ------------------------------------------------------------------------------
# VisionCache: one see() should feed many spatial actions
# ------------------------------------------------------------------------------

class VisionCache:
    """Latest frame + validity rules. A cached frame is usable only when
    it is younger than ttl AND the foreground window (hwnd + rect) is the one
    it was captured for. Pixel-level revalidation of the specific target
    region happens in the engine right before acting (cheap crop)."""

    def __init__(self, ttl: float = 4.0, keep: int = 4) -> None:
        self.ttl = ttl
        self.keep = keep
        self._frame: Optional[SpatialFrame] = None
        self._recent: Dict[str, SpatialFrame] = {}
        self._lock = threading.Lock()
        self.last_invalidation = ""

    def put(self, frame: SpatialFrame) -> None:
        with self._lock:
            self._frame = frame
            self._recent[frame.frame_id] = frame
            while len(self._recent) > self.keep:
                self._recent.pop(next(iter(self._recent)))

    def get_by_id(self, frame_id: str, max_age: float = 30.0) -> Optional[SpatialFrame]:
        """A recent frame by id (for 'the agent saw frame X' staleness checks)."""
        with self._lock:
            f = self._recent.get(frame_id)
            return f if (f is not None and f.age <= max_age) else None

    def peek(self) -> Optional[SpatialFrame]:
        with self._lock:
            return self._frame

    def get(self, hwnd: Optional[int] = None, win_rect: Optional[Dict[str, int]] = None,
            max_age: Optional[float] = None) -> Optional[SpatialFrame]:
        with self._lock:
            f = self._frame
            if f is None:
                return None
            ttl = self.ttl if max_age is None else max_age
            if f.age > ttl:
                self.last_invalidation = "ttl"
                return None
            if hwnd is not None and f.window.get("hwnd") not in (None, hwnd):
                self.last_invalidation = "window_changed"
                return None
            if win_rect is not None and f.window.get("rect") not in (None, win_rect):
                self.last_invalidation = "window_moved"
                return None
            return f

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            f = self._frame
            return {"current": f.frame_id if f else None,
                    "age_ms": int(f.age * 1000) if f else None,
                    "recent": list(self._recent), "last_invalidation": self.last_invalidation}

    def invalidate(self, reason: str = "") -> None:
        with self._lock:
            self._frame = None
            self.last_invalidation = reason or "manual"
            # recent frames stay addressable by id so staleness can be PROVEN
