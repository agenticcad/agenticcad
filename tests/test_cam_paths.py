"""Toolpath quality after the boat latch job on the Z1 (2026-11-02): tabs ramped on and off on straight runs, finishing
contours in one full-depth pass, pocket rings lobe by lobe with stay-down links, helical bores, feeds that follow the
engagement (Program.fit_feeds) and a real rating for chamfer tools."""
import math

import pytest

import cad_kernel as ck
import cam_kernel as cam
import cam_sim
from cam_data import Library

PLATE = '''
plate = Box(60, 40, 10, align=(Align.CENTER, Align.CENTER, Align.MIN))
plate = plate - Pos(-15, 0, 10) * Cylinder(4, 6, align=(Align.CENTER, Align.CENTER, Align.MAX))     # Ø8 x 6 blind hole
plate = plate - Pos(15, 0, 10) * Cylinder(2.5, 4, align=(Align.CENTER, Align.CENTER, Align.MAX))   # Ø5 x 4 blind hole
result = {"Plate": plate}
'''
# a dumbbell pocket: two 14 x 14 lobes joined by a 5 mm neck, 3 deep
DUMBBELL = '''
pk = Pos(-12, 0, 10) * Box(14, 14, 6) + Pos(12, 0, 10) * Box(14, 14, 6) + Pos(0, 0, 10) * Box(12, 5, 6)
plate = Box(60, 40, 10, align=(Align.CENTER, Align.CENTER, Align.MIN)) - pk
result = {"Plate": plate}
'''


@pytest.fixture(scope="module")
def machines(tmp_path_factory):
    return Library(tmp_path_factory.mktemp("lib")).machines()


def flat(d=3.175, **kw):
    a = dict(rpm=13000, feed=290, plunge=90, stepdown=0.8, stepover=0.4)
    a.update(kw)
    return cam.Tool(2, f"{d} flat", "flat", d, 1, 12, **a)


def z1(machines, model, top=0.0):
    return cam.Setup(machines["Makera Z1"], cam.Stock.from_model(model, margin=4, top=top))


def _feed_descents(op):
    """(angle deg, vertical rate mm/min) of every feed move that goes down."""
    out, prev = [], None
    for mv in op.moves:
        if prev is not None and mv[0] == cam.FEED and prev[3] - mv[3] > 1e-6:
            L = math.dist(prev[1:4], mv[1:4]); dz = prev[3] - mv[3]
            out.append((math.degrees(math.asin(min(1.0, dz / L))), mv[4] * dz / L))
        prev = mv
    return out


def test_tabs_ramp_on_and_off_on_straight_runs(machines):
    """Tab ends dropped up to 2.7 mm within one 0.5 mm sample at the cutting feed (79-88°, 3x the plunge rate) and the
    tabs were spread by length, landing on corner arcs."""
    st = cam.Setup(machines["Makera Z1"], cam.Stock.block(80, 60, 10, bottom=0))
    t = flat()
    part = cam.rect(-20, -15, 20, 15).buffer(4, join_style=1)               # straights joined by R4 arcs
    op = cam.contour(st, t, part, z_top=4, z_bottom=0, stepdown=0.5, tabs=4, tab_height=2, tab_width=6)
    d = _feed_descents(op)
    assert d and max(a for a, _ in d) <= max(cam.TAB_RAMP_DEG, cam.RAMP_DEG) + 0.5
    assert max(v for _, v in d) <= t.plunge + 1e-3                           # never down faster than the plunge feed
    # each tab: a flat at the tab top at least tab_width long, on a straight edge of the offset ring (|x| = 20 + 4 + r
    # or |y| = 15 + 4 + r), with the ramps outside it
    r = t.radius
    k0 = next(k for k, mv in enumerate(op.moves) if mv[3] < 2.0 - 1e-6)     # from the first pass below the tab top
    flat_pts = [mv for mv in op.moves[k0:] if mv[0] == cam.FEED and abs(mv[3] - 2.0) < 1e-6]
    on_edge = [mv for mv in flat_pts if abs(abs(mv[1]) - (24 + r)) < 1e-3 or abs(abs(mv[2]) - (19 + r)) < 1e-3]
    assert len(on_edge) == len(flat_pts) and flat_pts
    deep = min(mv[3] for mv in op.moves)
    lap = [mv for mv in op.moves[k0:] if mv[0] == cam.FEED]
    runs, cur = [], []
    for mv in lap:                                                           # runs of the deepest lap at the tab top
        if abs(mv[3] - 2.0) < 1e-6:
            cur.append(mv)
        elif cur:
            runs.append(cur); cur = []
    lengths = [sum(math.dist(a[1:3], b[1:3]) for a, b in zip(rr, rr[1:])) for rr in runs]
    assert sum(1 for ln in lengths if ln >= 6 - 1e-3) >= 4 and deep == pytest.approx(0.0)
    # placement on its own: a ring with long straights gets every tab centre on a straight
    ring = cam._ring_coords(part.buffer(r, join_style=1).exterior, True)
    cs = cam._tab_centres(ring, 4, 6, 2 / math.tan(math.radians(cam.TAB_RAMP_DEG)))
    ln = cam.LineString(ring + [ring[0]])
    for c in cs:
        for s in (c - 3, c, c + 3):
            p = ln.interpolate(s % ln.length)
            assert abs(abs(p.x) - (24 + r)) < 1e-3 or abs(abs(p.y) - (19 + r)) < 1e-3


