import math
import pytest

import cad_kernel as ck
import cam_kernel as cam
import threads as thr
from conftest import close


def test_iso_tables():
    d = thr.iso("m4")
    assert {k: d[k] for k in ("size", "major", "pitch", "tap_drill", "minor", "clearance")} == {"size": "M4", "major": 4.0, "pitch": 0.7, "tap_drill": 3.3, "minor": 3.242, "clearance": {"fine": 4.3, "medium": 4.5, "coarse": 4.8}}
    assert d["system"] == "iso" and d["label"] == "M4×0.7" and abs(d["tpi"] - 36.29) < 0.01
    assert thr.tap_drill("M6") == 5.0 and thr.clearance_dia("M6") == 6.6 and thr.clearance_dia("M6", "fine") == 6.4
    with pytest.raises(thr.ThreadError):
        thr.iso("M7")
    with pytest.raises(thr.ThreadError):
        thr.clearance_dia("M4", "loose")


def test_tap_and_tapped_hole_geometry_and_registry():
    m = ck.run_script('''
plate = Box(40, 30, 8)
plate = tap(plate, "M4", at=(-12, 0, 4), depth=6)
plate = tap(plate, "M6", at=(12, 0, 4), through=True)
plate -= tapped_hole("M3", 5, at=(0, 10, 4))
result = {"Plate": plate}
''')
    hs = {(round(h.x), round(h.y)): h for h in cam.holes(m.shape)}
    assert close(hs[(-12, 0)].diameter, 3.3) and close(hs[(-12, 0)].depth, 6.3, 0.05) and not hs[(-12, 0)].through
    assert close(hs[(12, 0)].diameter, 5.0) and hs[(12, 0)].through
    assert close(hs[(0, 10)].diameter, 2.5) and close(hs[(0, 10)].depth, 5.3, 0.05)
    labels = sorted(t.label() for t in m.threads)
    assert labels == ["M3×0.5 ↧5", "M4×0.7 ↧6", "M6×1 THRU"]
    assert "Threads:" in m.summary(0) and len(m.mesh["threads"]) == 3


def test_tap_along_other_axis():
    m = ck.run_script('result = tap(Box(20, 20, 20), "M5", at=(10, 0, 0), depth=8, axis=(-1, 0, 0))')
    h = cam.holes(m.shape)               # holes() only finds Z-axis holes -> none
    assert h == []
    cyl = [f for f in m.faces if f.kind == "CYLINDER"]
    assert len(cyl) == 1 and close(cyl[0].radius, 4.2 / 2)
    assert close(m.threads[0].at[0], 10) and m.threads[0].axis == (-1.0, 0.0, 0.0)


def test_real_thread_adds_material_and_fasteners_build():
    m = ck.run_script('''
rt = tap(Box(20, 20, 10), "M8", at=(0, 0, 5), depth=8, real=True)
b = Pos(30, 0, 0) * bolt("M5", 12, head="hex", real=True)
s = Pos(50, 0, 0) * bolt("M4", 10, head="socket")
n = Pos(70, 0, 0) * nut("M4")
w = Pos(90, 0, 0) * washer("M4")
result = {"T": rt, "B": b, "S": s, "N": n, "W": w}
''')
    t = m.body_by_name("T")
    cosmetic = ck.run_script('result = tap(Box(20, 20, 10), "M8", at=(0, 0, 5), depth=8)').volume
    plain = 20 * 20 * 10
    bored_at_major = plain - math.pi * 4 ** 2 * 8.3
    assert bored_at_major < t.volume < cosmetic   # real thread: hole bored at major, ridges added back (less than tap-drill solid)
    b = m.body_by_name("B")
    assert close(b.bbox_max[2] - b.bbox_min[2], 12 + 3.5, 0.05)     # 12 mm shank + hex head height
    assert len(b.shape.faces()) > 40                                 # helical thread faces present
    s = m.body_by_name("S")
    assert close(s.bbox_max[2] - s.bbox_min[2], 14, 0.05) and close(s.bbox_min[2], -10, 0.05)
    assert close(m.body_by_name("W").bbox_max[2] - m.body_by_name("W").bbox_min[2], 0.8)
    assert m.threads == [x for x in m.threads if not x.external] and len(m.threads) == 1
    with pytest.raises(ck.CadError):
        ck.run_script('result = bolt("M4", 10, head="torx")')


