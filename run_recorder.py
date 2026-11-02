"""Run recorder: what the machine did during a program, kept with the design for later analysis (planned vs actual).

Every program the Machine workspace sends is recorded under <workspace>/runs/<design>/<YYYYmmdd-HHMMSS>_<program>/:
  run.json      what ran (program, setup, ops with their line ranges and estimates, tools, air cut, app version), when, how
                it ended, and per-op actual time from the samples
  program.nc    the exact text uploaded to the machine (line numbers in the samples index it)
  cam.py        the CAM script it came from (when the run came from CAM operations)
  samples.jsonl.gz  one row per machine status the app saw (~4 Hz): t (s from start), state, line, op index, machine
                position, work position, feed [current, requested, override %], spindle [rpm, target, override %, temp C, power temp C],
                tool number
  events.jsonl  the app's notes during the run (tool changes, pauses, alarms) and interesting console lines (probe
                touches [PRB:...], errors, alarms), with t
Local files only; nothing leaves the machine. A recorder failure never stops a job (it just stops recording).
"""
from __future__ import annotations

import gzip
import json
import re
import shutil
import time
from pathlib import Path
from typing import Any

KEEP_PER_DESIGN = 40                        # older runs of a design are deleted (each is a few MB at most)
CONSOLE_KEEP = re.compile(r"\[PRB:|ALARM|[Ee]rror|Halt|halt", re.I)


def _slug(s: str, n: int = 40) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s or "").strip("_.")[:n] or "untitled"


class RunRecorder:
    def __init__(self, root: Path, info: dict, job: dict, text: str, cam_code: str | None = None, extra: dict | None = None):
        owner = info.get("owner") or {}
        self.t0 = time.time()
        design = owner.get("name") or info.get("program") or "untitled"     # an unsaved design is filed under its program
        self.dir = Path(root) / _slug(design) / (time.strftime("%Y%m%d-%H%M%S") + "_" + _slug(info.get("program") or job.get("name", "program")))
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "program.nc").write_text(text)
        if cam_code:
            (self.dir / "cam.py").write_text(cam_code)
        self.meta: dict[str, Any] = {
            "design": design, "design_saved": bool(owner.get("name")), "doc": owner.get("doc"), "program": info.get("program"),
            "setup": info.get("setup"), "wcs": info.get("wcs"), "file": job.get("name"), "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "t0": self.t0,
            "lines": job.get("lines"), "air_cut": job.get("air_cut"), "rotary": job.get("rotary"), "tools": job.get("tools"),
            "est_min": info.get("time_min"), "notes": job.get("notes"), "outcome": "running",
            "ops": [{k: o.get(k) for k in ("name", "kind", "key", "start", "end", "tool", "time")} for o in job.get("ops", [])],
            **(extra or {})}
        self._write_meta()
        self._samples = open(self.dir / "samples.jsonl", "a")
        self._events = open(self.dir / "events.jsonl", "a")
        self._last_status_t = 0.0
        self._last_log_t = self.t0
        self._flush_t = 0.0
        self.closed = False
        _prune(self.dir.parent)

    def _write_meta(self) -> None:
        tmp = self.dir / "run.json.tmp"
        tmp.write_text(json.dumps(self.meta, indent=1))
        tmp.replace(self.dir / "run.json")

    def _t(self) -> float:
        return round(time.time() - self.t0, 2)

    def sample(self, st: dict | None, status_t: float, job: dict, log: list | None = None) -> None:
        """One machine status (skipped if it's the one already recorded), plus new console lines worth keeping."""
        if self.closed:
            return
        if st and status_t > self._last_status_t:
            self._last_status_t = status_t
            f, s = st.get("feed") or {}, st.get("spindle") or {}
            row = {"t": round(status_t - self.t0, 2), "s": st.get("state"), "ln": job.get("line"), "op": job.get("op"),
                   "m": st.get("mpos"), "w": st.get("wpos"), "f": [f.get("current"), f.get("requested"), f.get("override")],
                   "sp": [s.get("rpm"), s.get("target"), s.get("override"), s.get("temp"), s.get("power_temp")],
                   "tl": (st.get("tool") or {}).get("number")}
            if st.get("mpos_ab"):
                row["a"] = st["mpos_ab"][0] if st["mpos_ab"] else None
            self._samples.write(json.dumps(row, separators=(",", ":")) + "\n")
        for t, ln in (log or []):
            if t > self._last_log_t:
                self._last_log_t = t
                if CONSOLE_KEEP.search(ln):
                    self.event("console", text=ln.strip(), t=round(t - self.t0, 2))
        if time.time() - self._flush_t > 2.0:
            self._flush_t = time.time()
            self._samples.flush(); self._events.flush()

    def event(self, kind: str, text: str = "", t: float | None = None, **kw) -> None:
        if self.closed:
            return
        self._events.write(json.dumps({"t": self._t() if t is None else t, "kind": kind, "text": text, **kw}) + "\n")

    def close(self, outcome: str, msg: str = "", job: dict | None = None) -> None:
        if self.closed:
            return
        self.closed = True
        self._samples.close(); self._events.close()
        raw = self.dir / "samples.jsonl"
        rows = [json.loads(x) for x in raw.read_text().splitlines() if x.strip()]
        with open(raw, "rb") as a, gzip.open(self.dir / "samples.jsonl.gz", "wb") as b:
            shutil.copyfileobj(a, b)
        raw.unlink()
        # per-op actual time: each sample's interval goes to the op it was in (tool-change waits count to the op they precede)
        spent: dict[int, float] = {}
        for a, b in zip(rows, rows[1:]):
            if a.get("op") is not None:
                spent[a["op"]] = spent.get(a["op"], 0.0) + (b["t"] - a["t"])
        for i, o in enumerate(self.meta["ops"]):
            o["actual_min"] = round(spent.get(i, 0.0) / 60.0, 2) if spent else None
        self.meta.update(outcome=outcome, message=msg, ended=time.strftime("%Y-%m-%dT%H:%M:%S"),
                         duration_min=round((time.time() - self.t0) / 60.0, 2), samples=len(rows),
                         machine_seconds=(job or {}).get("seconds"), last_line=(job or {}).get("line"))
        self._write_meta()


def _prune(design_dir: Path) -> None:
    runs = sorted(p for p in design_dir.iterdir() if p.is_dir())
    for p in runs[:-KEEP_PER_DESIGN]:
        shutil.rmtree(p, ignore_errors=True)


def run_dir(root: Path, run_id: str) -> Path | None:
    """The folder of run `<design>/<run>` (as list_runs gives it), only inside root."""
    parts = run_id.split("/")
    if len(parts) != 2 or any(not p or p in (".", "..") or p != _slug(p, 200) for p in parts):
        return None
    d = Path(root) / parts[0] / parts[1]
    return d if (d / "run.json").exists() else None


def list_runs(root: Path, design: str | None = None) -> list[dict]:
    """Recorded runs, newest first (run.json of each), optionally for one design."""
    root = Path(root)
    if not root.is_dir():
        return []
    dirs = [root / _slug(design)] if design else [p for p in root.iterdir() if p.is_dir()]
    out = []
    for d in dirs:
        if not d.is_dir():
            continue
        for r in d.iterdir():
            try:
                meta = json.loads((r / "run.json").read_text())
            except (OSError, ValueError):
                continue
            meta["id"] = f"{d.name}/{r.name}"
            out.append(meta)
    return sorted(out, key=lambda m: m.get("t0", 0), reverse=True)
