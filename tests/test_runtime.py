"""ComputerRuntime tests. Pure-Python simulation (Linux CI): these prove the
LOGIC of state, events, capability caching, tracking, interruption and the
Chrome profile state machine. They do NOT prove Windows behaviour."""
import json

import pytest
from PIL import Image

from modules.runtime import ComputerRuntime
from modules.runtime import chrome_flow
from modules.runtime.capabilities import CapabilityRegistry
from modules.runtime.changes import ChangeClassifier, cells_for_rect, signature_gray
from modules.runtime.chrome_registry import ChromeRegistry, parse_window_title
from modules.runtime.coords import describe_space
from modules.runtime.events import EventBuffer
from modules.runtime.stream import StreamRunner
from modules.runtime.tracker import TargetTracker
from modules.runtime.trajectory import plan_path, run_trajectory
from modules.spatial.displays import Display
from modules.spatial.geometry import Rect


# ---------------------------------------------------------------- helpers
def img(color=200, boxes=(), size=(1920, 1080)):
    im = Image.new("RGB", size, (color,) * 3)
    for (x, y, w, h, c) in boxes:
        im.paste((c,) * 3, (x, y, x + w, y + h))
    return im


class FakeSampler:
    def __init__(self):
        self.fg = {"hwnd": 1, "title": "App", "rect": {"x": 0, "y": 0, "w": 1920, "h": 1080}, "exe": "app.exe"}
        self.image = img()
        self.elements = []
        self.browser = None
        self.cursor = (10, 10)
        self.deep_calls = 0

    def fast(self):
        return {"cursor": self.cursor, "fg": dict(self.fg)}

    def medium(self):
        out = {"sig": signature_gray(self.image), "bounds": (0, 0, 1920, 1080)}
        if self.browser:
            out["browser"] = dict(self.browser)
        return out

    def deep(self):
        self.deep_calls += 1
        return {"elements": list(self.elements), "bounds": (0, 0, 1920, 1080), "sig": signature_gray(self.image)}


def el(text, x, y, w=100, h=30):
    return {"text": text, "bbox": (x, y, w, h), "confidence": 0.95, "source": "ocr"}


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t

    def adv(self, dt):
        self.t += dt


# ---------------------------------------------------------------- events
def test_event_buffer_bounded_and_since():
    b = EventBuffer(maxlen=5)
    for i in range(12):
        b.emit("URL_CHANGED", url=str(i))
    r = b.since(0)
    assert len(r["events"]) == 5 and r["dropped"] is True and r["last_seq"] == 12
    assert b.since(10)["events"][0]["seq"] == 11 and b.since(12)["events"] == []
    with pytest.raises(ValueError):
        b.emit("NOPE")


def test_screen_changed_bursts_coalesce():
    b = EventBuffer()
    for _ in range(20):
        b.emit("SCREEN_CHANGED", **{"class": "animation"})
    evs = b.since(0)["events"]
    assert len(evs) == 1 and evs[0]["n"] == 20


# ---------------------------------------------------------------- capabilities
def test_capability_probe_not_repeated_until_epoch_changes():
    caps = CapabilityRegistry()
    calls = []

    def bad():
        calls.append(1)
        raise ConnectionError("no cdp")

    for _ in range(10):
        st, _ = caps.probe("cdp", bad, epoch="pid1")
    assert st == "unavailable" and len(calls) == 1 and caps.stats["skipped"] == 9
    caps.probe("cdp", bad, epoch="pid2")           # material change (new browser) -> re-probe
    assert len(calls) == 2
    caps.probe("cdp", lambda: {"tabs": 1}, epoch="pid3")
    assert caps.state("cdp", "pid3") == "available"
    caps.reset("cdp")
    assert caps.state("cdp", "pid3") == "unknown"


def test_capability_change_emits_event():
    rt = ComputerRuntime()
    rt.caps.probe("cdp", lambda: None, epoch=1)
    assert any(e["kind"] == "CAPABILITY_CHANGED" for e in rt.events.since(0)["events"])


