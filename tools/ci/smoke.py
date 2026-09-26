"""Smoke-test an INSTALLED AgenticCAD against its local server: version, setup page without Claude Code, a real build
(plate + inch-threaded holes + spur gear) over the WebSocket, STEP/STL export and shop drawings.
Usage: smoke.py <base-url> <expected-version>. Exits non-zero on the first failure."""
from __future__ import annotations

import json
import sys
import time
import urllib.request

import websocket   # websocket-client

base, want = sys.argv[1].rstrip("/"), sys.argv[2].lstrip("v")


def get(path, raw=False):
    with urllib.request.urlopen(base + path, timeout=60) as r:
        body = r.read()
        return (r.status, body) if raw else json.loads(body)


def post(path, payload):
    req = urllib.request.Request(base + path, data=json.dumps(payload).encode(), headers={"content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


def ok(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        sys.exit(1)


v = get("/api/version")
ok(v["version"] == want, f"version {v['version']} == {want}")
cli = get("/api/agent/cli")
ok(cli["packaged"] is True, "running as the packaged app")
print("claude cli found:", cli["found"], "searched:", len(cli["searched"]), "places")
status, html = get("/setup", raw=True)
ok(status == 200 and b"Claude Code" in html, "setup page renders")
status, html = get("/", raw=True)
ok(status == 200 and b"settings-btn" in html, "main page renders")

CODE = '''plate_l, plate_w, plate_t = 3 * inch, 2 * inch, 0.25 * inch
plate = Box(plate_l, plate_w, plate_t)
plate = tap(plate, "1/4-20", at=(-inch, 0, plate_t / 2), through=True)
plate = tap(plate, "M6", at=(inch, 0, plate_t / 2), through=True)
gear = Pos(0, 0, plate_t / 2) * spur_gear(2, 20, 8, bore=6)
result = {"Plate": plate, "Gear": Pos(0, 60, 0) * gear}
'''
ws = websocket.create_connection(base.replace("http", "ws") + "/ws", timeout=180)
ws.send(json.dumps({"type": "run_code", "code": CODE}))
t0 = time.time(); model = None
while time.time() - t0 < 180:
    m = json.loads(ws.recv())
    if m["type"] in ("build_error", "error"):
        ok(False, f"build: {m.get('text', '')[:400]}")
    if m["type"] == "model" and m.get("source") == "user":
        model = m; break
ws.close()
ok(model is not None, f"build over WebSocket in {time.time() - t0:.1f}s")
names = sorted(b["name"] for b in model["mesh"]["bodies"])
ok(names == ["Gear", "Plate"], f"bodies {names}")
ok("1/4-20" in model["summary"] or "Threads" in model["summary"], "threads registered")

status, step = get("/api/export/step", raw=True)
ok(status == 200 and step.startswith(b"ISO-10303-21"), f"STEP export ({len(step) // 1024} KB)")
status, stl = get("/api/export/stl?tol=0.05&ang=0.2", raw=True)
ok(status == 200 and len(stl) > 10_000, f"STL export ({len(stl) // 1024} KB)")
d = post("/api/drawings", {"units": "in"})
ok(len(d["files"]) >= 2, f"shop drawings: {[f['svg'] for f in d['files']]}")
status, svg = get(f"/api/drawings/{d['design']}/{d['files'][0]['svg']}", raw=True)
ok(status == 200 and b"third angle" in svg and b"1/4-20 UNC" in svg, "drawing is dimensioned in inches with the UNC callout")
sl = get("/api/slicer")
print("slicer installed:", sl["installed"])
print("ALL SMOKE TESTS PASSED")
