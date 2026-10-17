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
    # Makera's dimensions: 226 × 103 footprint, jaws close along X, bolted to the bed with the MDF removed; 13 mm stock
    # (thinner than the 22.6 mm jaws) sits on the jaw ledges 20 mm above the 12.6 mm rails
    lo, hi = vise.bbox()
    assert hi[0] - lo[0] == pytest.approx(226) and hi[1] - lo[1] == pytest.approx(103) and hi[2] == pytest.approx(12.6 + 22.6)
    assert vise.clamp_axis == "x" and vise.replaces_spoilboard and vise.home == (0.0, -51.5)          # on the bed's pins
    assert vise.height == pytest.approx(12.6 + 20.0) and vise.max_opening == pytest.approx(136)
    assert not any(p.name.startswith("parallel") for p in vise.parts)
    rails = fl.model_for("Makera low-profile vise", {"seat": "rails"}, STOCK)
    assert rails.height == pytest.approx(12.6) and rails.max_opening == pytest.approx(130)
    assert fl.model_for("Makera low-profile vise", {}, {"x": 66, "y": 46, "z": 30}).height == pytest.approx(12.6)    # tall stock: rails
    # the jaws grip the stock: on the ledges the rebate walls sit at the stock's X faces (stock centred between them)
    jaws = {p.name: p.shape.bounding_box() for p in vise.parts if p.name in ("fixed jaw", "moving jaw")}
    assert jaws["moving jaw"].min.X - jaws["fixed jaw"].max.X == pytest.approx(66 - 6)
    assert (jaws["fixed jaw"].max.X + jaws["moving jaw"].min.X) / 2 == pytest.approx(vise.work_origin[0])
    with pytest.raises(fl.FixtureScriptError, match="opens to 136 mm"):
        fl.model_for("Makera low-profile vise", {}, {"x": 140, "y": 40, "z": 8})
    assert fl.model_for("Makera low-profile vise", {"base_h": 13.0}, STOCK).height == pytest.approx(33.0)   # caliper override
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
    assert fp["name"] == "Makera low-profile vise" and fp["height"] == st.fixture_height() == pytest.approx(32.6) and fp["clamp_axis"] == "x"
    assert fp["at"] == [0.0, -51.5] and fp["replaces_spoilboard"] and fp["extra"] == []         # its place on the Z1 bed's pins
    p = cam.Program(st)
    p.add(cam.face(st, flat(), z_top=st.stock.top, z_bottom=12))
    p.add(cam.pocket(st, flat(), cam.rect(-15, -10, 15, 10), z_top=12, z_bottom=8))
    r = cam_sim.simulate(p, part=plate)
    assert r.stats["collisions"]["count"] == 0 and r.stats["fixture"]["name"] == "Makera low-profile vise"
    assert p.to_payload()["setups"][0]["fixture"]["height"] == pytest.approx(32.6)
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


def test_several_fixtures_on_the_table(machines, plate):
    m = machines["Makera Z1"]
    stock = cam.Stock.from_model(plate, margin=3, top=1)
    st = cam.Setup(m, stock, name="Top", fixture="Makera low-profile vise",
                   fixtures=[{"name": "Tooling plate", "at": (0, 60), "rot": 90, "params": {"size": [60, 40], "thickness": 10}}])
    lay = st.fixture_layout()
    assert [f["name"] for f in lay] == ["Makera low-profile vise", "Tooling plate"] and lay[0]["holds"] and not lay[1]["holds"]
    # all fixtures stand on one plane: the plate's frame is as far below the stock as the vise's, 111.5 mm behind the vise
    assert lay[1]["origin_model"][2] == pytest.approx(lay[0]["origin_model"][2])
    assert lay[1]["origin_model"][1] - lay[0]["origin_model"][1] == pytest.approx(111.5)
    pay = st.fixture_payload()
    assert [e["name"] for e in pay["extra"]] == ["Tooling plate"] and pay["extra"][0]["rot"] == 90 and pay["replaces_spoilboard"]
    assert st.fixture_warnings() == []
    # moved onto the vise: the overlap is reported
    bad = cam.Setup(m, stock, name="Top", fixture="Makera low-profile vise", fixtures=[{"name": "Tooling plate", "at": (60, -40)}])
    assert any("overlap" in w for w in bad.fixture_warnings())
    # the simulator checks the tool against every fixture and names the one it hit
    p = cam.Program(st)
    ox, oy, oz = lay[1]["origin_model"]                 # the plate's top is 10 mm above its frame origin
    p.add(cam.contour(st, flat(), cam.rect(ox - 10, oy - 10, ox + 10, oy + 10), z_top=oz + 12, z_bottom=oz + 8, side="on", lead=0, ramp=0, name="Reach"))
    r = cam_sim.simulate(p, part=plate)
    assert {h["what"] for h in r.stats["collisions"]["hits"]} >= {"fixture (Tooling plate)"}
    assert [e["name"] for e in r.stats["fixture"]["extra"]] == ["Tooling plate"]
    # extra fixtures need a name; a Z1 vise on another machine is flagged
    with pytest.raises(cam.CamError, match="each extra fixture"):
        cam.Setup(m, stock, fixtures=[{"at": (0, 0)}])
    air = cam.Setup(machines["Carvera Air"], stock, name="Top", fixture="Makera low-profile vise")
    assert any("made for Makera Z1" in w for w in air.fixture_warnings())
    # the fixtures({...}) line carries the extra fixtures too
    cam.reset_overrides(); cam.fixtures({"Top": {"name": None, "extra": [{"name": "Step clamps", "at": [0, 0], "rot": 0}]}})
    st2 = cam.Setup(m, stock, name="Top", fixture="Makera low-profile vise")
    assert st2.fixture is None and [f["name"] for f in st2.fixture_layout()] == ["Step clamps"]
    cam.reset_overrides()


