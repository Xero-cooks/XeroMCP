# ==============================================================================
# mouse_runtime.py - THE HANDS. A resident aim computer + fire + proof engine.
#
# Field-report design implemented here:
#   * The agent never sends (x,y) as the normal path. It sends WHAT to hit and
#     WHAT MUST BE TRUE AFTER ("until"). Complexity lives here, not in MCP.
#   * Motion tiers: ghost (UIA Invoke, zero cursor travel) -> warp (SetCursorPos
#     + 8-16ms settle + SendInput down/up) -> flick (segmented, cap ~70ms, only
#     when explicitly asked). No pyautogui, no moveTo(duration=...), ever.
#   * Click storm: if proof fails, jittered retries inside the SAME call.
#   * Proof in the same breath: UIA re-find / OCR window crop / pixel delta /
#     window-under-point. "clicked: true" without visual proof is a lie.
#   * Resident: persistent PowerShell UIA bridge subprocess (JSON line protocol)
#     warmed at hub boot; a single worker thread serializes all mouse ops.
#
# This module runs INSIDE the hub process -> DPI awareness from desktop_native
# applies -> every coordinate is a physical 1080p pixel. Child-Python clicks
# (the old drift source) are banned by construction.
# ==============================================================================
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from .. import config
except ImportError:  # direct script execution
    import config  # type: ignore

from modules import desktop_native
from modules.ocr_engine import box_center, ocr_image_sync

if desktop_native.IS_WINDOWS:
    from ctypes import wintypes

_PS_BRIDGE = Path(__file__).resolve().parent / "uia_bridge.ps1"

# ------------------------------------------------------------------------------
# Win32: SendInput fire path (lowest jitter; no pyautogui on the hot path)
# ------------------------------------------------------------------------------
if desktop_native.IS_WINDOWS:
    _PUL = ctypes.POINTER(wintypes.ULONG)

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", _PUL)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]

    class _POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    _user32 = ctypes.windll.user32
    _MOUSE_MOVE = 0x0001
    _LEFT_DOWN = 0x0002
    _LEFT_UP = 0x0004
    _RIGHT_DOWN = 0x0008
    _RIGHT_UP = 0x0010
    _MIDDLE_DOWN = 0x0020
    _MIDDLE_UP = 0x0040
    _WHEEL = 0x0800
    _GA_ROOT = 2


from modules.input_lock import INPUT_LOCK

if desktop_native.IS_WINDOWS:
    _user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
    _user32.SendInput.restype = wintypes.UINT
    _user32.GetCursorPos.argtypes = (ctypes.POINTER(_POINT),)
    _user32.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)
    _user32.SetCursorPos.restype = wintypes.BOOL
    _user32.WindowFromPoint.argtypes = (_POINT,)
    _user32.WindowFromPoint.restype = wintypes.HWND
    _user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
    _user32.GetAncestor.restype = wintypes.HWND


class InputError(RuntimeError):
    """SendInput/SetCursorPos refused: UIPI (elevated target), secure
    desktop (locked / UAC prompt) or an invalid coordinate."""


def _mk(flags: int, data: int = 0) -> "_INPUT":
    inp = _INPUT(type=0)
    inp.union.mi.dwFlags = flags
    inp.union.mi.mouseData = ctypes.c_ulong(data & 0xFFFFFFFF).value
    return inp


def _send_inputs(events: List["_INPUT"]) -> None:
    """Send a gesture atomically; raise if Windows rejected any event."""
    arr = (_INPUT * len(events))(*events)
    sent = _user32.SendInput(len(events), arr, ctypes.sizeof(_INPUT))
    if sent != len(events):
        # never leave a button stuck down after a partial batch
        ups = [_mk(f) for f in (_LEFT_UP, _RIGHT_UP, _MIDDLE_UP)]
        _user32.SendInput(len(ups), (_INPUT * len(ups))(*ups), ctypes.sizeof(_INPUT))
        raise InputError(f"SendInput injected {sent}/{len(events)} events "
                         f"(error {ctypes.GetLastError()}): target may be elevated or desktop locked")


def _send_mouse(flags: int, data: int = 0) -> None:
    _send_inputs([_mk(flags, data)])


def cursor_pos() -> Tuple[int, int]:
    pt = _POINT()
    if not _user32.GetCursorPos(ctypes.byref(pt)):
        raise InputError("GetCursorPos failed (secure desktop?)")
    return int(pt.x), int(pt.y)


def _warp_to(x: int, y: int) -> None:
    if not _user32.SetCursorPos(int(x), int(y)):
        raise InputError(f"SetCursorPos({x},{y}) failed")


def move_to(x: int, y: int, tolerance: int = 0, verify: bool = True) -> Dict[str, Any]:
    """Absolute physical move. Skips the move when the cursor is already
    within `tolerance` px; verifies the hotspot landed (DPI/virtualisation
    bugs show up here as a mismatch instead of a silent wrong click)."""
    cx, cy = cursor_pos()
    if abs(cx - x) <= tolerance and abs(cy - y) <= tolerance:
        return {"moved": False, "at": (cx, cy)}
    _warp_to(x, y)
    if verify:
        ax, ay = cursor_pos()
        if abs(ax - x) > 1 or abs(ay - y) > 1:
            # one retry: another input source may have raced us
            _warp_to(x, y)
            ax, ay = cursor_pos()
            if abs(ax - x) > 1 or abs(ay - y) > 1:
                raise InputError(f"cursor landed at ({ax},{ay}) not ({x},{y}) - coordinate "
                                 "transform or DPI virtualisation problem")
        return {"moved": True, "at": (ax, ay)}
    return {"moved": True, "at": (x, y)}


_BTN = {"left": (_LEFT_DOWN, _LEFT_UP), "right": (_RIGHT_DOWN, _RIGHT_UP),
        "middle": (_MIDDLE_DOWN, _MIDDLE_UP)} if desktop_native.IS_WINDOWS else {}


