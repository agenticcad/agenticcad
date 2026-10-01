"""Record the REAL app building one big assembly from a single prompt, in real time, with an on-screen stopwatch.
The prompt comes from an eval case (evals/cases.py), so what the video shows is what the eval grades.
Usage: showcase.py <out-dir> <case-id> [Body,Names,To,Hide]
  e.g. showcase.py ../agenticcad-admin/promo/showcase-planetary cad_showcase_planetary FrontCover,RingGear,Stator
The optional names are hidden one by one with the Browser's show/hide toggles after the first orbit, to open the
assembly up; a name matches a body whose name equals it or starts with it (HousingScrew matches HousingScrew1-4).
Writes <out-dir>/raw/*.webm, <out-dir>/events.json and the finished design's script."""
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
sys.path[:0] = [str(ROOT / "evals"), str(ROOT)]
from cases import CASES  # noqa: E402

out = Path(sys.argv[1]).resolve(); out.mkdir(parents=True, exist_ok=True)
case = next(c for c in CASES if c.id == sys.argv[2])
REVEAL = [x for x in (sys.argv[3].split(",") if len(sys.argv) > 3 else []) if x]
ws = out / "workspace"; shutil.rmtree(ws, ignore_errors=True); ws.mkdir()
vid = out / "raw"; shutil.rmtree(vid, ignore_errors=True); vid.mkdir()

with socket.socket() as s:
    s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
env = {**os.environ, "AGENTICCAD_WORKSPACE": str(ws), "PORT": str(port), "MPLBACKEND": "Agg"}
server = subprocess.Popen([str(ROOT / ".venv/bin/python"), "server.py"], cwd=ROOT, env=env,
                          stdout=(out / "server.log").open("w"), stderr=subprocess.STDOUT)

# Stopwatch + body counter, drawn by the page so it is part of the recording (real elapsed time, even if sped up later)
OVERLAY = """() => {
  const css = document.createElement('style');
  css.textContent = `#sc-clock{position:fixed;top:92px;left:calc((100vw - 380px) / 2 + 60px);transform:translateX(-50%);z-index:99999;display:flex;gap:18px;align-items:baseline;
    padding:10px 22px;border-radius:14px;background:rgba(15,18,24,.82);backdrop-filter:blur(6px);color:#fff;
    font:600 30px/1 ui-monospace,SFMono-Regular,Menlo,monospace;box-shadow:0 6px 24px rgba(0,0,0,.35);opacity:0;transition:opacity .4s}
    #sc-clock small{font:500 14px/1 -apple-system,system-ui,sans-serif;color:#9aa4b2;letter-spacing:.04em;text-transform:uppercase}
    #sc-clock.on{opacity:1} #sc-clock.done #sc-t{color:#5ee0a0}`;
  document.head.appendChild(css);
  const d = document.createElement('div'); d.id = 'sc-clock';
  d.innerHTML = '<small>agent time</small><span id="sc-t">0:00.0</span><small id="sc-b"></small>';
  document.body.appendChild(d);
  const vr = document.querySelector('#viewer > canvas').getBoundingClientRect(); d.style.left = (vr.left + vr.width / 2) + 'px';   // centred over the 3D view
  let t0 = 0, raf = 0;
  const fmt = ms => { const s = ms / 1000; return Math.floor(s / 60) + ':' + (s % 60).toFixed(1).padStart(4, '0'); };
  const bodies = () => { const n = (window.agenticcad && window.agenticcad.model && window.agenticcad.model.bodies) ? window.agenticcad.model.bodies.size : 0;
                         return n ? n + (n === 1 ? ' body' : ' bodies') : ''; };
  const tick = () => { document.getElementById('sc-t').textContent = fmt(performance.now() - t0);
                       document.getElementById('sc-b').textContent = bodies(); raf = requestAnimationFrame(tick); };
  window.scClock = {
    start() { t0 = performance.now(); d.classList.add('on'); d.classList.remove('done'); tick(); },
    stop() { cancelAnimationFrame(raf); document.getElementById('sc-t').textContent = fmt(performance.now() - t0);
             document.getElementById('sc-b').textContent = bodies(); d.classList.add('done'); return performance.now() - t0; },
  };
}"""

