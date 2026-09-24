"""Manual (no-agent) operations over the WebSocket and the remaining HTTP endpoints."""
from __future__ import annotations

import base64
import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

from tests.test_server import wait_for


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    ws = tmp_path_factory.mktemp("ws")
    os.environ["AGENTICCAD_WORKSPACE"] = str(ws)
    os.environ["AGENTICCAD_NO_AGENT"] = "1"
    import cad_kernel as ck
    (ws / "model.py").write_text(ck.DEFAULT_CODE)        # these tests exercise the two-body demo, not the empty first run
    for m in ("server", "agent"):
        sys.modules.pop(m, None)
    import server
    with TestClient(server.app) as c:
        c.server = server
        yield c


def drain(ws):
    """Read the connection preamble up to the status message; return the initial model."""
    first = ws.receive_json()
    while ws.receive_json()["type"] != "status":
        pass
    return first


def reset(client, code):
    with client.websocket_connect("/ws") as ws:
        drain(ws)
        ws.send_json({"type": "run_code", "code": code})
        return wait_for(ws, "model")["mesh"]


PLATE = ('plate_l, plate_w, plate_t = 60, 40, 8\nwith BuildPart() as plate:\n    Box(plate_l, plate_w, plate_t)\n'
         '    with Locations((0, 0, plate_t / 2)):\n        Cylinder(10, 15, align=(Align.CENTER, Align.CENTER, Align.MIN))\n'
         '    Hole(4)\nresult = {"Plate": plate.part}\n')


def test_ws_rename_delete_and_primitive(client):
    reset(client, 'result = {"A": Box(20, 20, 10), "B": Pos(40, 0, 0) * Cylinder(5, 10)}\n')
    with client.websocket_connect("/ws") as ws:
        drain(ws)
        ws.send_json({"type": "rename_body", "path": "A", "name": "Base"})
        m = wait_for(ws, "model")
        assert [b["name"] for b in m["mesh"]["bodies"]] == ["Base", "B"] and m["source"] == "edit"
        ws.send_json({"type": "add_primitive", "kind": "box", "dims": {"x": 5, "y": 5, "z": 5}, "at": [0, 0, 20], "name": "Cube"})
        m = wait_for(ws, "model")
        assert "Cube" in [b["name"] for b in m["mesh"]["bodies"]]
        ws.send_json({"type": "delete_body", "path": "Cube"})
        m = wait_for(ws, "model")
        assert "Cube" not in [b["name"] for b in m["mesh"]["bodies"]]
        ws.send_json({"type": "transform_body", "path": "B", "move": [0, 0, 5], "rotate": [0, 0, 0]})
        m = wait_for(ws, "model")
        b = next(x for x in m["mesh"]["bodies"] if x["name"] == "B")
        assert abs(b["bboxMin"][2]) < 1e-6
        ws.send_json({"type": "get_model"})
        assert wait_for(ws, "model")["mesh"]["bodies"]


def test_ws_ribbon_ops(client):
    mesh = reset(client, PLATE)
    v_faces = len(mesh["faces"])
    with client.websocket_connect("/ws") as ws:
        drain(ws)
        ws.send_json({"type": "op", "kind": "extrude_face", "body": "Plate", "point": [7, 0, 19], "normal": [0, 0, 1], "amount": 5})
        m = wait_for(ws, "model")
        assert abs(m["mesh"]["bboxMax"][2] - 24) < 1e-6
        ws.send_json({"type": "op", "kind": "shell", "body": "Plate", "faces": [[0, 0, -4]], "thickness": 1.5})
        m = wait_for(ws, "model")
        assert "offset(" in m["code"]
        ws.send_json({"type": "undo"})
        assert wait_for(ws, "model")["source"] == "revert"
        ws.send_json({"type": "op", "kind": "hole", "body": "Plate", "at": [20, 10, 4], "normal": [0, 0, 1], "diameter": 5, "through": True})
        m = wait_for(ws, "model")
        assert len(m["mesh"]["faces"]) > v_faces
        ws.send_json({"type": "op", "kind": "hole", "body": "Plate", "at": [-20, 10, 4], "normal": [0, 0, 1], "depth": 5, "thread": "M3"})
        m = wait_for(ws, "model")
        assert any(t["label"].startswith("M3") for t in m["mesh"].get("threads", [])) or "tap(" in m["code"]
        ws.send_json({"type": "op", "kind": "fillet", "body": "Plate", "points": [], "radius": 1.5, "faces": [[7, 0, 24]]})
        m = wait_for(ws, "model")
        assert ".fillet(1.5" in m["code"]
        ws.send_json({"type": "op", "kind": "chamfer", "body": "Plate", "points": [[30, 20, 0]], "radius": 1})
        m = wait_for(ws, "model")
        assert ".chamfer(1, None" in m["code"]
        ws.send_json({"type": "op", "kind": "primitive", "shape": "cylinder", "dims": {"d": 4, "h": 5}, "at": [22, -12, 4], "normal": [0, 0, 1], "body": "Plate", "mode": "join"})
        m = wait_for(ws, "model")
        assert "Cylinder(2, 5" in m["code"]
        ws.send_json({"type": "op", "kind": "extrude_face", "body": "Plate"})          # missing keys → error, no crash
        e = ws.receive_json()
        while e["type"] != "error":
            e = ws.receive_json()
        assert "extrude_face failed" in e["text"]
        ws.send_json({"type": "undo"})
        assert wait_for(ws, "model")["source"] == "revert"


