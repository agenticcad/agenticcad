"""Extensions: the loader, the built-in examples, the agent's `extension` / `show_panel` tools and the WebSocket panel ops."""
import json

import pytest

import cad_kernel as ck
import extensions as X
from conftest import close
from test_agent_ops import ag, build  # noqa: F401  (fixture + helper)
from test_server import client, wait_for  # noqa: F401  (fixture + helper)

HOLED = '''wall = 3   # wall thickness
plate = Box(60, 40, wall * 2)
plate = tap(plate, "M4", (18, 12, wall), depth=5)
plate = plate - Pos(-18, -12, 0) * Cylinder(2.5, 20)
result = {"Plate": plate}
'''

GOOD_EXT = '''
@helper
def plate_area(shape):
    """Top-view area of a shape (bbox)."""
    bb = shape.bounding_box()
    return (bb.max.X - bb.min.X) * (bb.max.Y - bb.min.Y)

@tool("body_count", "How many bodies the design has")
def body_count(ctx, prefix: str = ""):
    return f"{len([b for b in ctx.bodies if b.path.startswith(prefix)])} bodies"

@tool("thicken", "Set wall", agent=False)
def thicken(ctx, wall: float = 4, **_):
    return {"set_params": {"wall": wall}}

@panel("Wall tuner", icon="W", description="test panel")
def wall_panel(ctx):
    w = ctx.state.get("wall", 3)
    return ui.panel(ui.slider("wall", "Wall", w, 1, 10, step=0.5, call="thicken"), ui.text(f"bodies: {len(ctx.bodies)}"),
                    ui.button("Thicker", call="thicken", args={"wall": 6}))

@on_build
def thin_wall(ctx):
    ps = {p["name"]: p["value"] for p in ctx.params()}
    if ps.get("wall", 99) < 2:
        return "wall is under 2 mm"
'''


def test_builtin_examples_load_and_render():
    E = X.for_workspace(None)
    mods = {m["id"]: m for m in E.modules()}
    assert {"parameter_explorer", "hole_report", "bolt_pattern"} <= set(mods) and not any(m["error"] for m in mods.values())
    m = ck.run_script(HOLED)
    for p in E.panels():
        ui = E.render(p.id, X.Ctx(m, None, state={}))
        assert ui["type"] == "panel" and ui["title"] == p.title and ui["children"]
    # hole detection: the tapped hole carries its thread, the plain one doesn't; the plain one goes through
    holes = {h["d"]: h for h in X.Ctx(m, None).holes()}
    assert holes[5.0]["through"] and holes[5.0]["thread"] is None
    assert holes[3.3]["thread"] == "M4" and not holes[3.3]["through"] and close(holes[3.3]["center"][0], 18, 0.01)
    # the hole report's table rows point at faces; the agent-visible tool lists them
    rows = next(c for c in E.render("hole_report.hole_report", X.Ctx(m, None, state={}))["children"] if c["type"] == "table")["rows"]
    assert len(rows) == 2 and all("face" in r for r in rows)
    assert "M4" in E.call("hole_report", X.Ctx(m, None), {})
    # bolt pattern: Apply returns a wrap edit that builds and adds the holes
    top = max((f for f in m.faces if f.kind == "PLANE"), key=lambda f: f.center[2])
    face = {"id": top.id, "body": "Plate", "label": top.short_label(), "center": list(top.center), "normal": list(top.normal)}
    res = E.call("apply_bolt_pattern", X.Ctx(m, None), {"face": face, "pcd": 30, "count": 3, "d": 2})
    import script_edit as se
    m2 = ck.run_script(se.wrap_body_expr(HOLED, "Plate", res["wrap"]["template"]))
    assert len(X.Ctx(m2, None).holes()) == 5 and m2.volume < m.volume
    # parameter explorer: the slider that changed is the parameter that is set
    assert E.call("apply_parameter", X.Ctx(m, None, state={"wall": 4.5}, event="wall"), {"wall": 4.5}) == {"set_params": {"wall": 4.5}}


