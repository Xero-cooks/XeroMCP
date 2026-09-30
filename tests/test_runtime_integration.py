"""Engine <-> ComputerRuntime integration on the fake desktop (simulation only)."""
from fake_world import FakeBackends, FakeWorld, hide, make_engine
from modules.runtime import ComputerRuntime
from modules.spatial import tool as spatial_tool


def rt_engine(world):
    eng = make_engine(world)
    rt = ComputerRuntime(on_invalidate=eng.invalidate_all)
    eng.runtime = rt
    return eng, rt


def click(eng, target=None, **kw):
    kw.setdefault("timeout_ms", 500)
    a = spatial_tool.build_action(kw.pop("action", "click"), target=target,
                                  **{k: kw.pop(k) for k in list(kw) if k in ("cell", "x", "y", "near", "text", "keys")})
    return eng.act(a, **kw)


def test_observe_feeds_runtime_state_and_shared_coordinate_space():
    w = FakeWorld(w=2560, h=1440, scale=1.25)
    w.add("Save", 100, 100)
    eng, rt = rt_engine(w)
    ctx = eng._context("")
    frame = eng.observe(ctx, force=True)
    snap = rt.observe()
    assert snap["visual"]["spatial_frame_id"] == frame.frame_id
    coord = snap["visual"]["coord"]
    assert coord["space_id"] == snap["space_id"] and coord["surface"] == frame.surface.to_dict()
    assert coord["dpi"] == 120 and coord["grid"] == {"cols": 16, "rows": 8, "local": 64}
    assert rt.space["virtual_desktop"] == {"x": 0, "y": 0, "w": 2560, "h": 1440}


def test_action_lifecycle_and_verification_events_from_engine():
    w = FakeWorld(uia_enabled=False)
    w.add("Go", 900, 500, on_click=hide)
    eng, rt = rt_engine(w)
    r = click(eng, "Go", until="Go gone")
    assert r["status"] == "hit"
    kinds = [e["kind"] for e in rt.events.since(0)["events"]]
    assert kinds.index("ACTION_STARTED") < kinds.index("ACTION_COMPLETED") and "VERIFICATION_PASSED" in kinds
    assert rt.observe()["interaction"]["action_status"] == "hit"
    r = click(eng, cell="A1", x=32, y=32)
    assert r["status"] == "fired_unverified" and r["proof"] == {"until_ok": None, "how": "not_requested", "note": r["proof"]["note"]}
    assert kinds.count("VERIFICATION_FAILED") == 0


def test_trajectory_walks_then_clicks_and_reports_steps():
    w = FakeWorld(uia_enabled=False)
    hits = []
    w.add("Far", 1500, 900, on_click=lambda world, el: hits.append(1))
    eng, rt = rt_engine(w)
    r = click(eng, "Far", motion="trajectory")
    assert r["status"] == "fired_unverified" and hits == [1]
    moves = [c for c in w.calls if c[0] == "pointer" and c[1] == "move"]
    assert len(moves) >= 4 and r["trajectory"]["steps_done"] == len(moves)
    xs = [m[2] for m in moves]
    assert xs == sorted(xs)                                  # monotonic approach, no random jitter
    assert [c[1] for c in w.calls if c[0] == "pointer"][-1] == "click"


def test_trajectory_interrupts_on_focus_loss_and_never_clicks():
    w = FakeWorld(uia_enabled=False)
    w.add("Far", 1500, 900, on_click=lambda world, el: None)
    eng, rt = rt_engine(w)
    b = eng.b
    orig = b.pointer
    n = {"m": 0}

    def pointer(verb, *a, **k):
        r = orig(verb, *a, **k)
        if verb == "move":
            n["m"] += 1
            if n["m"] == 2:
                w.fg_hwnd = 999                              # another window grabs focus mid-motion
        return r
    b.pointer = pointer
    r = click(eng, "Far", motion="trajectory")
    assert r["status"] == "interrupted" and r["reason"] == "focus_lost"
    assert not [c for c in w.calls if c[0] == "pointer" and c[1] == "click"]
    kinds = [e["kind"] for e in rt.events.since(0)["events"]]
    assert "ACTION_INTERRUPTED" in kinds


def test_runtime_invalidation_clears_engine_locks_and_cache():
    w = FakeWorld(uia_enabled=False)
    w.add("Go", 900, 500, on_click=hide)
    eng, rt = rt_engine(w)
    click(eng, "Go", until="Go gone")
    w.add("Keep", 300, 300)
    click(eng, "Keep", until="Keep visible")
    assert eng.locks.snapshot()
    rt.invalidate("test")
    assert eng.locks.snapshot() == []


def test_cancel_is_deterministic_with_trajectory():
    w = FakeWorld(uia_enabled=False)
    w.add("Far", 1500, 900)
    eng, rt = rt_engine(w)
    a = spatial_tool.build_action("click", target="Far")
    import time as _t
    eng.cancel()
    r = eng.act(a, submitted_at=_t.monotonic() - 5, motion="trajectory", timeout_ms=500)   # submitted BEFORE cancel
    assert r["status"] == "cancelled" and not [c for c in w.calls if c[0] == "pointer"]
    r = click(eng, "Far", motion="trajectory")                                             # later action unaffected
    assert r["status"] in ("fired_unverified", "hit")


def test_cdp_url_proof_probes_once_then_falls_back_without_reprobing(monkeypatch):
    import urllib.request
    from modules import mouse_runtime
    from modules.runtime import ComputerRuntime, set_runtime
    rt = ComputerRuntime()
    set_runtime(rt)
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise ConnectionRefusedError("no cdp")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setattr(mouse_runtime, "_uia_text_present", lambda *a, **k: None)
    monkeypatch.setattr(mouse_runtime, "_ocr_window_text", lambda *a, **k: [])
    win = {"title": "Blueprints - Render - Google Chrome - XeroCore", "rect": None}
    outs = [mouse_runtime._check_until("url", "blueprints", win) for _ in range(6)]
    assert calls["n"] == 1                                            # probed once, cached afterwards
    assert all(o["until_ok"] is True and o["how"] == "window_title" for o in outs)   # immediate fallback evidence
    miss = mouse_runtime._check_until("url", "settings", win)
    assert miss["until_ok"] is False and miss["how"] == "cdp_unavailable_fallback" and calls["n"] == 1
    assert rt.caps.state("cdp", mouse_runtime.config.CDP_ENDPOINT) == "unavailable"
    set_runtime(None)


def test_no_until_never_claims_semantic_verification():
    from modules import mouse_runtime, until_proof
    assert until_proof.check_until("")["until_ok"] is None
    assert mouse_runtime._check_until("", "", {})["until_ok"] is None
