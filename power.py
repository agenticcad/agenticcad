"""Estimated spindle power for a recorded run: the simulated material removal rate of each move it ran, times the
material's specific cutting energy - an ESTIMATE, not a measurement (the Z1 reports no load; M957's drive duty can
calibrate it later).

  P_cut [W] = u [J/mm³] × MRR [mm³/s]

u from handbook ranges (6061 ≈ 0.7 J/mm³; thin chips from small tools at light chip loads cost more - the size effect -
so the low/high band is ×0.7 .. ×1.8). MRR per move comes from cam_sim on the run's own cam.py with its model (saved with
the run from 1.7.0; older runs use the open design's model when the run's CAM script builds against it) and the full
stock history (earlier setups first). Each recorded sample is placed on the nearest simulated move of its operation.
Cached as power.json next to the run.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np

VERSION = 1
SPECIFIC_ENERGY = {"aluminium": 0.7, "brass": 1.4, "mild-steel": 2.7, "acrylic": 0.15, "hdpe": 0.1, "delrin": 0.15,
                   "plywood": 0.08, "mdf": 0.08, "hardwood": 0.1, "softwood": 0.06, "foam": 0.02, "fr4": 0.4,
                   "carbon-fibre": 0.6}
BAND = (0.7, 1.8)


def _matched(run_dir: Path):
    """The run's samples with their work position and program move (run_analysis's matcher)."""
    import run_analysis as ra
    data = ra.load(run_dir)
    moves = ra.parse_program(data["program"], data["meta"].get("ops") or [])
    matcher = ra._Matcher(moves)
    rows = data["rows"]
    off = {"xy": None, "z": {}, "fitted": False}
    if not any(r.get("w") for r in rows):
        off = ra.fit_offset(rows, matcher)
    out = []
    for r in rows:
        w = ra._work(r, off)
        k, dist, _ = matcher.match(w, r.get("ln")) if (w is not None and r.get("s") == "Run") else (None, math.inf, None)
        out.append((r, w, moves[k]["op"] if k is not None and dist < 2.0 else None))
    return data["meta"], out


def _use_gcode(prog, idx: list[int], run_dir: Path, meta: dict) -> None:
    """Replace the run's operations' moves with the moves of the G-code it ran (work coordinates back to the setup frame,
    arcs as segments): the CAM kernel may have changed since, and the estimate must follow what was cut. Earlier ops keep
    the rebuilt paths - they only make the stock the run started from."""
    import cam_kernel as cam
    import run_analysis as ra
    moves = ra.parse_program((run_dir / "program.nc").read_text(), meta.get("ops") or [])
    by_op: dict[int, list] = {}
    for m in moves:
        if m.get("op") is None or m.get("a") is None or m.get("machine"):
            continue
        pts = ra._arc_points(m["a"], m["b"], m["c"], m["g"] == 2)[1:] if m["g"] in (2, 3) and "c" in m else [m["b"]]
        kind = cam.RAPID if m["g"] == 0 else cam.FEED
        by_op.setdefault(m["op"], []).extend([kind, float(p[0]), float(p[1]), float(p[2]), float(m.get("f") or 0.0)] for p in pts)
    names = [o.get("name") for o in meta.get("ops") or []]
    for i in idx:
        op = prog.ops[i]
        j = names.index(op.name)
        if j not in by_op:
            continue
        org = prog.setup_of(op).origin_point()
        op.moves = [[k, x + org[0], y + org[1], z + org[2], f] for k, x, y, z, f in by_op[j]]
        op.sim_moves = None
        op.tiled = False


def estimate(run_dir: Path, workspace: Path, model_code: str | None = None, config: str | None = None,
             use_cache: bool = True) -> dict[str, Any]:
    """{"material", "u", "band", "basis", "ops": [{name, mean_w, peak_w}], "series": [[t, watts], ...], "peak_w", "mean_w"}
    or {"error": why} when the run's program can't be rebuilt."""
    run_dir = Path(run_dir)
    cache = run_dir / "power.json"
    stamp = [VERSION, (run_dir / "samples.jsonl.gz").stat().st_mtime if (run_dir / "samples.jsonl.gz").exists() else None]
    if use_cache and cache.exists():
        try:
            c = json.loads(cache.read_text())
            if c.get("stamp") == stamp:
                return c
        except (OSError, ValueError):
            pass
    import cad_kernel as ck
    import cam_kernel as cam
    import cam_sim
    from cam_data import Library
    cam_code = (run_dir / "cam.py").read_text() if (run_dir / "cam.py").exists() else None
    if not cam_code:
        return {"error": "this run has no CAM script with it (a file run): nothing to simulate"}
    own = run_dir / "model.py"
    code = own.read_text() if own.exists() else model_code
    meta_cfg = None
    try:
        meta_cfg = json.loads((run_dir / "run.json").read_text()).get("config")
    except (OSError, ValueError):
        pass
    if not code:
        return {"error": "the run's design isn't saved with it and no design is open to rebuild it"}
    try:
        model = ck.run_script(code, workspace=workspace, config=meta_cfg if own.exists() else (meta_cfg or config))
        lib = Library(workspace)
        ns = {"__name__": "__cam__"}
        exec("from cam_kernel import *\nimport math\nfrom math import *\nimport cam_kernel as cam\n", ns)
        ns.update({"model": model, "part": model.shape, "bodies": {b.path: b.shape for b in model.bodies},
                   "tools": lib.tool_map(), "machines": lib.machines()})
        cam.reset_overrides()
        exec(compile(cam_code, "cam.py", "exec"), ns)
        prog = ns["program"]
    except Exception as e:  # noqa: BLE001 - an old run whose design moved on
        return {"error": f"the run's CAM script doesn't rebuild against {'its' if own.exists() else 'the open'} design: {type(e).__name__}: {e}"}
    meta, samples = _matched(run_dir)
    names = [o.get("name") for o in meta.get("ops") or []]
    idx = [i for i, op in enumerate(prog.ops) if op.name in names]
    if not idx:
        return {"error": "the run's operations aren't in its CAM script any more"}
    _use_gcode(prog, idx, run_dir, meta)                   # simulate the moves that ran, not today's kernel's
    sim = cam_sim.simulate(prog, idx, model, None)
    mat = (getattr(prog, "material", None) or "aluminium").lower()
    u = SPECIFIC_ENERGY.get(mat, 0.7)
    from scipy.spatial import cKDTree
    trees = {}
    for i in idx:
        op = prog.ops[i]
        pm = (sim.per_move or {}).get(i) or {}
        mrr = np.asarray(pm.get("mrr", []), dtype=float)
        if not len(mrr):
            continue
        org = np.array(prog.setup_of(op).origin_point(), dtype=float)
        pts, which = [], []
        mv = op.moves
        for k in range(1, len(mv)):
            a, b = np.array(mv[k - 1][1:4], float), np.array(mv[k][1:4], float)
            n = max(1, int(np.linalg.norm(b - a) / 0.3))
            for j in range(n + 1):
                pts.append(a + (b - a) * j / n - org); which.append(k)
        if pts:
            trees[op.name] = (cKDTree(np.array(pts)), np.array(which), mrr)
    series, per = [], {}
    for r, w, opi in samples:
        if w is None or opi is None or opi >= len(names) or names[opi] not in trees:
            continue
        tr, which, mrr = trees[names[opi]]
        d, j = tr.query(np.asarray(w, float))
        if d > 1.5:
            continue
        k = which[j]
        f = r.get("f") or [None, None, None]
        scale = (f[2] or 100.0) / 100.0                    # a feed override scales the removal rate with it
        watts = u * mrr[k] * scale / 60.0 if k < len(mrr) else 0.0
        series.append([r["t"], round(watts, 2)])
        per.setdefault(names[opi], []).append(watts)
    if not series:
        return {"error": "no sample could be placed on the simulated moves"}
    step = max(1, len(series) // 2400)
    ws_ = np.array([w for _, w in series])
    out = {"stamp": stamp, "estimate": True, "material": mat, "u": u, "band": [round(u * BAND[0], 2), round(u * BAND[1], 2)],
           "basis": "simulated removal rate × specific cutting energy" + ("" if getattr(prog, "material", None) else " (material assumed aluminium)")
                    + (", the run's own design" if own.exists() else ", the open design's model"),
           "peak_w": round(float(np.percentile(ws_, 99.5)), 1), "mean_w": round(float(ws_[ws_ > 0.05].mean()) if (ws_ > 0.05).any() else 0.0, 1),
           "ops": [{"name": n, "mean_w": round(float(np.mean([x for x in v if x > 0.05] or [0])), 1), "peak_w": round(float(np.percentile(v, 99)), 1)}
                   for n, v in per.items()],
           "series": series[::step]}
    try:
        cache.write_text(json.dumps(out))
    except OSError:
        pass
    return out