def test_finish_contour_after_a_roughing_band_is_one_full_depth_pass(machines):
    """Finish walls ran 7-11 laps at 0.8 DOC to clean 0.2 radially (36-56 % air): after a band that left the skin,
    the contour is one pass at full depth, entered straight down in the cleared band; a contour in fresh stock (a slot)
    keeps its stepdown laps."""
    st = cam.Setup(machines["Makera Z1"], cam.Stock.block(80, 60, 10, bottom=0))
    t = flat()
    cam.reset_overrides()
    part = cam.rect(-15, -10, 15, 10)
    band = part.buffer(6, join_style=2).difference(part)
    cam.pocket(st, t, band, z_top=10, z_bottom=5, stock_to_leave=0.2)
    fin = cam.contour(st, t, part, z_top=10, z_bottom=5, side="outside")
    assert fin.params["finish"] == 1 and fin.params["passes"] == 1 and fin.params["ap"] == pytest.approx(5.0)
    assert min(mv[3] for mv in fin.moves) == pytest.approx(5.0)
    assert sum(1 for mv in fin.moves if mv[0] == cam.FEED and abs(mv[3] - 5.0) < 1e-6) >= 4
    assert any(mv[0] == cam.PLUNGE and mv[3] == pytest.approx(5.0) for mv in fin.moves)    # down in the cleared band
    # below the band (fresh stock beside the wall: a slot) the stepdown stays
    slot = cam.contour(st, t, part, z_top=5, z_bottom=2, side="outside")
    assert "finish" not in slot.params and slot.params["passes"] == math.ceil(3 / t.stepdown)
    # forced either way
    assert cam.contour(st, t, part, z_top=10, z_bottom=5, finish=False).params["passes"] == math.ceil(5 / t.stepdown)
    assert cam.contour(st, t, part, z_top=5, z_bottom=2, finish=True).params["passes"] == 1
    # an explicit stepdown keeps the laps
    assert "finish" not in cam.contour(st, t, part, z_top=10, z_bottom=5, stepdown=1.0).params
    # a new script forgets what the last one cleared
    cam.reset_overrides()
    assert "finish" not in cam.contour(st, t, part, z_top=10, z_bottom=5).params


def test_pocket_lobes_one_entry_each_and_no_retract_inside(machines):
    """The flange relief retracted 168 times to safe Z and re-ramped (14.9 min of 3° ramps): rings are now cut lobe by
    lobe, links stay down or lift just over the cleared floor, and the pocket is still exact."""
    model = ck.run_script(DUMBBELL)
    st = z1(machines, model)
    t = flat()
    region = cam.free_region(model.shape, 10, 7, within=cam.rect(-19, -7, 19, 7))
    op = cam.pocket(st, t, region, z_top=10, z_bottom=7, strategy="rings")
    levels = op.params["passes"]
    safe = sum(1 for mv in op.moves if mv[0] == cam.RAPID and mv[3] >= st.safe_z - 1e-6)
    assert safe <= 2                                                         # in at the start, out at the end
    entries = sum(1 for mv in op.moves if mv[0] == cam.PLUNGE)
    assert entries <= 3 * levels                                             # a lobe or two per level, not a ramp per ring
    assert max(a for a, _ in _feed_descents(op)) <= cam.RAMP_DEG + 0.2
    p = cam.Program(st); p.add(op)
    r = cam_sim.simulate(p, part=model)
    assert r.stats["gouge"]["cells"] == 0
    assert r.stats["left"]["area_mm2"] < 20, r.stats["left"]


