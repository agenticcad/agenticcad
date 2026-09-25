"""API tests: the FastAPI app with the Claude session disabled (AGENTICCAD_NO_AGENT=1)."""
import json
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def wait_for(ws, kind, limit=40):
    """Receive until a message of `kind` arrives; fail fast on error messages instead of hanging."""
    for _ in range(limit):
        m = ws.receive_json()
        if m["type"] == kind:
            return m
        if m["type"] in ("error", "build_error", "cam_error"):
            raise AssertionError(f"got {m['type']}: {m.get('text', '')[:300]}")
    raise AssertionError(f"no {kind} message within {limit} messages")


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    ws = tmp_path_factory.mktemp("ws")
    os.environ["AGENTICCAD_WORKSPACE"] = str(ws)
    os.environ["AGENTICCAD_NO_AGENT"] = "1"
    import cad_kernel as ck
    # these tests exercise the two-body demo saved as a named design (an unsaved demo working copy is upgraded to a blank start)
    (ws / "designs").mkdir(exist_ok=True); (ws / "designs" / "demo.py").write_text(ck.DEFAULT_CODE)
    (ws / "model.py").write_text(ck.DEFAULT_CODE); (ws / "state.json").write_text('{"design": "demo"}')
    for m in ("server", "agent"):
        sys.modules.pop(m, None)
    import server
    with TestClient(server.app) as c:
        yield c


def test_index_and_initial_model(client):
    assert "AgenticCAD" in client.get("/").text
    with client.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "model" and len(first["mesh"]["bodies"]) == 2


def test_params_roundtrip_rebuilds(client):
    ps = client.get("/api/params").json()["params"]
    assert {p["name"] for p in ps} >= {"plate_l", "boss_h"}
    r = client.post("/api/params", json={"values": {"plate_l": 80}}).json()
    assert next(p["value"] for p in r["params"] if p["name"] == "plate_l") == 80
    with client.websocket_connect("/ws") as ws:
        m = ws.receive_json()
        assert abs(m["mesh"]["bboxMax"][0] - 40) < 1e-6


def test_measure_endpoint(client):
    with client.websocket_connect("/ws") as ws:
        mesh = ws.receive_json()["mesh"]
    top = next(f for f in mesh["faces"] if f["kind"] == "PLANE" and f["normal"] and f["normal"][2] > 0.99 and abs(f["center"][2] - 4) < 1e-6)
    bot = next(f for f in mesh["faces"] if f["kind"] == "PLANE" and f["normal"] and f["normal"][2] < -0.99)
    r = client.post("/api/measure", json={"faces": [top["id"], bot["id"]]}).json()
    assert r["relation"] == "parallel" and abs(r["plane_gap"] - 8) < 1e-6


def test_design_save_open_new(client):
    assert client.post("/api/design/save", json={"name": "t1"}).json()["name"] == "t1"
    d = client.get("/api/designs").json()
    assert d["current"] == "t1" and any(x["name"] == "t1" for x in d["designs"])
    assert client.post("/api/design/new").json()["ok"]
    assert client.get("/api/designs").json()["current"] is None
    assert client.post("/api/design/open", json={"name": "t1"}).json()["name"] == "t1"
    assert client.post("/api/design/open", json={"name": "nope"}).status_code == 400
    assert "result" in client.get("/api/design/download").text


def test_exports(client):
    step = client.get("/api/export/step")
    assert step.status_code == 200 and b"MANIFOLD_SOLID_BREP" in step.content
    fine = len(client.get("/api/export/stl?tol=0.005&ang=0.03").content)
    coarse = len(client.get("/api/export/stl?tol=0.2&ang=0.5").content)
    assert fine > 3 * coarse
    assert client.get("/api/export/step?body=Nope").status_code == 400


def test_run_code_over_websocket(client):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()                                   # model
        while ws.receive_json()["type"] != "status":
            pass
        ws.send_json({"type": "run_code", "code": "result = Box(5, 5, 5)"})
        msg = ws.receive_json()
        while msg["type"] != "model":
            msg = ws.receive_json()
        assert abs(msg["mesh"]["bboxMax"][0] - 2.5) < 1e-6 and msg["source"] == "user"
        ws.send_json({"type": "run_code", "code": "result = Box(5, 5,"})
        msg = ws.receive_json()
        while msg["type"] != "build_error":
            msg = ws.receive_json()
        assert "SyntaxError" in msg["text"]
        ws.send_json({"type": "undo"})
        msg = ws.receive_json()
        while msg["type"] != "model":
            msg = ws.receive_json()
        assert msg["source"] == "revert"