def click_at(x: int, y: int, button: str = "left", count: int = 1, settle_ms: int = 10) -> Dict[str, Any]:
    """Move (if needed) + ONE atomic SendInput batch of down/up pairs. A
    double click is two pairs in the same batch -> always inside the
    system double-click time, never split by other input."""
    down, up = _BTN.get(button, _BTN["left"])
    with INPUT_LOCK:
        mv = move_to(x, y, tolerance=0)
        if mv["moved"]:
            time.sleep(settle_ms / 1000.0)   # let hover/hit-testing catch up
        _send_inputs([_mk(f) for _ in range(max(1, min(3, count))) for f in (down, up)])
    return {"moved": mv["moved"]}


def drag_path(x: int, y: int, x2: int, y2: int, steps: int = 12, hold_ms: int = 40,
              step_ms: int = 12, button: str = "left") -> Dict[str, Any]:
    """Press at (x,y), exceed the system drag threshold, travel, release.
    The button is ALWAYS released (finally), even on errors."""
    down, up = _BTN.get(button, _BTN["left"])
    with INPUT_LOCK:
        move_to(x, y)
        time.sleep(0.01)
        _send_inputs([_mk(down)])
        try:
            time.sleep(hold_ms / 1000.0)
            thr = max(4, int(_user32.GetSystemMetrics(68)) + 2)  # SM_CXDRAG
            dx = thr if x2 >= x else -thr
            _warp_to(x + dx, y)                                  # break the drag threshold
            time.sleep(step_ms / 1000.0)
            n = max(2, steps)
            for i in range(1, n + 1):
                _warp_to(int(round(x + (x2 - x) * i / n)), int(round(y + (y2 - y) * i / n)))
                time.sleep(step_ms / 1000.0)
            time.sleep(hold_ms / 1000.0)                          # drop targets need a beat
        finally:
            _send_inputs([_mk(up)])
    return {"moved": True}


def scroll_at(x: Optional[int], y: Optional[int], amount: int) -> Dict[str, Any]:
    amt = max(-50, min(50, int(amount)))
    if amt == 0:
        raise ValueError("scroll amount must be non-zero (wheel notches, + = up)")
    with INPUT_LOCK:
        if x is not None and y is not None:
            move_to(x, y, tolerance=2)
            time.sleep(0.01)
        _send_inputs([_mk(_WHEEL, 120 if amt > 0 else -120) for _ in range(abs(amt))])
    return {"notches": amt}


def execute_pointer(verb: str, x: int, y: int, x2: Optional[int] = None, y2: Optional[int] = None,
                    amount: int = 0, motion: str = "sniper") -> Dict[str, Any]:
    """The single physical-execution entry point for XeroSpatial.
    Returns {ok, motion} or {ok: False, status, error}. Never raises."""
    try:
        if verb == "click":
            if motion == "human":
                cx, cy = cursor_pos()
                with INPUT_LOCK:
                    _motion_flick(cx, cy, x, y)
                return {"ok": True, "motion": "flick"}
            click_at(x, y, "left", 1)
        elif verb == "double_click":
            click_at(x, y, "left", 2)
        elif verb == "right_click":
            click_at(x, y, "right", 1)
        elif verb in ("move", "hover"):
            with INPUT_LOCK:
                move_to(x, y, tolerance=0)
            if verb == "hover":
                time.sleep(0.25)
        elif verb == "drag":
            if x2 is None or y2 is None:
                return {"ok": False, "status": "bad_request", "error": "drag needs an end point"}
            drag_path(x, y, x2, y2)
        elif verb == "scroll":
            scroll_at(x, y, amount)
        else:
            return {"ok": False, "status": "bad_request", "error": f"unknown pointer verb {verb!r}"}
        return {"ok": True, "motion": "warp"}
    except InputError as e:
        return {"ok": False, "status": "input_blocked", "error": str(e)}
    except ValueError as e:
        return {"ok": False, "status": "bad_request", "error": str(e)}
    except Exception as e:  # report, never swallow
        return {"ok": False, "status": "error", "error": f"{type(e).__name__}: {e}"}


def _window_under_point(x: int, y: int) -> Tuple[Optional[int], str]:
    hwnd, title, _pid = window_under_point_ex(x, y)
    return hwnd, title


def window_under_point_ex(x: int, y: int) -> Tuple[Optional[int], str, int]:
    pt = _POINT(int(x), int(y))
    hwnd = _user32.WindowFromPoint(pt)
    if not hwnd:
        return None, "", 0
    root = _user32.GetAncestor(hwnd, _GA_ROOT) or hwnd
    root = int(root)
    return root, desktop_native._get_window_title(root), desktop_native.window_pid(root)


# ------------------------------------------------------------------------------
# Resident UIA bridge (ghost clicks + fast gone-checks; warm at boot)
# ------------------------------------------------------------------------------

