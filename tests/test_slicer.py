"""3D-printing hand-off: slicer detection, Orca profile flattening, G-code → layers, and the gated agent tools.
Unit tests use a fake profile tree; the integration tests run only when a real slicer is installed."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import slicer
from agent import CadAgent
from tests.test_agent_ops import PLATE, TWO, Recorder

GCODE = """; HEADER_BLOCK_START
; total layer number: 2
; max_z_height: 0.40
; HEADER_BLOCK_END
M83
G1 X0 Y0 Z0.2 F3000
;TYPE:Skirt
G1 X10 Y0 E0.5
;LAYER_CHANGE
;Z:0.2
;HEIGHT:0.2
;TYPE:Outer wall
G1 X10 Y10 E0.5
G1 X0 Y10 E0.5
G1 X0 Y0 F9000          ; travel (no E)
;TYPE:Sparse infill
G1 X5 Y5 E0.3
;LAYER_CHANGE
;Z:0.4
;HEIGHT:0.2
G1 Z0.4
;TYPE:Outer wall
G2 X15 Y5 I5 J0 E1.0    ; clockwise arc, radius 5
M82
G1 X15 Y0 E100
G1 X20 Y0 E100          ; absolute E unchanged: a travel, not an extrusion
; filament used [mm] = 123.4
; filament used [cm3] = 0.3
; filament used [g] = 0.37
; estimated printing time (normal mode) = 1h 2m 3s
"""


def test_parse_gcode_layers_features_and_arcs():
    out = slicer.parse_gcode(GCODE, translate=(100.0, 50.0, 0.0))
    assert out["types"][:3] == ["Skirt", "Outer wall", "Sparse infill"]
    assert len(out["layers"]) == 2
    assert out["layers"][0]["z"] == 0.2 and out["layers"][1]["h"] == 0.2
    assert out["layers"][0]["start"] == 0 and out["layers"][1]["start"] == 4   # skirt joins layer 1; layer 2 starts after 1 skirt + 2 wall + 1 infill segments
    # first wall segment is (10,0)->(10,10) in bed coords -> (-90,-50) -> (-90,-40) in model coords
    assert out["pos"][6:12] == [-90.0, -50.0, 0.2, -90.0, -40.0, 0.2]
    # the arc was chopped into chords, all on the outer-wall feature, ending at the arc end point
    wall = out["types"].index("Outer wall")
    ends = [tuple(out["pos"][i * 6 + 3:i * 6 + 6]) for i in range(out["segments"]) if out["feat"][i] == wall and out["pos"][i * 6 + 2] == 0.4]
    assert ends.index((-85.0, -45.0, 0.4)) >= 8            # ≥ 9 chords for a 5 mm-radius half circle, ending at the arc end point
    assert ends[-1] == (-85.0, -50.0, 0.4)                  # then the absolute-E extrusion to (15, 0)
    # absolute-E travel (E unchanged) must not draw
    assert out["segments"] == len(out["feat"]) == len(out["pos"]) // 6
    assert not any(out["pos"][i * 6] == -85.0 and out["pos"][i * 6 + 3] == -80.0 for i in range(out["segments"]))
    assert out["colors"][wall] == slicer.FEATURE_COLORS["Outer wall"]
    walls = slicer.parse_gcode(GCODE, walls_only=True)
    assert "Skirt" not in [walls["types"][f] for f in walls["feat"]] and "Sparse infill" not in [walls["types"][f] for f in walls["feat"]]


def test_parse_time_and_result_reading(tmp_path):
    assert slicer._parse_time("1h 2m 3s") == 3723 and slicer._parse_time("45m 10s") == 2710 and slicer._parse_time("2d 1h") == 176400
    g = tmp_path / "plate_1.gcode"; g.write_text(GCODE)
    import zipfile
    mf = tmp_path / "out.3mf"
    with zipfile.ZipFile(mf, "w") as z:
        z.writestr("3D/3dmodel.model", '<model><build><item objectid="1" transform="1 0 0 0 1 0 0 0 1 110 120 5"/></build></model>')
        z.writestr("Metadata/slice_info.config", '<config><plate><metadata key="prediction" value="3723"/><metadata key="weight" value="0.37"/>'
                                                 '<warning msg="bed_temperature_too_high_than_filament" level="1"/></plate></config>')
    res = slicer.SliceResult(str(g), str(mf), "M", "P", "F", {})
    slicer._read_results(res)
    assert (res.layers, res.height_mm, res.filament_mm, res.filament_g, res.time_s) == (2, 0.4, 123.4, 0.37, 3723.0)
    assert res.translate == (110.0, 120.0, 5.0)
    assert res.warnings == ["bed temperature too high than filament"]
    assert "2 layers" in res.summary() and "1h 2m" in res.summary()


@pytest.fixture
def fake_tree(tmp_path):
    """A miniature Orca profile tree: vendor with an inherits chain, plus a user preset, plus a generic filament."""
    prof = tmp_path / "profiles"; user = tmp_path / "user"
    v = prof / "Acme"
    for kind in ("machine", "process", "filament"):
        (v / kind).mkdir(parents=True); (user / kind).mkdir(parents=True); (prof / "OrcaFilamentLibrary" / kind).mkdir(parents=True)
    j = lambda p, d: p.write_text(json.dumps(d))
    j(v / "machine" / "fdm_acme_common.json", {"type": "machine", "instantiation": "false", "printable_height": "250", "retraction_length": ["1"]})
    j(v / "machine" / "Acme One 0.4 nozzle.json", {"type": "machine", "inherits": "fdm_acme_common", "instantiation": "true", "printer_model": "Acme One",
                                                    "nozzle_diameter": ["0.4"], "printable_area": ["0x0", "220x0", "220x220", "0x220"],
                                                    "default_print_profile": "0.20mm Standard @Acme", "default_filament_profile": ["Acme PLA"]})
    j(v / "process" / "fdm_process_common.json", {"type": "process", "instantiation": "false", "layer_height": "0.2", "wall_loops": "2", "sparse_infill_density": "15%"})
    j(v / "process" / "0.20mm Standard @Acme.json", {"type": "process", "inherits": "fdm_process_common", "instantiation": "true", "compatible_printers": ["Acme One 0.4 nozzle"]})
    j(v / "process" / "0.20mm Other @Other.json", {"type": "process", "instantiation": "true", "compatible_printers": ["Other 0.4 nozzle"]})
    j(v / "filament" / "Acme PLA.json", {"type": "filament", "instantiation": "true", "filament_type": ["PLA"], "compatible_printers": ["Acme One 0.4 nozzle"]})
    j(prof / "OrcaFilamentLibrary" / "filament" / "Generic PETG.json", {"type": "filament", "instantiation": "true", "filament_type": ["PETG"], "compatible_printers": []})
    j(user / "process" / "My draft.json", {"type": "process", "inherits": "0.20mm Standard @Acme", "instantiation": "true", "layer_height": "0.3", "compatible_printers": ["Acme One 0.4 nozzle"]})
    exe = tmp_path / "fake-slicer"; exe.write_text("#!/bin/sh\nexit 3\n"); exe.chmod(0o755)
    conf = tmp_path / "OrcaSlicer.conf"; j(conf, {"presets": {"machine": "Acme One 0.4 nozzle"}})
    slicer._cache.clear()
    return slicer.SlicerInfo("OrcaSlicer", str(exe), str(prof), str(user), str(conf), "9.9")


def test_flatten_index_and_compat(fake_tree):
    info = fake_tree
    m = slicer.flatten(info, "machine", "Acme One 0.4 nozzle")
    assert m["printable_height"] == "250" and m["printer_model"] == "Acme One" and "inherits" not in m
    p = slicer.flatten(info, "process", "My draft")      # user preset -> vendor -> common
    assert (p["layer_height"], p["wall_loops"], p["sparse_infill_density"]) == ("0.3", "2", "15%")
    with pytest.raises(slicer.SlicerError):
        slicer.flatten(info, "process", "nope")
    ms = slicer.machines(info)
    assert [x["name"] for x in ms] == ["Acme One 0.4 nozzle"] and ms[0]["nozzle"] == "0.4" and ms[0]["default_process"] == "0.20mm Standard @Acme"
    assert slicer.default_machine(info) == "Acme One 0.4 nozzle"
    pf = slicer.profiles_for(info, "Acme One 0.4 nozzle")
    assert [x["name"] for x in pf["processes"]] == ["0.20mm Standard @Acme", "My draft"]
    assert sorted(x["name"] for x in pf["filaments"]) == ["Acme PLA", "Generic PETG"]      # generic (empty list) is compatible with all


def test_overrides_written_and_failure_reported(fake_tree, tmp_path):
    info = fake_tree
    stl = tmp_path / "m.stl"; stl.write_bytes(b"solid x\nendsolid x\n")
    out = tmp_path / "out"
    with pytest.raises(slicer.SlicerError, match="slicer failed"):
        slicer.slice_file(info, stl, "Acme One 0.4 nozzle", None, None, {"layer_height": 0.28, "sparse_infill_density": 20, "enable_support": True, "brim_type": "no_brim"}, out)
    proc = json.loads((out / "process.json").read_text())
    assert (proc["layer_height"], proc["initial_layer_print_height"], proc["sparse_infill_density"], proc["enable_support"], proc["brim_type"]) == ("0.28", "0.28", "20%", "1", "no_brim")
    assert proc["wall_loops"] == "2" and proc["name"] == "0.20mm Standard @Acme"       # inherited value kept, name set for the CLI
    assert json.loads((out / "filament.json").read_text())["name"] == "Acme PLA"
    with pytest.raises(slicer.SlicerError, match="unsupported override"):
        slicer.slice_file(info, stl, "Acme One 0.4 nozzle", None, None, {"nozzle_temperature": 250}, out)
    with pytest.raises(slicer.SlicerError, match="no filament profile named"):
        slicer.slice_file(info, stl, "Acme One 0.4 nozzle", None, "Unobtainium", {}, out)


def test_find_slicer_env_override_and_absence(tmp_path, monkeypatch):
    slicer._cache.clear()
    monkeypatch.setenv("AGENTICCAD_SLICER", str(tmp_path / "missing"))
    monkeypatch.setattr(slicer, "_candidates", lambda: [])
    assert slicer.find_slicer(refresh=True) is None
    exe = tmp_path / "bin" / "orca"; exe.parent.mkdir(); exe.write_text("#!/bin/sh\necho 'OrcaSlicer-1.2.3 help'\n"); exe.chmod(0o755)
    prof = tmp_path / "prof"; prof.mkdir()
    monkeypatch.setenv("AGENTICCAD_SLICER", f"{exe}::{prof}")
    info = slicer.find_slicer(refresh=True)
    assert info and info.exe == str(exe) and info.profiles_dir == str(prof) and info.version == "1.2.3"
    slicer._cache.clear()


def _agent(tmp_path):
    ws = tmp_path / "ws"
    for sub in ("exports", "history", "designs", "imports", "images", "drawings"):
        (ws / sub).mkdir(parents=True)
    rec = Recorder()
    a = CadAgent(ws, rec.emit, None); a.rec = rec; a.quality = "draft"
    a._make_server()
    return a


async def test_tools_only_registered_when_slicer_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(slicer, "find_slicer", lambda refresh=False: None)
    a = _agent(tmp_path)
    assert "slice_for_printing" not in a.tool_handlers and "slicer_info" not in a.tool_handlers
    assert a.slicer_payload() == {"type": "slicer", "installed": False, "slicer": None, "default_machine": None, "result": None}
    with pytest.raises(slicer.SlicerError, match="no slicer installed"):
        await a.slice_model()


async def test_tools_with_fake_slicer(tmp_path, monkeypatch, fake_tree):
    monkeypatch.setattr(slicer, "find_slicer", lambda refresh=False: fake_tree)
    a = _agent(tmp_path)
    T = a.tool_handlers
    assert "slice_for_printing" in T and "slicer_info" in T
    r = await T["slicer_info"]({"machine": "Acme One 0.4 nozzle"})
    j = json.loads(r["content"][0]["text"])
    assert j["default_machine"] == "Acme One 0.4 nozzle" and j["printers"] == ["Acme One 0.4 nozzle"] and "My draft" in j["processes"] and "layer_height" in j["overrides"]
    r = await T["slice_for_printing"]({"layer_height": 0.28})           # empty design
    assert r.get("is_error") and "empty" in r["content"][0]["text"]
    await a.build(PLATE, source="user")
    r = await T["slice_for_printing"]({"layer_height": 0.28})           # fake exe exits 3
    assert r.get("is_error") and "slicer failed" in r["content"][0]["text"]
    assert (a.workspace / "slicing" / "model.stl").exists()
    assert a.slicer_payload()["installed"] and a.slicer_payload()["slicer"]["version"] == "9.9"


REAL = slicer.find_slicer(refresh=True)
needs_real = pytest.mark.skipif(REAL is None, reason="no OrcaSlicer/Bambu Studio installed")


@needs_real
async def test_real_slice_end_to_end(tmp_path):
    a = _agent(tmp_path)
    await a.build(TWO, source="user")
    machine = slicer.default_machine(REAL) or slicer.machines(REAL)[0]["name"]
    res = await a.slice_model(machine, None, None, {"layer_height": 0.28, "sparse_infill_density": 10, "brim_type": "no_brim"}, body=None)
    assert res.layers > 5 and res.height_mm > 5 and res.filament_mm > 0 and Path(res.gcode).exists() and Path(res.three_mf).exists()
    ev = [e for e in a.rec.events if e["type"] == "sliced"]
    assert ev and ev[-1]["layers"]["segments"] > 100 and "Outer wall" in ev[-1]["layers"]["types"]
    # layers land in MODEL coordinates (bed placement undone): box A spans x -10..10, cylinder B x 35..45
    L = ev[-1]["layers"]; xs = L["pos"][0::3]
    wall = L["types"].index("Outer wall")
    wall_x = [xs[2 * i] for i in range(L["segments"]) if L["feat"][i] == wall]
    assert -10.5 < min(wall_x) < -9 and 44 < max(wall_x) < 45.5
    r = await a.tool_handlers["slice_for_printing"]({"body": "A", "layer_height": 0.2})
    assert not r.get("is_error") and "layers" in r["content"][0]["text"]
    assert len(a.slicer_payload()["result"]["summary"]) > 10


@needs_real
def test_real_slicer_api(tmp_path, monkeypatch):
    import sys
    from fastapi.testclient import TestClient
    ws = tmp_path / "ws"; ws.mkdir()
    monkeypatch.setenv("AGENTICCAD_WORKSPACE", str(ws)); monkeypatch.setenv("AGENTICCAD_NO_AGENT", "1")
    import cad_kernel as ck
    # these tests exercise the two-body demo saved as a named design (an unsaved demo working copy is upgraded to a blank start)
    (ws / "designs").mkdir(exist_ok=True); (ws / "designs" / "demo.py").write_text(ck.DEFAULT_CODE)
    (ws / "model.py").write_text(ck.DEFAULT_CODE); (ws / "state.json").write_text('{"design": "demo"}')
    for m in ("server", "agent"):
        sys.modules.pop(m, None)
    import server
    with TestClient(server.app) as c:
        info = c.get("/api/slicer").json()
        assert info["installed"] and info["slicer"]["name"] in ("OrcaSlicer", "BambuStudio")
        ms = c.get("/api/slicer/machines").json()
        machine = ms["default"] or ms["machines"][0]["name"]
        pf = c.get("/api/slicer/profiles", params={"machine": machine}).json()
        assert pf["processes"] and pf["filaments"] and pf["default_process"]
        assert c.get("/api/slicer/gcode").status_code == 404
        r = c.post("/api/slicer/slice", json={"machine": machine, "overrides": {"layer_height": 0.3}}).json()
        assert r["layers"] > 3 and r["overrides"]["layer_height"] == "0.3"
        assert c.get("/api/slicer/gcode").status_code == 200 and c.get("/api/slicer/3mf").status_code == 200
        with c.websocket_connect("/ws") as w:                 # a fresh tab receives the last slice
            kinds = []
            for _ in range(12):
                m = w.receive_json(); kinds.append(m["type"])
                if m["type"] == "sliced":
                    assert m["restore"] and m["layers"]["segments"] > 0; break
            assert "sliced" in kinds
        assert c.post("/api/slicer/slice", json={"machine": "No Such Printer 0.4"}).status_code == 400
    for m in ("server", "agent"):
        sys.modules.pop(m, None)


async def test_slice_defaults_come_from_settings(tmp_path, monkeypatch, fake_tree):
    """Ribbon ▸ Slice passes nothing: printer, profiles and overrides come from Settings ▸ 3D printing; explicit
    arguments win, and saved quality/filament are ignored when a different printer is named."""
    monkeypatch.setattr(slicer, "find_slicer", lambda refresh=False: fake_tree)
    a = _agent(tmp_path)
    a.save_settings({"print": {"machine": "Acme One 0.4 nozzle", "process": "My draft", "filament": "Generic PETG", "layer_height": 0.3, "enable_support": True, "brim_type": "outer_only"}})
    await a.build(PLATE, source="user")
    with pytest.raises(slicer.SlicerError, match="slicer failed"):          # fake exe exits 3, but the inputs were written
        await a.slice_model()
    out = a.workspace / "slicing"
    proc = json.loads((out / "process.json").read_text()); fil = json.loads((out / "filament.json").read_text())
    assert proc["name"] == "My draft" and fil["name"] == "Generic PETG"
    assert (proc["layer_height"], proc["enable_support"], proc["brim_type"]) == ("0.3", "1", "outer_only")
    with pytest.raises(slicer.SlicerError):
        await a.slice_model(overrides={"layer_height": 0.12, "enable_support": False})
    proc = json.loads((out / "process.json").read_text())
    assert (proc["layer_height"], proc["enable_support"], proc["brim_type"]) == ("0.12", "0", "outer_only")
    a.save_settings({"print": {"machine": "Other One", "process": "My draft"}})
    with pytest.raises(slicer.SlicerError):
        await a.slice_model(machine="Acme One 0.4 nozzle")               # different printer: saved process not applied
    assert json.loads((out / "process.json").read_text())["name"] == "0.20mm Standard @Acme"


def test_relative_paths_are_resolved(fake_tree, tmp_path, monkeypatch):
    """The slicer CLI runs with cwd = the slicing folder, so a relative workspace path must be made absolute first
    (a relative AGENTICCAD_WORKSPACE made OrcaSlicer report 'No such file: …/model.stl')."""
    import subprocess as sp
    seen = {}
    real = sp.run
    def fake_run(cmd, cwd=None, **kw):
        seen["cmd"], seen["cwd"] = cmd, cwd
        return real(["true"], capture_output=True, text=True)
    monkeypatch.setattr(slicer.subprocess, "run", fake_run)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "ws").mkdir(); (tmp_path / "ws" / "m.stl").write_bytes(b"solid x\nendsolid x\n")
    with pytest.raises(slicer.SlicerError):
        slicer.slice_file(fake_tree, "ws/m.stl", "Acme One 0.4 nozzle", None, None, {}, "ws/out")
    assert Path(seen["cmd"][-1]).is_absolute() and Path(seen["cmd"][-1]).exists()
    assert Path(seen["cwd"]).is_absolute()
    settings = seen["cmd"][seen["cmd"].index("--load-settings") + 1].split(";")
    assert all(Path(x).is_absolute() for x in settings)
