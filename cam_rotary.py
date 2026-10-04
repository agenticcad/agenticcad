"""4th-axis (rotary A about X) toolpaths for AgenticCAD CAM.

Frame: a rotary Setup's frame has the rotary axis on X (Y = Z = 0). The tool points down -Z and stays at Y = 0 for
continuous ops (its axis passes through the rotary axis); A turns the part. Moves are [kind, x, y, z, feed, a].

The core is a radial map of the part: R[i_a, i_x] = how far the part's surface is from the axis in the direction that
faces the spindle when A = a. A tool-tip map T then says how high the tool tip must sit at (x, a) so the tool (flat or
ball, with its real footprint across neighbouring angles) touches but never cuts the part.

    rotary_rough(setup, tool, part, stepdown=1, stepover=None, mode="rings"|"lines", leave=0.3)
    rotary_finish(setup, tool, part, stepover=None, mode="lines"|"rings"|"spiral")
    rotary_wrap(setup, tool, polys, radius, z_bottom, kind="pocket"|"contour", ...)   flat 2D geometry wrapped on a cylinder
"""
from __future__ import annotations

import math

import numpy as np

import cad_kernel as ck
import cam_kernel as cam
from cam_kernel import FEED, PLUNGE, RAPID, CamError, Op, PathBuilder, Setup, Tool

COLORS = {"rotary_rough": "#ff9f43", "rotary_finish": "#c58cff", "rotary_wrap": "#ff9f7a"}


def _need_rotary(setup: Setup, what: str) -> None:
    if not setup.rotary:
        raise CamError(f"{what} needs a rotary setup: Setup(machine_with_4th_axis, stock, rotary=True)")


# ----------------------------------------------------------------------------- radial map
def radial_map(setup: Setup, shape, res_x: float = 0.25, res_a: float = 1.0, x_range: tuple[float, float] | None = None):
    """Part surface distance from the rotary axis facing the spindle, sampled on (A, X).
    Returns (xs, angles_deg, R) with R shape (n_a, n_x); 0 where there is no part."""
    _need_rotary(setup, "radial_map")
    view = setup.view(shape, a=0.0)
    tmp = ck.build_model(ck.coerce_bodies(view), "", "fine")
    x0, x1 = x_range or (setup.stock.xmin, setup.stock.xmax)
    nx = int(math.ceil((x1 - x0) / res_x)) + 1
    na = int(round(360.0 / res_a))
    res_a = 360.0 / na
    R = np.zeros((na, nx), dtype=np.float64)
    for bd in tmp.mesh["bodies"]:
        P = np.array(bd["positions"], dtype=np.float64).reshape(-1, 3)
        I = np.array(bd["indices"], dtype=np.int64).reshape(-1, 3)
        P, I = _subdivide_polar(P, I, max_deg=2.0 * res_a, max_dx=1e9)      # r is linear along X on a face: split by angle only
        # (x, a, r): a = angle that brings this point to the top = atan2(y, z) in the A = 0 frame
        ang = np.degrees(np.arctan2(P[:, 1], P[:, 2])) % 360.0
        rad = np.hypot(P[:, 1], P[:, 2])
        Q = np.stack([P[:, 0], ang, rad], axis=1)
        tri_a = ang[I]
        span = tri_a.max(axis=1) - tri_a.min(axis=1)
        normal = I[span <= 180.0]
        seam = I[span > 180.0]
        _raster(Q, normal, R, x0, res_x, res_a)
        if len(seam):                                       # triangles across 0/360: rasterise shifted copies
            for shift in (-360.0, 360.0):
                Qs = Q.copy()
                Qs[:, 1] = np.where(Qs[:, 1] > 180.0, Qs[:, 1] + (shift if shift < 0 else 0.0),
                                    Qs[:, 1] + (shift if shift > 0 else 0.0))
                _raster(Qs, seam, R, x0, res_x, res_a)
    xs = x0 + np.arange(nx) * res_x
    angles = np.arange(na) * res_a
    return xs, angles, R


