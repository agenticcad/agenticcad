"""Interference analysis: which bodies overlap, by how much, and where (exact OCCT booleans on the B-rep)."""
from __future__ import annotations

import math
import time
from typing import Any

import cad_kernel as ck


def _bbox(shape):
    b = shape.bounding_box()
    return (b.min.X, b.min.Y, b.min.Z), (b.max.X, b.max.Y, b.max.Z)


def _overlap(a, b, tol: float) -> bool:
    (a0, a1), (b0, b1) = a, b
    return all(a0[i] < b1[i] - tol and b0[i] < a1[i] - tol for i in range(3))


def _thread_of(bbox, threads) -> Any:
    """The registered thread whose threaded zone contains the overlap `bbox` (all of it within the thread length,
    and within the thread's radius of its axis): a bolt engaging a tapped hole. Anything else is real interference."""
    corners = [(x, y, z) for x in (bbox[0][0], bbox[1][0]) for y in (bbox[0][1], bbox[1][1]) for z in (bbox[0][2], bbox[1][2])]
    for t in threads or []:
        try:
            d = float(str(t.size).upper().lstrip("M").split("X")[0]) if str(t.size).upper().startswith("M") else 6.0
        except ValueError:
            d = 6.0
        pitch = float(getattr(t, "pitch", 0.5) or 0.5)
        ax = t.axis; n = math.sqrt(sum(v * v for v in ax)) or 1.0
        u = [v / n for v in ax]
        length = t.depth if t.depth else 1e9
        ok = True
        for c in corners:
            w = [c[i] - t.at[i] for i in range(3)]
            along = sum(w[i] * u[i] for i in range(3))
            perp = math.sqrt(max(0.0, sum(v * v for v in w) - along * along))
            if not (-pitch <= along <= length + pitch) or perp > d / 2 * math.sqrt(2) + pitch:
                ok = False
                break
        if ok:
            return t
    return None


def _near_any_thread(box, threads) -> bool:
    """The body's bounding box contains a registered thread's start point (a tapped part or the bolt in it)."""
    (lo, hi) = box
    return any(all(lo[i] - 0.5 <= t.at[i] <= hi[i] + 0.5 for i in range(3)) for t in threads or [])


