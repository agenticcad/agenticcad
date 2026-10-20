"""Post and link quality: findings from the 2026-10-20 review of the obsidian badge toolpaths (agenticcad-admin/
toolpath-efficiency-2026-10-20.md)."""
import math
import re

import cam_kernel as cam
from tests.test_cam_setups import flat, ball


def test_collinear_merge_never_accumulates_error():
    """C1: a flat tab top followed by a shallow exit ramp used to merge into ONE sloped line (0.058 mm low at the tab's
    end) because each dropped point was only tested against its successor's chord."""
    pts = [(0.0, 0.0, 0.0)] + [(x, 0.0, 0.0) for x in (1.0, 2.0, 3.0, 3.5)] + [(x, 0.0, -0.05 * (x - 3.5)) for x in (4.0, 4.5, 5.0, 5.5)]
    out = cam._merge_collinear3(pts, 0.005)
    # every dropped point lies within 0.005 of the emitted chord that replaces it
    def dev(p, a, b):
        ab = [b[i] - a[i] for i in range(3)]; ap = [p[i] - a[i] for i in range(3)]
        L = math.sqrt(sum(v * v for v in ab)); t = sum(x * y for x, y in zip(ap, ab)) / L
        return math.sqrt(max(sum(x * x for x in ap) - t * t, 0.0))
    for a, b in zip(out, out[1:]):
        i, j = pts.index(a), pts.index(b)
        assert all(dev(p, a, b) <= 0.005 + 1e-9 for p in pts[i:j + 1])
    assert (3.5, 0.0, 0.0) in out                                # the tab end (the slope change) is kept
    flat2d = [(x, 0.0) for x in range(0, 50)] + [(50.0, 0.0), (51.0, 0.3)]
    assert cam._merge_collinear(flat2d, 0.01)[-2] == (50.0, 0.0)


def test_straight_runs_with_rounding_noise_post_as_lines_not_huge_arcs(cam_setup):
    setup, tools, _ = cam_setup
    # a long straight raster line with 4-decimal rounding noise: one G1, no R>1000 arc
    moves = [[0, 0.0, 0.0, 5.0, 0.0], [2, 0.0, 0.0, -1.0, 100.0]]
    for k in range(1, 60):
        moves.append([1, round(k * 0.5, 4), round(0.0001 * ((k * 7) % 3 - 1), 4), -1.0, 600.0])
    items = list(cam._with_arcs(moves, 0.05, math.pi, True))
    assert not any(it[0] == "arc" for it in items)
    assert sum(1 for it in items if it[0] == 1) <= 2
    # a real circle still posts as arcs
    op = cam.contour(setup, tools[1], cam.circle(0, 0, 20), z_top=4, z_bottom=2, side="on", lead=0)
    items = list(cam._with_arcs(op.moves, 0.05, math.pi, True))
    assert sum(1 for it in items if it[0] == "arc") >= 2


def test_finishing_ops_post_to_a_fine_tolerance(cam_setup, bracket):
    """C2: the machine's 0.05 mm arc tolerance is for roughing; a 3D finish or V-bit cut is posted to 0.005."""
    setup, tools, _ = cam_setup
    import cad_kernel as ck
    dome = ck.run_script("result = {'D': Pos(33, 23, 0) * Sphere(12)}").shape      # a curved surface: the tolerance decides the chords
    prog = cam.Program(setup, [cam.parallel3d(setup, ball(), dome, stepover=0.5)])
    fine = prog.gcode().count("\n")
    prog.ops[0].post_tol = 0.05                                   # the roughing tolerance, forced: far fewer, coarser lines
    coarse = prog.gcode().count("\n")
    assert fine > coarse * 1.15, (fine, coarse)
    assert cam.FINE_POST_TOL == 0.005 and "parallel3d" in cam.FINE_POST_KINDS


def test_contour_closing_lap_runs_at_the_feed_not_the_plunge_rate(cam_setup, bracket):
    """E5: the lead-out's remainder lap fed every Z-changing move (tab climbs, ramps) at the plunge feed."""
    setup, tools, _ = cam_setup
    op = cam.contour(setup, tools[1], cam.section(bracket, 0.0), z_top=4, z_bottom=-4.5, tabs=4, tab_height=0.5)
    slow = [m for m in op.moves if m[0] == 1 and m[4] <= tools[1].plunge + 1e-6]
    total = sum(1 for m in op.moves if m[0] == 1)
    assert len(slow) / total < 0.05, f"{len(slow)} of {total} feed moves at the plunge rate"


def test_vcarve_hops_across_the_text_block_instead_of_retracting(cam_setup):
    setup, tools, _ = cam_setup
    from shapely.geometry import box
    v = cam.Tool(9, "V", "vbit", 3.175, 2, 8, rpm=12000, feed=300, plunge=60, stepdown=0.5, stepover=0.2, angle=30, tip_diameter=0.1)
    letters = [box(0, 0, 2, 8), box(6, 0, 8, 8), box(12, 0, 14, 8)]          # three strokes 4 mm apart
    op = cam.vcarve(setup, v, letters, z_top=10.0, max_depth=0.5)
    hop = op.params["hop_z"]
    cuts = [i for i, m in enumerate(op.moves) if m[0] != 0]
    rap_z = [m[3] for m in op.moves[cuts[0]:cuts[-1]] if m[0] == 0]          # links between strokes (not the entry/exit)
    assert rap_z and max(rap_z) <= hop + 1e-6                        # no retract to safe Z between letters


