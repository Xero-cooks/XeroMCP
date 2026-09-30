"""A deterministic fake desktop for XeroSpatial tests (no Windows needed).

Elements are solid, uniquely-coloured rectangles on a grey screen. The fake
OCR finds each element's colour inside whatever crop it is handed, so crop /
upscale / coordinate-transform bugs in the engine show up as wrong boxes.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image

from modules.spatial.displays import Display
from modules.spatial.geometry import Rect

BG = (40, 40, 40)


@dataclass
class El:
    text: str
    rect: Rect
    color: Tuple[int, int, int]
    ctype: str = "button"
    uia: bool = True
    visible: bool = True
    on_click: Optional[Callable[["FakeWorld", "El"], None]] = None


@dataclass
class FakeWorld:
    w: int = 1920
    h: int = 1080
    origin: Tuple[int, int] = (0, 0)
    scale: float = 1.0
    elements: List[El] = field(default_factory=list)
    uia_enabled: bool = True
    ocr_enabled: bool = True
    capture_enabled: bool = True
    focus_ok: bool = True
    input_blocked: bool = False
    occluder: Optional[Rect] = None
    fg_hwnd: int = 100
    swallow_clicks: int = 0           # first N clicks do nothing
    extra_displays: List[Display] = field(default_factory=list)
    calls: List[Tuple] = field(default_factory=list)
    cursor: Tuple[int, int] = (0, 0)
    _next_color: int = 60

    # ---- building ------------------------------------------------------------
    def add(self, text: str, x: int, y: int, w: int = 120, h: int = 32, **kw) -> El:
        self._next_color += 23
        c = (self._next_color % 256, (self._next_color * 7) % 256, 200)
        el = El(text, Rect(self.origin[0] + x, self.origin[1] + y, w, h), c, **kw)
        self.elements.append(el)
        return el

    def find(self, text: str) -> Optional[El]:
        return next((e for e in self.elements if e.text == text and e.visible), None)

    @property
    def bounds(self) -> Rect:
        return Rect(self.origin[0], self.origin[1], self.w, self.h)

    def render(self) -> Image.Image:
        img = Image.new("RGB", (self.w, self.h), BG)
        arr = np.array(img)
        for e in self.elements:
            if e.visible:
                x0, y0 = e.rect.x - self.origin[0], e.rect.y - self.origin[1]
                arr[max(0, y0):y0 + e.rect.h, max(0, x0):x0 + e.rect.w] = e.color
        return Image.fromarray(arr)


class FakeBackends:
    def __init__(self, world: FakeWorld) -> None:
        self.w = world

    # windows / displays
    def _info(self) -> Dict[str, Any]:
        b = self.w.bounds
        return {"ok": True, "hwnd": self.w.fg_hwnd, "title": "Fake App", "pid": 1, "exe": "fake.exe",
                "rect": {"x": b.x, "y": b.y, "w": b.w, "h": b.h - 40}, "maximized": True}

    def foreground(self):
        return self._info()

    def focus(self, window: str):
        if not self.w.focus_ok:
            return {"ok": False, "error": f"window {window!r} not found"}
        self.w.fg_hwnd = 100
        return self._info()

    def displays(self):
        b = self.w.bounds
        main = Display(0, b, Rect(b.x, b.y, b.w, b.h - 40), self.w.scale, True, "fake")
        return [main] + list(self.w.extra_displays)

    # eyes
    def capture(self, rect: Rect):
        self.w.calls.append(("capture", rect.to_dict()))
        if not self.w.capture_enabled:
            return None
        full = self.w.render()
        ox, oy = self.w.origin
        return full.crop((rect.x - ox, rect.y - oy, rect.right - ox, rect.bottom - oy))

    def ocr(self, img):
        self.w.calls.append(("ocr", img.size))
        if not self.w.ocr_enabled:
            return {"ok": False, "engine": "none", "lines": []}
        arr = np.asarray(img.convert("RGB"))
        lines = []
        for e in self.w.elements:
            if not e.visible:
                continue
            m = np.all(arr == np.array(e.color, dtype=arr.dtype), axis=-1)
            ys, xs = np.nonzero(m)
            if len(xs) < 4:
                continue
            lines.append({"text": e.text, "x": int(xs.min()), "y": int(ys.min()),
                          "w": int(xs.max() - xs.min() + 1), "h": int(ys.max() - ys.min() + 1)})
        return {"ok": True, "engine": "fake", "lines": lines}

    def uia_find(self, label, window_title, hwnd):
        self.w.calls.append(("uia_find", label))
        if not self.w.uia_enabled:
            return None
        return [{"name": e.text, "x": e.rect.x, "y": e.rect.y, "w": e.rect.w, "h": e.rect.h,
                 "ctype": e.ctype, "invoke": True, "enabled": True}
                for e in self.w.elements if e.visible and e.uia and label.lower() in e.text.lower()]

    def uia_invoke(self, label, window_title, hwnd, rect):
        self.w.calls.append(("uia_invoke", label))
        el = next((e for e in self.w.elements if e.visible and e.rect == rect), None)
        if el is None:
            return {"ok": False, "error": "scored_element_not_found"}
        if el.on_click:
            el.on_click(self.w, el)
        return {"ok": True, "how": "invoke"}

    # hands
    def cursor_pos(self):
        return self.w.cursor

    def window_under_point(self, x, y):
        if self.w.occluder is not None and self.w.occluder.contains(x, y):
            return 999, "Popup Other App", 2
        return 100, "Fake App", 1

    def pointer(self, verb, x, y, x2=None, y2=None, amount=0, motion="sniper"):
        self.w.calls.append(("pointer", verb, x, y, x2, y2))
        if self.w.input_blocked:
            return {"ok": False, "status": "input_blocked", "error": "SendInput blocked (UIPI)"}
        self.w.cursor = (x2, y2) if x2 is not None else (x, y)
        if verb == "click":
            if self.w.swallow_clicks > 0:
                self.w.swallow_clicks -= 1
                return {"ok": True, "motion": "warp"}
            hit = [e for e in self.w.elements if e.visible and e.rect.contains(x, y)]
            if hit and hit[-1].on_click:
                hit[-1].on_click(self.w, hit[-1])
        return {"ok": True, "motion": "warp"}

    def type_text(self, text):
        self.w.calls.append(("type", text))
        return {"status": "ok", "sent": len(text)}

    def send_keys(self, combo):
        self.w.calls.append(("keys", combo))
        return {"status": "ok", "sent": combo}

    # proof
    def check_until(self, until, ctx, deadline):
        u = until.strip().strip('"')
        if u.lower().endswith(" gone"):
            name = u[:-5].strip().strip('"')
            return {"until_ok": self.w.find(name) is None, "how": "fake_gone"}
        return {"until_ok": self.w.find(u) is not None, "how": "fake_visible"}

    def sleep(self, s):
        time.sleep(min(s, 0.01))


def hide(world: FakeWorld, el: El) -> None:
    el.visible = False


def make_engine(world: FakeWorld):
    from modules.spatial.engine import SpatialEngine
    from modules.spatial.memory import SpatialMemory
    from modules.spatial.telemetry import Telemetry
    return SpatialEngine(FakeBackends(world), memory=SpatialMemory(persist_path=None), telemetry=Telemetry())
