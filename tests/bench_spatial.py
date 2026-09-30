"""XeroSpatial benchmark.

  python tests/bench_spatial.py --fake        # engine overhead on a synthetic desktop (any OS)
  python tests/bench_spatial.py --live        # Windows desktop: real clicks on tests/mouse_target_app.py
  python tests/bench_spatial.py --live -n 20 --json out.json

Live mode compares, on the same self-spawned tkinter window:
  raw          desktop_native.mouse_click at a known coordinate (+ OCR proof)
  point        legacy semantic `point` tool (mouse_runtime.point_sync)
  spatial      spatial_point by label, cold (locks + cache cleared each run)
  spatial_lock spatial_point by label, warm (target lock reused)
  spatial_cell spatial_point by grid cell + local coords (the agent already knows where)
Each click alternates between "Press TOP" and "Press BOTTOM" so every proof
requires a REAL state change ("top was clicked" / "bottom was clicked").
Success = the until-condition was proven; anything else is a failure.
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

TARGETS = [("Press TOP", "top was clicked"), ("Press BOTTOM", "bottom was clicked")]


def _pct(xs: List[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    k = min(len(xs) - 1, max(0, int(round(p / 100.0 * (len(xs) - 1)))))
    return xs[k]


def _summ(name: str, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    ms = [r["ms"] for r in rows]
    ok = [r for r in rows if r["ok"]]
    stages: Dict[str, List[float]] = {}
    for r in rows:
        for k, v in (r.get("timing") or {}).items():
            stages.setdefault(k, []).append(float(v))
    return {"mode": name, "n": len(rows), "success": len(ok), "success_rate": round(len(ok) / max(1, len(rows)), 3),
            "p50_ms": round(statistics.median(ms), 1) if ms else 0, "p95_ms": round(_pct(ms, 95), 1),
            "stages_p50_ms": {k: round(statistics.median(v), 2) for k, v in sorted(stages.items())},
            "statuses": sorted({r["status"] for r in rows})}


def _run(name: str, fn: Callable[[int], Dict[str, Any]], n: int) -> Dict[str, Any]:
    rows = []
    for i in range(n):
        t0 = time.perf_counter()
        try:
            r = fn(i) or {}
        except Exception as e:  # a crash is a failed sample, not a crashed benchmark
            r = {"status": f"exception:{type(e).__name__}", "error": str(e)}
        rows.append({"ms": (time.perf_counter() - t0) * 1000, "ok": r.get("status") == "hit",
                     "status": str(r.get("status")), "timing": r.get("timing_ms")})
    return _summ(name, rows)


# ------------------------------------------------------------------------------
# fake: pure engine overhead (no OS input, deterministic)
# ------------------------------------------------------------------------------

def bench_fake(n: int) -> List[Dict[str, Any]]:
    from fake_world import FakeWorld, make_engine
    from modules.spatial import tool
    from modules.spatial.geometry import Grid, point_to_local

    out = []
    for res in ((1920, 1080), (2560, 1440), (3840, 2160)):
        for uia in (True, False):
            w = FakeWorld(w=res[0], h=res[1], uia_enabled=uia)
            state = {"last": ""}

            def react(world, el, text):
                for t in ("top was clicked", "bottom was clicked"):
                    e = world.find(t)
                    if e:
                        e.visible = False
                world.add(text, 100, 900, 200, 24, uia=False)
            for i, (lbl, proof) in enumerate(TARGETS):
                el = w.add(lbl, 400 + 300 * i, 300, 140, 34)
                el.on_click = (lambda p: (lambda world, e: react(world, e, p)))(proof)
            eng = make_engine(w)

            def cold(i):
                eng.locks.clear()
                eng.cache.invalidate("bench")
                lbl, proof = TARGETS[i % 2]
                return eng.act(tool.build_action("click", target=lbl), until=proof, timeout_ms=2000, retries=0)

            def warm(i):
                lbl, proof = TARGETS[i % 2]
                return eng.act(tool.build_action("click", target=lbl), until=proof, timeout_ms=2000, retries=0)

            g = Grid(w.bounds, 16, 8)

            def cell(i):
                lbl, proof = TARGETS[i % 2]
                el = w.find(lbl)
                cx, cy = el.rect.center
                c = g.cell_at(cx, cy)
                lx, ly = point_to_local(g.cell_rect(c), cx, cy)
                return eng.act(tool.build_action("click", cell=c, x=lx, y=ly), until=proof, timeout_ms=2000,
                               retries=0)
            tag = f"{res[0]}x{res[1]} uia={'on' if uia else 'off'}"
            for name, fn in (("spatial", cold), ("spatial_lock", warm), ("spatial_cell", cell)):
                s = _run(name, fn, n)
                s["config"] = tag
                out.append(s)
    return out


# ------------------------------------------------------------------------------
# live: Windows desktop, real input
# ------------------------------------------------------------------------------

def bench_live(n: int) -> List[Dict[str, Any]]:
    if sys.platform != "win32":
        raise SystemExit("--live needs the Windows desktop session the hub runs in")
    from modules import desktop_native, mouse_runtime
    from modules.spatial import tool
    from modules.spatial.backends import engine
    from modules.spatial.geometry import Grid, point_to_local, Rect

    app = subprocess.Popen([sys.executable, str(ROOT / "tests" / "mouse_target_app.py")])
    try:
        time.sleep(2.0)
        mouse_runtime.warmup()
        eng = engine()
        win = "MouseTarget"
        out = []

        # locate both buttons once (for raw + cell modes)
        where = {}
        for lbl, proof in TARGETS:
            r = eng.act(tool.build_action("click", target=lbl), window=win, dry_run=True)
            where[lbl] = (r.get("point") or {}).get("x"), (r.get("point") or {}).get("y")

        def raw(i):
            lbl, proof = TARGETS[i % 2]
            x, y = where[lbl]
            t0 = time.perf_counter()
            desktop_native.bring_window_to_front(win)
            desktop_native.mouse_click(x, y, "left", 1)
            ok = False
            deadline = time.monotonic() + 2.5
            while time.monotonic() < deadline and not ok:
                ok = proof in (mouse_runtime._ocr_window_text(desktop_native.foreground_info()) or "").lower()
            return {"status": "hit" if ok else "fired_unverified",
                    "timing_ms": {"total": (time.perf_counter() - t0) * 1000}}

        def legacy(i):
            lbl, proof = TARGETS[i % 2]
            return mouse_runtime.point_sync(do="click", target=lbl, window=win, until=proof, timeout_ms=2500)

        def cold(i):
            eng.locks.clear()
            eng.cache.invalidate("bench")
            lbl, proof = TARGETS[i % 2]
            return eng.act(tool.build_action("click", target=lbl), window=win, until=proof, timeout_ms=2500)

        def warm(i):
            lbl, proof = TARGETS[i % 2]
            return eng.act(tool.build_action("click", target=lbl), window=win, until=proof, timeout_ms=2500)

        def cell(i):
            lbl, proof = TARGETS[i % 2]
            x, y = where[lbl]
            ctx = eng._context(win)
            g = eng.grid_for(ctx)
            c = g.cell_at(x, y)
            lx, ly = point_to_local(g.cell_rect(c), x, y)
            return eng.act(tool.build_action("click", cell=c, x=lx, y=ly), window=win, until=proof, timeout_ms=2500)

        for name, fn in (("raw", raw), ("point", legacy), ("spatial", cold), ("spatial_lock", warm),
                         ("spatial_cell", cell)):
            out.append(_run(name, fn, n))
        return out
    finally:
        app.terminate()


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--fake", action="store_true")
    g.add_argument("--live", action="store_true")
    ap.add_argument("-n", type=int, default=12)
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    res = bench_fake(a.n) if a.fake else bench_live(a.n)
    for r in res:
        cfg = f"[{r['config']}] " if r.get("config") else ""
        print(f"{cfg}{r['mode']:<13} ok {r['success']}/{r['n']}  p50 {r['p50_ms']:>7.1f} ms  "
              f"p95 {r['p95_ms']:>7.1f} ms  stages {r['stages_p50_ms']}  {r['statuses']}")
    if a.json:
        Path(a.json).write_text(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
