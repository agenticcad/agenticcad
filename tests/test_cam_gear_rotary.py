"""4th-axis gear teeth with a disc-type module cutter (tool type gear_cutter, cut from the side of the blank) and
rotary_profile (any polar cross-section as continuous-A rings), checked in the radial material-removal simulation."""
import math
import re

import numpy as np
import pytest
import shapely

import cad_kernel as ck
import cam_kernel as cam
import cam_sim
import tool_shape as ts
from cam_data import Library

M, N, W = 1.0, 20, 8.0            # module 1, 20 teeth, 8 mm face: tip Ø22, root Ø17.5


@pytest.fixture(scope="module")
def machine(tmp_path_factory):
    return Library(tmp_path_factory.mktemp("lib")).machines()["Makera Z1 + 4th axis"]


@pytest.fixture(scope="module")
def gear():
    """spur_gear is Z up with tooth 0 on +X; Rot(0, 90, 0) puts its axis on +X (x = 0..8), its old +X on -Z."""
    return ck.run_script(f"result = {{'G': Rot(0, 90, 0) * spur_gear({M:g}, {N}, {W:g})}}")


def cutter(teeth_min=N, **kw):
    base = dict(number=9, name="m1 cutter", type="gear_cutter", diameter=40, flutes=12, flute_length=0, rpm=600, feed=60, plunge=30,
                stepdown=0.8, stepover=0.3, module=M, cutter_teeth_min=teeth_min, shank_diameter=3.175)
    base.update(kw)
    return cam.Tool(**base)


def flat(d=3.175, n=1):
    return cam.Tool(n, f"{d} flat", "flat", d, 2, 12, rpm=12000, feed=600, plunge=150, stepdown=0.8, stepover=0.4)


def ball(d=3.175, n=2):
    return cam.Tool(n, f"{d} ball", "ball", d, 2, 12, rpm=12000, feed=500, plunge=150, stepdown=0.8, stepover=0.15)


def blank(machine):
    return cam.Setup(machine, cam.Stock.cylinder(2 * M * (N / 2 + 1), W, x0=0), rotary=True)


# ------------------------------------------------------------------ the tool
def test_gear_cutter_shape_follows_the_involute_space():
    t = cutter()
    assert t.spec() == "m1 · 20° · 20T cutter · Ø40 x 2.75 · arbor Ø3.175"
    assert cutter(26, thickness=2.5).spec().startswith("m1 · 20° · No.5 (26–34T) · Ø40 x 2.50")
    sh = t.shape
    nut_h, th = 4.0, sh.p["thickness"]
    assert sh.cut_from == nut_h and sh.flute_length == pytest.approx(nut_h + th) and sh.cut_radius == 20.0
    # the form: the space of a 20-tooth m1 gear, root (narrow) at the periphery, tip line (wide) at the gullet R - 2.25 m
    depth, half, root_half = ts.gear_space_form(M, N, 20.0)
    # (the tip corner of the space is r_a cos(theta_a) along the centre line: a little less than the 2.25 m working depth)
    assert th == pytest.approx(2 * half[-1]) and 2.6 < th < 2.9 and 2.25 * M - 0.1 < depth[-1] < 2.25 * M
    mid = nut_h + th / 2
    r = sh.radius_at(np.array([1.0, mid, mid + root_half * 0.9, mid + th / 2 - 1e-3, nut_h + th + 0.5]))
    assert r[0] == pytest.approx(5.0) and r[1] == pytest.approx(20.0) and r[2] == pytest.approx(20.0)
    assert r[3] == pytest.approx(20.0 - depth[-1], abs=0.01) and r[4] == pytest.approx(3.175 / 2)
    # the nut does not cut: the lowest cutting point at the axis is the disc's underside
    assert sh.cut_height(np.array([2.0]))[0] == pytest.approx(nut_h) and math.isinf(sh.cut_height(np.array([20.5]))[0])
    b0, b1, rho = sh.cut_slabs(0.02)
    assert b0[0] == pytest.approx(nut_h) and b1[-1] >= nut_h + th - 0.003 and rho.max() == 20.0 and len(rho) > 50
    # a slab never under-states the tool and over-states it by at most 0.02 plus one 2 µm sample's radius change (the radial
    # flank near the root runs almost along the cutter's radius: 15 mm of radius per mm of height)
    for z0, z1, rr in zip(b0, b1, rho):
        zz = np.linspace(z0, z1, 9)
        assert (sh.radius_at(zz) <= rr + 1e-9).all() and sh.radius_at(zz).min() > rr - 0.06
    assert sh.payload()["cut_from"] == nut_h and sh.payload()["gear"]["gullet_diameter"] == pytest.approx(2 * (20 - depth[-1]), abs=0.01)
    for bad, why in ((dict(module=0), "module"), (dict(cutter_teeth_min=0), "tooth count"), (dict(shank_diameter=0), "shank"),
                     (dict(thickness=1.0), "narrower"), (dict(diameter=12), "too small")):
        with pytest.raises(ts.ToolShapeError, match=why):
            cutter(**bad).shape