# ---------------------------------------------------------------- change classes
def test_change_classification():
    c = ChangeClassifier()
    base = signature_gray(img())
    assert c.classify(base, base)["class"] == "none"
    cur = cells_for_rect((0, 0, 60, 60), (0, 0, 1920, 1080))
    sig_c = signature_gray(img(boxes=[(5, 5, 40, 40, 0)]))
    r = c.classify(base, sig_c, cursor_cells=cur)
    assert r["class"] == "cursor_only" and r["keep_locks"] and not r["interrupt"]
    far = signature_gray(img(boxes=[(900, 500, 200, 100, 0)]))
    assert c.classify(base, far)["class"] == "small_region"
    tgt = cells_for_rect((900, 500, 200, 100), (0, 0, 1920, 1080))
    r = c.classify(base, far, target_cells=tgt)
    assert r["class"] == "target_region" and r["interrupt"] and not r["keep_locks"]
    assert c.classify(base, signature_gray(img(color=30)))["class"] == "major_layout"
    assert c.classify(base, base, window_changed=True)["class"] == "window"
    assert c.classify(base, base, url_changed=True)["class"] == "navigation"


def test_repeated_same_cells_becomes_animation():
    c = ChangeClassifier()
    a, b = signature_gray(img()), signature_gray(img(boxes=[(900, 500, 100, 100, 0)]))
    kinds = [c.classify(a if i % 2 else b, b if i % 2 else a)["class"] for i in range(5)]
    assert kinds[-1] == "animation"


# ---------------------------------------------------------------- tracker
def test_tracker_updates_moved_target_and_multicell():
    ev = []
    t = TargetTracker(on_event=lambda k, **d: ev.append(k))
    t.track("Save", (100, 100, 80, 30), frame=1)
    r = t.update([el("Save", 130, 105, 80, 30)], 2, (0, 0, 1920, 1080))
    assert r["moved"] == ["save"] and t.get("save").state == "visible" and "TARGET_MOVED" in ev
    assert t.get("save").bbox == (130, 105, 80, 30) and t.get("save").last_seen_frame == 2
    wide = t.track("Wide label", (100, 100, 600, 30), frame=3)
    cells = wide.to_dict((0, 0, 1920, 1080))["cells"]
    assert len(cells) >= 5 and cells == sorted(cells)      # spans many cells, kept whole


def test_tracker_disappear_predict_then_lose_then_reacquire():
    clk = Clock()
    ev = []
    t = TargetTracker(on_event=lambda k, **d: ev.append(k), clock=clk)
    t.track("Row", (100, 100, 80, 30), 1)
    clk.adv(0.1)
    t.update([el("Row", 130, 100, 80, 30)], 2, (0, 0, 1920, 1080))    # moving right
    clk.adv(0.1)
    t.update([], 3, (0, 0, 1920, 1080))
    assert t.get("row").state == "predicted" and t.usable("row")
    clk.adv(1.0)
    t.update([], 4, (0, 0, 1920, 1080))
    assert t.get("row").state == "lost" and t.usable("row") is None and "TARGET_DISAPPEARED" in ev
    clk.adv(0.2)
    t.update([el("Row", 135, 100, 80, 30)], 5, (0, 0, 1920, 1080))
    assert t.get("row").state == "visible" and t.reacquisitions == 1
    clk.adv(5)
    t.update([], 6, (0, 0, 1920, 1080))
    assert t.get("row") is None                                    # not kept forever


def test_tracker_does_not_jump_to_far_same_label():
    t = TargetTracker()
    t.track("OK", (100, 100, 40, 20), 1)
    t.update([el("OK", 1700, 900, 40, 20)], 2, (0, 0, 1920, 1080))     # a DIFFERENT "OK"
    assert t.get("ok").state != "visible" or t.get("ok").bbox[0] == 100


