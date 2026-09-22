import pytest

import cad_kernel as ck
import drawing
from library import PartLibrary
from conftest import close


def test_drawings_one_sheet_per_body_plus_assembly(demo_model, workspace):
    res = drawing.drawings_for_model(demo_model, workspace / "drawings" / "demo", "demo", material="Aluminium", density=2.7)
    assert [r["body"] for r in res] == ["Bracket", "Pin", "assembly"]
    svg = open(res[0]["svg"]).read()
    for label in ("FRONT", "TOP", "RIGHT", "ISO", "HOLE TABLE", "TITLE", "MASS"):
        assert label in svg
    assert res[0]["views"]["top"]["holes"] == 5 and res[0]["views"]["front"]["holes"] == 0
    top_rows = [h for h in res[0]["holes"] if h["view"] == "top"]
    assert sorted(round(h["d"], 2) for h in top_rows) == [3.2, 3.2, 3.2, 3.2, 6.0]
    assert all(r.get("dxf") for r in res)
    assert open(res[0]["dxf"]).read().count("HIDDEN") >= 1


def test_drawing_scale_fits_sheet():
    from build123d import Box
    big = ck.build_model(Box(500, 300, 20))
    res = drawing.drawings_for_model(big, __import__("pathlib").Path(__import__("tempfile").mkdtemp()), "big")
    assert res[0]["scale"] < 1


def test_library_script_part_with_param_override(workspace):
    lib = PartLibrary(workspace)
    meta = lib.save_script("hex standoff", "size = 10\nh = 20\nresult = Cylinder(size / 2, h)\n", description="test")
    assert meta["params"] == {"size": 10, "h": 20} and lib.get("hex standoff")["kind"] == "script"
    shape = lib.load_shape("hex standoff", h=35)
    assert close(shape.bounding_box().size.Z, 35)
    with pytest.raises(ck.CadError):
        lib.load_shape("hex standoff", bogus=1)
    with pytest.raises(ck.CadError):
        lib.load_shape("missing")


def test_library_step_part_and_from_library_in_script(workspace, demo_model):
    lib = PartLibrary(workspace)
    from build123d import export_step, Compound
    pin = demo_model.body_by_name("Pin").shape
    path = workspace / "pin.step"
    export_step(Compound(children=[pin]), str(path))
    lib.save_step("pin", path.read_bytes())
    ck.LIBRARY = lib
    m = ck.run_script('base = Box(20, 20, 4)\np = Pos(0, 0, 20) * from_library("pin")\nresult = {"Base": base, "Pin": p}')
    assert [b.path for b in m.bodies] == ["Base", "Pin"]
    assert close(m.body_by_name("Pin").volume, pin.volume, 1e-2)
    assert "pin" in lib.describe() and lib.delete("pin") and lib.get("pin") is None


def test_import_step_resolves_workspace_imports(workspace, demo_model):
    from build123d import export_step, Compound
    export_step(Compound(children=[demo_model.body_by_name("Bracket").shape]), str(workspace / "imports" / "b.step"))
    m = ck.run_script('result = {"B": import_step("b.step")}')
    assert len(m.bodies) == 1 and len(m.faces) == 17
    with pytest.raises(ck.CadError):
        ck.run_script('result = import_step("nope.step")')


def test_two_workspaces_in_one_process_do_not_share_libraries(tmp_path):
    """Regression: from_library / import_step are bound per run, not via module globals."""
    a, b = tmp_path / "a", tmp_path / "b"
    for ws in (a, b):
        (ws / "imports").mkdir(parents=True)
    la, lb = PartLibrary(a), PartLibrary(b)
    la.save_script("peg", "result = Cylinder(2, 10)\n")
    lb.save_script("peg", "result = Cylinder(2, 30)\n")
    ma = ck.run_script('result = {"P": from_library("peg")}', workspace=a, library=la)
    mb = ck.run_script('result = {"P": from_library("peg")}', workspace=b, library=lb)
    assert close(mb.bbox_max[2] - mb.bbox_min[2], 30) and close(ma.bbox_max[2] - ma.bbox_min[2], 10)
    with pytest.raises(ck.CadError):
        ck.run_script('result = from_library("peg")', workspace=a, library=PartLibrary(tmp_path / "empty"))
