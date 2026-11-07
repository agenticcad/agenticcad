"""Multiple open designs (tabs): every request and page belongs to one design, each design has its own working files,
model, CAM program and assistant; the machine is shared and belongs to the design whose program it runs."""
import json
import os
import sys
import time

import pytest
from fastapi.testclient import TestClient


BRACKET = """from build123d import *
result = Box(40, 20, 6)
"""
GEAR = """from build123d import *
result = Cylinder(15, 8)
"""


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    ws = tmp_path_factory.mktemp("ws")
    os.environ["AGENTICCAD_WORKSPACE"] = str(ws)
    os.environ["AGENTICCAD_NO_AGENT"] = "1"
    os.environ["AGENTICCAD_NO_REMOTE"] = "1"
    os.environ["AGENTICCAD_STARTUP_SYNC"] = "1"
    (ws / "designs").mkdir(exist_ok=True)
    (ws / "designs" / "gear.py").write_text(GEAR)
    for m in ("server", "agent"):
        sys.modules.pop(m, None)
    import server
    with TestClient(server.app) as c:
        c.server = server
        yield c


def _wait_ready(c, doc, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        info = {d["id"]: d for d in c.get("/api/docs").json()["docs"]}
        if doc in info and info[doc]["ready"]:
            return info[doc]
        time.sleep(0.1)
    raise AssertionError(f"{doc} never finished building")


def _bodies(c, doc, how="header"):
    if how == "header":
        r = c.get("/api/params", headers={"X-Doc": doc})
    elif how == "query":
        r = c.get(f"/api/params?doc={doc}")
    else:
        r = c.get("/api/params", headers={"referer": f"http://testserver/?doc={doc}"})
    return r


def test_tabs_are_isolated_designs(client):
    server = client.server
    docs = client.get("/api/docs").json()["docs"]
    assert [d["id"] for d in docs] == ["d1"]
    # a second tab that opens a saved design, a third that imports code
    r = client.post("/api/docs/new", json={"name": "gear"}).json()
    assert r["id"] == "d2"
    assert client.post("/api/docs/new", json={"name": "gear"}).json() == {"id": "d2", "existing": True}   # reused, not doubled
    r3 = client.post("/api/docs/new", json={"code": BRACKET}).json()
    assert r3["id"] == "d3"
    for d in ("d2", "d3"):
        _wait_ready(client, d)
    t0 = time.time()
    while time.time() - t0 < 30 and server.DOCS["d2"].design_name != "gear":
        time.sleep(0.1)
    a1, a2, a3 = server.DOCS["d1"], server.DOCS["d2"], server.DOCS["d3"]
    assert a2.design_name == "gear" and a3.design_name is None
    assert "Cylinder(15, 8)" in a2.model.code and "Box(40, 20, 6)" in a3.model.code
    # each tab has its own working copy
    ws = server.WORKSPACE
    assert (ws / "docs" / "d2" / "model.py").read_text().strip() == GEAR.strip()
    assert "Box(40, 20, 6)" in (ws / "docs" / "d3" / "model.py").read_text()
    assert a1.doc_dir == ws and a2.doc_dir == ws / "docs" / "d2"
    # the libraries are shared
    assert a1.library is a2.library is a3.library and a1.parts is a3.parts
    # requests pick their design by header, query or the page that made them (Referer)
    for how in ("header", "query", "referer"):
        assert client.get("/api/design/download", headers={"X-Doc": "d3"} if how == "header" else {"referer": "http://testserver/?doc=d3"} if how == "referer" else {},
                          params={"doc": "d3"} if how == "query" else None).text.count("Box(40, 20, 6)") == 1
    assert "Cylinder(15, 8)" in client.get("/api/design/download", headers={"X-Doc": "d2"}).text
    # registry listing
    info = {d["id"]: d for d in client.get("/api/docs").json()["docs"]}
    assert info["d2"]["name"] == "gear" and info["d3"]["dirty"] is True and info["d1"]["program"] is None
    assert (ws / "open_docs.json").read_text().count("d3") == 1


def test_pages_get_only_their_designs_events(client):
    with client.websocket_connect("/ws?doc=d2") as w2, client.websocket_connect("/ws?doc=d3") as w3:
        first2 = w2.receive_json(); first3 = w3.receive_json()
        assert first2["type"] == "model" and "Cylinder" in first2["code"]
        assert first3["type"] == "model" and "Box" in first3["code"]
        # an edit in d3 reaches d3's page only
        w3.send_json({"type": "run_code", "code": "from build123d import *\nresult = Box(41, 20, 6)\n"})
        got3 = None
        for _ in range(60):
            m = w3.receive_json()
            if m["type"] == "model" and "Box(41" in m.get("code", ""):
                got3 = m; break
        assert got3 is not None
        assert "Box(41" in client.get("/api/design/download", headers={"X-Doc": "d3"}).text
        assert "Box(41" not in client.get("/api/design/download", headers={"X-Doc": "d2"}).text
        # a machine (global) event reaches both
        server = client.server
        client.portal.call(server.bus.emit_all, {"type": "machine_link", "connected": False})
        seen2 = []
        for _ in range(40):                                   # everything d2's page got up to the machine event
            m = w2.receive_json(); seen2.append(m)
            if m["type"] == "machine_link":
                break
        assert seen2[-1]["type"] == "machine_link"
        assert not any("Box(41" in str(m.get("code", "")) for m in seen2)   # d3's rebuild never reached d2's page
        for _ in range(40):
            if w3.receive_json()["type"] == "machine_link":
                break
        else:
            raise AssertionError("d3's page missed the machine event")


def test_machine_belongs_to_the_design_running_a_program(client):
    server = client.server
    m = server.machine
    m.job = {"stage": "running", "owner": {"doc": "d2", "name": "gear"}, "name": "x.nc"}
    try:
        assert m.job_active
        # the design list says who owns it; that design cannot be closed while it runs
        assert client.get("/api/docs").json()["machine_doc"] == "d2"
        r = client.post("/api/docs/close", json={"id": "d2"})
        assert r.status_code == 409 and "running on the machine" in r.json()["error"]
        # another design's assistant is refused with the owner's name
        import machine_link as ml
        a3 = server.DOCS["d3"]
        m._need = lambda **kw: {"state": "Idle", "mpos": [0, 0, 0], "wpos": [0, 0, 0]}   # a connected, armed, homed machine
        with pytest.raises(ml.Refused, match="gear's program is running"):
            a3._machine_plan("raise_z", {})
        # a request that names no design (the phone page) acts for the machine's design
        assert server._default_doc() == "d2"
    finally:
        m.job = None
        m.__dict__.pop("_need", None)


def test_close_tab_removes_its_working_copy(client):
    server = client.server
    d = server.WORKSPACE / "docs" / "d3"
    assert d.exists()
    assert client.post("/api/docs/close", json={"id": "d3"}).json() == {"ok": True}
    assert "d3" not in server.DOCS and not d.exists()
    assert [x["id"] for x in client.get("/api/docs").json()["docs"]] == ["d1", "d2"]
    # the next new tab reuses the id with a fresh folder
    assert client.post("/api/docs/new", json={}).json()["id"] == "d3"
    _wait_ready(client, "d3")
    assert server.DOCS["d3"].design_name is None


def test_settings_are_shared_and_a_new_key_reaches_a_running_tab(client):
    """An API key saved in one tab reaches every tab; a tab that still holds the old settings must not erase it."""
    import asyncio
    import server
    if len(server.DOCS) < 2:
        client.post("/api/docs/new", json={})
    d1, d2 = list(server.DOCS)[:2]
    a1, a2 = server.DOCS[d1], server.DOCS[d2]
    assert client.post("/api/settings", json={"settings": {"api_key": "sk-test"}}, headers={"X-Doc": d1}).status_code == 200
    assert a2.settings["api_key"] == "sk-test"
    assert client.post("/api/settings", json={"settings": {"api_key": "__keep__", "units": "mm"}}, headers={"X-Doc": d2}).status_code == 200
    assert json.loads(a1._settings_path().read_text())["api_key"] == "sk-test" and a1.settings["units"] == "mm"

    class Live:
        stopped = False

        async def disconnect(self):
            self.stopped = True

    live = Live()
    a2.client, a2.client_key = live, "sk-test"            # started with this key: left running
    asyncio.run(a2.chat("hello"))
    assert a2.client is live and not live.stopped
    a2.client_key = ""                                    # started on the old sign-in: stops, and the turn starts it again
    asyncio.run(a2.chat("hello"))
    assert a2.client is None and live.stopped
    client.post("/api/settings", json={"settings": {"api_key": "", "units": ""}}, headers={"X-Doc": d1})
