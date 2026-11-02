"""Planned vs actual for a recorded run (run_recorder): what the machine really did against the program it was sent.

The Z1 reports the line it is *reading* (its planner runs a few blocks ahead of the motion) and the programmed feed of
that block, not the speed it is moving at. So the analysis goes by position: each status sample's machine position is
matched to the nearest planned move within a window of lines behind the reported one, and the real speed comes from
the distance between consecutive samples. The work offset is recorded with each sample (from 1.5.1); for older runs it
is found by fitting the samples to the program (XY once, Z per tool: the tool length offset changes with the tool).

analyse(run_dir) -> {"run", "ops", "timeline", "path", "moves", "findings", "summary"}; the result is cached next to the
run as analysis.json (rebuilt when the run's files change).
"""
from __future__ import annotations

import gzip
import json
import math
import re
from pathlib import Path
from typing import Any

import numpy as np

VERSION = 1                       # bump when the analysis changes shape: cached results are rebuilt
WINDOW = 60                       # how many lines behind the reported one the motion can be (planner look-ahead)
ARC_STEP = 0.5                    # mm between points when arcs are turned into segments for matching
RAPID = None                      # the commanded feed of a G0 (the machine's own rapid rate)
SLOW = 0.8                        # a cutting sample below this fraction of its commanded feed is "slowed"
TEMP_HALT = 70.0                  # the Z1 halts above this spindle temperature (C)
WORD = re.compile(r"([A-Z])\s*(-?\d*\.?\d+)")


# ---------------------------------------------------------------- loading
def load(run_dir: Path) -> dict[str, Any]:
    d = Path(run_dir)
    meta = json.loads((d / "run.json").read_text())
    if (d / "samples.jsonl.gz").exists():
        lines = gzip.open(d / "samples.jsonl.gz", "rt").read().splitlines()
    elif (d / "samples.jsonl").exists():
        lines = (d / "samples.jsonl").read_text().splitlines()
    else:
        lines = []
    rows = []
    for x in lines:
        try:
            rows.append(json.loads(x))
        except ValueError:                       # a run still being written can end mid-line
            pass
    events = []
    if (d / "events.jsonl").exists():
        for x in (d / "events.jsonl").read_text().splitlines():
            try:
                events.append(json.loads(x))
            except ValueError:
                pass
    prog = (d / "program.nc").read_text() if (d / "program.nc").exists() else ""
    cam = (d / "cam.py").read_text() if (d / "cam.py").exists() else None
    return {"dir": d, "meta": meta, "rows": rows, "events": events, "program": prog, "cam": cam}


# ---------------------------------------------------------------- the program
def parse_program(text: str, ops: list[dict] | None = None) -> list[dict]:
    """One entry per motion line: {ln (1-based, as the machine counts), g (0-3), a, b (work xyz), f (mm/min, None for
    G0), tool, op (index into ops by line range), machine (a G53 move: machine coordinates, not matched)}."""
    pos = [0.0, 0.0, 0.0]
    g, f, tool, known = 0, None, None, [False, False, False]
    out: list[dict] = []
    ranges = [(o.get("start") or 0, o.get("end") or 0) for o in (ops or [])]

    def op_of(ln: int) -> int | None:
        for i, (s, e) in enumerate(ranges):
            if s <= ln <= e:
                return i
        return None

    for i, raw in enumerate(text.splitlines(), start=1):
        s = raw.split(";")[0].split("(")[0].strip().upper()
        if not s:
            continue
        words = WORD.findall(s)
        gs = [float(v) for k, v in words if k == "G"]
        vals = {k: float(v) for k, v in words if k != "G"}
        if any(int(x) == 6 for k, x in ((k, float(v)) for k, v in words if k == "M")) and "T" in vals:
            tool = int(vals["T"])
        machine = 53 in [int(x) for x in gs]
        for x in gs:
            if x in (0, 1, 2, 3):
                g = int(x)
        if "F" in vals:
            f = vals["F"]
        if not any(k in vals for k in "XYZ"):
            continue
        if machine:                                  # G53: machine coordinates; the work position afterwards is unknown on that axis
            for j, k in enumerate("XYZ"):
                if k in vals:
                    known[j] = False
            out.append({"ln": i, "g": g, "a": None, "b": None, "f": None, "tool": tool, "op": op_of(i), "machine": True})
            continue
        a = list(pos)
        b = [vals.get(k, pos[j]) for j, k in enumerate("XYZ")]
        from_known = all(known)
        for j, k in enumerate("XYZ"):
            if k in vals:
                known[j] = True
        mv = {"ln": i, "g": g, "a": a if from_known else None, "b": b, "f": None if g == 0 else f, "tool": tool, "op": op_of(i),
              "machine": False}
        if g in (2, 3) and from_known:
            mv["c"] = [a[0] + vals.get("I", 0.0), a[1] + vals.get("J", 0.0)]
        out.append(mv)
        pos = b
    return out