# ---------------------------------------------------------------- coordinate space
def _d(i, x, y, w, h, s, primary=False):
    return Display(id=i, bounds=Rect(x, y, w, h), work=Rect(x, y, w, h - 40), scale=s, primary=primary)


@pytest.mark.parametrize("w,h,s", [(1920, 1080, 1.0), (2560, 1440, 1.25), (3840, 2160, 1.5), (3840, 2160, 2.0)])
def test_space_id_stable_and_changes_with_layout(w, h, s):
    a = describe_space([_d(0, 0, 0, w, h, s, True)])
    assert a["space_id"] == describe_space([_d(0, 0, 0, w, h, s, True)])["space_id"]
    assert a["virtual_desktop"] == {"x": 0, "y": 0, "w": w, "h": h} and a["grid"]["cells"] == 128
    b = describe_space([_d(0, 0, 0, w, h, s, True), _d(1, -1920, -200, 1920, 1080, 1.0)])
    assert b["space_id"] != a["space_id"] and b["virtual_desktop"]["x"] == -1920
    assert b["virtual_desktop"]["w"] == w + 1920


def test_space_change_invalidates_targets():
    rt = ComputerRuntime()
    rt.set_space(describe_space([_d(0, 0, 0, 1920, 1080, 1.0, True)]))
    rt.tracker.track("Save", (10, 10, 50, 20), 1)
    rt.set_space(describe_space([_d(0, 0, 0, 2560, 1440, 1.0, True)]))
    assert rt.tracker.get("save") is None
    assert any(e["kind"] == "STATE_INVALIDATED" for e in rt.events.since(0)["events"])


# ---------------------------------------------------------------- trajectory
def test_plan_path_deterministic_bounded_and_reaches_end():
    p = plan_path((0, 0), (1900, 1000))
    assert p == plan_path((0, 0), (1900, 1000)) and p[-1] == (1900, 1000) and len(p) <= 17
    assert plan_path((5, 5), (5, 5)) == [(5, 5)]
    assert plan_path((0, 0), (30, 0))[-1] == (30, 0)


def test_trajectory_interrupts_and_retargets_and_input_failure():
    moved = []
    path = plan_path((0, 0), (1000, 0))
    r = run_trajectory(path, moved.append, check_fn=lambda i, p: "focus_lost" if i == 3 else None)
    assert r["status"] == "interrupted" and r["reason"] == "focus_lost" and r["steps_done"] == 3
    moved.clear()
    calls = {"n": 0}

    def retarget():
        calls["n"] += 1
        return (1000, 300) if calls["n"] == 2 else None
    r = run_trajectory(path, moved.append, retarget_fn=retarget)
    assert r["status"] == "reached" and r["retargets"] == 1 and moved[-1] == (1000, 300)
    r = run_trajectory(path, lambda p: {"ok": False, "error": "blocked"})
    assert r["status"] == "input_failed"


# ---------------------------------------------------------------- runtime state
def make_rt():
    clk = Clock()
    s = FakeSampler()
    rt = ComputerRuntime(sampler=s, clock=clk, wall=clk)
    return rt, s, clk


def test_state_transitions_events_and_frame_ids():
    rt, s, clk = make_rt()
    rt.tick_fast(); rt.tick_medium()
    rt.session()
    f0 = rt.frame_id
    s.fg = {**s.fg, "hwnd": 2, "title": "Other"}
    rt.tick_fast()
    kinds = [e["kind"] for e in rt.events.since(0)["events"]]
    assert "FOCUS_CHANGED" in kinds and "WINDOW_CHANGED" in kinds and rt.frame_id > f0
    snap = rt.observe()
    assert snap["window"]["title"] == "Other" and snap["frame_id"] == rt.frame_id
    assert snap["events"] and rt.observe()["events"] == []       # cursor-based: only NEW events
    assert "image" not in json.dumps(snap) and len(json.dumps(snap)) < 4000    # compact


