"""Machine model scripts: the built-ins run through the script API, a user's script in the workspace replaces the
built-in for that machine, validation errors are specific, and the agent/server round-trip (build, code, delete)."""
import json

import pytest

import cam_kernel as cam
import machine_models as mm
import machine_script as ms
from cam_data import Library
from test_server import client, wait_for  # noqa: F401

GOOD = '''
node("base")
node("bed", "base", stock=True)
node("gantry", "base", axis="y")
node("head", "gantry", axis="x")
node("spindle", "head", axis="z")
part("bed", "bed", box(400, 300, 18, zmin=-18), "mdf", collision=True)
part("gantry", "gantry", box(520, 40, 60, zmin=120), "anodised", collision=True)
part("spindle body", "spindle", cyl_z(32.5, 90, at=(-150, 100), zmin=110), "alu", collision=True)
part("collet nut", "spindle", cyl_z(9.5, 18, at=(-150, 100), zmin=92), "steel", collision=True)
machine(home=(-150, 100, 92), travel=(300, 200, 80), clearance=92, nose=[{"name": "collet nut", "r": 9.5, "h": 18}, {"name": "spindle", "r": 32.5, "h": 90}],
        table={"x": 400, "y": 300, "t": 18}, spoilboard=18, sources={"everything": "estimated: test"}, notes="test router")
'''


def rec(name="My router", post="grbl", rotary=None):
    return cam.Machine(name=name, post=post, travel={"x": 300, "y": 200, "z": 80}, rotary=rotary)


def test_builtin_scripts_cover_every_default_machine(tmp_path):
    lib = Library(tmp_path)
    for m in lib.machines().values():
        code, is_user = ms.machine_code(m, tmp_path)
        assert not is_user and ms.builtin_key(m) in code.lower().replace("-", "_") or not is_user
        model = ms.run(code, m, tmp_path)
        assert model.name == m.name and model.parts and not model.warnings


def test_script_api_builds_and_summarises():
    model = ms.run(GOOD, rec())
    assert model.key == "my-router" and model.home == (-150, 100, 92) and model.spoilboard == 18 and [p.name for p in model.parts][:2] == ["bed", "gantry"]
    assert next(n for n in model.nodes if n.stock).name == "bed"
    text = ms.summary(model)
    assert "collet nut Ø19.0 × 18" in text and "spindle < head: Z head" in text and "collision parts: bed, gantry" in text
    pay = mm.mesh_payload(model, "draft")
    assert len(pay["parts"]) == 4 and pay["machine"]["key"] == "my-router"


@pytest.mark.parametrize("code, msg", [
    ("node('base', stock=True); node('z', 'base', axis='z'); part('x', 'nope', box(1, 1, 1)); machine(home=(0,0,0), travel=(1,1,1), clearance=1, nose=[{'r': 1, 'h': 1}], table={'x': 1, 'y': 1})", "node 'nope' is not a node"),
    ("node('base'); machine(home=(0,0,0), travel=(1,1,1), clearance=1, nose=[{'r': 1, 'h': 1}], table={'x': 1, 'y': 1})", "no parts"),
    ("node('base', stock=True); node('z', 'base', axis='z'); part('x', 'base', box(1, 1, 1))", "was not called"),
    ("node('base'); node('b', 'base', stock=True); part('x', 'base', box(1, 1, 1)); machine(home=(0,0,0), travel=(1,1,1), clearance=1, nose=[{'r': 1, 'h': 1}], table={'x': 1, 'y': 1})", "no spindle node"),
    ("node('base'); node('z', 'base', axis='z'); part('x', 'base', box(1, 1, 1)); machine(home=(0,0,0), travel=(1,1,1), clearance=1, nose=[], table={'x': 1, 'y': 1})", "exactly one node must carry the work"),
    ("node('base', stock=True); node('z', 'base', axis='z'); part('x', 'base', box(1, 1, 1)); machine(home=(0,0,0), travel=(1,1,1), clearance=1, nose=[], table={'x': 1, 'y': 1})", "nose needs at least one level"),
    ("node('base', stock=True); node('z', 'base', axis='q')", "axis must be one of"),
    ("node('base'); part('x', 'base', box(1, 1, 1), material='gold')", "unknown material"),
    ("node('base'); node('c', 'base', axis='a')", "needs pivot"),
    ("node('base'); x = 1 / 0", "ZeroDivisionError"),
])
def test_validation_messages(code, msg):
    with pytest.raises(ms.MachineScriptError, match=msg):
        ms.run(code, rec())