def test_drawing_calls_out_threads(workspace):
    import drawing
    m = ck.run_script('result = tap(tap(Box(40, 30, 8), "M4", at=(-10, 0, 4), depth=6), "M6", at=(10, 0, 4), through=True)')
    res = drawing.drawings_for_model(m, workspace / "drawings" / "t", "t")
    svg = open(res[0]["svg"]).read()
    assert "1× M4×0.7 ↧6" in svg and "1× M6×1 THRU" in svg
    rows = {r["thread"] for r in res[0]["holes"] if r["view"] == "top"}
    assert rows == {"M4×0.7 ↧6", "M6×1 THRU"}


def test_helpers_are_safe_inside_builders_and_locations():
    """bolt/tap/hole/... inside BuildPart + Locations must neither raise nor add to the builder."""
    import cad_kernel as ck
    m = ck.run_script('''
blank = Box(10, 10, 10)
with BuildPart() as bp:
    Box(20, 20, 5)
    with Locations((5, 5, 0), (-5, -5, 0)):
        b = bolt("M3", 10, head="socket")
        w = washer("M3")
        t = tap(blank, "M3", at=(0, 0, 5), depth=4)
result = {"Plate": bp.part, "Bolt": Pos(0, 0, 20) * b, "Tapped": Pos(-30, 0, 0) * t}
''')
    assert abs(m.body_by_name("Plate").volume - 20 * 20 * 5) < 1e-6
    assert abs(m.body_by_name("Bolt").bbox_min[2] - 10) < 0.01


def test_real_bolts_are_reused_and_independent():
    from build123d import Pos
    a = thr.bolt("M2", 5, head="socket", real=True)
    b = thr.bolt("M2", 5, head="socket", real=True)
    assert a is not b and abs(a.volume - b.volume) < 1e-9
    moved = Pos(10, 0, 0) * a
    assert abs(b.bounding_box().center().X) < 0.01 and abs(moved.bounding_box().center().X - 10) < 0.01


def test_draft_mode_builds_real_threads_plain_and_counts_them():
    code = '''
s = kit.socket_screw("M3", 10, real=True)
b = bolt("M2", 6, head="socket", real=True)
p = tap(Box(10, 10, 6), "M3", at=(0, 0, 3), depth=4, real=True)
result = {"S": s, "B": Pos(10, 0, 0) * b, "P": Pos(-15, 0, 0) * p}
'''
    draft = ck.run_script(code, threads="draft")
    real = ck.run_script(code)
    helix = lambda m: sum(1 for f in m.faces if f.kind == "BSPLINE")  # noqa: E731
    assert draft.draft_threads >= 3 and helix(draft) == 0 and "built plain" in draft.summary()
    assert real.draft_threads == 0 and helix(real) > 0 and "built plain" not in real.summary()
    again = ck.run_script(code, threads="draft")                  # the cache must not hand a draft screw to a real build
    assert helix(ck.run_script(code)) == helix(real) and again.draft_threads >= 1



@pytest.mark.parametrize("size,depth", [("M2", 7), ("M2.5", 9), ("M4", 10), ("M5", 15)])
def test_real_tapped_holes_stay_one_solid(size, depth):
    """OCCT's fuse-then-clean silently returned an EMPTY part for these (M2 over ~5 mm, M4, M5)."""
    from build123d import Align, Box
    x = thr.tap(Box(14, 14, 20, align=(Align.CENTER, Align.CENTER, Align.MIN)), size, at=(0, 0, 20), depth=depth, real=True)
    assert len(x.solids()) == 1 and x.volume > 3000 and any(f.geom_type.name == "BSPLINE" for f in x.faces())


def test_countersink_is_a_90_degree_cone_widest_at_the_surface():
    """Regression: the countersink cone was cut upside down (an undercut, Ø4.2 at the surface widening to Ø5 inside),
    so correct 90° screw heads interfered with their seats."""
    import math
    import build123d as b
    from threads import hole
    plate = b.Box(20, 20, 2, align=(b.Align.CENTER, b.Align.CENTER, b.Align.MIN))
    for axis, at, sign in (((0, 0, -1), (0, 0, 2), -1), ((0, 0, 1), (0, 0, 0), 1)):
        h = hole(plate, 2.2, at=at, through=True, axis=axis, countersink=4.0)
        for depth, r_expected in ((0.1, 1.9), (0.45, 1.55), (0.85, 1.15), (1.0, 1.1)):
            sec = h & b.Pos(0, 0, at[2] + sign * depth) * b.Box(30, 30, 1e-3)
            r = math.sqrt((400 * 1e-3 - sec.volume) / 1e-3 / math.pi)
            assert abs(r - r_expected) < 0.01, (axis, depth, r)