def test_modal_detected_and_check_interrupts():
    rt, s, clk = make_rt()
    rt.tick_fast(); rt.tick_medium()
    tok = rt.assume()
    assert rt.check(tok) is None
    s.fg = {"hwnd": 9, "title": "Save changes?", "rect": {"x": 700, "y": 400, "w": 500, "h": 250}, "exe": "app.exe"}
    rt.tick_fast()
    assert rt.check(tok) == "modal_appeared"


def test_focus_lost_and_profile_change_and_navigation():
    rt, s, clk = make_rt()
    rt.tick_fast(); rt.tick_medium()
    tok = rt.assume()
    s.fg = {**s.fg, "hwnd": 2, "rect": {"x": 0, "y": 0, "w": 1920, "h": 1080}}
    rt.tick_fast()
    assert rt.check(tok) == "focus_lost"
    s.browser = {"profile": "Personal", "directory": "Default", "url": "https://notion.so", "hwnd": 2}
    rt.tick_medium()
    tok = rt.assume()
    s.browser = {**s.browser, "url": "https://render.com"}
    rt.tick_medium()
    assert rt.check(tok) == "navigation"
    tok = rt.assume()
    s.browser = {"profile": "XeroCore", "directory": "Profile 3", "url": "https://render.com", "hwnd": 2}
    rt.tick_medium()
    assert rt.check(tok) == "profile_changed"


def test_stale_state_and_unhealthy_screen_change():
    rt, s, clk = make_rt()
    rt.tick_fast(); rt.tick_medium()
    tok = rt.assume()
    clk.adv(30)
    assert rt.check(tok) == "stale_state"
    assert rt.observe()["interaction"]["stale"] is True
    rt.tick_fast(); rt.tick_medium()
    tok = rt.assume()
    s.image = img(color=20)                                # radical change
    rt.tick_medium()
    assert rt.check(tok) == "screen_changed"


def test_cursor_only_and_animation_do_not_interrupt_or_bump():
    rt, s, clk = make_rt()
    rt.tick_fast(); rt.tick_medium()
    tok, f = rt.assume(), rt.frame_id
    s.cursor = (30, 30)
    s.image = img(boxes=[(10, 10, 30, 30, 0)])
    rt.tick_fast(); rt.tick_medium()
    assert rt.check(tok) is None and rt.frame_id == f


def test_deep_triggered_by_uncertainty_not_every_tick():
    rt, s, clk = make_rt()
    for _ in range(5):
        rt.tick_fast(); rt.tick_medium()
    assert s.deep_calls == 0 and rt._deep_reason == "foreground window changed"   # initial sight only
    rt.tick_deep()
    assert rt._deep_reason is None and s.deep_calls == 1
    s.image = img(color=20)
    rt.tick_medium()
    assert rt._deep_reason and "screen" in rt._deep_reason


def test_background_loop_runs_and_stops():
    rt, s, clk = make_rt()
    import time as _t
    rt._clock = _t.monotonic; rt.events._clock = _t.time
    rt.intervals.update(fast=0.02, medium=0.05)
    rt.session("a")
    assert rt.start()
    _t.sleep(0.4)
    rt.stop()
    assert rt.stats["fast"] >= 3 and rt.stats["medium"] >= 1 and rt.stats["errors"] == 0


def test_action_lifecycle_events_and_verification_semantics():
    rt, s, clk = make_rt()
    rt.action_started({"type": "click", "target": "Save", "text": "secret-password-value-long"}, expect="Saved visible")
    assert rt.observe()["interaction"]["current_action"]["text"] != "secret-password-value-long"
    rt.action_finished({"status": "fired_unverified", "proof": {"until_ok": None, "how": "not_requested"}})
    kinds = [e["kind"] for e in rt.events.since(0)["events"]]
    assert "VERIFICATION_PASSED" not in kinds and "VERIFICATION_FAILED" not in kinds
    rt.action_finished({"status": "hit", "proof": {"until_ok": True, "how": "uia"}})
    assert "VERIFICATION_PASSED" in [e["kind"] for e in rt.events.since(0)["events"]]


