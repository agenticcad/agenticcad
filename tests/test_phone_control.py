"""Phone control: off by default, a separate listener whose every API call needs the key from the QR link, and the
desktop app's own endpoints are not reachable through it."""
import os

from fastapi.testclient import TestClient


def test_phone_listener_needs_the_key_and_exposes_only_the_machine(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTICCAD_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("AGENTICCAD_NO_AGENT", "1")
    monkeypatch.setenv("AGENTICCAD_NO_REMOTE", "1")
    import importlib
    import server
    importlib.reload(server)
    cfg = server._remote_cfg()
    assert cfg["enabled"] is False and len(cfg["token"]) >= 20          # off by default, a random key
    c = TestClient(server.phone)
    assert c.get("/m").status_code == 200                               # the page itself
    assert c.get("/m/api/machine").status_code == 401                   # no key
    assert c.get("/m/api/machine", headers={"X-Key": "wrong"}).status_code == 401
    r = c.get("/m/api/machine", headers={"X-Key": cfg["token"]})
    assert r.status_code == 200 and r.json()["connected"] is False
    assert c.get(f"/m/api/machine?k={cfg['token']}").status_code == 200   # the camera <img> passes it in the URL
    assert c.get("/m/api/tool_photo/1").status_code == 401                # tool photos need the key too
    assert c.get(f"/m/api/tool_photo/1?k={cfg['token']}").status_code == 404   # T1 has no photo
    assert c.get("/api/settings", headers={"X-Key": cfg["token"]}).status_code in (401, 404)   # not the desktop app
    # a new link locks out the old key
    old = cfg["token"]
    d = TestClient(server.app)
    st = d.post("/api/remote", json={"new_link": True}).json()
    assert old not in st["url"] and c.get("/m/api/machine", headers={"X-Key": old}).status_code == 401


def test_key_can_be_turned_off_in_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTICCAD_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("AGENTICCAD_NO_AGENT", "1")
    monkeypatch.setenv("AGENTICCAD_NO_REMOTE", "1")
    import importlib
    import server
    importlib.reload(server)
    c, d = TestClient(server.phone), TestClient(server.app)
    assert server._remote_cfg()["require_key"] is True                     # on by default
    st = d.post("/api/remote", json={"require_key": False}).json()
    assert st["require_key"] is False and st["url"].endswith("/m")         # a plain link, nothing to rescan
    assert c.get("/m/api/machine").status_code == 200
    assert c.get("/api/settings").status_code == 404                       # still only the machine
    d.post("/api/remote", json={"require_key": True})
    assert c.get("/m/api/machine").status_code == 401
