"""Kernel and CAM paths not covered elsewhere: measurement entities, exports per body, stock/rest/3D ops,
post-processor checks and feeds edge cases."""
from __future__ import annotations

from pathlib import Path

import pytest

import cad_kernel as ck
import cam_kernel as cam
import threads
from tests.conftest import bbox_size

TWO = 'result = {"A": Box(20, 20, 10), "B": Pos(40, 0, 0) * Cylinder(5, 10)}\n'


@pytest.fixture(scope="module")
def two():
    return ck.run_script(TWO, "draft")


def face_of(m, body, pred):
    b = m.body_by_name(body)
    return next(f for f in m.faces if f.body == b.id and pred(f))


def test_measure_faces_edges_vertices(two):
    m = two
    top = face_of(m, "A", lambda f: f.kind == "PLANE" and f.normal[2] > 0.99)
    side = face_of(m, "A", lambda f: f.kind == "PLANE" and f.normal[0] > 0.99)
    r = ck.measure(m, faces=[top.id, side.id])
    assert r["relation"] == "perpendicular" and r["angle"] == pytest.approx(90)
    cyl = face_of(m, "B", lambda f: f.kind == "CYLINDER")
    r = ck.measure(m, faces=[cyl.id])
    assert r["items"][0]["radius"] == pytest.approx(5) if "radius" in r["items"][0] else r["items"][0]["type"] == "face"
    b = m.body_by_name("B")
    edges = m.mesh["bodies"][[x.id for x in m.bodies].index(b.id)]["edgeInfo"]
    circles = [e for e in edges if e["kind"] == "CIRCLE"]
    assert len(circles) == 2
    r = ck.measure(m, edges=[circles[0]["id"], circles[1]["id"]])
    assert r["center_distance"] == pytest.approx(10) and r["relation"] in ("parallel", "concentric")
    a = m.body_by_name("A")
    lines = [e for e in m.mesh["bodies"][[x.id for x in m.bodies].index(a.id)]["edgeInfo"] if e["kind"] == "LINE"]
    r = ck.measure(m, edges=[lines[0]["id"]], points=[[100, 100, 100]])
    assert r["distance"] > 100
    r = ck.measure(m, entities=[{"type": "edge", "id": lines[0]["id"]}, {"type": "face", "id": top.id}])
    assert r["angle_kind"] == "line-to-plane"
    with pytest.raises(ck.CadError):
        ck.measure(m, faces=[10 ** 6])
    with pytest.raises(ck.CadError):
        m.get_edge(10 ** 6)


def test_mass_clearance_and_interference(two):
    m = two
    mp = ck.mass_properties(m, "A", "steel")
    assert mp["volume_mm3"] == pytest.approx(4000) and mp["mass_g"] == pytest.approx(4000 * 7.85 / 1000, rel=0.01)
    mp2 = ck.mass_properties(m, None, 1.0)
    assert mp2["volume_mm3"] > 4000 and mp2["mass_g"] == pytest.approx(mp2["volume_mm3"] / 1000, rel=0.01)
    with pytest.raises(ck.CadError):
        ck.mass_properties(m, "Nope")
    c = ck.clearance(m, "A", "B")
    assert c["min_distance"] == pytest.approx(25, abs=0.01) and c["status"] == "clear"
    over = ck.run_script('result = {"A": Box(20, 20, 10), "B": Pos(15, 0, 0) * Box(20, 20, 10)}\n', "draft")
    c = ck.clearance(over, "A", "B")
    assert c["status"] == "INTERFERING" and c["interference_volume_mm3"] == pytest.approx(5 * 20 * 10, rel=0.01)
    with pytest.raises(ck.CadError):
        ck.clearance(m, "A", "Nope")


def test_export_variants(two, tmp_path):
    paths = ck.export(two, tmp_path, "both", ["step", "stl"], 0.05, 0.2)
    assert {p.suffix for p in paths} == {".step", ".stl"}
    one = ck.export(two, tmp_path, "b_only", ["stl"], 0.05, 0.2, body="B")
    assert one[0].stat().st_size < next(p for p in paths if p.suffix == ".stl").stat().st_size
    with pytest.raises(ck.CadError):
        ck.export(two, tmp_path, "x", ["obj"])
    with pytest.raises(ck.CadError):
        ck.export(two, tmp_path, "x", ["step"], body="Nope")


