"""A fresh workspace starts as an EMPTY design (no demo geometry), and the ribbon/code can populate it."""
import os
import sys

from fastapi.testclient import TestClient

from tests.test_server import wait_for


def test_fresh_workspace_is_empty_and_usable(tmp_path):
    ws = tmp_path / "fresh"; ws.mkdir()
    os.environ["AGENTICCAD_WORKSPACE"] = str(ws)
    os.environ["AGENTICCAD_NO_AGENT"] = "1"
    for m in ("server", "agent"):
        sys.modules.pop(m, None)
    import server
    import cad_kernel as ck
    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as sock:
            first = sock.receive_json()
            assert first["type"] == "model" and first["mesh"]["bodies"] == [] and first["mesh"]["tree"] == []
            assert first["code"] == ck.NEW_DESIGN_CODE and "empty design" in first["summary"]
            while sock.receive_json()["type"] != "status":
                pass
            # an empty design still exports nothing (clean error) and takes a primitive from the ribbon
            assert client.get("/api/export/step").status_code == 400
            sock.send_json({"type": "op", "kind": "primitive", "shape": "box", "dims": {"x": 10, "y": 10, "z": 5}, "at": [0, 0, 0], "normal": [0, 0, 1], "body": None, "mode": "new"})
            m = wait_for(sock, "model")
            assert [b["name"] for b in m["mesh"]["bodies"]] == ["Box"]
            sock.send_json({"type": "run_code", "code": "result = None\n"})
            m = wait_for(sock, "model")
            assert m["mesh"]["bodies"] == []
        assert client.get("/api/designs").json()["current"] is None
