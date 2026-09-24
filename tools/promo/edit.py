"""Cut the promo from the LIVE recording: speed up only the agent's waiting time, add cinematic captions,
prepend the title clip and append a montage (with the drawing the agent actually produced) and the end card,
then mix the soundtrack. Usage: edit.py <site-url> <promo-dir>"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from imageio_ffmpeg import get_ffmpeg_exe
from playwright.sync_api import sync_playwright

site, P = sys.argv[1], Path(sys.argv[2])
FF = get_ffmpeg_exe()
ev = json.loads((P / "live-events.json").read_text())
events = {e["name"]: e["t"] for e in ev["events"]}
raw = Path(ev["video"])
W = P / "work"; W.mkdir(exist_ok=True)

# ---------------------------------------------------------------- 1. speed map: waits compress to ~2.4 s, all else 1×
names = [e["name"] for e in ev["events"]]
cuts: list[tuple[float, float, float]] = []            # (start, end, speed)
t = events["start"] - 0.4
for e in ev["events"]:
    if e["name"].startswith("wait:"):
        label = e["name"][5:]
        a, b = e["t"], events[f"done:{label}"]
        if a > t: cuts.append((t, a, 1.0))
        speed = max(1.0, (b - a) / 2.4)
        cuts.append((a, b, speed)); t = b
cuts.append((t, events["end"] + 0.6, 1.0))

def edited_time(t_raw: float) -> float:
    out = 0.0
    for a, b, k in cuts:
        if t_raw >= b: out += (b - a) / k
        elif t_raw > a: out += (t_raw - a) / k; break
    return out

main_len = edited_time(events["end"] + 0.6)
print("cuts:", [(round(a, 1), round(b, 1), round(k, 1)) for a, b, k in cuts]); print(f"main footage {main_len:.1f}s")

# ---------------------------------------------------------------- 2. title / montage / end clips from the stage
def record_scene(scene: str, extra: str = "") -> Path:
    out = W / f"scene-{scene}"; 
    import shutil; shutil.rmtree(out, ignore_errors=True); out.mkdir()
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True, args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        ctx = b.new_context(viewport={"width": 1920, "height": 1080}, record_video_dir=str(out), record_video_size={"width": 1920, "height": 1080})
        pg = ctx.new_page(); pg.goto(f"{site}/promo/?rec=1&scene={scene}{extra}", wait_until="networkidle")
        pg.wait_for_function("window.__promoDone === true", timeout=60_000); secs = pg.evaluate("window.__promoSeconds")
        ctx.close(); b.close()
    webm = next(out.glob("*.webm")); (out / "len.txt").write_text(str(secs)); return webm

import shutil
if (P / "live-drawing.svg").exists():
    shutil.copy(P / "live-drawing.svg", Path("/Users/mike/projects/agenticcad-site/promo/live-drawing.svg"))
title = record_scene("title"); montage = record_scene("montage", "&img=live-drawing.svg" if (P / "live-drawing.svg").exists() else ""); end = record_scene("end")

# ---------------------------------------------------------------- 3. caption overlays (PNG with alpha, rendered from the stage's CSS)
CAPS = [("start", 0.6, "send:plate", "Describe <em>it.</em>", "Plain words in. An exact, parametric model out."),
        ("done:plate", 0.3, "click_face", "Real geometry, <em>live.</em>", "Every face is exact B-rep; the script is yours to keep."),
        ("click_face", 0.0, "done:chamfer", "Point <em>at it.</em>", "Click a face. The agent works on exactly that geometry."),
        ("ribbon_pull", 0.0, "filleted", "Tweak it <em>by hand.</em>", "Fusion-style tools. Every click is written into the script."),
        ("send:drawings", -0.5, "end", "Drawings and <em>exports.</em>", "Ask, or use the Design tab. Exact STEP, STL, shop drawings.")]
cap_html = """<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;800&display=swap" rel="stylesheet">
<style>body{margin:0;width:1920px;height:1080px;background:transparent;font-family:Inter,sans-serif;overflow:hidden}
.cap{position:absolute;left:70px;bottom:52px;display:flex;flex-direction:column;gap:4px;text-shadow:0 2px 18px rgba(0,0,0,.9),0 0 2px rgba(0,0,0,.9)}
.cap b{font-size:60px;font-weight:800;letter-spacing:-.02em;word-spacing:.08em;color:#fff;line-height:1.05;white-space:pre}.cap b em{font-style:normal;color:#ff8c42}
.cap span{font-size:24px;color:#dfe5ec}
.bar{position:absolute;left:0;right:0;bottom:0;height:190px;background:linear-gradient(180deg,rgba(11,14,19,0),rgba(11,14,19,.85))}</style>
<div class="bar"></div><div class="cap"><b>%s</b><span>%s</span></div>"""
overlays = []
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True); pg = b.new_page(viewport={"width": 1920, "height": 1080})
    for i, (a_ev, a_off, b_ev, big, small) in enumerate(CAPS):
        pg.set_content(cap_html % (big, small)); pg.wait_for_timeout(600)
        png = W / f"cap{i}.png"; pg.screenshot(path=str(png), omit_background=True)
        s, e = edited_time(events[a_ev]) + a_off, edited_time(events[b_ev]) - 0.2
        overlays.append((png, max(0, s), max(s + 1.5, e)))
    b.close()

# ---------------------------------------------------------------- 4. assemble
def clip_len(d: Path) -> float: return float((d.parent / "len.txt").read_text()) + 0.3
t_len, m_len, e_len = clip_len(title), clip_len(montage), clip_len(end)
total = t_len + main_len + m_len + e_len
inputs = ["-i", str(title), "-i", str(raw), "-i", str(montage), "-i", str(end), "-i", str(P / "promo-music.wav")]
for png, _, e in overlays: inputs += ["-loop", "1", "-t", f"{min(e + 0.2, main_len):.2f}", "-i", str(png)]   # never longer than the footage: overlay would freeze-extend it
fc = []
fc.append(f"[0:v]trim=0:{t_len:.2f},setpts=PTS-STARTPTS,fps=30,scale=1920:1080,format=yuv420p[tt]")
segs = []
for i, (a, b_, k) in enumerate(cuts):
    fc.append(f"[1:v]trim=start={a:.3f}:end={b_:.3f},setpts=(PTS-STARTPTS)/{k:.4f}[s{i}]"); segs.append(f"[s{i}]")
fc.append("".join(segs) + f"concat=n={len(segs)}:v=1:a=0,fps=30,scale=1920:1080,format=yuv420p[m0]")
cur = "m0"
for i, (png, s, e) in enumerate(overlays):
    idx = 5 + i
    fc.append(f"[{idx}:v]format=rgba,fade=t=in:st={s:.2f}:d=0.45:alpha=1,fade=t=out:st={max(s, e - 0.45):.2f}:d=0.45:alpha=1[c{i}]")
    fc.append(f"[{cur}][c{i}]overlay=0:0:eof_action=pass:enable='between(t,{s:.2f},{e:.2f})'[m{i + 1}]"); cur = f"m{i + 1}"
fc.append(f"[2:v]trim=0:{m_len:.2f},setpts=PTS-STARTPTS,fps=30,scale=1920:1080,format=yuv420p[mm]")
fc.append(f"[3:v]trim=0:{e_len:.2f},setpts=PTS-STARTPTS,fps=30,scale=1920:1080,format=yuv420p[ee]")
fc.append(f"[tt][{cur}][mm][ee]concat=n=4:v=1:a=0[vout]")
fc.append(f"[4:a]atrim=0:{total:.2f},afade=t=in:st=0:d=0.8,afade=t=out:st={total - 4:.2f}:d=4,volume=0.95[aout]")
mp4 = P / "AgenticCAD-promo-1080p.mp4"
cmd = [FF, "-y", *inputs, "-filter_complex", ";".join(fc), "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", str(mp4)]
r = subprocess.run(cmd, capture_output=True, text=True)
if r.returncode: print(r.stderr[-3000:]); sys.exit(1)
subprocess.run([FF, "-y", "-ss", "3.6", "-i", str(mp4), "-frames:v", "1", str(P / "AgenticCAD-promo-thumbnail.png")], check=True, capture_output=True)
sq = P / "AgenticCAD-promo-square-1080.mp4"
subprocess.run([FF, "-loglevel", "error", "-y", "-i", str(mp4), "-filter_complex", "[0:v]scale=1080:1080:force_original_aspect_ratio=increase,crop=1080:1080,boxblur=30:8,eq=brightness=-0.15[bg];[0:v]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]", "-map", "[v]", "-map", "0:a", "-c:v", "libx264", "-preset", "slow", "-crf", "19", "-c:a", "copy", "-movflags", "+faststart", str(sq)], check=True)
probe = subprocess.run([FF, "-i", str(mp4)], capture_output=True, text=True).stderr
print("\n".join(l.strip() for l in probe.splitlines() if "Duration" in l))
print(f"title {t_len:.1f}s + main {main_len:.1f}s + montage {m_len:.1f}s + end {e_len:.1f}s = {total:.1f}s → {mp4} ({mp4.stat().st_size // 1024} KB)")
