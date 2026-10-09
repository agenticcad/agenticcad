"""Parametric configurations: the script's `configurations` dict, builds with a configuration active, the agent's tool and the API."""
import pytest

import cad_kernel as ck
import script_edit as se
from conftest import close
from test_agent_ops import ag, build  # noqa: F401
from test_server import client, wait_for  # noqa: F401

CODE = "plate_l = 60   # length\nplate_w, plate_t = 40, 8\nhole_d = 5.0\nresult = {'Plate': Box(plate_l, plate_w, plate_t) - Cylinder(hole_d / 2, 50)}\n"
CFGS = {"Small": {"plate_l": 40, "plate_w": 30}, "Large": {"plate_l": 120.5, "hole_d": 8}}


def test_script_edit_round_trip_and_validation():
    assert se.configurations(CODE) == {}
    c2 = se.set_configurations(CODE, CFGS)
    assert se.configurations(c2) == {"Small": {"plate_l": 40, "plate_w": 30}, "Large": {"plate_l": 120.5, "hole_d": 8}}
    assert c2.index("configurations =") < c2.index("result =") and c2.count("configurations =") == 1
    assert se.params(c2)[0]["comment"] == "length"                     # the parameter line kept its comment
    c3 = se.set_configurations(c2, {"Small": {"plate_l": 45}})          # replace in place, one block
    assert se.configurations(c3) == {"Small": {"plate_l": 45}} and c3.count("configurations =") == 1
    assert "configurations" not in se.set_configurations(c3, {})        # empty removes the block
    with pytest.raises(se.Refused, match="not numeric top-level"):
        se.set_configurations(CODE, {"X": {"nope": 1}})
    with pytest.raises(se.Refused, match="name"):
        se.set_configurations(CODE, {" bad": {"plate_l": 1}})
    # a hand-written block that is not a literal is refused on read
    with pytest.raises(se.Unsupported):
        se.configurations(CODE + "configurations = {'A': {'plate_l': plate_l * 2}}\n")


def test_kernel_builds_the_active_configuration():
    code = se.set_configurations(CODE, CFGS)
    base = ck.run_script(code, "draft")
    assert base.config is None and base.configurations == ["Small", "Large"] and close(base.bbox_max[0], 30)
    large = ck.run_script(code, "draft", config="Large")
    assert large.config == "Large" and close(large.bbox_max[0], 60.25) and large.code == code    # the script itself is untouched
    assert any(abs(f.radius - 4) < 1e-6 for f in large.faces if f.kind == "CYLINDER")               # hole_d 8 applied
    with pytest.raises(ck.CadError, match="no configuration named"):
        ck.run_script(code, "draft", config="Huge")
    # an override naming a parameter the script no longer has is ignored with a warning, not a failure
    stale = code.replace("hole_d = 5.0\n", "").replace("Cylinder(hole_d / 2, 50)", "Cylinder(2.5, 50)")
    m = ck.run_script(stale, "draft", config="Large")
    assert close(m.bbox_max[0], 60.25) and any("hole_d" in w for w in m.warnings)


async def test_agent_configurations_flow(ag):
    ag._make_server()
    await build(ag, CODE)
    h = ag.tool_handlers
    assert "no configurations" in (await h["configurations"]({"action": "list"}))["content"][0]["text"]
    r = await h["configurations"]({"action": "set", "name": "Large", "values": {"plate_l": 120, "hole_d": 8}})
    assert "Large" in r["content"][0]["text"] and se.configurations(ag.model.code) == {"Large": {"plate_l": 120, "hole_d": 8}}
    assert ag.config is None and close(ag.model.bbox_max[0], 30)            # declaring a variant does not switch to it
    r = await h["configurations"]({"action": "activate", "name": "Large"})
    assert ag.config == "Large" and close(ag.model.bbox_max[0], 60) and ag.rec.last("design")["config"] == "Large"
    ps = {p["name"]: p for p in ag.get_params()}
    assert ps["plate_l"]["value"] == 120 and ps["plate_l"]["base"] == 60 and ps["plate_l"].get("override")
    assert ps["plate_w"]["value"] == 40 and not ps["plate_w"].get("override")
    # editing a parameter while a configuration is active edits that configuration, not the literal
    await ag.set_params({"plate_w": 55})
    assert se.params(ag.model.code)[1]["value"] == 40 and se.configurations(ag.model.code)["Large"]["plate_w"] == 55
    assert close(ag.model.bbox_max[1], 27.5)
    # the agent rewriting the script without the active configuration falls back to the base instead of failing
    await ag.build(CODE, source="agent")
    assert ag.config is None and close(ag.model.bbox_max[0], 30)
    # export every variant and the base, named after the design
    await ag.set_configurations({"Small": {"plate_l": 40}, "Large": {"plate_l": 120}})
    paths = await ag.export_configurations(["step", "stl"])
    names = sorted(p.name for p in paths)
    assert names == sorted(["untitled-base.step", "untitled-base.stl", "untitled-Small.step", "untitled-Small.stl", "untitled-Large.step", "untitled-Large.stl"])
    assert all(p.exists() and p.stat().st_size > 1000 for p in paths)
    r = await h["configurations"]({"action": "delete", "name": "Small"})
    assert list(se.configurations(ag.model.code)) == ["Large"]
    assert (await h["configurations"]({"action": "activate", "name": "Nope"})).get("is_error")
    # state round-trips through state.json
    await ag.activate_configuration("Large")
    import json
    assert json.loads((ag.workspace / "state.json").read_text())["config"] == "Large"


def test_api_configs(client):
    import json
    with client.websocket_connect("/ws") as ws:
        wait_for(ws, "design")
        ws.send_json({"type": "run_code", "code": se.set_configurations(CODE, {"Small": {"plate_l": 40}})})
        wait_for(ws, "model")
    r = client.get("/api/configs").json()
    assert r["active"] is None and list(r["configurations"]) == ["Small"] and r["params"][0]["base"] == 60
    r = client.post("/api/configs", json={"action": "activate", "name": "Small"}).json()
    assert r["active"] == "Small"
    p = client.get("/api/params").json()
    assert p["config"] == "Small" and p["configurations"] == ["Small"] and {x["name"]: x["value"] for x in p["params"]}["plate_l"] == 40
    r = client.post("/api/configs", json={"action": "set_all", "configurations": {"Small": {"plate_l": 45}, "Big": {"plate_l": 90}}}).json()
    assert list(r["configurations"]) == ["Small", "Big"] and r["active"] == "Small"
    r = client.post("/api/configs", json={"action": "export", "formats": ["stl"], "names": ["Big"]}).json()
    assert r["files"] == ["demo-Big.stl"] or r["files"][0].endswith("-Big.stl")
    assert client.post("/api/configs", json={"action": "activate", "name": "Nope"}).status_code == 400
    client.post("/api/configs", json={"action": "activate", "name": None})
    assert client.get("/api/configs").json()["active"] is None
