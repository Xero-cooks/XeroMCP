import asyncio, subprocess, sys, time, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from modules import desktop_native, mouse_runtime

async def main():
    app = subprocess.Popen([sys.executable, str(Path(__file__).resolve().parent / "mouse_target_app.py")])
    t0 = time.monotonic(); win = None
    while time.monotonic() - t0 < 8 and not win:
        for w in desktop_native.list_open_windows():
            if "mousetarget" in w["title"].lower():
                win = w
                break
        time.sleep(0.2)
    time.sleep(0.8)
    try:
        print("=== T1: move (window_under_point) ===")
        r = await mouse_runtime.point_async(do="move", target="Click Me", window="MouseTarget")
        print(json.dumps(r, default=str, indent=1))

        print("=== T2: near-anchor two Press buttons ===")
        r = await mouse_runtime.point_async(do="click", target="Press", near="TOP ROW",
                                            window="MouseTarget", until="top was clicked",
                                            timeout_ms=2500)
        print(json.dumps(r, default=str, indent=1))

        print("=== T3: ambiguity guard with near given ===")
        r = await mouse_runtime.point_async(do="click", target="Button", window="MouseTarget",
                                            timeout_ms=1500)
        print(json.dumps(r, default=str, indent=1))

        print("=== T4: second button ===")
        r = await mouse_runtime.point_async(do="click", target="Second Button",
                                            window="MouseTarget", until="second clicked",
                                            timeout_ms=2500)
        print(json.dumps(r, default=str, indent=1))
    finally:
        app.kill()

asyncio.run(main())