try:
    for _ in range(120):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/version", timeout=1); break
        except Exception:
            time.sleep(0.5)
    events: list[dict] = []
    T0 = 0.0

    def mark(name, **kw):
        events.append({"name": name, "t": round(time.time() - T0, 3), **kw}); print(f"{events[-1]['t']:7.2f}s  {name}", flush=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True, args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        ctx = browser.new_context(viewport={"width": 1920, "height": 1080}, device_scale_factor=1,
                                  record_video_dir=str(vid), record_video_size={"width": 1920, "height": 1080})
        page = ctx.new_page(); T0 = time.time()
        page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
        page.wait_for_function("document.getElementById('status-text').textContent.includes('ready')", timeout=90_000)
        page.evaluate(OVERLAY)
        page.wait_for_timeout(1200); mark("start")

        page.click("#input"); page.wait_for_timeout(300)
        page.keyboard.type(case.prompt, delay=4)                  # fast typist: the prompt is long, the build is the show
        page.wait_for_timeout(700)
        mark("send"); page.evaluate("window.scClock.start()"); page.keyboard.press("Enter")
        page.wait_for_function("document.getElementById('status').className === 'busy'", timeout=15_000)
        mark("wait:build")
        page.wait_for_function("document.getElementById('status').className !== 'busy'", timeout=600_000, polling=200)
        agent_ms = page.evaluate("window.scClock.stop()")
        mark("done:build", agent_s=round(agent_ms / 1000, 1),
             bodies=page.evaluate("window.agenticcad.model ? window.agenticcad.model.bodies.size : 0"))
        page.wait_for_timeout(1500)

        # the finished assembly: fit, then a slow orbit so every part is seen
        page.evaluate("document.activeElement && document.activeElement.blur()")
        page.evaluate("document.getElementById('home').click()"); page.wait_for_timeout(1200)
        cv = page.query_selector("#viewer > canvas").bounding_box()
        page.mouse.move(cv["x"] + cv["width"] * 0.5, cv["y"] + cv["height"] * 0.5)
        for _ in range(5): page.mouse.wheel(0, -120); page.wait_for_timeout(120)       # ease in: the assembly fills the frame
        page.wait_for_timeout(500)
        mark("orbit")
        x0, y0 = cv["x"] + cv["width"] * 0.55, cv["y"] + cv["height"] * 0.8
        page.mouse.move(x0, y0); page.mouse.down()
        steps = 240
        for i in range(1, steps + 1):                                                 # a slow half turn, rising a little
            page.mouse.move(x0 - 900 * i / steps, y0 + 50 * i / steps); page.wait_for_timeout(9000 / steps)
        page.mouse.up(); page.wait_for_timeout(1500)

        if REVEAL:                                                                    # open it up with the real show/hide toggles
            mark("reveal")
            for name in REVEAL:
                while True:
                    row = page.evaluate_handle("""n => [...document.querySelectorAll('#treebody .node')].find(r => {
                        const t = (r.querySelector('.nm') || {}).textContent || ''; return r.querySelector('.eye') && !r.classList.contains('hidden') && (t === n || (t.startsWith(n) && /^\\d+$/.test(t.slice(n.length)))); }) || null""", name)
                    if not row.as_element():
                        break
                    row.as_element().scroll_into_view_if_needed(); page.wait_for_timeout(120)
                    row.as_element().query_selector(".eye").click(); page.wait_for_timeout(260)
            page.wait_for_timeout(1200)
            page.mouse.move(x0, y0); page.mouse.down()
            for i in range(1, steps + 1):
                page.mouse.move(x0 - 900 + 700 * i / steps, y0 + 50 - 30 * i / steps); page.wait_for_timeout(8000 / steps)
            page.mouse.up(); page.wait_for_timeout(1500)
        mark("end")
        video_path = page.video.path()
        try:
            (out / "design.py").write_bytes(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/design/download", timeout=10).read())
        except Exception as ex:  # noqa: BLE001
            print("design download failed:", ex)
        mark("close"); ctx.close(); browser.close()                              # the video ends here: edit.py aligns on it
    (out / "events.json").write_text(json.dumps({"case": case.id, "prompt": case.prompt, "video": str(video_path), "events": events}, indent=1))
    print("video", video_path)
finally:
    server.terminate()
