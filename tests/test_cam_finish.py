"""Finishing passes by default (2026-11-02, the machinist's ask after the latch job): pockets rough leaving 0.2 mm on
the walls and 0.1 mm on the floor, then cut the walls in one full-depth pass and the floor with a tidy pattern -
one-way rows along X by default (one lay, all climb, lifted just clear of the floor to go back), or offset rings /
a spiral. Through-cuts have no floor to finish, bored holes finish with their last circle, an explicit stock_to_leave
is a roughing op, and a floor a later op cuts deeper is not finished there."""
import math

import pytest

import cad_kernel as ck
import cam_kernel as cam
import cam_sim
from cam_data import Library

# a 40 x 24 pocket 3 deep with a 8 x 8 island in it, in a 70 x 50 x 10 plate
POCKET = '''
pk = Pos(0, 0, 10) * Box(40, 24, 6) - Pos(8, 0, 10) * Box(8, 8, 6)
plate = Box(70, 50, 10, align=(Align.CENTER, Align.CENTER, Align.MIN)) - pk
result = {"Plate": plate}
'''


PLAIN = POCKET.replace(" - Pos(8, 0, 10) * Box(8, 8, 6)", "")
# a 40 x 24 step 2 deep, its left 24 mm 2 deeper again
STEP = '''
pk = Pos(0, 0, 10) * Box(40, 24, 4) + Pos(-8, 0, 8) * Box(24, 24, 4)
plate = Box(70, 50, 10, align=(Align.CENTER, Align.CENTER, Align.MIN)) - pk
result = {"Plate": plate}
'''


@pytest.fixture(scope="module")
def machines(tmp_path_factory):
    return Library(tmp_path_factory.mktemp("lib")).machines()


@pytest.fixture(scope="module")
def model():
    return ck.run_script(POCKET)


def flat(d=3.175, **kw):
    a = dict(rpm=13000, feed=290, plunge=90, stepdown=0.8, stepover=0.4)
    a.update(kw)
    return cam.Tool(2, f"{d} flat", "flat", d, 1, 12, **a)


def setup(machines):
    return cam.Setup(machines["Makera Z1"], cam.Stock.block(70, 50, 10, bottom=0))


REGION = cam.rect(-20, -12, 20, 12).difference(cam.rect(4, -4, 12, 4))


def _block(op, which="floor"):
    """The moves of the finishing ranges: 'floor' (no planned ap) or 'walls' (ap_moves)."""
    walls = {(a, b) for a, b, _ in op.params.get("ap_moves", [])}
    out = []
    for a, b in op.params.get("finish_moves", []):
        if ((a, b) in walls) == (which == "walls"):
            out.append(op.moves[a:b])
    return out


def _sim(st, ops, model):
    p = cam.Program(st)
    for o in ops:
        p.add(o)
    return cam_sim.simulate(p, part=model)


def test_pocket_finishes_walls_and_floor_by_default(machines, model):
    st = setup(machines); t = flat()
    op = cam.pocket(st, t, REGION, z_top=10, z_bottom=7)
    pr = op.params
    assert pr["finish_wall"] == 0.2 and pr["finish_floor"] == 0.1 and pr["finish_pattern"] == "parallel"
    assert pr["finish_angle"] == 0 and pr["finish_stepover"] == pytest.approx(0.45 * t.diameter, abs=1e-3)
    # the walls: one full-depth pass (planned ap 3 for the simulation's BITE check), the outer wall and the island
    (w0, w1, ap), = [x for x in pr["ap_moves"]][:1]
    assert ap == pytest.approx(3.0)
    walls = _block(op, "walls")[0]
    assert {round(m[3], 3) for m in walls if m[0] == cam.FEED} >= {7.0}
    # the floor: rows along +X only (one lay, climb: the next row is on the left = +Y), at the floor depth
    floor = _block(op, "floor")[0]
    cuts = [(a, b) for a, b in zip(floor, floor[1:]) if b[0] == cam.FEED and abs(a[3] - 7) < 1e-6 and abs(b[3] - 7) < 1e-6
            and math.dist(a[1:3], b[1:3]) > 0.5]
    assert len(cuts) >= 10
    assert all(b[1] > a[1] and abs(b[2] - a[2]) < 1e-6 for a, b in cuts)
    # going back: lifted just clear of the 0.1 floor stock, never dragged across the finished floor
    backs = [b for a, b in zip(floor, floor[1:]) if b[0] == cam.RAPID and b[3] < st.safe_z - 1e-6]
    assert backs and all(7.15 < b[3] <= 7.5 + 1e-6 for b in backs) and sum(abs(b[3] - 7.2) < 1e-6 for b in backs) >= 10
    # the rows come down where the wall pass already cut to the floor: no descent deeper than the lift
    for a, b in zip(floor, floor[1:]):
        if b[0] == cam.PLUNGE and a[3] < st.safe_z - 1e-6:
            assert a[3] - b[3] <= 0.5 + 1e-6
    # the rows stay 0.05 off the finished walls and the island
    dom = REGION.buffer(-(t.radius + 0.05 - 0.01))
    assert all(dom.covers(cam.Point(m[1], m[2])) for m in floor if m[0] == cam.FEED and abs(m[3] - 7) < 1e-6)
    # the walls before the floor (the rows then start in cleared ground)
    assert pr["finish_moves"][0][0] < pr["finish_moves"][-1][0] and pr["ap_moves"][0][0] == pr["finish_moves"][0][0]
    # cut to size: nothing left on walls or floor, no gouge; and no worse than one roughing strategy
    r = _sim(st, [op], model)
    r0 = _sim(st, [cam.pocket(st, t, REGION, z_top=10, z_bottom=7, finish=False)], model)
    assert r.stats["gouge"]["cells"] == 0
    assert r.stats["left"]["area_mm2"] <= max(r0.stats["left"]["area_mm2"], 1.0), (r.stats["left"], r0.stats["left"])
    # the planned full-depth wall pass is not a BITE (cam_sim reads ap_moves)
    assert not [e for e in r.verdict()["errors"] if e["code"] == "BITE"]


