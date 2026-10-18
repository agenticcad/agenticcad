"""Record the REAL app with the REAL agent — the v1.0 storyboard (v2 layout). Starts a server on a fresh workspace
(the planetary gearhead seeded as a saved design, a tool library with packet photos copied in), a Z1 emulator for the
Machine workspace, drives the UI in Chrome while recording, and logs event timestamps: `wait:*`/`done:*` spans get
compressed by edit3.py, `clip:<name>:start/end` spans become the site's feature clips, `still:<name>` marks become
screenshots.

Usage: live3.py <out-dir> [--tools <workspace with tools.json + tool_photos>] [--scenes design,cam,tools,machine,gearhead]
       [--keep <workspace>]   (reuse a workspace from an earlier take: skips seeding; scenes that need the agent's
                               model/program then run on what is there)
       [--headless] [--shots]  (--shots: a PNG at every mark, for checking a take without cutting video)"""
from __future__ import annotations

import argparse
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
ap = argparse.ArgumentParser()
ap.add_argument("out"); ap.add_argument("--tools"); ap.add_argument("--keep")
ap.add_argument("--scenes", default="design,cam,tools,machine,gearhead")
ap.add_argument("--headless", action="store_true"); ap.add_argument("--shots", action="store_true")
A = ap.parse_args()
SCENES = A.scenes.split(",")
out = Path(A.out); out.mkdir(parents=True, exist_ok=True)
if A.keep:
    ws = Path(A.keep)
else:
    ws = out / "live-workspace"; shutil.rmtree(ws, ignore_errors=True); ws.mkdir()
    (ws / "designs").mkdir()
    shutil.copy(ROOT / "evals/reference/planetary_gearhead.py", ws / "designs" / "planetary_gearhead.py")
    if A.tools:
        shutil.copy(Path(A.tools) / "tools.json", ws / "tools.json")
        shutil.copytree(Path(A.tools) / "tool_photos", ws / "tool_photos")
vid_dir = out / "raw-live"; shutil.rmtree(vid_dir, ignore_errors=True); vid_dir.mkdir()
shots = out / "shots"
if A.shots:
    shutil.rmtree(shots, ignore_errors=True); shots.mkdir()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]


port, emu_port, emu_cam = free_port(), free_port(), free_port()
env = {**os.environ, "AGENTICCAD_WORKSPACE": str(ws), "PORT": str(port), "MPLBACKEND": "Agg"}
server = subprocess.Popen([str(ROOT / ".venv/bin/python"), "server.py"], cwd=ROOT, env=env, stdout=(out / "live-server.log").open("w"), stderr=subprocess.STDOUT)
emu = subprocess.Popen([str(ROOT / ".venv/bin/python"), "tools/z1_emu.py", "--port", str(emu_port), "--cam-port", str(emu_cam)], cwd=ROOT,
                       stdout=(out / "live-emu.log").open("w"), stderr=subprocess.STDOUT)