def test_helical_bore(machines):
    """Four Ø8 register holes took 17.8 min as pockets (8.7 min of it ramping at F90): a helix with the tool over the
    centre (no core pin), a second out to the wall, a flat circle at the bottom."""
    model = ck.run_script(PLATE)
    st = z1(machines, model)
    t = flat()
    cam.reset_overrides()
    rings = cam.pocket(st, t, cam.circle(-15, 0, 8), z_top=10, z_bottom=4, strategy="rings")
    cam.reset_overrides()                                                    # (the rings would count as cleared stock)
    b = cam.bore(st, t, -15, 0, 8, z_top=10, z_bottom=4)
    assert b.kind == "pocket" and b.params["bored"] == 1
    d = _feed_descents(b)
    assert max(a for a, _ in d) <= cam.RAMP_DEG + 0.05 and max(v for _, v in d) <= t.plunge + 1e-3
    assert sum(1 for mv in b.moves if mv[0] == cam.FEED and abs(mv[3] - 4) < 1e-6) >= 24     # the bottom circle
    assert b.time_minutes(st.machine) < rings.time_minutes(st.machine)
    # auto: a Ø5 hole (1.6x) is bored too; the sim finds the holes cut exactly (no pin, no gouge)
    small = cam.pocket(st, t, cam.circle(15, 0, 5), z_top=10, z_bottom=6)
    assert small.params.get("bored") == 1
    p = cam.Program(st); p.add(b); p.add(small)
    r = cam_sim.simulate(p, part=model, resolution=0.1)
    assert r.stats["gouge"]["cells"] == 0
    assert r.stats["left"]["area_mm2"] < 1.0, r.stats["left"]
    # outside 1.3-3x the tool: rings, said so
    big = cam.bore(st, t, 0, 0, 12, z_top=10, z_bottom=8)
    assert "bored" not in big.params and any("pocketed with rings" in w for w in big.warnings)


def test_fit_feeds_follows_engagement(machines):
    """Slots ran at 2.5x the tool's rating and skims at 10 % of it at the same feed: fit_feeds keeps the planned chip
    load (with radial chip thinning), within the caps, leaves descents and explicit feed overrides alone."""
    st = cam.Setup(machines["Makera Z1"], cam.Stock.block(80, 60, 10, bottom=0))
    t = flat()
    top = st.stock.top

    def line_op(name, y, depth, y_from=None, z=None):
        op = cam.Op(name, "pocket", t)
        pb = cam.PathBuilder(st, t, op)
        pb._add(cam.RAPID, -30, y, top + 3)
        pb._add(cam.PLUNGE, -30, y, top - depth, t.plunge)
        pb._add(cam.FEED, 30, y, top - depth, t.feed)
        pb._add(cam.FEED, 30, y, top + 1, t.feed)                         # up out of the cut
        pb._add(cam.FEED, -30, y, top + 1, t.feed)                         # a feed in the air
        pb._add(cam.RAPID, -30, y, top + 3)
        return op
    slot = line_op("slot", 10, 0.8)
    skim = cam.Op("skim", "pocket", t)                                    # 0.3 mm of the side of the slot: light cut
    pb = cam.PathBuilder(st, t, skim)
    pb._add(cam.RAPID, -30, 10 + 0.3, top + 3)
    pb._add(cam.PLUNGE, -30, 10 + 0.3, top - 0.8, t.plunge)
    pb._add(cam.FEED, 30, 10 + 0.3, top - 0.8, t.feed)
    pb._add(cam.RAPID, 30, 10.3, top + 3)
    fixed = line_op("fixed", -10, 0.8)
    fixed.overrides = {"feed": 290.0}
    p = cam.Program(st)
    for o in (slot, skim, fixed):
        p.add(o)
    r = cam_sim.simulate(p, resolution=0.1)
    res = p.fit_feeds(r)
    rct = lambda a: 1 / math.sqrt(1 - (1 - 2 * a) ** 2)
    assert slot.moves[2][4] == pytest.approx(290 / rct(0.4), rel=0.05)    # a full slot: the planned chip load
    assert slot.moves[1][4] == t.plunge                                   # the plunge is left alone
    assert slot.moves[4][4] == pytest.approx(2 * 290)                     # the air feed goes to the cap
    f1 = skim.moves[2][4]                       # 0.3 mm of a 3.175 cutter (the 0.1 mm cells read it as 0.3-0.4): x1.4-1.65
    assert 1.3 * 290 < f1 <= 290 * rct(0.3 / t.diameter) / rct(0.4) + 1
    assert all(m[4] == 290 or m[0] != cam.FEED for m in fixed.moves) and "fixed" not in res["ops"]
    assert res["ops"]["slot"]["minutes_after"] < res["ops"]["slot"]["minutes_before"]
    # load: softer halves... down to the floor; per-op load wins over the program's
    r = cam_sim.simulate(p, resolution=0.1)
    slot.params["load"] = 0.7
    p.fit_feeds(r, load=1.3)
    assert slot.moves[2][4] == pytest.approx(0.7 * 290 / rct(0.4), rel=0.05)
    assert skim.moves[2][4] == pytest.approx(min(2 * 290, 1.3 * f1), rel=0.02)