@pytest.mark.parametrize("pattern", ["offset", "spiral"])
def test_floor_patterns_offset_and_spiral(machines, pattern):
    model = ck.run_script(PLAIN)
    st = setup(machines); t = flat()
    region = cam.rect(-20, -12, 20, 12)                                       # no island: the spiral is one piece
    op = cam.pocket(st, t, region, z_top=10, z_bottom=7, finish_pattern=pattern)
    assert op.params["finish_pattern"] == pattern and "finish_angle" not in op.params or op.params["finish_angle"] is None
    floor = _block(op, "floor")[0]
    at_floor = [m for m in floor if m[0] == cam.FEED and abs(m[3] - 7) < 1e-6]
    assert len(at_floor) > 20
    inner = [m for m in floor if m[0] in (cam.FEED, cam.PLUNGE)]
    # one entry (ramped down along the middle ring from just above the floor stock), then it stays down
    rapids_low = [m for m in floor if m[0] == cam.RAPID and m[3] < st.safe_z - 1e-6]
    if pattern == "spiral":
        assert len(rapids_low) <= 1
    descents = [(a, b) for a, b in zip(floor, floor[1:]) if b[3] < a[3] - 1e-6 and b[0] == cam.FEED]
    assert descents and all(math.degrees(math.atan2(a[3] - b[3], math.dist(a[1:3], b[1:3]) + 1e-12)) <= cam.RAMP_DEG + 0.3
                            for a, b in descents)
    r = _sim(st, [op], model)
    assert r.stats["gouge"]["cells"] == 0 and r.stats["left"]["area_mm2"] < 1.0, r.stats["left"]
    assert inner


def test_spiral_falls_back_to_offset_round_an_island(machines):
    st = setup(machines)
    op = cam.pocket(st, flat(), REGION, z_top=10, z_bottom=7, finish_pattern="spiral")
    assert any("spiral" in w for w in op.warnings)


def test_finish_angle_turns_the_rows(machines):
    st = setup(machines)
    op = cam.pocket(st, flat(), cam.rect(-20, -12, 20, 12), z_top=10, z_bottom=7, finish_angle=90)
    floor = _block(op, "floor")[0]
    cuts = [(a, b) for a, b in zip(floor, floor[1:]) if b[0] == cam.FEED and abs(a[3] - 7) < 1e-6 and abs(b[3] - 7) < 1e-6
            and math.dist(a[1:3], b[1:3]) > 0.5]
    assert cuts and all(abs(b[1] - a[1]) < 1e-3 and b[2] > a[2] for a, b in cuts)   # along +Y, the next row on the left (-X)


def test_what_gets_finished(machines):
    st = setup(machines); t = flat()
    # a through-cut has no floor: the walls only
    thru = cam.pocket(st, t, cam.rect(-20, -12, 20, 12), z_top=10, z_bottom=-0.2)
    assert thru.params["finish_pattern"] is None and thru.params["ap_moves"] and len(_block(thru, "floor")) == 0
    # an explicit stock_to_leave is a roughing op for a later finish: nothing finished (finish="floor" asks for the floor)
    rough = cam.pocket(st, t, cam.rect(-20, -12, 20, 12), z_top=10, z_bottom=7, stock_to_leave=0.2)
    assert "finish_moves" not in rough.params
    assert rough.moves == cam.pocket(st, t, cam.rect(-20, -12, 20, 12), z_top=10, z_bottom=7, stock_to_leave=0.2, finish=False).moves
    fl = cam.pocket(st, t, cam.rect(-20, -12, 20, 12), z_top=10, z_bottom=7, stock_to_leave=0.2, finish="floor")
    assert len(_block(fl, "floor")) == 1 and "ap_moves" not in fl.params
    # walls only / floor only
    w = cam.pocket(st, t, cam.rect(-20, -12, 20, 12), z_top=10, z_bottom=7, finish="walls")
    assert w.params["ap_moves"] and not _block(w, "floor") and min(m[3] for m in w.moves) == pytest.approx(7)
    # a bored hole finishes with its last circle: no extra passes
    b = cam.pocket(st, t, cam.circle(0, 0, 8), z_top=10, z_bottom=7)
    assert b.params.get("bored") == 1 and "finish_moves" not in b.params
    # a skim no deeper than twice the floor allowance is its own floor finish
    sk = cam.pocket(st, t, cam.rect(-20, -12, 20, 12), z_top=10, z_bottom=9.85)
    assert not _block(sk, "floor")
    # small tools leave less
    s2 = cam.pocket(st, flat(2.0), cam.rect(-20, -12, 20, 12), z_top=10, z_bottom=7)
    assert s2.params["finish_wall"] == pytest.approx(0.16) and s2.params["finish_floor"] == pytest.approx(0.08)
    with pytest.raises(cam.CamError):
        cam.pocket(st, t, cam.rect(-20, -12, 20, 12), z_top=10, z_bottom=7, finish="sides")


