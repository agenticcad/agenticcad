import math
import numpy as np
import shapely
import pytest

import cam_kernel as cam
from conftest import close


def test_hole_detection_with_through_flag(bracket):
    hs = cam.holes(bracket)
    ds = sorted(round(h.diameter, 2) for h in hs)
    assert ds == [3.2, 3.2, 3.2, 3.2, 6.0]
    assert all(h.through for h in hs)
    assert cam.holes(bracket, dmax=4)[0].diameter < 4


def test_section_and_stock_minus(bracket, cam_setup):
    setup, tools, _ = cam_setup
    sec = cam.section(bracket, 0.0)
    expected = 60 * 40 - 4 * (16 - 4 * math.pi) - math.pi * 3 ** 2 - 4 * math.pi * 1.6 ** 2   # plate − corner fillets − bore − 4 holes
    assert len(sec.geoms) == 1 and close(sec.area, expected, 0.5) and len(sec.geoms[0].interiors) == 5
    reg = cam.stock_minus(setup, bracket, 0.0)
    assert close(reg.area, setup.stock.rect().area - sec.area, 1)


def test_contour_levels_tabs_and_leads(bracket, cam_setup):
    setup, tools, _ = cam_setup
    op = cam.contour(setup, tools[1], cam.section(bracket, 0.0), z_top=4, z_bottom=-4.5, tabs=4, tab_height=3)
    zs = sorted({m[3] for m in op.moves if m[0] != cam.RAPID})
    assert op.params["passes"] == 5 and min(zs) == -4.5
    # at the deepest pass, some moves are lifted to the tab height
    deep = [m for m in op.moves if m[0] == cam.FEED and m[3] > -4.5 + 1e-6 and m[3] <= -1.5 + 1e-6]
    assert deep, "tabs should lift the tool"
    assert op.params["lead"] == tools[1].radius


def _count(g, *prefixes):
    return sum(1 for l in g.splitlines() if l.startswith(tuple(p + " " for p in prefixes)))


def test_post_emits_arcs_only_for_circular_runs(cam_setup):
    setup, tools, _ = cam_setup
    prog = cam.Program(setup, [cam.contour(setup, tools[1], cam.circle(0, 0, 20), z_top=4, z_bottom=2, side="on", lead=0)])
    g = prog.gcode()
    assert _count(g, "G2", "G3") >= 2 and _count(g, "G1") < 10       # a circle is a few arcs, not 250 lines
    square = cam.Program(setup, [cam.contour(setup, tools[1], cam.rect(-10, -10, 10, 10), z_top=4, z_bottom=2, side="on", lead=0)]).gcode()
    assert _count(square, "G2", "G3") == 0 and _count(square, "G1") <= 8


def test_post_header_units_wcs_and_tool_changes(bracket, cam_setup):
    setup, tools, _ = cam_setup
    hs = cam.holes(bracket, dmax=4)
    prog = cam.Program(setup, name="t")
    prog.add(cam.face(setup, tools[1], z_top=setup.stock.top, z_bottom=4))
    prog.add(cam.drill(setup, tools[5], hs, peck="auto"))
    prog.add(cam.contour(setup, tools[1], cam.section(bracket, 0.0), z_top=4, z_bottom=-4.5))
    g = prog.gcode()
    lines = g.splitlines()
    assert "G21 G90 G94 G17" in lines and "G54" in lines and lines[-1] == "M30"
    assert _count(g, "M6") == 2 and sum(1 for l in lines if l == "M0") == 2    # T1 -> T5 -> T1
    assert _count(g, "M3") == 3
    # WCS origin is the stock top-left: the very first rapid is the (subtracted) safe height
    ox, oy, oz = setup.origin_point()
    first_z = next(l for l in lines if l.startswith("G0 Z"))
    assert close(float(first_z[4:]), setup.safe_z - oz)
    assert "G0 X66" in g or "X66 " in g          # stock width 66 mm along X from the corner


def test_program_checks_travel_and_spindle(bracket, cam_setup):
    setup, tools, machines = cam_setup
    small = cam.Machine(name="tiny", travel={"x": 30, "y": 30, "z": 30}, spindle={"min": 1000, "max": 5000})
    s2 = cam.Setup(small, setup.stock)
    prog = cam.Program(s2, [cam.contour(s2, tools[1], cam.section(bracket, 0.0), z_top=4, z_bottom=-4.5)])
    w = " ".join(prog.check())
    assert "exceeds machine travel" in w and "rpm" in w and "below the stock bottom" in w


def test_pocket_concentric_and_tool_fit(cam_setup):
    setup, tools, _ = cam_setup
    op = cam.pocket(setup, tools[2], cam.circle(0, 0, 6), z_top=4, z_bottom=2)
    assert op.params["rings"] >= 1 and op.moves
    with pytest.raises(cam.CamError):
        cam.pocket(setup, tools[1], cam.circle(0, 0, 4), z_top=4, z_bottom=2)   # Ø6 tool in a Ø4 pocket


def test_drill_pecks_and_breakthrough(bracket, cam_setup):
    setup, tools, _ = cam_setup
    hs = cam.holes(bracket, dmax=4)
    op = cam.drill(setup, tools[5], hs, peck=2.0)
    plunges = [m for m in op.moves if m[0] == cam.PLUNGE]
    assert len(plunges) >= 4 * 4               # 8 mm plate, 2 mm pecks, 4 holes
    assert min(m[3] for m in plunges) < -4     # breaks through the bottom (z=-4)
    assert op.params["holes"] == 4


