"""Real platform wiring for the SpatialEngine (Windows hub process).

Every method is a thin adapter over the EXISTING, hardened XeroMCP primitives:
  capture   -> desktop_native.grab (in-memory GDI BitBlt, all monitors)
  ocr       -> ocr_engine.ocr_image_sync (winsdk -> PowerShell WinRT -> tesseract)
  uia_*     -> mouse_runtime's resident UIA bridge
  pointer   -> mouse_runtime.execute_pointer (SendInput, verified cursor)
  keyboard  -> keys.type_text / keys.send_keys (SendInput, checked)
  until     -> mouse_runtime._check_until (UIA / OCR / CDP proofs)
No competing implementations live here.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from .displays import Display, get_displays
from .geometry import Rect


class WindowsBackends:
    def __init__(self) -> None:
        from modules import desktop_native, mouse_runtime  # noqa: F401  (DPI awareness side effect)
        self.dn = desktop_native
        self.mr = mouse_runtime

    # ---- windows / displays -------------------------------------------------
    def foreground(self) -> Dict[str, Any]:
        return self.dn.foreground_info()

    def focus(self, window: str) -> Dict[str, Any]:
        proof = self.dn.bring_window_to_front(window)
        if not proof.get("verified"):
            return {"ok": False, "error": proof.get("error") or proof.get("note") or "focus failed"}
        return self.dn.window_info(proof["hwnd"])

    def displays(self) -> List[Display]:
        return get_displays(fallback_size=self.dn.screen_size())

    # ---- eyes ---------------------------------------------------------------
    def capture(self, rect: Rect):
        return self.dn.grab(rect.x, rect.y, rect.w, rect.h)

    def ocr(self, img) -> Dict[str, Any]:
        from modules.ocr_engine import ocr_image_sync
        res = ocr_image_sync(img)
        return {"ok": res.get("engine") != "none", "engine": res.get("engine"), "lines": res.get("lines", [])}

    def uia_find(self, label: str, window_title: str, hwnd: Optional[int]) -> Optional[List[Dict[str, Any]]]:
        br = self.mr._bridge()
        if br is None or not br.alive():
            return None
        return br.find(label, window=window_title, timeout=1.2, hwnd=hwnd)

    def uia_invoke(self, label: str, window_title: str, hwnd: Optional[int], rect: Optional[Rect]) -> Optional[Dict[str, Any]]:
        br = self.mr._bridge()
        if br is None or not br.alive():
            return None
        return br.invoke(label, window=window_title, rect=rect.to_dict() if rect else None, hwnd=hwnd)

    # ---- hands --------------------------------------------------------------
    def cursor_pos(self) -> Tuple[int, int]:
        try:
            return self.mr.cursor_pos()
        except Exception:
            return (0, 0)

    def window_under_point(self, x: int, y: int) -> Tuple[Optional[int], str, int]:
        return self.mr.window_under_point_ex(x, y)

    def pointer(self, verb: str, x: int, y: int, x2: Optional[int] = None, y2: Optional[int] = None,
                amount: int = 0, motion: str = "sniper") -> Dict[str, Any]:
        return self.mr.execute_pointer(verb, x, y, x2, y2, amount, motion)

    def type_text(self, text: str) -> Dict[str, Any]:
        from modules import keys
        return keys.type_text(text)

    def send_keys(self, combo: str) -> Dict[str, Any]:
        from modules import keys
        return keys.send_keys(combo)

    # ---- proof --------------------------------------------------------------
    def check_until(self, until: str, ctx: Dict[str, Any], deadline: float) -> Dict[str, Any]:
        kind, needle = self.mr._parse_until(until)
        if kind == "contains" and needle.lower().startswith("title contains "):
            t = self.dn.foreground_info().get("title", "")
            n = needle[15:].strip().lower()
            return {"until_ok": n in t.lower(), "how": "window_title"}
        win = {"hwnd": ctx.get("hwnd"), "title": ctx.get("title", ""), "rect": ctx.get("rect")}
        # re-read the rect: the click may have moved/resized the window
        if ctx.get("hwnd"):
            win["rect"] = self.dn.window_rect(ctx["hwnd"]) or ctx.get("rect")
        return self.mr._check_until(kind, needle, win, None, deadline)

    def sleep(self, s: float) -> None:
        time.sleep(max(0.0, s))


class WindowsSampler:
    """Perception feeds for ComputerRuntime. READ-ONLY: never injects input."""

    def __init__(self, eng) -> None:
        self.eng = eng
        self.b = eng.b

    def fast(self) -> Dict[str, Any]:
        info = self.b.foreground() or {}
        fg = {k: info.get(k) for k in ("hwnd", "title", "rect", "exe", "pid")} if info.get("ok", True) else {}
        return {"cursor": self.b.cursor_pos(), "fg": fg}

    def _ctx(self) -> Dict[str, Any]:
        return self.eng._context("")

    def medium(self) -> Dict[str, Any]:
        from modules.runtime.changes import signature_gray
        from modules.runtime.coords import describe_space
        ctx = self._ctx()
        surface, _ = self.eng._surface(ctx, "screen", None)
        out: Dict[str, Any] = {"space": describe_space(ctx["displays"], self.eng.cols, self.eng.rows),
                               "bounds": (surface.x, surface.y, surface.w, surface.h)}
        img = self.b.capture(surface)
        if img is not None:
            out["sig"] = signature_gray(img)
        if "chrome" in str(ctx.get("exe") or "").lower():
            from modules import chrome_go
            fg = chrome_go.get_registry().observe()["foreground"]
            if fg:
                out["browser"] = {"profile": fg["profile"], "directory": fg["directory"], "profile_verified": fg["verified"],
                                  "page_title": fg["page_title"], "hwnd": fg["hwnd"], "url": None}
        return out

    def deep(self) -> Dict[str, Any]:
        self.eng.observe(self._ctx(), force=True)     # feeds the runtime via ingest_frame
        return {"ingested": True}


_ENGINE = None


def engine():
    """Process-wide SpatialEngine singleton (shared cache/locks/memory)."""
    global _ENGINE
    if _ENGINE is None:
        import os
        from .engine import SpatialEngine
        from .memory import SpatialMemory
        try:
            import config
            default_mem = str(config.PROJECT_ROOT / ".xerospatial_memory.json")
        except Exception:
            default_mem = ""
        persist = os.environ.get("XEROSPATIAL_MEMORY_FILE", default_mem)
        if os.environ.get("XEROSPATIAL_MEMORY", "1") == "0":
            persist = ""
        _ENGINE = SpatialEngine(WindowsBackends(), memory=SpatialMemory(persist_path=persist or None))
        from modules.runtime import ComputerRuntime, set_runtime, get_runtime
        rt = get_runtime()
        rt.sampler = WindowsSampler(_ENGINE)
        rt.on_invalidate = _ENGINE.invalidate_all
        _ENGINE.runtime = rt
    return _ENGINE
