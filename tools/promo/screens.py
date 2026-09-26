"""Site screenshots from the REAL app (no agent needed): starts a server on a fresh workspace, builds a few real parts,
saves them to the part library, and captures
  params-measure.jpg  — viewer with the Parameters card and a live measurement (landing page, ribbon section)
  library-tab.jpg     — the Library tab with real parts and thumbnails (landing page)
Usage: screens.py <out-dir>"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
out = Path(sys.argv[1]).resolve(); out.mkdir(parents=True, exist_ok=True)
ws = out / "screens-workspace"; shutil.rmtree(ws, ignore_errors=True); ws.mkdir()

PARTS = [
    ("Spur gear m2 z20", "20 teeth, module 2, 8 mm thick, 6 mm bore", "gear, transmission",
     "module_m = 2\nteeth = 20\nthickness = 8\nbore_d = 6\nresult = {'Gear': spur_gear(module_m, teeth, thickness, bore=bore_d)}\n"),
    ("L bracket 50", "Angle bracket with two mounting holes per leg", "bracket, sheet",
     "leg, width, t, r = 50, 30, 4, 3\nwith BuildPart() as bp:\n    with BuildSketch(Plane.XZ):\n        with BuildLine():\n            Polyline((0, 0), (leg, 0), (leg, t), (t, t), (t, leg), (0, leg), close=True)\n        make_face()\n    extrude(amount=width / 2, both=True)\n"
     "    fillet(bp.edges().filter_by(Axis.Y).sort_by_distance((t, 0, t))[0], r)\nb = bp.part\nfor x in (18, 38):\n    b = hole(b, 5.5, at=(x, 0, t), through=True)\n    b = hole(b, 5.5, at=(0, 0, x), through=True, axis=(1, 0, 0))\nresult = {'Bracket': b}\n"),
    ("M6 hex standoff 25", "Hex spacer, 10 mm across flats, tapped M6 both ends", "fastener, spacer",
     "af, length = 10, 25\ns = extrude(RegularPolygon(af / 2 / math.cos(math.pi / 6), 6), length)\ns = tap(s, 'M6', at=(0, 0, length), depth=10)\ns = tap(s, 'M6', at=(0, 0, 0), depth=10, axis=(0, 0, 1))\nresult = {'Standoff': s}\n"),
    ("Control knob 30", "Ø30 knob with a 6 mm D-shaft bore and a finger grip", "knob, handle",
     "knob_d, knob_h, shaft_d = 30, 18, 6\nwith BuildPart() as k:\n    Cylinder(knob_d / 2, knob_h, align=(Align.CENTER, Align.CENTER, Align.MIN))\n    fillet(k.edges().group_by(Axis.Z)[-1], 3)\n"
     "    with PolarLocations(knob_d / 2 + 1.2, 18):\n        Cylinder(2, knob_h * 3, mode=Mode.SUBTRACT)\n    Cylinder(shaft_d / 2, 12, align=(Align.CENTER, Align.CENTER, Align.MIN), mode=Mode.SUBTRACT)\nresult = {'Knob': k.part}\n"),
    ("NEMA 17 motor plate", "42 mm stepper mount, 31 mm hole square, Ø22 pilot", "motor, mount, plate",
     "size, t = 56, 5\np = Box(size, size, t)\np = hole(p, 22.5, at=(0, 0, t / 2), through=True)\nfor x in (-15.5, 15.5):\n    for y in (-15.5, 15.5):\n        p = hole(p, 3.4, at=(x, y, t / 2), through=True, counterbore=(6.5, 3))\nresult = {'Motor plate': p.fillet(4, p.edges().filter_by(Axis.Z))}\n"),
]
SHOW = ("plate_l, plate_w, plate_t = 80, 50, 8\nboss_d, boss_h, bore_d = 26, 18, 10\nhole_d, inset = 5.5, 9\n"
        "with BuildPart() as bp:\n    Box(plate_l, plate_w, plate_t)\n    with Locations((0, 0, plate_t / 2)):\n        Cylinder(boss_d / 2, boss_h, align=(Align.CENTER, Align.CENTER, Align.MIN))\n"
        "    Hole(bore_d / 2)\n    with Locations(*[(sx * (plate_l / 2 - inset), sy * (plate_w / 2 - inset), plate_t / 2) for sx in (-1, 1) for sy in (-1, 1)]):\n        Hole(hole_d / 2)\n"
        "    fillet(bp.edges().filter_by(Axis.Z).group_by(Axis.X)[0] + bp.edges().filter_by(Axis.Z).group_by(Axis.X)[-1], 4)\n"
        "pin = Pos(28, 0, plate_t / 2) * Cylinder(4, 30, align=(Align.CENTER, Align.CENTER, Align.MIN))\nresult = {'Plate': bp.part, 'Pin': pin}\n")

with socket.socket() as s:
    s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
env = {**os.environ, "AGENTICCAD_WORKSPACE": str(ws), "AGENTICCAD_NO_AGENT": "1", "PORT": str(port), "MPLBACKEND": "Agg"}
server = subprocess.Popen([str(ROOT / ".venv/bin/python"), "server.py"], cwd=ROOT, env=env, stdout=(out / "screens-server.log").open("w"), stderr=subprocess.STDOUT)
try:
    for _ in range(120):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/version", timeout=1); break
        except Exception:
            time.sleep(0.5)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True, args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        page = browser.new_page(viewport={"width": 1600, "height": 960}, device_scale_factor=1)
        page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle"); page.wait_for_timeout(2500)

        def build(code):
            before = page.evaluate("document.getElementById('stats').textContent")
            page.evaluate("c => window.agenticcad.send({type: 'run_code', code: c})", code)
            page.wait_for_function("b => document.getElementById('stats').textContent !== b", arg=before, timeout=60_000)
            page.wait_for_timeout(1200)

        # ---- library: build each part and save it through the real "+ Design" flow (thumbnail rendered by the app)
        page.click(".tab[data-pane='library']")
        for name, desc, tags, code in PARTS:
            build(code)
            page.click("#lib-add"); page.wait_for_selector("#la-name")
            page.fill("#la-name", name); page.fill("#la-desc", desc); page.fill("#la-tags", tags)
            page.click("#modal .row .btn.primary")
            page.wait_for_function("n => document.getElementById('lib-list').textContent.includes(n)", arg=name, timeout=30_000)
            page.wait_for_timeout(500)
        build(PARTS[0][3])                                           # leave the gear in the viewer
        page.evaluate("document.getElementById('fit').click()"); page.wait_for_timeout(1200)
        page.screenshot(path=str(out / "library-tab.png"))

        # ---- parameters card + measure panel on a two-body design
        build(SHOW)
        page.click(".tab[data-pane='chat']")
        page.evaluate("document.getElementById('home').click()"); page.wait_for_timeout(1400)
        page.keyboard.press("i"); page.wait_for_timeout(600)

        def face_xy(pred, off=(0, 0, 0)):
            return page.evaluate("""([pred, off]) => {
                const A = window.agenticcad, cam = A.camera, cv = document.querySelector('#viewer > canvas'), r = cv.getBoundingClientRect();
                const f = [...A.model.faces.values()].find(new Function('f', 'return (' + pred + ')(f)'));
                const V = cam.position.constructor; const v = new V(f.center[0] + off[0], f.center[1] + off[1], f.center[2] + off[2]).project(cam);
                return { x: r.left + (v.x + 1) / 2 * r.width, y: r.top + (1 - v.y) / 2 * r.height };
            }""", [pred, list(off)])
        a = face_xy("f => f.kind === 'PLANE' && f.normal && f.normal[2] > 0.99 && Math.abs(f.center[2] - 4) < 0.1", (-22, -12, 0))
        b = face_xy("f => f.kind === 'PLANE' && f.normal && f.normal[2] > 0.99 && f.center[2] > 20 && f.center[2] < 23", (8, 0, 0))
        for pt in (a, b):
            page.mouse.move(pt["x"], pt["y"]); page.wait_for_timeout(400); page.mouse.click(pt["x"], pt["y"]); page.wait_for_timeout(900)
        page.mouse.move(1180, 820); page.wait_for_timeout(1200)
        page.screenshot(path=str(out / "params-measure.png"))
        browser.close()
finally:
    server.terminate()

from PIL import Image  # noqa: E402
for n in ("library-tab", "params-measure"):
    Image.open(out / f"{n}.png").convert("RGB").save(out / f"{n}.jpg", quality=86, optimize=True)
    print(out / f"{n}.jpg", (out / f"{n}.jpg").stat().st_size // 1024, "KB")