def test_adaptive_lift_links_come_down_to_the_cleared_level(cam_setup, bracket):
    """E9: a lift link whose target is off the rapid-safe set plunged 1.1-1.3 mm from above the stock at the plunge
    rate; the previous level cleared everything above it, so a rapid to just above that level is safe."""
    setup, tools, _ = cam_setup
    op = cam.adaptive(setup, tools[1], cam.section(bracket, 0.0).buffer(8), z_top=setup.stock.top, z_bottom=setup.stock.top - 3, stepdown=1.0)
    top = setup.stock.top
    link_plunges = [(op.moves[i - 1][3] - m[3]) for i, m in enumerate(op.moves)
                    if m[0] == 2 and i and op.moves[i - 1][3] < top + cam.ENTRY_GAP - 1e-6]    # not the entries from above the stock
    assert link_plunges and max(link_plunges) <= 1.0 + 0.06, f"link plunge of {max(link_plunges):.2f} mm (stepdown 1.0)"


def test_finish_lay_options_one_way_radial_spiral_and_face_angle(cam_setup, bracket):
    """Finishing is presentation: one uniform lay (one-way), radial or spiral patterns, and a face at an angle."""
    setup, tools, _ = cam_setup
    b = ball()
    zz = cam.parallel3d(setup, b, bracket, stepover=0.6)
    ow = cam.parallel3d(setup, b, bracket, stepover=0.6, one_way=True)
    # one-way: every cutting pass runs the same way (+X here); the zigzag alternates
    def pass_dirs(op):                                                 # passes = feed moves sharing one Y (rows along X)
        d = []; run = []
        for m in op.moves:
            if m[0] == 1 and (not run or abs(m[2] - run[-1][2]) < 1e-6):
                run.append(m); continue
            if len(run) > 3: d.append(1 if run[-1][1] > run[0][1] else -1)
            run = [m] if m[0] == 1 else []
        if len(run) > 3: d.append(1 if run[-1][1] > run[0][1] else -1)
        return d
    assert len(set(pass_dirs(ow))) == 1 and len(set(pass_dirs(zz))) == 2
    assert ow.params["one_way"] and not zz.params["one_way"]
    # the one-way returns are rapids above the surface, never feed moves across it
    for i, m in enumerate(ow.moves[1:], 1):
        p = ow.moves[i - 1]
        if m[0] == 0 and abs(m[1] - p[1]) > 1.0:                  # a sideways rapid
            assert m[3] >= max(p[3], m[3]) - 1e-9 and m[3] >= ow.params["z_bottom"] + 0.3 - 1e-6
    rad = cam.parallel3d(setup, b, bracket, stepover=1.0, pattern="radial")
    spi = cam.parallel3d(setup, b, bracket, stepover=1.0, pattern="spiral")
    assert rad.params["pattern"] == "radial" and len(rad.moves) > 50
    assert spi.params["pattern"] == "spiral" and len(spi.moves) > 50
    # spiral: one continuous pass (clipped only where it leaves the region) - far fewer plunges than a row raster
    assert sum(1 for m in spi.moves if m[0] == 2) <= len(pass_dirs(ow))
    # face at 45 degrees: the rows run along the diagonal
    zz0 = cam.face(setup, tools[1], z_top=setup.stock.top, z_bottom=setup.stock.top - 0.5)
    f45 = cam.face(setup, tools[1], z_top=setup.stock.top, z_bottom=setup.stock.top - 0.5, angle=45)
    cuts = [(p, m) for p, m in zip(f45.moves, f45.moves[1:]) if m[0] == 1 and p[0] == 1 and abs(m[3] - p[3]) < 1e-9]
    long_ = [(p, m) for p, m in cuts if math.dist(p[1:3], m[1:3]) > 5]
    assert long_ and all(abs(abs(math.degrees(math.atan2(m[2] - p[2], m[1] - p[1]))) - 45) < 1.0 or
                         abs(abs(math.degrees(math.atan2(m[2] - p[2], m[1] - p[1]))) - 135) < 1.0 for p, m in long_)
    f1 = cam.face(setup, tools[1], z_top=setup.stock.top, z_bottom=setup.stock.top - 0.5, one_way=True)
    rows1 = {1 if m[1] > p[1] else -1 for p, m in zip(f1.moves, f1.moves[1:]) if m[0] == 1 and abs(m[1] - p[1]) > 5}
    rows0 = {1 if m[1] > p[1] else -1 for p, m in zip(zz0.moves, zz0.moves[1:]) if m[0] == 1 and abs(m[1] - p[1]) > 5}
    assert rows1 == {1} and rows0 == {1, -1}
    # two levels: the second starts where the first ended (no retract-and-return across the face)
    f2 = cam.face(setup, tools[1], z_top=setup.stock.top, z_bottom=setup.stock.top - 1.0, stepdown=0.5)
    assert f2.params["z_bottom"] == setup.stock.top - 1.0