def test_ws_sketch_flow_and_quality(client):
    reset(client, PLATE)
    plane = {"origin": [0, 0, 4], "x_dir": [1, 0, 0], "z_dir": [0, 0, 1], "label": "top"}
    items = [{"type": "rect", "cx": 18, "cy": 0, "w": 10, "h": 10, "angle": 0, "mode": "add"},
             {"type": "slot", "x1": -24, "y1": 0, "x2": -12, "y2": 0, "w": 4, "mode": "add"},
             {"type": "polygon", "pts": [[0, 12], [3, 15], [0, 18], [-3, 15]], "mode": "add"}]
    with client.websocket_connect("/ws") as ws:
        drain(ws)
        ws.send_json({"type": "set_sketch", "name": "sk1", "plane": plane, "items": items})
        m = wait_for(ws, "model")
        assert [s["name"] for s in m["mesh"]["sketches"]] == ["sk1"]
        assert client.get("/api/sketches").json()["sketches"][0]["name"] == "sk1"
        ws.send_json({"type": "extrude_sketch", "name": "sk1", "amount": 4, "op": "join", "body": "Plate"})
        m = wait_for(ws, "model")
        assert "extrude(sk1, amount=4)" in m["code"]
        ws.send_json({"type": "set_sketch", "name": "bad name", "plane": plane, "items": items})
        e = ws.receive_json()
        while e["type"] != "error":
            e = ws.receive_json()
        assert "sketch failed" in e["text"]
        ws.send_json({"type": "set_sketch", "name": "sk2", "plane": plane, "items": [{"type": "slot", "cx": 1}]})
        e = ws.receive_json()
        while e["type"] != "error":
            e = ws.receive_json()
        assert "missing field" in e["text"]
        ws.send_json({"type": "delete_sketch", "name": "sk1"})          # referenced by the extrusion → refused
        e = ws.receive_json()
        while e["type"] != "error":
            e = ws.receive_json()
        assert "still used" in e["text"]
        ws.send_json({"type": "undo"})
        wait_for(ws, "model")
        ws.send_json({"type": "delete_sketch", "name": "sk1"})
        m = wait_for(ws, "model")
        assert m["mesh"]["sketches"] == []
        ws.send_json({"type": "rename_body", "path": "Nope", "name": "X"})   # bad request keeps the socket alive
        e = ws.receive_json()
        while e["type"] != "error":
            e = ws.receive_json()
        ws.send_json({"type": "get_model"})
        assert wait_for(ws, "model")["mesh"]["bodies"]
        ws.send_json({"type": "set_quality", "quality": "fine"})
        m = wait_for(ws, "model")
        assert m["source"] == "requality"
        ws.send_json({"type": "set_quality", "quality": "draft"})
        wait_for(ws, "model")


def test_ws_cam_and_gcode_endpoints(client):
    reset(client, PLATE)
    code = ("part = bodies['Plate']\nstock = Stock.from_model(part, margin=2, top=1)\nsetup = Setup(machines['Generic 3018'], stock)\n"
            "program = Program(setup, name='t')\nprogram.add(face(setup, tools[1], z_top=stock.top, z_bottom=19))\n"
            "program.add(contour(setup, tools[1], section(part, 0.0), z_top=4, z_bottom=-1, tabs=2))\n")
    assert client.get("/api/gcode").status_code == 400
    assert client.get("/api/gcode/preview").text == ""
    with client.websocket_connect("/ws") as ws:
        drain(ws)
        ws.send_json({"type": "run_cam", "code": code})
        m = wait_for(ws, "cam")
        assert m["program"] and len(m["program"]["ops"]) == 2 and m["gcode_lines"] > 10
        ws.send_json({"type": "run_cam", "code": "program = None\n"})
        e = ws.receive_json()
        while e["type"] != "cam_error":
            e = ws.receive_json()
        assert "program" in e["text"]
    g = client.get("/api/gcode")
    assert g.status_code == 200 and "G21" in g.text and "attachment" in g.headers["content-disposition"]
    p = client.get("/api/gcode/preview?lines=5").text
    assert p.count("\n") <= 6 and "more lines" in p
    f = client.get("/api/cam/feeds?tool=1&material=aluminium").json()
    assert f["rpm"] > 0 and f["feed"] > 0
    assert client.get("/api/cam/feeds?tool=99&material=aluminium").status_code == 400
    mats = client.get("/api/cam/materials").json()
    assert "aluminium" in mats["materials"] or any("alumin" in m for m in mats["materials"])