# ------------------------------------------------------------------ gear teeth: the side-cutting geometry in the simulation
def test_gear_teeth_cuts_a_modelled_gear_cleanly(machine, gear):
    st = blank(machine)
    # which A puts a tooth space at the cutter (3 o'clock = +Y): the radial map's angle is the one that brings a point to +Z
    xs, angles, R = cam.radial_map(st, gear.shape, 0.2, 1.0)
    col = R[:, len(xs) // 2]
    assert col.max() == pytest.approx(11.0, abs=0.01) and col.min() == pytest.approx(8.75, abs=0.01)
    a0 = (float(angles[int(np.argmin(col))]) - 90.0) % (360.0 / N)
    assert a0 == pytest.approx(9.0, abs=0.01)
    op = cam.gear_teeth(st, cutter(), M, N, a0=a0, stepdown=0.8)
    assert op.kind == "gear_teeth" and op.uses_a and op.key == "Gear teeth"
    p = op.params
    assert p["r_pitch"] == 10 and p["r_root"] == 8.75 and p["r_tip"] == 11 and p["spaces"] == N and p["passes"] == 3
    assert p["module"] == M and p["teeth"] == N and p["pressure_angle"] == 20 and p["helix"] == 0
    # A stays within [0, 360) and N spaces are cut, each at a fixed A, the cutter at Y = r + R on the +Y side, Z = disc centre on the axis
    cuts = [mv for mv in op.moves if mv[0] == cam.FEED]
    assert len(cuts) == N * 3 and all(0 <= mv[5] < 360 for mv in op.moves)
    spaces = sorted({round(mv[5], 3) for mv in cuts})
    assert len(spaces) == N and np.allclose(np.diff(spaces), 360.0 / N)
    assert min(mv[2] for mv in cuts) == pytest.approx(8.75 + 20.0) and all(mv[3] == cuts[0][3] for mv in cuts)
    assert cuts[0][3] == pytest.approx(-(4.0 + cutter().shape.p["thickness"] / 2), abs=1e-3)
    # one X direction only (climb on +Y = -X), lead-in/out 22 mm beyond both faces
    assert all(mv[1] == pytest.approx(-22.0) for mv in cuts) and p["feed_dir"] == "-X" and p["lead"] == 22
    assert any("past the stock's end" in w for w in op.warnings)
    # the proof: the radial simulation of the side cut matches the modelled involute gear
    prog = cam.Program(st); prog.add(op)
    res = cam_sim.simulate(prog, part=gear)
    assert res.mode == "radial" and res.stats["removed_cm3"] > 0.4
    assert res.stats["gouge"]["max_depth"] <= 0.05 and res.stats["left"]["max"] <= 0.1 and not res.stats["rapid_hits"]
    # two degrees off the spaces and the same cut gouges the teeth deeply: the comparison is sensitive
    bad = cam.Program(st); bad.add(cam.gear_teeth(st, cutter(), M, N, a0=a0 + 2.0, stepdown=0.8))
    assert cam_sim.simulate(bad, part=gear).stats["gouge"]["max_depth"] > 0.5
    # the post: A words (indexing), no arcs, lines within the Makera limit
    g = prog.gcode()
    assert re.search(r"^G0 .*A\d", g, re.M) and not re.search(r"^G[23] ", g, re.M)
    assert all(len(l) <= cam.MAKERA_MAX_LINE for l in g.splitlines())


def test_gear_teeth_helix_mismatch_and_warnings(machine):
    st = blank(machine)
    op = cam.gear_teeth(st, cutter(), M, N, a0=9.0, helix=15.0)
    pairs = [(op.moves[i - 1], mv) for i, mv in enumerate(op.moves) if mv[0] == cam.FEED]     # the Y approach before each cut
    starts, cuts = [s for s, _ in pairs], [c for _, c in pairs]
    assert all(s[0] == cam.RAPID and s[2] == c[2] and s[1] != c[1] for s, c in pairs)
    da = [c[5] - s[5] for s, c in pairs]
    expect = math.degrees(math.tan(math.radians(15.0)) / 10.0) * (W + 2 * 22.0)        # dA = dx tan(helix) / r_pitch over the pass
    assert len(da) == len(cuts) == N * 3 and all(abs(abs(d) - expect) < 1e-3 for d in da)
    assert all(d > 0 for d in da)                                       # feeding -X: right hand = A rises as X falls
    assert all(0 <= s[5] < 360 for s in starts) and op.params["helix"] == 15
    g = cam.Program(st, [op]).gcode()
    assert re.search(r"^G1 .*A-?\d", g, re.M) and not re.search(r"^G[23] ", g, re.M)
    # the other side feeds the other way; conventional flips it again
    assert cam.gear_teeth(st, cutter(), M, N, side="-y").params["feed_dir"] == "+X"
    assert cam.gear_teeth(st, cutter(), M, N, climb=False).params["feed_dir"] == "+X"
    assert min(mv[2] for mv in cam.gear_teeth(st, cutter(), M, N, side="-y").moves) == pytest.approx(-(11 + 22))
    # a module cutter only cuts its own module; other tools are refused
    with pytest.raises(cam.CamError, match="module"):
        cam.gear_teeth(st, cutter(), 1.5, N)
    with pytest.raises(cam.CamError, match="gear_cutter"):
        cam.gear_teeth(st, flat(), M, N)
    # the wrong cutter of the set, too much surface speed, a thin disc, a blank that is not the tip diameter
    hot = cutter(26, thickness=2.5, rpm=3000)
    big = cam.Setup(machine, cam.Stock.cylinder(24, W, x0=0), rotary=True)
    w = cam.gear_teeth(big, hot, M, N, width=10).warnings
    assert any("No.5 (26–34T)" in x and "wants No.3 (17–20T)" in x for x in w)
    assert any("377 m/min" in x and "636 rpm" in x for x in w)
    assert any("flanks near the tooth tips" in x for x in w)
    assert any("bigger than the gear's tip" in x for x in w) and any("beyond the blank" in x for x in w)
    # per-operation overrides know the new kind (tool values and cut settings)
    cam.reset_overrides()
    cam.overrides({"Gear teeth": {"stepdown": 0.5, "helix": 10, "side": "-y"}})
    ov = cam.gear_teeth(st, cutter(), M, N)
    assert ov.params["passes"] == 5 and ov.params["helix"] == 10 and ov.params["side"] == "-y" and ov.cut_overrides == {"helix": 10, "side": "-y"}
    cam.reset_overrides()


# ------------------------------------------------------------------ rotary_profile: a hexagon from round stock
def test_rotary_profile_hex_from_round_in_the_sim(machine):
    af = 16.0
    rc = af / math.sqrt(3)
    part = ck.run_script(f"result = {{'H': Rot(0, 90, 0) * extrude(RegularPolygon({rc}, 6), 30)}}")
    # RegularPolygon's vertex on +X goes to -Z under Rot(0, 90, 0): the section in the setup's (y, z) plane
    hexagon = shapely.Polygon([(rc * math.sin(math.radians(60 * k)), -rc * math.cos(math.radians(60 * k))) for k in range(6)])
    st = cam.Setup(machine, cam.Stock.cylinder(22, 30, x0=0), rotary=True)
    op = cam.rotary_profile(st, flat(), hexagon, stepdown=1.0)
    assert op.kind == "rotary_profile" and op.uses_a and op.params["levels"] == 3 and op.params["r_min"] == pytest.approx(8.0, abs=1e-3)
    av = [mv[5] for mv in op.moves]
    assert min(av) >= 0 and max(av) <= 360 and not op.warnings                 # rings alternate, A never winds up; nothing unreachable
    assert all(mv[2] == 0 for mv in op.moves)                                  # radial: the tool stays on Y = 0
    prog = cam.Program(st, [op])
    res = cam_sim.simulate(prog, part=part)
    assert res.stats["gouge"]["max_depth"] <= 0.05 and res.stats["left"]["max"] <= 0.1 and not res.stats["rapid_hits"]
    g = prog.gcode()
    assert re.search(r"^G1 .*A-?\d", g, re.M) and not re.search(r"^G[23] ", g, re.M)
    # a notch narrower than the tool: both a flat and a ball leave material there and say so
    notch = hexagon.difference(shapely.box(-1.0, 6.0, 1.0, 12.0))
    for t in (flat(), ball()):
        w = cam.rotary_profile(st, t, notch, stepdown=1.0, x_range=(5, 10)).warnings
        assert any("cannot reach" in x for x in w), (t.type, w)
    with pytest.raises(cam.CamError, match="one polygon"):
        cam.rotary_profile(st, flat(), shapely.MultiPolygon([hexagon, shapely.Point(15, 0).buffer(1)]))
