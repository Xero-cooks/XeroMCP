"""pytest config: make the hub importable on non-Windows CI.

pyautogui needs an X display on Linux; the real code paths are Windows-only
anyway, so a stub keeps module import (and the pure-Python XeroSpatial
tests) runnable anywhere. On Windows nothing is stubbed."""
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if os.name != "nt" and "pyautogui" not in sys.modules and not os.environ.get("DISPLAY"):
    stub = types.ModuleType("pyautogui")
    stub.FAILSAFE = False
    stub.PAUSE = 0

    def _noop(*a, **k):
        return None

    for name in ("click", "doubleClick", "rightClick", "moveTo", "dragTo", "scroll",
                 "hotkey", "press", "write", "typewrite", "mouseDown", "mouseUp",
                 "keyDown", "keyUp", "screenshot"):
        setattr(stub, name, _noop)
    stub.size = lambda: (1920, 1080)
    stub.position = lambda: (0, 0)
    sys.modules["pyautogui"] = stub

os.environ.setdefault("XEROSPATIAL_MEMORY", "0")

# Live scripts (run directly on the Windows box: `python tests/test_mouse.py`),
# not pytest modules - they need the hub, a desktop session and Chrome.
collect_ignore = ["test_fat_tools.py", "test_mouse.py", "test_browser_tool.py",
                  "debug_point.py", "probe_variants.py", "mouse_target_app.py", "bench_spatial.py"]