def _subdivide_polar(P: np.ndarray, I: np.ndarray, max_deg: float, max_dx: float, cap: int = 48):
    """Split triangles so none spans more than `max_deg` around the axis: a flat face is not linear in (x, a, r), so a
    big flat triangle rasterised in polar coordinates would lose the dip of the flat towards the axis."""
    ang = np.degrees(np.arctan2(P[:, 1], P[:, 2]))
    outP = [P]
    outI = []
    base = len(P)
    tri_a = ang[I]
    span = tri_a.max(axis=1) - tri_a.min(axis=1)
    span = np.where(span > 180.0, 360.0 - span, span)
    tri_x = P[I][:, :, 0]
    dx = tri_x.max(axis=1) - tri_x.min(axis=1)
    n_all = np.clip(np.maximum(np.ceil(span / max_deg), np.ceil(dx / max_dx)), 1, cap).astype(int)
    keep = I[n_all == 1]
    outI.append(keep)
    for t, n in zip(I[n_all > 1], n_all[n_all > 1]):
        A, B, C = P[t[0]], P[t[1]], P[t[2]]
        ii, jj = np.meshgrid(np.arange(n + 1), np.arange(n + 1))
        m = (ii + jj) <= n
        ii, jj = ii[m], jj[m]
        pts = A + (B - A) * (ii / n)[:, None] + (C - A) * (jj / n)[:, None]
        index = -np.ones((n + 1, n + 1), dtype=np.int64)
        index[ii, jj] = base + np.arange(len(ii))
        tris = []
        for i in range(n):
            for j in range(n - i):
                tris.append((index[i, j], index[i + 1, j], index[i, j + 1]))
                if i + j + 1 < n:
                    tris.append((index[i + 1, j], index[i + 1, j + 1], index[i, j + 1]))
        outP.append(pts)
        outI.append(np.array(tris, dtype=np.int64))
        base += len(pts)
    return np.concatenate(outP), np.concatenate([x for x in outI if len(x)]) if outI else I


def _raster(Q: np.ndarray, I: np.ndarray, R: np.ndarray, x0: float, res_x: float, res_a: float) -> None:
    """Max-rasterise triangles of (x, a, r) into R[a, x] (a wraps every 360 degrees)."""
    na, nx = R.shape
    for tri in I:
        a, b, c = Q[tri[0]], Q[tri[1]], Q[tri[2]]
        ix0 = max(int(math.floor((min(a[0], b[0], c[0]) - x0) / res_x)), 0)
        ix1 = min(int(math.ceil((max(a[0], b[0], c[0]) - x0) / res_x)), nx - 1)
        ia0 = int(math.floor(min(a[1], b[1], c[1]) / res_a))
        ia1 = int(math.ceil(max(a[1], b[1], c[1]) / res_a))
        if ix1 < ix0 or ia1 < ia0:
            continue
        xs = x0 + np.arange(ix0, ix1 + 1) * res_x
        As = np.arange(ia0, ia1 + 1) * res_a
        X, A = np.meshgrid(xs, As)
        d = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
        if abs(d) < 1e-12:
            continue
        l1 = ((b[1] - c[1]) * (X - c[0]) + (c[0] - b[0]) * (A - c[1])) / d
        l2 = ((c[1] - a[1]) * (X - c[0]) + (a[0] - c[0]) * (A - c[1])) / d
        l3 = 1 - l1 - l2
        m = (l1 >= -1e-6) & (l2 >= -1e-6) & (l3 >= -1e-6)
        if not m.any():
            continue
        Z = l1 * a[2] + l2 * b[2] + l3 * c[2]
        rows = np.arange(ia0, ia1 + 1) % na
        cols = np.arange(ix0, ix1 + 1)
        for k, row in enumerate(rows):
            sub = R[row, cols]
            R[row, cols] = np.maximum(sub, np.where(m[k], Z[k], -np.inf))


def _dilate(R: np.ndarray) -> np.ndarray:
    """Each cell as high as its tallest neighbour (X and angle): a wall or edge between two samples is then never
    under-estimated, so toolpaths stay clear of it (they may leave up to one cell of material instead)."""
    D = R.copy()
    D[:, 1:] = np.maximum(D[:, 1:], R[:, :-1]); D[:, :-1] = np.maximum(D[:, :-1], R[:, 1:])
    D = np.maximum(D, np.maximum(np.roll(D, 1, axis=0), np.roll(D, -1, axis=0)))
    return D