# ---------------------------------------------------------------- stream
def stream(rt, script, cond=lambda c, t: {"until_ok": True, "how": "ocr"}, cancelled=lambda: False):
    it = iter(script)
    seen = []

    def act(step):
        seen.append(step)
        r = next(it)
        return r() if callable(r) else r
    runner = StreamRunner(rt, act, cond, cancelled=cancelled, settle_s=0.0, sleep=lambda s: None, clock=rt._clock)
    return runner, seen


HIT = {"status": "hit", "proof": {"until_ok": True, "how": "uia"}}


def test_stream_completes_verified_steps():
    rt, s, clk = make_rt()
    runner, seen = stream(rt, [HIT, {"status": "ok"}, HIT])
    r = runner.run([{"type": "click", "target": "A", "until": "x"}, {"type": "wait_until", "condition": "list visible"},
                    {"type": "click", "target": "B", "until": "y"}])
    assert r["status"] == "completed" and r["completed"] == 3 and r["model_round_trips"] == 1
    assert len(seen) == 2                                    # wait_until handled by the runtime, not the engine
    assert all(st["verified"] and st["evidence"] for st in r["steps"])


def test_stream_stops_on_unverified_click_without_evidence():
    rt, s, clk = make_rt()
    runner, seen = stream(rt, [{"status": "fired_unverified", "proof": {"until_ok": None, "how": "not_requested"}}, HIT])
    r = runner.run([{"type": "click", "target": "A"}, {"type": "click", "target": "B", "until": "y"}])
    assert r["status"] == "unverified" and len(seen) == 1 and r["steps"][0]["verified"] is False


def test_stream_accepts_weak_state_evidence_and_labels_it():
    rt, s, clk = make_rt()

    def click():
        s.image = img(color=20)                           # the click visibly changed the whole screen
        return {"status": "fired_unverified", "proof": {"until_ok": None, "how": "not_requested"}}
    runner, _ = stream(rt, [click])
    r = runner.run([{"type": "click", "target": "A"}])
    st = r["steps"][0]
    assert r["status"] == "completed" and st["evidence_strength"] == "weak" and "weak" in st["evidence"][0]


def test_stream_interrupts_before_fire_on_modal_and_does_not_fire():
    rt, s, clk = make_rt()

    def first():
        s.fg = {"hwnd": 9, "title": "Are you sure?", "rect": {"x": 700, "y": 400, "w": 500, "h": 250}}
        return HIT
    runner, seen = stream(rt, [first, HIT])
    r = runner.run([{"type": "click", "target": "A", "until": "x"}, {"type": "click", "target": "Delete", "until": "y"}])
    assert r["status"] == "interrupted" and r["interruption"]["reason"] == "modal_appeared"
    assert r["interruption"]["fired"] is False and len(seen) == 1
    assert r["completed"] == 1


def test_stream_reacquires_once_on_soft_change_then_continues():
    rt, s, clk = make_rt()

    def first():
        s.image = img(color=20)
        return HIT
    runner, seen = stream(rt, [first, HIT])
    s.elements = [el("B", 10, 10)]
    r = runner.run([{"type": "click", "target": "A", "until": "x"}, {"type": "click", "target": "B", "until": "y"}])
    assert r["status"] == "completed" and r["reacquired"] == 1 and s.deep_calls >= 1


def test_stream_cancel_and_failure_are_structured():
    rt, s, clk = make_rt()
    flag = {"c": False}

    def first():
        flag["c"] = True
        return HIT
    runner, seen = stream(rt, [first, HIT], cancelled=lambda: flag["c"])
    r = runner.run([{"type": "click", "target": "A", "until": "x"}, {"type": "click", "target": "B", "until": "y"}])
    assert r["status"] == "cancelled" and len(seen) == 1
    rt2, *_ = make_rt()
    runner, _ = stream(rt2, [{"status": "stale_frame", "error": "moved"}])
    r = runner.run([{"type": "click", "target": "A"}])
    assert r["status"] == "failed" and r["interruption"]["reason"] == "stale_frame"


