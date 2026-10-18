"""Tool shapes: the profile of every type, and that toolpaths and the simulator use it — paths that never put any part
of the tool into the surface (checked against exact geometry, not the height map they came from), 2.5D cuts planned
with the tool's width at the cut depth, and shanks / buried flutes reported as collisions."""
import math

import numpy as np
import pytest

import cam_kernel as cam
import cam_sim
import tool_shape as ts
from build123d import Box, Pos, Sphere
from cam_data import Library


@pytest.fixture(scope="module")
def z1(tmp_path_factory):
    return Library(tmp_path_factory.mktemp("lib")).machines()["Makera Z1"]


def T(**kw):
    base = dict(number=1, name="t", flute_length=12, rpm=10000, feed=600, plunge=150, stepdown=1, stepover=0.2)
    base.update(kw)
    return cam.Tool(**base)


def test_profiles_match_their_closed_forms():
    ball = T(type="ball", diameter=6).shape
    d = np.array([0.0, 1.0, 2.0, 2.9])
    assert np.allclose(ball.cut_height(d), 3 - np.sqrt(9 - d * d), atol=2e-3)
    bull = T(type="bull", diameter=6, corner_radius=1).shape
    assert np.allclose(bull.cut_height(np.array([0.0, 2.0, 2.5, 2.99])), [0, 0, 1 - math.sqrt(0.75), 1 - math.sqrt(1 - 0.99 ** 2)], atol=2e-3)
    v = T(type="vbit", diameter=3.175, angle=30, tip_diameter=0.1, flute_length=3).shape
    k = math.tan(math.radians(15))
    assert v.cut_height(np.array([0.05]))[0] == pytest.approx(0, abs=1e-3)
    assert v.cut_height(np.array([0.5]))[0] == pytest.approx((0.5 - 0.05) / k, abs=5e-3)
    assert v.width_at_depth(0.2) == pytest.approx(0.1 + 0.4 * k, abs=1e-6)
    assert v.cut_radius == pytest.approx(0.05 + 3 * k, abs=1e-3) and v.max_radius == pytest.approx(1.5875)   # cone carries on uncut
    tb = T(type="taper_ball", diameter=3.175, tip_diameter=0.5, taper=5).shape
    assert tb.cut_height(np.array([0.1]))[0] == pytest.approx(0.25 - math.sqrt(0.25 ** 2 - 0.01), abs=2e-3)
    micro = T(type="flat", diameter=0.3, flute_length=1, shank_diameter=3.175).shape
    assert micro.cut_radius == pytest.approx(0.15) and micro.max_radius == pytest.approx(1.5875)
    assert math.isinf(micro.cut_height(np.array([0.5]))[0]) and 1.0 < micro.height(np.array([0.5]))[0] < 2.0
    # bad definitions are refused with the reason
    for bad, why in ((dict(type="bull", diameter=6), "corner radius"), (dict(type="vbit", diameter=6), "included angle"),
                     (dict(type="taper_ball", diameter=3, tip_diameter=0.5), "taper"), (dict(type="flat", diameter=2, neck_diameter=3), "neck")):
        with pytest.raises(ts.ToolShapeError, match=why):
            T(**bad).shape


def _clearance(tool, tips, centre, rad):
    """Smallest distance from the sphere surface to any point of the tool's body (negative = inside the sphere)."""
    sh = tool.shape
    zs = np.linspace(0, 6, 1201)
    rs = sh.radius_at(zs)
    ang = np.linspace(0, 2 * math.pi, 72, endpoint=False)
    best = np.inf
    for x, y, z in tips:
        X = x + rs[:, None] * np.cos(ang)[None, :]; Y = y + rs[:, None] * np.sin(ang)[None, :]; Z = z + zs[:, None] * np.ones_like(ang)[None, :]
        # include the axis (a flat bottom is a disc): points inside radius too, coarse
        dist = np.sqrt((X - centre[0]) ** 2 + (Y - centre[1]) ** 2 + (Z - centre[2]) ** 2) - rad
        for f in (0.0, 0.5):
            Xi = x + f * rs[:, None] * np.cos(ang)[None, :]; Yi = y + f * rs[:, None] * np.sin(ang)[None, :]
            di = np.sqrt((Xi - centre[0]) ** 2 + (Yi - centre[1]) ** 2 + (Z - centre[2]) ** 2) - rad
            dist = np.minimum(dist, di)
        best = min(best, float(dist.min()))
    return best