class _UiaBridge:
    """Persistent PowerShell UIAutomation child. JSON-per-line protocol."""

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._out_q: "queue.Queue[str]" = None  # set in _spawn
        self._reader: Optional[threading.Thread] = None
        self._resp_q: "queue.Queue[dict]" = None
        self._timeouts = 0
        self._spawn()

    def _spawn(self) -> None:
        import queue
        old = self._proc
        if old is not None and old.poll() is None:
            try:
                old.kill()      # never leak a hung PowerShell child
            except Exception:
                pass
        self._timeouts = 0
        self._resp_q = queue.Queue()
        self._proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
             "Bypass", "-File", str(_PS_BRIDGE)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1,
        )

        def _reader():
            proc = self._proc
            if not proc or not proc.stdout:
                return
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._resp_q.put(json.loads(line, strict=False))
                except Exception:
                    pass

        self._reader = threading.Thread(target=_reader, daemon=True,
                                        name="uia-bridge-reader")
        self._reader.start()

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _request(self, payload: Dict[str, Any], timeout: float = 4.0) -> Optional[Dict[str, Any]]:
        if not self.alive():
            return None
        req_id = payload.setdefault("id", uuid.uuid4().hex[:8])
        with self._lock:
            # drain stale responses first
            while not self._resp_q.empty():
                try:
                    stale = self._resp_q.get_nowait()
                    if stale.get("id") == req_id:
                        return stale
                except Exception:
                    break
            try:
                assert self._proc and self._proc.stdin
                self._proc.stdin.write(json.dumps(payload) + "\n")
                self._proc.stdin.flush()
            except Exception:
                self._spawn()
                return None
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    resp = self._resp_q.get(timeout=max(0.05, deadline - time.monotonic()))
                    if resp.get("id") == req_id:
                        self._timeouts = 0
                        return resp
                    # else: a late answer to an earlier timed-out request - drop it
                except Exception:
                    break
            # Timed out. Two in a row = the bridge is wedged (huge UIA tree,
            # hung provider): recycle it instead of paying the timeout forever.
            self._timeouts += 1
            if self._timeouts >= 2:
                self._spawn()
            return None

    def ping(self) -> bool:
        resp = self._request({"op": "ping"}, timeout=6.0)
        if resp is None and not self.alive():
            self._spawn()
        return bool(resp and resp.get("ok"))

    def find(self, name: str, window: str = "", limit: int = 12,
             timeout: float = 3.0, hwnd: Optional[int] = None) -> Optional[List[Dict[str, Any]]]:
        """[] = scanned cleanly, nothing matched; None = could not scan
        (bridge down, timeout, window not found) -> callers fall through."""
        req: Dict[str, Any] = {"op": "find", "name": name, "window": window, "limit": limit}
        if hwnd:
            req["hwnd"] = int(hwnd)
        resp = self._request(req, timeout=timeout)
        if resp is None:
            return None
        if resp.get("error") and resp.get("error") != "element_not_found":
            return None
        return [c for c in (resp.get("candidates") or [])
                if c.get("enabled", True) is not False and c.get("w", 0) > 0 and c.get("h", 0) > 0]

    def invoke(self, name: str, window: str = "", index: int = 0,
               rect: Optional[Dict[str, int]] = None, hwnd: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Invoke the element whose bounds match `rect` (the candidate we
        actually scored) - not merely the Nth name match."""
        req: Dict[str, Any] = {"op": "invoke", "name": name, "window": window, "index": index}
        if rect:
            req["rect"] = {k: int(rect[k]) for k in ("x", "y", "w", "h")}
        if hwnd:
            req["hwnd"] = int(hwnd)
        return self._request(req)


_BRIDGE: Optional[_UiaBridge] = None
_EXEC = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mouse")


def warmup() -> Dict[str, Any]:
    """Spawn/verify the UIA bridge. Called at hub boot so the first click pays
    no PowerShell cold-start tax."""
    global _BRIDGE
    if _BRIDGE is None:
        _BRIDGE = _UiaBridge()
    ok = _BRIDGE.ping()
    return {"bridge": "alive" if ok else "unavailable", "warm": ok}


def _bridge() -> Optional[_UiaBridge]:
    global _BRIDGE
    if _BRIDGE is None:
        _BRIDGE = _UiaBridge()
    return _BRIDGE


# ------------------------------------------------------------------------------
# Eyes cache + last-seen memory (speculative pre-aim)
# ------------------------------------------------------------------------------

_EYES: Dict[str, Any] = {"lines": [], "origin": (0, 0), "ts": 0.0, "window": "", "size": (0, 0),
                         "hwnd": None}
_EYES_TTL = 1.2
_LAST_SEEN: Dict[str, Tuple[Dict[str, int], float]] = {}
_LAST_SEEN_TTL = 10.0
_MEM_LOCK = threading.Lock()


def feed_eyes(pil_img, ocr_lines: List[Dict[str, Any]], origin: Tuple[int, int] = (0, 0),
              window: str = "", hwnd: Optional[int] = None) -> None:
    """Called by `see` after every capture: the runtime always holds the last
    frame's boxes so a hit can start aiming before a new screenshot develops."""
    with _MEM_LOCK:
        _EYES.update(lines=ocr_lines, origin=tuple(origin), ts=time.monotonic(),
                     window=window or "", size=(pil_img.width, pil_img.height),
                     hwnd=hwnd if hwnd is not None else desktop_native._get_active_hwnd())


def _eyes_fresh(hwnd: Optional[int] = None) -> bool:
    """Fresh = young AND captured for the window we are about to act on.
    (Before: OCR boxes from ANY window were reused for 1.2 s.)"""
    if not _EYES["lines"] or (time.monotonic() - _EYES["ts"]) >= _EYES_TTL:
        return False
    cur = hwnd if hwnd is not None else desktop_native._get_active_hwnd()
    return _EYES.get("hwnd") in (None, cur)


def _remember(label: str, box: Dict[str, int]) -> None:
    with _MEM_LOCK:
        _LAST_SEEN[label.lower()] = (dict(box), time.monotonic())


def _recall(label: str) -> Optional[Dict[str, int]]:
    with _MEM_LOCK:
        item = _LAST_SEEN.get((label or "").lower())
        if item and (time.monotonic() - item[1]) < _LAST_SEEN_TTL:
            return item[0]
    return None


# ------------------------------------------------------------------------------
# Geometry helpers: exclusions, color blob (avatar circle), scoring
# ------------------------------------------------------------------------------

def _exclusion_zones() -> List[Tuple[int, int, int, int]]:
    """Real taskbar/appbar rects of every monitor (work area vs bounds) + the
    DPI-scaled toast corner of the primary. Falls back to the old guess."""
    try:
        from modules.spatial.displays import get_displays
        zones: List[Tuple[int, int, int, int]] = []
        for d in get_displays(fallback_size=desktop_native.screen_size()):
            zones += [(r.x, r.y, r.w, r.h) for r in d.taskbar_rects()]
            if d.primary:
                s_ = d.scale
                zones.append((d.work.right - int(440 * s_), d.work.bottom - int(230 * s_),
                              int(432 * s_), int(224 * s_)))
        if zones:
            return zones
    except Exception:
        pass
    W, H = desktop_native.screen_size()
    return [(0, H - 56, W, 56), (W - 440, H - 280, 432, 224)]


def _in_exclusions(cx: float, cy: float) -> bool:
    for (x, y, w, h) in _exclusion_zones():
        if x <= cx <= x + w and y <= cy <= y + h:
            return True
    return False


def _find_all_ocr(lines: List[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
    q = (query or "").strip().lower()
    if not q:
        return []
    q_words = [w for w in q.replace(":", " ").split() if w]
    hits = []
    for ln in lines:
        t = ln["text"].lower()
        if q in t:
            ratio = len(q) / max(len(t), 1)
            hits.append((ratio + 0.5, ln))  # exact phrase beats word-match
            continue
        # fuzzy word match: WinOCR misreads a char now and then; require most
        # of the needle's words to be present in the line (or the line in a word).
        if q_words:
            got = sum(1 for w in q_words if w in t or (len(w) > 5 and w[:5] in t))
            if got / len(q_words) >= 0.7:
                hits.append((got / len(q_words), ln))
    hits.sort(key=lambda p: -p[0])
    return [ln for _, ln in hits]


def _color_blob_above(img, label_box: Dict[str, Any], hint_bgr: Optional[Tuple[int, int, int]] = None,
                      max_up: int = 240, band_w: int = 260) -> Optional[Tuple[int, int, int, int]]:
    """Find the circular avatar/icon ABOVE a text label via saturated-color blob
    (the Kartik fix: click the circle, never the caption). Returns (x,y,w,h)
    in image coords, or None."""
    try:
        lx, ly = int(label_box["x"]), int(label_box["y"])
        cx_label = lx + int(label_box["w"]) / 2
        x0 = max(0, int(cx_label - band_w / 2))
        y0 = max(0, ly - max_up)
        crop = img.crop((x0, y0, min(img.width, int(cx_label + band_w / 2)), ly + 2)).convert("RGB")
        w, h = crop.size
        if w < 8 or h < 8:
            return None
        px = crop.load()
        pts: List[Tuple[int, int]] = []
        hint = hint_bgr
        # collect saturated pixels (or hint-colored ones)
        best_hue: Dict[Tuple[int, int, int], int] = {}
        for yy in range(0, h, 2):
            for xx in range(0, w, 2):
                r, g, b = px[xx, yy]
                mx, mn = max(r, g, b), min(r, g, b)
                sat = 0 if mx == 0 else (mx - mn) * 255 // mx
                if sat < 70 or mx < 80:
                    continue
                if hint is not None:
                    if abs(r - hint[0]) + abs(g - hint[1]) + abs(b - hint[2]) < 150:
                        pts.append((xx, yy))
                else:
                    key = (r // 40 * 40, g // 40 * 40, b // 40 * 40)
                    best_hue[key] = best_hue.get(key, 0) + 1
        if hint is None and best_hue:
            dom = max(best_hue, key=best_hue.get)  # type: ignore[arg-type]
            if best_hue[dom] < 12:
                return None
            for yy in range(0, h, 2):
                for xx in range(0, w, 2):
                    r, g, b = px[xx, yy]
                    if (r // 40 * 40, g // 40 * 40, b // 40 * 40) == dom:
                        pts.append((xx, yy))
        if len(pts) < 20:
            return None
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        # densest 80x80 window around the median blob center
        mx = sorted(xs)[len(xs) // 2]
        my = sorted(ys)[len(ys) // 2]
        win = [(xx, yy) for (xx, yy) in pts if abs(xx - mx) <= 48 and abs(yy - my) <= 48]
        if len(win) < max(12, len(pts) // 6):
            return None
        cx = sum(p[0] for p in win) / len(win) + x0
        cy = sum(p[1] for p in win) / len(win) + y0
        r = max(14, min(60, (max(p[0] for p in win) - min(p[0] for p in win)) // 2 or 20))
        return (int(cx - r), int(cy - r), int(2 * r), int(2 * r))
    except Exception:
        return None


def _score_candidates(cands: List[Dict[str, Any]], query: str, near_pt: Optional[Tuple[int, int]],
                      role: str) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """Score name match + role fit + proximity + exclusion zones.
    Returns (best, sorted_all). Ambiguity is decided by the caller."""
    scored = []
    for c in cands:
        name = (c.get("name") or c.get("text") or "").lower()
        q = query.lower().strip()
        match = 1.0 if name == q else (0.75 if name.startswith(q) else len(q) / max(len(name), 1))
        s = match
        cx = c.get("cx") or (c["x"] + c["w"] / 2)
        cy = c.get("cy") or (c["y"] + c["h"] / 2)
        if _in_exclusions(cx, cy):
            s -= 2.0  # taskbar/toast hits effectively die
        if role and role in c.get("ctype", "").lower():
            s += 0.3
        if near_pt:
            d = ((cx - near_pt[0]) ** 2 + (cy - near_pt[1]) ** 2) ** 0.5
            s += max(0.0, 0.5 - d / 2000.0)
        scored.append((s, c))
    scored.sort(key=lambda p: -p[0])
    if not scored:
        return None, []
    return scored[0][1], [c for _, c in scored]


# ------------------------------------------------------------------------------
# Window locking + capture for aiming
# ------------------------------------------------------------------------------

def _lock_window(window: str) -> Dict[str, Any]:
    """Foreground the target window and return its rect (physical px)."""
    if not window:
        hwnd = desktop_native._get_active_hwnd()
        title = desktop_native._get_window_title(hwnd) if hwnd else ""
    else:
        proof = desktop_native.bring_window_to_front(window)
        if not proof.get("verified"):
            return {"ok": False, "error": f"window focus failed: {proof.get('error', 'unknown')}"}
        hwnd = proof["hwnd"]
        title = proof.get("target_title", "")
    rect = desktop_native.window_rect(hwnd) if hwnd else None
    return {"ok": True, "hwnd": hwnd, "title": title, "rect": rect,
            "pid": desktop_native.window_pid(hwnd) if hwnd else 0}


def _capture_for_aim(win_rect: Optional[Dict[str, int]]):
    """Capture pixels to aim against (IN MEMORY - no more region_*.jpg per
    click on disk): window crop when possible, else the virtual desktop."""
    if win_rect and win_rect["w"] > 40 and win_rect["h"] > 40:
        img = desktop_native.grab(win_rect["x"], win_rect["y"], win_rect["w"], win_rect["h"])
        vs = desktop_native.virtual_screen()
        origin = (max(win_rect["x"], vs["x"]), max(win_rect["y"], vs["y"]))
    else:
        vs = desktop_native.virtual_screen()
        img = desktop_native.grab()
        origin = (vs["x"], vs["y"])
    return img, origin


# ------------------------------------------------------------------------------
# Aim computer
# ------------------------------------------------------------------------------

def _resolve_target(target: Any, near: str, role: str, window: str,
                    win_info: Dict[str, Any], timeout_budget: float) -> Dict[str, Any]:
    """Return {ok, kind, x, y, box, method, invoke, candidates?} - one local
    loop through UIA -> eyes cache/OCR -> memory; no MCP round trips."""
    # ---- 0. raw coordinates (debug escape hatch) ----
    if isinstance(target, dict) and "x" in target and "y" in target and "w" not in target:
        return {"ok": True, "kind": "point", "x": int(target["x"]), "y": int(target["y"]),
                "box": None, "method": "coords", "invoke": False}
    if isinstance(target, dict) and "x" in target and "w" in target:
        return {"ok": True, "kind": "box", "x": int(target["x"] + target["w"] / 2),
                "y": int(target["y"] + target["h"] / 2),
                "box": {k: int(target[k]) for k in ("x", "y", "w", "h")},
                "method": "coords", "invoke": False}

    query = target if isinstance(target, str) else str(target.get("text") or target.get("name") or "")
    if not query.strip():
        return {"ok": False, "error": "target_resolved_to_empty_query"}

    near_pt: Optional[Tuple[int, int]] = None

    # ---- 1. UIA (real control bounds; also enables ghost click) ----
    br = _bridge()
    uia_cands: List[Dict[str, Any]] = []
    if br is not None:
        _uia_t = min(1.2, max(0.4, timeout_budget * 0.5))  # respect the call budget

        def _collect(cands: Optional[List[Dict[str, Any]]]) -> None:
            for c in cands or []:
                if c.get("w", 0) <= 0 or c.get("h", 0) <= 0:
                    continue
                uia_cands.append({"x": c["x"], "y": c["y"], "w": c["w"], "h": c["h"],
                                  "cx": c["x"] + c["w"] / 2, "cy": c["y"] + c["h"] / 2,
                                  "name": c.get("name", ""), "ctype": c.get("ctype", ""),
                                  "invoke": bool(c.get("invoke") or c.get("toggle") or c.get("legacy"))})

        # Pass 1: descendants of the top-level window (fast, warm).
        p1 = br.find(query, window=window, timeout=_uia_t, hwnd=win_info.get("hwnd") if window else None)
        _collect(p1)
        # Pass 2 (desktop-wide popup scan) ONLY when pass 1 timed out/errored
        # (None) or no window was scoped. A clean empty list means the window
        # was scanned fine - a second 1.2s sweep would be pure latency.
        if not uia_cands and (p1 is None or not window):
            _collect(br.find(query, window="", timeout=_uia_t))

    # ---- 2. OCR: fresh eyes cache or a new window crop ----
    ocr_lines: List[Dict[str, Any]] = []
    origin = (0, 0)
    if _eyes_fresh(win_info.get("hwnd")):
        ocr_lines = _EYES["lines"]
        origin = tuple(_EYES["origin"])
    else:
        img, origin = _capture_for_aim(win_info.get("rect"))
        if img is not None:
            res = ocr_image_sync(img)
            ocr_lines = res.get("lines", [])
            feed_eyes(img, ocr_lines, origin, win_info.get("title", ""))

    ocr_cands: List[Dict[str, Any]] = []
    for ln in _find_all_ocr(ocr_lines, query):
        bx = {"x": ln["x"] + origin[0], "y": ln["y"] + origin[1], "w": ln["w"], "h": ln["h"]}
        ocr_cands.append({**bx, "cx": bx["x"] + bx["w"] / 2, "cy": bx["y"] + bx["h"] / 2,
                          "name": ln["text"], "ctype": "text", "invoke": False})
        _remember(query, bx)

    # ---- 3. last-seen memory ----
    # Memory is a PRIOR, never truth: re-read the remembered spot (small crop
    # OCR) and only use it if the label is still there. (Before: the runtime
    # clicked a 10-second-old box blindly when UIA and OCR both missed.)
    mem_cands: List[Dict[str, Any]] = []
    mem = _recall(query)
    if mem and not uia_cands and not ocr_cands:
        pad = 24
        crop = desktop_native.grab(mem["x"] - pad, mem["y"] - pad, mem["w"] + 2 * pad, mem["h"] + 2 * pad)
        if crop is not None:
            ox, oy = mem["x"] - pad, mem["y"] - pad
            for ln in _find_all_ocr(ocr_image_sync(crop).get("lines", []), query)[:1]:
                bx = {"x": ln["x"] + ox, "y": ln["y"] + oy, "w": ln["w"], "h": ln["h"]}
                mem_cands.append({**bx, "cx": bx["x"] + bx["w"] / 2, "cy": bx["y"] + bx["h"] / 2,
                                  "name": ln["text"], "ctype": "memory", "invoke": False})

    # ---- resolve 'near' anchor from the same OCR lines ----
    if near:
        for ln in _find_all_ocr(ocr_lines, near):
            near_pt = (ln["x"] + origin[0] + ln["w"] / 2, ln["y"] + origin[1] + ln["h"] / 2)
            break

    pool = uia_cands or ocr_cands or mem_cands
    if not pool:
        return {"ok": False, "error": "target_not_found",
                "uia_tried": br is not None, "ocr_lines": len(ocr_lines)}
    best, ordered = _score_candidates(pool, query, near_pt, role)
    assert best is not None
    # ambiguity guard: two strong candidates near each other -> refuse to guess
    if len(ordered) > 1 and len(uia_cands) + len(ocr_cands) > 1 and not near:
        second = ordered[1]
        d = ((second["cx"] - best["cx"]) ** 2 + (second["cy"] - best["cy"]) ** 2) ** 0.5
        names_differ = (second.get("name", "").lower() != best.get("name", "").lower())
        if d > 60 and names_differ:
            return {"ok": False, "error": "ambiguous",
                    "candidates": [{"name": c.get("name", ""), "cx": int(c["cx"]), "cy": int(c["cy"])}
                                   for c in ordered[:4]]}

    x, y = int(best["cx"]), int(best["cy"])
    box = {"x": int(best["x"]), "y": int(best["y"]), "w": int(best["w"]), "h": int(best["h"])}
    method = "uia" if best in uia_cands else ("memory" if best in mem_cands else "ocr")

    # ---- 4. role refinement: avatar/icon -> circle above the label ----
    if role in ("avatar", "icon") and method == "ocr":
        img = None
        img, org = _capture_for_aim(win_info.get("rect"))  # need pixels for the blob
        if img is not None:
            lbl = {"x": best["x"] - org[0], "y": best["y"] - org[1], "w": best["w"], "h": best["h"]}
            blob = _color_blob_above(img, lbl)
            if blob:
                gx, gy = blob[0] + org[0] + blob[2] // 2, blob[1] + org[1] + blob[3] // 2
                x, y = int(gx), max(0, int(gy))
                box = {"x": blob[0] + org[0], "y": blob[1] + org[1], "w": blob[2], "h": blob[3]}
                method = "ocr+colorblob"

    return {"ok": True, "kind": "box", "x": x, "y": y, "box": box, "method": method,
            "invoke": bool(best.get("invoke")) and method == "uia", "name": best.get("name", ""),
            "ctype": best.get("ctype", "") if method == "uia" else "",
            "uia_rect": {k: int(best[k]) for k in ("x", "y", "w", "h")} if method == "uia" else None}


# ------------------------------------------------------------------------------
# Proof ("until") - same breath, no extra agent round trip
# ------------------------------------------------------------------------------

def _parse_until(until: str) -> Tuple[str, str]:
    u = (until or "").strip()
    if not u:
        return "", ""
    low = u.lower()
    if low.startswith("url contains "):
        return "url", u[13:].strip()
    if low.endswith(" gone"):
        return "gone", u[:-5].strip()
    if low.startswith("gone:"):
        return "gone", u[5:].strip()
    return "contains", u


def _uia_text_present(text: str, window: str, timeout: float = 1.2) -> Optional[bool]:
    br = _bridge()
    if br is None:
        return None
    found = br.find(text, window=window, limit=3, timeout=timeout)
    if found is None:
        return None
    return len(found) > 0


def _ocr_window_text(win_info: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """Fresh OCR of the target window. None = could not read (capture or OCR
    engine failed) - which must never be mistaken for 'text is gone'."""
    img, origin = _capture_for_aim(win_info.get("rect"))
    if img is None:
        return None
    res = ocr_image_sync(img)
    if res.get("engine") == "none":
        return None
    return res.get("lines", [])


def _check_until(kind: str, needle: str, win_info: Dict[str, Any],
                 pre_ocr_lines: Optional[List[Dict[str, Any]]] = None,
                 deadline: Optional[float] = None) -> Dict[str, Any]:
    if not kind:
        # nothing was asked, so nothing was proven: never report success here
        return {"until_ok": None, "how": "not_requested"}
    time_left = (deadline - time.monotonic()) if deadline else 5.0
    if kind == "url":
        # CDP is probed ONCE per epoch (endpoint); when unavailable, fall back to
        # window-title / UIA / OCR evidence immediately instead of re-probing.
        from modules.runtime import get_runtime
        caps = get_runtime().caps

        def _cdp_tabs():
            import json as _json
            import urllib.request
            with urllib.request.urlopen(f"{config.CDP_ENDPOINT}/json", timeout=2) as r:
                return _json.loads(r.read().decode("utf-8", "replace")) or None
        st, tabs = caps.probe("cdp", _cdp_tabs, epoch=config.CDP_ENDPOINT)
        if st == "available":
            hit = any(needle in (t.get("url") or "") for t in tabs)
            return {"until_ok": hit, "how": "cdp_urls"}
        title = (win_info.get("title") or "")
        if needle.lower() in title.lower():
            return {"until_ok": True, "how": "window_title", "note": "cdp unavailable (cached); title match is weaker than a URL match"}
        present = _uia_text_present(needle, title) if time_left > 0.35 else None
        if present is True:
            return {"until_ok": True, "how": "uia_text", "note": "cdp unavailable (cached)"}
        lines = pre_ocr_lines if pre_ocr_lines is not None else _ocr_window_text(win_info)
        if lines is not None and _find_all_ocr(lines, needle):
            return {"until_ok": True, "how": "ocr_text", "note": "cdp unavailable (cached)"}
        return {"until_ok": False, "how": "cdp_unavailable_fallback", "error": "cdp unavailable; url not confirmed by title/UIA/OCR"}
    # UIA fast path (menus/popup HWNDs are handled inside the bridge)
    present = None
    if time_left > 0.35:
        present = _uia_text_present(needle, win_info.get("title", ""))
    if present is True:
        return {"until_ok": kind == "contains", "how": "uia"}
    # UIA absent (or bridge down) is INCONCLUSIVE, never final - pixels decide.
    # (Win11 notepad's edit text, popup menus etc. are blind spots for UIA.)
    lines = pre_ocr_lines if pre_ocr_lines is not None else _ocr_window_text(win_info)
    if lines is None:
        return {"until_ok": False, "how": "ocr", "error": "ocr unavailable - cannot prove"}
    present_v = bool(_find_all_ocr(lines, needle))
    return {"until_ok": (kind == "contains") == present_v, "how": "ocr"}


def _pixel_delta(before_img, after_img, cx: int, cy: int, half: int = 24) -> float:
    """Mean abs channel delta in a small crop around the hit point."""
    try:
        b = before_img.crop((cx - half, cy - half, cx + half, cy + half)).convert("L")
        a = after_img.crop((cx - half, cy - half, cx + half, cy + half)).convert("L")
        bd, ad = list(b.getdata()), list(a.getdata())
        n = min(len(bd), len(ad))
        return sum(abs(bd[i] - ad[i]) for i in range(0, n, 3)) / max(1, n // 3)
    except Exception:
        return -1.0


# ------------------------------------------------------------------------------
# Motion: ghost | warp | flick  (+ storm retry)
# ------------------------------------------------------------------------------

def _motion_warp_click(x: int, y: int, button: str = "left", settle_ms: int = 10) -> None:
    """Back-compat alias; the hardened path is click_at()."""
    click_at(x, y, button, 1, settle_ms)


_FLICK_TABLE = [(200, 1, 0.024), (800, 2, 0.044), (10**9, 3, 0.068)]  # (dist, segs, cap_s)


def _motion_flick(x0: int, y0: int, x1: int, y1: int) -> None:
    """Ballistic dash with 4-8px overshoot + snap back. Cap ~70ms."""
    import math
    dist = math.hypot(x1 - x0, y1 - y0)
    for lim, segs, cap in _FLICK_TABLE:
        if dist <= lim:
            total, n = cap, segs
            break
    ox = 6 if x1 >= x0 else -6
    oy = 6 if y1 >= y0 else -6
    tx, ty = x1 + ox, y1 + oy
    for i in range(1, n + 1):
        _warp_to(int(x0 + (tx - x0) * i / n), int(y0 + (ty - y0) * i / n))
        time.sleep(total / n * 0.5)
    _warp_to(x1, y1)
    time.sleep(0.008)
    _send_inputs([_mk(_LEFT_DOWN), _mk(_LEFT_UP)])


# ------------------------------------------------------------------------------
# Public entry: the point engine (sync; runs on the single mouse worker thread)
# ------------------------------------------------------------------------------

def _probe(x: int, y: int, half: int = 24):
    """48 px in-memory crop around the hit point (visual-delta evidence)."""
    try:
        return desktop_native.grab(x - half, y - half, 2 * half, 2 * half)
    except Exception:
        return None


_GHOST_OK = ("button", "menuitem", "hyperlink", "listitem", "tabitem",
             "checkbox", "radiobutton", "treeitem", "splitbutton")
# Re-clicking these flips state back -> a retry would UNDO the user's action.
_NO_RETRY = ("checkbox", "radiobutton", "toggle", "combobox", "edit", "slider", "treeitem")


def point_sync(do: str = "click", target: Any = None, target2: Any = None,
               near: str = "", window: str = "", until: str = "", role: str = "",
               motion: str = "sniper", timeout_ms: int = 1500,
               dx: int = 0, dy: int = 0, amount: int = 0) -> Dict[str, Any]:
    try:
        with INPUT_LOCK:
            return _point_sync(do, target, target2, near, window, until, role, motion,
                               timeout_ms, dx, dy, amount)
    except InputError as e:
        return {"status": "input_blocked", "error": str(e)}
    except Exception as e:  # never swallow silently, never crash the tool
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def _point_sync(do, target, target2, near, window, until, role, motion,
                timeout_ms, dx, dy, amount) -> Dict[str, Any]:
    t0 = time.monotonic()
    do = (do or "click").lower()
    do = {"double_click": "double", "right_click": "right", "hover": "move"}.get(do, do)
    budget = max(400, timeout_ms) / 1000.0

    def _ms() -> int:
        return int((time.monotonic() - t0) * 1000)

    if do == "status":
        br = _bridge()
        return {"status": "ok", "bridge_alive": bool(br and br.alive()),
                "eyes_age_ms": int((time.monotonic() - _EYES["ts"]) * 1000) if _EYES["ts"] else None,
                "last_seen_entries": len(_LAST_SEEN), "dpi_mode": desktop_native.dpi_mode()}
    if do not in ("click", "double", "right", "move", "drag", "scroll"):
        return {"status": "failed", "error": f"unknown do={do!r}",
                "valid": ["click", "double", "right", "move", "drag", "scroll", "status"]}

    # ---- 1. lock the window (abort rather than click the wrong app) ----
    win_info = _lock_window(window)
    if not win_info.get("ok"):
        return {"status": "failed", "error": win_info.get("error"), "ms": _ms()}

    # ---- 2. scroll: at the target when given, else where the cursor is ----
    if do == "scroll":
        sx = sy = None
        if target is not None:
            aim_s = _resolve_target(target, near, role, window, win_info, budget)
            if not aim_s.get("ok"):
                return {"status": aim_s.get("error", "failed"), "error": aim_s.get("error"), "ms": _ms()}
            sx, sy = aim_s["x"], aim_s["y"]
        if not int(amount or 0):
            return {"status": "failed", "error": "amount must be non-zero (+ = up)", "ms": _ms()}
        before = _probe(*(cursor_pos() if sx is None else (sx, sy)))
        r = scroll_at(sx, sy, int(amount))
        time.sleep(0.08)
        after = _probe(*(cursor_pos() if sx is None else (sx, sy)))
        vd = _pixel_delta(before, after, 24, 24) if before is not None and after is not None else -1
        return {"status": "hit" if vd >= 1.5 else "fired_unverified", "action": "scroll",
                "amount": r["notches"], "visual_delta": round(vd, 1) if vd >= 0 else None,
                "proof": {"until_ok": vd >= 1.5, "how": "visual_delta"}, "ms": _ms()}

    if target is None:
        return {"status": "failed", "error": "target required", "ms": _ms()}

    # ---- 3. aim ----
    aim = _resolve_target(target, near, role, window, win_info, budget)
    if not aim.get("ok"):
        return {"status": aim.get("error", "failed"),
                "error": aim.get("error"), "candidates": aim.get("candidates"),
                "ms": _ms()}
    x, y = aim["x"] + int(dx), aim["y"] + int(dy)
    vs = desktop_native.virtual_screen()          # ALL monitors, not just primary
    x = max(vs["x"], min(x, vs["x"] + vs["w"] - 1))
    y = max(vs["y"], min(y, vs["y"] + vs["h"] - 1))

    u_kind, u_needle = _parse_until(until)
    pre_present: Optional[bool] = None
    if u_kind == "gone":
        # Baseline ONLY to confirm the text existed before the click. It must
        # never be used as the post-click proof (that made the first check
        # always fail -> a guaranteed extra click).
        pre = _EYES["lines"] if _eyes_fresh(win_info.get("hwnd")) else _ocr_window_text(win_info)
        pre_present = bool(_find_all_ocr(pre or [], u_needle)) if pre is not None else None

    if do == "move":
        move_to(x, y)
        time.sleep(0.03)
        cx, cy = cursor_pos()
        on = abs(cx - x) <= 1 and abs(cy - y) <= 1
        hwnd, title = _window_under_point(x, y)
        return {"status": "hit" if on else "fired_unverified", "action": do,
                "hit": {"x": x, "y": y, "method": aim["method"]},
                "proof": {"until_ok": on, "how": "cursor_position"},
                "window_under_point": title, "ms": _ms()}

    # ---- 4. drag has its own gesture (and NEVER a click first) ----
    if do == "drag":
        aim2 = _resolve_target(target2 if target2 is not None else target, "", "", window, win_info, budget)
        if not aim2.get("ok"):
            return {"status": "failed", "error": "drag end target not found", "ms": _ms()}
        x2, y2 = aim2["x"], aim2["y"]
        drag_path(x, y, x2, y2)
        proof = {"until_ok": None, "how": "none"}
        if u_kind:
            time.sleep(0.12)
            proof = _check_until(u_kind, u_needle, win_info, None, time.monotonic() + max(0.9, budget))
        return {"status": "hit" if proof.get("until_ok") else "fired_unverified", "action": "drag",
                "from": {"x": x, "y": y}, "to": {"x": x2, "y": y2}, "proof": proof, "ms": _ms()}

    # ---- 5. fire (ghost -> warp; flick only on request) ----
    before_hwnd, before_title = _window_under_point(x, y)
    before_crop = _probe(x, y)
    fired = False
    used_motion = ""
    ctype = (aim.get("ctype") or "").lower()
    # Ghost = UIA Invoke. Only for a plain left click (Invoke is NOT a right
    # or double click), only on the exact element we scored (rect-matched),
    # and never for avatars (we must hit the circle, not the label).
    if do == "click" and motion != "human" and aim.get("invoke") \
            and role not in ("avatar", "icon") and any(g in ctype for g in _GHOST_OK):
        br = _bridge()
        if br is not None:
            resp = br.invoke(aim.get("name") or (target if isinstance(target, str) else ""),
                             window=window, rect=aim.get("uia_rect"),
                             hwnd=win_info.get("hwnd") if window else None)
            if resp and resp.get("ok"):
                fired, used_motion = True, f"ghost:{resp.get('how', 'invoke')}"
    if not fired and do == "click" and motion == "human":
        cx0, cy0 = cursor_pos()
        _motion_flick(cx0, cy0, x, y)
        used_motion, fired = "flick", True
    if not fired:
        button = "right" if do == "right" else "left"
        click_at(x, y, button, 2 if do == "double" else 1)
        used_motion, fired = "warp", True

    # ---- 6. proof in the same breath (+ SAFE retry) ----
    tries = 1
    proof: Dict[str, Any] = {"until_ok": None, "how": "not_requested"}
    if u_kind:
        deadline = time.monotonic() + max(0.9, budget - (time.monotonic() - t0))
        time.sleep(0.12)  # let the UI react (menus render a beat after the click)
        proof = _check_until(u_kind, u_needle, win_info, None, deadline)
        while not proof.get("until_ok") and time.monotonic() < deadline - 0.2:
            # Re-click ONLY if the first click visibly did nothing (swallowed
            # by a focus change etc.) and re-clicking cannot undo a toggle.
            # Otherwise keep polling: the UI may just be slow.
            after_probe = _probe(x, y)
            untouched = (before_crop is not None and after_probe is not None
                         and 0 <= _pixel_delta(before_crop, after_probe, 24, 24) < 1.5)
            if untouched and tries < 3 and do in ("click", "right") and not any(n in ctype for n in _NO_RETRY):
                offs = [(0, 0), (0, -4), (0, 4)]
                ox_, oy_ = offs[tries % len(offs)]
                if role in ("avatar", "icon") and tries == 2:
                    ox_, oy_ = 0, -14
                click_at(x + ox_, y + oy_, "right" if do == "right" else "left", 1)
                tries += 1
            time.sleep(0.12)
            proof = _check_until(u_kind, u_needle, win_info, None, deadline)
        if u_kind == "gone" and pre_present is False:
            proof["warning"] = f"{u_needle!r} was not visible BEFORE the click - 'gone' proves nothing"
            proof["until_ok"] = False

    # visual delta + window-under-point evidence (cheap, always)
    hwnd_after, title_after = _window_under_point(x, y)
    visual_delta = None
    after_crop = _probe(x, y)
    if before_crop is not None and after_crop is not None:
        visual_delta = _pixel_delta(before_crop, after_crop, 24, 24)

    # Success ONLY with a satisfied postcondition. No `until` = unverified,
    # whatever the click API returned (README: fired_unverified is a miss).
    status = "hit" if proof.get("until_ok") else "fired_unverified"
    result = {
        "status": status,
        "action": do,
        "hit": {"x": x, "y": y, "method": aim["method"], "box": aim.get("box"),
                "resolved_name": aim.get("name", "")},
        "motion": used_motion,
        "tries": tries,
        "proof": proof,
        "window_under_point_before": before_title,
        "window_under_point_after": title_after,
        "visual_delta": round(visual_delta, 1) if isinstance(visual_delta, float) and visual_delta >= 0 else None,
        "ms": _ms(),
    }
    if not u_kind:
        result["note"] = "no until given: pass until=... to get a proven 'hit'"
    return result


def point_async(**kwargs) -> Any:
    """Awaitable entry for MCP tools: serialized on the single mouse thread."""
    import asyncio
    return asyncio.get_running_loop().run_in_executor(_EXEC, lambda: point_sync(**kwargs))


def click_box_with_proof(box: Dict[str, int], window: str = "", until: str = "") -> Dict[str, Any]:
    """Warp-click a known box with the proof pipeline (used by see.click_text).
    Callers on the event loop must go through click_box_async instead."""
    return point_sync(do="click", target=box, window=window, until=until, timeout_ms=1200)


def click_box_async(box: Dict[str, int], window: str = "", until: str = "") -> Any:
    import asyncio
    return asyncio.get_running_loop().run_in_executor(
        _EXEC, lambda: click_box_with_proof(box, window=window, until=until))


def run_on_input_thread(fn, *args, **kwargs) -> Any:
    """Awaitable: run any input-touching callable on the single mouse thread."""
    import asyncio
    return asyncio.get_running_loop().run_in_executor(_EXEC, lambda: fn(*args, **kwargs))


def shutdown() -> None:
    global _BRIDGE
    br = _BRIDGE
    _BRIDGE = None
    if br is not None and br._proc is not None:
        try:
            br._proc.kill()
        except Exception:
            pass
