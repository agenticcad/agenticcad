"""Fixtures: built-in scripts build, setups seat the stock in them, the simulator flags jaw strikes, the fixtures() line
and the API/WebSocket round-trips."""
import json

import pytest

import cad_kernel as ck
import cam_kernel as cam
import cam_sim
import fixture_lib as fl
from cam_data import Library
from test_cam_setups import PLATE, flat
from test_server import client, wait_for  # noqa: F401

STOCK = {"x": 66.0, "y": 46.0, "z": 13.0}
MINI = '''
part("block", box(80, 40, 20, zmin=0), "alu")
part("post", cyl_z(5, 30, at=(0, -30), zmin=0), "steel", collision=False)
fixture("Mini riser", work_origin=(0, 0, 20), clamp_axis=None, notes="test")
'''


@pytest.fixture(scope="module")
def machines(tmp_path_factory):
    return Library(tmp_path_factory.mktemp("lib")).machines()


@pytest.fixture(scope="module")
def plate():
    return ck.run_script(PLATE)


def test_builtin_fixtures_build_and_seat_the_stock():
    names = [f["name"] for f in fl.list_fixtures(None)]
    assert set(names) == set(fl.BUILTINS.values())
    for name in names:
        m = fl.model_for(name, {}, STOCK)
        assert m.parts and not m.warnings and m.height >= 0 and len(m.to_payload()["bbox"]) == 2
        pay = fl.mesh_payload(m, "draft")
        assert len(pay["parts"]) == len(m.parts) and not pay["warnings"]
    vise = fl.model_for("Makera low-profile vise", {}, STOCK)
    assert vise.clamp_axis == "y" and vise.height == pytest.approx(12 + (20 - 13 + 2))      # thin stock on an auto parallel
    assert any(p.name.startswith("parallel") for p in vise.parts)
    assert fl.model_for("Makera low-profile vise", {"parallel": 0}, STOCK).height == 12       # explicit: no parallel
    assert fl.model_for('6" Kurt-style vise', {"parallel": 20}, STOCK).height == pytest.approx(63)
    assert "Stock sits" in fl.summary(vise) or "work origin" in fl.summary(vise)
    with pytest.raises(fl.FixtureScriptError, match="no fixture named"):
        fl.model_for("Nope")
    with pytest.raises(fl.FixtureScriptError, match="stock size"):
        fl.model_for("Step clamps", {}, None)


def test_user_fixture_script_lists_and_replaces(tmp_path):
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "mini-riser.fixture.py").write_text(MINI)
    names = {f["name"]: f for f in fl.list_fixtures(tmp_path)}
    assert names["Mini riser"]["user"] and not names["Tooling plate"]["user"]
    m = fl.model_for("Mini riser", {}, STOCK, tmp_path)
    assert m.height == 20 and [p.collision for p in m.parts] == [True, False] and "user" in m.sources["model"]
    code, is_user = fl.fixture_code("Mini riser", tmp_path); assert is_user and code == MINI
    (tmp_path / "fixtures" / "three.fixture.py").write_text(MINI.replace('fixture("Mini riser"', "fixture('3\" mini vise'"))
    assert '3" mini vise' in {f["name"] for f in fl.list_fixtures(tmp_path)} and fl.fixture_code('3" mini vise', tmp_path)[1]
    for bad, msg in [("fixture('x', work_origin=(0,0,0))", "no parts"), ("part('a', box(1,1,1))", "was not called"),
                     ("part('a', box(1,1,1)); fixture('x')", "work_origin"), ("part('a', box(1,1,1)); fixture('x', work_origin=(0,0,0), clamp_axis='z')", "clamp_axis"),
                     ("part('a', box(1,1,1), material='gold'); fixture('x', work_origin=(0,0,0))", "unknown material")]:
        with pytest.raises(fl.FixtureScriptError, match=msg):
            fl.run(bad, {}, STOCK)