def interference(model: ck.Model, paths: list[str] | None = None, min_volume: float = 1e-3,
                 budget_s: float = 90.0, mesh: bool = True) -> dict[str, Any]:
    """Every pair of bodies (or of `paths`) whose solids overlap by more than `min_volume` mm³. Touching faces do
    not count. Returns {pairs: [{a, b, volume, center, bbox, thread, mesh}], checked, skipped_bbox, elapsed, notes}.
    `thread` names a registered thread when the overlap sits on its axis (a bolt in a tapped hole overlaps the
    thread by design)."""
    bodies = [b for b in model.bodies if paths is None or b.path in paths or b.name in paths]
    if paths:
        missing = [p for p in paths if not any(b.path == p or b.name == p for b in bodies)]
        if missing:
            raise ck.CadError(f"interference: no body {missing}; bodies are {[b.path for b in model.bodies]}")
    lo, hi = model.bbox_min, model.bbox_max
    diag = math.dist(lo, hi) or 1.0
    boxes = {b.path: _bbox(b.shape) for b in bodies}
    t0 = time.time()
    pairs, checked, skipped, notes = [], 0, 0, []
    todo = [(i, j) for i in range(len(bodies)) for j in range(i + 1, len(bodies))]
    # helical threads make booleans slow and fragile: test the plain pairs first so the time budget goes to gears,
    # shafts and housings, and threaded fasteners last
    helical = [_near_any_thread(boxes[b.path], model.threads) for b in bodies]
    todo.sort(key=lambda ij: helical[ij[0]] + helical[ij[1]])
    for n, (i, j) in enumerate(todo):
        a, b = bodies[i], bodies[j]
        if not _overlap(boxes[a.path], boxes[b.path], 1e-6):
            skipped += 1
            continue
        if time.time() - t0 > budget_s:
            notes.append(f"stopped after {budget_s:.0f} s: {len(todo) - n} pair(s) with overlapping bounding boxes not checked")
            break
        checked += 1
        try:
            common = a.shape & b.shape
            if isinstance(common, (list, tuple)):          # some build123d versions return a ShapeList
                common = ck.b3d.Compound(list(common)) if common else None
        except Exception as e:  # noqa: BLE001
            notes.append(f"{a.path} × {b.path}: boolean failed ({type(e).__name__})")
            continue
        vol = float(getattr(common, "volume", 0.0) or 0.0) if common is not None else 0.0
        if vol <= min_volume:
            continue
        c = common.center()
        center = [round(c.X, 3), round(c.Y, 3), round(c.Z, 3)]
        bb = _bbox(common)
        th = _thread_of(bb, model.threads)
        rec: dict[str, Any] = {"a": a.path, "b": b.path, "volume": round(vol, 4), "center": center,
                               "bbox": [[round(v, 3) for v in bb[0]], [round(v, 3) for v in bb[1]]],
                               "thread": th.label() if th is not None else None}
        if mesh:
            try:
                verts, tris = common.tessellate(max(0.01, diag * 0.0008), 0.3)
                rec["mesh"] = {"positions": [round(v, 4) for p in verts for v in (p.X, p.Y, p.Z)],
                               "indices": [k for t in tris for k in t]}
            except Exception:  # noqa: BLE001
                pass
        pairs.append(rec)
    pairs.sort(key=lambda p: (p["thread"] is not None, -p["volume"]))
    return {"pairs": pairs, "bodies": len(bodies), "checked": checked, "skipped_bbox": skipped,
            "elapsed": round(time.time() - t0, 2), "notes": notes}


def summary(res: dict[str, Any]) -> str:
    real = [p for p in res["pairs"] if not p["thread"]]
    thr = [p for p in res["pairs"] if p["thread"]]
    lines = [f"Interference check: {res['bodies']} bodies, {res['checked']} pair(s) with overlapping bounding boxes "
             f"tested exactly ({res['skipped_bbox']} pairs clear by bounding box), {res['elapsed']} s."]
    if not real:
        lines.append("No interference: no two bodies overlap" + (" (apart from thread engagement)." if thr else "."))
    else:
        lines.append(f"INTERFERENCE: {len(real)} overlapping pair(s):")
        for p in real[:40]:
            lines.append(f"  - {p['a']} × {p['b']}: {p['volume']:.4g} mm³ around ({', '.join(f'{v:g}' for v in p['center'])}), "
                         f"extent {'×'.join(f'{p['bbox'][1][i] - p['bbox'][0][i]:.3g}' for i in range(3))} mm")
    if thr:
        lines.append(f"{len(thr)} overlap(s) on thread axes (a bolt in a tapped hole overlaps the thread by design): "
                     + "; ".join(f"{p['a']} × {p['b']} ({p['thread']})" for p in thr[:10]))
    lines += [f"note: {n}" for n in res["notes"]]
    return "\n".join(lines)


# ---------------------------------------------------------------- collisions through motion
def _moved(shape, M):
    import build123d as b3d
    from OCP.gp import gp_Trsf
    t = gp_Trsf()
    t.SetValues(*[float(M[i, j]) for i in range(3) for j in range(4)])
    return shape.moved(b3d.Location(t))


def _vol(a, b) -> float:
    try:
        c = a & b
        if isinstance(c, (list, tuple)):
            c = ck.b3d.Compound(list(c)) if c else None
        return float(getattr(c, "volume", 0.0) or 0.0) if c is not None else 0.0
    except Exception:  # noqa: BLE001
        return -1.0


