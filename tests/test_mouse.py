# ==============================================================================
# test_mouse.py - Live tests of the `point` tool + mouse runtime internals.
# Uses a SELF-SPAWNED tkinter window with deterministic labeled buttons as the
# click target (Win11 notepad's UIA tree is a blind spot and has no menus).
# Non-destructive: everything it touches, it spawned itself.
# ==============================================================================
import asyncio
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules import desktop_native, mouse_runtime  # noqa: E402

PASSED, FAILED = [], []
MARKER = "MAGICRESULT:orange-777"
TARGET_APP = str(Path(__file__).resolve().parent / "mouse_target_app.py")


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def _wait_for_window(title_sub: str, timeout=8.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        for w in desktop_native.list_open_windows():
            if title_sub.lower() in w["title"].lower():
                return w
        time.sleep(0.2)
    return None


async def main() -> int:
    print("=== mouse runtime / point tool live tests ===\n")

    # --- 0. runtime status (bridge warm) ---
    st = await mouse_runtime.point_async(do="status")
    check("status: bridge alive", st.get("bridge_alive") is True, str(st))

    # --- spawn the target app ---
    app = subprocess.Popen([sys.executable, TARGET_APP])
    win = _wait_for_window("MouseTarget")
    if not win:
        app.kill()
        print("FATAL: target window did not open")
        return 1
    time.sleep(0.8)

    try:
        # --- 1. move: window lock + cursor lands on the target window ---
        r = await mouse_runtime.point_async(
            do="move", target="Click Me", window="MouseTarget")
        check("move: window locked + cursor placed", r.get("status") == "ok", str(r.get("error", "")))
        check("move: window_under_point is MouseTarget",
              "mousetarget" in (r.get("window_under_point") or "").lower(),
              r.get("window_under_point", ""))

        # --- 2. click by text with until-proof: clicking 'Click Me' shows
        #        'RESULT PANEL' label -> until="RESULT PANEL" must prove hit ---
        r = await mouse_runtime.point_async(
            do="click", target="Click Me", window="MouseTarget",
            until="RESULT PANEL", timeout_ms=2500)
        check("click 'Click Me' proven by until='RESULT PANEL'",
              r.get("status") == "hit", f"status={r.get('status')} proof={r.get('proof')}")
        check("motion is ghost or warp (never tween)",
              str(r.get("motion", "")).startswith(("ghost", "warp")), str(r.get("motion")))
        check("click under 3s end-to-end (cold OCR incl.)", r.get("ms", 99999) < 3000, f"{r.get('ms')}ms")

        # --- 3. contains-proof of OCR-visible text already on screen ---
        r = await mouse_runtime.point_async(
            do="click", target="Second Button", window="MouseTarget",
            until="second clicked", timeout_ms=2500)
        check("click 'Second Button' + contains proof",
              r.get("status") == "hit", f"proof={r.get('proof')}")

        # --- 4. ambiguity guard: 'Press' matches TOP + BOTTOM -> without a
        #        near-anchor it must REFUSE, not guess ---
        r = await mouse_runtime.point_async(
            do="click", target="Press", window="MouseTarget", timeout_ms=1800)
        check("ambiguous match refused (no blind click)",
              r.get("status") in ("ambiguous", "target_not_found", "failed"),
              f"status={r.get('status')}")

        # --- 5. near-anchor disambiguation: two 'Press' buttons; near='TOP ROW'
        #        must pick the top one ---
        r = await mouse_runtime.point_async(
            do="click", target="Press", near="TOP ROW", window="MouseTarget",
            until="top was clicked", timeout_ms=2500)
        check("near-anchor picks the right 'Press' (top)",
              r.get("status") == "hit", f"status={r.get('status')} proof={r.get('proof')}")

        # --- 6. scroll ---
        r = await mouse_runtime.point_async(do="scroll", amount=-3, window="MouseTarget")
        check("scroll executes", r.get("status") == "ok", str(r.get("error", "")))

        # --- 7. color blob unit test (the Kartik avatar geometry) ---
        from PIL import Image, ImageDraw
        from modules.mouse_runtime import _color_blob_above
        img = Image.new("RGB", (400, 300), (250, 250, 250))
        d = ImageDraw.Draw(img)
        d.ellipse((150, 60, 210, 120), fill=(220, 40, 40))
        d.text((150, 130), "Kartik", fill=(0, 0, 0))
        blob = _color_blob_above(img, {"x": 150, "y": 128, "w": 60, "h": 16})
        check("color blob finds red circle above label", blob is not None, str(blob))
        if blob:
            bx, by = blob[0] + blob[2] // 2, blob[1] + blob[3] // 2
            check("blob centroid lands on the circle (not the caption)",
                  60 <= by <= 120 and 150 <= bx <= 210, f"center=({bx},{by})")

    finally:
        app.kill()

    print(f"\nRESULTS: {len(PASSED)} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