def test_setup_with_fixture_and_jaw_strike(machines, plate):
    m = machines["Makera Z1"]
    stock = cam.Stock.from_model(plate, margin=3, top=1)
    st = cam.Setup(m, stock, name="Top", fixture="Makera low-profile vise")
    fp = st.fixture_payload()
    assert fp["name"] == "Makera low-profile vise" and fp["height"] == st.fixture_height() == pytest.approx(21) and fp["clamp_axis"] == "y"
    p = cam.Program(st)
    p.add(cam.face(st, flat(), z_top=st.stock.top, z_bottom=12))
    p.add(cam.pocket(st, flat(), cam.rect(-15, -10, 15, 10), z_top=12, z_bottom=8))
    r = cam_sim.simulate(p, part=plate)
    assert r.stats["collisions"]["count"] == 0 and r.stats["fixture"]["name"] == "Makera low-profile vise"
    assert p.to_payload()["setups"][0]["fixture"]["height"] == pytest.approx(21)
    deep = cam.Program(st)
    deep.add(cam.contour(st, flat(), cam.section(plate.shape, 6), z_top=12, z_bottom=-2, side="outside", name="Outline"))
    r2 = cam_sim.simulate(deep, part=plate)
    hits = r2.stats["collisions"]["hits"]
    assert r2.stats["collisions"]["count"] > 0 and any(h["what"].startswith("fixture") and h["part"] == "tool" for h in hits)
    assert "fixture" in r2.summary()
    # the fixtures({...}) line (CAM tab) wins over Setup(fixture=...)
    cam.reset_overrides(); cam.fixtures({"Top": {"name": None}})
    assert cam.Setup(m, stock, name="Top", fixture="Makera low-profile vise").fixture is None
    cam.reset_overrides(); cam.fixtures({"Top": {"name": "Tooling plate", "params": {"thickness": 20}}})
    st3 = cam.Setup(m, stock, name="Top"); assert st3.fixture == "Tooling plate" and st3.fixture_height() == 20
    cam.reset_overrides()
    with pytest.raises(cam.CamError, match="no fixture named"):
        cam.Setup(m, stock, fixture="Nope")
    with pytest.raises(cam.CamError, match="rotary"):
        cam.Setup(machines["Makera Z1 + 4th axis"], cam.Stock.cylinder(32, 62, x0=-1), rotary=True, fixture="Tooling plate")


def test_fixture_api_and_ws(client):
    names = {f["name"] for f in client.get("/api/cam/fixtures").json()["fixtures"]}
    assert "Makera low-profile vise" in names and "Step clamps" in names
    r = client.get("/api/cam/fixture_model?name=Tooling%20plate&quality=draft&stock=%7B%22x%22%3A60%2C%22y%22%3A40%2C%22z%22%3A12%7D").json()
    assert r["fixture"]["name"] == "Tooling plate" and r["parts"][0]["name"] == "plate" and r["fixture"]["height"] == 15
    assert client.get("/api/cam/fixture_model?name=Nope").status_code == 400
    assert "work_origin" in client.get("/api/cam/fixture_code?name=Step%20clamps").json()["code"]
    code = ("part = bodies['Bracket']\nstock = Stock.from_model(part, margin=2, top=1)\n"
            "setup = Setup(machines['Makera Z1'], stock, name='Top')\nprogram = Program(setup)\n"
            "program.add(face(setup, tools[1], z_top=stock.top, z_bottom=stock.top - 1))\n")
    with client.websocket_connect("/ws") as ws:
        wait_for(ws, "model")
        ws.send_json({"type": "run_cam", "code": code})
        m = wait_for(ws, "cam")
        while not m.get("program"):
            m = wait_for(ws, "cam")
        assert m["program"]["setups"][0]["fixture"] is None
        ws.send_json({"type": "set_fixture", "setup": "Top", "fixture": {"name": "Makera low-profile vise"}})
        m = wait_for(ws, "cam")
        assert m["program"]["setups"][0]["fixture"]["name"] == "Makera low-profile vise" and "fixtures({" in m["code"]
        ws.send_json({"type": "set_fixture", "setup": "Top", "fixture": None})
        m = wait_for(ws, "cam")
        assert m["program"]["setups"][0]["fixture"] is None and "fixtures({" not in m["code"]
        ws.send_json({"type": "run_fixture", "code": MINI})
        f = wait_for(ws, "fixture")
        assert f["name"] == "Mini riser" and f["user"] and "Mini riser" in f["summary"]
        lib = wait_for(ws, "library")
        assert any(x["name"] == "Mini riser" and x["user"] for x in lib["fixtures"])
        ws.send_json({"type": "run_fixture", "code": "part('a', box(1,1,1))"})
        assert "was not called" in wait_for(ws, "fixture_error")["text"]
    assert client.get("/api/cam/fixture_code?name=Mini%20riser").json()["user"]
