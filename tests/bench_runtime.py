"""OLD vs NEW workflow benchmark on the FAKE desktop (simulation, not Windows).

OLD  = see -> (model) -> act -> see -> (model) -> act ... : every step costs
       an observe round trip + an action round trip (+ a final see).
NEW  = xero stream: persistent state, all steps in ONE call, verification inside.

Metrics: tool round trips, screenshots (fake capture calls), bytes returned,
retries, false-success (reported done but the UI never reached the state),
target reacquisitions. Latency here is dominated by the fake renderer - read it
as relative bookkeeping, NOT as Windows latency. Run the live variant on Windows
via `python tests/bench_spatial.py --live -n 20`.

    python tests/bench_runtime.py            # prints a table, writes .xerospatial_bench/runtime_bench.json
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("XEROSPATIAL_MEMORY", "0")

from fake_world import FakeWorld, make_engine  # noqa: E402
from modules.runtime import ComputerRuntime  # noqa: E402
from modules.runtime import api as rt_api  # noqa: E402
from modules.runtime.changes import signature_gray  # noqa: E402
from modules.spatial import tool as spatial_tool  # noqa: E402

CHAIN = ["Blueprints", "xerocore-1", "Deploy", "Logs"]


def build(swallow=0):
    w = FakeWorld(uia_enabled=False, swallow_clicks=swallow)
    els = []
    for i, name in enumerate(CHAIN):
        e = w.add(name, 200 + i * 260, 300 + i * 120, w=180, h=40)
        e.visible = i == 0
        els.append(e)

    w.clicked = []

    def reveal(i):
        def cb(world, el):
            w.clicked.append(i)
            if i + 1 < len(els):
                els[i + 1].visible = True
            el.visible = False
        return cb
    for i, e in enumerate(els):
        e.on_click = reveal(i)
    eng = make_engine(w)
    rt = ComputerRuntime(on_invalidate=eng.invalidate_all)
    eng.runtime = rt
    return w, eng, rt


def captures(w):
    return sum(1 for c in w.calls if c[0] == "capture")


def old_flow(swallow=0, verify=True):
    """Model loop: see; click(until) ; see ... One tool call at a time."""
    w, eng, rt = build(swallow)
    trips = nbytes = retries = 0
    t0 = time.perf_counter()
    reported_ok = 0
    for i, name in enumerate(CHAIN):
        f = eng.observe(eng._context(""), force=True)                       # see
        trips += 1
        nbytes += len(json.dumps(f.cell_map())) + 60_000                    # ~60 KB thumbnail per see (jpeg q60 @800px)
        until = CHAIN[i + 1] if (verify and i + 1 < len(CHAIN)) else ("" if not verify else f"{name} gone")
        r = eng.act(spatial_tool.build_action("click", target=name, until=until), timeout_ms=2500, frame_id=f.frame_id)
        trips += 1
        nbytes += len(json.dumps(r, default=str))
        retries += max(0, int(r.get("tries", 1)) - 1)
        if r["status"] in ("hit", "fired_unverified"):
            reported_ok += 1                                                # a naive model treats "fired" as done
    trips += 1                                                              # final see to confirm
    eng.observe(eng._context(""), force=True)
    nbytes += 60_000
    done = not any(e.visible for e in w.elements) or w.elements[-1].visible is False
    actually = len(w.clicked)
    return dict(mode="OLD see->act", trips=trips, screenshots=captures(w), bytes=nbytes, retries=retries,
                reached=actually >= len(CHAIN), reported_ok=reported_ok, ms=int((time.perf_counter() - t0) * 1000),
                false_success=max(0, reported_ok - actually), reacquisitions=0)


def new_flow(swallow=0):
    w, eng, rt = build(swallow)
    steps = []
    for i, name in enumerate(CHAIN):
        nxt = CHAIN[i + 1] if i + 1 < len(CHAIN) else None
        steps.append({"type": "click", "target": name, **({"until": nxt} if nxt else {"until": f"{name} gone"}),
                      "timeout_ms": 2500})
        if nxt:
            steps.append({"type": "wait_until", "condition": nxt, "timeout_ms": 800})
    t0 = time.perf_counter()
    res = rt_api.run_stream(rt, eng, spatial_tool.run, steps, motion="sniper", timeout_ms=2500)
    ms = int((time.perf_counter() - t0) * 1000)
    nbytes = len(json.dumps(res, default=str))
    actually = len(w.clicked)
    verified = sum(1 for s in res["steps"] if s.get("verified") and s["type"] == "click")
    retries = sum(max(0, int(s.get("tries", 1)) - 1) for s in res["steps"])
    return dict(mode="NEW xero stream", trips=1, screenshots=captures(w), bytes=nbytes, retries=retries,
                reached=actually >= len(CHAIN), reported_ok=verified, ms=ms, status=res["status"],
                false_success=max(0, verified - actually), reacquisitions=res.get("reacquired", 0))


def main():
    rows = []
    for label, swallow in (("happy path", 0), ("first click swallowed", 1)):
        for fn in (old_flow, new_flow):
            r = fn(swallow=swallow)
            r["scenario"] = label
            rows.append(r)
    # the "naive" old flow that never asks for proof (fire and hope)
    r = old_flow(swallow=1, verify=False)
    r["scenario"], r["mode"] = "first click swallowed", "OLD no-until"
    rows.append(r)
    print(f"{'scenario':24}{'mode':18}{'trips':>6}{'shots':>6}{'bytes':>9}{'retry':>6}{'ok?':>5}{'false':>6}{'ms':>7}")
    for r in rows:
        print(f"{r['scenario']:24}{r['mode']:18}{r['trips']:>6}{r['screenshots']:>6}{r['bytes']:>9}{r['retries']:>6}"
              f"{str(r['reached']):>5}{r['false_success']:>6}{r['ms']:>7}")
    out = ROOT / ".xerospatial_bench"
    out.mkdir(exist_ok=True)
    (out / "runtime_bench.json").write_text(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
