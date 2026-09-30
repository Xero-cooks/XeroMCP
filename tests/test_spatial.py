"""XeroSpatial unit + engine tests. Platform independent (fake desktop).

Run:  python -m pytest tests/test_spatial.py -q
"""
from __future__ import annotations

import ctypes
import time

import pytest

from modules.spatial import protocol
from modules.spatial.displays import Display, display_for_point, display_for_rect, virtual_bounds
from modules.spatial.frame import VisionCache, SpatialFrame, signature
from modules.spatial.geometry import (FrameTransform, Grid, Rect, address_for_point, local_to_point,
                                      point_to_local, resolve_address)
from modules.spatial.locks import LockStore, TargetLock
from modules.spatial.memory import SpatialMemory, is_persistable
from modules.spatial.protocol import Action, TargetSpec
from modules.spatial.safe_point import hazard_zones, safe_point
from modules.spatial import tool as spatial_tool

from fake_world import FakeWorld, hide, make_engine

RESOLUTIONS = [(1920, 1080), (2560, 1440), (3840, 2160), (1366, 768), (1536, 864)]
SCALES = [1.0, 1.25, 1.5, 1.75, 2.0]


# ==============================================================================
# geometry
# ==============================================================================

@pytest.mark.parametrize("w,h", RESOLUTIONS)
def test_grid_tiles_screen_exactly(w, h):
    g = Grid(Rect(0, 0, w, h), 16, 8)
    cells = list(g.cells())
    assert len(cells) == 128
    assert sum(r.area for _, r in cells) == w * h            # no gaps, no overlap
    for x in (0, w // 3, w - 1):
        for y in (0, h // 2, h - 1):
            cid = g.cell_at(x, y)
            assert cid is not None and g.cell_rect(cid).contains(x, y)
    assert g.cell_at(w, 0) is None and g.cell_at(0, h) is None and g.cell_at(-1, 5) is None


@pytest.mark.parametrize("w,h", RESOLUTIONS)
def test_cell_boundaries_are_half_open(w, h):
    g = Grid(Rect(0, 0, w, h), 16, 8)
    a = g.cell_rect("A1")
    assert g.cell_at(a.right - 1, a.bottom - 1) == "A1"
    assert g.cell_at(a.right, a.y) == "B1"
    assert g.cell_at(a.x, a.bottom) == "A2"
    assert g.cell_rect("P8").right == w and g.cell_rect("P8").bottom == h


@pytest.mark.parametrize("w,h", RESOLUTIONS)
@pytest.mark.parametrize("origin", [(0, 0), (-2560, 0), (1920, -400), (-1920, -1080)])
def test_local_round_trip(w, h, origin):
    g = Grid(Rect(origin[0], origin[1], w, h), 16, 8)
    for cid in ("A1", "G4", "P8", "H5"):
        r = g.cell_rect(cid)
        assert local_to_point(r, 0, 0) == (r.x, r.y)
        px, py = local_to_point(r, 64, 64)
        assert (px, py) == (r.right - 1, r.bottom - 1)       # never spills into next cell
        for lx, ly in ((32, 32), (10, 50), (63.5, 0.5)):
            px, py = local_to_point(r, lx, ly)
            assert r.contains(px, py) and g.cell_at(px, py) == cid
            bx, by = point_to_local(r, px, py)
            assert abs(bx - lx) <= 64 / r.w + 1e-6 * 64 + 64 / r.w and abs(by - ly) <= 64 / r.h * 2


def test_local_clamps_out_of_range():
    r = Rect(100, 100, 120, 135)
    assert local_to_point(r, -10, 999) == (100, 234)


@pytest.mark.parametrize("w,h", RESOLUTIONS)
def test_refinement_address(w, h):
    g = Grid(Rect(0, 0, w, h), 16, 8)
    a = resolve_address(g, "G4/B3")
    cell = g.cell_rect("G4")
    assert cell.intersect(a.region) == a.region and a.region.area < cell.area
    px, py = local_to_point(a.region, 32, 32)
    addr, lx, ly = address_for_point(g, px, py, levels=2)
    assert addr == "G4/B3"
    with pytest.raises(ValueError):
        resolve_address(g, "Q9")


@pytest.mark.parametrize("scale", SCALES)
def test_display_logical_physical(scale):
    d = Display(1, Rect(-2560, 0, 2560, 1440), Rect(-2560, 0, 2560, 1400), scale, False)
    lx, ly = d.physical_to_logical(-1280, 720)
    assert d.logical_to_physical(lx, ly) == (-1280, 720)
    assert d.dpi == round(96 * scale)


def test_multi_monitor_negative_origin():
    left = Display(1, Rect(-2560, -200, 2560, 1440), Rect(-2560, -200, 2560, 1400), 1.0, False)
    main = Display(0, Rect(0, 0, 3840, 2160), Rect(0, 0, 3840, 2100), 1.5, True)
    ds = [main, left]
    assert display_for_point(ds, -10, 500) is left
    assert display_for_point(ds, 10, 500) is main
    assert display_for_rect(ds, Rect(-100, 100, 400, 300)) is main   # larger overlap wins
    vb = virtual_bounds(ds)
    assert (vb.x, vb.y, vb.right, vb.bottom) == (-2560, -200, 3840, 2160)
    g = Grid(left.bounds, 16, 8)
    px, py = local_to_point(g.cell_rect("A1"), 0, 0)
    assert (px, py) == (-2560, -200)
    assert left.taskbar_rects() and all(t.y >= 1200 for t in left.taskbar_rects())


def test_frame_transform_downscaled_capture():
    tr = FrameTransform(-1920, 0, 2.0, 2.0)          # 3840 px surface captured at 1920
    assert tr.to_physical(0, 0) == (-1920, 0)
    assert tr.to_physical(960, 540) == (0, 1080)
    ix, iy = tr.to_image(0, 1080)
    assert (ix, iy) == (960, 540)


# ==============================================================================
# protocol
# ==============================================================================

def test_protocol_forms():
    a, = protocol.parse("CLICK G4 32 48")
    assert a.verb == "click" and a.target.cell == "G4" and (a.target.x, a.target.y) == (32, 48)
    a, = protocol.parse('click "Save" IN P1 UNTIL "Saved"')
    assert a.target.label == "Save" and a.target.cell == "P1" and a.until == '"Saved"'.strip('"') or a.until
    a, = protocol.parse("CLICK 1:G4/B3")
    assert a.target.display == 1 and a.target.cell == "G4/B3"
    a, = protocol.parse('CLICK "G4"')                   # quoted -> label, not a cell
    assert a.target.label == "G4" and not a.target.cell
    a, = protocol.parse("DRAG B2 10 10 TO C5 32 32")
    assert a.target2.cell == "C5"
    steps = protocol.parse('CLICK G4; TYPE "a;b"\nKEY CTRL+L')
    assert [s.verb for s in steps] == ["click", "type", "key"] and steps[1].text == "a;b"
    a, = protocol.parse("SCROLL H4 -3")
    assert a.amount == -3 and a.target.cell == "H4"


@pytest.mark.parametrize("bad", ["", "JUMP G4", "CLICK", "WAIT", 'TYPE "x', "CLICK G4 32 48 99",
                                 "; ".join(["WAIT 1"] * 21)])
def test_protocol_rejects(bad):
    with pytest.raises(protocol.ProtocolError):
        protocol.parse(bad)


def test_tool_build_action_validation():
    a = spatial_tool.build_action("click", target={"cell": "g4", "x": 10, "y": 20})
    assert a.target.cell == "G4" and a.target.x == 10
    a = spatial_tool.build_action("right", target="1:B2")
    assert a.verb == "right_click" and a.target.display == 1
    for kw in ({"cell": "G4", "x": 70, "y": 1}, {"cell": "G4", "x": 5}, {"cell": "Z99"}):
        with pytest.raises(protocol.ProtocolError):
            spatial_tool.build_action("click", **kw)
    with pytest.raises(protocol.ProtocolError):
        spatial_tool.build_action("drag", cell="A1")


# ==============================================================================
# safe point
# ==============================================================================

def test_safe_point_centre_of_plain_box():
    sp = safe_point(Rect(100, 100, 200, 40))
    assert Rect(100, 100, 200, 40).contains(sp.x, sp.y)
    assert abs(sp.x - 200) <= 20 and abs(sp.y - 120) <= 6 and sp.quality > 0.8


def test_safe_point_tiny_box_stays_inside():
    box = Rect(10, 10, 3, 3)
    sp = safe_point(box)
    assert box.contains(sp.x, sp.y)


def test_safe_point_avoids_overlap_and_hazards():
    box = Rect(0, 0, 200, 40)
    close_btn = Rect(90, 0, 110, 40)           # right half covered by another control
    sp = safe_point(box, obstacles=[close_btn])
    assert not close_btn.contains(sp.x, sp.y) and box.contains(sp.x, sp.y)
    parent = Rect(-10, -10, 400, 400)          # containers are ignored
    sp2 = safe_point(box, obstacles=[parent])
    assert abs(sp2.x - 100) <= 20
    hz = hazard_zones({"x": 0, "y": 0, "w": 800, "h": 600}, 1.0, maximized=False)
    sp3 = safe_point(Rect(0, 200, 30, 30), hazards=hz)
    assert not any(z.contains(sp3.x, sp3.y) for _, z in hz if _ == "resize")


def test_safe_point_rejects_empty_box():
    with pytest.raises(ValueError):
        safe_point(Rect(0, 0, 0, 10))


# ==============================================================================
# locks / memory / cache
# ==============================================================================

def _lock(label="Save", sig=b"\x10" * 64, hwnd=1, wr=None):
    return TargetLock(label=label, rect=Rect(10, 10, 50, 20), cell="A1", confidence=0.9, source="uia",
                      frame_id="f", hwnd=hwnd, win_rect=wr or {"x": 0, "y": 0, "w": 800, "h": 600},
                      region_sig=sig)


def test_lock_ttl_window_and_pixels():
    ls = LockStore(ttl=0.2)
    ls.put(_lock())
    wr = {"x": 0, "y": 0, "w": 800, "h": 600}
    assert ls.get("save", 1, wr) is not None                     # case/space-insensitive
    assert ls.get("Save", 2, wr) is None                         # other window -> dropped
    ls.put(_lock())
    assert ls.get("Save", 1, {"x": 5, "y": 0, "w": 800, "h": 600}) is None   # window moved
    ls.put(_lock())
    lk = ls.get("Save", 1, wr)
    assert ls.revalidate(lk, b"\x10" * 64)["valid"]
    assert not ls.revalidate(lk, b"\xf0" * 64)["valid"]
    assert ls.get("Save", 1, wr) is None                         # pixel change invalidated it
    ls.put(_lock())
    time.sleep(0.25)
    assert ls.get("Save", 1, wr) is None                         # ttl


def test_memory_privacy_and_priors(tmp_path):
    assert is_persistable("Save", "button")
    assert not is_persistable("john.doe@example.com", "button")
    assert not is_persistable("Save", "text")                    # only controls, never content
    assert not is_persistable("4111 1111 1111 1111", "edit")
    m = SpatialMemory(persist_path=str(tmp_path / "m.json"))
    wr = {"x": 100, "y": 100, "w": 1000, "h": 800}
    assert not m.learn("app.exe", "Save", Rect(1000, 150, 40, 20), wr, "button", verified=False)
    assert m.learn("app.exe", "Save", Rect(1000, 150, 40, 20), wr, "button", verified=True)
    moved = {"x": 300, "y": 300, "w": 1000, "h": 800}              # window moved: prior follows it
    p = m.priors("app.exe", "Save", moved)
    assert p and p[0]["rect"].contains(1220, 360)
    m2 = SpatialMemory(persist_path=str(tmp_path / "m.json"))    # persisted
    assert m2.priors("app.exe", "save", wr)


def test_vision_cache_rules():
    img = FakeWorld(w=320, h=160).render()
    s = Rect(0, 0, 320, 160)
    f = SpatialFrame(surface=s, grid=Grid(s, 16, 8), transform=FrameTransform(0, 0, 1, 1), image=img,
                     window={"hwnd": 1, "rect": {"x": 0, "y": 0, "w": 320, "h": 160}})
    c = VisionCache(ttl=0.3)
    c.put(f)
    assert c.get(1, {"x": 0, "y": 0, "w": 320, "h": 160}) is f
    assert c.get(2, None) is None and c.last_invalidation == "window_changed"
    assert c.get(1, {"x": 1, "y": 0, "w": 320, "h": 160}) is None
    c.invalidate("x")
    assert c.peek() is None and c.get_by_id(f.frame_id) is f      # still provable by id
    time.sleep(0.35)
    c.put(f)
    assert c.get(1, None) is None and c.last_invalidation == "ttl"


# ==============================================================================
# engine end-to-end on a fake desktop
# ==============================================================================

def _world(**kw):
    w = FakeWorld(**kw)
    return w


def _click(eng, target=None, **kw):
    kw.setdefault("timeout_ms", 400)
    a = spatial_tool.build_action(kw.pop("action", "click"), target=target, **{k: kw.pop(k) for k in list(kw)
                                  if k in ("cell", "x", "y", "near", "target2", "to_cell", "to_x", "to_y",
                                           "text", "keys", "amount")})
    return eng.act(a, **kw)


def test_uia_hit_without_ocr():
    w = _world()
    w.add("Continue", 900, 500, on_click=hide)
    eng = make_engine(w)
    r = _click(eng, "Continue", until="Continue gone")
    assert r["status"] == "hit", r
    assert r["resolved"]["source"] == "uia" and r["motion"].startswith("ghost")
    assert not any(c[0] == "ocr" for c in w.calls)                # UIA answered -> no OCR paid
    assert "timing_ms" in r and "resolve.uia" in r["timing_ms"]


def test_uia_unavailable_falls_back_to_ocr():
    w = _world(uia_enabled=False)
    el = w.add("Accept all", 300, 700, on_click=hide)
    eng = make_engine(w)
    r = _click(eng, "Accept all", until="Accept all gone")
    assert r["status"] == "hit", r
    assert r["resolved"]["source"] in ("ocr", "ocr_refined")
    x, y = r["point"]["x"], r["point"]["y"]
    assert el.rect.contains(x, y)
    assert r["motion"] == "warp"


def test_ocr_and_uia_unavailable_is_not_found_not_a_guess():
    w = _world(uia_enabled=False, ocr_enabled=False)
    w.add("Accept", 300, 700)
    eng = make_engine(w)
    r = _click(eng, "Accept")
    assert r["status"] == "not_found"
    assert not any(c[0] == "pointer" for c in w.calls)


def test_vision_unavailable_cell_click_still_works_label_does_not():
    w = _world(uia_enabled=False, capture_enabled=False)
    w.add("Menu", 10, 10)
    eng = make_engine(w)
    r = _click(eng, "Menu")
    assert r["status"] == "vision_unavailable" and not any(c[0] == "pointer" for c in w.calls)
    r = _click(eng, cell="A1", x=32, y=32)
    assert r["status"] == "fired_unverified"                     # no until -> never "hit"
    assert r["proof"]["how"] == "not_requested" and r["proof"]["until_ok"] is None


@pytest.mark.parametrize("res,origin", [((1920, 1080), (0, 0)), ((2560, 1440), (0, 0)),
                                        ((3840, 2160), (0, 0)), ((1920, 1080), (-1920, 0))])
def test_cell_click_lands_in_cell(res, origin):
    w = _world(w=res[0], h=res[1], origin=origin)
    eng = make_engine(w)
    r = _click(eng, cell="G4", x=32, y=48)
    g = Grid(w.bounds, 16, 8)
    x, y = r["point"]["x"], r["point"]["y"]
    assert g.cell_at(x, y) == "G4"
    assert (x, y) == local_to_point(g.cell_rect("G4"), 32, 48)
    assert r["resolved"]["cell"] == "G4"


def test_stale_frame_id_refused():
    w = _world()
    btn = w.add("Buy", 800, 400, on_click=hide)
    eng = make_engine(w)
    obs = eng.act(Action("observe"))
    fid = obs["frame"]["frame_id"]
    cell = Grid(w.bounds, 16, 8).cell_at(*btn.rect.center)
    # screen changes between observe and click
    btn.rect = Rect(1500, 900, 120, 32)
    r = _click(eng, cell=cell, frame_id=fid)
    assert r["status"] == "stale_frame", r
    assert not any(c[0] == "pointer" for c in w.calls)
    r = _click(eng, cell=cell, frame_id="deadbeef00")
    assert r["status"] == "stale_frame"


def test_fresh_frame_id_snaps_to_observed_element():
    w = _world()
    btn = w.add("Buy", 850, 455, 100, 36)                       # sits on H4's centre
    eng = make_engine(w)
    fid = eng.act(Action("observe"))["frame"]["frame_id"]
    g = Grid(w.bounds, 16, 8)
    cell = g.cell_at(*btn.rect.center)
    assert cell == "H4"
    r = _click(eng, cell=cell, frame_id=fid, dry_run=True)
    assert r["status"] == "dry_run" and btn.rect.contains(r["point"]["x"], r["point"]["y"])
    assert not any(c[0] == "pointer" for c in w.calls)


def test_cached_label_revalidated_after_screen_change():
    w = _world(uia_enabled=False)
    btn = w.add("Next", 400, 300, on_click=hide)
    eng = make_engine(w)
    eng.act(Action("observe"))
    btn.rect = Rect(1200, 800, 120, 32)                          # moved after observation
    r = _click(eng, "Next", until="Next gone")
    assert r["status"] == "hit", r
    assert btn.rect.contains(r["point"]["x"], r["point"]["y"])     # new position, not the cached one


def test_failed_click_is_fired_unverified_and_retried_once_only_if_untouched():
    w = _world()
    w.add("Save", 500, 500)                                      # click does nothing
    eng = make_engine(w)
    r = _click(eng, "Save", until="Save gone", motion="human", timeout_ms=1500)
    assert r["status"] == "fired_unverified"
    clicks = [c for c in w.calls if c[0] == "pointer"]
    assert len(clicks) == 2 and r["tries"] == 2                  # one safe retry, never a storm


def test_swallowed_click_retry_then_hit():
    w = _world(uia_enabled=False, swallow_clicks=1)
    w.add("Open", 500, 500, on_click=hide)
    eng = make_engine(w)
    r = _click(eng, "Open", until="Open gone", timeout_ms=1500)
    assert r["status"] == "hit" and r["tries"] == 2 and r["proof"].get("retried")


def test_toggle_never_reclicked():
    w = _world()
    w.add("Dark mode", 500, 500, ctype="checkbox")
    eng = make_engine(w)
    r = _click(eng, "Dark mode", until="Dark mode gone", timeout_ms=800)
    assert r["status"] == "fired_unverified" and r["tries"] == 1


def test_label_not_in_cell_refuses():
    w = _world()
    w.add("Delete", 1700, 900)                                   # far from G4
    eng = make_engine(w)
    r = _click(eng, "Delete", cell="G4", x=32, y=32)
    assert r["status"] == "refused_low_confidence", r
    assert not any(c[0] == "pointer" for c in w.calls)


def test_ambiguous_refuses_then_near_disambiguates():
    w = _world(uia_enabled=False)
    w.add("Save", 200, 200)
    right = w.add("Save", 1500, 800)
    dlg = w.add("Export dialog", 1480, 740)
    right.on_click = lambda world, el: setattr(dlg, "visible", False)
    eng = make_engine(w)
    r = _click(eng, "Save")
    assert r["status"] == "ambiguous" and len(r["candidates"]) >= 2
    assert not any(c[0] == "pointer" for c in w.calls)
    r = _click(eng, "Save", near="Export dialog", until="Export dialog gone", timeout_ms=800)
    assert r["status"] == "hit", r
    assert right.rect.contains(r["point"]["x"], r["point"]["y"])


def test_focus_failure():
    w = _world(focus_ok=False)
    w.add("OK", 100, 100)
    eng = make_engine(w)
    r = _click(eng, "OK", window="Notepad")
    assert r["status"] == "focus_failed" and not any(c[0] == "pointer" for c in w.calls)


def test_occluded_by_other_window():
    w = _world()
    btn = w.add("Pay", 800, 500)
    w.occluder = btn.rect.inflate(30)
    eng = make_engine(w)
    r = _click(eng, "Pay")
    assert r["status"] == "occluded" and not any(c[0] == "pointer" for c in w.calls)


def test_input_blocked_reported():
    w = _world(uia_enabled=False, input_blocked=True)
    w.add("Run", 800, 500)
    eng = make_engine(w)
    r = _click(eng, "Run")
    assert r["status"] == "input_blocked" and "hint" in r


def test_typing_refused_when_focus_lost():
    w = _world()
    eng = make_engine(w)

    class Switcher(type(eng.b)):
        def foreground(self):
            info = super().foreground()
            self.n = getattr(self, "n", 0) + 1
            if self.n > 1:
                info["hwnd"] = 555                            # another window stole focus
            return info
    eng.b = Switcher(w)
    r = eng.act(Action("type", text="secret"))
    assert r["status"] == "focus_lost" and not any(c[0] == "type" for c in w.calls)


def test_cancel_only_affects_earlier_submissions():
    w = _world()
    w.add("Go", 100, 100)
    eng = make_engine(w)
    before = time.monotonic()
    eng.cancel()
    r = eng.act(spatial_tool.build_action("click", target="Go"), submitted_at=before, timeout_ms=400)
    assert r["status"] == "cancelled"
    r = eng.act(spatial_tool.build_action("click", target="Go"), timeout_ms=400)
    assert r["status"] != "cancelled"                              # stale cancel does not leak


def test_lock_reused_on_second_click_and_dropped_on_change():
    w = _world()
    el = w.add("Refresh", 600, 600)

    def toggle_text(world, e):                                    # proof: status label appears
        world.add("Refreshed", 600, 700) if not world.find("Refreshed") else None
    el.on_click = toggle_text
    eng = make_engine(w)
    r1 = _click(eng, "Refresh", until="Refreshed")
    assert r1["status"] == "hit"
    w.find("Refreshed").visible = False
    w.calls.clear()
    r2 = _click(eng, "Refresh", until="Refreshed")
    assert r2["status"] == "hit" and r2["resolved"]["source"] == "lock", r2
    assert not any(c[0] == "uia_find" for c in w.calls)
    el.color = (250, 250, 250)                                        # control repainted -> lock invalid
    w.find("Refreshed").visible = False
    r3 = _click(eng, "Refresh", until="Refreshed")
    assert r3["resolved"]["source"] != "lock"


def test_multi_action_run_stops_on_failure():
    w = _world()
    w.add("Name", 300, 300)
    eng = make_engine(w)
    r = eng.run('CLICK "Name"; TYPE "abc"; CLICK "Missing"; KEY ENTER', timeout_ms=400)
    assert r["total"] == 4 and r["completed"] == 2 and r["status"] == "not_found"
    assert ("type", "abc") in w.calls and not any(c[0] == "keys" for c in w.calls)
    assert eng.run("FLY G4")["status"] == "bad_request"


def test_drag_and_scroll_and_move():
    w = _world()
    eng = make_engine(w)
    r = _click(eng, action="drag", cell="B2", to_cell="C5", x=10, y=10, to_x=32, to_y=32)
    p = [c for c in w.calls if c[0] == "pointer"][-1]
    g = Grid(w.bounds, 16, 8)
    assert p[1] == "drag" and g.cell_at(p[2], p[3]) == "B2" and g.cell_at(p[4], p[5]) == "C5"
    r = _click(eng, action="move", cell="H4")
    assert r["status"] == "hit" and r["proof"]["how"] == "cursor_position"
    r = eng.act(Action("scroll", amount=-3), timeout_ms=300)
    assert [c for c in w.calls if c[0] == "pointer"][-1][1] == "scroll"


def test_debug_image_and_observe_grid():
    w = _world()
    w.add("File", 10, 10)
    eng = make_engine(w)
    r = eng.act(Action("observe"), debug=True)
    assert r["status"] == "ok" and r["image"]["mimeType"] == "image/jpeg" if "mimeType" in r["image"] else r["image"]
    assert any("File" in str(v) for v in r["cells"].values())
    r = _click(eng, cell="A1", dry_run=True, debug=True)
    assert "image" in r


def test_multi_display_cell_addressing():
    from modules.spatial.displays import Display as D
    w = _world()
    w.extra_displays = [D(1, Rect(-2560, 0, 2560, 1440), Rect(-2560, 0, 2560, 1400), 1.0, False)]
    eng = make_engine(w)
    r = _click(eng, "1:A1", x=0, y=0, dry_run=True)
    assert r["point"] == {"x": -2560, "y": 0}
    r = _click(eng, "5:A1", dry_run=True)
    assert r["status"] == "bad_request"


def test_telemetry_recorded_per_stage():
    w = _world()
    w.add("OK", 100, 100, on_click=hide)
    eng = make_engine(w)
    _click(eng, "OK", until="OK gone")
    s = spatial_tool.stats(eng)
    assert s["status"] == "ok" and "click" in s["telemetry"]


# ==============================================================================
# keyboard layer (keys.py)
# ==============================================================================

def test_input_struct_layout_is_40_bytes_on_x64():
    """The old INPUT union only held KEYBDINPUT (sizeof 32 on x64) and Windows
    rejected every SendInput call. Rebuild the documented layout portably."""
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        pytest.skip("x64 layout check")
    ULONG_PTR = ctypes.c_uint64
    DWORD, WORD, LONG = ctypes.c_uint32, ctypes.c_uint16, ctypes.c_int32

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = (("dx", LONG), ("dy", LONG), ("mouseData", DWORD), ("dwFlags", DWORD),
                    ("time", DWORD), ("dwExtraInfo", ULONG_PTR))

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = (("wVk", WORD), ("wScan", WORD), ("dwFlags", DWORD), ("time", DWORD),
                    ("dwExtraInfo", ULONG_PTR))

    class U(ctypes.Union):
        _fields_ = (("mi", MOUSEINPUT), ("ki", KEYBDINPUT))

    class INPUT(ctypes.Structure):
        _fields_ = (("type", DWORD), ("u", U))

    class BadU(ctypes.Union):
        _fields_ = (("ki", KEYBDINPUT),)

    class BadINPUT(ctypes.Structure):
        _fields_ = (("type", DWORD), ("u", BadU))
    assert ctypes.sizeof(INPUT) == 40 and ctypes.sizeof(BadINPUT) == 32
    src = open(__import__("pathlib").Path(__file__).resolve().parents[1] / "modules" / "keys.py", encoding="utf-8").read()
    assert '("mi", MOUSEINPUT)' in src and '("ki", KEYBDINPUT)' in src


@pytest.mark.parametrize("combo,n", [("ctrl+l", 2), ("CTRL+SHIFT+T", 3), ("ctrl++", 2), ("ctrl-l", 2),
                                     ("alt+f4", 2), ("enter", 1), ("ctrl+1", 2), ("f12", 1)])
def test_parse_chord(combo, n):
    from modules import keys
    vks, err = keys.parse_chord(combo)
    assert not err and len(vks) == n


def test_parse_chord_unknown():
    from modules import keys
    vks, err = keys.parse_chord("ctrl+banana")
    assert vks == [] and "banana" in err