def test_workspace_save_validate_toggle_delete(tmp_path):
    E = X.Extensions(tmp_path)
    assert E.describe().count("[builtin]") == 3
    with pytest.raises(X.ExtError, match="does not load"):
        E.save("bad", "def x(:\n")
    with pytest.raises(X.ExtError, match="registered nothing"):
        E.save("empty", "x = 1\n")
    with pytest.raises(X.ExtError, match="built-in"):
        E.save("hole_report", GOOD_EXT)
    with pytest.raises(X.ExtError, match="shadow"):
        E.save("shadow", "@helper\ndef extrude(x): return x\n")
    r = E.save("My Tuner", GOOD_EXT)
    assert r == {"module": "my_tuner", "helpers": ["plate_area"], "tools": ["body_count", "thicken"], "panels": ["Wall tuner"], "hooks": ["thin_wall"]}
    assert (tmp_path / "extensions" / "my_tuner.py").exists()
    # the helper is in the script namespace of this workspace; the tool schema comes from the signature
    m = ck.run_script("result = {'P': Box(10, 20, 3)}\nprint(plate_area(result['P']))", workspace=tmp_path)
    assert "200" in m.stdout
    t = E.tool("body_count")
    assert t.schema == {"type": "object", "properties": {"prefix": {"type": "string"}}} and t.agent
    assert not E.tool("my_tuner.thicken").agent and [e.name for e in E.tools(agent_only=True) if e.module == "my_tuner"] == ["body_count"]
    assert E.call("body_count", X.Ctx(m, tmp_path), {"prefix": "P"}) == "1 bodies"
    # hooks and panels
    assert E.run_hooks(X.Ctx(ck.run_script("wall = 1\nresult = Box(10, 10, wall)"), tmp_path)) == ["thin_wall: wall is under 2 mm"]
    ui = E.render("my_tuner.wall_tuner", X.Ctx(m, tmp_path, state={"wall": 7}))
    assert ui["children"][0]["value"] == 7 and ui["children"][0]["call"] == "thicken"
    # a clash with another extension's names is refused; disable hides it from every lookup; delete removes the file
    with pytest.raises(X.ExtError, match="taken"):
        E.save("other", "@tool('body_count', 'dup')\ndef body_count(ctx): return 1\n")
    E.set_enabled("my_tuner", False)
    assert "(disabled)" in E.describe() and not [e for e in E.panels() if e.module == "my_tuner"]
    with pytest.raises(X.ExtError):
        E.tool("body_count")
    E.set_enabled("my_tuner", True)
    assert E.tool("body_count")
    # a file that breaks later shows as broken and never raises for the others
    (tmp_path / "extensions" / "my_tuner.py").write_text("raise RuntimeError('boom')\n")
    broken = next(m_ for m_ in E.modules() if m_["id"] == "my_tuner")
    assert "boom" in broken["error"] and len(E.panels()) == 3
    E.delete("my_tuner")
    assert not (tmp_path / "extensions" / "my_tuner.py").exists()
    with pytest.raises(X.ExtError):
        E.delete("hole_report")


def test_ui_validation():
    X.validate_ui(X.ui.panel(X.ui.text("hi"), X.ui.table(["a"], [[1.23456]])))
    with pytest.raises(X.ExtError, match="needs an id"):
        X.validate_ui({"type": "panel", "children": [{"type": "number", "label": "x"}]})
    with pytest.raises(X.ExtError, match="duplicate"):
        X.validate_ui(X.ui.panel(X.ui.number("a", "A", 1), X.ui.checkbox("a", "A")))
    with pytest.raises(X.ExtError, match="unknown"):
        X.validate_ui({"type": "script", "src": "x"})
    assert X.ui.table(["a"], [[1.23456]])["rows"][0]["cells"] == [1.235]
    assert X.ui.select("s", "S", "b", ["a", ["b", "Bee"]])["options"] == [["a", "a"], ["b", "Bee"]]