def motion_interference(model: ck.Model, steps: int = 12, min_volume: float = 1e-3, budget_s: float = 150.0) -> dict[str, Any]:
    """Run the driven joint through its range in `steps` and report bodies that collide because of the motion:
    overlap at some pose beyond what the pair already overlaps at rest (so thread engagement and fixed parts don't
    count). Pairs: moving × static and moving × moving on different joints."""
    import numpy as np
    import motion as mo
    mot = model.motion
    if not mot or not mot.get("drive"):
        raise ck.CadError("no motion: declare joints with revolute()/slider() (and drive()) in the script")
    drv = mot["drive"]
    owner = {p: j["name"] for j in mot["joints"] for p in j["bodies"]}
    bodies = {b.path: b for b in model.bodies}
    moving = [p for p in owner if p in bodies]
    static = [p for p in bodies if p not in owner]
    rest_box = {p: _bbox(bodies[p].shape) for p in bodies}
    rest_overlap: dict[tuple, float] = {}
    hits: dict[tuple, dict] = {}
    t0 = time.time()
    notes, checked = [], 0
    for k in range(steps + 1):
        if time.time() - t0 > budget_s:
            notes.append(f"stopped after {budget_s:.0f} s at step {k} of {steps}")
            break
        v = drv["start"] + (drv["stop"] - drv["start"]) * k / max(steps, 1)
        vals = mo.joint_values(mot, {drv["joint"]: v})
        mats = mo.joint_matrices(mot, vals)
        posed = {p: _moved(bodies[p].shape, mats[owner[p]]) for p in moving}
        boxes = {p: _bbox(s) for p, s in posed.items()}
        pairs = [(a, b) for a in moving for b in static] + \
                [(a, b) for i, a in enumerate(moving) for b in moving[i + 1:] if owner[a] != owner[b]]
        for a, b in pairs:
            ba = boxes[a]; bb = boxes.get(b, rest_box[b])
            if not _overlap(ba, bb, 1e-6):
                continue
            checked += 1
            vol = _vol(posed[a], posed.get(b, bodies[b].shape))
            if vol < 0:
                continue
            key = (a, b)
            if key not in rest_overlap:
                rest_overlap[key] = max(0.0, _vol(bodies[a].shape, bodies[b].shape)) if _overlap(rest_box[a], rest_box[b], 1e-6) else 0.0
            extra = vol - rest_overlap[key]
            if extra > max(min_volume, 1e-3 * rest_overlap[key]):
                h = hits.setdefault(key, {"a": a, "b": b, "first": v, "max_volume": 0.0, "at": v, "joint": drv["joint"]})
                if extra > h["max_volume"]:
                    h["max_volume"], h["at"] = round(extra, 4), v
    unit = "°" if next(j for j in mot["joints"] if j["name"] == drv["joint"])["type"] == "revolute" else " mm"
    return {"collisions": sorted(hits.values(), key=lambda h: -h["max_volume"]), "steps": steps, "drive": drv, "unit": unit,
            "moving": len(moving), "checked": checked, "elapsed": round(time.time() - t0, 2), "notes": notes}


def motion_summary(res: dict[str, Any]) -> str:
    d = res["drive"]
    unit = res.get("unit", "°")
    lines = [f"Motion check: '{d['joint']}' from {d['start']:g} to {d['stop']:g} in {res['steps']} steps, {res['moving']} moving "
             f"bodies, {res['checked']} exact pair checks, {res['elapsed']} s."]
    if not res["collisions"]:
        lines.append("No collisions through the motion.")
    else:
        lines.append(f"COLLISIONS: {len(res['collisions'])} pair(s) hit each other while moving:")
        for h in res["collisions"][:30]:
            lines.append(f"  - {h['a']} × {h['b']}: from {d['joint']} = {h['first']:g}{unit}, worst {h['max_volume']:.4g} mm³ at {h['at']:g}{unit}")
    lines += [f"note: {n}" for n in res["notes"]]
    return "\n".join(lines)