def tip_map(R: np.ndarray, xs: np.ndarray, angles: np.ndarray, tool: Tool) -> np.ndarray:
    """Lowest tool-tip height (setup Z, tool on the Y = 0 plane pointing at the axis) at each (a, x) that keeps the
    tool clear of the part, using the real footprint: a surface point at angle offset d and radius r sits at
    (y, z) = (r sin d, r cos d) under the tool."""
    na, nx = R.shape
    res_x = float(xs[1] - xs[0]) if nx > 1 else 1.0
    res_a = float(angles[1] - angles[0]) if na > 1 else 1.0
    rt = tool.radius
    T = np.zeros_like(R)
    kx = int(math.ceil(rt / res_x))
    r_pos = R[R > 0]
    r_min = max(float(r_pos.min()) if r_pos.size else rt, rt, 0.5)
    ka = int(math.ceil(math.degrees(math.asin(min(1.0, rt / r_min))) / res_a)) + 1
    for i in range(-ka, ka + 1):
        d = math.radians(i * res_a)
        if abs(d) >= math.pi / 2:
            continue
        Rs = np.roll(R, -i, axis=0)                     # value at a + i
        ys = Rs * math.sin(d)
        zs = Rs * math.cos(d)
        for j in range(-kx, kx + 1):
            dx = j * res_x
            if j > 0:
                Ys = np.pad(ys[:, j:], ((0, 0), (0, j)), constant_values=0.0)
                Zs = np.pad(zs[:, j:], ((0, 0), (0, j)), constant_values=0.0)
            elif j < 0:
                Ys = np.pad(ys[:, :j], ((0, 0), (-j, 0)), constant_values=0.0)
                Zs = np.pad(zs[:, :j], ((0, 0), (-j, 0)), constant_values=0.0)
            else:
                Ys, Zs = ys, zs
            dist2 = dx * dx + Ys * Ys
            inside = dist2 <= rt * rt + 1e-9
            if tool.type == "ball":
                prof = rt - np.sqrt(np.maximum(rt * rt - dist2, 0.0))
            else:
                prof = 0.0
            cand = np.where(inside, Zs - prof, -np.inf)
            np.maximum(T, cand, out=T)
    return T


def _interp_row(T: np.ndarray, angles: np.ndarray, a_deg: float) -> np.ndarray:
    na = len(angles)
    res_a = 360.0 / na
    f = (a_deg % 360.0) / res_a
    i0 = int(math.floor(f)) % na
    if f - math.floor(f) < 1e-9:
        return T[i0]
    # conservative: never below either neighbouring row (linear blending lets the tool dip into steps and edges)
    return np.maximum(T[i0], T[(i0 + 1) % na])


# ----------------------------------------------------------------------------- toolpaths
def _rb(setup, tool, op):
    pb = PathBuilder(setup, tool, op)
    pb.cur_a = None
    return pb


def _rapid_to(pb: PathBuilder, x: float, a: float) -> None:
    """Retract to safe height (clear of the swept stock), turn A, move X."""
    s = pb.setup.safe_z
    if pb.pos is not None and pb.pos[2] < s - 1e-6:
        pb._add(RAPID, pb.pos[0], 0.0, s, 0.0, pb.cur_a)
    pb._add(RAPID, x, 0.0, s, 0.0, a)
    pb.cur_a = a


def _feed(pb: PathBuilder, x: float, z: float, a: float, f: float | None = None, kind=FEED) -> None:
    pb._add(kind, x, 0.0, z, f or pb.feed, a)
    pb.cur_a = a


def _x_limits(setup: Setup, tool: Tool, x_range, shape=None):
    """Default: the part's length (stock beyond the part's ends is usually in the chuck or left as a support)."""
    if x_range:
        return float(x_range[0]), float(x_range[1])
    x0, x1 = setup.stock.xmin, setup.stock.xmax
    if shape is not None:
        bb = setup.view(shape, a=0.0).bounding_box()
        x0, x1 = max(x0, bb.min.X), min(x1, bb.max.X)
    return x0, x1