async def test_agent_tools_and_result_application(ag):
    ag._make_server()
    await build(ag, HOLED)
    h = ag.tool_handlers
    assert "extension" in h and "show_panel" in h and "ext_hole_report" in h   # built-in @tool promoted to a first-class tool
    assert "ext_apply_parameter" not in h                                        # agent=False stays UI-only
    out = (await h["extension"]({"action": "list"}))["content"][0]["text"]
    assert "hole_report" in out and "Panels:" in out
    assert "RESULT KEYS" in (await h["extension"]({"action": "api"}))["content"][0]["text"]
    assert "M4" in (await h["ext_hole_report"]({}))["content"][0]["text"]
    # save, then call the new tool through the dispatcher (no restart needed); the panel opens on request
    r = await h["extension"]({"action": "save", "module": "tuner", "code": GOOD_EXT})
    assert "saved extension tuner" in r["content"][0]["text"] and ag.rec.last("extensions")["modules"]
    assert (await h["extension"]({"action": "call", "name": "body_count"}))["content"][0]["text"] == "1 bodies"
    await h["extension"]({"action": "open", "panel": "tuner.wall_tuner"})
    ev = ag.rec.last("ext_panel")
    assert ev["id"] == "tuner.wall_tuner" and ev["ui"]["children"][0]["type"] == "slider"
    # a UI-only tool reached through a button: its set_params rebuilds and the panel re-renders with the new state
    text = await ag.ext_call("thicken", {"wall": 6}, state={"wall": 3}, panel="tuner.wall_tuner")
    assert "wall=6" in text and ag.model.bbox_max[2] == pytest.approx(6, abs=1e-6)
    assert ag.rec.last("model")["source"] == "extension" and ag.rec.last("ext_panel")["state"]["wall"] == 3
    # on_build hooks warn through the chat and onto the model
    await ag.set_params({"wall": 1.5})
    assert any("wall is under 2 mm" in t for t in ag.rec.texts("info")) and any("thin_wall" in w for w in ag.model.warnings)
    # ephemeral panels and direct actions
    r = await h["show_panel"]({"title": "Holes", "ui": {"children": [{"type": "table", "columns": ["a"], "rows": [[1]]}]}})
    assert r["content"][0]["text"] == "panel shown" and ag.rec.last("ext_panel")["ephemeral"]
    r = await h["show_panel"]({"title": "bad", "ui": {"children": [{"type": "iframe"}]}})
    assert r.get("is_error")
    await ag.ext_apply({"select": {"faces": [1, 2]}, "chat": "add screws"})
    assert ag.rec.last("ext_view")["op"] == "select" and ag.rec.last("ext_chat")["text"] == "add screws"
    assert (await h["extension"]({"action": "save", "module": "x", "code": "x=1"})).get("is_error")
    await h["extension"]({"action": "delete", "module": "tuner"})
    assert "tuner" not in (await h["extension"]({"action": "list"}))["content"][0]["text"]


def test_ws_panel_ops_and_http(client):
    from test_server import wait_for
    r = client.get("/api/extensions").json()
    assert {p["id"] for p in r["panels"]} >= {"hole_report.hole_report"} and "RESULT KEYS" in r["api"]
    assert "def hole_report" in client.get("/api/extensions/hole_report/code").text
    assert client.get("/api/extensions/nope/code").status_code == 404
    with client.websocket_connect("/ws") as ws:
        wait_for(ws, "extensions")
        ws.send_json({"type": "ext", "op": "open", "panel": "hole_report.hole_report", "state": {}, "selection": {"faces": [], "bodies": []}})
        m = wait_for(ws, "ext_panel")
        assert m["id"] == "hole_report.hole_report" and m["ui"]["title"] == "Hole report"
        ws.send_json({"type": "ext", "op": "change", "panel": "hole_report.hole_report", "state": {"threaded_only": True}, "event": "threaded_only"})
        m = wait_for(ws, "ext_panel")
        assert m["state"] == {"threaded_only": True}
        ws.send_json({"type": "ext", "op": "action", "action": {"highlight": {"faces": [0]}}, "state": {}})
        assert wait_for(ws, "ext_view")["op"] == "highlight"
        ws.send_json({"type": "ext", "op": "call", "panel": None, "call": "no_such_tool", "state": {}})
        m = ws.receive_json()
        assert m["type"] == "error" and "no extension tool" in m["text"]
        # disable via HTTP → the list changes and the panel is gone
        assert client.post("/api/extensions/hole_report", json={"enabled": False}).status_code == 200
        m = wait_for(ws, "extensions")
        assert "hole_report.hole_report" not in {p["id"] for p in m["panels"]}
        client.post("/api/extensions/hole_report", json={"enabled": True})
        assert client.delete("/api/extensions/hole_report").status_code == 400
