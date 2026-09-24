"""CadAgent manual operations and design management, without a Claude session.
Every op is written into the script (design-as-code); we check the script AND the rebuilt geometry."""
from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path

import pytest

import cad_kernel as ck
import cam_kernel as cam
import script_edit
from agent import CadAgent

PLATE = '''plate_l, plate_w, plate_t = 60, 40, 8
with BuildPart() as plate:
    Box(plate_l, plate_w, plate_t)
    with Locations((0, 0, plate_t / 2)):
        Cylinder(10, 15, align=(Align.CENTER, Align.CENTER, Align.MIN))
    Hole(4)
result = {"Plate": plate.part}
'''
TWO = 'result = {"A": Box(20, 20, 10), "B": Pos(40, 0, 0) * Cylinder(5, 10)}\n'


class Recorder:
    def __init__(self):
        self.events: list[dict] = []

    async def emit(self, e):
        self.events.append(e)

    def kinds(self):
        return [e["type"] for e in self.events]

    def last(self, kind):
        return next((e for e in reversed(self.events) if e["type"] == kind), None)

    def texts(self, kind):
        return [e.get("text", "") for e in self.events if e["type"] == kind]


@pytest.fixture
def ag(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    for sub in ("exports", "history", "designs", "imports", "images", "drawings"):
        (ws / sub).mkdir(parents=True)
    rec = Recorder()

    async def shot(spec):
        raise RuntimeError("no browser in tests")
    a = CadAgent(ws, rec.emit, shot)
    a.rec = rec
    a.quality = "draft"
    return a


async def build(a, code=PLATE):
    return await a.build(code, source="user")


# ------------------------------------------------------------------ build / quality / undo / history
async def test_build_records_history_and_notes(ag):
    m = await build(ag)
    assert m.volume > 0 and (ag.workspace / "model.py").read_text() == PLATE
    assert len(ag._history_files()) == 1
    assert ag.rec.last("model")["source"] == "user" and ag.rec.last("model")["history_len"] == 1
    assert any("edited and rebuilt" in n for n in ag.notes)
    with pytest.raises(ck.CadError):
        await ag.build("result = Box(", source="user")
    assert ag.model.code == PLATE                      # failed build leaves the model untouched


async def test_set_quality_retessellates_without_history(ag):
    await build(ag)
    n_draft = len(ag.model.mesh["bodies"][0]["positions"])
    await ag.set_quality("fine")
    assert ag.quality == "fine" and len(ag.model.mesh["bodies"][0]["positions"]) > n_draft
    assert ag.rec.last("model")["source"] == "requality" and len(ag._history_files()) == 1
    await ag.set_quality("bogus")
    assert ag.quality == "fine"


async def test_undo_reverts_and_moves_history(ag):
    await build(ag)
    await ag.undo()
    assert "Nothing to undo" in ag.rec.texts("error")[-1]
    await build(ag, TWO)
    assert len(ag.model.bodies) == 2
    await ag.undo()
    assert ag.model.code == PLATE and ag.rec.last("model")["source"] == "revert"
    assert list((ag.workspace / "history" / "undone").glob("*.py"))


# ------------------------------------------------------------------ designs
async def test_save_open_new_import_and_dirty(ag):
    await build(ag)
    assert ag.dirty
    name = await ag.save_design("My Plate!")
    assert name == "my_plate" or name.lower().startswith("my")
    assert not ag.dirty and (ag.designs_dir / f"{name}.py").exists()
    assert [d["name"] for d in ag.list_designs()] == [name]
    await ag.set_cam_code("program = None", rebuild=False)
    assert ag.dirty                                    # CAM script differs from the saved one
    await ag.new_design()
    assert ag.design_name is None and ag.model.code == ck.NEW_DESIGN_CODE
    await ag.open_design(name)
    assert ag.design_name == name and ag.model.code == PLATE and not ag.dirty
    with pytest.raises(ck.CadError):
        await ag.open_design("nope")
    imported = await ag.import_design("other.py", TWO)
    assert imported == "other" and len(ag.model.bodies) == 2
    with pytest.raises(ck.CadError):
        await ag.import_design("bad.py", "result = Box(")
    st = json.loads((ag.workspace / "state.json").read_text())
    assert st["design"] == "other"


async def test_load_initial_reopens_and_falls_back(ag):
    await build(ag)
    await ag.save_design("keep")
    ag2 = CadAgent(ag.workspace, ag.rec.emit, ag.screenshot_fn)
    ag2.load_initial()
    assert ag2.design_name == "keep" and ag2.model.code == PLATE
    (ag.workspace / "model.py").write_text("result = Box(")          # corrupt working copy
    ag3 = CadAgent(ag.workspace, ag.rec.emit, ag.screenshot_fn)
    ag3.load_initial()
    assert ag3.model.code == ck.NEW_DESIGN_CODE and ag3.design_name is None and ag3.model.bodies == []


# ------------------------------------------------------------------ parameters
async def test_params_get_set(ag):
    await build(ag)
    names = {p["name"] for p in ag.get_params()}
    assert names >= {"plate_l", "plate_w", "plate_t"}
    await ag.set_params({"plate_l": 100, "plate_t": 10}, source="params")
    assert ag.model.bbox_max[0] == pytest.approx(50) and "plate_l, plate_w, plate_t = 100, 40, 10" in ag.model.code
    assert any("parameter" in n.lower() for n in ag.notes)
    with pytest.raises(script_edit.Refused):
        await ag.set_params({"does_not_exist": 1})         # unknown names must not be silently ignored


# ------------------------------------------------------------------ bodies: rename / delete / add / transform
async def test_edit_body_rename_delete_refused(ag):
    await build(ag, TWO)
    await ag.edit_body("rename", "A", "Base")
    assert {b.name for b in ag.model.bodies} == {"Base", "B"} and '"Base"' in ag.model.code
    await ag.edit_body("delete", "B")
    assert [b.name for b in ag.model.bodies] == ["Base"]
    await ag.edit_body("delete", "Base")                             # last body: refused
    assert any("can't delete" in t for t in ag.rec.texts("error"))
    assert [b.name for b in ag.model.bodies] == ["Base"]


async def test_add_primitive_and_name_dedupe(ag):
    await build(ag, TWO)
    await ag.add_primitive("box", {"x": 5, "y": 6, "z": 7}, [0, 0, 30])
    await ag.add_primitive("cylinder", {"d": 4, "h": 3}, [0, 0, 0], "Peg")
    await ag.add_primitive("sphere", {"d": 6}, [10, 10, 10], "Peg")   # same name → "Peg 2"
    names = [b.name for b in ag.model.bodies]
    assert names == ["A", "B", "Box", "Peg", "Peg 2"]
    assert "Pos(0, 0, 30) * Box(5, 6, 7)" in ag.model.code and "Sphere(3)" in ag.model.code
    await ag.add_primitive("cone", {}, [0, 0, 0])
    assert "unknown primitive" in ag.rec.texts("error")[-1]


async def test_transform_body(ag):
    await build(ag, TWO)
    await ag.transform_body("B", [0, 0, 5], [0, 0, 0])
    b = ag.model.body_by_name("B")
    assert b.bbox_min[2] == pytest.approx(0) and "Pos(0, 0, 5) * (" in ag.model.code
    await ag.transform_body("A", [0, 0, 0], [0, 0, 45])
    assert "Rot(0, 0, 45) * (" in ag.model.code
    before = ag.model.code
    await ag.transform_body("A", [0, 0, 0], [0, 0, 0])              # no-op
    assert ag.model.code == before


# ------------------------------------------------------------------ ribbon ops (face/edge based, code-backed)
async def test_op_extrude_face_join_and_cut(ag):
    m = await build(ag)
    v0 = m.volume
    await ag.op_extrude_face("Plate", [7, 0, 19], [0, 0, 1], 5, "join")       # boss top up 5 → +π(10²−4²)·5
    assert ag.model.volume == pytest.approx(v0 + 3.14159 * (100 - 16) * 5, rel=0.01)
    assert "_plate + extrude(_plate.faces().sort_by_distance((7, 0, 19))[0], amount=5, dir=(0, 0, 1))" in ag.model.code
    v1 = ag.model.volume
    await ag.op_extrude_face("Plate", [-25, 15, 4], [0, 0, 1], -2, "join")    # negative = cut 2 mm into the plate top
    assert ag.model.volume < v1 and "- extrude(" in ag.model.code
    assert any("extrude face" in n for n in ag.notes)


async def test_op_hole_plain_through_tapped_counterbored(ag):
    m = await build(ag)
    v0 = m.volume
    await ag.op_hole("Plate", [20, 10, 4], [0, 0, 1], 5, None, True)
    assert ag.model.volume == pytest.approx(v0 - 3.14159 * 2.5 ** 2 * 8, rel=0.02)
    assert 'hole(_plate, 5, at=(20, 10, 4), through=True, axis=(0, 0, -1))' in ag.model.code or 'hole(_plate, 5, at=(20, 10, 4), through=True, axis=(0, -0, -1))' in ag.model.code
    await ag.op_hole("Plate", [-20, 10, 4], [0, 0, 1], 0, 6, False, thread="M4")
    assert 'tap(' in ag.model.code and ag.model.threads and ag.model.threads[0].label().startswith("M4")
    await ag.op_hole("Plate", [-20, -10, 4], [0, 0, 1], 3, 4, False, counterbore=[6, 2])
    assert "counterbore=(6, 2)" in ag.model.code
    assert len(cam.holes(ag.model.shape)) >= 4


async def test_op_edges_fillet_chamfer_and_shell(ag):
    m = await build(ag)
    n0 = len(m.faces)
    await ag.op_edges("Plate", [], 2, "fillet", face_centers=[[7, 0, 19]])     # all edges of the boss top
    assert len(ag.model.faces) == n0 + 2 and ".fillet(2, [*_plate.faces().sort_by_distance((7, 0, 19))[0].edges()])" in ag.model.code
    await ag.op_edges("Plate", [[30, 20, 0]], 1, "chamfer")                     # nearest edge to a plate corner
    assert ".chamfer(1, None, [" in ag.model.code and len(ag.model.faces) == n0 + 3
    await ag.op_edges("Plate", [], 1, "fillet")
    assert "pick at least one" in ag.rec.texts("error")[-1]
    v = ag.model.volume
    await ag.op_shell("Plate", [[0, 0, -4]], 1.5)                               # open the bottom face
    assert ag.model.volume < v * 0.6 and "offset(" in ag.model.code and "openings=[" in ag.model.code


async def test_op_primitive_join_cut_new(ag):
    m = await build(ag)
    v0 = m.volume
    await ag.op_primitive("cylinder", {"d": 4, "h": 6}, [20, 0, 4], [0, 0, 1], "Plate", "join")
    assert ag.model.volume == pytest.approx(v0 + 3.14159 * 4 * 6, rel=0.02) and len(ag.model.bodies) == 1
    await ag.op_primitive("box", {"x": 4, "y": 4, "z": 3}, [-20, 0, 4], [0, 0, -1], "Plate", "cut")
    assert ag.model.volume == pytest.approx(v0 + 3.14159 * 4 * 6 - 48, rel=0.02)
    await ag.op_primitive("sphere", {"d": 6}, [0, 30, 0], None, None, "new", "Ball")
    assert [b.name for b in ag.model.bodies] == ["Plate", "Ball"]
    await ag.op_primitive("torus", {}, [0, 0, 0], None, "Plate")
    assert "unknown primitive" in ag.rec.texts("error")[-1]


async def test_modify_body_unsupported_falls_back_gracefully(ag):
    await build(ag, "result = Box(10, 10, 10)\n")       # not a dict → wrap_body_expr unsupported
    ag.busy = True
    await ag.transform_body("Box", [1, 0, 0], [0, 0, 0])
    assert any("agent is busy" in t for t in ag.rec.texts("error"))


# ------------------------------------------------------------------ sketches
PLANE = {"origin": [0, 0, 4], "x_dir": [1, 0, 0], "z_dir": [0, 0, 1], "label": "top"}
ITEMS = [{"type": "rect", "cx": 18, "cy": 0, "w": 10, "h": 10, "angle": 0, "mode": "add"}]


async def test_sketch_set_extrude_delete(ag):
    m = await build(ag)
    v0 = m.volume
    await ag.set_sketch("sk1", PLANE, ITEMS)
    assert [s["name"] for s in ag.model.sketches] == ["sk1"] and ag.sketch_defs()[0]["items"] == ITEMS
    await ag.extrude_sketch("sk1", 5, "join", "Plate")
    assert ag.model.volume == pytest.approx(v0 + 500, rel=0.01)
    await ag.extrude_sketch("sk1", 3, "new")
    assert [b.name for b in ag.model.bodies] == ["Plate", "sk1 extrude"]
    await ag.extrude_sketch("sk1", 2, "cut", "Plate")
    assert "- extrude(sk1, amount=2)" in ag.model.code
    await ag.extrude_sketch("nope", 1)
    assert "no sketch" in ag.rec.texts("error")[-1]
    await ag.extrude_sketch("sk1", 1, "cut")           # cut without a body
    assert "op must be" in ag.rec.texts("error")[-1]
    await ag.delete_sketch("sk1")                     # still referenced by the extrusions → refused, model intact
    assert "still used" in ag.rec.texts("error")[-1] and ag.sketch_defs()[0]["name"] == "sk1"
    await ag.build(script_edit.set_sketch(PLATE, "sk1", PLANE, ITEMS), source="user")
    await ag.delete_sketch("sk1")
    assert ag.sketch_defs() == [] and "# sketch:sk1" not in ag.model.code
    await ag.delete_sketch("nope")
    assert ag.rec.texts("error")[-1]
    await ag.set_sketch("sk2", PLANE, [{"type": "circle", "cx": 0, "cy": 0, "r": 3, "mode": "add"}], by_agent=True)
    assert ag.model.sketches[0]["name"] == "sk2"


# ------------------------------------------------------------------ library / STEP import
async def test_library_roundtrip_and_step_import(ag):
    await build(ag, TWO)
    meta = await ag.add_to_library("Pair", None, "two shapes", ["demo"], None)
    assert meta["kind"] == "script"
    meta_b = await ag.add_to_library("Cyl", "B", "one body", [], None)          # screenshot fails → no thumb, still saved
    assert meta_b["kind"] == "step"
    assert {p["name"] for p in ag.parts.list()} == {"Pair", "Cyl"}
    await ag.insert_library_part("Cyl", None, "CylCopy")
    assert "CylCopy" in [b.name for b in ag.model.bodies] and 'from_library("Cyl")' in ag.model.code
    with pytest.raises(ck.CadError):
        await ag.insert_library_part("missing")
    # STEP import in all three modes
    step = ck.export(ag.model, ag.workspace / "exports", "x", ["step"], body="A")[0]
    data = step.read_bytes()
    r = await ag.import_step("cube part.step", data, "add")
    assert r["file"] == "cube part.step" and "cube part" in [b.name for b in ag.model.bodies]
    r = await ag.import_step("lib.step", data, "library", "from step")
    assert r["library"] == "lib" and ag.parts.get("lib")["kind"] == "step"
    r = await ag.import_step("fresh.step", data, "new")
    assert ag.design_name is None and len(ag.model.bodies) == 1 and 'import_step("fresh.step")' in ag.model.code


# ------------------------------------------------------------------ measure / drawings / CAM plumbing
async def test_measure_and_drawings(ag):
    await build(ag, TWO)
    a = ag.model.body_by_name("A"); b = ag.model.body_by_name("B")
    res = ag.measure(bodies=[a.id, b.id])
    assert res["clearance"]["min_distance"] == pytest.approx(25, abs=0.01) and res["clearance"]["status"] == "clear" and len(res["bodies"]) == 2
    files = await ag.make_drawings("Aluminium", None, "A4", "test")
    assert len(files) >= 2 and ag.rec.last("drawings")["files"]
    ag.model = None
    assert ag.measure() == {}
    with pytest.raises(ck.CadError):
        await ag.make_drawings()


async def test_cam_code_and_context(ag):
    await build(ag)
    ctx = ag.cam_context()
    assert "Generic 3018" in ctx and "Plate" in ctx and "hole" in ctx.lower()
    code = ("part = bodies['Plate']\nstock = Stock.from_model(part, margin=2, top=1)\n"
            "setup = Setup(machines['Generic 3018'], stock)\nprogram = Program(setup, name='t')\n"
            "program.add(face(setup, tools[1], z_top=stock.top, z_bottom=19))\n")
    prog = await ag.set_cam_code(code, rebuild=True, source="user")
    assert prog is not None and len(prog.ops) == 1 and ag.rec.last("cam")["program"]["ops"]
    assert (ag.workspace / "cam.py").read_text() == code
    assert "Current program" in ag.cam_context()
    with pytest.raises(cam.CamError):
        await ag.set_cam_code("x = 1\n", rebuild=True)              # no `program`
    with pytest.raises(cam.CamError):
        await ag.set_cam_code("program = Program(Setup(machines['Generic 3018'], Stock.from_model(part)))\n", rebuild=True)  # no ops
    with pytest.raises(cam.CamError):
        await ag.set_cam_code("1/0\n", rebuild=True)
    await ag.set_cam_code("", rebuild=False)
    assert ag.program is None and ag.rec.last("cam")["program"] is None


# ------------------------------------------------------------------ chat plumbing that needs no session
async def test_expand_selection_and_images(ag):
    await build(ag, TWO)
    a = ag.model.body_by_name("A")
    top = next(f for f in ag.model.faces if f.body == a.id and f.normal and f.normal[2] > 0.99)
    txt = ag._expand_selection({"faces": [top.id], "bodies": [a.id], "sketches": []})
    assert "[Selected geometry]" in txt and f"face#{top.id}" in txt and "body#" in txt
    assert ag._expand_selection([top.id]).count("face#") == 1
    assert ag._expand_selection(None) == ""
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 20).decode()
    saved = ag._save_images([{"name": "ref one.png", "data": "data:image/png;base64," + png},
                             {"name": "x", "data": "!!notbase64!!", "mime": "image/jpeg"}])
    assert len(saved) == 1 and saved[0][1] == "image/png" and saved[0][0].suffix == ".png" and saved[0][0].exists()