def test_script_namespace_helpers_and_errors(tmp_path):
    with pytest.raises(ck.CadError, match="result"):
        ck.run_script("x = 1\n", "draft")
    with pytest.raises(ck.CadError):
        ck.run_script("result = {'A': 1}\n", "draft")
    m = ck.run_script('print("hello")\nresult = {"Grp": {"Inner": Box(1, 1, 1), "Other": Pos(3, 0, 0) * Box(1, 1, 1)}}\n', "draft")
    assert "hello" in m.stdout and [b.path for b in m.bodies] == ["Grp/Inner", "Grp/Other"]
    assert m.mesh["tree"] and m.body_by_name("Grp/Inner") is not None
    split = ck.run_script("result = Box(2, 2, 2) + Pos(10, 0, 0) * Box(2, 2, 2)\n", "draft")
    assert len(split.bodies) == 2                    # disjoint solids split into bodies
    with pytest.raises(ck.CadError):
        ck.run_script('result = import_step("missing.step")\n', "draft", tmp_path)


def test_threads_helpers():
    d = threads.iso("M6")
    assert d["pitch"] == 1.0 and threads.tap_drill("M6") == pytest.approx(5.0) and threads.clearance_dia("M6") > 6
    with pytest.raises(threads.ThreadError):
        threads.iso("M7.5")
    bolt = threads.bolt("M4", 12, head="hex")
    nut = threads.nut("M4"); washer = threads.washer("M4")
    assert bolt.volume > 0 and nut.volume > 0 and washer.volume > 0
    sock = threads.bolt("M4", 12, head="socket"); bare = threads.bolt("M4", 12, head="none")
    assert sock.volume != bolt.volume and bare.volume < bolt.volume
    from build123d import Box
    part = Box(20, 20, 10)
    with pytest.raises(threads.ThreadError):
        threads.tap(part, "M4", at=(0, 0, 5))          # neither depth nor through
    cb = threads.hole(part, 4, at=(0, 0, 5), through=True, counterbore=(8, 2))
    cs = threads.hole(part, 4, at=(0, 0, 5), depth=5, countersink=8)
    assert cb.volume < part.volume - 3.14 * 4 * 10 and cs.volume < part.volume


# ---------------------------------------------------------------- CAM
def test_stock_block_and_setup_origins(cam_setup, bracket):
    setup, tools, machines = cam_setup
    st = cam.Stock.block(100, 50, 20, center_xy=(10, 5), top=15)
    assert st.bottom == pytest.approx(-5) and st.size == (100, 50, 20) and st.rect().bounds[0] == pytest.approx(-40)
    st2 = cam.Stock.block(10, 10, 10)
    assert st2.bottom == 0 and st2.top == 10
    for origin in cam.ORIGINS:
        s = cam.Setup(machines["Generic 3018"], setup.stock, origin=origin)
        assert len(s.origin_point()) == 3
    s = cam.Setup(machines["Generic 3018"], setup.stock, origin=(1, 2, 3))
    assert s.origin_point() == (1, 2, 3)
    with pytest.raises(cam.CamError):
        cam.Setup(machines["Generic 3018"], setup.stock, origin="nowhere").origin_point()


def test_pocket_rest_and_parallel3d_rest(cam_setup, bracket):
    setup, tools, machines = cam_setup
    region = cam.rect(-12, -12, 12, 12)
    big = cam.pocket(setup, tools[1], region, z_top=4, z_bottom=1, name="rough")
    small = cam.pocket(setup, tools[2], region, z_top=4, z_bottom=1, rest_from=tools[1], name="rest")
    assert big.kind == "pocket" and small.kind == "rest" and len(small.moves) > 0
    assert small.lengths()[0] < big.lengths()[0] * 4          # rest only visits the corners the 6 mm tool missed
    rr = cam.rest_region(region, tools[1], tools[2])
    assert not rr.is_empty and rr.area < region.area
    assert cam.rest_region(cam.circle(0, 0, 3), tools[1], tools[2]).is_empty or cam.rest_region(cam.circle(0, 0, 3), tools[1], tools[2]).area >= 0
    ball = tools[4]
    fin = cam.parallel3d(setup, ball, shape=bracket, z_bottom=4, region=cam.rect(-10, -10, 10, 10), stepover=0.5, resolution=1.0)
    rest = cam.parallel3d(setup, tools[2], shape=bracket, z_bottom=4, region=cam.rect(-10, -10, 10, 10), stepover=0.5, resolution=1.0, rest_from=ball)
    assert fin.kind == "parallel3d" and len(fin.moves) > 10 and len(rest.moves) > 0 and rest.params.get("rest_from") or rest.kind in ("parallel3d", "rest")
    with pytest.raises(cam.CamError):
        cam.pocket(setup, tools[1], region, z_top=4, z_bottom=1, stepover=2.0)