def test_bare_stock_against_the_l_bracket_with_strap_clamps(machines, plate):
    """The Z1 test setup: a block pushed into the L-bracket (fixed by dowels) and held by strap clamps."""
    m = machines["Makera Z1"]
    stock = cam.Stock.block(101.42, 100.32, 10.0, center_xy=(5.0, -3.0), top=2.0)
    at = (-88 + 101.42 / 2, -88 + 100.32 / 2)                       # front-left corner in the bracket's inside corner
    cam.reset_overrides()
    cam.fixtures({"Top": {"name": None, "at": list(at), "extra": [
        {"name": "Makera L-bracket", "params": {"type": "thin"}},
        {"name": "Strap clamp", "at": [at[0] + 101.42 / 2, at[1], ], "rot": 0}]}})
    st = cam.Setup(m, stock, name="Top", origin="stock-top-left")
    lay = st.fixture_layout()
    br = next(f for f in lay if f["name"] == "Makera L-bracket")
    assert br["at"] == (-88.0, -88.0)                                # its own dowel position, no "at" needed
    assert br["origin_model"] == pytest.approx((stock.xmin, stock.ymin, stock.zmin), abs=1e-6)   # bracket corner = stock corner
    pay = st.fixture_payload()
    assert pay["name"] is None and pay["at"] == pytest.approx(list(at)) and [e["name"] for e in pay["extra"]] == ["Makera L-bracket", "Strap clamp"]
    # a cut that runs past the stock's left face below the bracket top hits the bracket; one inside the stock doesn't
    p = cam.Program(st)
    p.add(cam.contour(st, flat(), cam.rect(stock.xmin - 3, stock.ymin + 20, stock.xmin + 10, stock.ymin + 40), z_top=stock.top,
                      z_bottom=stock.zmin + 3, side="on", lead=0, ramp=0, name="Past the face"))
    r = cam_sim.simulate(p, part=None)
    assert any(h["what"] == "fixture (Makera L-bracket)" for h in r.stats["collisions"]["hits"])
    ok = cam.Program(st)
    ok.add(cam.contour(st, flat(), cam.rect(stock.xmin + 15, stock.ymin + 20, stock.xmin + 40, stock.ymin + 40), z_top=stock.top,
                       z_bottom=stock.top - 3, side="on", lead=0, ramp=0, name="Inside"))
    r2 = cam_sim.simulate(ok, part=None)
    assert not any("L-bracket" in h["what"] for h in r2.stats["collisions"]["hits"])
    cam.reset_overrides()


def test_table_check_uses_the_bed_under_a_removed_spoilboard(machines, plate):
    m = machines["Makera Z1"]
    stock = cam.Stock.from_model(plate, margin=3, top=1)
    st = cam.Setup(m, stock, name="Top", fixture="Makera low-profile vise")
    p = cam.Program(st)            # 40 mm below the stock bottom: through the ledge gap and the 12.6 mm extrusion, into the bed
    p.add(cam.drill(st, cam.Tool(5, "3 mm drill", "drill", 3.0, 2, 60, rpm=8000, feed=300, plunge=150, stepdown=3.0, stepover=1.0, angle=118),
                    [(0, 0)], z_top=st.stock.top, z_bottom=st.stock.bottom - 40))
    r = cam_sim.simulate(p, part=plate)
    hits = r.stats["collisions"]["hits"]
    assert any(h["what"] == "table" and h["depth"] == pytest.approx(40 - 32.6, abs=0.3) for h in hits)


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
        ws.send_json({"type": "set_fixture", "setup": "Top", "fixture": {"name": "Makera low-profile vise", "at": [0, 10],
                      "extra": [{"name": "Tooling plate", "at": [0, -80], "rot": 90}]}})
        m = wait_for(ws, "cam")
        fx = m["program"]["setups"][0]["fixture"]
        assert [e["name"] for e in fx["extra"]] == ["Tooling plate"] and fx["extra"][0]["rot"] == 90 and "'extra'" in m["code"]
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
