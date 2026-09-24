import math
import pytest

import cad_kernel as ck
from conftest import bbox_size, close


def test_default_script_builds_two_bodies(demo_model):
    m = demo_model
    assert [b.path for b in m.bodies] == ["Bracket", "Pin"]
    assert len(m.faces) == 20
    assert close(bbox_size(m)[0], 60) and close(bbox_size(m)[1], 40) and close(bbox_size(m)[2], 45)
    assert m.mesh["exact"] is True
    assert m.summary(3).startswith("Model OK: 2 bodies")


def test_face_ids_are_global_and_contiguous(demo_model):
    ids = [f.id for f in demo_model.faces]
    assert ids == list(range(len(ids)))
    for b in demo_model.bodies:
        assert b.face_ids == sorted(b.face_ids)
        assert len(b.face_ids) == len(b.shape.faces())
        assert len(b.edge_ids) == len(b.shape.edges())
    # every mesh triangle range maps back to a known face
    for bd in demo_model.mesh["bodies"]:
        for fid, start, count in bd["faceRanges"]:
            assert fid in {f.id for f in demo_model.faces}
            assert count % 3 == 0


def test_display_normals_point_outward():
    """Exact per-vertex normals: each must point away from the solid (box + boss test)."""
    from build123d import Box, Cylinder, Pos
    m = ck.build_model(Box(10, 10, 10) + Pos(0, 0, 5) * Cylinder(3, 5))
    bd = m.mesh["bodies"][0]
    P, N = bd["positions"], bd["normals"]
    assert len(N) == len(P) > 0
    inward = 0
    for i in range(0, len(P), 3):
        p, n = P[i:i + 3], N[i:i + 3]
        d = (p[0], p[1], p[2] - 2.5)
        if sum(a * b for a, b in zip(d, n)) < -1e-6:
            inward += 1
    assert inward == 0


def test_quality_presets_change_triangle_count():
    counts = {}
    for q in ("draft", "normal", "fine"):
        m = ck.run_script(ck.DEFAULT_CODE, q)
        counts[q] = sum(len(b["indices"]) for b in m.mesh["bodies"])
    assert counts["draft"] < counts["normal"] < counts["fine"]


def test_disjoint_solids_are_split_into_bodies():
    m = ck.run_script("result = Box(10,10,10) + Pos(30,0,0)*Box(10,10,10)")
    assert [b.path for b in m.bodies] == ["Body1 1", "Body1 2"]


def test_nested_dict_makes_component_tree():
    m = ck.run_script('result = {"Asm": {"A": Box(10,10,10), "B": Pos(30,0,0)*Box(5,5,5)}, "Loose": Pos(0,30,0)*Sphere(4)}')
    assert [b.path for b in m.bodies] == ["Asm/A", "Asm/B", "Loose"]
    ids = [n["id"] for n in m.mesh["tree"]]
    assert ids == ["c:Asm", "b:0", "b:1", "b:2"]
    assert m.body_by_name("Asm/B").name == "B"


@pytest.mark.parametrize("code,msg", [
    ("x = 1", "assign the final shape"),
    ("result = 42", "must be build123d shapes"),
    ("result = Box(1,1,", "SyntaxError"),
    ("result = Box(1,1,1).nonexistent()", "AttributeError"),
])
def test_script_errors_are_cad_errors(code, msg):
    with pytest.raises(ck.CadError) as ei:
        ck.run_script(code)
    assert msg in str(ei.value)


def test_empty_result_is_an_empty_design():
    for code in ("result = None\n", "result = {}\n", ck.NEW_DESIGN_CODE):
        m = ck.run_script(code)
        assert m.bodies == [] and m.faces == [] and m.mesh["bodies"] == [] and m.mesh["tree"] == []
        assert "empty design" in m.summary() and m.mesh["bboxMin"] == [0.0, 0.0, 0.0]


def test_get_face_and_get_edge(demo_model):
    f = demo_model.get_face(demo_model.faces[0].id)
    assert close(float(f.area), demo_model.faces[0].area, 1e-2)
    e = demo_model.get_edge(demo_model.bodies[0].edge_ids[0])
    assert e.length > 0
    with pytest.raises(ck.CadError):
        demo_model.get_face(9999)


def test_step_export_is_exact_and_keeps_bodies_separate(demo_model, workspace):
    (path,) = ck.export(demo_model, workspace / "exports", "demo", ["step"])
    txt = path.read_text()
    assert txt.count("MANIFOLD_SOLID_BREP") == 2
    assert "CYLINDRICAL_SURFACE" in txt and "B_SPLINE_SURFACE" not in txt


