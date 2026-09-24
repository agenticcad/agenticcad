"""Record the promo stage page with Playwright (Chrome), then mux with the soundtrack via ffmpeg.
Usage: record.py <site-url> <out-dir>   e.g. record.py http://localhost:8790 ../agenticcad-admin/promo"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

from imageio_ffmpeg import get_ffmpeg_exe
from playwright.sync_api import sync_playwright

site, out_dir = sys.argv[1], Path(sys.argv[2])
out_dir.mkdir(parents=True, exist_ok=True)
vid_dir = out_dir / "raw"; shutil.rmtree(vid_dir, ignore_errors=True); vid_dir.mkdir()

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome", headless=True, args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--autoplay-policy=no-user-gesture-required"])
    ctx = browser.new_context(viewport={"width": 1920, "height": 1080}, device_scale_factor=1,
                              record_video_dir=str(vid_dir), record_video_size={"width": 1920, "height": 1080})
    page = ctx.new_page()
    page.goto(f"{site}/promo/?rec=1", wait_until="networkidle")
    t0 = time.time()
    while time.time() - t0 < 200:
        if page.evaluate("window.__promoDone === true"):
            break
        time.sleep(0.5)
    secs = page.evaluate("window.__promoSeconds || 0")
    page.screenshot(path=str(out_dir / "last-frame.png"))
    ctx.close(); browser.close()
webm = next(vid_dir.glob("*.webm"))
print(f"recorded {webm} ({webm.stat().st_size // 1024} KB), stage ran {secs:.1f}s")

ff = get_ffmpeg_exe()
music = out_dir / "promo-music.wav"
mp4 = out_dir / "AgenticCAD-promo-1080p.mp4"
# trim the recording to the stage duration (+0.8s lead-in that Playwright records before navigation completes is kept),
# fade the music with the picture, encode H.264 + AAC for YouTube/social.
dur = secs + 1.0
subprocess.run([ff, "-y", "-i", str(webm), "-i", str(music),
                "-filter_complex", f"[0:v]trim=0:{dur:.2f},setpts=PTS-STARTPTS,fps=30,format=yuv420p[v];[1:a]atrim=0:{dur:.2f},afade=t=in:st=0:d=1.5,afade=t=out:st={dur-4:.2f}:d=4,volume=0.9[a]",
                "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(mp4)], check=True, capture_output=True)
# poster frame for the thumbnail (title card at ~3.5 s)
subprocess.run([ff, "-y", "-ss", "3.6", "-i", str(mp4), "-frames:v", "1", str(out_dir / "AgenticCAD-promo-thumbnail.png")], check=True, capture_output=True)
probe = subprocess.run([ff, "-i", str(mp4)], capture_output=True, text=True).stderr
print("\n".join(l.strip() for l in probe.splitlines() if "Duration" in l or "Stream" in l))
print(f"wrote {mp4} ({mp4.stat().st_size // 1024} KB)")