def _arc_points(a, b, c, cw: bool) -> list[list[float]]:
    r = math.hypot(a[0] - c[0], a[1] - c[1])
    t0, t1 = math.atan2(a[1] - c[1], a[0] - c[0]), math.atan2(b[1] - c[1], b[0] - c[0])
    sweep = t1 - t0
    if cw and sweep >= -1e-9:
        sweep -= 2 * math.pi
    elif not cw and sweep <= 1e-9:
        sweep += 2 * math.pi
    n = max(2, int(abs(sweep) * r / ARC_STEP) + 1)
    return [[c[0] + r * math.cos(t0 + sweep * k / n), c[1] + r * math.sin(t0 + sweep * k / n), a[2] + (b[2] - a[2]) * k / n]
            for k in range(n + 1)]


def move_length(m: dict) -> float:
    if m.get("a") is None:
        return 0.0
    if m["g"] in (2, 3) and "c" in m:
        p = _arc_points(m["a"], m["b"], m["c"], m["g"] == 2)
        return float(sum(math.dist(p[k], p[k + 1]) for k in range(len(p) - 1)))
    return math.dist(m["a"], m["b"])


def segments(moves: list[dict]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Every matchable move as straight segments: (P0 [n,3], P1 [n,3], move index [n])."""
    p0, p1, idx = [], [], []
    for k, m in enumerate(moves):
        if m["machine"] or m.get("a") is None:
            continue
        pts = _arc_points(m["a"], m["b"], m["c"], m["g"] == 2) if m["g"] in (2, 3) and "c" in m else [m["a"], m["b"]]
        for q in range(len(pts) - 1):
            p0.append(pts[q]); p1.append(pts[q + 1]); idx.append(k)
    return np.array(p0, float).reshape(-1, 3), np.array(p1, float).reshape(-1, 3), np.array(idx, int)


# ---------------------------------------------------------------- matching samples to moves
def _nearest(p: np.ndarray, P0: np.ndarray, P1: np.ndarray) -> tuple[int, float, np.ndarray]:
    d = P1 - P0
    L2 = (d * d).sum(1)
    t = np.where(L2 > 0, ((p - P0) * d).sum(1) / np.where(L2 > 0, L2, 1), 0.0).clip(0, 1)
    q = P0 + d * t[:, None]
    dist = np.sqrt(((q - p) ** 2).sum(1))
    k = int(dist.argmin())
    return k, float(dist[k]), q[k]


class _Matcher:
    def __init__(self, moves: list[dict]):
        self.moves = moves
        self.P0, self.P1, self.idx = segments(moves)
        self.seg_ln = np.array([moves[k]["ln"] for k in self.idx], int) if len(self.idx) else np.zeros(0, int)

    def window(self, ln: int | None) -> slice:
        if not ln:
            return slice(0, 0)
        lo = int(np.searchsorted(self.seg_ln, ln - WINDOW, "left"))
        hi = int(np.searchsorted(self.seg_ln, ln + 1, "right"))
        return slice(lo, hi)

    def match(self, work: np.ndarray, ln: int | None) -> tuple[int | None, float, np.ndarray | None]:
        w = self.window(ln)
        if w.stop <= w.start:
            return None, math.inf, None
        k, dist, q = _nearest(work, self.P0[w], self.P1[w])
        return int(self.idx[w.start + k]), dist, q


def fit_offset(rows: list[dict], matcher: _Matcher, iters: int = 6) -> dict[str, Any]:
    """Work coordinate offset (machine - work) for a run recorded without it: XY shared, Z per tool (each tool has its own
    length offset). Iterative closest point within the line window. How far the reported line runs ahead of the motion
    isn't known, so it starts from several guesses (the motion k lines behind the reported one) and keeps the best fit."""
    use = [r for r in rows if r.get("s") == "Run" and r.get("ln") and r.get("m")]
    if not use or not len(matcher.idx):
        return {"xy": None, "z": {}, "fitted": True, "residual_mm": None}
    end_of = {m["ln"]: m["b"] for m in matcher.moves if not m["machine"] and m.get("b") is not None}
    lns = np.array(sorted(end_of))

    def guess(k: int):
        d = []
        for r in use[:: max(1, len(use) // 2000)]:
            j = int(np.searchsorted(lns, r["ln"] - k, "right")) - 1
            if j >= 0:
                d.append((r.get("tl"), np.array(r["m"]) - np.array(end_of[int(lns[j])])))
        if not d:
            return None
        return np.median(np.array([x[1][:2] for x in d]), 0), {t: float(np.median([x[1][2] for x in d if x[0] == t])) for t in {x[0] for x in d}}

    def icp(off_xy, off_z, sample, n):
        res = None
        for _ in range(n):
            dxy, dz, dist = [], {}, []
            for r in sample:
                t = r.get("tl")
                k, dd, q = matcher.match(np.array(r["m"]) - np.array([off_xy[0], off_xy[1], off_z.get(t, 0.0)]), r["ln"])
                if k is None or dd > 25:
                    continue
                delta = np.array(r["m"]) - q
                dxy.append(delta[:2]); dz.setdefault(t, []).append(delta[2]); dist.append(dd)
            if len(dist) < max(5, len(sample) // 5):
                return off_xy, off_z, math.inf
            off_xy = np.median(np.array(dxy), 0)
            off_z = {**off_z, **{t: float(np.median(v)) for t, v in dz.items()}}
            res = float(np.median(dist)) * len(sample) / len(dist)          # unplaced samples count against a fit
        return off_xy, off_z, res if res is not None else math.inf

    screen = use[:: max(1, len(use) // 600)]
    best = None
    for k in (0, 1, 2, 3, 5, 8, 12, 20, 30, 45):
        g = guess(k)
        if g is None:
            continue
        cand = icp(g[0], g[1], screen, 4)
        if best is None or cand[2] < best[2]:
            best = cand
    if best is None:
        return {"xy": None, "z": {}, "fitted": True, "residual_mm": None}
    off_xy, off_z, res = icp(best[0], best[1], use[:: max(1, len(use) // 4000)], iters)
    if not math.isfinite(res):
        off_xy, off_z, res = best
    return {"xy": [round(float(off_xy[0]), 3), round(float(off_xy[1]), 3)], "z": {str(k): round(v, 3) for k, v in off_z.items()},
            "fitted": True, "residual_mm": round(res, 3) if math.isfinite(res) else None}


# ---------------------------------------------------------------- the analysis
def _work(r: dict, off: dict) -> np.ndarray | None:
    if r.get("w"):
        return np.array(r["w"], float)
    if not r.get("m") or off.get("xy") is None:
        return None
    z = off["z"].get(str(r.get("tl")), next(iter(off["z"].values()), 0.0)) if off["z"] else 0.0
    return np.array(r["m"], float) - np.array([off["xy"][0], off["xy"][1], z])


def _pct(v: list[float], q: float) -> float | None:
    return round(float(np.percentile(v, q)), 3) if v else None


def analyse(run_dir: Path, use_cache: bool = True) -> dict[str, Any]:
    d = Path(run_dir)
    cache = d / "analysis.json"
    stamp = [p.stat().st_mtime for p in (d / "run.json", d / "samples.jsonl", d / "samples.jsonl.gz") if p.exists()]
    if use_cache and cache.exists():
        try:
            c = json.loads(cache.read_text())
            if c.get("version") == VERSION and c.get("stamp") == stamp:
                return c
        except ValueError:
            pass
    out = _analyse(load(d))
    out["version"], out["stamp"] = VERSION, stamp
    if out["run"].get("outcome") != "running":             # a run in progress changes every second: don't cache it
        try:
            cache.write_text(json.dumps(out, separators=(",", ":")))
        except OSError:
            pass
    return out


def _analyse(data: dict) -> dict[str, Any]:
    meta, rows = data["meta"], data["rows"]
    ops = meta.get("ops") or []
    moves = parse_program(data["program"], ops)
    matcher = _Matcher(moves)
    off = {"xy": None, "z": {}, "fitted": False}
    if not any(r.get("w") for r in rows):
        off = fit_offset(rows, matcher)

    # each sample: where it was (work), which move, real speed over the interval to the next sample
    S = []
    for i, r in enumerate(rows):
        w = _work(r, off)
        k, dist, _ = matcher.match(w, r.get("ln")) if (w is not None and r.get("s") == "Run") else (None, math.inf, None)
        S.append({"r": r, "w": w, "k": k if dist < 2.0 else None, "dist": dist})
    for i in range(len(S) - 1):
        a, b = S[i], S[i + 1]
        dt = b["r"]["t"] - a["r"]["t"]
        a["dt"] = dt if 0 < dt < 5 else 0.0
        a["v"] = (math.dist(a["r"]["m"], b["r"]["m"]) / dt * 60.0) if (a["r"].get("m") and b["r"].get("m") and 0 < dt < 5) else None
    if S:
        S[-1]["dt"], S[-1]["v"] = 0.0, None

    # per move: planned time vs time spent there
    mv_planned = {}
    for k, m in enumerate(moves):
        if m.get("f") and m.get("a") is not None:
            mv_planned[k] = move_length(m) / m["f"] * 60.0
    mv_spent: dict[int, float] = {}
    for s in S:
        if s["k"] is not None:
            mv_spent[s["k"]] = mv_spent.get(s["k"], 0.0) + s["dt"]

    def cutting(s) -> bool:
        m = moves[s["k"]] if s["k"] is not None else None
        return bool(m and m["g"] in (1, 2, 3) and m.get("f") and (s["r"].get("sp") or [0])[0] and s["r"]["sp"][0] > 100)

    # ---- per op
    op_out = []
    for i, o in enumerate(ops):
        mine = [s for s in S if (moves[s["k"]]["op"] if s["k"] is not None else s["r"].get("op")) == i]
        spent = sum(s["dt"] for s in mine)
        cut = [s for s in mine if cutting(s) and s.get("v") is not None]
        reached = [min(s["v"] / moves[s["k"]]["f"], 1.5) for s in cut]
        wts = [s["dt"] for s in cut]
        slow_t = sum(s["dt"] for s, x in zip(cut, reached) if x < SLOW)
        cut_t = sum(wts)
        sp = [s["r"]["sp"] for s in mine if s["r"].get("sp")]
        rpm_ratio = [s["r"]["sp"][0] / s["r"]["sp"][1] for s in cut if s["r"]["sp"][1]]    # while cutting (not spin-up)
        temps = [x[3] for x in sp if x[3] is not None]
        fo = [s["r"]["f"][2] for s in mine if s["r"].get("f") and s["r"]["f"][2] is not None]
        so = [x[2] for x in sp if x[2] is not None]
        planned_cut = sum(mv_planned.get(k, 0.0) for k, m in enumerate(moves) if m["op"] == i)
        waits = sum(s["dt"] for s in mine if s["r"].get("s") not in ("Run",))
        op_out.append({
            "name": o.get("name"), "kind": o.get("kind"), "tool": o.get("tool"), "start": o.get("start"), "end": o.get("end"),
            "est_min": o.get("time"), "actual_min": round(spent / 60, 2) if mine else None,
            "cut_min": round(cut_t / 60, 2), "planned_cut_min": round(planned_cut / 60, 2), "wait_min": round(waits / 60, 2),
            "feed_reached": round(float(np.average(reached, weights=wts)), 3) if cut_t > 0 else None,
            "slow_min": round(slow_t / 60, 2), "rpm_min": _pct(rpm_ratio, 0), "rpm_p5": _pct(rpm_ratio, 5),
            "temp_max": max(temps) if temps else None, "temp_start": temps[0] if temps else None,
            "feed_ovr": [min(fo), max(fo)] if fo else None, "spindle_ovr": [min(so), max(so)] if so else None,
            "done": bool(mine) and max((s["r"].get("ln") or 0) for s in mine) >= (o.get("end") or 0) - WINDOW,
        })

    # ---- where the time goes: moves the machine can't run at their feed, by kind
    groups: dict[tuple, dict] = {}
    for k, m in enumerate(moves):
        if k not in mv_planned or k not in mv_spent or mv_planned[k] <= 0:
            continue
        L = move_length(m)
        kind = ("arc" if m["g"] in (2, 3) else "line") + (" ramp" if m["a"] and abs(m["b"][2] - m["a"][2]) > 0.01 and m["g"] in (2, 3)
                                                            else " plunge" if m["a"] and abs(m["b"][2] - m["a"][2]) > 0.01 and L - abs(m["b"][2] - m["a"][2]) < 0.01
                                                            else "")
        size = "short (<1 mm)" if L < 1 else "medium (1-5 mm)" if L < 5 else "long (>5 mm)"
        if m["g"] in (2, 3) and "c" in m:
            r = math.hypot(m["a"][0] - m["c"][0], m["a"][1] - m["c"][1])
            size = "radius <1 mm" if r < 1 else "radius 1-3 mm" if r < 3 else "radius >3 mm"
        key = (m["op"], kind, size)
        g = groups.setdefault(key, {"op": m["op"], "kind": kind, "size": size, "n": 0, "planned_s": 0.0, "spent_s": 0.0, "feeds": set(), "lines": []})
        g["n"] += 1; g["planned_s"] += mv_planned[k]; g["spent_s"] += mv_spent[k]; g["feeds"].add(m["f"])
        if len(g["lines"]) < 400:
            g["lines"].append(m["ln"])
    move_groups = sorted(({**g, "feeds": sorted(g["feeds"]), "lost_s": round(g["spent_s"] - g["planned_s"], 1),
                           "planned_s": round(g["planned_s"], 1), "spent_s": round(g["spent_s"], 1)} for g in groups.values()),
                         key=lambda g: -g["lost_s"])

    # ---- timeline (downsampled) and the path (work coordinates with the metrics)
    step = max(1, len(S) // 2400)
    tl = []
    for s in S[::step]:
        r = s["r"]; m = moves[s["k"]] if s["k"] is not None else None
        sp = r.get("sp") or [None] * 5
        reached = round(min(s["v"] / m["f"], 1.5), 3) if (m and m.get("f") and s.get("v") is not None) else None
        tl.append([r["t"], r.get("s"), m["op"] if m else r.get("op"), reached, (r.get("f") or [None] * 3)[2],
                   round(sp[0] / sp[1], 4) if sp[0] is not None and sp[1] else None, sp[3], sp[4], sp[2], r.get("tl")])
    pstep = max(1, len(S) // 8000)
    path = []
    for s in S[::pstep]:
        if s["w"] is None or s["k"] is None:
            continue
        m = moves[s["k"]]; sp = s["r"].get("sp") or [None] * 5
        reached = round(min(s["v"] / m["f"], 1.5), 3) if (m.get("f") and s.get("v") is not None) else None
        path.append([round(float(s["w"][0]), 2), round(float(s["w"][1]), 2), round(float(s["w"][2]), 2), s["r"]["t"], m["op"],
                     reached, round(sp[0] / sp[1], 4) if sp[0] and sp[1] else None, sp[3], (s["r"].get("f") or [None] * 3)[2], m["g"]])
    planned = []                                     # the program's own path (decimated) for context, per op
    for k, m in enumerate(moves):
        if m["machine"] or m.get("a") is None:
            continue
        pts = _arc_points(m["a"], m["b"], m["c"], m["g"] == 2) if m["g"] in (2, 3) and "c" in m else [m["a"], m["b"]]
        planned.append([m["op"], m["g"], [[round(p[0], 2), round(p[1], 2), round(p[2], 2)] for p in pts[:: max(1, len(pts) // 8)] + ([pts[-1]] if len(pts) > 8 else [])]])

    matched = [s for s in S if s["r"].get("s") == "Run"]
    match_rate = (sum(1 for s in matched if s["k"] is not None) / len(matched)) if matched else None
    dists = [s["dist"] for s in matched if s["k"] is not None]
    t_end = rows[-1]["t"] if rows else 0.0
    sp_all = [r["sp"] for r in rows if r.get("sp")]
    temps = [(r["t"], r["sp"][3]) for r in rows if r.get("sp") and r["sp"][3] is not None]
    summary = {
        "duration_min": round(t_end / 60, 2), "est_min": meta.get("est_min"),
        "cut_min": round(sum(s["dt"] for s in S if s["k"] is not None and cutting(s)) / 60, 2),
        "rapid_min": round(sum(s["dt"] for s in S if s["k"] is not None and moves[s["k"]]["g"] == 0) / 60, 2),
        "wait_min": round(sum(s.get("dt", 0) for s in S if s["r"].get("s") != "Run") / 60, 2),
        "spindle_temp_max": max((t for _, t in temps), default=None),
        "power_temp_max": max((x[4] for x in sp_all if x[4] is not None), default=None),
        "rpm_min_ratio": min((o["rpm_min"] for o in op_out if o.get("rpm_min") is not None), default=None),
        "feed_ovr": [min(r["f"][2] for r in rows if r.get("f")), max(r["f"][2] for r in rows if r.get("f"))] if rows else None,
        "match_rate": round(match_rate, 3) if match_rate is not None else None,
        "match_median_mm": round(float(np.median(dists)), 3) if dists else None,
        "offset": off, "samples": len(rows), "lines_reached": max((r.get("ln") or 0) for r in rows) if rows else 0,
        "temp_slope_c_per_10min": _temp_slope(temps),
    }
    out = {"run": {k: meta.get(k) for k in ("design", "design_saved", "doc", "program", "setup", "wcs", "file", "started", "ended",
                                              "outcome", "message", "est_min", "duration_min", "air_cut", "tools", "app_version", "machine",
                                              "probes", "lines")},
           "ops": op_out, "move_groups": move_groups[:40], "timeline": tl, "path": path, "planned": planned, "summary": summary,
           "events": data["events"]}
    out["findings"] = findings(out)
    return out


def _temp_slope(temps: list[tuple[float, float]]) -> float | None:
    """Spindle temperature rise over the last 30 minutes, C per 10 min (a straight-line fit)."""
    if len(temps) < 20:
        return None
    t_end = temps[-1][0]
    last = [(t, c) for t, c in temps if t >= t_end - 1800]
    if len(last) < 20 or last[-1][0] - last[0][0] < 300:
        return None
    t = np.array([x[0] for x in last]); c = np.array([x[1] for x in last])
    return round(float(np.polyfit(t, c, 1)[0]) * 600, 2)


# ---------------------------------------------------------------- findings (measured, no model involved)
def findings(a: dict) -> list[dict]:
    """What the numbers say, each with its evidence. The assistant's suggestions start from these."""
    out: list[dict] = []
    s = a["summary"]
    for i, o in enumerate(a["ops"]):
        if not o["actual_min"] or not o["est_min"] or not o["done"]:
            continue
        over = o["actual_min"] - o["est_min"]
        if abs(over) >= max(1.0, 0.15 * o["est_min"]):
            out.append({"id": f"est-{i}", "kind": "estimate", "op": i, "severity": "info" if over < 0 else "warn",
                        "title": f"{o['name']}: {o['actual_min']:.1f} min against {o['est_min']:.1f} estimated",
                        "detail": ("ran longer than estimated" if over > 0 else "ran shorter than estimated")
                                  + f" by {abs(over):.1f} min ({abs(over) / o['est_min'] * 100:.0f}%)"})
    for g in a["move_groups"][:8]:
        if g["lost_s"] < 30 or g["spent_s"] < 1.3 * g["planned_s"]:
            continue
        name = a["ops"][g["op"]]["name"] if g["op"] is not None and g["op"] < len(a["ops"]) else "?"
        out.append({"id": f"slow-{g['op']}-{g['kind']}-{g['size']}", "kind": "feed", "op": g["op"], "severity": "warn",
                    "title": f"{name}: {g['n']} {g['kind']} moves, {g['size']}, take {g['spent_s'] / max(g['planned_s'], 0.1):.1f}x their planned time",
                    "detail": f"{g['spent_s'] / 60:.1f} min spent where the feed ({', '.join(f'F{f:g}' for f in g['feeds'])}) plans "
                              f"{g['planned_s'] / 60:.1f}: {g['lost_s'] / 60:.1f} min lost to acceleration on moves too short to reach the feed",
                    "lines": g["lines"][:200], "lost_min": round(g["lost_s"] / 60, 2)})
    if s.get("temp_slope_c_per_10min") and s.get("spindle_temp_max"):
        left = TEMP_HALT - s["spindle_temp_max"]
        rate = s["temp_slope_c_per_10min"]
        if rate > 0.05:
            out.append({"id": "temp", "kind": "temperature", "op": None, "severity": "warn" if left / rate * 10 < 60 else "info",
                        "title": f"Spindle {s['spindle_temp_max']:.0f} °C and rising {rate:.1f} °C per 10 min",
                        "detail": f"{left:.0f} °C below the {TEMP_HALT:.0f} °C halt: at this rate about {left / rate * 10:.0f} more minutes of the same cutting"})
    for i, o in enumerate(a["ops"]):
        if o.get("rpm_min") is not None and o["rpm_min"] < 0.9:
            out.append({"id": f"rpm-{i}", "kind": "spindle", "op": i, "severity": "warn",
                        "title": f"{o['name']}: spindle dropped to {o['rpm_min'] * 100:.0f}% of its speed",
                        "detail": "the cut loads the spindle enough to slow it: a lighter step-down or stepover, or a lower feed"})
        if o.get("feed_ovr") and (o["feed_ovr"][0] < 100 or o["feed_ovr"][1] > 100):
            out.append({"id": f"ovr-{i}", "kind": "override", "op": i, "severity": "info",
                        "title": f"{o['name']}: feed override {o['feed_ovr'][0]:.0f}-{o['feed_ovr'][1]:.0f}%",
                        "detail": "the feed was changed by hand during this operation: its programmed feed may want the same change"})
    if s.get("match_rate") is not None and s["match_rate"] < 0.8:
        out.append({"id": "match", "kind": "data", "op": None, "severity": "info",
                    "title": f"Only {s['match_rate'] * 100:.0f}% of the samples could be placed on the program",
                    "detail": "the path colouring and per-move numbers cover the rest of the run only roughly"})
    return out


def compact(a: dict) -> dict:
    """What the assistant gets: the numbers without the per-sample arrays."""
    return {k: v for k, v in a.items() if k not in ("timeline", "path", "planned", "stamp")} | {
        "move_groups": [{k: v for k, v in g.items() if k != "lines"} | {"first_lines": g["lines"][:12]} for g in a["move_groups"][:20]]}
