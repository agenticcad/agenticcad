"""Material-removal simulation for AgenticCAD CAM programs.

Two representations, picked by the setups of the simulated ops:
  zmap   (3-axis, top and flipped setups): per XY cell a column of material [zlo, zhi] in MODEL coordinates. A tool
         coming from above lowers zhi; a tool from below (a flipped setup) raises zlo.
  radial (4th axis, indexed or continuous): per (x, angle) cell the distance r from the rotary axis to the surface,
         angle measured in the part's A = 0 frame (as cam_rotary.radial_map). The tool (vertical, any Y offset) cuts
         a ray where it covers it out to the current surface.
Tools are flat, ball or V; the shank above the tip is treated as part of the tool (rapids through material show up).
The result keeps compressed snapshots for the viewer and compares the final stock with the part: gouges (cut into
the part), material left on the part, and rapids that hit material.
"""
from __future__ import annotations

import base64
import math
import zlib
from dataclasses import dataclass, field

import numpy as np

import cad_kernel as ck
import cam_kernel as cam

EMPTY = 65535


def _profile(tool: cam.Tool, d: np.ndarray) -> np.ndarray:
    """Height of the tool's cutting surface above its tip at radial distance d (inside the tool radius)."""
    r = tool.radius
    if tool.type == "ball":
        return r - np.sqrt(np.maximum(r * r - d * d, 0.0))
    if tool.type in ("vbit", "chamfer", "drill") and tool.angle:
        return d / math.tan(math.radians(tool.angle / 2))
    return np.zeros_like(d)


def _samples(moves, step: float, with_a: bool):
    """Points along each move (from the previous one), with the index of the move they belong to and its kind."""
    pts, idx, kinds = [], [], []
    prev = None
    for i, m in enumerate(moves):
        cur = np.array(m[1:4] + ([m[5] if len(m) > 5 else (prev[3] if prev is not None and len(prev) > 3 else 0.0)] if with_a else []),
                       dtype=float)
        if prev is None:
            pts.append(cur); idx.append(i); kinds.append(m[0])
        else:
            d = float(np.linalg.norm(cur[:3] - prev[:3]))
            if with_a:
                r = max(math.hypot(cur[1], cur[2]), math.hypot(prev[1], prev[2]), 1.0)
                d = max(d, math.radians(abs(cur[3] - prev[3])) * r)
            n = max(1, min(4000, int(math.ceil(d / step))))
            t = (np.arange(1, n + 1) / n)[:, None]
            seg = prev[None, :] + (cur - prev)[None, :] * t
            pts.extend(seg); idx.extend([i] * n); kinds.extend([m[0]] * n)
        prev = cur
    if not pts:
        return np.zeros((0, 4 if with_a else 3)), np.zeros(0, int), np.zeros(0, int)
    return np.array(pts), np.array(idx), np.array(kinds)


@dataclass
class SimResult:
    mode: str
    grid: dict
    frames: list = field(default_factory=list)          # [(upto_moves, {name: compressed bytes})]
    stats: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    final: dict = field(default_factory=dict)            # name -> np.ndarray (final state)
    masks: dict = field(default_factory=dict)            # gouge / left masks (bool arrays)

    def summary(self) -> str:
        s = self.stats
        lines = [f"Simulation ({self.mode}, {self.grid['res']:.2f} mm cells): {s.get('moves', 0)} moves in {s.get('ops', 0)} ops, "
                 f"removed {s.get('removed_cm3', 0):.2f} cm³"]
        g = s.get("gouge")
        if g and g["cells"]:
            lines.append(f"GOUGES: the tool cut up to {g['max_depth']:.2f} mm into the part over {g['area_mm2']:.1f} mm² "
                         f"(worst at {g['worst']})" + (f"; caused by: {', '.join(g['ops'])}" if g['ops'] else ""))
        else:
            lines.append("No gouges: the tool never cut into the part.")
        lf = s.get("left")
        if lf:
            lines.append(f"Material left on the part: up to {lf['max']:.2f} mm, {lf['area_mm2']:.0f} mm² above {lf['tol']} mm "
                         f"({lf['volume_cm3']:.3f} cm³)")
        if s.get("outside_cm3"):
            lines.append(f"Stock still standing outside the part (frame, tabs, uncut margins): {s['outside_cm3']:.2f} cm³")
        col = s.get("collisions")
        if col and col["count"]:
            lines.append(f"COLLISIONS: {col['count']} contact(s) between the tool/machine and the stock, table or fixture (worst {col['max_depth']:.2f} mm): "
                         + "; ".join(f"{h['part']} hits {h['what']} in {h['op']} at ({h['pos'][0]:.1f}, {h['pos'][1]:.1f}, {h['pos'][2]:.1f}), {h['depth']:.2f} mm"
                                     for h in col["hits"][:4]) + (" …" if col["count"] > 4 else ""))
        elif "collisions" in s:
            lines.append("No machine collisions: the holder, nose and head clear the stock; nothing cuts into the table.")
        rc = s.get("rapid_hits") or []
        if rc:
            lines.append(f"RAPIDS THROUGH MATERIAL: {len(rc)} — " + "; ".join(f"{h['op']} move {h['move']} ({h['depth']:.2f} mm deep)" for h in rc[:5]))
        lines += [f"note: {n}" for n in self.notes]
        return "\n".join(lines)

    def frame_payload(self, k: int) -> dict:
        k = max(0, min(k, len(self.frames) - 1))
        upto, data = self.frames[k]
        out = {"index": k, "upto": upto, "mode": self.mode, "grid": self.grid,
               "data": {n: base64.b64encode(v).decode() for n, v in data.items()}}
        if k == len(self.frames) - 1:
            for n, m in self.masks.items():
                out["data"][n] = base64.b64encode(zlib.compress(np.packbits(m.astype(np.uint8)).tobytes(), 6)).decode()
        return out

    def to_payload(self) -> dict:
        return {"mode": self.mode, "grid": self.grid, "frames": [f[0] for f in self.frames], "stats": self.stats,
                "notes": self.notes, "summary": self.summary()}