def test_stream_wait_until_timeout_fails_honestly():
    rt, s, clk = make_rt()
    runner, _ = stream(rt, [], cond=lambda c, t: {"until_ok": False, "how": "ocr"})
    r = runner.run([{"type": "wait_until", "condition": "never"}])
    assert r["status"] == "failed" and r["steps"][0]["status"] == "timeout"


# ---------------------------------------------------------------- chrome
def write_local_state(tmp_path, profiles):
    (tmp_path / "Local State").write_text(json.dumps({"profile": {"info_cache": profiles}}))
    return tmp_path


PROFILES = {"Profile 11": {"name": "Kartik Raghav", "user_name": "k@example.com", "gaia_name": "Kartik"},
            "Default": {"name": "Personal", "user_name": ""},
            "Profile 3": {"name": "XeroCore", "user_name": "x@example.com"}}


class ChromeWorld:
    """windows list + foreground + launch/focus effects, all simulated."""
    def __init__(self):
        self.windows = []
        self.fg = 0
        self.launched = []
        self.launch_result = None       # callable(world, argv) to mutate windows
        self.focus_ok = True

    def focus(self, hwnd):
        if self.focus_ok:
            self.fg = hwnd
        return self.focus_ok

    def launch(self, argv):
        self.launched.append(argv)
        if self.launch_result:
            self.launch_result(self, argv)
        return True


def registry(tmp_path, world):
    write_local_state(tmp_path, PROFILES)
    return ChromeRegistry(user_data_dir=tmp_path, config_path=tmp_path / "none.json",
                          windows_provider=lambda: [dict(w) for w in world.windows],
                          foreground_provider=lambda: world.fg)


def go(reg, world, url="https://dashboard.render.com", profile="Kartik Raghav", **kw):
    return chrome_flow.go(reg, url, profile, focus=world.focus, launch=world.launch, exe="chrome.exe",
                          sleep=lambda s: None, polls=3, **kw)


def test_parse_window_title():
    t = parse_window_title("Dashboard | Render - Google Chrome - Kartik Raghav")
    assert t == {"page": "Dashboard | Render", "profile_label": "Kartik Raghav", "is_chrome_title": True}
    assert parse_window_title("Untitled - Notepad")["is_chrome_title"] is False


def test_registry_discovers_and_resolves_profiles(tmp_path):
    reg = registry(tmp_path, ChromeWorld())
    assert reg.resolve("XeroCore")["profile"].directory == "Profile 3"
    assert reg.resolve("profile 11")["profile"].name == "Kartik Raghav"
    assert reg.resolve("k@example.com")["profile"].directory == "Profile 11"
    assert reg.resolve("Kart")["match"] == "prefix"
    assert reg.resolve("nobody")["reason"] == "unknown_profile"
    assert all(p.purpose == "" for p in reg.profiles())         # purpose is never fabricated


def test_registry_purpose_only_from_config(tmp_path):
    write_local_state(tmp_path, PROFILES)
    (tmp_path / "cfg.json").write_text(json.dumps({"profiles": {"XeroCore": {"purpose": "infra", "aliases": ["xc"]}}}))
    reg = ChromeRegistry(user_data_dir=tmp_path, config_path=tmp_path / "cfg.json")
    assert reg.resolve("xc")["profile"].purpose == "infra"
    assert next(p for p in reg.profiles() if p.name == "Personal").purpose == ""


def test_already_open_requires_profile_and_destination(tmp_path):
    w = ChromeWorld()
    w.windows = [{"hwnd": 10, "title": "Dashboard | Render - Google Chrome - Kartik Raghav"}]
    r = go(registry(tmp_path, w), w)
    assert r["status"] == "ok" and r["already_open"] is True and r["verified"] is True
    assert r["observed_profile"]["directory"] == "Profile 11" and r["observed_profile"]["verified"]
    assert r["stages"] == ["profile_verified", "window_found", "tab_found", "destination_visible"]
    assert r["destination_evidence"] == "window_title" and r["url_verified"] is False
    assert w.launched == []