def rotary_rough(setup: Setup, tool: Tool, shape=None, model=None, stepdown: float | None = None, stepover: float | None = None,
                 mode: str = "rings", leave: float = 0.3, x_range: tuple[float, float] | None = None,
                 res_x: float | None = None, res_a: float = 2.0, name: str = "Rotary rough") -> Op:
    """Rough a part on the 4th axis from the stock's swing radius down, `stepdown` per level, leaving `leave` mm.
    mode="rings": at each X step, A turns a full revolution (continuous 4-axis, Z follows the part);
    mode="lines": at each A step, cut along X (A indexed per pass). Cuts only where material is left from the
    previous level."""
    _need_rotary(setup, "rotary_rough")
    shape = shape if shape is not None else (model.shape if model is not None else None)
    if shape is None:
        raise CamError("rotary_rough: give shape= (or model=)")
    x0, x1 = _x_limits(setup, tool, x_range, shape)
    rx = res_x or max(0.1, min(tool.diameter / 12, 0.25))      # fine: walls are widened by one cell for safety
    xs, angles, R = radial_map(setup, shape, rx, res_a, (x0 - tool.radius, x1 + tool.radius))
    T = tip_map(_dilate(R), xs, angles, tool) + leave
    sd = stepdown or tool.stepdown
    so = (stepover or tool.stepover) * tool.diameter
    top = setup.max_radius
    inx = (xs >= x0 - 1e-9) & (xs <= x1 + 1e-9)
    floor = float(max(T[:, inx].min(), 0.0))
    levels = []
    z = top - sd
    while z > floor + 1e-6:
        levels.append(z)
        z -= sd
    levels.append(floor)
    op = Op(name, "rotary_rough", tool, color=COLORS["rotary_rough"])
    pb = _rb(setup, tool, op)
    prev = top
    xi = np.where(inx)[0]
    for L in levels:
        Z = np.maximum(T, L)
        if mode == "rings":
            n_x = max(1, int(math.ceil((x1 - x0) / so)) + 1)
            for xc in np.linspace(x0, x1, n_x):
                j = int(np.clip(round((xc - xs[0]) / rx), 0, len(xs) - 1))
                if (Z[:, j] >= prev - 1e-3).all():
                    continue                                # nothing left at this level here
                a_step = max(1.0, min(5.0, math.degrees(so / max(L, so)) / 2))
                n = int(math.ceil(360.0 / a_step))
                start = pb.cur_a or 0.0                     # A is absolute: keep turning the same way, never unwind
                aa = start + np.linspace(0.0, 360.0, n + 1)
                zz = np.array([_interp_row(Z, angles, a)[j] for a in aa])
                _rapid_to(pb, float(xc), float(start))
                _feed(pb, float(xc), float(zz[0]), float(start), pb.plunge, PLUNGE)
                for a, zv in zip(aa[1:], zz[1:]):
                    _feed(pb, float(xc), float(zv), float(a))
        else:
            a_step = math.degrees(so / max(L, so))
            n = max(4, int(math.ceil(360.0 / a_step)))
            for k in range(n):
                a = 360.0 * k / n
                zrow = _interp_row(Z, angles, a)
                cut = (zrow < prev - 1e-3) & inx
                cut = cut[xi]
                if not cut.any():
                    continue
                runs = np.split(xi[cut], np.where(np.diff(xi[cut]) > 1)[0] + 1)
                for run in runs:
                    if len(run) == 0:
                        continue
                    _rapid_to(pb, float(xs[run[0]]), a)
                    _feed(pb, float(xs[run[0]]), float(zrow[run[0]]), a, pb.plunge, PLUNGE)
                    for jj in run[1:]:
                        _feed(pb, float(xs[jj]), float(zrow[jj]), a)
        prev = L
    if pb.pos is not None:
        pb._add(RAPID, pb.pos[0], 0.0, setup.safe_z, 0.0, pb.cur_a)
    op.params = dict(levels=len(levels), stepdown=sd, stepover=round(so, 3), mode=mode, leave=leave)
    return op