def _part_mesh(model_or_shape):
    if isinstance(model_or_shape, ck.Model):
        return model_or_shape.mesh
    return ck.build_model(ck.coerce_bodies(model_or_shape), "", "fine").mesh


def simulate(program: cam.Program, ops: list[int] | None = None, part=None, resolution: float | None = None,
             frames: int = 48, tol: float = 0.05, machine_model=None, placement=None) -> SimResult:
    """Simulate the given ops (indices into program.ops, default all) in order. `part` (model or shape) enables the
    gouge / left-material comparison. Returns a SimResult (summary(), frame_payload(k))."""
    sel = list(range(len(program.ops))) if ops is None else [i for i in ops if 0 <= i < len(program.ops)]
    if not sel:
        raise cam.CamError("simulate: no operations selected")
    setups = [program.setup_of(program.ops[i]) for i in sel]
    rot = [s.rotary for s in setups]
    if any(rot) and not all(rot):
        raise cam.CamError("simulate 4th-axis and flat setups separately (they use different stock models)")
    if machine_model is None:
        try:
            import machine_models as _mm
            machine_model = _mm.model_for(program.setup.machine)
        except Exception:  # noqa: BLE001 - the machine model never stops a simulation
            machine_model = None
    if all(rot):
        out = _sim_radial(program, sel, part, resolution, frames, tol)
        if machine_model is not None:
            _collide_rotary(out, program, sel, machine_model, placement or {})
        return out
    out = _sim_zmap(program, sel, part, resolution, frames, tol, machine_model, placement or {})
    return out


def _frame_marks(total: int, frames: int) -> list[int]:
    if total <= 0:
        return [0]
    n = max(1, min(frames, total))
    return sorted({int(round(total * (i + 1) / n)) for i in range(n)})


