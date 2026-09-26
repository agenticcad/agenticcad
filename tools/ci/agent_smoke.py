"""End-to-end agent test against an INSTALLED AgenticCAD: give the app an API key through its Settings API (the same
path as a user pasting one), wait for the Claude session, send one real design request over the WebSocket, and check
the agent built it. The key is read from ANTHROPIC_API_KEY, sent only to the app on localhost, and never printed.
Usage: agent_smoke.py <base-url>"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

import websocket   # websocket-client

base = sys.argv[1].rstrip("/")
key = os.environ.get("ANTHROPIC_API_KEY", "")
if not key:
    print("SKIP no ANTHROPIC_API_KEY (forks and runs without the secret skip the agent test)"); sys.exit(0)


def call(path, payload=None, timeout=120):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(base + path, data=data, headers={"content-type": "application/json"}, method="GET" if data is None else "POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def ok(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        sys.exit(1)


cli = call("/api/agent/cli")
ok(cli["found"], f"Claude Code found at {cli['path']}")
r = call("/api/settings", {"settings": {"api_key": key}, "restart": True}, timeout=180)
ok(r["settings"].get("api_key_set") is True and "api_key" not in r["settings"], "API key accepted and never echoed back")
st = {}
for _ in range(60):
    st = call("/api/agent/status")
    if st.get("connected") and not st.get("auth_required"):
        break
    time.sleep(2)
ok(st.get("connected") and not st.get("auth_required"), f"agent connected (model {st.get('model')})")

PROMPT = "Make a 40 x 20 x 10 mm block named Block with a 6 mm hole straight through the middle. Keep the reply to one sentence."
ws = websocket.create_connection(base.replace("http", "ws") + "/ws", timeout=300)
ws.send(json.dumps({"type": "chat", "text": PROMPT}))
t0 = time.time(); tools, model, result, errors, answer = [], None, None, [], ""
while time.time() - t0 < 300:
    m = json.loads(ws.recv())
    t = m.get("type")
    if t == "tool_use":
        tools.append(m.get("name", "").replace("mcp__cad__", ""))
    elif t == "model":
        model = m
    elif t in ("error", "agent_auth_required", "agent_missing_cli"):
        errors.append(f"{t}: {m.get('text', '')[:200]}")
    elif t == "text":
        answer = m.get("text", "")
    elif t == "result":
        result = m; break
ws.close()
ok(not errors, f"no agent errors {errors}")
ok(result is not None, f"turn finished in {time.time() - t0:.0f}s · tools {tools} · ${(result or {}).get('cost') or 0:.3f}")
ok(model is not None, "model rebuilt by the agent")
bodies = {b["name"]: b for b in model["mesh"]["bodies"]}
ok("Block" in bodies, f"body named Block ({list(bodies)})")
b = bodies["Block"]
size = sorted(round(hi - lo, 1) for lo, hi in zip(b["bboxMin"], b["bboxMax"]))
ok(size == [10.0, 20.0, 40.0], f"block is 40 x 20 x 10 ({size})")
holes = [f for f in model["mesh"]["faces"] if f.get("kind") == "CYLINDER" and abs((f.get("radius") or 0) - 3) < 0.05]
ok(bool(holes), "Ø6 hole present")
print("agent answer:", answer.strip()[:200])
print("AGENT SMOKE TEST PASSED")
