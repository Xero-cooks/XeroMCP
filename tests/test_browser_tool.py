# ==============================================================================
# tests/test_browser_tool.py - Stability-patch self-verification
# ==============================================================================
"""
Verifies (no Chrome/CDP required for tests 1-4):
  1. CDP-unavailable calls degrade gracefully (no asyncio loop crashes)
  2. Screenshots save to disk + scale matrix correctness
  3. bring_window_to_front returns proof-of-action structure
  4. keyboard_type / set_clipboard_and_paste return verification dicts
  5. (Optional, needs CDP Chrome) browser_open_and_act Tier 1 + Tier 2 vs
     tests/test_page.html served locally
"""

import asyncio
import sys
import threading
import time
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from modules import browser_cdp, desktop_native  # noqa: E402

PASSED, FAILED = [], []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {('- ' + str(detail)) if detail and not cond else ''}")


def serve_test_page():
    """Serve tests/ on port 8123 in a background thread."""
    handler = lambda *a, **kw: SimpleHTTPRequestHandler(*a, directory=str(ROOT / "tests"), **kw)
    srv = HTTPServer(("127.0.0.1", 8123), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main() -> int:
    print("=" * 70)
    print("STABILITY PATCH SELF-VERIFICATION")
    print("=" * 70)

    # --- 1. No asyncio loop collisions --------------------------------------
    print("\n[1] Async event-loop safety (no asyncio.run inside tools)")
    try:
        src = (ROOT / "modules" / "browser_cdp.py").read_text(encoding="utf-8")
        check("no asyncio.run( in browser_cdp.py", "asyncio.run(" not in src)
        # Calling an async tool from a fresh loop must not raise loop errors
        res = asyncio.run(browser_cdp.read_focused_element_value())
        check("async tool callable via asyncio.run (graceful CDP failure)",
              isinstance(res, dict) and ("error" in res or "focused" in res), res)
    except Exception as e:
        check("async tool callable via asyncio.run", False, e)

    # --- 2. Screenshot overhaul ---------------------------------------------
    print("\n[2] Screenshot artifacts + dual-coordinate matrix")
    try:
        res = desktop_native.take_screenshot()
        p = Path(res["inspection_image_path"])
        check("screenshot saved to disk", p.exists() and p.stat().st_size > 1000)
        check("served URL shape", res["inspection_image_url"].endswith("/inspections/latest.jpg"))
        check("scale matrix present",
              all(k in res for k in ("scale_x", "scale_y", "physical_width", "physical_height", "image_width", "image_height")))
        check("no base64 by default", "base64_jpeg" not in res)
        check("scale math consistent",
              abs(res["scale_x"] - res["physical_width"] / res["image_width"]) < 0.01)

        big = desktop_native.take_screenshot(return_base64=True)
        check("base64 only when requested", "base64_jpeg" in big and len(big["base64_jpeg"]) > 1000)

        reg = desktop_native.take_region_screenshot(0, 0, 400, 300)
        check("region screenshot 1:1 scale", reg["scale_x"] == 1.0 and reg["scale_y"] == 1.0)
    except Exception as e:
        check("screenshot pipeline", False, e)

    # --- 3. Window focus proof ----------------------------------------------
    print("\n[3] bring_window_to_front proof-of-action")
    try:
        res = desktop_native.bring_window_to_front("__no_such_window_xyz__")
        check("missing window -> verified=False (no fake success)", res.get("verified") is False and "error" in res)
        wins = desktop_native.list_open_windows()
        if wins:
            res2 = desktop_native.bring_window_to_front(wins[0]["title"][:20])
            check("real window returns verified key + before/after",
                  all(k in res2 for k in ("verified", "active_window_before", "active_window_title")))
        else:
            print("  [SKIP] no windows to foreground")
    except Exception as e:
        check("bring_window_to_front structure", False, e)

    # --- 4. Verified typing/pasting ------------------------------------------
    print("\n[4] keyboard_type / set_clipboard_and_paste verification dicts")
    try:
        res = desktop_native.keyboard_type("")
        check("keyboard_type returns verification dict",
              all(k in res for k in ("typed_chars", "active_window_title", "focus_lost_during_typing")))
        res = desktop_native.set_clipboard_and_paste("probe")
        check("paste returns focus-retention proof",
              all(k in res for k in ("pasted_chars", "focus_lost_during_paste")))
    except Exception as e:
        check("keyboard/paste verification structure", False, e)

    # --- 5. Optional: live CDP test ------------------------------------------
    print("\n[5] Live CDP browser_open_and_act (needs Chrome on port 9222)")
    srv = serve_test_page()
    url = "http://127.0.0.1:8123/test_page.html"
    try:
        import requests
        r = requests.get(f"http://127.0.0.1:{config.CDP_PORT}/json/version", timeout=2)
        cdp_live = r.status_code == 200
    except Exception:
        cdp_live = False

    if not cdp_live:
        print("  [SKIP] CDP not active - start Chrome with launch_chrome_with_cdp to run Tier 1-3 tests")
    else:
        # Tier 1: exact selector
        r1 = asyncio.run(browser_cdp.browser_open_and_act(url, selector="#composer", text="tier one input", submit=True))
        check("Tier1 selector: submitted", r1.get("status") in ("success", "fallback_used") and r1.get("submitted") is True, r1)
        check("Tier1 read-back matches", (r1.get("verified_input_value") or "").strip() == "tier one input", r1.get("verified_input_value"))
        check("Tier1 method_used", r1.get("method_used") == "cdp_selector", r1.get("method_used"))

        # Tier 2: no selector -> heuristic finds the visible textarea (skips display:none decoy)
        r2 = asyncio.run(browser_cdp.browser_open_and_act(url, text="tier two heuristic", submit=True))
        check("Tier2 heuristic: submitted", r2.get("status") == "fallback_used" and r2.get("submitted") is True, r2)
        check("Tier2 read-back matches", (r2.get("verified_input_value") or "").strip() == "tier two heuristic", r2.get("verified_input_value"))
        check("Tier2 inspection artifact", bool(r2.get("inspection_image_path")) and Path(r2["inspection_image_path"]).exists())
        check("Tier2 element_focused", r2.get("element_focused") is True)

    print("\n" + "=" * 70)
    print(f"RESULTS: {len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for f in FAILED:
            print(f"  - {f}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