async def test_settings_roundtrip_and_status(ag):
    st = ag.save_settings({"model": "claude-sonnet-5", "effort": "high", "mcp_servers": {
        "fs": {"type": "stdio", "command": "npx", "args": ["x"], "enabled": True},
        "off": {"type": "http", "url": "http://x", "enabled": False},
        "web": {"type": "sse", "url": "http://y", "headers": {"a": "b"}, "enabled": True}}})
    assert st["model"] == "claude-sonnet-5" and json.loads((ag.workspace / "settings.json").read_text())["effort"] == "high"
    cfg = ag._mcp_configs()
    assert set(cfg) == {"cad", "fs", "web"} and cfg["web"]["headers"] == {"a": "b"}
    status = await ag.agent_status()
    assert status["connected"] is False and status["model"] == "claude-sonnet-5" and status["mcp"] == []
    ag2 = CadAgent(ag.workspace, ag.rec.emit, ag.screenshot_fn)
    assert ag2.settings["effort"] == "high"


async def test_api_key_setting_is_masked_and_used(ag, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ag.save_settings({"api_key": "sk-ant-secret"})
    pub = ag.public_settings()
    assert "api_key" not in pub and pub["api_key_set"] is True
    assert oct((ag.workspace / "settings.json").stat().st_mode & 0o777) == "0o600"
    assert ag.auth_status() == {"logged_in": True, "auth_method": "api_key", "error": None}
    ag.save_settings({"api_key": ""})
    assert ag.public_settings()["api_key_set"] is False
