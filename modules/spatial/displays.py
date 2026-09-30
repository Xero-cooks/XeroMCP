"""Display model: every monitor with physical bounds, work area and DPI scale.

Designed for multi-monitor from day one: nothing assumes display 0 is the only
screen or sits at (0,0). Secondary monitors may have negative coordinates.
Enumeration is Windows-only; other platforms (and tests) build Display objects
directly.
"""
from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .geometry import Rect, _round

IS_WINDOWS = sys.platform == "win32"


@dataclass(frozen=True)
class Display:
    id: int
    bounds: Rect            # physical pixels, virtual-desktop coordinates
    work: Rect              # bounds minus taskbar/appbars
    scale: float = 1.0      # DPI / 96 (1.25 = 125 %)
    primary: bool = False
    name: str = ""

    @property
    def dpi(self) -> int:
        return _round(self.scale * 96)

    def logical_to_physical(self, lx: float, ly: float) -> Tuple[int, int]:
        """DPI-unaware (DIP) coordinates on THIS display -> physical pixels.
        Logical origin of a display = physical origin / scale (Windows
        virtualisation model for unaware callers)."""
        ox, oy = self.bounds.x / self.scale, self.bounds.y / self.scale
        return (self.bounds.x + _round((lx - ox) * self.scale),
                self.bounds.y + _round((ly - oy) * self.scale))

    def physical_to_logical(self, px: float, py: float) -> Tuple[float, float]:
        ox, oy = self.bounds.x / self.scale, self.bounds.y / self.scale
        return (ox + (px - self.bounds.x) / self.scale, oy + (py - self.bounds.y) / self.scale)

    def normalized_to_physical(self, nx: float, ny: float) -> Tuple[int, int]:
        """0..1 normalized -> physical pixel on this display (always inside)."""
        nx = min(max(nx, 0.0), 1.0)
        ny = min(max(ny, 0.0), 1.0)
        return (self.bounds.x + _round(nx * (self.bounds.w - 1)),
                self.bounds.y + _round(ny * (self.bounds.h - 1)))

    def taskbar_rects(self) -> List[Rect]:
        """Areas of bounds not in the work area (taskbar / appbars)."""
        b, w = self.bounds, self.work
        out = []
        if w.y > b.y:
            out.append(Rect(b.x, b.y, b.w, w.y - b.y))
        if w.bottom < b.bottom:
            out.append(Rect(b.x, w.bottom, b.w, b.bottom - w.bottom))
        if w.x > b.x:
            out.append(Rect(b.x, b.y, w.x - b.x, b.h))
        if w.right < b.right:
            out.append(Rect(w.right, b.y, b.right - w.right, b.h))
        return out

    def to_dict(self) -> Dict[str, object]:
        return {"id": self.id, "bounds": self.bounds.to_dict(), "work": self.work.to_dict(),
                "scale": self.scale, "dpi": self.dpi, "primary": self.primary, "name": self.name}


def virtual_bounds(displays: List[Display]) -> Rect:
    x0 = min(d.bounds.x for d in displays)
    y0 = min(d.bounds.y for d in displays)
    x1 = max(d.bounds.right for d in displays)
    y1 = max(d.bounds.bottom for d in displays)
    return Rect(x0, y0, x1 - x0, y1 - y0)


def display_for_point(displays: List[Display], px: float, py: float) -> Optional[Display]:
    for d in displays:
        if d.bounds.contains(px, py):
            return d
    return None


def display_for_rect(displays: List[Display], rect: Rect) -> Display:
    """Display with the largest overlap (Windows MONITOR_DEFAULTTONEAREST-ish)."""
    best, best_area = None, -1
    for d in displays:
        inter = d.bounds.intersect(rect)
        a = inter.area if inter else 0
        if a > best_area:
            best, best_area = d, a
    if best_area <= 0:
        cx, cy = rect.center
        best = min(displays, key=lambda d: (d.bounds.center[0] - cx) ** 2 + (d.bounds.center[1] - cy) ** 2)
    return best  # type: ignore[return-value]


def point_on_any_display(displays: List[Display], px: float, py: float) -> bool:
    return display_for_point(displays, px, py) is not None


# ------------------------------------------------------------------------------
# Windows enumeration (cached; monitors change rarely - re-read every 5 s)
# ------------------------------------------------------------------------------
_CACHE: Dict[str, object] = {"ts": 0.0, "displays": None}
_CACHE_TTL = 5.0
_LOCK = threading.Lock()


def _enumerate_windows() -> List[Display]:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD),
                    ("szDevice", ctypes.c_wchar * 32)]

    try:
        shcore = ctypes.windll.shcore
    except Exception:
        shcore = None

    out: List[Display] = []
    MonitorEnumProc = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.POINTER(wintypes.RECT), ctypes.c_double)

    def _cb(hmon, _hdc, _lprc, _data):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if not user32.GetMonitorInfoW(ctypes.c_void_p(hmon), ctypes.byref(info)):
            return 1
        m, w = info.rcMonitor, info.rcWork
        scale = 1.0
        if shcore is not None:
            dx, dy = wintypes.UINT(), wintypes.UINT()
            try:
                # MDT_EFFECTIVE_DPI = 0
                if shcore.GetDpiForMonitor(ctypes.c_void_p(hmon), 0, ctypes.byref(dx), ctypes.byref(dy)) == 0:
                    scale = round(dx.value / 96.0, 4)
            except Exception:
                pass
        out.append(Display(
            id=len(out),
            bounds=Rect(m.left, m.top, m.right - m.left, m.bottom - m.top),
            work=Rect(w.left, w.top, w.right - w.left, w.bottom - w.top),
            scale=scale, primary=bool(info.dwFlags & 1), name=info.szDevice))
        return 1

    user32.EnumDisplayMonitors(None, None, MonitorEnumProc(_cb), 0)
    # Stable ids: primary first, then left-to-right, top-to-bottom.
    out.sort(key=lambda d: (not d.primary, d.bounds.x, d.bounds.y))
    return [Display(i, d.bounds, d.work, d.scale, d.primary, d.name) for i, d in enumerate(out)]


def get_displays(refresh: bool = False, fallback_size: Optional[Tuple[int, int]] = None) -> List[Display]:
    with _LOCK:
        cached = _CACHE.get("displays")
        if not refresh and cached and time.monotonic() - float(_CACHE["ts"]) < _CACHE_TTL:
            return list(cached)  # type: ignore[arg-type]
        displays: List[Display] = []
        if IS_WINDOWS:
            try:
                displays = _enumerate_windows()
            except Exception:
                displays = []
        if not displays:
            w, h = fallback_size or (1920, 1080)
            r = Rect(0, 0, int(w), int(h))
            displays = [Display(0, r, r, 1.0, True, "fallback")]
        _CACHE.update(ts=time.monotonic(), displays=displays)
        return list(displays)
