"""Cut the showcase recording (showcase.py) into two videos, captions from the recorded numbers:
  <dir>/showcase-realtime.mp4   everything at 1× (the in-page clock and the video clock agree)
  <dir>/showcase-short.mp4      the agent's working time sped up to ~12 s, with a badge saying so; the in-page
                                clock still shows real elapsed time
Captions and the end card are HTML rendered to transparent PNGs by Playwright, overlaid with ffmpeg.
Usage: showcase_edit.py <dir> "<subject caption>" ["<caption for the opened-up view>"]     e.g. showcase_edit.py ../agenticcad-admin/promo/showcase-quad "a 5-inch FPV quadcopter"
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

from imageio_ffmpeg import get_ffmpeg_exe
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
D = Path(sys.argv[1]).resolve(); subject = sys.argv[2]
reveal_line = sys.argv[3] if len(sys.argv) > 3 else "every part is its own body"
FF = get_ffmpeg_exe()
ev = json.loads((D / "events.json").read_text())
E = {e["name"]: e for e in ev["events"]}
raw = Path(ev["video"])
# The recording starts a little after the script's clock (page creation lag). It ends at ctx.close(), logged as
# "close", so shift every event by (close time - video length) to put it on the video's own timeline.
_probe = subprocess.run([get_ffmpeg_exe(), "-i", str(raw), "-map", "0:v", "-f", "null", "-"], capture_output=True, text=True).stderr
_vlen = [ln for ln in _probe.splitlines() if "time=" in ln][-1].split("time=")[1].split()[0]
_vlen = sum(float(x) * 60 ** i for i, x in enumerate(reversed(_vlen.split(":"))))
OFFSET = (E["close"]["t"] if "close" in E else E["end"]["t"] + 0.05) - _vlen
for _e in ev["events"]:
    _e["t"] = round(_e["t"] - OFFSET, 3)
print(f"video {_vlen:.2f} s, events shifted by {OFFSET:.2f} s")
agent_s, bodies = E["done:build"]["agent_s"], E["done:build"]["bodies"]
W = D / "work"; W.mkdir(exist_ok=True)
ADMIN = ROOT.parent / "agenticcad-admin"
logo = base64.b64encode((ADMIN / "brand/agenticcad-logo-on-dark.png").read_bytes()).decode()
music = ADMIN / "promo/promo-music.wav"
mmss = f"{int(agent_s // 60)}:{agent_s % 60:04.1f}"
FAST = round(max(1.0, (E["done:build"]["t"] - E["send"]["t"]) / 12.0), 1)     # short cut: the build takes ~12 s on screen

CSS = """*{margin:0;box-sizing:border-box} html,body{width:1920px;height:1080px;background:transparent;font-family:-apple-system,'SF Pro Display','Helvetica Neue',sans-serif}
.lt{position:absolute;left:64px;bottom:120px;padding:22px 30px;border-radius:18px;background:rgba(12,15,20,.86);color:#fff;box-shadow:0 10px 40px rgba(0,0,0,.4)}
.lt b{display:block;font-size:40px;font-weight:700;letter-spacing:-.01em} .lt span{display:block;margin-top:8px;font-size:24px;color:#aeb8c6}
.badge{position:absolute;left:50%;bottom:34px;transform:translateX(-50%);padding:10px 20px;border-radius:999px;background:rgba(12,15,20,.86);color:#fff;font-size:22px;font-weight:600}
.badge i{font-style:normal;color:#5ee0a0}
.end{position:absolute;inset:0;background:radial-gradient(ellipse at 50% 40%,#1c2330 0%,#0b0e13 70%);display:flex;flex-direction:column;align-items:center;justify-content:center;color:#fff;gap:26px}
.end img{height:120px} .end b{font-size:46px;font-weight:700} .end span{font-size:30px;color:#aeb8c6} .end u{text-decoration:none;font-size:34px;color:#5ee0a0;font-weight:600}"""


def card(name: str, html: str) -> Path:
    path = W / f"{name}.png"
    page.set_content(f"<style>{CSS}</style>{html}"); page.wait_for_timeout(150)
    page.screenshot(path=str(path), omit_background=True)
    return path


with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True)
    page = b.new_page(viewport={"width": 1920, "height": 1080})
    cap_prompt = card("cap-prompt", f'<div class="lt"><b>One prompt: {subject}</b><span>{bodies} separate parts, every dimension given</span></div>')
    cap_done = card("cap-done", f'<div class="lt"><b>Done in {mmss}</b><span>{bodies} bodies · exact B-rep · an editable build123d script</span></div>')
    end = card("end", f'<div class="end"><img src="data:image/png;base64,{logo}"><b>Describe the part. Get exact CAD.</b>'
                      f'<span>Free for personal and non-commercial use · macOS and Windows</span><u>agenticcad.github.io/agenticcad</u></div>')
    cap_reveal = card("cap-reveal", f'<div class="lt"><b>Every part is real geometry</b><span>{reveal_line}</span></div>')
    badge_png = card("badge", f'<div class="badge"><i>{FAST:.1f}× speed</i> · the clock at the top shows real time</div>')
    b.close()


def make(out: Path, speed: float) -> None:
    a0 = E["start"]["t"] - 0.2
    s0 = E["send"]["t"] + 0.6                                  # prompt visible in the chat before speeding up
    s1 = E["done:build"]["t"] + 0.4
    t_end = E["end"]["t"]
    segs = [(a0, s0, 1.0), (s0, s1, speed), (s1, t_end, 1.0)]
    L = [(b_ - a_) / k for a_, b_, k in segs]
    main_len = sum(L); END_LEN = 3.6; total = main_len + END_LEN
    t_send, t_fast_end = L[0], L[0] + L[1]
    badge = badge_png if speed > 1.01 else None

    inputs = ["-i", str(raw)]
    fc = []
    for i, (a_, b_, k) in enumerate(segs):
        fc.append(f"[0:v]trim={a_:.3f}:{b_:.3f},setpts=(PTS-STARTPTS)/{k},fps=30,format=yuv420p[s{i}]")
    fc.append("[s0][s1][s2]concat=n=3:v=1:a=0[main]")

    def overlay(src: str, png: Path, t0: float, t1: float, n: int) -> str:
        inputs.extend(["-loop", "1", "-t", f"{t1 - t0:.3f}", "-i", str(png)])
        idx = len([x for x in inputs if x == "-i"]) - 1
        dur = t1 - t0
        fc.append(f"[{idx}:v]format=rgba,fade=in:st=0:d=0.35:alpha=1,fade=out:st={dur - 0.35:.3f}:d=0.35:alpha=1,setpts=PTS-STARTPTS+{t0:.3f}/TB[o{n}]")
        fc.append(f"[{src}][o{n}]overlay=eof_action=pass:format=auto[v{n}]")
        return f"v{n}"

    v = overlay("main", cap_prompt, 0.3, max(t_send + 1.8, 4.0), 1)
    if badge:
        v = overlay(v, badge, t_send + 0.2, t_fast_end, 2)
    v = overlay(v, cap_done, t_fast_end + 0.3, min(t_fast_end + 5.0, main_len - 0.2), 3)
    if "reveal" in E:
        t_rev = t_fast_end + (E["reveal"]["t"] - s1)
        v = overlay(v, cap_reveal, t_rev + 0.5, min(t_rev + 7.0, main_len - 0.2), 4)
    inputs.extend(["-loop", "1", "-t", f"{END_LEN}", "-i", str(end)])
    ei = len([x for x in inputs if x == "-i"]) - 1
    fc.append(f"[{ei}:v]fps=30,format=yuv420p,fade=in:st=0:d=0.5[endv]")
    fc.append(f"[{v}]format=yuv420p[vm]"); fc.append("[vm][endv]concat=n=2:v=1:a=0[vout]")
    inputs.extend(["-stream_loop", "-1", "-i", str(music)])
    mi = len([x for x in inputs if x == "-i"]) - 1
    fc.append(f"[{mi}:a]atrim=0:{total:.3f},asetpts=PTS-STARTPTS,volume=0.55,afade=in:st=0:d=1.0,afade=out:st={total - 2.2:.3f}:d=2.2[aout]")
    cmd = [FF, "-loglevel", "error", "-y", *inputs, "-filter_complex", ";".join(fc), "-map", "[vout]", "-map", "[aout]",
           "-c:v", "libx264", "-preset", "slow", "-crf", "19", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
           "-movflags", "+faststart", "-t", f"{total:.3f}", str(out)]
    subprocess.run(cmd, check=True)
    print(f"{out.name}: {total:.1f} s (build {E['done:build']['t'] - E['send']['t']:.1f} s shown at {speed:.1f}×)")


make(D / "showcase-realtime.mp4", 1.0)
make(D / "showcase-short.mp4", FAST)
