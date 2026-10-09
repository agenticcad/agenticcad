"""Cut the v0.24 promo from a live2.py recording, plus the site's feature clips and stills.
Speeds up only the agent's waiting time, overlays captions rendered from the stage CSS, prepends the title and appends
the montage (with a frame from the take) and the end card, mixes the soundtrack; then cuts every `clip:<name>` span at
1× into a muted MP4 and grabs every `still:<name>` as a JPEG. Usage: edit2.py <site-url> <promo-dir> [--skip-hero]"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from imageio_ffmpeg import get_ffmpeg_exe
from playwright.sync_api import sync_playwright

site, P = sys.argv[1], Path(sys.argv[2])
SKIP_HERO = "--skip-hero" in sys.argv
FF = get_ffmpeg_exe()
ev = json.loads((P / "live-events.json").read_text())
events = {e["name"]: e["t"] for e in ev["events"]}
raw = Path(ev["video"])
W = P / "work"; W.mkdir(exist_ok=True)
SITE_DIR = Path("/Users/mike/projects/agenticcad-site")


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode:
        print(r.stderr[-3000:]); sys.exit(1)
    return r


# ---------------------------------------------------------------- stills: frames at the marks → site images
STILLS = {n[6:]: t for n, t in events.items() if n.startswith("still:")}
(P / "stills").mkdir(exist_ok=True)
for name, t in STILLS.items():
    png = P / "stills" / f"{name}.png"
    run([FF, "-y", "-ss", f"{t + 0.15:.3f}", "-i", str(raw), "-frames:v", "1", str(png)])
    from PIL import Image
    Image.open(png).convert("RGB").save(P / "stills" / f"{name}.jpg", quality=86, optimize=True)
    print("still", name, (P / "stills" / f"{name}.jpg").stat().st_size // 1024, "KB")

# ---------------------------------------------------------------- clips: 1× spans, muted, 1280 wide, looping on the site
CLIPS = sorted({n.split(":")[1] for n in events if n.startswith("clip:")})
(P / "clips").mkdir(exist_ok=True)
for name in CLIPS:
    a, b = events.get(f"clip:{name}:start"), events.get(f"clip:{name}:end")
    if a is None or b is None:
        print("clip", name, "incomplete"); continue
    # compress any agent/compute wait inside the clip to 2 s, keep the rest at 1×
    cuts, t = [], a
    for e in ev["events"]:
        if e["name"].startswith("wait:") and a <= e["t"] < b:
            d = events.get("done:" + e["name"][5:])
            if d is None or d > b: continue
            if e["t"] > t: cuts.append((t, e["t"], 1.0))
            cuts.append((e["t"], d, max(1.0, (d - e["t"]) / 2.0))); t = d
    cuts.append((t, b, 1.0))
    fc = []; segs = []
    for i, (s0, s1, k) in enumerate(cuts):
        fc.append(f"[0:v]trim=start={s0:.3f}:end={s1:.3f},setpts=(PTS-STARTPTS)/{k:.4f}[s{i}]"); segs.append(f"[s{i}]")
    fc.append("".join(segs) + f"concat=n={len(segs)}:v=1:a=0,fps=30,scale=1280:-2,format=yuv420p[v]")
    mp4 = P / "clips" / f"{name}.mp4"
    run([FF, "-y", "-i", str(raw), "-filter_complex", ";".join(fc), "-map", "[v]", "-an", "-c:v", "libx264", "-preset", "slow", "-crf", "23", "-movflags", "+faststart", str(mp4)])
    run([FF, "-y", "-ss", "0.5", "-i", str(mp4), "-frames:v", "1", "-q:v", "4", str(P / "clips" / f"{name}.jpg")])
    dur = sum((s1 - s0) / k for s0, s1, k in cuts)
    print(f"clip {name}: {dur:.1f}s {mp4.stat().st_size // 1024} KB")
if SKIP_HERO:
    sys.exit(0)

# ---------------------------------------------------------------- 1. speed map: waits compress to ~2.4 s, all else 1×
cuts: list[tuple[float, float, float]] = []
t = events["start"] - 0.4
for e in ev["events"]:
    if e["name"].startswith("wait:"):
        label = e["name"][5:]
        a, b = e["t"], events[f"done:{label}"]
        if a > t: cuts.append((t, a, 1.0))
        speed = max(1.0, (b - a) / (3.0 if label in ("open", "ext") else 2.4))
        cuts.append((a, b, speed)); t = b
cuts.append((t, events["end"] + 0.6, 1.0))
# pace the 1× stretches: drawing and orbits read fine a little faster than real time
PACE = [("clip:sketch:start", "clip:sketch:end", 1.9), ("clip:ext:start", "clip:ext:end", 1.3), ("clip:cam:start", "clip:cam:end", 1.25),
        ("done:open", "clip:section:start", 1.5), ("clip:section:start", "clip:section:end", 1.25), ("done:interf", "clip:motion:start", 1.3),
        ("clip:motion:start", "clip:motion:end", 1.3), ("clip:explode:start", "clip:explode:end", 1.25), ("clip:render:start", "clip:render:end", 1.2),
        ("clip:render:end", "end", 1.6)]
def _split(cuts, a, b, k):
    out = []
    for (s0, s1, k0) in cuts:
        if k0 != 1.0 or s1 <= a or s0 >= b: out.append((s0, s1, k0)); continue
        if s0 < a: out.append((s0, a, k0))
        out.append((max(s0, a), min(s1, b), k))
        if s1 > b: out.append((b, s1, k0))
    return out
for a_ev, b_ev, k in PACE:
    if a_ev in events and b_ev in events and events[b_ev] > events[a_ev]:
        cuts = _split(cuts, events[a_ev], events[b_ev], k)
cuts = [c for c in cuts if c[1] - c[0] > 1e-3]


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
    out = W / f"scene-{scene}"; shutil.rmtree(out, ignore_errors=True); out.mkdir()
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True, args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        ctx = b.new_context(viewport={"width": 1920, "height": 1080}, record_video_dir=str(out), record_video_size={"width": 1920, "height": 1080})
        pg = ctx.new_page(); pg.goto(f"{site}/promo/?rec=1&scene={scene}{extra}", wait_until="networkidle")
        pg.wait_for_function("window.__promoDone === true", timeout=60_000); secs = pg.evaluate("window.__promoSeconds")
        ctx.close(); b.close()
    webm = next(out.glob("*.webm")); (out / "len.txt").write_text(str(secs)); return webm


montage_img = P / "stills" / "render.jpg"
if montage_img.exists():
    shutil.copy(montage_img, SITE_DIR / "promo" / "live-render.jpg")
title = record_scene("title"); montage = record_scene("montage", "&img=live-render.jpg" if montage_img.exists() else ""); end = record_scene("end")

# ---------------------------------------------------------------- 3. caption overlays
CAPS = [("start", 0.6, "send:plate", "Describe <em>it.</em>", "Plain words in. An exact, parametric model out."),
        ("done:plate", 0.3, "click_face", "Real geometry, <em>live.</em>", "Every face is exact B-rep; the script is yours to keep."),
        ("click_face", 0.0, "done:chamfer", "Point <em>at it.</em>", "Click a face. The agent works on exactly that geometry."),
        ("sketch_start", 0.0, "sketch_done", "Sketch with <em>constraints.</em>", "Lines snap, constraints appear, dimensions drive. Drag and it holds."),
        ("ext", 0.0, "ext_applied", "It extends <em>itself.</em>", "Ask for a tool. The agent writes a panel, and it appears in the ribbon."),
        ("send:cam", -0.3, "open_gearhead", "Then <em>cut it.</em>", "CAM for your Makera Z1, simulated before you run it. Experimental."),
        ("open_gearhead", 0.0, "clip:section:start", "113 parts, <em>one prompt.</em>", "A planetary gearhead the agent built, screws and bearings included."),
        ("clip:section:start", 0.0, "clip:interf:end", "See <em>inside.</em>", "Section anywhere. Check every pair of parts for interference."),
        ("clip:motion:start", 0.0, "end", "Make it <em>move.</em>", "Joints and gear ratios from the script. Explode it. Render it.")]
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
        if a_ev not in events or b_ev not in events:
            print("caption skipped (missing mark):", big); continue
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
for png, _, e in overlays: inputs += ["-loop", "1", "-t", f"{min(e + 0.2, main_len):.2f}", "-i", str(png)]
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
run([FF, "-y", *inputs, "-filter_complex", ";".join(fc), "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", str(mp4)])
run([FF, "-y", "-ss", "3.6", "-i", str(mp4), "-frames:v", "1", str(P / "AgenticCAD-promo-thumbnail.png")])
poster_t = edited_time(events.get("still:hero", events["end"])) + t_len
run([FF, "-y", "-ss", f"{poster_t:.2f}", "-i", str(mp4), "-frames:v", "1", "-q:v", "3", str(P / "promo-poster.jpg")])
sq = P / "AgenticCAD-promo-square-1080.mp4"
run([FF, "-loglevel", "error", "-y", "-i", str(mp4), "-filter_complex", "[0:v]scale=1080:1080:force_original_aspect_ratio=increase,crop=1080:1080,boxblur=30:8,eq=brightness=-0.15[bg];[0:v]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]", "-map", "[v]", "-map", "0:a", "-c:v", "libx264", "-preset", "slow", "-crf", "19", "-c:a", "copy", "-movflags", "+faststart", str(sq)])
probe = subprocess.run([FF, "-i", str(mp4)], capture_output=True, text=True).stderr
print("\n".join(l.strip() for l in probe.splitlines() if "Duration" in l))
print(f"title {t_len:.1f}s + main {main_len:.1f}s + montage {m_len:.1f}s + end {e_len:.1f}s = {total:.1f}s → {mp4} ({mp4.stat().st_size // 1024} KB)")