# ----------------------------------------------------------------------------- zmap
def _sim_zmap(program, sel, part, resolution, frames, tol, machine_model=None, placement=None) -> SimResult:
    placement = placement or {}
    setups = [program.setup_of(program.ops[i]) for i in sel]
    st_boxes = [s.model_stock for s in setups]
    x0 = min(b.xmin for b in st_boxes); x1 = max(b.xmax for b in st_boxes)
    y0 = min(b.ymin for b in st_boxes); y1 = max(b.ymax for b in st_boxes)
    zmin = min(b.zmin for b in st_boxes); zmax = max(b.zmax for b in st_boxes)
    area = (x1 - x0) * (y1 - y0)
    res = resolution or max(0.15, math.sqrt(area / 160_000.0))
    # the fixture (vise / clamps / plate) of the top setup, in model coordinates: uncuttable, so the grid covers it too
    fix_shapes = _fixture_shapes(setups)
    if fix_shapes:
        for shp in fix_shapes:
            bb = shp.bounding_box()
            x0, x1 = min(x0, bb.min.X), max(x1, bb.max.X); y0, y1 = min(y0, bb.min.Y), max(y1, bb.max.Y)
        res = max(res, math.sqrt((x1 - x0) * (y1 - y0) / 400_000.0))      # cap the grid at ~400k cells
    nx = int(math.ceil((x1 - x0) / res)) + 1
    ny = int(math.ceil((y1 - y0) / res)) + 1
    zhi = np.full((ny, nx), -np.inf); zlo = np.full((ny, nx), np.inf)
    st0 = st_boxes[0]
    ix0 = int(round((st0.xmin - x0) / res)); ix1 = int(round((st0.xmax - x0) / res))
    iy0 = int(round((st0.ymin - y0) / res)); iy1 = int(round((st0.ymax - y0) / res))
    zhi[iy0:iy1 + 1, ix0:ix1 + 1] = st0.zmax
    zlo[iy0:iy1 + 1, ix0:ix1 + 1] = st0.zmin
    initial = np.where(zhi > zlo, zhi - zlo, 0.0).sum() * res * res
    grid = {"res": res, "x0": x0, "y0": y0, "nx": nx, "ny": ny, "zmin": zmin, "zmax": zmax}
    out = SimResult("zmap", grid)
    has_bottom = False
    plans = []                                            # (op_index, op, side, model moves)
    for i in sel:
        op = program.ops[i]; st = program.setup_of(op)
        ax = st.tool_axis_model
        if ax[2] > 0.999:
            side = 1
        elif ax[2] < -0.999:
            side = -1; has_bottom = True
        else:
            out.notes.append(f"{op.name}: side setup ({st.orient}) is not simulated in the height-map model")
            plans.append((i, op, 0, None)); continue
        P = st.to_model(np.array([m[1:4] for m in op.moves], dtype=float)) if op.moves else np.zeros((0, 3))
        mv = [[m[0], *p] for m, p in zip(op.moves, P)]
        plans.append((i, op, side, mv))
    total = sum(len(op.moves) for _, op, _, _ in plans)
    marks = _frame_marks(total, frames)
    done = 0
    mark_i = 0
    rapid_hits = []
    collisions: list[dict] = []

    def snap(upto):
        q = {"zhi": _quant(zhi, zlo, zmin, zmax)}
        if has_bottom:
            q["zlo"] = _quant(zlo, zhi, zmin, zmax, low=True)
        out.frames.append((upto, q))

    pmaps = _part_maps_z(part, res, x0, y0, nx, ny) if part is not None else None
    fix_hi = _fixture_map(fix_shapes, res, x0, y0, nx, ny) if fix_shapes else None
    if fix_shapes:
        placement = {**placement, "fixture": placement.get("fixture", setups[0].fixture_height())}
    gouged = np.zeros((ny, nx), dtype=bool)
    gouge_ops: list[str] = []

    snap(0)
    for i, op, side, mv in plans:
        if side == 0 or not mv:
            done += len(op.moves)
            while mark_i < len(marks) and marks[mark_i] <= done:
                snap(marks[mark_i]); mark_i += 1
            continue
        tool = op.tool
        pts, idx, kinds = _samples(mv, res / 2, False)
        # process in pieces that end at frame marks so snapshots are exact
        start = 0
        while start < len(pts):
            next_mark = marks[mark_i] if mark_i < len(marks) else None
            if next_mark is not None and next_mark - done <= len(op.moves):
                end_move = next_mark - done - 1            # last move index (in this op) of this piece
                end = int(np.searchsorted(idx, end_move, side="right"))
            else:
                end = len(pts)
            end = max(end, start + 1) if end <= start else end
            P = pts[start:end]; K = kinds[start:end]; I = idx[start:end]
            _stamp_z(zhi, zlo, P, K, I, tool, side, res, x0, y0, rapid_hits, op.name)
            if machine_model is not None and side == 1:
                _collide_z(collisions, zhi, P, I, done, tool, machine_model, res, x0, y0, op.name, st_boxes[0], placement)
            if fix_hi is not None and side == 1:
                _collide_fixture(collisions, fix_hi, P, K, I, tool, machine_model, res, x0, y0, op.name, setups[0].fixture)
            start = end
            if next_mark is not None and end_move_done(idx, end, done, next_mark):
                snap(next_mark); mark_i += 1
        done += len(op.moves)
        if pmaps is not None:
            g = _gouge_z(pmaps, zhi, zlo, tol, has_bottom)[0]
            if (g & ~gouged).sum() * res * res > 0.5:
                gouge_ops.append(op.name)
            gouged |= g
        while mark_i < len(marks) and marks[mark_i] <= done:
            snap(marks[mark_i]); mark_i += 1
    if not out.frames or out.frames[-1][0] != total:
        snap(total)
    remaining = np.where(zhi > zlo, zhi - zlo, 0.0).sum() * res * res
    out.stats = {"moves": total, "ops": len(sel), "removed_cm3": round((initial - remaining) / 1000.0, 3),
                 "rapid_hits": rapid_hits[:50]}
    if machine_model is not None or fix_hi is not None:
        out.stats["collisions"] = _collision_stats(collisions)
        if machine_model is not None:
            out.stats["machine"] = {"key": machine_model.key, "name": machine_model.name, "placement": placement}
        if fix_shapes:
            out.stats["fixture"] = setups[0].fixture_payload()
        if has_bottom:
            out.notes.append("machine collisions are checked on top setups only; flipped (bottom) setups are not checked against the holder")
    if pmaps is not None:
        _compare_zmap(out, pmaps, zhi, zlo, res, x0, y0, nx, tol, has_bottom)
        out.stats["gouge"]["ops"] = gouge_ops
    out.final = {"zhi": zhi, "zlo": zlo}
    return out


def end_move_done(idx, end, done, next_mark) -> bool:
    return end > 0 and idx[end - 1] + done + 1 >= next_mark


def _quant(a, other, lo, hi, low=False) -> bytes:
    span = max(hi - lo, 1e-9)
    empty = ~(a > other) if not low else ~(other > a)
    q = np.clip(np.round((np.nan_to_num(a, posinf=hi, neginf=lo) - lo) / span * 65534), 0, 65534).astype(np.uint16)
    q[empty] = EMPTY
    return zlib.compress(q.tobytes(), 6)


def _runs(K):
    """(start, end) of consecutive samples that are all rapids or all feeds, in order."""
    if len(K) == 0:
        return []
    r = (K == cam.RAPID).astype(np.int8)
    cuts = np.flatnonzero(np.diff(r)) + 1
    edges = np.concatenate([[0], cuts, [len(K)]])
    return list(zip(edges[:-1], edges[1:]))


def _stamp_z(zhi, zlo, P, K, I, tool, side, res, x0, y0, rapid_hits, op_name):
    for a, b in _runs(K):                              # in order: a rapid sees everything cut before it
        _stamp_z_run(zhi, zlo, P[a:b], K[a:b], I[a:b], tool, side, res, x0, y0, rapid_hits, op_name)