def test_false_positive_already_open_other_profile_is_not_reported(tmp_path):
    """The audited bug: Render open in ANOTHER profile must not count."""
    w = ChromeWorld()
    w.windows = [{"hwnd": 10, "title": "Dashboard | Render - Google Chrome - Personal"},
                 {"hwnd": 11, "title": "Notion - Google Chrome - Personal"}]
    w.fg = 11
    r = go(registry(tmp_path, w), w)
    assert r["already_open"] is False and r["status"] != "ok"
    assert w.launched and w.launched[0][1] == "--profile-directory=Profile 11"
    assert r["status"] == "profile_mismatch" and r["other_profile_has_destination"] is True
    assert r["requested_profile"]["directory"] == "Profile 11" and r["observed_profile"]["name"] == "Personal"


def test_launch_then_navigation_verified(tmp_path):
    w = ChromeWorld()

    def launch(world, argv):
        world.windows.append({"hwnd": 20, "title": "Dashboard | Render - Google Chrome - Kartik Raghav"})
    w.launch_result = launch
    r = go(registry(tmp_path, w), w)
    assert r["status"] == "ok" and r["already_open"] is False and r["launched"] is True
    for s in ("launch_requested", "launch_started", "profile_verified", "navigation_verified", "destination_visible"):
        assert s in r["stages"]
    assert "--profile-directory=Profile 11" in w.launched[0]


def test_profile_window_exists_but_destination_unconfirmed(tmp_path):
    w = ChromeWorld()
    w.windows = [{"hwnd": 10, "title": "New Tab - Google Chrome - Kartik Raghav"}]
    r = go(registry(tmp_path, w), w)
    assert r["status"] == "navigation_unverified" and r["verified"] is False and r["already_open"] is False
    assert "navigation_started" in r["stages"] and "destination_visible" not in r["stages"]


def test_focus_failure_is_reported_not_success(tmp_path):
    w = ChromeWorld()
    w.windows = [{"hwnd": 10, "title": "Dashboard | Render - Google Chrome - Kartik Raghav"}]
    w.focus_ok = False
    r = go(registry(tmp_path, w), w)
    assert r["status"] == "focus_failed" and r["already_open"] is False


def test_unknown_profile_and_xerocore_resolution(tmp_path):
    w = ChromeWorld()
    r = go(registry(tmp_path, w), w, profile="Ghost")
    assert r["status"] == "profile_unknown" and "XeroCore" in r["candidates"] and w.launched == []
    w.windows = [{"hwnd": 30, "title": "Home - Google Chrome - XeroCore"}]
    w.launch_result = lambda world, argv: world.windows.append({"hwnd": 31, "title": "Blueprints - Render - Google Chrome - XeroCore"})
    r = go(registry(tmp_path, w), w, profile="XeroCore")
    assert r["requested_profile"]["directory"] == "Profile 3" and r["status"] == "ok"


def test_until_unmet_downgrades_status(tmp_path):
    w = ChromeWorld()
    w.windows = [{"hwnd": 10, "title": "Dashboard | Render - Google Chrome - Kartik Raghav"}]
    r = go(registry(tmp_path, w), w, until="url contains blueprints")
    assert r["status"] == "launched_unverified" and r["already_open"] is False


def test_registry_view_lists_active_and_window(tmp_path):
    w = ChromeWorld()
    w.windows = [{"hwnd": 10, "title": "Dashboard - Google Chrome - XeroCore"}]
    w.fg = 10
    rows = {r["name"]: r for r in registry(tmp_path, w).registry_view()}
    assert rows["XeroCore"]["active"] and rows["XeroCore"]["window"] == 10 and rows["XeroCore"]["tab"] == "Dashboard"
    assert rows["Personal"]["open"] is False and rows["Personal"]["last_observed"] is None