def rotary_finish(setup: Setup, tool: Tool, shape=None, model=None, stepover: float | None = None, mode: str = "lines",
                  x_range: tuple[float, float] | None = None, leave: float = 0.0, res_x: float | None = None,
                  res_a: float = 1.0, name: str = "Rotary finish") -> Op:
    """Finish a part on the 4th axis by following its surface (tool tip from the radial map and the tool's footprint).
    mode="lines": passes along X, A indexed between passes (zig-zag, A turns while the tool is lifted clear);
    mode="rings": a full A revolution at each X step;
    mode="spiral": one continuous helix, X advancing `stepover` per revolution (true simultaneous 4-axis).
    stepover is a fraction of the tool diameter (default 0.15 for ball, tool.stepover otherwise), measured on the
    part's largest radius for lines."""
    _need_rotary(setup, "rotary_finish")
    shape = shape if shape is not None else (model.shape if model is not None else None)
    if shape is None:
        raise CamError("rotary_finish: give shape= (or model=)")
    x0, x1 = _x_limits(setup, tool, x_range, shape)
    so = (stepover or (0.15 if tool.type == "ball" else tool.stepover)) * tool.diameter
    rx = res_x or max(0.05, min(tool.diameter / 30, so / 2, 0.1))
    xs, angles, R = radial_map(setup, shape, rx, res_a, (x0 - tool.radius, x1 + tool.radius))
    T = tip_map(_dilate(R), xs, angles, tool) + leave
    T = np.maximum(T, 0.0)
    r_part = float(R.max()) if R.size else setup.max_radius
    op = Op(name, "rotary_finish", tool, color=COLORS["rotary_finish"])
    pb = _rb(setup, tool, op)
    sel = (xs >= x0 - 1e-9) & (xs <= x1 + 1e-9)
    xi = np.where(sel)[0]

    def z_at(x: float, a: float) -> float:
        row = _interp_row(T, angles, a)
        f = (x - xs[0]) / rx
        i0 = int(np.clip(math.floor(f), 0, len(xs) - 1))
        i1 = min(i0 + 1, len(xs) - 1)
        return float(row[i0]) if abs(f - round(f)) < 1e-9 else float(max(row[i0], row[i1]))   # conservative, as above

    if mode == "lines":
        n = max(8, int(math.ceil(360.0 / math.degrees(so / max(r_part, so)))))
        direction = 1
        for k in range(n):
            a = 360.0 * k / n
            row = _interp_row(T, angles, a)
            idx = xi if direction > 0 else xi[::-1]
            pts = [(float(xs[j]), 0.0, float(row[j])) for j in idx]
            pts = cam._simplify_z(pts, 0.002)
            if pb.pos is None:
                _rapid_to(pb, pts[0][0], a)
                _feed(pb, pts[0][0], pts[0][2], a, pb.plunge, PLUNGE)
            else:
                lift = max(pb.pos[2], pts[0][2]) + 0.5        # clear both ends while A turns
                _feed(pb, pb.pos[0], lift, pb.cur_a)
                _feed(pb, pts[0][0], lift, a)
                _feed(pb, pts[0][0], pts[0][2], a, pb.plunge, PLUNGE)
            for x, _, z in pts[1:]:
                _feed(pb, x, z, a)
            direction *= -1
    elif mode == "rings":
        n_x = max(1, int(math.ceil((x1 - x0) / so)) + 1)
        a_step = max(0.5, min(3.0, math.degrees(rx / max(r_part, rx))))
        for xc in np.linspace(x0, x1, n_x):
            start = pb.cur_a or 0.0
            aa = start + np.arange(0.0, 360.0 + 1e-9, a_step)
            if pb.pos is None:
                _rapid_to(pb, float(xc), float(start))
                _feed(pb, float(xc), z_at(xc, start), float(start), pb.plunge, PLUNGE)
            else:
                lift = max(pb.pos[2], z_at(xc, start)) + 0.5
                _feed(pb, pb.pos[0], lift, pb.cur_a)
                _feed(pb, float(xc), lift, float(start))
                _feed(pb, float(xc), z_at(xc, start), float(start), pb.plunge, PLUNGE)
            for a in aa[1:]:
                _feed(pb, float(xc), z_at(xc, a), float(a))
    elif mode == "spiral":
        turns = max(1.0, (x1 - x0) / so)
        a_step = max(0.5, min(3.0, math.degrees(rx / max(r_part, rx))))
        aa = np.arange(0.0, 360.0 * turns + 1e-9, a_step)
        _rapid_to(pb, x0, 0.0)
        _feed(pb, x0, z_at(x0, 0.0), 0.0, pb.plunge, PLUNGE)
        for a in aa[1:]:
            x = x0 + (x1 - x0) * a / (360.0 * turns)
            _feed(pb, float(x), z_at(x, a), float(a))
    else:
        raise CamError("rotary_finish mode must be lines | rings | spiral")
    if pb.pos is not None:
        pb._add(RAPID, pb.pos[0], 0.0, setup.safe_z, 0.0, pb.cur_a)
    op.params = dict(mode=mode, stepover=round(so, 3), grid=[len(xs), len(angles)], leave=leave)
    return op