def _stamp_z_run(zhi, zlo, P, K, I, tool, side, res, x0, y0, rapid_hits, op_name):
    ny, nx = zhi.shape
    if len(P) == 0:
        return
    R = tool.radius
    k = int(math.ceil(R / res)) + 1
    oy, ox = np.mgrid[-k:k + 1, -k:k + 1]
    oy = oy.ravel(); ox = ox.ravel()
    bx = np.floor((P[:, 0] - x0) / res).astype(np.int64)
    by = np.floor((P[:, 1] - y0) / res).astype(np.int64)
    chunk = max(1, int(2_000_000 // len(ox)))
    for s in range(0, len(P), chunk):
        e = s + chunk
        CX = bx[s:e, None] + ox[None, :]; CY = by[s:e, None] + oy[None, :]
        # exact distance from the real tool position to each cell centre (no snapping)
        d = np.hypot(x0 + CX * res - P[s:e, 0:1], y0 + CY * res - P[s:e, 1:2])
        ok = (CX >= 0) & (CX < nx) & (CY >= 0) & (CY < ny) & (d < R - 1e-4)   # touching a cell centre is not cutting it
        flat = (CY * nx + CX)[ok]
        prof = _profile(tool, d[ok])
        zt = np.broadcast_to(P[s:e, 2:3], d.shape)[ok]
        vals = zt + prof if side > 0 else zt - prof
        rap = (K[s:e] == cam.RAPID)
        if rap.any():                                  # rapids that would cut material
            rows = np.broadcast_to(rap[:, None], d.shape)[ok]
            if rows.any():
                f = flat[rows]; v = vals[rows]
                hi = zhi.ravel()[f]; lo = zlo.ravel()[f]
                mat = hi > lo
                depth = (hi - v) if side > 0 else (v - lo)
                hit = mat & (depth > 0.05)
                if hit.any() and len(rapid_hits) < 50:
                    owner = np.broadcast_to(I[s:e][:, None], d.shape)[ok][rows]
                    j = int(np.argmax(np.where(hit, depth, -1)))
                    rapid_hits.append({"op": op_name, "move": int(owner[j]) + 1, "depth": round(float(depth[j]), 3)})
        if side > 0:
            np.minimum.at(zhi.ravel(), flat, vals)
        else:
            np.maximum.at(zlo.ravel(), flat, vals)


def _part_maps_z(part, res, x0, y0, nx, ny):
    """The part's top and bottom surface heights per cell (-inf / +inf where there is no part)."""
    mesh = _part_mesh(part)
    top = np.full((ny, nx), -np.inf); nbot = np.full((ny, nx), -np.inf)
    for bd in mesh["bodies"]:
        P = np.array(bd["positions"], dtype=float).reshape(-1, 3)
        I = np.array(bd["indices"], dtype=np.int64).reshape(-1, 3)
        cam._raster_triangles(P, I, top, x0, y0, res)
        Q = P.copy(); Q[:, 2] = -Q[:, 2]
        cam._raster_triangles(Q, I, nbot, x0, y0, res)
    return top, -nbot


def _gouge_z(pmaps, zhi, zlo, tol, has_bottom):
    top, bot = pmaps
    has = np.isfinite(top)                               # the part occupies this column
    mat = zhi > zlo
    # ignore the outermost cell ring of the part: an outline cut that runs exactly along the edge is not a gouge
    core = has.copy()
    core[1:, :] &= has[:-1, :]; core[:-1, :] &= has[1:, :]; core[:, 1:] &= has[:, :-1]; core[:, :-1] &= has[:, 1:]
    g_top = core & ((~mat) | (zhi < top - tol))           # cut below the part's top surface (or right through it)
    g_bot = (core & mat & (zlo > bot + tol)) if has_bottom else np.zeros_like(has)
    gouge = g_top | g_bot
    with np.errstate(invalid="ignore"):                   # inf - inf outside the part, never selected
        d_top = np.where(mat, top - zhi, top - bot)
        d_bot = np.where(mat, zlo - bot, 0.0)
        depth = np.where(gouge, np.maximum(np.where(g_top, d_top, 0.0), np.where(g_bot, d_bot, 0.0)), 0.0)
    return gouge, depth, has, mat


def _no_step(H, res):
    """Cells whose 4 neighbours are within max(0.5, 2·res) of their own height (finite heights only)."""
    jump = max(0.5, 2 * res)
    ok = np.isfinite(H)
    with np.errstate(invalid="ignore"):
        for axis, shift in ((0, 1), (0, -1), (1, 1), (1, -1)):
            nb = np.roll(H, shift, axis=axis)
            d = np.abs(nb - H)
            ok &= ~(np.isfinite(nb) & (d > jump))
    return ok


def _compare_zmap(out, pmaps, zhi, zlo, res, x0, y0, nx, tol, has_bottom):
    top, bot = pmaps
    gouge, depth, has, mat = _gouge_z(pmaps, zhi, zlo, tol, has_bottom)
    flat_t, flat_b = _no_step(top, res), _no_step(bot, res)   # a column straddling a wall is not "left" material
    left = has & mat & flat_t & (zhi > top + tol)
    with np.errstate(invalid="ignore"):
        left_amt = np.where(left, zhi - top, 0.0)
        if has_bottom:
            left_b = has & mat & flat_b & (zlo < bot - tol)
            left_amt = np.maximum(left_amt, np.where(left_b, bot - zlo, 0.0))
            left = left | left_b
    outside = (~has) & mat
    cell = res * res
    g = {"cells": int(gouge.sum()), "area_mm2": round(float(gouge.sum() * cell), 2),
         "max_depth": round(float(depth.max()), 3) if gouge.any() else 0.0, "worst": None, "ops": []}
    if gouge.any():
        j = int(np.argmax(depth)); iy, ix = divmod(j, nx)
        g["worst"] = (round(x0 + ix * res, 2), round(y0 + iy * res, 2))
    out.stats["gouge"] = g
    out.stats["left"] = {"max": round(float(left_amt.max()), 3) if left.any() else 0.0, "area_mm2": round(float(left.sum() * cell), 1),
                         "volume_cm3": round(float(left_amt.sum() * cell / 1000.0), 3), "tol": tol}
    out.stats["outside_cm3"] = round(float(np.where(outside, zhi - zlo, 0.0).sum() * cell / 1000.0), 3)
    out.masks = {"gouge": gouge, "left": left}


# ----------------------------------------------------------------------------- radial
def _sim_radial(program, sel, part, resolution, frames, tol) -> SimResult:
    st = program.setup_of(program.ops[sel[0]])
    x0, x1 = st.stock.xmin, st.stock.xmax
    res_x = resolution or max(0.2, (x1 - x0) / 400.0)
    nx = int(math.ceil((x1 - x0) / res_x)) + 1
    na = 360
    res_a = 360.0 / na
    psi = np.radians(np.arange(na) * res_a)
    # initial radius per angle: cylinder, or the stock box's cross-section seen from the axis
    ms = st.model_stock
    if ms.is_cylinder:
        r0 = np.full(na, ms.radius)
    else:
        y0a, z0a = st.axis
        hy0, hy1 = ms.ymin - y0a, ms.ymax - y0a
        hz0, hz1 = ms.zmin - z0a, ms.zmax - z0a
        s_, c_ = np.sin(psi), np.cos(psi)
        with np.errstate(divide="ignore", invalid="ignore"):
            ty = np.where(s_ > 1e-12, hy1 / s_, np.where(s_ < -1e-12, hy0 / s_, np.inf))
            tz = np.where(c_ > 1e-12, hz1 / c_, np.where(c_ < -1e-12, hz0 / c_, np.inf))
        r0 = np.minimum(ty, tz)
    r = np.tile(r0[:, None], (1, nx))
    initial = 0.5 * (r ** 2).sum() * math.radians(res_a) * res_x
    rmax = float(r0.max())
    grid = {"res": res_x, "x0": x0, "nx": nx, "na": na, "res_a": res_a, "rmax": rmax,
            "axis": list(st.axis), "x1": x1}
    out = SimResult("radial", grid)
    total = sum(len(program.ops[i].moves) for i in sel)
    marks = _frame_marks(total, frames)
    mark_i = 0
    done = 0
    rapid_hits = []

    def snap(upto):
        q = np.clip(np.round(r / max(rmax, 1e-9) * 65534), 0, 65534).astype(np.uint16)
        out.frames.append((upto, {"r": zlib.compress(q.tobytes(), 6)}))

    snap(0)
    for i in sel:
        op = program.ops[i]; s_ = program.setup_of(op)
        if not op.moves:
            continue
        tool = op.tool
        mv = [list(m[:5]) + [m[5] if len(m) > 5 else s_.a] for m in op.moves]
        pts, idx, kinds = _samples(mv, min(res_x / 2, 0.25), True)
        start = 0
        while start < len(pts):
            next_mark = marks[mark_i] if mark_i < len(marks) else None
            if next_mark is not None and next_mark - done <= len(op.moves):
                end = int(np.searchsorted(idx, next_mark - done - 1, side="right"))
            else:
                end = len(pts)
            if end <= start:
                end = start + 1
            _stamp_r(r, pts[start:end], kinds[start:end], idx[start:end], tool, res_x, res_a, x0, rapid_hits, op.name)
            start = end
            if next_mark is not None and end_move_done(idx, end, done, next_mark):
                snap(next_mark); mark_i += 1
        done += len(op.moves)
        while mark_i < len(marks) and marks[mark_i] <= done:
            snap(marks[mark_i]); mark_i += 1
    if not out.frames or out.frames[-1][0] != total:
        snap(total)
    remaining = 0.5 * (r ** 2).sum() * math.radians(res_a) * res_x
    out.stats = {"moves": total, "ops": len(sel), "removed_cm3": round((initial - remaining) / 1000.0, 3), "rapid_hits": rapid_hits[:50]}
    if part is not None:
        shape = part.shape if isinstance(part, ck.Model) else part
        xs, angles, R = cam.radial_map(st, shape, res_x, res_a, (x0, x0 + (nx - 1) * res_x))
        R = R[:, :nx] if R.shape[1] >= nx else np.pad(R, ((0, 0), (0, nx - R.shape[1])))
        has = R > 0
        # cells beside a step in the part (shoulders, groove walls, the edges of flats) belong to both sides of the
        # step on this grid: leave them out of the gouge test
        jump = max(0.5, 2 * res_x)
        core = has.copy()
        for axis, shift in ((1, 1), (1, -1), (0, 1), (0, -1)):
            nb = np.roll(R, shift, axis=axis)
            if axis == 1:                                # no wrap along X
                if shift > 0:
                    nb[:, 0] = R[:, 0]
                else:
                    nb[:, -1] = R[:, -1]
            core &= np.abs(nb - R) <= jump
        gouge = core & (r < R - tol)
        left = core & (r > R + tol)                     # material in a corner shows up beside the step, not on it
        cell = np.outer(np.full(na, 1.0), np.full(nx, res_x)) * np.radians(res_a)
        g = {"cells": int(gouge.sum()), "area_mm2": round(float((cell * R)[gouge].sum()), 2),
             "max_depth": round(float((R - r)[gouge].max()), 3) if gouge.any() else 0.0, "worst": None, "ops": []}
        if gouge.any():
            j = int(np.argmax(np.where(gouge, R - r, -1))); ia, ix = divmod(j, nx)
            g["worst"] = {"x": round(x0 + ix * res_x, 2), "angle": round(ia * res_a, 1)}
        out.stats["gouge"] = g
        amt = np.where(left, r - R, 0.0)
        out.stats["left"] = {"max": round(float(amt.max()), 3) if left.any() else 0.0, "area_mm2": round(float((cell * R)[left].sum()), 1),
                             "volume_cm3": round(float((amt * cell * R)[left].sum() / 1000.0), 3), "tol": tol}
        out.masks = {"gouge": gouge, "left": left}
    out.final = {"r": r}
    return out


def _stamp_r(r, P, K, I, tool, res_x, res_a, x0, rapid_hits, op_name):
    for a, b in _runs(K):
        _stamp_r_run(r, P[a:b], K[a:b], I[a:b], tool, res_x, res_a, x0, rapid_hits, op_name)


def _stamp_r_run(r, P, K, I, tool, res_x, res_a, x0, rapid_hits, op_name):
    """P: (n, 4) samples (x, y, z, a) in the setup frame at A = a. Cut each (x, angle) ray the tool covers."""
    na, nx = r.shape
    if len(P) == 0:
        return
    R = tool.radius
    kx = int(math.ceil(R / res_x)) + 1
    dxs = np.arange(-kx, kx + 2) * res_x
    # angular window: how far around the axis the tool reaches from its own direction
    zt_min = max(float(np.min(np.hypot(P[:, 1], P[:, 2]))), R, 0.5)
    ka = int(math.ceil(math.degrees(math.asin(min(1.0, (R + 0.5) / zt_min))) / res_a)) + 2
    ka = min(ka, na // 4)
    das = np.arange(-ka, ka + 1)
    chunk = max(1, int(1_500_000 // (len(dxs) * len(das))))
    for s in range(0, len(P), chunk):
        Q = P[s:s + chunk]
        xt, yt, zt, at = Q[:, 0], Q[:, 1], Q[:, 2], Q[:, 3]
        # part angle psi whose ray points at the tool tip: machine angle phi = atan2(y, z), psi = phi + a
        psi_c = np.degrees(np.arctan2(yt, zt)) + at
        ia = (np.rint(psi_c / res_a).astype(np.int64)[:, None] + das[None, :]) % na              # (n, A)
        phi = np.radians(ia * res_a - at[:, None])                                                # machine angle of each ray
        sphi, cphi = np.sin(phi), np.cos(phi)
        ix = np.floor((xt - x0) / res_x).astype(np.int64)[:, None] + np.arange(-kx, kx + 2)[None, :]   # (n, X)
        dx = (x0 + ix * res_x) - xt[:, None]                       # exact X offset of each cell from the tool
        okx = (ix >= 0) & (ix < nx) & (np.abs(dx) < R - 1e-4)
        rho_x = np.sqrt(np.maximum(R * R - dx * dx, 0.0))
        ball = tool.type == "ball"
        prof_x = 0.0 if ball else _profile(tool, np.minimum(np.abs(dx), R))
        # broadcast to (n, A, X). Tool = cylinder (|y - yt| <= rho at this X) above z_c, plus for a ball the sphere
        # centred R above the tip. The ray is s * (sin phi, cos phi) in machine (y, z).
        z_c = zt[:, None, None] + (R if ball else 0.0) + (prof_x[:, None, :] if not ball else 0.0)
        rh = rho_x[:, None, :]
        sp = sphi[:, :, None]; cp = cphi[:, :, None]
        with np.errstate(divide="ignore", invalid="ignore"):
            s_z = np.where(cp > 1e-9, z_c / cp, np.inf)
            ylo = (yt[:, None, None] - rh); yhi = (yt[:, None, None] + rh)
            t1 = np.where(np.abs(sp) > 1e-9, ylo / sp, np.where((ylo <= 0) & (yhi >= 0), -np.inf, np.inf))
            t2 = np.where(np.abs(sp) > 1e-9, yhi / sp, np.where((ylo <= 0) & (yhi >= 0), np.inf, -np.inf))
        s_lo = np.maximum(np.minimum(t1, t2), 0.0)
        s_hi = np.maximum(t1, t2)
        s1 = np.maximum(s_z, s_lo)
        valid_c = (cp > 1e-9) & (s1 < s_hi)
        if ball:
            # sphere: |(dx, s sin - yt, s cos - zc)|^2 <= R^2  ->  s^2 - 2 b s + c <= 0
            yt3 = yt[:, None, None]; zc3 = z_c
            bq = yt3 * sp + zc3 * cp
            cq = (dx * dx)[:, None, :] + yt3 * yt3 + zc3 * zc3 - R * R
            disc = bq * bq - cq
            ok_s = disc > 0
            sq = np.sqrt(np.maximum(disc, 0.0))
            sa, sb = bq - sq, bq + sq
            # union of the sphere's and the shank's intervals (they join at the sphere's equator)
            s1 = np.where(ok_s & valid_c, np.minimum(sa, s1), np.where(ok_s, sa, s1))
            s_hi = np.where(ok_s & valid_c, np.maximum(sb, s_hi), np.where(ok_s, sb, s_hi))
            valid = (valid_c | (ok_s & (sb > 0))) & okx[:, None, :]
            s1 = np.maximum(s1, 0.0)
        else:
            valid = valid_c & okx[:, None, :]
        if not valid.any():
            continue
        IA = np.broadcast_to(ia[:, :, None], valid.shape)[valid]
        IX = np.broadcast_to(np.clip(ix, 0, nx - 1)[:, None, :], valid.shape)[valid]
        S1 = s1[valid]; SH = s_hi[valid]
        cur = r[IA, IX]
        reach = SH >= cur - 1e-3                                                                   # tool covers out to the surface
        cut = reach & (S1 < cur)
        rap = np.broadcast_to((K[s:s + chunk] == cam.RAPID)[:, None, None], valid.shape)[valid]
        hit = cut & rap & (cur - S1 > 0.05)
        if hit.any() and len(rapid_hits) < 50:
            owner = np.broadcast_to(I[s:s + chunk][:, None, None], valid.shape)[valid][hit]
            depth = (cur - S1)[hit]
            j = int(np.argmax(depth))
            rapid_hits.append({"op": op_name, "move": int(owner[j]) + 1, "depth": round(float(depth[j]), 3)})
        if cut.any():
            np.minimum.at(r, (IA[cut], IX[cut]), S1[cut])


# ----------------------------------------------------------------------------- machine collisions (holder / nose / head / table)
def tool_stickout(tool: cam.Tool) -> float:
    """How far the tool projects below the nose/nut face: flute length plus the shank that clears the collet (estimated)."""
    return float(getattr(tool, "stickout", 0) or 0) or (float(tool.flute_length) + 6.0)


def _collision_stats(hits: list[dict]) -> dict:
    if not hits:
        return {"count": 0, "max_depth": 0.0, "hits": []}
    # one entry per (op, part, what) keeps the worst depth; the list is sorted deepest first
    best: dict[tuple, dict] = {}
    for h in hits:
        k = (h["op"], h["part"], h["what"])
        if k not in best or h["depth"] > best[k]["depth"]:
            best[k] = h
    return {"count": len(hits), "max_depth": max(h["depth"] for h in hits), "hits": sorted(best.values(), key=lambda h: -h["depth"])[:50],
            "all": sorted(hits, key=lambda h: -h["depth"])[:400]}


def holder_levels(machine_model, tool: cam.Tool) -> list[tuple[str, float, float, float]]:
    """(name, radius, bottom above the tool tip, height) for everything above the flutes: the tool shank, then the
    machine's nose profile (collet nut, nose, collar, head)."""
    stick = tool_stickout(tool)
    ov = getattr(machine_model, "stickout", None)
    if ov:
        stick = max(float(ov), float(tool.flute_length))
    levels = [("tool shank", tool.radius, float(tool.flute_length), max(stick - float(tool.flute_length), 0.0))]
    z = stick
    for lvl in machine_model.nose:
        levels.append((lvl["name"], float(lvl["r"]), z, float(lvl["h"])))
        z += float(lvl["h"])
    return levels


def _pool_max(zhi: np.ndarray, c: int) -> np.ndarray:
    """Block-max of zhi over c×c cells (padded with -inf)."""
    ny, nx = zhi.shape
    py, px = (-ny) % c, (-nx) % c
    Z = np.pad(zhi, ((0, py), (0, px)), constant_values=-np.inf)
    return Z.reshape(Z.shape[0] // c, c, Z.shape[1] // c, c).max(axis=(1, 3))


def _collide_z(hits, zhi, P, I, done, tool, mm, res, x0, y0, op_name, stock, placement):
    """After a piece of moves is stamped: does any level of the holder stack dip below the remaining stock around it, or
    does the tool tip go below the table (through the spoilboard)? Sampled every r/2 along the path."""
    if len(P) == 0:
        return
    levels = holder_levels(mm, tool)
    ny, nx = zhi.shape
    spoil = float(mm.spoilboard) + float(placement.get("fixture", 0.0))
    table_z = stock.zmin - float(placement.get("fixture", 0.0))      # table top in model Z (stock sits on it)
    for name, r, zb_off, h in levels:
        if r <= tool.radius + 1e-6 and name == "tool shank":
            continue                                                   # the flutes clear their own radius
        c = max(1, int(r / (2 * res)))                                 # coarse cells ~ r/2
        coarse = _pool_max(zhi, c)
        step = max(1, int((r / 2) / (res / 2)))                        # sample spacing ≤ r/2 (samples are res/2 apart)
        rc = int(math.ceil(r / res))
        for k in range(0, len(P), step):
            x, y, z = P[k]
            zb = z + zb_off
            ix, iy = int(round((x - x0) / res)), int(round((y - y0) / res))
            cx0, cx1 = max(0, (ix - rc) // c), min(coarse.shape[1] - 1, (ix + rc) // c)
            cy0, cy1 = max(0, (iy - rc) // c), min(coarse.shape[0] - 1, (iy + rc) // c)
            if cx0 > cx1 or cy0 > cy1:
                continue
            if coarse[cy0:cy1 + 1, cx0:cx1 + 1].max() <= zb + 0.02:
                continue
            # exact: cells within r of the axis, above the level's bottom
            xa, xb = max(0, ix - rc), min(nx - 1, ix + rc); ya, yb = max(0, iy - rc), min(ny - 1, iy + rc)
            sub = zhi[ya:yb + 1, xa:xb + 1]
            yy, xx = np.mgrid[ya:yb + 1, xa:xb + 1]
            d2 = ((xx - (x - x0) / res) ** 2 + (yy - (y - y0) / res) ** 2) * res * res
            mask = (d2 <= r * r) & (d2 > tool.radius * tool.radius) & (sub > zb + 0.02) & np.isfinite(sub)
            if mask.any():
                depth = float((sub[mask] - zb).max())
                hits.append({"op": op_name, "move": int(I[k]), "pos": [round(float(x), 2), round(float(y), 2), round(float(z), 2)],
                             "part": name, "what": "stock", "depth": round(depth, 2)})
                break                                                  # one hit per level per piece is enough to report
    # the tool tip through the spoilboard into the table
    zmin = float(P[:, 2].min())
    if zmin < table_z - spoil - 0.02:
        k = int(np.argmin(P[:, 2]))
        hits.append({"op": op_name, "move": int(I[k]), "pos": [round(float(P[k, 0]), 2), round(float(P[k, 1]), 2), round(zmin, 2)],
                     "part": "tool", "what": "table" if spoil <= 0 else "bed under the spoilboard", "depth": round(table_z - spoil - zmin, 2)})


def _collide_rotary(out, program, sel, mm, placement):
    """4th-axis setups: the holder stack against the chuck face and the tailstock (the stock is handled radially)."""
    rot = mm.rotary
    if not rot:
        return
    hits = []
    st = program.setup_of(program.ops[sel[0]])
    stock = st.model_stock
    dx = float(placement.get("x", 0.0))
    # stock placement along the rotary: its xmin sits `gap` from the chuck jaws unless a placement says otherwise
    gap = float(placement.get("chuck_gap", 5.0))
    chuck_face_model = stock.xmin - gap
    tail_model = chuck_face_model + (rot["tail_x"] - rot["chuck_face_x"])
    for i in sel:
        op = program.ops[i]
        if not op.moves:
            continue
        levels = holder_levels(mm, op.tool)
        P = st.to_model(np.array([m[1:4] for m in op.moves], dtype=float))
        for name, r, zb_off, h in levels:
            if name == "tool shank":
                continue
            # chuck: a disc of radius chuck_r on the axis; the level is a cylinder of radius r from zb up h along the tool axis
            near = P[:, 0] - r < chuck_face_model + dx
            if near.any():
                k = int(np.argmax(near)); hits.append({"op": op.name, "move": k, "pos": [round(float(v), 2) for v in P[k]], "part": name, "what": "chuck", "depth": round(float(chuck_face_model + dx - (P[k, 0] - r)), 2)})
                break
            near = P[:, 0] + r > tail_model + dx
            if near.any():
                k = int(np.argmax(near)); hits.append({"op": op.name, "move": k, "pos": [round(float(v), 2) for v in P[k]], "part": name, "what": "tailstock", "depth": round(float((P[k, 0] + r) - (tail_model + dx)), 2)})
                break
    out.stats["collisions"] = _collision_stats(hits)
    out.stats["machine"] = {"key": mm.key, "name": mm.name, "placement": placement}




# ----------------------------------------------------------------------------- fixtures (vise jaws, clamps, plates: uncuttable)
def _fixture_shapes(setups):
    """Collision shapes of the top setup's fixture in model coordinates (the stock's bottom-centre sits at the work origin)."""
    st = setups[0]
    fm = st.fixture_model() if hasattr(st, "fixture_model") else None
    if fm is None or st.orient != "top":
        return []
    import fixture_lib
    ms = st.model_stock
    bc = ((ms.xmin + ms.xmax) / 2, (ms.ymin + ms.ymax) / 2, ms.zmin)
    return [fixture_lib.transform_to_model(p.shape, st.fixture_rot, bc, fm.work_origin) for p in fm.parts if p.collision]


def _fixture_map(shapes, res, x0, y0, nx, ny):
    """Top-surface height of the fixture per cell (-inf where there is none)."""
    from build123d import Compound
    top = np.full((ny, nx), -np.inf)
    mesh = _part_mesh(Compound(shapes))
    for bd in mesh["bodies"]:
        P = np.array(bd["positions"], dtype=float).reshape(-1, 3)
        I = np.array(bd["indices"], dtype=np.int64).reshape(-1, 3)
        cam._raster_triangles(P, I, top, x0, y0, res)
    return top


def _collide_fixture(hits, fix_hi, P, K, I, tool, mm, res, x0, y0, op_name, fixture_name):
    """The tool (flutes included) and the holder stack must stay clear of the fixture: any contact is a collision."""
    if len(P) == 0:
        return
    levels = [("tool", float(tool.radius), 0.0, float(tool.flute_length))] + (holder_levels(mm, tool) if mm is not None else [])
    ny, nx = fix_hi.shape
    for name, r, zb_off, h in levels:
        c = max(1, int(r / (2 * res)))
        coarse = _pool_max(fix_hi, c)
        step = max(1, int(r / res))
        rc = int(math.ceil(r / res))
        for k in range(0, len(P), step):
            x, y, z = P[k]; zb = z + zb_off
            ix, iy = int(round((x - x0) / res)), int(round((y - y0) / res))
            cx0, cx1 = max(0, (ix - rc) // c), min(coarse.shape[1] - 1, (ix + rc) // c)
            cy0, cy1 = max(0, (iy - rc) // c), min(coarse.shape[0] - 1, (iy + rc) // c)
            if cx0 > cx1 or cy0 > cy1 or coarse[cy0:cy1 + 1, cx0:cx1 + 1].max() <= zb + 0.02:
                continue
            xa, xb = max(0, ix - rc), min(nx - 1, ix + rc); ya, yb = max(0, iy - rc), min(ny - 1, iy + rc)
            sub = fix_hi[ya:yb + 1, xa:xb + 1]
            yy, xx = np.mgrid[ya:yb + 1, xa:xb + 1]
            d2 = ((xx - (x - x0) / res) ** 2 + (yy - (y - y0) / res) ** 2) * res * res
            mask = (d2 <= r * r) & (sub > zb + 0.02) & np.isfinite(sub)
            if mask.any():
                hits.append({"op": op_name, "move": int(I[k]), "pos": [round(float(x), 2), round(float(y), 2), round(float(z), 2)],
                             "part": name, "what": f"fixture ({fixture_name})", "depth": round(float((sub[mask] - zb).max()), 2),
                             "rapid": bool(K[k] == 0) if hasattr(K, "__len__") else False})
                break