def test_stl_tolerance_controls_tessellation(demo_model, workspace):
    fine = ck.export(demo_model, workspace / "exports", "fine", ["stl"], 0.005, 0.03)[0].stat().st_size
    coarse = ck.export(demo_model, workspace / "exports", "coarse", ["stl"], 0.2, 0.5)[0].stat().st_size
    assert fine > 3 * coarse


def test_single_body_export(demo_model, workspace):
    (path,) = ck.export(demo_model, workspace / "exports", "one", ["step"], body="Pin")
    assert path.name == "one_Pin.step"
    assert path.read_text().count("MANIFOLD_SOLID_BREP") == 1
    with pytest.raises(ck.CadError):
        ck.export(demo_model, workspace / "exports", "x", ["step"], body="Nope")


def test_script_namespace_has_helpers():
    ns = ck.script_namespace()
    assert ns["import_step"] is ck.import_step and ns["from_library"] is ck.from_library
    assert "BuildPart" in ns and "Box" in ns


# --------------------------------------------------------------------------- measurement
def _edges(model, kind):
    return [e for bd in model.mesh["bodies"] for e in bd["edgeInfo"] if e["kind"] == kind]


def test_measure_two_parallel_faces_gives_plane_gap(demo_model):
    top = next(f for f in demo_model.faces if f.body_name == "Bracket" and f.kind == "PLANE" and f.normal[2] > 0.99 and close(f.center[2], 4))
    bot = next(f for f in demo_model.faces if f.body_name == "Bracket" and f.kind == "PLANE" and f.normal[2] < -0.99)
    r = ck.measure(demo_model, faces=[top.id, bot.id])
    assert r["relation"] == "parallel"
    assert close(r["plane_gap"], 8) and close(r["distance"], 8)


def test_measure_perpendicular_lines():
    m = ck.run_script("result = Box(20, 10, 5)")
    lines = _edges(m, "LINE")
    x_edge = next(e for e in lines if abs(e["dir"][0]) > 0.99)
    z_edge = next(e for e in lines if abs(e["dir"][2]) > 0.99)
    r = ck.measure(m, edges=[x_edge["id"], z_edge["id"]])
    assert r["angle_kind"] == "line-to-line" and close(r["angle"], 90)
    assert r["relation"].startswith("perpendicular")


def test_measure_hole_spacing(demo_model):
    circles = [e for e in _edges(demo_model, "CIRCLE") if e.get("closed") and close(e["radius"], 1.6) and close(e["center"][2], 4)]
    a = next(e for e in circles if e["center"][0] < 0 and e["center"][1] < 0)
    b = next(e for e in circles if e["center"][0] > 0 and e["center"][1] < 0)
    r = ck.measure(demo_model, edges=[a["id"], b["id"]])
    assert close(r["center_distance"], 44)
    assert close(r["items"][0]["radius"], 1.6)


def test_measure_point_to_face(demo_model):
    bot = next(f for f in demo_model.faces if f.body_name == "Bracket" and f.kind == "PLANE" and f.normal[2] < -0.99)
    r = ck.measure(demo_model, entities=[{"type": "point", "p": [10, 0, 10]}, {"type": "face", "id": bot.id}])
    assert close(r["distance"], 14)


def test_mass_properties_and_density(demo_model):
    alu = ck.mass_properties(demo_model, "Bracket", "aluminium")
    steel = ck.mass_properties(demo_model, "Bracket", 7.85)
    assert close(alu["volume_mm3"], steel["volume_mm3"])
    assert close(steel["mass_g"] / alu["mass_g"], 7.85 / 2.7, 1e-2)
    assert alu["bbox_size"] == [60.0, 40.0, 28.0]
    with pytest.raises(ck.CadError):
        ck.mass_properties(demo_model, "Bracket", "unobtainium")


def test_clearance_and_interference():
    clear = ck.run_script('result = {"A": Box(10,10,10), "B": Pos(15,0,0)*Box(10,10,10)}')
    r = ck.clearance(clear, "A", "B")
    assert r["status"] == "clear" and close(r["min_distance"], 5)
    hit = ck.run_script('result = {"A": Box(10,10,10), "B": Pos(8,0,0)*Box(10,10,10)}')
    r = ck.clearance(hit, "A", "B")
    assert r["status"] == "INTERFERING" and close(r["interference_volume_mm3"], 200, 0.5)