def test_library_and_drawings_api(client):
    client.post("/api/design/open", json={"name": "t1"})
    meta = client.post("/api/library/add", json={"name": "demo bracket", "body": None, "description": "d", "tags": []}).json()
    assert meta["kind"] == "script" and "plate_l" in meta["params"]
    parts = client.get("/api/library/parts").json()["parts"]
    assert any(p["name"] == "demo bracket" for p in parts)
    assert client.post("/api/library/insert", json={"name": "demo bracket", "params": {"plate_l": 30}}).json()["ok"]
    with client.websocket_connect("/ws") as ws:
        assert len(ws.receive_json()["mesh"]["bodies"]) == 4        # Bracket, Pin + 2 inserted (split)
    r = client.post("/api/drawings", json={"material": "Aluminium", "density": 2.7, "sheet": "A4"}).json()
    assert len(r["files"]) >= 2
    svg = client.get(f"/api/drawings/{r['design']}/{r['files'][0]['svg']}")
    assert svg.status_code == 200 and b"HOLE TABLE" in svg.content
    assert client.delete("/api/library/demo-bracket").json()["deleted"]


def test_cam_library_api(client):
    lib = client.get("/api/cam/library").json()
    assert len(lib["tools"]) >= 5 and len(lib["machines"]) >= 2
    t = {"number": 99, "name": "test 2 mm", "type": "flat", "diameter": 2, "flutes": 2, "flute_length": 8, "rpm": 12000,
         "feed": 500, "plunge": 150, "stepdown": 0.5, "stepover": 0.4}
    assert client.post("/api/cam/tool", json={"tool": t}).json()["number"] == 99
    assert client.post("/api/cam/tool", json={"tool": {"name": "no number"}}).status_code == 400
    assert client.delete("/api/cam/tool/99").json()["deleted"]


def test_settings_and_status_api(client):
    s = client.get("/api/settings").json()
    assert "model" in s["settings"] and "claude-opus-5" in s["models"]
    r = client.post("/api/settings", json={"settings": {"model": "claude-sonnet-5", "effort": "high", "max_turns": 30,
                                                        "mcp_servers": {"fs": {"type": "stdio", "command": "npx", "args": ["-y", "x"], "enabled": True}}}}).json()
    assert r["settings"]["model"] == "claude-sonnet-5" and r["settings"]["mcp_servers"]["fs"]["type"] == "stdio"
    assert client.post("/api/settings", json={"settings": {"effort": "turbo"}}).status_code == 400
    assert client.post("/api/settings", json={"settings": {"mcp_servers": {"bad": {"type": "http"}}}}).status_code == 400
    st = client.get("/api/agent/status").json()
    assert st["connected"] is False and st["model"] == "claude-sonnet-5"
    client.post("/api/settings", json={"settings": {"model": "", "effort": "", "max_turns": 60, "mcp_servers": {}}})


def test_sketch_over_websocket(client):
    client.post("/api/design/new")                       # fresh script: earlier tests may leave library refs behind
    plane = {"origin": [0, 0, 4], "x_dir": [1, 0, 0], "z_dir": [0, 0, 1], "label": "t"}
    items = [{"type": "rect", "cx": 0, "cy": 0, "w": 10, "h": 6, "angle": 0, "mode": "add"}]
    with client.websocket_connect("/ws") as ws:
        wait_for(ws, "status")
        ws.send_json({"type": "set_sketch", "name": "sk_t", "plane": plane, "items": items})
        msg = wait_for(ws, "model")
        assert [s["name"] for s in msg["mesh"]["sketches"]] == ["sk_t"] and msg["source"] == "sketch"
        assert client.get("/api/sketches").json()["sketches"][0]["items"] == items
        ws.send_json({"type": "delete_sketch", "name": "sk_t"})
        assert wait_for(ws, "model")["mesh"]["sketches"] == []


def test_version_endpoint(client):
    from version import __version__
    v = client.get("/api/version").json()
    assert v["version"] == __version__ and f"## [{__version__}]" in v["changelog"]


def test_setup_page_and_cli_probe(client):
    r = client.get("/setup")
    assert r.status_code == 200 and "Claude Code" in r.text
    r = client.get("/api/agent/cli")
    j = r.json()
    assert set(j) >= {"found", "path", "connected", "searched", "workspace"}
    assert isinstance(j["searched"], list) and j["searched"]
