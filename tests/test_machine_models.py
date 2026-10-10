"""Machine models: every supported machine builds, the kinematics/nose profile are sane, the simulator reports machine
collisions (holder against stock, tool through the spoilboard, holder against the chuck), and the API serves the model."""
import json

import pytest

import cad_kernel as ck
import cam_kernel as cam
import cam_sim
import machine_models as mm
from cam_data import Library
from test_cam_setups import PLATE, flat, two_sided, ball, SHAFT   # noqa: F401
from test_server import client, wait_for  # noqa: F401


@pytest.fixture(scope="module")
def machines(tmp_path_factory):
    return Library(tmp_path_factory.mktemp("lib")).machines()


@pytest.fixture(scope="module")
def plate():
    return ck.run_script(PLATE)


def test_every_default_machine_has_a_valid_model(machines):
    seen = set()
    for m in machines.values():
        model = mm.model_for(m)
        seen.add(model.key)
        names = {n.name for n in model.nodes}
        assert sum(n.stock for n in model.nodes) == 1 and all(n.parent in names for n in model.nodes if n.parent)
        assert any(n.axis == "z" and n.mode == "head" for n in model.nodes), model.name      # the spindle always moves Z
        assert model.nose and all(l["r"] > 0 and l["h"] > 0 for l in model.nose)
        assert model.clearance > 0 and model.table["x"] > 0 and model.sources
        for p in model.parts:
            assert p.shape.is_valid, f"{model.name}: {p.name} invalid"
            assert p.material in mm.MATERIALS and p.node in names
        assert any(p.collision for p in model.parts)
        if m.rotary:
            assert model.rotary and any("chuck" in p.name for p in model.parts)
    assert {"z1", "z1_4axis", "air", "air_4axis", "haas", "router"} <= seen


def test_payload_shapes_and_home_geometry(machines):
    z1 = mm.makera_z1(False)
    assert z1.home[2] == z1.clearance == 116 and z1.spoilboard == 6 and len(z1.table["holes"]) == 36
    pay = mm.mesh_payload(z1, "draft")
    assert pay["machine"]["key"] == "z1" and len(pay["parts"]) == len(z1.parts) and not pay["warnings"]
    for p in pay["parts"]:
        assert len(p["positions"]) % 3 == 0 and len(p["indices"]) % 3 == 0 and max(p["indices"]) * 3 < len(p["positions"])
    # the nose stack (collet nut + spindle nose) sits exactly at the home position: its bottom at clearance height
    stack = [p for p in z1.parts if p.name in ("spindle nose", "collet nut")]
    bbs = [p.shape.bounding_box() for p in stack]
    assert min(bb.min.Z for bb in bbs) == pytest.approx(116, abs=1e-6)
    assert all(bb.center().X == pytest.approx(z1.home[0], abs=0.5) and bb.center().Y == pytest.approx(z1.home[1], abs=0.5) for bb in bbs)
    haas = mm.haas_vmc("Haas VF-2")
    tb = next(p for p in haas.parts if p.name == "table").shape.bounding_box()
    assert tb.max.Z == pytest.approx(0) and tb.max.X - tb.min.X == pytest.approx(914.4)
    # user overrides are applied and flagged
    m = machines["Makera Z1"]
    m2 = cam.Machine(**{**m.__dict__, "model": {"nose": [{"name": "my nut", "r": 10, "h": 15}], "stickout": 22, "spoilboard": 5}})
    o = mm.model_for(m2)
    assert o.nose[0]["r"] == 10 and o.stickout == 22 and o.spoilboard == 5 and "user" in o.sources["nose profile"]
    assert cam_sim.holder_levels(o, flat())[1] == ("my nut", 10.0, 22.0, 15.0)


