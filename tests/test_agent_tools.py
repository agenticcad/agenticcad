"""The agent's MCP tools, called directly through CadAgent.tool_handlers (no Claude session).
These are the exact functions Claude calls; each returns {"content": [...], "is_error"?}."""
from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import pytest

import cad_kernel as ck
from agent import CadAgent
from tests.test_agent_ops import PLATE, TWO, Recorder, PLANE, ITEMS


def text(res) -> str:
    return "\n".join(c.get("text", "") for c in res["content"] if c.get("type") == "text")


def ok(res) -> bool:
    return not res.get("is_error")


@pytest.fixture
async def ag(tmp_path):
    ws = tmp_path / "ws"
    for sub in ("exports", "history", "designs", "imports", "images", "drawings"):
        (ws / sub).mkdir(parents=True)
    rec = Recorder()

    async def shot(spec):
        # a 2x2 white JPEG, base64 — what the browser would send back
        from PIL import Image
        buf = io.BytesIO(); Image.new("RGB", (2, 2), "white").save(buf, "JPEG")
        return base64.b64encode(buf.getvalue()).decode()
    a = CadAgent(ws, rec.emit, shot)
    a.rec = rec
    a.quality = "draft"
    a._make_server()
    await a.build(PLATE, source="user")
    return a


async def test_build_and_inspect(ag):
    T = ag.tool_handlers
    r = await T["build_model"]({"code": TWO})
    assert ok(r) and "2 bodies" in text(r)
    r = await T["build_model"]({"code": "result = Box("})
    assert not ok(r) and "BUILD FAILED" in text(r) and "SyntaxError" in text(r)
    r = await T["build_model"]({"code": "result = 42"})
    assert not ok(r)
    r = await T["inspect_model"]({})
    assert ok(r) and "face#" in text(r)
    r = await T["inspect_model"]({"kind": "cylinder"})
    assert "CYLINDER" in text(r) and "PLANE" not in text(r)
    r = await T["inspect_model"]({"body": "B", "limit": 3})
    assert ok(r) and text(r).count("face#") <= 3
    r = await T["inspect_model"]({"body": "Nope"})
    assert not ok(r) and "Bodies:" in text(r)
    r = await T["inspect_model"]({"near": [40, 0, 5], "limit": 2})
    assert ok(r) and "CYLINDER" in text(r) and text(r).count("face#") <= 2
    assert ok(await T["get_code"]({})) and text(await T["get_code"]({})) == TWO


async def test_screenshot_and_export(ag, tmp_path):
    T = ag.tool_handlers
    r = await T["screenshot"]({"view": "top", "highlight_faces": [1]})
    assert ok(r) and r["content"][0]["type"] == "image" and "view=top" in text(r)
    ag.screenshot_fn = None
    r = await T["screenshot"]({})
    assert not ok(r) and "screenshot unavailable" in text(r)
    r = await T["export_model"]({"name": "out", "formats": ["step", "stl"], "tolerance": 0.05})
    assert ok(r) and "exported:" in text(r), text(r)
    files = sorted(ag.rec.last("exported")["files"])
    assert files == ["out.step", "out.stl"], files
    assert all((ag.workspace / "exports" / f).exists() for f in files)
    r = await T["export_model"]({"body": "Nope"})
    assert not ok(r)
    r = await T["save_design"]({"name": "tool saved"})
    assert ok(r) and (ag.designs_dir / "tool saved.py").exists()


async def test_parameters_measure_mass(ag):
    T = ag.tool_handlers
    r = await T["get_parameters"]({})
    assert "plate_l = 60" in text(r)
    r = await T["set_parameters"]({"values": {"plate_l": 70}})
    assert ok(r) and ag.model.bbox_max[0] == pytest.approx(35)
    r = await T["set_parameters"]({"values": {"nope": 1}})
    assert not ok(r)
    top = next(f for f in ag.model.faces if f.kind == "PLANE" and f.normal and f.normal[2] > 0.99 and abs(f.center[2] - 4) < 1e-3)
    bot = next(f for f in ag.model.faces if f.kind == "PLANE" and f.normal and f.normal[2] < -0.99)
    r = await T["measure"]({"faces": [top.id, bot.id]})
    d = json.loads(text(r))
    assert d["relation"] == "parallel" and d["plane_gap"] == pytest.approx(8)
    r = await T["measure"]({"points": [[0, 0, 0], [3, 4, 0]]})
    assert json.loads(text(r))["distance"] == pytest.approx(5)
    r = await T["measure"]({"bodies": ["Nope"]})
    assert not ok(r)
    r = await T["mass_properties"]({"body": "Plate", "density": "steel"})
    d = json.loads(text(r)) if text(r).startswith("{") else None
    assert ok(r) and ("mass" in text(r))
    r = await T["mass_properties"]({"density": "unobtainium"})
    assert not ok(r)