def test_rest_region_finds_corners_left_by_bigger_tool(cam_setup):
    setup, tools, _ = cam_setup
    slot = cam.rect(-25, -5, 25, 5)
    rr = cam.rest_region(slot, tools[1], tools[2])
    assert 0 < rr.area < slot.area * 0.3
    assert len(rr.geoms) == 2                  # both ends of the slot
    op = cam.pocket(setup, tools[2], slot, z_top=4, z_bottom=2, rest_from=tools[1])
    assert op.kind == "rest" and op.moves


def _engagement_audit(op, P, r):
    """Independent fine-grid re-simulation: leading-half contact angle per feed step, and uncut fraction."""
    res = 0.05
    minx, miny, maxx, maxy = P.bounds
    xs = np.arange(minx - r - 1, maxx + r + 1, res); ys = np.arange(miny - r - 1, maxy + r + 1, res)
    gx, gy = np.meshgrid(xs, ys)
    M = shapely.contains_xy(P, gx.ravel(), gy.ravel()).reshape(gy.shape); M0 = M.sum()
    ang = np.arange(360) * math.pi / 180; ox, oy = np.cos(ang), np.sin(ang)
    prev, angles = None, []
    for m in op.moves:
        if prev is not None and m[0] != cam.RAPID and prev[0] != cam.RAPID and abs(m[3] - prev[3]) < 1e-9:
            hx, hy = m[1] - prev[1], m[2] - prev[2]; L = math.hypot(hx, hy)
            if L > 1e-6:
                hx, hy = hx / L, hy / L
                sx, sy = m[1] + (r - 0.03) * ox, m[2] + (r - 0.03) * oy
                ii = np.rint((sy - ys[0]) / res).astype(int); jj = np.rint((sx - xs[0]) / res).astype(int)
                ok = (ii >= 0) & (ii < M.shape[0]) & (jj >= 0) & (jj < M.shape[1])
                inm = np.zeros(360, bool); inm[ok] = M[ii[ok], jj[ok]]
                angles.append((inm & ((ox * hx + oy * hy) > 0)).sum())
        if prev is not None:
            for t in np.linspace(0, 1, 6):
                cx, cy = prev[1] + (m[1] - prev[1]) * t, prev[2] + (m[2] - prev[2]) * t
                M[(gx - cx) ** 2 + (gy - cy) ** 2 <= r * r] = False
        else:
            M[(gx - m[1]) ** 2 + (gy - m[2]) ** 2 <= r * r] = False
        prev = m
    return np.array(angles), M.sum() / M0


def test_adaptive_bore_is_constant_engagement(cam_setup):
    setup, tools, _ = cam_setup
    P = cam.circle(0, 0, 12)
    op = cam.adaptive(setup, tools[1], P, z_top=24, z_bottom=22, stepover=0.15)
    target = op.params["engagement_angle"]
    angles, uncut = _engagement_audit(op, P, tools[1].radius)
    assert angles.max() <= 1.3 * target, f"max {angles.max()} vs target {target}"
    assert uncut < 0.02
    assert op.warnings == []


def test_adaptive_slot_loops_and_slows_in_corners(cam_setup):
    setup, tools, _ = cam_setup
    P = cam.rect(-25, -5, 25, 5)
    op = cam.adaptive(setup, tools[1], P, z_top=4, z_bottom=0, stepover=0.15)
    angles, uncut = _engagement_audit(op, P, tools[1].radius)
    target = op.params["engagement_angle"]
    assert np.percentile(angles, 50) <= 1.3 * target      # the bulk of the path is at target
    assert uncut < 0.05
    feeds = {m[4] for m in op.moves if m[0] == cam.FEED}
    assert min(feeds) < max(feeds)                        # corner feed reduction happened
    assert op.params["retracts"] <= op.params["fronts"]


def test_adaptive_rest_nothing_left_warns(cam_setup):
    setup, tools, _ = cam_setup
    op = cam.adaptive(setup, tools[2], cam.circle(0, 0, 20), z_top=4, z_bottom=2, rest_from=tools[1])
    assert any("nothing left" in w for w in op.warnings) and not op.moves


def test_parallel3d_follows_surface(bracket, cam_setup):
    setup, tools, _ = cam_setup
    op = cam.parallel3d(setup, tools[4], shape=bracket, z_bottom=4, region=cam.rect(-16, -16, 16, 16), stepover=0.3)
    zs = [m[3] for m in op.moves if m[0] == cam.FEED]
    assert max(zs) >= 24 - 0.1 and min(zs) <= 4 + 0.1      # boss top and plate top both touched
    assert op.params["grid"][0] > 10


def test_feeds_calculator_clamps_and_thins(cam_setup):
    setup, tools, machines = cam_setup
    f = cam.feeds(tools[2], "aluminium", machines["Generic 3018"])
    assert f["rpm"] == 10000 and any("spindle-limited" in n for n in f["notes"])
    assert 300 <= f["feed"] <= 600
    thin = cam.feeds(tools[1], "alu", machines["Shapeoko-class router"], radial_engagement=0.15)
    assert any("chip thinning" in n for n in thin["notes"])
    with pytest.raises(cam.CamError):
        cam.feeds(tools[1], "cheese")
    t = cam.apply_feeds(tools[5], "mdf", machines["Generic 3018"])
    assert t.feed > 0 and "[mdf" in t.notes


def test_program_payload_and_time(bracket, cam_setup):
    setup, tools, _ = cam_setup
    prog = cam.Program(setup, [cam.contour(setup, tools[1], cam.section(bracket, 0.0), z_top=4, z_bottom=-4.5)], name="p")
    p = prog.to_payload()
    assert p["ops"][0]["kind"] == "contour" and p["time"] > 0 and p["origin"][2] == setup.stock.top
    assert all(len(m) == 5 for m in p["ops"][0]["moves"])
