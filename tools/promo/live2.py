"""Record the REAL app with the REAL agent — the v0.24 storyboard. Starts a server on a fresh workspace (with the
planetary gearhead reference seeded as a saved design), drives the UI in a headed Chrome on the Mac GPU while recording,
and logs event timestamps: `wait:*`/`done:*` spans get compressed by edit2.py, `clip:<name>:start/end` spans are cut
out as the site's feature clips, and `still:<name>` marks become screenshots. Usage: live2.py <out-dir>"""
from __future__ import annotations

import json
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
out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
ws = out / "live-workspace"; shutil.rmtree(ws, ignore_errors=True); ws.mkdir()
(ws / "designs").mkdir()
shutil.copy(ROOT / "evals/reference/planetary_gearhead.py", ws / "designs" / "planetary_gearhead.py")
vid_dir = out / "raw-live"; shutil.rmtree(vid_dir, ignore_errors=True); vid_dir.mkdir()

with socket.socket() as s:
    s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
env = {**os.environ, "AGENTICCAD_WORKSPACE": str(ws), "PORT": str(port), "MPLBACKEND": "Agg"}
server = subprocess.Popen([str(ROOT / ".venv/bin/python"), "server.py"], cwd=ROOT, env=env, stdout=(out / "live-server.log").open("w"), stderr=subprocess.STDOUT)
try:
    for _ in range(120):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/version", timeout=1); break
        except Exception:
            time.sleep(0.5)
    machines = [p.stem for p in (ws / "machines").glob("*.json")]
    z1 = next((m for m in machines if "z1" in m.lower() or "makera" in m.lower()), None)
    print("machines:", machines, "→", z1 or "Generic 3018", flush=True)
    events: list[dict] = []
    T0 = None

    def mark(name, **kw):
        events.append({"name": name, "t": round(time.time() - T0, 3), **kw}); print(f"{events[-1]['t']:7.2f}s  {name}", flush=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=False, args=["--window-size=1920,1130", "--window-position=0,0", "--hide-scrollbars"])
        ctx = browser.new_context(viewport={"width": 1920, "height": 1080}, device_scale_factor=1,
                                  record_video_dir=str(vid_dir), record_video_size={"width": 1920, "height": 1080})
        page = ctx.new_page(); T0 = time.time()
        page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
        page.wait_for_function("document.getElementById('status-text').textContent.includes('ready')", timeout=90_000)
        page.wait_for_timeout(1500); mark("start")

        def blur(): page.evaluate("document.activeElement && document.activeElement.blur()")
        def stats(): return page.evaluate("document.getElementById('stats').textContent")

        def wait_agent(label, timeout=300_000):
            mark(f"wait:{label}")
            page.wait_for_function("document.getElementById('status').className === 'busy'", timeout=15_000)
            page.wait_for_function("document.getElementById('status').className !== 'busy'", timeout=timeout)
            page.wait_for_timeout(600); mark(f"done:{label}")

        def say(text, label):
            page.click("#input"); page.wait_for_timeout(300)
            page.keyboard.type(text, delay=20)
            page.wait_for_timeout(500); mark(f"send:{label}"); page.keyboard.press("Enter")
            wait_agent(label)

        def wait_rebuild(before, timeout=200_000):
            page.wait_for_function("b => document.getElementById('stats').textContent !== b", arg=before, timeout=timeout)
            page.wait_for_timeout(900)

        def world_xy(pt):
            """Screen coords of a model-space point."""
            return page.evaluate("""(pt) => { const A = window.agenticcad, cv = document.querySelector('#viewer > canvas'), r = cv.getBoundingClientRect();
                const V = A.camera.position.constructor; const v = new V(pt[0], pt[1], pt[2]).project(A.camera);
                return { x: r.left + (v.x + 1) / 2 * r.width, y: r.top + (1 - v.y) / 2 * r.height }; }""", list(pt))

        def face_xy(pred_js, offset=(0.0, 0.0, 0.0)):
            """Screen coords on the LARGEST face matching the predicate (centre + offset in model mm)."""
            return page.evaluate("""([pred, off]) => { const A = window.agenticcad, cv = document.querySelector('#viewer > canvas'), r = cv.getBoundingClientRect();
                const fs = [...A.model.faces.values()].filter(new Function('f', 'return (' + pred + ')(f)')).sort((a, b) => b.area - a.area); const f = fs[0]; if (!f) return null;
                const V = A.camera.position.constructor; const v = new V(f.center[0] + off[0], f.center[1] + off[1], f.center[2] + off[2]).project(A.camera);
                return { x: r.left + (v.x + 1) / 2 * r.width, y: r.top + (1 - v.y) / 2 * r.height, id: f.id, label: f.label }; }""", [pred_js, list(offset)])

        def sketch_xy(u, v):
            """Screen coords of sketch-plane coordinates (u, v) while a sketch is open."""
            return page.evaluate("""([u, v]) => { const A = window.agenticcad, sk = A.sketch, cv = document.querySelector('#viewer > canvas'), r = cv.getBoundingClientRect();
                const P = sk.plane, V = A.camera.position.constructor; const w = new V(...P.origin).addScaledVector(P.x, u).addScaledVector(P.y, v).project(A.camera);
                return { x: r.left + (w.x + 1) / 2 * r.width, y: r.top + (1 - w.y) / 2 * r.height }; }""", [u, v])

        def click_at(pt, settle=700):
            page.mouse.move(pt["x"], pt["y"]); page.wait_for_timeout(120); page.mouse.click(pt["x"], pt["y"]); page.wait_for_timeout(settle)

        def orbit(dx, dy, steps=40, ms=900, at=(0.62, 0.72)):
            b = page.query_selector("#viewer > canvas").bounding_box()
            x0, y0 = b["x"] + b["width"] * at[0], b["y"] + b["height"] * at[1]
            page.mouse.move(x0, y0); page.mouse.down()
            for i in range(1, steps + 1):
                page.mouse.move(x0 + dx * i / steps, y0 + dy * i / steps); page.wait_for_timeout(ms / steps)
            page.mouse.up(); page.wait_for_timeout(300)

        def slide(selector, frm, to, ms=3000, steps=60):
            """Animate a range input, firing input events like a drag."""
            for i in range(steps + 1):
                v = frm + (to - frm) * i / steps
                page.evaluate("([s, v]) => { const el = document.querySelector(s); el.value = v; el.dispatchEvent(new Event('input', { bubbles: true })); }", [selector, v])
                page.wait_for_timeout(ms / steps)
            page.evaluate("s => document.querySelector(s).dispatchEvent(new Event('change', { bubbles: true }))", selector)

        TOP = "f => f.kind === 'PLANE' && f.normal && f.normal[2] > 0.99 && f.area > 1000"           # the plate's top (largest +Z plane)
        BOSS_TOP = "f => f.kind === 'PLANE' && f.normal && f.normal[2] > 0.99 && f.area < 400 && f.area > 100"   # the boss top ring

        # ---------------------------------------------------------------- 1 · describe
        say("Make a 60 × 40 × 8 mm mounting plate with four Ø5 holes 8 mm in from each corner, and a Ø20 boss 15 mm tall in the middle with a Ø8 through bore. Call it Plate.", "plate")
        page.wait_for_timeout(600); mark("orbit"); orbit(-260, 40)
        cv = page.query_selector("#viewer > canvas").bounding_box(); page.mouse.move(cv["x"] + cv["width"] * 0.5, cv["y"] + cv["height"] * 0.5)
        for _ in range(2): page.mouse.wheel(0, -120); page.wait_for_timeout(90)
        page.wait_for_timeout(500); mark("still:plate")

        # ---------------------------------------------------------------- 2 · point at the boss top and ask
        top = face_xy(BOSS_TOP, (7, 0, 0)); mark("click_face", **(top or {}))
        click_at(top, 900)
        say("Put a 1 mm chamfer on the outer edge of this face.", "chamfer")
        page.wait_for_timeout(600)

        # ---------------------------------------------------------------- 3 · sketch with constraints on the plate top
        page.keyboard.press("Escape"); blur()
        page.evaluate("document.getElementById('fit').click()"); page.wait_for_timeout(1300)   # frame the whole plate before picking a face on it
        for _try in range(3):
            pt = face_xy(TOP, (0, -16, 0)); click_at(pt, 700)             # select the plate's top face → K sketches on it
            if page.evaluate("[...window.agenticcad.model.faces.values()].some(f => f.kind === 'PLANE' && document.getElementById('chips').textContent.includes('plane'))"):
                break
            orbit(60, 40, ms=600)
        page.wait_for_timeout(300)
        mark("clip:sketch:start"); mark("sketch_start")
        page.keyboard.press("k"); page.wait_for_function("!!window.agenticcad.sketch", timeout=10_000); page.wait_for_timeout(1400)
        for (u, v) in [(17, -4), (27, -4), (27, 4), (17, 4), (17, -4)]:     # right half of the plate: clear of the Parameters card    # a rectangle of lines: H/V constraints appear as you draw
            click_at(sketch_xy(u, v), 650)
        page.wait_for_timeout(900)
        page.keyboard.press("v"); page.wait_for_timeout(500)
        def seg_mid(k):
            """Midpoint (u, v) of profile segment k, read live from the editor (the solver moves points as dimensions land)."""
            return page.evaluate("""k => { const it = window.agenticcad.sketch.items[0]; const P = [it.start, ...it.segs.slice(0, -1).map(s => s.to)];
                const a = P[k], b = P[(k + 1) % P.length]; return [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]; }""", k)

        def dimension(k, value):
            for _try in range(3):
                u, v = seg_mid(k); click_at(sketch_xy(u, v), 600)
                if page.evaluate("(window.agenticcad.sketch.picks || []).length") == 1: break
                page.keyboard.press("Escape"); page.wait_for_timeout(300)
            n0 = page.evaluate("window.agenticcad.sketch.cons.length")
            page.keyboard.press("d"); page.wait_for_timeout(700); page.keyboard.type(value, delay=160); page.wait_for_timeout(500); page.keyboard.press("Enter")
            page.wait_for_timeout(1100)
            return page.evaluate("window.agenticcad.sketch.cons.length") > n0
        dimension(2, "12")                                                     # top edge → 12
        dimension(3, "10")                                                     # left edge → 10
        page.wait_for_timeout(1300); mark("still:sketch")
        # drag the far corner: the dimensions hold, the rest follows
        c_uv = page.evaluate("window.agenticcad.sketch.items[0].segs[1].to"); corner = sketch_xy(*c_uv); page.mouse.move(corner["x"], corner["y"]); page.wait_for_timeout(300); page.mouse.down()
        tgt = sketch_xy(c_uv[0] + 2, c_uv[1] + 3)
        for i in range(1, 31): page.mouse.move(corner["x"] + (tgt["x"] - corner["x"]) * i / 30, corner["y"] + (tgt["y"] - corner["y"]) * i / 30); page.wait_for_timeout(28)
        page.mouse.up(); page.wait_for_timeout(1200)
        page.click("#sk-finish"); page.wait_for_function("!window.agenticcad.sketch", timeout=60_000)      # a sketch adds no volume: wait for the editor to close, then the rebuild
        page.wait_for_function("[...document.querySelectorAll('.msg.system')].some(m => m.textContent.includes('sketch saved'))", timeout=120_000)
        page.wait_for_timeout(1200); mark("sketch_done"); page.wait_for_timeout(600); mark("clip:sketch:end")
        say("Cut that sketch 3 mm into the plate.", "cut")
        page.wait_for_timeout(500)

        # ---------------------------------------------------------------- 4 · the agent extends the app: a panel with a picker, a slider and Apply
        blur(); mark("ext")
        say("Write an extension with a panel called \"Round corners\": pick a body, choose a radius with a slider from 0.5 to 6 mm, show how many vertical edges it has, and an Apply button that fillets just the vertical edges of that body with that radius by editing the script. Save it and open the panel.", "ext")
        page.wait_for_function("document.querySelector('#ext-panels .xp') !== null", timeout=20_000); page.wait_for_timeout(1200)
        mark("clip:ext:start"); mark("still:ext")
        page.click("#ext-panels .pk"); page.wait_for_timeout(700)
        click_at(face_xy(TOP, (0, -16, 0)), 1200)                              # pick the plate
        sl = "#ext-panels input[type=range]"
        if page.query_selector(sl):
            v0 = float(page.evaluate("s => document.querySelector(s).value", sl)); slide(sl, v0, 3.0, ms=1600, steps=30); page.wait_for_timeout(900)
        before = stats(); nerr = page.evaluate("document.querySelectorAll('.msg.error').length")
        btn = page.query_selector("#ext-panels button.primary") or page.query_selector("#ext-panels button:has-text('Apply')")
        if btn:
            btn.click()
            page.wait_for_function("([b, n]) => document.getElementById('stats').textContent !== b || document.querySelectorAll('.msg.error').length > n", arg=[before, nerr], timeout=90_000)
            page.wait_for_timeout(900)
        page.wait_for_timeout(900); mark("ext_applied"); orbit(160, -20, ms=1200); mark("clip:ext:end")

        # ---------------------------------------------------------------- 5 · CAM on the Makera Z1, then simulate
        machine = "Makera Z1" if z1 else "Generic 3018"
        page.click("#input"); page.wait_for_timeout(300)
        page.keyboard.type(f"Make a CAM program for the {machine} in aluminium: face the top, drill the four holes, pocket the bore, and cut the outline with four tabs. Simulate it.", delay=20)
        page.wait_for_timeout(500); mark("send:cam"); page.keyboard.press("Enter"); wait_agent("cam", timeout=720_000)   # CAM + simulation is the agent's longest turn
        page.click(".tab[data-pane='cam']"); page.wait_for_timeout(1200)
        if page.evaluate("getComputedStyle(document.getElementById('cam-sim')).display === 'none'"):
            page.wait_for_timeout(1500)
        if page.evaluate("getComputedStyle(document.getElementById('cam-sim')).display !== 'none'"):
            mark("clip:cam:start")
            if page.evaluate("+document.getElementById('sim-range').max === 0"):
                mark("wait:sim"); page.click("#sim-run"); page.wait_for_function("+document.getElementById('sim-range').max > 0", timeout=240_000); page.wait_for_timeout(400); mark("done:sim")
            n = page.evaluate("+document.getElementById('sim-range').max")
            slide("#sim-range", 0, n, ms=6000, steps=90); page.wait_for_timeout(1500); mark("still:cam"); mark("clip:cam:end")
        page.click(".tab[data-pane='chat']"); page.wait_for_timeout(600)
        page.evaluate("document.querySelectorAll('#ext-panels .x').forEach(b => b.click())"); page.wait_for_timeout(400)   # tidy: close the extension panel

        # ---------------------------------------------------------------- 6 · open the 113-part gearhead the agent built from one prompt
        mark("open_gearhead")
        page.click("#filemenu > button")
        page.wait_for_timeout(400); page.click("[data-act='open']"); page.wait_for_selector("#modal .design"); page.wait_for_timeout(600)
        before = stats(); mark("wait:open"); page.click("#modal .design:has-text('planetary_gearhead')")
        page.wait_for_timeout(700)
        ok = page.query_selector("#modal button:has-text('OK')")                 # "Discard unsaved changes?" — the plate was never saved
        if ok: ok.click()
        wait_rebuild(before, timeout=400_000); mark("done:open"); page.wait_for_timeout(800)
        page.evaluate("document.getElementById('fit').click()"); page.wait_for_timeout(1200)
        mark("clip:gearhead:start"); orbit(-320, 60, ms=2200, steps=60); mark("still:gearhead"); page.wait_for_timeout(500)

        # section: a slow cut through the middle, front to back
        blur(); mark("clip:section:start"); page.keyboard.press("s"); page.wait_for_selector("#sr", timeout=10_000); page.wait_for_timeout(900)
        lo, hi = page.evaluate("[+document.getElementById('sr').min, +document.getElementById('sr').max]")
        slide("#sr", hi, (lo + hi) / 2, ms=3200, steps=64); page.wait_for_timeout(1200); mark("still:section")
        orbit(-120, 10, ms=1400); page.wait_for_timeout(600); mark("clip:section:end")
        page.keyboard.press("Escape"); page.wait_for_timeout(500)

        # interference: exact overlap check of every pair
        blur(); mark("clip:interf:start"); page.keyboard.press("j"); page.wait_for_timeout(900)
        run = page.query_selector("#cmd button.primary") or page.query_selector("#cmd button:has-text('Check')")
        if run:
            mark("wait:interf"); run.click()
            page.wait_for_function("!!document.querySelector('#cmd .iflist, #cmd .prompt.ok, #cmd .prompt.bad') && !document.querySelector('#cmd .prompt').textContent.includes('Check which')", timeout=400_000)
            page.wait_for_timeout(1200); mark("done:interf")
        page.wait_for_timeout(2200); mark("still:interf"); mark("clip:interf:end")
        page.keyboard.press("Escape"); page.wait_for_timeout(400)
        page.evaluate("const c = document.querySelector('#sec-chip b'); c && c.click()"); page.wait_for_timeout(800)     # section off

        # motion, explode, studio render
        page.click("#anim-btn"); page.wait_for_selector("#ap-play", timeout=10_000); page.wait_for_timeout(700)
        mark("clip:motion:start"); page.click("#ap-play"); page.wait_for_timeout(7000); mark("clip:motion:end"); page.click("#ap-play"); page.wait_for_timeout(500)
        mark("clip:explode:start"); slide("#ap-ex", 0, 1, ms=3200, steps=64); page.wait_for_timeout(1500); mark("still:explode")
        orbit(-150, 20, ms=1800, at=(0.45, 0.5)); page.wait_for_timeout(500); mark("clip:explode:end")
        mark("clip:render:start"); page.click("#ap-rd"); page.wait_for_timeout(2500); mark("still:render")
        orbit(-200, 30, ms=2600, steps=60, at=(0.45, 0.5)); page.wait_for_timeout(800); mark("clip:render:end")
        slide("#ap-ex", 1, 0, ms=2200, steps=50); page.wait_for_timeout(1200)
        page.click("#ap-play"); page.wait_for_timeout(4500); mark("still:hero")
        mark("clip:gearhead:end")
        page.wait_for_timeout(800); mark("end")
        page.screenshot(path=str(out / "live-last.png"))
        ctx.close(); browser.close()
    webm = next(vid_dir.glob("*.webm"))
    (out / "live-events.json").write_text(json.dumps({"video": str(webm), "events": events}, indent=1))
    drawings = list((ws / "drawings").rglob("*.svg"))
    if drawings:
        shutil.copy(drawings[0], out / "live-drawing.svg")
    for f in (ws / "extensions").glob("*.py"):
        shutil.copy(f, out / f"live-ext-{f.name}")
    print(f"recorded {webm} ({webm.stat().st_size // 1024} KB), {len(events)} events, {events[-1]['t']:.0f}s")
finally:
    server.terminate()