async def test_drawings_library_sketch_tools(ag):
    T = ag.tool_handlers
    r = await T["make_drawings"]({"material": "Aluminium 6061"})
    assert ok(r) and ".svg" in text(r)
    r = await T["library"]({"action": "list"})
    assert ok(r)
    r = await T["library"]({"action": "save_design", "name": "plate lib", "description": "d", "tags": ["t"]})
    assert ok(r)
    r = await T["library"]({"action": "save_body", "name": "plate body", "body": "Plate"})
    assert ok(r)
    r = await T["library"]({"action": "search", "query": "plate"})
    assert "plate lib" in text(r) and "plate body" in text(r)
    r = await T["library"]({"action": "get", "name": "plate lib"})
    assert ok(r) and "plate_l" in text(r)
    r = await T["library"]({"action": "insert", "name": "plate body", "body_name": "Copy"})
    assert ok(r) and "Copy" in [b.name for b in ag.model.bodies]
    r = await T["library"]({"action": "insert", "name": "missing"})
    assert not ok(r)
    r = await T["library"]({"action": "delete", "name": "plate lib"})
    assert ok(r) and ag.parts.get("plate lib") is None
    r = await T["library"]({"action": "bogus"})
    assert not ok(r)
    # sketches
    r = await T["sketch"]({"action": "list"})
    assert ok(r)
    r = await T["sketch"]({"action": "set", "name": "sk1", "plane": PLANE, "items": ITEMS})
    assert ok(r) and "sk1" in text(r) and ag.model.sketches[0]["name"] == "sk1"
    r = await T["sketch"]({"action": "get", "name": "sk1"})
    assert json.loads(text(r))["items"] == ITEMS
    r = await T["sketch"]({"action": "get", "name": "nope"})
    assert "no editable sketch" in text(r)
    r = await T["sketch"]({"action": "set", "name": "bad name!", "plane": PLANE, "items": ITEMS})
    assert not ok(r)
    r = await T["sketch"]({"action": "delete", "name": "sk1"})
    assert ok(r) and ag.model.sketches == []


CAM_CODE = ("part = bodies['Plate']\nstock = Stock.from_model(part, margin=2, top=1)\n"
            "setup = Setup(machines['Generic 3018'], stock)\nt6 = apply_feeds(tools[1], 'aluminium', setup.machine)\n"
            "program = Program(setup, name='t')\nprogram.add(face(setup, t6, z_top=stock.top, z_bottom=19))\n"
            "program.add(drill(setup, tools[5], holes(part), peck='auto'))\n")


async def test_cam_tools(ag):
    T = ag.tool_handlers
    r = await T["cam_context"]({})
    assert "Generic 3018" in text(r) and "d=8.00" in text(r)
    r = await T["export_gcode"]({})
    assert not ok(r) and "no CAM program" in text(r)
    r = await T["get_cam_code"]({})
    assert "no CAM script" in text(r)
    r = await T["build_cam"]({"code": "program = 1"})
    assert not ok(r) and "CAM BUILD FAILED" in text(r)
    r = await T["build_cam"]({"code": CAM_CODE})
    assert ok(r) and "2 ops" in text(r)
    assert text(await T["get_cam_code"]({})) == CAM_CODE
    r = await T["export_gcode"]({"name": "prog one"})
    assert ok(r) and (ag.workspace / "exports" / "prog one.nc").exists() and "G1" in text(r)
    r = await T["feeds_speeds"]({"tool": "T1", "material": "aluminium"})
    assert ok(r) and "rpm" in text(r).lower()
    r = await T["feeds_speeds"]({"tool": "T99", "material": "aluminium"})
    assert not ok(r)
    r = await T["feeds_speeds"]({"tool": "6 mm flat endmill", "material": "kryptonite"})
    assert not ok(r)
    r = await T["feeds_speeds"]({"tool": "T2", "material": "mdf", "aggressiveness": 0.8, "apply": True})
    assert ok(r) and ag.rec.last("library")
    r = await T["save_tool"]({"tool": {"number": 9, "name": "tiny", "type": "flat", "diameter": 1.0, "flutes": 2}})
    assert ok(r) and 9 in ag.library.tool_map()
    r = await T["save_tool"]({"tool": {"name": "no number"}})
    assert not ok(r)
    r = await T["save_machine"]({"machine": {"name": "Test Mill", "travel": {"x": 100, "y": 100, "z": 50}}})
    assert ok(r) and "Test Mill" in ag.library.machines()
    r = await T["save_machine"]({"machine": {"travel": "bad"}})
    assert not ok(r)


async def test_tools_without_model(ag):
    T = ag.tool_handlers
    ag.model = None
    for name, args in [("inspect_model", {}), ("screenshot", {}), ("export_model", {}), ("measure", {}),
                       ("mass_properties", {}), ("make_drawings", {}), ("set_parameters", {"values": {}})]:
        r = await T[name](args)
        assert not ok(r) or "no model" in text(r).lower(), name
    assert text(await T["get_code"]({})) == ""
    r = await T["save_design"]({})
    assert not ok(r)
