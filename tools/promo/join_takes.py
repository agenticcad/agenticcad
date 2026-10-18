"""Join two live3.py takes into one recording for edit3.py: take A up to mark `cut` (exclusive), then take B from the
same mark. A take that stopped early has no live-events.json; its marks are read from the log live3.py printed.
Usage: join_takes.py <takeA-dir> <takeA-log> <takeB-dir> <cut-mark> <out-dir>"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from imageio_ffmpeg import get_ffmpeg_exe

A_dir, A_log, B_dir, cut, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4], Path(sys.argv[5])
out.mkdir(parents=True, exist_ok=True)
FF = get_ffmpeg_exe()


def marks(take: Path, log: Path | None):
    ev = take / "live-events.json"
    if ev.exists():
        d = json.loads(ev.read_text()); return Path(d["video"]), d["events"]
    rows = [m.groups() for m in re.finditer(r"^\s+([0-9.]+)s\s+(\S+)$", log.read_text(), re.M)]
    return next((take / "raw-live").glob("*.webm")), [{"name": n, "t": float(t)} for t, n in rows]


va, ea = marks(A_dir, A_log)
vb, eb = marks(B_dir, None)
ta = next(e["t"] for e in ea if e["name"] == cut) - 0.4
tb = next(e["t"] for e in eb if e["name"] == cut) - 0.4
events = [e for e in ea if e["t"] < ta] + [{**e, "t": round(e["t"] - tb + ta, 3)} for e in eb if e["t"] >= tb]
mp4 = out / "joined.mp4"
fc = (f"[0:v]trim=0:{ta:.3f},setpts=PTS-STARTPTS,fps=25[a];[1:v]trim=start={tb:.3f},setpts=PTS-STARTPTS,fps=25[b];"
      "[a][b]concat=n=2:v=1:a=0,format=yuv420p[v]")
r = subprocess.run([FF, "-y", "-i", str(va), "-i", str(vb), "-filter_complex", fc, "-map", "[v]", "-c:v", "libx264",
                    "-preset", "medium", "-crf", "12", str(mp4)], capture_output=True, text=True)
if r.returncode:
    print(r.stderr[-2000:]); sys.exit(1)
(out / "live-events.json").write_text(json.dumps({"video": str(mp4), "events": events}, indent=1))
print(f"joined: A 0..{ta:.1f}s + B {tb:.1f}s.. -> {mp4} ({mp4.stat().st_size // 1024 // 1024} MB), {len(events)} events")