def test_floor_cut_deeper_later_is_not_finished(machines):
    """B1/B2 on the latch: a floor finish over ground the next op cuts deeper is wasted - Program.add rebuilds the
    earlier op's floor block over what stays (and drops it when nothing does)."""
    st = setup(machines); t = flat()
    upper = cam.pocket(st, t, cam.rect(-20, -12, 20, 12), z_top=10, z_bottom=8)
    before = cam.Op("x", "x", t, moves=list(upper.moves)).time_minutes(st.machine)
    p = cam.Program(st)
    p.add(upper)
    p.add(cam.pocket(st, t, cam.rect(-20, -12, 4, 12), z_top=8, z_bottom=6))      # 60 % of it goes deeper
    assert upper.params["finish_trimmed_mm2"] > 300
    assert upper.time_minutes(st.machine) < before
    floor = _block(upper, "floor")[0]
    assert all(m[1] > 4 - t.radius - 0.2 for m in floor if m[0] == cam.FEED and abs(m[3] - 8) < 1e-6)
    r = cam_sim.simulate(p, part=ck.run_script(STEP))
    # (what's left is the step's two inside corners, which a round tool can't reach: 2 x (1 - pi/4) r^2)
    assert r.stats["gouge"]["cells"] == 0 and r.stats["left"]["area_mm2"] < 3.0, r.stats["left"]
    # all of it cut deeper: the block is dropped
    p2 = cam.Program(st)
    up2 = p2.add(cam.pocket(st, t, cam.rect(-20, -12, 20, 12), z_top=10, z_bottom=8))
    p2.add(cam.pocket(st, t, cam.rect(-20, -12, 20, 12), z_top=8, z_bottom=6))
    assert not _block(up2, "floor") and up2.params["ap_moves"]


def test_fit_feeds_keeps_finish_passes_at_the_planned_feed_and_smooths(machines, model):
    st = setup(machines); t = flat()
    p = cam.Program(st)
    op = p.add(cam.pocket(st, t, REGION, z_top=10, z_bottom=7))
    r = cam_sim.simulate(p, part=model)
    p.fit_feeds(r)
    fin = set()
    for a, b in op.params["finish_moves"]:
        fin.update(range(a, b))
    assert all(op.moves[k][4] <= t.feed + 0.1 for k in fin if op.moves[k][0] == cam.FEED)


def test_face_ends_with_a_one_way_finishing_pass(machines):
    st = setup(machines); t = flat()
    op = cam.face(st, t, z_top=10, z_bottom=9)
    assert op.params["finish_floor"] == 0.1 and op.params["finish_moves"]
    a, b = op.params["finish_moves"][0]
    fin = op.moves[a:b]
    rows = [(p, q) for p, q in zip(fin, fin[1:]) if q[0] == cam.FEED and abs(p[3] - 9) < 1e-6 and abs(q[3] - 9) < 1e-6
            and math.dist(p[1:3], q[1:3]) > 1]
    assert rows and all(q[1] > p[1] for p, q in rows)                          # every row the same way: one lay
    ys = sorted({round(p[2], 3) for p, _ in rows})
    assert max(b_ - a_ for a_, b_ in zip(ys, ys[1:])) <= 0.45 * t.diameter + 1e-3
    assert any(abs(m[3] - 9.1) < 1e-6 for m in op.moves[:a])                  # the levels above stop 0.1 short
    zz = cam.face(st, t, z_top=10, z_bottom=9, finish=False)
    assert "finish_moves" not in zz.params and min(m[3] for m in zz.moves) == pytest.approx(9)
    skim = cam.face(st, t, z_top=10, z_bottom=9.9)                           # a skim is the finishing pass alone
    assert skim.params["finish_floor"] == 0.0 and skim.params["finish_moves"][0][0] <= 2