@pytest.mark.parametrize("kw", [dict(type="ball", diameter=6), dict(type="bull", diameter=6, corner_radius=1),
                                dict(type="flat", diameter=4), dict(type="vbit", diameter=6, angle=90, tip_diameter=0.5),
                                dict(type="taper_ball", diameter=6, tip_diameter=2, taper=10)])
def test_parallel3d_never_puts_any_tool_type_into_the_surface(z1, kw):
    """A dome (sphere radius 20, its top 8 mm above the stock bottom): every tip of the 3D finishing path, with the real
    tool around it, stays outside the exact sphere, and the tool touches it (not floating) where it runs over the dome."""
    rad, cz = 20.0, -12.0
    dome = Pos(0, 0, cz) * Sphere(rad) & Pos(0, 0, 4) * Box(30, 30, 8)
    stock = cam.Stock.block(30, 30, 8, top=8.5)
    st = cam.Setup(z1, stock, origin="stock-top-center")
    tool = T(**kw)
    op = cam.parallel3d(st, tool, shape=dome, z_bottom=0.0, stepover=0.15, resolution=0.05,
                        region=cam.rect(-8, -8, 8, 8))
    tips = np.array([m[1:4] for m in op.moves if m[0] == cam.FEED])
    tips = tips[(np.abs(tips[:, 0]) < 7) & (np.abs(tips[:, 1]) < 7)]
    assert len(tips) > 50
    sub = tips[:: max(1, len(tips) // 120)]
    gap = _clearance(tool, sub, (0, 0, cz), rad)
    assert gap > -0.02, f"{kw['type']} goes {-gap:.3f} mm into the dome"
    # touching: from each tip, lowering the tool 0.03 mm would put it into the dome
    touch = [_clearance(tool, [(x, y, z - 0.03)], (0, 0, cz), rad) for x, y, z in sub[::6]]
    assert max(touch) < 0.0, f"{kw['type']} floats above the dome"


def test_2d_cuts_plan_with_the_width_at_depth_and_say_what_the_shape_leaves(z1):
    stock = cam.Stock.block(40, 40, 6, top=0)
    st = cam.Setup(z1, stock, origin="stock-top-left")
    v = T(type="vbit", diameter=3.175, angle=30, tip_diameter=0.1, flute_length=3, stepover=0.4)
    sq = cam.rect(-10, -10, 10, 10)
    c = cam.contour(st, v, sq, z_top=0, z_bottom=-0.2, side="inside", ramp=0, lead=0, name="Engrave edge")
    assert c.tool.type == "vbit"                                   # the op keeps the real tool
    w = v.shape.width_at_depth(0.2)
    xs = [m[1] for m in c.moves if m[0] == cam.FEED]
    assert max(xs) == pytest.approx(10 - w / 2, abs=0.01)          # the top of the cut lands on the boundary
    assert any("planned with its Ø0.207" in x for x in c.warnings)
    b = cam.pocket(st, T(type="ball", diameter=3.175, stepover=0.3), sq, z_top=0, z_bottom=-2, name="Ball pocket")
    note = next(x for x in b.warnings if "ball end" in x)
    s = 0.3 * 3.175
    assert f"{1.5875 - math.sqrt(1.5875 ** 2 - (s / 2) ** 2):.3f} mm ridges" in note
    deep = cam.pocket(st, T(type="flat", diameter=0.3, flute_length=1, shank_diameter=3.175, stepover=0.4), cam.rect(-1, -1, 1, 1),
                      z_top=0, z_bottom=-3, name="Too deep")
    assert any("flutes are 1 mm long" in x for x in deep.warnings) and any("widens to Ø3.175" in x for x in deep.warnings)


def test_simulator_reports_a_wide_shank_and_buried_flutes(z1):
    stock = cam.Stock.block(40, 40, 10, top=0)
    st = cam.Setup(z1, stock, origin="stock-top-left")
    p = cam.Program(st)
    # a 0.3 mm endmill on a 1/8" shank, 3 mm down a 1 mm-wide slot: the cut itself is fine, the shank meets the slot walls
    micro = T(number=9, type="flat", diameter=0.3, flute_length=1, shank_diameter=3.175, stepdown=1, stepover=0.4)
    p.add(cam.pocket(st, T(number=2, type="flat", diameter=1.0, flute_length=4, stepdown=1, stepover=0.4), cam.rect(-10, -0.6, 10, 0.6),
                     z_top=0, z_bottom=-3, finish=False, ramp=0, name="Slot"))
    p.add(cam.contour(st, micro, cam.rect(-5, -0.1, 5, 0.1), z_top=-2.8, z_bottom=-3, side="on", ramp=0, lead=0, name="Fine"))
    r = cam_sim.simulate(p)
    hits = r.stats["collisions"]["hits"]
    assert any(h["op"] == "Fine" and h["part"] in ("tool shank", "tool neck") for h in hits), hits
    assert not any(h["op"] == "Slot" for h in hits)
    # a 2 mm-flute endmill taking a 3 mm cut in one pass: the body above the flutes is in the material
    q = cam.Program(st)
    short = T(number=3, type="flat", diameter=3.175, flute_length=2, stepdown=3)
    q.add(cam.contour(st, short, cam.rect(-10, -10, 10, 10), z_top=0, z_bottom=-3, stepdown=3, side="outside", ramp=0, lead=0, name="Deep"))
    hits = cam_sim.simulate(q).stats["collisions"]["hits"]
    assert any(h["part"].startswith("tool above its 2 mm flutes") for h in hits), hits


def test_v_tip_counts_everywhere_and_its_absence_is_reported(z1):
    """A V-bit's tip: the program says when it isn't set (modelled as a sharp point), an engraving on the line says how
    wide a groove it cuts, and feeds & speeds size the tool by its cutting width, not its 1/8" body."""
    stock = cam.Stock.block(40, 40, 6, top=0)
    st = cam.Setup(z1, stock, origin="stock-top-left")
    line = cam.rect(-10, -5, 10, 5)
    sharp = T(number=5, name="0.1 mm 30° V-bit", type="vbit", diameter=3.175, angle=30, flute_length=3, stepdown=0.2)
    tipped = T(number=6, name="0.1 mm 30° V-bit", type="vbit", diameter=3.175, angle=30, flute_length=3, tip_diameter=0.1, stepdown=0.2)
    k = math.tan(math.radians(15))
    for tool, w in ((sharp, 0.4 * k), (tipped, 0.1 + 0.4 * k)):
        p = cam.Program(st)
        p.add(cam.contour(st, tool, line, z_top=0, z_bottom=-0.2, side="on", ramp=0, lead=0, name="Engrave"))
        note = next(x for x in p.ops[0].warnings if "groove" in x)
        assert f"groove {w:.3f} mm wide" in note
        tipless = [x for x in p.check() if "no tip" in x]
        assert bool(tipless) == (tool is sharp)
    assert tipped.spec() == "V 30° · tip Ø0.1 · Ø3.175 · flutes 3 mm" and "no tip Ø set" in sharp.spec()
    assert T(type="ball", diameter=2, flute_length=17, shank_diameter=3.175).spec() == "ball R1 (Ø2) · flutes 17 mm · shank Ø3.175"
    body = cam.feeds(T(type="flat", diameter=3.175, flutes=2), "aluminium", z1)
    v = cam.feeds(tipped, "aluminium", z1, depth=0.2)
    assert v["chipload"] < body["chipload"] and v["feed"] < body["feed"] and v["stepdown"] <= 0.2
    assert any("Ø0.207 cutting width at 0.2 mm" in n for n in v["notes"])


@pytest.mark.parametrize("region", ["square", "circle"])
def test_ball_pocket_floor_is_as_flat_as_the_stepover_promises(z1, region):
    """Concentric rings planned for the ball's full width left the pocket's middle and the rings' corners up to 0.13 mm
    high (an R1 ball, 0.6 mm stepover promising 0.046): short clean-up passes there bring the whole floor within it."""
    from shapely import contains_xy
    stock = cam.Stock.block(14, 14, 6, top=0)
    st = cam.Setup(z1, stock, origin="stock-top-center")
    ball = T(number=3, type="ball", diameter=2, flute_length=17, stepdown=0.5, stepover=0.3)
    poly = cam.rect(-5, -5, 5, 5) if region == "square" else cam.circle(0, 0, 9)
    op = cam.pocket(st, ball, poly, z_top=0, z_bottom=-1.5, name="Ball pocket")
    assert any("clean-up" in w and "within 0.046" in w for w in op.warnings), op.warnings
    assert sum(1 for m in op.moves if m[0] == cam.PLUNGE and abs(m[3] + 1.5) < 1e-6) <= 2      # linked at the floor, not a plunge each
    p = cam.Program(st); p.add(op)
    r = cam_sim.simulate(p, resolution=0.02)
    g, z = r.grid, r.final["zhi"]
    X = g["x0"] + (np.arange(g["nx"]) + 0.5) * g["res"]; Y = g["y0"] + (np.arange(g["ny"]) + 0.5) * g["res"]
    XX, YY = np.meshgrid(X, Y)
    floor = z[contains_xy(poly.buffer(-1.02), XX, YY)]
    assert floor.max() + 1.5 <= 0.046 + 0.01, f"{floor.max() + 1.5:.3f} mm left on the floor"


def test_a_sharp_point_is_a_setting_not_a_missing_tip(z1):
    st = cam.Setup(z1, cam.Stock.block(20, 20, 4, top=0), origin="stock-top-center")
    for sharp in (False, True):
        ch = T(number=4, name="90° chamfer", type="chamfer", diameter=3.175, angle=90, flute_length=2, sharp=sharp, stepdown=0.3)
        p = cam.Program(st); p.add(cam.contour(st, ch, cam.rect(-5, -5, 5, 5), z_top=0, z_bottom=-0.3, side="on", ramp=0, lead=0, name="C"))
        assert bool([w for w in p.check() if "no tip" in w]) is (not sharp)
        assert ("sharp point" in ch.spec()) is sharp


def _sim_row(st, ops, res=0.02, y=0.0, axis="x"):
    p = cam.Program(st)
    for o in ops:
        p.add(o)
    r = cam_sim.simulate(p, resolution=res)
    g, z = r.grid, r.final["zhi"]
    X = g["x0"] + (np.arange(g["nx"]) + 0.5) * g["res"]; Y = g["y0"] + (np.arange(g["ny"]) + 0.5) * g["res"]
    return (X, z[np.argmin(abs(Y - y))]) if axis == "x" else (Y, z[:, np.argmin(abs(X - y))])


@pytest.mark.parametrize("kw,width", [(dict(type="chamfer", angle=90, sharp=True), 0.5),
                                      (dict(type="vbit", angle=30, tip_diameter=0.1), 0.2)])
def test_chamfer_edges_cuts_the_chamfer_asked_for(z1, kw, width):
    """A boss 4 x 4 (profiled first): the chamfer is `width` wide on top and width / tan(half angle) down the wall, with
    the tip size in the offset; the top inside it and the wall below it are untouched."""
    st = cam.Setup(z1, cam.Stock.block(10, 10, 4, top=0), origin="stock-top-center")
    flat = T(number=2, type="flat", diameter=2, flute_length=8, stepdown=1)
    tool = T(number=4, diameter=3.175, flute_length=3, stepdown=0.3, **kw)
    sq = cam.rect(-2, -2, 2, 2)
    ch = cam.chamfer_edges(st, tool, sq, z_top=0, width=width, name="Chamfer")
    X, row = _sim_row(st, [cam.contour(st, flat, sq, z_top=0, z_bottom=-2, side="outside", name="Profile"), ch])
    k = math.tan(math.radians(tool.angle / 2))
    for x in np.linspace(2 - width * 0.9, 1.995, 6):
        i = np.argmin(abs(X - x))
        assert abs(row[i] + max(0.0, X[i] - (2 - width)) / k) < 0.02 / k + 0.005, (X[i], row[i])
    inner = row[(X > -2 + width + 0.05) & (X < 2 - width - 0.05)]
    assert abs(inner).max() < 1e-6                              # the top inside the chamfer is untouched
    with pytest.raises(cam.CamError, match="V-bit or chamfer"):
        cam.chamfer_edges(st, T(type="flat", diameter=2), sq, z_top=0, width=0.5)


def test_vcarve_follows_the_centreline_as_deep_as_the_shape_is_wide(z1):
    st = cam.Setup(z1, cam.Stock.block(24, 10, 4, top=0), origin="stock-top-center")
    v90 = T(number=4, type="chamfer", diameter=6, angle=90, flute_length=3, sharp=True)
    op = cam.vcarve(st, v90, cam.rect(-8, -1, 8, 1), z_top=0, name="V")
    Y, col = _sim_row(st, [op], y=0.0, axis="y")
    for y in (-0.5, 0.0, 0.5):
        assert abs(col[np.argmin(abs(Y - y))] + (1 - abs(y))) < 0.03       # 90°: as deep as the distance to the edge
    assert abs(col[np.argmin(abs(Y - 1.1))]) < 1e-6 and abs(col[np.argmin(abs(Y + 1.1))]) < 1e-6   # nothing outside
    capped = cam.vcarve(st, v90, cam.rect(-8, -1, 8, 1), z_top=0, max_depth=0.4, name="V flat")
    assert capped.params["z_bottom"] == pytest.approx(-0.4) and capped.params["flat_rings"] > 0
    stepped = cam.vcarve(st, v90, cam.rect(-8, -1, 8, 1), z_top=0, max_depth=0.4, stepdown=0.1, name="V stepped")
    assert stepped.params["levels"] == 4                         # never more than a stepdown of new depth per level