def test_cam_library_edit_endpoints(client):
    r = client.post("/api/cam/machine", json={"machine": {"name": "Bench Mill", "travel": {"x": 200, "y": 150, "z": 80}}})
    assert r.status_code == 200
    assert any(m["name"] == "Bench Mill" for m in client.get("/api/cam/library").json()["machines"])
    assert client.delete("/api/cam/machine/Bench%20Mill").json()["deleted"]
    assert client.delete("/api/cam/machine/Nope").status_code == 404
    r = client.post("/api/cam/tool", json={"tool": {"number": 42, "name": "probe", "type": "flat", "diameter": 2}})
    assert r.status_code == 200
    assert client.delete("/api/cam/tool/42").json()["deleted"]
    assert client.delete("/api/cam/tool/4242").status_code == 404
    assert client.post("/api/cam/tool", json={"tool": {"name": "no number"}}).status_code == 400


def test_import_step_and_design_import_endpoints(client):
    reset(client, 'result = {"A": Box(20, 20, 10)}\n')
    step = client.get("/api/export/step?body=A").content
    data = "data:model/step;base64," + base64.b64encode(step).decode()
    r = client.post("/api/import/step", json={"name": "part one.step", "data": data, "mode": "add"}).json()
    assert r["file"] == "part one.step"
    with client.websocket_connect("/ws") as ws:
        assert len(ws.receive_json()["mesh"]["bodies"]) == 2
    r = client.post("/api/import/step", json={"name": "lib.step", "data": base64.b64encode(step).decode(), "mode": "library", "description": "x"}).json()
    assert r["library"] == "lib"
    assert client.post("/api/import/step", json={"name": "bad.step", "data": "AAAA", "mode": "add"}).status_code == 400
    r = client.post("/api/design/import", json={"name": "imp.py", "code": "result = Sphere(4)\n"}).json()
    assert r["name"] == "imp"
    assert client.post("/api/design/import", json={"name": "bad.py", "code": "result = Box("}).status_code == 400
    thumb = client.get("/api/library/thumb/lib")
    assert thumb.status_code in (200, 404)
    assert client.delete("/api/library/lib").json()["deleted"]
    assert client.delete("/api/library/lib").status_code == 404


def test_settings_status_and_version(client):
    s = client.get("/api/settings").json()
    assert "settings" in s and "models" in s
    r = client.post("/api/settings", json={"settings": {"effort": "low", "max_turns": 12}, "restart": False}).json()
    assert r["settings"]["effort"] == "low" and r["settings"]["max_turns"] == 12
    st = client.get("/api/agent/status").json()
    assert st["connected"] is False and st["effort"] == "low"
    v = client.get("/api/version").json()
    assert v["version"] and "Changelog" in v["changelog"]
    c = client.get("/api/agent/cli").json()
    assert "found" in c


def test_measure_endpoint_variants(client):
    mesh = reset(client, 'result = {"A": Box(20, 20, 10), "B": Pos(40, 0, 0) * Cylinder(5, 10)}\n')
    a = next(b for b in mesh["bodies"] if b["name"] == "A"); b = next(b for b in mesh["bodies"] if b["name"] == "B")
    r = client.post("/api/measure", json={"bodies": [a["id"], b["id"]]}).json()
    assert abs(r["clearance"]["min_distance"] - 25) < 0.01 and len(r["bodies"]) == 2
    r = client.post("/api/measure", json={"points": [[0, 0, 0], [0, 0, 7]]}).json()
    assert abs(r["distance"] - 7) < 1e-6
    circ = next(e for e in b["edgeInfo"] if e["kind"] == "CIRCLE")
    r = client.post("/api/measure", json={"edges": [circ["id"]]}).json()
    assert abs(r["items"][0]["radius"] - 5) < 1e-6
    r = client.post("/api/measure", json={"entities": [{"type": "point", "p": [0, 0, 0]}, {"type": "edge", "id": circ["id"]}]}).json()
    assert "center_distance" in r
    assert client.post("/api/measure", json={"faces": [99999]}).status_code == 400


def test_settings_never_return_the_api_key(client):
    r = client.post("/api/settings", json={"settings": {"api_key": "sk-ant-abc"}, "restart": False}).json()
    assert "api_key" not in r["settings"] and r["settings"]["api_key_set"] is True
    assert "api_key" not in client.get("/api/settings").json()["settings"]
    assert client.get("/api/agent/cli").json()["api_key_set"] is True
    r = client.post("/api/settings", json={"settings": {"api_key": "__keep__", "effort": "medium"}, "restart": False}).json()
    assert r["settings"]["api_key_set"] is True and r["settings"]["effort"] == "medium"
    r = client.post("/api/settings", json={"settings": {"api_key": ""}, "restart": False}).json()
    assert r["settings"]["api_key_set"] is False