def test_simulation_reports_holder_and_table_collisions(machines, plate):
    good = cam_sim.simulate(two_sided(machines["Makera Z1"], plate), part=plate)
    assert good.stats["collisions"]["count"] == 0 and good.stats["machine"]["key"] == "z1"
    assert "No machine collisions" in good.summary()
    # a narrow slot deeper than the flutes: the Ø16 nose meets the walls, and the floor goes through the 6 mm spoilboard into the bed
    m = machines["Makera Z1"]
    st = cam.Setup(m, cam.Stock.from_model(plate, margin=3, top=1), name="Top")
    p = cam.Program(st)
    p.add(cam.pocket(st, flat(), cam.rect(-10, -3, 10, 3), z_top=13, z_bottom=-8, name="Deep slot"))
    r = cam_sim.simulate(p, part=plate)
    col = r.stats["collisions"]
    assert col["count"] > 0 and "COLLISIONS" in r.summary()
    kinds = {(h["part"], h["what"]) for h in col["hits"]}
    assert ("collet / nose", "stock") in kinds and ("tool", "bed under the spoilboard") in kinds
    nose = max(h for h in col["hits"] if h["part"] == "collet / nose")
    assert nose["depth"] == pytest.approx(3.0, abs=0.2)                     # nose bottom 18 above the tip at z=-8 → 10; stock top is 13
    bed = next(h for h in col["hits"] if h["what"] == "bed under the spoilboard")
    assert bed["depth"] == pytest.approx(2.0, abs=0.05) and bed["op"] == "Deep slot" and 0 <= bed["move"] < len(p.ops[0].moves)
    # the same slot only 10 deep clears everything
    ok = cam.Program(st); ok.add(cam.pocket(st, flat(), cam.rect(-10, -3, 10, 3), z_top=13, z_bottom=3, name="Slot"))
    assert cam_sim.simulate(ok, part=plate).stats["collisions"]["count"] == 0


def test_rotary_simulation_checks_the_chuck(machines):
    shaft = ck.run_script(SHAFT)
    m = machines["Makera Z1 + 4th axis"]
    st = cam.Setup(m, cam.Stock.cylinder(32, 62, x0=-1), rotary=True)
    p = cam.Program(st)
    p.add(cam.rotary_rough(st, flat(), shaft.shape, stepdown=2.0, mode="rings"))
    r = cam_sim.simulate(p, part=shaft)
    assert r.mode == "radial" and "collisions" in r.stats and r.stats["machine"]["key"] == "z1_4axis"
    # a cut that runs past the chuck face with a wide holder hits the chuck
    big = cam.Tool(9, "big", "flat", 3.175, 2, 12, rpm=12000, feed=600, plunge=150, stepdown=0.8, stepover=0.4)
    op = cam.Op("over the chuck", "contour", big)
    pb = cam.PathBuilder(st, op.tool, op)
    pb._add(cam.RAPID, -30, 0, 20, 0); pb._add(cam.FEED, -30, 0, 18, 300)
    crash = cam.Program(st); crash.add(op)
    model = mm.model_for(m); model.nose = [{"name": "nut", "r": 20.0, "h": 10.0}]
    r = cam_sim.simulate(crash, machine_model=model)
    assert any(h["what"] == "chuck" for h in r.stats["collisions"]["hits"])


def test_machine_model_api(client):
    assert client.get("/api/cam/machine_model").status_code == 404
    r = client.get("/api/cam/machine_model?name=Haas%20VF-2&info=1").json()
    assert r["machine"]["key"] == "haas" and r["machine"]["nose"][0]["name"] == "ER32 nut" and "table size and T-slots" in r["machine"]["sources"]
    r = client.get("/api/cam/machine_model?name=Makera%20Z1&quality=draft").json()
    assert r["machine"]["key"] == "z1" and {p["name"] for p in r["parts"]} >= {"spoilboard", "spindle nose", "enclosure"} and "acrylic" in r["materials"]
    code = ("part = bodies['Bracket']\nstock = Stock.from_model(part, margin=2, top=1)\n"
            "setup = Setup(machines['Generic 3018'], stock)\nprogram = Program(setup)\n"
            "program.add(face(setup, tools[1], z_top=stock.top, z_bottom=stock.top - 1))\n")
    with client.websocket_connect("/ws") as ws:
        wait_for(ws, "model")
        ws.send_json({"type": "run_cam", "code": code})
        m = wait_for(ws, "cam")
        while not m.get("program"):
            m = wait_for(ws, "cam")
    r = client.get("/api/cam/machine_model?quality=draft").json()
    assert r["machine"]["key"] == "router" and r["machine"]["name"] == "Generic 3018"
    s = client.post("/api/cam/simulate", json={"ops": [0]}).json()
    assert s["stats"]["collisions"]["count"] == 0 and s["stats"]["machine"]["key"] == "router"
    # overrides round-trip through the machine library
    mach = next(x for x in client.get("/api/cam/library").json()["machines"] if x["name"] == "Generic 3018")
    mach["model"] = {"stickout": 25}
    assert client.post("/api/cam/machine", json={"machine": mach}).status_code == 200
    assert client.get("/api/cam/machine_model?name=Generic%203018&info=1").json()["machine"]["stickout"] == 25
