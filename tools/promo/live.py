"""Record the REAL app with the REAL agent: starts a server on a fresh workspace, drives the UI in Chrome via
Playwright (typing, sending, clicking faces, ribbon tools) while recording video, and logs event timestamps so
edit.py can speed up only the waiting. Usage: live.py <out-dir>"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
ws = out / "live-workspace"; shutil.rmtree(ws, ignore_errors=True); ws.mkdir()
vid_dir = out / "raw-live"; shutil.rmtree(vid_dir, ignore_errors=True); vid_dir.mkdir()

with socket.socket() as s:
    s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
env = {**os.environ, "AGENTICCAD_WORKSPACE": str(ws), "PORT": str(port), "MPLBACKEND": "Agg"}
server = subprocess.Popen([str(ROOT / ".venv/bin/python"), "server.py"], cwd=ROOT, env=env, stdout=(out / "live-server.log").open("w"), stderr=subprocess.STDOUT)
try:
    import urllib.request
    for _ in range(120):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/version", timeout=1); break
        except Exception:
            time.sleep(0.5)
    events: list[dict] = []
    T0 = None

    def mark(name, **kw):
        events.append({"name": name, "t": round(time.time() - T0, 3), **kw}); print(f"{events[-1]['t']:7.2f}s  {name}", flush=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True, args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        ctx = browser.new_context(viewport={"width": 1920, "height": 1080}, device_scale_factor=1,
                                  record_video_dir=str(vid_dir), record_video_size={"width": 1920, "height": 1080})
        page = ctx.new_page(); T0 = time.time()
        page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
        page.wait_for_function("document.getElementById('status-text').textContent.includes('ready')", timeout=90_000)
        page.wait_for_timeout(1500); mark("start")

        def wait_agent(label, timeout=180_000):
            mark(f"wait:{label}")
            page.wait_for_function("document.getElementById('status').className === 'busy'", timeout=15_000)
            page.wait_for_function("document.getElementById('status').className !== 'busy'", timeout=timeout)
            page.wait_for_timeout(600)
            mark(f"done:{label}")

        def say(text, label):
            page.click("#input"); page.wait_for_timeout(300)
            page.keyboard.type(text, delay=22)
            page.wait_for_timeout(500); mark(f"send:{label}"); page.keyboard.press("Enter")
            wait_agent(label)

        def face_screen_xy(pred_js, offset=(0.0, 0.0, 0.0)):
            """Screen coords of a point on the face matching a JS predicate: face centre + offset (model mm).
            The offset matters for annular faces such as a bored boss top, whose centre is in the hole."""
            return page.evaluate("""([pred, off]) => {
                const A = window.agenticcad, cam = A.camera, cv = document.querySelector('#viewer > canvas'), r = cv.getBoundingClientRect();
                const f = [...A.model.faces.values()].find(new Function('f', 'return (' + pred + ')(f)'));
                if (!f) return null;
                const V = cam.position.constructor; const v = new V(f.center[0] + off[0], f.center[1] + off[1], f.center[2] + off[2]).project(cam);
                return { x: r.left + (v.x + 1) / 2 * r.width, y: r.top + (1 - v.y) / 2 * r.height, id: f.id, label: f.label };
            }""", [pred_js, list(offset)])

        def stats():
            return page.evaluate("document.getElementById('stats').textContent")

        def wait_rebuild(before, timeout=30_000):
            page.wait_for_function("b => document.getElementById('stats').textContent !== b", arg=before, timeout=timeout)
            page.wait_for_timeout(900)

        def orbit(dx, dy, steps=40, ms=900):
            cv = page.query_selector("#viewer > canvas"); b = cv.bounding_box()
            x0, y0 = b["x"] + b["width"] * 0.62, b["y"] + b["height"] * 0.72
            page.mouse.move(x0, y0); page.mouse.down()
            for i in range(1, steps + 1):
                page.mouse.move(x0 + dx * i / steps, y0 + dy * i / steps); page.wait_for_timeout(ms / steps)
            page.mouse.up(); page.wait_for_timeout(300)

        # 1 · describe
        say("Make a 60 × 40 × 8 mm mounting plate with four Ø5 holes 8 mm in from each corner, and a Ø20 boss 15 mm tall in the middle with a Ø8 through bore. Call it Plate.", "plate")
        page.wait_for_timeout(800); mark("orbit"); orbit(-260, 40)
        cv = page.query_selector("#viewer > canvas").bounding_box(); page.mouse.move(cv["x"] + cv["width"] * 0.5, cv["y"] + cv["height"] * 0.5)
        for _ in range(6): page.mouse.wheel(0, -120); page.wait_for_timeout(90)          # ease in a little
        page.wait_for_timeout(600)

        # 2 · point at the boss top and ask
        top = face_screen_xy("f => f.kind === 'PLANE' && f.normal && f.normal[2] > 0.99 && f.center[2] > 15", (7, 0, 0))
        mark("click_face", **(top or {}))
        page.mouse.click(top["x"], top["y"]); page.wait_for_timeout(900)
        say("Put a 1 mm chamfer on the outer edge of this face.", "chamfer")
        page.wait_for_timeout(700)

        # 3 · ribbon: press/pull the boss top 5 mm, then fillet its edges
        page.keyboard.press("Escape"); page.evaluate("document.activeElement && document.activeElement.blur()")
        mark("ribbon_pull"); page.keyboard.press("q"); page.wait_for_timeout(700)
        top = face_screen_xy("f => f.kind === 'PLANE' && f.normal && f.normal[2] > 0.99 && f.center[2] > 15", (7, 0, 0))
        page.mouse.click(top["x"], top["y"]); page.wait_for_timeout(900)
        before = stats()
        page.keyboard.press("Meta+a"); page.keyboard.type("5", delay=120); page.wait_for_timeout(500); page.keyboard.press("Enter")
        wait_rebuild(before); page.wait_for_timeout(800); mark("pulled")
        page.evaluate("document.activeElement && document.activeElement.blur()")
        mark("ribbon_fillet"); page.keyboard.press("f"); page.wait_for_timeout(700)
        top = face_screen_xy("f => f.kind === 'PLANE' && f.normal && f.normal[2] > 0.99 && f.center[2] > 20", (7, 0, 0))
        page.mouse.click(top["x"], top["y"]); page.wait_for_timeout(800)
        before = stats()
        page.click("#cmd input[type=number]"); page.keyboard.press("Meta+a"); page.keyboard.type("2", delay=120); page.wait_for_timeout(400); page.keyboard.press("Enter")
        wait_rebuild(before); page.wait_for_timeout(800); mark("filleted")
        page.keyboard.press("Escape"); page.wait_for_timeout(400)                       # close the tool so no dialog lingers
        page.evaluate("document.activeElement && document.activeElement.blur()")
        orbit(180, -30, ms=1100); page.wait_for_timeout(400)

        # 5 · drawings + export
        page.click(".tab[data-pane='chat']"); page.wait_for_timeout(500)
        say("Add a second body: a Ø8 × 30 mm pin named Pin standing on the plate next to the boss at (22, 0). Then make shop drawings in aluminium 6061 and export a STEP.", "drawings")
        page.click(".tab[data-pane='design']"); page.wait_for_timeout(2500); mark("design_tab")
        page.wait_for_timeout(1200); mark("end")
        page.screenshot(path=str(out / "live-last.png"))
        ctx.close(); browser.close()
    webm = next(vid_dir.glob("*.webm"))
    (out / "live-events.json").write_text(json.dumps({"video": str(webm), "events": events}, indent=1))
    drawings = list((ws / "drawings").rglob("*Plate*.svg")) or list((ws / "drawings").rglob("*.svg"))
    if drawings:
        shutil.copy(drawings[0], out / "live-drawing.svg"); print("drawing:", drawings[0].name)
    print(f"recorded {webm} ({webm.stat().st_size // 1024} KB), {len(events)} events, {events[-1]['t']:.0f}s")
finally:
    server.terminate()
