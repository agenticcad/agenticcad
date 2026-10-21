"""Phone control: off by default, a separate listener whose every API call needs the key from the QR link, and the
desktop app's own endpoints are not reachable through it."""
import json
import os
import pytest

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
    assert c.get(f"/m/k/{cfg['token']}").status_code == 200              # the page with the key in its path (Home Screen apps)
    assert c.get("/m/k/whatever").status_code == 200                      # the page itself is public; the API is not
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



def _sub(n=1):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    import phone_push as pp
    ua = ec.generate_private_key(ec.SECP256R1())
    pub = ua.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {"endpoint": f"https://web.push.apple.com/sub{n}", "keys": {"p256dh": pp.b64u(pub), "auth": pp.b64u(b"\x02" * 16)}}


def test_phone_push_endpoints_subscribe_notify_and_prune(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTICCAD_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("AGENTICCAD_NO_AGENT", "1")
    monkeypatch.setenv("AGENTICCAD_NO_REMOTE", "1")
    import asyncio
    import importlib
    import server
    importlib.reload(server)
    key = server._remote_cfg()["token"]
    c = TestClient(server.phone)
    # public without the key: the CA certificate and the service worker; the manifest and push API need it
    r = c.get("/m/ca.crt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-x509-ca-cert") and r.content[:2] == b"\x30\x82"
    r = c.get("/m/sw.js")
    assert r.status_code == 200 and r.headers["service-worker-allowed"] == "/m" and b"showNotification" in r.content
    r = c.get("/m/api/ping")                                                # public, from any origin: the trust probe
    assert r.status_code == 200 and r.json()["ok"] is True and r.headers["access-control-allow-origin"] == "*"
    assert c.get("/m/manifest.webmanifest").status_code == 401
    m = c.get(f"/m/manifest.webmanifest?k={key}").json()
    assert m["start_url"] == f"/m/k/{key}" and m["scope"] == "/m" and m["display"] == "standalone"
    assert c.get("/m/api/push").status_code == 401
    info = c.get("/m/api/push", headers={"X-Key": key}).json()
    assert info["enabled"] is False and len(info["vapid"]) > 80 and info["https_url"].startswith("https://") and f":8791/m/k/{key}" in info["https_url"]
    assert info["secure"] is False and info["count"] == 0
    # subscribe (bad ones refused), the computer lists the phone
    assert c.post("/m/api/push/subscribe", headers={"X-Key": key}, json={"subscription": {"endpoint": "http://x"}}).status_code == 400
    assert c.post("/m/api/push/subscribe", headers={"X-Key": key}, json={"subscription": _sub(1), "label": "iPhone · Home Screen"}).json()["count"] == 1
    assert c.post("/m/api/push/subscribe", headers={"X-Key": key}, json={"subscription": _sub(2), "label": "Pixel · browser"}).json()["count"] == 2
    d = TestClient(server.app)
    st = d.get("/api/remote").json()
    assert [x["label"] for x in st["push"]["devices"]] == ["iPhone · Home Screen", "Pixel · browser"] and st["push"]["enabled"] is False
    assert st["https"]["port"] == 8791 and st["https"]["running"] is False and st["push"]["cert"] is None   # certs are made at start
    # delivery: a fake push service; off -> nothing; on -> encrypted posts with VAPID; 410 prunes the phone
    sent = []
    def fake_send(self, sub, payload, subject, ttl=3600, urgency="high", topic=None):
        sent.append((sub["endpoint"], payload, ttl, urgency, topic))
        return 410 if sub["endpoint"].endswith("sub2") else 201
    monkeypatch.setattr(server.phone_push.PushStore, "send", fake_send)
    asyncio.run(server._phone_notify("tool_change", {"tool": 3, "what": "T3", "msg": "fit T3", "nonce": "n0nce"}))
    assert sent == []
    assert d.post("/api/remote", json={"push": True}).json()["push"]["enabled"] is True
    res = asyncio.run(server._phone_notify("tool_change", {"tool": 3, "what": "T3", "msg": "fit T3, then press the front button", "nonce": "n0nce",
                                                             "program": "badge.nc", "op": "V-carve"}))
    assert res["sent"] == 1 and res["gone"] == 1 and len(sent) == 2
    ep, p, ttl, urg, topic = sent[0]
    assert p["title"] == "Tool change: fit T3" and p["body"].startswith("badge.nc · V-carve\n") and p["kind"] == "tool_change"
    assert p["url"] == "/m" and p["confirm_url"].endswith(":8791/m/api/confirm/n0nce") and ttl == 3600 and urg == "high" and topic == "tool_change"
    assert key not in json.dumps(p)                                       # the page's key never travels
    st = d.get("/api/remote").json()
    assert [x["label"] for x in st["push"]["devices"]] == ["iPhone · Home Screen"] and st["push"]["devices"][0]["last"]
    assert st["push"]["last"]["sent"] == 1
    # the test button from the phone targets that phone only; the computer's test goes to all
    r = c.post("/m/api/push/test", headers={"X-Key": key}, json={"endpoint": "https://web.push.apple.com/sub1"}).json()
    assert r["sent"] == 1 and sent[-1][1]["title"] == "AgenticCAD notifications work" and sent[-1][2] == 300
    assert d.post("/api/remote", json={"push_test": True}).json()["push"]["last"]["sent"] == 1
    # unsubscribe from the phone; forget all from the computer
    assert c.post("/m/api/push/unsubscribe", headers={"X-Key": key}, json={"endpoint": "https://web.push.apple.com/sub1"}).json()["count"] == 0
    c.post("/m/api/push/subscribe", headers={"X-Key": key}, json={"subscription": _sub(3)})
    assert d.post("/api/remote", json={"push_forget": True}).json()["push"]["devices"] == []
    r = asyncio.run(server._phone_notify("program_done", {"program": "x.nc", "msg": "finished"}))
    assert r["error"] == "no phone has notifications on" and r["sent"] == 0


def test_secure_phone_page_serves_with_the_apps_own_certificate(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTICCAD_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("AGENTICCAD_NO_AGENT", "1")
    monkeypatch.setenv("AGENTICCAD_NO_REMOTE", "1")
    import asyncio
    import importlib
    import socket
    import httpx
    import server
    importlib.reload(server)
    s_ = socket.socket(); s_.bind(("127.0.0.1", 0)); port = s_.getsockname()[1]; s_.close()
    if port % 2:
        port -= 1
    d = TestClient(server.app)
    key = server._remote_cfg()["token"]

    async def go():
        d.post("/api/remote", json={"port": port, "enabled": False})
        await server._remote_start()
        try:
            await asyncio.sleep(0.5)
            st = server._remote_status()
            assert st["https"]["running"] and st["https"]["error"] is None, st["https"]
            cert = st["push"]["cert"]
            assert "127.0.0.1" in cert["hosts"] and cert["hosts"][0].endswith(".local")
            ca = str(server._push().ca_crt)
            async with httpx.AsyncClient(verify=ca) as h:                   # trusting only our CA, as the phone will
                r = await h.get(f"https://127.0.0.1:{port + 1}/m")
                assert r.status_code == 200 and b"Notifications" in r.content
                r = await h.get(f"https://127.0.0.1:{port + 1}/m/api/push", headers={"X-Key": key})
                assert r.json()["secure"] is True
                r = await h.get(f"http://127.0.0.1:{port}/m/api/push", headers={"X-Key": key})
                assert r.json()["secure"] is False and r.json()["https_running"] is True
            async with httpx.AsyncClient(verify=False) as h:
                pass
            with pytest.raises(httpx.ConnectError):                         # not trusted by the system: our CA only
                async with httpx.AsyncClient() as h:
                    await h.get(f"https://127.0.0.1:{port + 1}/m")
            # the certificate survives a restart (same CA), the leaf is kept while its names still fit
            ca_pem = server._push().ca_crt.read_bytes(); leaf_pem = server._push().srv_crt.read_bytes()
            await server._remote_start()
            await asyncio.sleep(0.3)
            assert server._push().ca_crt.read_bytes() == ca_pem and server._push().srv_crt.read_bytes() == leaf_pem
        finally:
            await server._remote_stop()
    asyncio.run(go())