def test_user_script_replaces_the_builtin_and_cache_follows_it(tmp_path):
    lib = Library(tmp_path)
    m = lib.machines()["Generic 3018"]
    assert mm.model_for(m, tmp_path).key == "router"
    p = ms.user_path(m.name, tmp_path); p.write_text(GOOD)
    model = mm.model_for(m, tmp_path)
    assert model.key == "generic-3018" and model.notes == "test router" and "user" in model.sources["model"]
    pay1 = mm.cached_payload(m, "draft", tmp_path); assert len(pay1["parts"]) == 4
    p.write_text(GOOD.replace('part("gantry"', 'part("gantry2"')); import os, time; os.utime(p, (time.time() + 2, time.time() + 2))
    assert {x["name"] for x in mm.cached_payload(m, "draft", tmp_path)["parts"]} >= {"gantry2"}
    assert lib.machine_scripts() == ["Generic 3018"]
    assert lib.delete_machine("Generic 3018") and not p.exists()


def test_machine_script_api_and_ws(client):
    r = client.get("/api/cam/machine_code?name=Makera%20Z1").json()
    assert not r["user"] and "node(\"spindle\"" in r["code"] and "bed_holes(\"makera_z1_mdf\")" in r["code"]
    assert client.get("/api/cam/machine_code?name=Nope").status_code == 404
    with client.websocket_connect("/ws") as ws:
        wait_for(ws, "model")
        ws.send_json({"type": "get_machine_code", "name": "Generic 3018"})
        m = wait_for(ws, "machine")
        assert m["name"] == "Generic 3018" and m["source"] == "load" and not m["user"] and not m["show"]
        ws.send_json({"type": "run_machine", "name": "Generic 3018", "code": "node('base')"})
        e = wait_for(ws, "machine_error")
        assert "no parts" in e["text"]
        ws.send_json({"type": "run_machine", "name": "Generic 3018", "code": GOOD})
        m = wait_for(ws, "machine")
        assert m["user"] and m["show"] and "4 parts on 5 nodes" in m["summary"]
        lib = wait_for(ws, "library")
        assert lib["machine_scripts"] == ["Generic 3018"]
    r = client.get("/api/cam/machine_code?name=Generic%203018").json()
    assert r["user"] and r["code"] == GOOD
    r = client.get("/api/cam/machine_model?name=Generic%203018&quality=draft").json()
    assert r["machine"]["key"] == "generic-3018" and {p["name"] for p in r["parts"]} == {"bed", "gantry", "spindle body", "collet nut"}


DOORS = GOOD + '''
node("lid", "base", door="hinge", pivot=(0, 150, 200), direction=(1, 0, 0), open=-80)
part("lid", "lid", box(400, 300, 4, at=(0, 0, 0), zmin=200), "acrylic")
node("door L", "base", door="slide", direction=(-1, 0, 0), open=300)
part("door L", "door L", box(200, 4, 150, at=(-100, -152, 0), zmin=0), "panel")
'''


def test_door_nodes_validate_and_reach_the_payload():
    model = ms.run(DOORS, rec())
    doors = {n.name: n for n in model.nodes if n.door}
    assert doors["lid"].door == "hinge" and doors["lid"].open == -80 and doors["lid"].pivot == (0, 150, 200)
    assert doors["door L"].door == "slide" and doors["door L"].direction == (-1.0, 0.0, 0.0)
    pay = model.to_payload()
    nd = {n["name"]: n for n in pay["nodes"]}
    assert nd["lid"]["door"] == "hinge" and nd["lid"]["open"] == -80 and nd["door L"]["direction"] == [-1.0, 0.0, 0.0]
    assert "door (hinge, open -80)" in ms.summary(model)
    for code, msg in [("node('base'); node('d', 'base', door='swing')", "door must be"),
                      ("node('base'); node('d', 'base', door='hinge', direction=(1,0,0), open=10)", "needs pivot"),
                      ("node('base'); node('d', 'base', door='slide', direction=(0,0,0), open=10)", "non-zero"),
                      ("node('base'); node('d', 'base', door='slide', direction=(1,0,0))", "needs open"),
                      ("node('base'); node('d', 'base', axis='x', door='slide', direction=(1,0,0), open=10)", "has no axis")]:
        with pytest.raises(ms.MachineScriptError, match=msg):
            ms.run(code, rec())


def test_builtins_have_doors_where_the_machine_has_them(tmp_path):
    lib = Library(tmp_path).machines()
    doors = {n: [x.door for x in mm.model_for(lib[n], tmp_path).nodes if x.door] for n in ("Makera Z1", "Carvera Air", "Haas VF-4", "Haas VF-2", "Generic 3018")}
    assert doors["Makera Z1"] == ["hinge"] and doors["Carvera Air"] == ["hinge"] and doors["Haas VF-4"] == ["slide", "slide"] and doors["Haas VF-2"] == ["slide", "slide"] and doors["Generic 3018"] == []
    z1 = mm.model_for(lib["Makera Z1"], tmp_path)
    assert any(p.name == "tool length sensor" and p.node == "bed" for p in z1.parts) and not any("probe" in p.name or "rail" in p.name for p in z1.parts)