try:
    for _ in range(120):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/version", timeout=1); break
        except Exception:
            time.sleep(0.5)
    events: list[dict] = []
    T0 = None

    with sync_playwright() as p:
        args = ["--window-size=1920,1130", "--window-position=0,0", "--hide-scrollbars"]
        browser = p.chromium.launch(channel="chrome", headless=A.headless, args=args + (["--use-angle=metal", "--enable-gpu"] if A.headless else []))
        ctx = browser.new_context(viewport={"width": 1920, "height": 1080}, device_scale_factor=1,
                                  record_video_dir=str(vid_dir), record_video_size={"width": 1920, "height": 1080})
        page = ctx.new_page(); T0 = time.time()

        def mark(name, **kw):
            events.append({"name": name, "t": round(time.time() - T0, 3), **kw}); print(f"{events[-1]['t']:7.2f}s  {name}", flush=True)
            if A.shots and (name.startswith("still:") or name.startswith("clip:") or name.startswith("done:") or name.startswith("shot:")):
                page.screenshot(path=str(shots / f"{len(events):03d}-{name.replace(':', '_')}.png"))

        page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
        page.wait_for_function("document.getElementById('status-text').textContent.includes('ready')", timeout=90_000)
        page.wait_for_timeout(1500); mark("start")

        def js(code, arg=None): return page.evaluate(code, arg)
        def blur(): js("document.activeElement && document.activeElement.blur()")
        def stats(): return js("document.getElementById('stats').textContent")
        def ws_(name): page.click(f"#ws [data-rtab={name}]"); page.wait_for_timeout(900)
        def right(pane): page.click(f".tab[data-pane={pane}]"); page.wait_for_timeout(400)

        def wait_agent(label, timeout=300_000):
            mark(f"wait:{label}")
            page.wait_for_function("document.getElementById('status').className === 'busy'", timeout=15_000)
            page.wait_for_function("document.getElementById('status').className !== 'busy'", timeout=timeout)
            page.wait_for_timeout(600); mark(f"done:{label}")

        def say(text, label, timeout=300_000):
            right("chat"); page.click("#input"); page.wait_for_timeout(300)
            page.keyboard.type(text, delay=18)
            page.wait_for_timeout(500); mark(f"send:{label}"); page.keyboard.press("Enter")
            wait_agent(label, timeout)

        def wait_rebuild(before, timeout=200_000):
            page.wait_for_function("b => document.getElementById('stats').textContent !== b", arg=before, timeout=timeout)
            page.wait_for_timeout(900)

        def face_xy(pred_js, offset=(0.0, 0.0, 0.0)):
            """Screen coords on the LARGEST face matching the predicate (centre + offset in model mm)."""
            return js("""([pred, off]) => { const A = window.agenticcad, cv = document.querySelector('#viewer > canvas'), r = cv.getBoundingClientRect();
                const fs = [...A.model.faces.values()].filter(new Function('f', 'return (' + pred + ')(f)')).sort((a, b) => b.area - a.area); const f = fs[0]; if (!f) return null;
                const V = A.camera.position.constructor; const v = new V(f.center[0] + off[0], f.center[1] + off[1], f.center[2] + off[2]).project(A.camera);
                return { x: r.left + (v.x + 1) / 2 * r.width, y: r.top + (1 - v.y) / 2 * r.height, id: f.id, label: f.label }; }""", [pred_js, list(offset)])

        def sketch_xy(u, v):
            return js("""([u, v]) => { const A = window.agenticcad, sk = A.sketch, cv = document.querySelector('#viewer > canvas'), r = cv.getBoundingClientRect();
                const P = sk.plane, V = A.camera.position.constructor; const w = new V(...P.origin).addScaledVector(P.x, u).addScaledVector(P.y, v).project(A.camera);
                return { x: r.left + (w.x + 1) / 2 * r.width, y: r.top + (1 - w.y) / 2 * r.height }; }""", [u, v])

        def click_at(pt, settle=700):
            page.mouse.move(pt["x"], pt["y"]); page.wait_for_timeout(120); page.mouse.click(pt["x"], pt["y"]); page.wait_for_timeout(settle)

        def orbit(dx, dy, steps=40, ms=900, at=(0.55, 0.62)):
            b = page.query_selector("#viewer > canvas").bounding_box()
            x0, y0 = b["x"] + b["width"] * at[0], b["y"] + b["height"] * at[1]
            page.mouse.move(x0, y0); page.mouse.down()
            for i in range(1, steps + 1):
                page.mouse.move(x0 + dx * i / steps, y0 + dy * i / steps); page.wait_for_timeout(ms / steps)
            page.mouse.up(); page.wait_for_timeout(300)

        def zoom(clicks, at=(0.5, 0.5)):
            b = page.query_selector("#viewer > canvas").bounding_box(); page.mouse.move(b["x"] + b["width"] * at[0], b["y"] + b["height"] * at[1])
            for _ in range(abs(clicks)): page.mouse.wheel(0, -120 if clicks > 0 else 120); page.wait_for_timeout(90)

        def slide(selector, frm, to, ms=3000, steps=60):
            for i in range(steps + 1):
                v = frm + (to - frm) * i / steps
                js("([s, v]) => { const el = document.querySelector(s); el.value = v; el.dispatchEvent(new Event('input', { bubbles: true })); }", [selector, v])
                page.wait_for_timeout(ms / steps)
            js("s => document.querySelector(s).dispatchEvent(new Event('change', { bubbles: true }))", selector)

        def modal_ok(text=None, timeout=15_000):
            """Click the primary button of the open app modal (optionally the one whose label contains `text`)."""
            page.wait_for_selector("#modal button", timeout=timeout); page.wait_for_timeout(500)
            sel = f"#modal button:has-text('{text}')" if text else "#modal button.pri, #modal button.primary"
            page.click(sel); page.wait_for_timeout(500)

        TOP = "f => f.kind === 'PLANE' && f.normal && f.normal[2] > 0.99 && f.area > 1000"
        BOSS_TOP = "f => f.kind === 'PLANE' && f.normal && f.normal[2] > 0.99 && f.area < 400 && f.area > 100"

        # ================================================================ DESIGN
        if "design" in SCENES:
            say("Make a 70 × 46 × 8 mm mounting plate with four Ø5 holes 7 mm in from each corner, a Ø20 boss 12 mm tall "
                "in the middle with a Ø8 through bore, and the word AGENTIC engraved 0.4 mm deep along the front edge in "
                "6 mm letters. Call it Plate.", "plate")
            page.wait_for_timeout(600); mark("orbit"); orbit(70, 12, ms=1400); zoom(-1)
            page.wait_for_timeout(500); mark("still:plate")

            top = face_xy(BOSS_TOP, (7, 0, 0)); mark("click_face", **(top or {}))
            click_at(top, 900)
            say("Put a 1 mm chamfer on the outer edge of this face.", "chamfer")
            page.wait_for_timeout(600)

            # sketch with constraints on the plate top, right of the boss
            page.keyboard.press("Escape"); blur()
            js("document.getElementById('fit').click()"); page.wait_for_timeout(1300)
            for _try in range(3):
                pt = face_xy(TOP, (20, 6, 0)); click_at(pt, 700)
                if js("document.getElementById('chips').textContent.includes('plane')"):
                    break
                orbit(60, 40, ms=600)
            page.wait_for_timeout(300)
            mark("clip:sketch:start"); mark("sketch_start")
            page.keyboard.press("k"); page.wait_for_function("!!window.agenticcad.sketch", timeout=10_000); page.wait_for_timeout(1400)
            for (u, v) in [(17, 2), (27, 2), (27, 10), (17, 10), (17, 2)]:
                click_at(sketch_xy(u, v), 650)
            page.wait_for_timeout(900)
            page.keyboard.press("v"); page.wait_for_timeout(500)

            def seg_mid(k):
                return js("""k => { const it = window.agenticcad.sketch.items[0]; const P = [it.start, ...it.segs.slice(0, -1).map(s => s.to)];
                    const a = P[k], b = P[(k + 1) % P.length]; return [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]; }""", k)

            def dimension(k, value):
                for _try in range(3):
                    u, v = seg_mid(k); click_at(sketch_xy(u, v), 600)
                    if js("(window.agenticcad.sketch.picks || []).length") == 1: break
                    page.keyboard.press("Escape"); page.wait_for_timeout(300)
                n0 = js("window.agenticcad.sketch.cons.length")
                page.keyboard.press("d"); page.wait_for_timeout(700); page.keyboard.type(value, delay=160); page.wait_for_timeout(500); page.keyboard.press("Enter")
                page.wait_for_timeout(1100)
                return js("window.agenticcad.sketch.cons.length") > n0
            dimension(2, "12"); dimension(3, "8")
            page.wait_for_timeout(1300); mark("still:sketch")
            c_uv = js("window.agenticcad.sketch.items[0].segs[1].to"); corner = sketch_xy(*c_uv); page.mouse.move(corner["x"], corner["y"]); page.wait_for_timeout(300); page.mouse.down()
            tgt = sketch_xy(c_uv[0] + 2, c_uv[1] + 2)
            for i in range(1, 31): page.mouse.move(corner["x"] + (tgt["x"] - corner["x"]) * i / 30, corner["y"] + (tgt["y"] - corner["y"]) * i / 30); page.wait_for_timeout(28)
            page.mouse.up(); page.wait_for_timeout(1200)
            page.click("#sk-finish"); page.wait_for_function("!window.agenticcad.sketch", timeout=60_000)
            page.wait_for_function("[...document.querySelectorAll('.msg.system')].some(m => m.textContent.includes('sketch saved'))", timeout=120_000)
            page.wait_for_timeout(1200); mark("sketch_done"); page.wait_for_timeout(600); mark("clip:sketch:end")
            say("Cut that sketch 2 mm into the plate.", "cut")
            page.wait_for_timeout(500)

            # the agent extends the app: a panel with a picker, a slider and Apply
            blur(); mark("ext")
            say("Write an extension with a panel called \"Round corners\": pick a body, choose a radius with a slider from 0.5 to 6 mm, "
                "show how many vertical edges it has, and an Apply button that fillets just the vertical edges of that body with that "
                "radius by editing the script. Save it and open the panel.", "ext")
            page.wait_for_function("document.querySelector('#ext-panels .xp') !== null", timeout=20_000); page.wait_for_timeout(1200)
            mark("clip:ext:start"); mark("still:ext")
            page.click("#ext-panels .pk"); page.wait_for_timeout(700)
            click_at(face_xy(TOP, (-24, 10, 0)), 1200)
            sl = "#ext-panels input[type=range]"
            if page.query_selector(sl):
                v0 = float(js("s => document.querySelector(s).value", sl)); slide(sl, v0, 4.0, ms=1600, steps=30); page.wait_for_timeout(900)
            before = stats(); nerr = js("document.querySelectorAll('.msg.error').length")
            btn = page.query_selector("#ext-panels button.primary") or page.query_selector("#ext-panels button:has-text('Apply')")
            if btn:
                btn.click()
                page.wait_for_function("([b, n]) => document.getElementById('stats').textContent !== b || document.querySelectorAll('.msg.error').length > n", arg=[before, nerr], timeout=90_000)
                page.wait_for_timeout(900)
            page.wait_for_timeout(900); mark("ext_applied"); orbit(160, -20, ms=1200); mark("clip:ext:end")
            js("document.querySelectorAll('#ext-panels .x').forEach(b => b.click())"); page.wait_for_timeout(400)

        # ================================================================ CAM
        if "cam" in SCENES:
            if "design" in SCENES:
                say("Make a CAM program for the Makera Z1 in 6061 aluminium. The stock is a 76 × 52 × 22 block in the L-bracket, "
                    "origin at its front-left top corner. Use the tools in my library: face the top, drill the four holes, pocket "
                    "the bore and the slot, V-carve the AGENTIC lettering with the 0.3 mm 30° engraving bit (0.3 mm max depth), chamfer "
                    "the plate's top edge 0.5 mm with the 90° chamfer bit, and cut the outline with four tabs. Simulate it.", "cam", timeout=900_000)
            ws_("cam"); page.wait_for_function("document.body.classList.contains('has-program')", timeout=60_000); page.wait_for_timeout(1500)
            mark("clip:cam:start"); mark("still:cam-tree")
            ops = page.query_selector_all("#cam-tree .ct-op")
            for o in ops[:: max(1, len(ops) // 4)][:4]:          # walk a few operations: the Inspector shows each one
                o.click(); page.wait_for_timeout(1300)
            vc = page.query_selector("#cam-tree .ct-op:has-text('V-carve'), #cam-tree .ct-op:has-text('carve')")
            if vc:
                vc.click(); page.wait_for_timeout(600); page.click("#cv-scope [data-v=sel]"); page.wait_for_timeout(900)
                js("document.getElementById('fit').click()"); page.wait_for_timeout(1200); zoom(4, (0.5, 0.62)); page.wait_for_timeout(900)
                mark("still:cam-vcarve")
                page.click("#cv-scope [data-v=all]"); page.wait_for_timeout(600); js("document.getElementById('fit').click()"); page.wait_for_timeout(900)
            if js("+document.getElementById('sim-range').max === 0 || !document.getElementById('cam-dock').classList.contains('simmed')"):
                mark("wait:sim"); page.click("#sim-run")
                page.wait_for_function("!document.getElementById('sim-run').disabled && !/…|Simulating/.test(document.getElementById('sim-run').textContent)", timeout=400_000)
                page.wait_for_timeout(600); mark("done:sim")
            slide("#sim-range", 0, 1000, ms=7000, steps=100); page.wait_for_timeout(1500); orbit(-140, 20, ms=1600); mark("still:cam"); mark("clip:cam:end")

            # Add operation without chat or code
            add = page.query_selector("#cam-tree .ct-add")
            if add:
                mark("clip:addop:start"); add.click(); page.wait_for_timeout(1200); mark("shot:addop-menu")
                mark("still:addop"); page.wait_for_timeout(1500)
                page.keyboard.press("Escape"); page.wait_for_timeout(400); mark("clip:addop:end")

        # ================================================================ TOOLS
        if "tools" in SCENES:
            page.click("#settings-btn"); page.wait_for_timeout(900)
            nav = page.query_selector("[data-sp=tools], .set-nav [data-pane=tools], #modal [data-tab=tools]")
            js("() => { const b = [...document.querySelectorAll('#modal button, #modal a, #modal div')].find(e => e.children.length === 0 && e.textContent.trim() === 'Tools'); b && b.click(); }")
            page.wait_for_timeout(1200); mark("clip:tools:start"); mark("shot:tools-list")
            items = page.query_selector_all("#modal [data-pane=tools] .tlist .ti, #modal [data-pane=tools] [data-tool]")
            for it in items[:6]:
                it.click(); page.wait_for_timeout(1400)
            vb = page.query_selector("#modal [data-pane=tools] :text('V-bit')")
            if vb:
                vb.click(); page.wait_for_timeout(1500)
            mark("still:tools"); page.wait_for_timeout(1200); mark("clip:tools:end")
            page.keyboard.press("Escape"); page.wait_for_timeout(600)

        # ================================================================ MACHINE (Z1 emulator; clips are cropped to the strip + Inspector)
        if "machine" in SCENES:
            ws_("machine"); right("inspect")
            page.fill("#mv-host", f"127.0.0.1:{emu_port}/{emu_cam}"); page.wait_for_timeout(400)
            mark("clip:machine:start"); page.click("#mv-connect")
            page.wait_for_function("window.MV && MV.st && MV.st.connected", timeout=30_000); page.wait_for_timeout(1500)
            js("document.getElementById('mv-cam-on').checked = false; document.getElementById('mv-cam-on').dispatchEvent(new Event('change'))")
            page.click("#ms-ctl"); page.wait_for_function("MV.st && MV.st.armed", timeout=10_000); page.wait_for_timeout(800)
            page.click("#mp-machine [data-act=home]"); page.wait_for_timeout(600)
            if page.query_selector("#modal button"): modal_ok()
            mark("wait:home"); page.wait_for_function("MV.st && MV.st.homed && MV.st.status && MV.st.status.state === 'Idle'", timeout=120_000); page.wait_for_timeout(800); mark("done:home")
            mark("still:machine-prepare")

            def tool_change(n):
                page.fill("#mp-tc-num", str(n)); page.click("#mp-tc-go"); page.wait_for_timeout(500)
                if page.query_selector("#modal button"): modal_ok()
                page.wait_for_selector("[data-act=confirm_tool]", timeout=60_000); page.wait_for_timeout(1800); mark(f"shot:fit-T{n}")
                page.click("[data-act=confirm_tool]")
                mark(f"wait:tc{n}"); page.wait_for_function(f"MV.st && MV.st.tool && MV.st.tool.number === {n} && !(MV.st.routine && ['running','awaiting'].includes(MV.st.routine.stage)) && MV.st.status.state === 'Idle'", timeout=180_000)
                page.wait_for_timeout(800); mark(f"done:tc{n}")
            tool_change(9999)

            # Zero: find the block with three probed corners
            page.click("[data-act=step][data-step=zero], .msteps [data-step=zero]"); page.wait_for_timeout(1200); mark("shot:zero")
            for k in ("obr", "obl", "ofr"):
                b = page.query_selector(f"#mz-find [data-act=pickc][data-k={k}]")
                if b: b.click(); page.wait_for_timeout(600)
                go = page.query_selector("#mstep-zero [data-act=probe3d], #mstep-zero button:has-text('Probe')")
                if not go: break
                go.click(); page.wait_for_timeout(500)
                if page.query_selector("#modal button"): modal_ok()
                mark(f"wait:probe-{k}")
                page.wait_for_function("MV.st && MV.st.routine && ['done','failed'].includes(MV.st.routine.stage) && MV.st.status.state === 'Idle'", timeout=240_000)
                page.wait_for_timeout(900); mark(f"done:probe-{k}")
            sx = page.query_selector("#mz-find [data-act=origin3]:not([disabled])")
            if sx:
                sx.click(); page.wait_for_timeout(500)
                if page.query_selector("#modal button"): modal_ok()
                page.wait_for_timeout(2500)
            mark("still:machine-zero")

            # Run: the first tool's block as an air cut
            page.click("[data-act=step][data-step=run]"); page.wait_for_timeout(1500); mark("shot:run-ready")
            first = js("(() => { const b = mvToolBlocks(program.ops.map((o, i) => ({...o, i})).filter(o => o.setup === MV.si))[0]; return b && b[0].tool.number; })()")
            if first is not None:
                tool_change(int(first)); page.click("[data-act=step][data-step=run]"); page.wait_for_timeout(1200)
            mark("still:machine-run-ready")
            page.click("#mvp-run"); page.wait_for_timeout(800)
            if page.query_selector("#modal button"): modal_ok()
            page.wait_for_function("MV.st && MV.st.job && ['running','done'].includes(MV.st.job.stage)", timeout=120_000)
            page.wait_for_timeout(9000); mark("still:machine-run")
            page.wait_for_timeout(5000); mark("clip:machine:end")
            page.click("#mstrip button:has-text('Stop program'), [data-act=jobstop]", timeout=5000) if page.query_selector("#mstrip button:has-text('Stop program')") else None
            page.wait_for_timeout(1500)
            ws_("design")

        # ================================================================ GEARHEAD (113 parts from one prompt)
        if "gearhead" in SCENES:
            if js("document.body.dataset.ws") != "design": ws_("design")
            mark("open_gearhead")
            page.click("#filemenu > button"); page.wait_for_timeout(400); page.click("[data-act='open']"); page.wait_for_selector("#modal .design"); page.wait_for_timeout(600)
            before = stats(); mark("wait:open"); page.click("#modal .design:has-text('planetary_gearhead')")
            page.wait_for_timeout(700)
            ok = page.query_selector("#modal button:has-text('OK'), #modal button:has-text('Discard')")
            if ok: ok.click()
            wait_rebuild(before, timeout=400_000); mark("done:open"); page.wait_for_timeout(800)
            js("document.getElementById('fit').click()"); page.wait_for_timeout(1200)
            mark("clip:gearhead:start"); orbit(-320, 60, ms=2200, steps=60); mark("still:gearhead"); page.wait_for_timeout(500)

            blur(); mark("clip:section:start"); page.keyboard.press("s"); page.wait_for_selector("#sr", timeout=10_000); page.wait_for_timeout(900)
            lo, hi = js("[+document.getElementById('sr').min, +document.getElementById('sr').max]")
            slide("#sr", hi, (lo + hi) / 2, ms=3200, steps=64); page.wait_for_timeout(1200); mark("still:section")
            orbit(-120, 10, ms=1400); page.wait_for_timeout(600); mark("clip:section:end")
            page.keyboard.press("Escape"); page.wait_for_timeout(500)

            blur(); mark("clip:interf:start"); page.keyboard.press("j"); page.wait_for_timeout(900)
            run = page.query_selector("#cmd button.primary") or page.query_selector("#cmd button:has-text('Check')")
            if run:
                mark("wait:interf"); run.click()
                page.wait_for_function("!!document.querySelector('#cmd .iflist, #cmd .prompt.ok, #cmd .prompt.bad') && !document.querySelector('#cmd .prompt').textContent.includes('Check which')", timeout=400_000)
                page.wait_for_timeout(1200); mark("done:interf")
            page.wait_for_timeout(2200); mark("still:interf"); mark("clip:interf:end")
            page.keyboard.press("Escape"); page.wait_for_timeout(400)
            js("const c = document.querySelector('#sec-chip b'); c && c.click()"); page.wait_for_timeout(800)

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
    (out / "live-events.json").write_text(json.dumps({"video": str(webm), "events": events, "workspace": str(ws)}, indent=1))
    for f in (ws / "extensions").glob("*.py"):
        shutil.copy(f, out / f"live-ext-{f.name}")
    print(f"recorded {webm} ({webm.stat().st_size // 1024} KB), {len(events)} events, {events[-1]['t']:.0f}s")
finally:
    server.terminate(); emu.terminate()