def rotary_wrap(setup: Setup, tool: Tool, polys, radius: float | None = None, z_bottom: float | None = None,
                depth: float | None = None, kind: str = "pocket", side: str = "inside", stepdown: float | None = None,
                stepover: float | None = None, name: str = "Wrapped", **kw) -> Op:
    """Wrap flat 2D geometry onto a cylinder of `radius` (default: the stock's): u = X along the axis, v = distance
    around the circumference (v = 0 faces the spindle at A = 0; A = v / radius in degrees). Cut to radius
    `z_bottom` (or `depth` below the surface) with a pocket or a contour (side inside/outside/on). Straight lines
    in (u, v) become simultaneous X + A moves. The tool stays radial at the contact point; flat tools on small
    radii cut slightly deeper at their edges (r - sqrt(r^2 - R^2))."""
    _need_rotary(setup, "rotary_wrap")
    r = radius or setup.max_radius
    zb = z_bottom if z_bottom is not None else r - (depth if depth is not None else 0.5)
    polys_l = cam._polys(polys)
    if not polys_l:
        raise CamError("rotary_wrap: no geometry")
    from shapely.ops import unary_union
    u0, v0, u1, v1 = unary_union(polys_l).bounds
    flat_stock = cam.Stock(min(u0, setup.stock.xmin) - tool.diameter, max(u1, setup.stock.xmax) + tool.diameter,
                           v0 - tool.diameter, v1 + tool.diameter, zb - 1.0, r)
    flat = Setup(setup.machine, flat_stock, origin="model-origin", safe_z=setup.safe_z, clearance_z=setup.clearance_z,
                 name=setup.name + " (unrolled)")
    if kind == "pocket":
        base = cam.pocket(flat, tool, polys, z_top=r, z_bottom=zb, stepdown=stepdown, stepover=stepover, name=name, **kw)
    elif kind == "contour":
        base = cam.contour(flat, tool, polys, z_top=r, z_bottom=zb, side=side, stepdown=stepdown, name=name, **kw)
    else:
        raise CamError("rotary_wrap kind must be pocket | contour")
    op = Op(name, "rotary_wrap", tool, color=COLORS["rotary_wrap"], params=dict(base.params, radius=r, z_bottom=zb, kind=kind),
            warnings=list(base.warnings))
    op.setup = setup
    for m in base.moves:
        k, u, v, z, f = m[:5]
        a = math.degrees(v / r)
        z = setup.safe_z if k == RAPID and z >= flat.safe_z - 1e-6 else z
        op.moves.append([k, u, 0.0, z, f, round(a, 4)])
    if tool.type == "flat" and tool.radius / r > 0.15:
        op.warnings.append(f"flat Ø{tool.diameter} on a {2 * r:.1f} mm cylinder cuts {r - math.sqrt(max(r * r - tool.radius ** 2, 0)):.2f} mm "
                           "deeper at its edges: consider a ball or V tool, or a smaller cutter")
    return op