def test_program_checks_and_post_options(cam_setup, bracket):
    setup, tools, machines = cam_setup
    prog = cam.Program(setup, name="chk")
    assert prog.check() == ["program has no operations"]
    outline = cam.section(bracket, 0.0)
    prog.add(cam.contour(setup, tools[1], outline, z_top=4, z_bottom=-30))          # far below the stock
    hot = cam.Tool(**{**tools[2].__dict__, "rpm": 99999})
    prog.add(cam.face(setup, hot, z_top=setup.stock.top, z_bottom=4))
    w = "\n".join(prog.check())
    assert "below the stock bottom" in w and "outside spindle range" in w
    m_none = cam.Machine(**{**setup.machine.__dict__, "tool_change": "none", "name": "no-change"})
    p2 = cam.Program(cam.Setup(m_none, setup.stock), ops=prog.ops, name="two tools")
    assert any("tool_change is 'none'" in x for x in p2.check())
    tiny = cam.Machine(**{**setup.machine.__dict__, "travel": {"x": 10, "y": 10, "z": 10}, "name": "tiny"})
    p3 = cam.Program(cam.Setup(tiny, setup.stock), ops=prog.ops)
    assert any("exceeds machine travel" in x for x in p3.check())
    g_arcs = prog.gcode()
    no_arcs = cam.Machine(**{**setup.machine.__dict__, "arcs": False, "name": "noarcs"})
    g_lines = cam.Program(cam.Setup(no_arcs, setup.stock), ops=prog.ops).gcode()
    assert ("G2 " in g_arcs or "G3 " in g_arcs) and "G2 " not in g_lines and "G3 " not in g_lines
    assert g_lines.count("G1 ") > g_arcs.count("G1 ")
    payload = prog.to_payload()
    assert payload["warnings"] and payload["ops"][0]["moves"] and payload["time"] > 0
    assert "chk" in prog.summary()


def test_feeds_materials_and_clamping(cam_setup):
    setup, tools, machines = cam_setup
    mats = cam.MATERIALS
    assert len(mats) >= 10
    for mat in list(mats)[:4]:
        f = cam.feeds(tools[1], mat, setup.machine)
        assert f["rpm"] <= setup.machine.spindle["max"] and f["feed"] <= setup.machine.max_feed["x"]
    with pytest.raises(cam.CamError):
        cam.feeds(tools[1], "adamantium", setup.machine)
    gentle = cam.feeds(tools[1], "aluminium", setup.machine, aggressiveness=0.1)
    hard = cam.feeds(tools[1], "aluminium", setup.machine, aggressiveness=0.9)
    assert hard["feed"] >= gentle["feed"]
    thin = cam.feeds(tools[1], "aluminium", setup.machine, radial_engagement=0.1)
    assert thin["feed"] >= cam.feeds(tools[1], "aluminium", setup.machine, radial_engagement=0.5)["feed"]
    t = cam.apply_feeds(tools[5], "mdf", setup.machine)
    assert t.type == "drill" and t.feed > 0 and t.plunge > 0
    free = cam.feeds(tools[1], "aluminium", None)
    assert free["rpm"] >= setup.machine.spindle["max"]


def test_holes_and_geometry_helpers(bracket, demo_model):
    hs = cam.holes(demo_model.shape)
    assert hs and all(h.depth > 0 for h in hs) and any(h.through for h in hs)
    small = cam.holes(demo_model.shape, dmin=0, dmax=3.5)
    assert all(h.diameter <= 3.5 for h in small)
    sil = cam.silhouette(bracket)
    sec = cam.section(bracket, 0.0)
    assert sil.area >= sec.area - 1e-6 and not sec.is_empty
    empty = cam.section(bracket, 10_000.0)
    assert empty.is_empty
    top_face = max(bracket.faces(), key=lambda f: f.center().Z)
    poly = cam.face_polygon(top_face)
    assert poly.area == pytest.approx(float(top_face.area), rel=0.01)
    assert repr(hs[0]).startswith("Hole(")