def test_load_override_and_script_value(machines):
    st = cam.Setup(machines["Makera Z1"], cam.Stock.block(80, 60, 10, bottom=0))
    t = flat()
    cam.reset_overrides()
    cam.overrides({"P": {"load": 0.8}})
    op = cam.pocket(st, t, cam.rect(-10, -10, 10, 10), z_top=10, z_bottom=9, name="P")
    assert op.params["load"] == 0.8 and op.cut_overrides["load"] == 0.8 and "load" in op.cut_defaults
    q = cam.pocket(st, t, cam.rect(-10, -10, 10, 10), z_top=10, z_bottom=9, name="Q", load=1.2)
    assert q.params["load"] == 1.2 and cam.unused_overrides() == []
    with pytest.raises(cam.CamError):
        cam.overrides({"P": {"load": 5}})
    cam.reset_overrides()


def test_chamfer_tool_rating_is_its_cone_lap():
    """The 90° chamfer bit was rated 12 mm3/min (tip x stepdown) and ran 2-10x that without trouble."""
    c = cam.Tool(4, "chamfer", "chamfer", 3.175, 1, 2, rpm=13000, feed=300, plunge=50, stepdown=0.2, angle=90, sharp=True)
    assert c.rated_mrr() == pytest.approx(0.2 * 3.175 / 2 * 300)
    v = cam.Tool(5, "V", "vbit", 3.175, 2, 3, feed=150, stepdown=0.05, angle=30, tip_diameter=0.1)
    assert v.rated_mrr() == pytest.approx(0.05 * 3.075 * 150)
    assert flat().rated_mrr() == pytest.approx(0.8 * 0.4 * 3.175 * 290)


def test_tall_tabs_on_a_short_ring_keep_full_depth_cut_and_never_gouge(machines):
    """A 30 x 30 square with 5 mm tabs: 15° ramps (18.7 mm each side) would leave no full-depth cut between four tabs;
    the ramps steepen (or tabs drop) with a warning, and the remainder lap ends where the lead-out starts (it once
    jumped across the part from a tab: a 4.4 mm gouge)."""
    model = ck.run_script("result = {'P': Box(30, 30, 10, align=(Align.MIN, Align.MIN, Align.MIN))}")
    st = cam.Setup(machines["Makera Z1"], cam.Stock.from_model(model, margin=8, material="aluminum"))
    assert st.stock.material == "aluminium"
    t = flat()
    for th in (2, 5, 8):
        op = cam.contour(st, t, cam.rect(0, 0, 30, 30), z_top=10, z_bottom=-0.2, tabs=4, tab_height=th, tab_width=6,
                         bodies="P")
        assert op.params["bodies"] == ["P"] and op.params["tab_top"] == pytest.approx(-0.2 + th)
        assert op.params["tab_width"] == 6 and len(op.params["tab_xy"]) == 1
        n = len(op.params["tab_xy"][0])
        if th >= 5:
            assert any(w.startswith("tabs:") for w in op.warnings)
        # along the deepest lap: full-depth stretches between tabs, at least 2 x Ø long each
        k0 = max(k for k, mv in enumerate(op.moves) if mv[0] == cam.FEED and mv[3] > -0.2 + th - 1e-6 and k < len(op.moves) - 30)
        runs, cur = [], 0.0
        prev = None
        for mv in op.moves:
            if prev is not None and mv[0] == cam.FEED and abs(mv[3] + 0.2) < 1e-6 and abs(prev[3] + 0.2) < 1e-6:
                cur += math.dist(prev[1:3], mv[1:3])
            elif cur:
                runs.append(cur); cur = 0.0
            prev = mv
        assert sum(1 for x in runs if x >= 2 * t.diameter - 0.5) >= n
        p = cam.Program(st); p.add(op)
        assert p.material == "aluminium"
        r = cam_sim.simulate(p, part=model)
        assert r.stats["gouge"]["cells"] == 0
        if hasattr(r, "verdict"):
            assert not [e for e in r.verdict()["errors"] if e["code"] in ("GOUGE", "RAPID", "COLLISION")]
