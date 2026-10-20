"""4th-axis (rotary A about X) toolpaths for AgenticCAD CAM.

Frame: a rotary Setup's frame has the rotary axis on X (Y = Z = 0). The tool points down -Z and stays at Y = 0 for
continuous ops (its axis passes through the rotary axis); A turns the part. Moves are [kind, x, y, z, feed, a].

The core is a radial map of the part: R[i_a, i_x] = how far the part's surface is from the axis in the direction that
faces the spindle when A = a. A tool-tip map T then says how high the tool tip must sit at (x, a) so the tool (flat or
ball, with its real footprint across neighbouring angles) touches but never cuts the part.

    rotary_rough(setup, tool, part, stepdown=1, stepover=None, mode="rings"|"lines", leave=0.3)
    rotary_finish(setup, tool, part, stepover=None, mode="lines"|"rings"|"spiral")
    rotary_wrap(setup, tool, polys, radius, z_bottom, kind="pocket"|"contour", ...)   flat 2D geometry wrapped on a cylinder
    rotary_profile(setup, tool, profile, ...)      any polar cross-section (cams, hex / square from round, lobes) as rings
    gear_teeth(setup, tool, module, teeth, ...)    involute gear teeth with a disc cutter from the side, A indexed per space
"""
from __future__ import annotations

import math

import numpy as np

import cad_kernel as ck
import cam_kernel as cam
from cam_kernel import FEED, PLUNGE, RAPID, CamError, Op, PathBuilder, Setup, Tool

COLORS = {"rotary_rough": "#ff9f43", "rotary_finish": "#c58cff", "rotary_wrap": "#ff9f7a", "rotary_profile": "#7bd88f",
          "gear_teeth": "#ffd166"}
GEAR_CUTTER_MAX_VC = 80.0          # m/min: about the most an HSS involute cutter likes in brass / aluminium / plastics


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
    sh = tool.shape                                     # the tool's profile: any type, its neck and shank included
    rt = max(tool.radius, sh.max_radius)
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
            prof = sh.height(np.sqrt(np.where(inside, dist2, 0.0)))
            cand = np.where(inside & np.isfinite(prof), Zs - prof, -np.inf)
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


# ----------------------------------------------------------------------------- arbitrary polar cross-section
def _profile_tip_map(tool: Tool, poly, res_a: float, seg: float = 0.05):
    """Lowest tool-tip Z at each A (tool radial at Y = 0) that keeps the whole tool clear of the cross-section `poly`
    (shapely, in the setup's Y-Z plane) turned by A. Exact for the sampled boundary: a material point at (y, z)
    turned by A sits at (r sin(b - A), r cos(b - A)) under the tool, and the tool's profile says how high the tip
    must be above it at that lateral offset."""
    import shapely
    sh = tool.shape
    bound = shapely.segmentize(poly.boundary, seg)
    P = shapely.get_coordinates(bound)
    r = np.hypot(P[:, 0], P[:, 1])
    beta = np.arctan2(P[:, 0], P[:, 1])
    na = max(8, int(round(360.0 / res_a)))
    res_a = 360.0 / na
    angles = np.arange(na) * res_a
    rt = max(tool.radius, sh.max_radius)
    T = np.full(na, -np.inf)
    for i0 in range(0, na, 32):                      # (angles x points) in blocks
        A = np.radians(angles[i0:i0 + 32])[:, None]
        y = r[None, :] * np.sin(beta[None, :] - A)
        z = r[None, :] * np.cos(beta[None, :] - A)
        d = np.abs(y)
        inside = d <= rt + 1e-9
        prof = sh.height(np.where(inside, d, 0.0))
        cand = np.where(inside & np.isfinite(prof), z - prof, -np.inf)
        T[i0:i0 + 32] = cand.max(axis=1)
    return angles, np.maximum(T, 0.0)


def _profile_unreachable(tool: Tool, poly, T: np.ndarray, angles: np.ndarray, top: float):
    """Material of the stock the rings leave on the part: the stock disc minus the union of the tool's cross-sections at
    every A (concave corners tighter than the tool, slots narrower than it). Returns (area_mm2, max_thickness)."""
    import shapely
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    sh = tool.shape
    out = sh.outline()
    cap = max(top - float(T.min()) + 1.0, 0.5)
    right = [(rr, zz) for zz, rr in out if zz <= cap]
    if not right or right[-1][1] < cap:
        rc = float(sh.radius_at(np.array([cap]))[0])
        right.append((rc, cap))
    if right[0][0] > 0:
        right.insert(0, (0.0, right[0][1]))
    half = right + [(right[-1][0], cap + 1.0)]
    base = half + [(-x, z) for x, z in reversed(half)]
    shapes = []
    for i in range(len(angles)):
        A = math.radians(float(angles[i])); zt = float(T[i])
        ca, sa = math.cos(A), math.sin(A)
        pts = [(yp * ca + (zp + zt) * sa, (zp + zt) * ca - yp * sa) for yp, zp in base]
        pg = Polygon(pts).buffer(0)
        if not pg.is_empty and pg.area > 0:
            shapes.append(pg)
    if not shapes:
        return 0.0, 0.0
    removed = unary_union(shapes)
    disc = shapely.Point(0, 0).buffer(top, 64)
    sliver = 0.04                                    # the discrete sweep leaves slivers thinner than this along faces: not real
    left = disc.difference(removed).difference(poly.buffer(0.02)).buffer(-sliver).buffer(sliver)
    if left.is_empty or left.area < 0.05:
        return 0.0, 0.0
    pieces = list(getattr(left, "geoms", [left]))
    thick = 2 * max((shapely.maximum_inscribed_circle(g, 0.01).length for g in pieces if g.area > 0), default=0.0)
    return round(float(left.area), 2), round(float(thick), 2)


def rotary_profile(setup: Setup, tool: Tool, profile, x_range: tuple[float, float] | None = None, stepdown: float | None = None,
                   leave: float = 0.0, res_a: float = 1.0, stepover: float | None = None, name: str = "Rotary profile") -> Op:
    """Machine an arbitrary polar cross-section (a cam, a hex or square from round, splines, lobes) along the 4th axis:
    `profile` is a shapely Polygon in the setup's Y-Z plane (the section perpendicular to the axis, the same at every X
    in x_range, default the stock's length). The tool points at the axis from Y = 0; for every A its tip sits at the
    lowest Z that keeps the whole tool (real profile: flat, ball, bull, V...) clear of the turned section. Cut as rings
    (one revolution per X station, X stepover `stepover` x D from the tool, ring direction alternating so A never winds
    up) from the stock radius down `stepdown` per level, each level only where the one above left material; radial
    plunge entries; retract to safe Z at the end. Warns about material the tool cannot reach (concave corners tighter
    than the tool, slots narrower than it)."""
    _need_rotary(setup, "rotary_profile")
    polys = cam._polys(profile)
    if not polys:
        raise CamError("rotary_profile: give the cross-section as a shapely Polygon in the setup's Y-Z plane")
    from shapely.ops import unary_union
    poly = unary_union(polys)
    if poly.geom_type != "Polygon":
        raise CamError("rotary_profile: the cross-section must be one polygon (one piece of material round the axis)")
    if res_a <= 0 or res_a > 15:
        raise CamError("rotary_profile: res_a is the A step in degrees (0 < res_a <= 15)")
    top = setup.max_radius
    far = max(math.hypot(*c) for c in poly.exterior.coords)
    angles, T = _profile_tip_map(tool, poly, res_a)
    res_a = float(angles[1] - angles[0])
    T = T + leave
    x0, x1 = _x_limits(setup, tool, x_range)
    so = (stepover or (0.15 if tool.type == "ball" else tool.stepover)) * tool.diameter
    sd = stepdown or tool.stepdown
    floor = float(T.min())
    levels = []
    z = top - sd
    while z > floor + 1e-6:
        levels.append(z)
        z -= sd
    levels.append(floor)
    op = Op(name, "rotary_profile", tool, color=COLORS["rotary_profile"])
    if far > top + 1e-6:
        op.warnings.append(f"the profile reaches {far:.2f} mm from the axis, outside the Ø{2 * top:.1f} stock (r {top:.2f})")
    pb = _rb(setup, tool, op)
    n_x = max(1, int(math.ceil((x1 - x0) / so)) + 1)
    xs = np.linspace(x0, x1, n_x)
    na = len(angles)
    fwd = np.arange(na + 1) * res_a                  # 0 .. 360 (absolute A of the ring's points)
    prev = top
    direction = 1
    rings = 0
    for L in levels:
        Z = np.maximum(T, L)
        lift = prev + 0.5                            # clear of what the level above left
        for xc in xs:
            if (Z >= prev - 1e-3).all():
                continue                             # nothing left at this level
            aa = fwd if direction > 0 else fwd[::-1]
            zz = Z[(np.rint(aa / res_a).astype(int)) % na]
            if pb.pos is None:
                _rapid_to(pb, float(xc), float(aa[0]))
            else:
                _feed(pb, pb.pos[0], lift, pb.cur_a)
                pb._add(RAPID, float(xc), 0.0, lift, 0.0, float(aa[0]))
                pb.cur_a = float(aa[0])
            _feed(pb, float(xc), float(zz[0]), float(aa[0]), pb.plunge, PLUNGE)
            for a, zv in zip(aa[1:], zz[1:]):
                _feed(pb, float(xc), float(zv), float(a))
            direction *= -1
            rings += 1
        prev = L
    if pb.pos is not None:
        pb._add(RAPID, pb.pos[0], 0.0, setup.safe_z, 0.0, pb.cur_a)
    area, thick = _profile_unreachable(tool, poly, T, angles, top)
    if area > 0:
        op.warnings.append(f"{tool.label()} cannot reach {area:g} mm² of the section (up to {thick:g} mm thick, left on the part): "
                           "concave corners tighter than the tool or gaps narrower than it; use a smaller tool or a ball")
    op.params = dict(levels=len(levels), rings=rings, stepdown=round(sd, 3), stepover=round(so, 3), leave=leave, res_a=res_a,
                     r_min=round(floor - leave, 3), unreachable_mm2=area)
    return op


# ----------------------------------------------------------------------------- gear teeth with a disc cutter
def gear_teeth(setup: Setup, tool: Tool, module: float, teeth: int, width: float | None = None, x0: float | None = None,
               pressure_angle: float = 20.0, side: str = "+y", stepdown: float | None = None, leave: float = 0.0,
               helix: float = 0.0, climb: bool = True, backlash: float = 0.0, a0: float = 0.0, name: str = "Gear teeth") -> Op:
    """Cut involute spur (or helical) gear teeth into a round blank on the 4th axis with a disc-type module cutter
    (tool type gear_cutter) from the SIDE of the blank, indexing A between tooth spaces. The cutter's axis is vertical;
    its disc is centred on the axis height (Z = 0) at Y = ±(r + R_cutter) on `side` ("+y" | "-y"), so the form cuts the
    space at the 3 o'clock position with the gear's tangent along the cutter axis. Depth passes are Y moves toward the
    axis (`stepdown` mm of radial depth each, default the tool's; the last to the root radius m(N/2 - 1.25) + `leave`
    - `backlash`), each feeding along X across the face (x0 .. x0 + width, default the stock) with R_cutter + 2 mm
    lead-in/out beyond both faces, in one X direction only (climb: the cutter's edge runs with the feed at the cut;
    the return is made with the cutter retracted clear of the blank). A is indexed by 360/N from `a0` and kept in
    [0, 360). `helix` (deg, + = right hand) turns A with X: dA = -dx tan(helix) / r_pitch."""
    import tool_shape
    _need_rotary(setup, "gear_teeth")
    if tool.type != "gear_cutter":
        raise CamError(f"gear_teeth needs a gear cutter (tool type gear_cutter); {tool.label()} is a {tool.type}")
    try:
        sh = tool.shape
    except tool_shape.ToolShapeError as e:
        raise CamError(f"gear_teeth: {tool.label()}: {e}") from e
    m = float(module)
    N = int(teeth)
    if m <= 0 or N < 4:
        raise CamError("gear_teeth: module must be > 0 and teeth >= 4")
    if abs(float(tool.module) - m) > 1e-6:
        raise CamError(f"gear_teeth: {tool.label()} is a module {tool.module:g} cutter; the gear is module {m:g} "
                       "(a module cutter only cuts its own module)")
    if side not in ("+y", "-y"):
        raise CamError("gear_teeth: side must be '+y' or '-y'")
    sgn = 1.0 if side == "+y" else -1.0
    r_p = m * N / 2
    r_tip = r_p + m
    r_root = r_p - 1.25 * m
    R = tool.radius
    D = tool.diameter
    t = float(sh.p["thickness"])
    z_tip = -(sh.cut_from + t / 2)                   # tool tip (nut underside) with the disc's mid-plane on the axis
    r_blank = setup.max_radius
    stock = setup.stock
    x_lo = float(x0) if x0 is not None else float(stock.xmin)
    w = float(width) if width is not None else float(stock.xmax) - x_lo
    if w <= 0:
        raise CamError("gear_teeth: the face width must be > 0")
    x_hi = x_lo + w
    lead = R + 2.0
    xa, xb = x_lo - lead, x_hi + lead
    dirx = -1 if (bool(climb) == (sgn > 0)) else 1      # M3 (clockwise from above): the edge at the cut runs -X on the +Y side
    x_start, x_end = (xa, xb) if dirx > 0 else (xb, xa)
    r_final = r_root + float(leave) - float(backlash)
    depth = r_blank - r_final
    if depth <= 0:
        raise CamError(f"gear_teeth: the Ø{2 * r_blank:g} blank is already inside the gear's root Ø{2 * r_root:.2f}")
    sd = float(stepdown or tool.stepdown)
    if sd <= 0:
        raise CamError("gear_teeth: stepdown must be > 0")
    n_pass = max(1, int(math.ceil(depth / sd - 1e-9)))
    radii = [r_blank - depth * (k + 1) / n_pass for k in range(n_pass)]
    y_safe = sgn * (r_blank + R + 2.0)
    pitch_a = 360.0 / N
    k_helix = math.degrees(math.tan(math.radians(helix)) / r_p)      # degrees of A per mm of X (right hand: A falls with +X)
    op = Op(name, "gear_teeth", tool, color=COLORS["gear_teeth"])
    pb = _rb(setup, tool, op)
    # ---- warnings
    if abs(float(tool.pressure_angle or 20.0) - float(pressure_angle)) > 0.5:
        op.warnings.append(f"{tool.label()} is a {tool.pressure_angle:g}° cutter; the gear asks for {pressure_angle:g}°: the flanks will be wrong")
    rng = tool_shape.cutter_number(tool.cutter_teeth_min) if tool.cutter_teeth_min else None
    if rng and not (rng[1] <= N <= rng[2]):
        want = tool_shape.cutter_range_for(N)
        op.warnings.append(f"{tool.label()} is cutter No.{rng[0]} ({rng[1]}–{rng[2]}T); a {N}-tooth gear wants No.{want[0]} "
                           f"({want[1]}–{want[2]}T): the tooth form will be off")
    elif not rng and tool.cutter_teeth_min and tool.cutter_teeth_min != N:
        op.warnings.append(f"{tool.label()} is cut for {tool.cutter_teeth_min} teeth exactly; the gear has {N}: the tooth form will be off")
    vc = math.pi * D * tool.rpm / 1000.0
    if vc > GEAR_CUTTER_MAX_VC:
        ok_rpm = int(GEAR_CUTTER_MAX_VC * 1000.0 / (math.pi * D))
        sp = setup.machine.spindle or {}
        low = sp.get("min", 0)
        op.warnings.append(f"{vc:.0f} m/min surface speed at {tool.rpm:g} rpm on the Ø{D:g} cutter: too fast for an HSS gear cutter "
                           f"(≤ {GEAR_CUTTER_MAX_VC:g} m/min): run it at ≤ {ok_rpm} rpm"
                           + (f" ({setup.machine.name}'s spindle goes down to {low:g} rpm)" if low and low > ok_rpm else ""))
    if sh.p["thickness"] < sh.p["form_width"] - 0.02:
        op.warnings.append(f"the disc is {t:.2f} mm wide but the {tool.cutter_teeth_min}-tooth space is {sh.p['form_width']:.2f} at the "
                           "tip line: the flanks near the tooth tips are left uncut")
    if r_blank > r_tip + 0.02:
        op.warnings.append(f"the blank (Ø{2 * r_blank:g}) is bigger than the gear's tip Ø{2 * r_tip:.2f}: the first "
                           f"{r_blank - r_tip:.2f} mm is cut with the disc's flat sides; turn the blank to Ø{2 * r_tip:.2f} first (rotary_finish)")
    elif r_blank < r_tip - 0.02:
        op.warnings.append(f"the blank (Ø{2 * r_blank:g}) is smaller than the gear's tip Ø{2 * r_tip:.2f}: the teeth come out "
                           f"{r_tip - r_blank:.2f} mm short")
    if x_lo < stock.xmin - 1e-6 or x_hi > stock.xmax + 1e-6:
        op.warnings.append(f"the face width X {x_lo:g}..{x_hi:g} runs beyond the blank (X {stock.xmin:g}..{stock.xmax:g})")
    rot = setup.machine.rotary or {}
    chuck_x = rot.get("chuck_face_x")
    tail_x = rot.get("tail_x")
    if xa < stock.xmin - 1e-6:
        op.warnings.append(f"the lead-in/out runs {stock.xmin - xa:.1f} mm past the stock's end at X {stock.xmin:g} (the Ø{D:g} disc must clear "
                           "the face): make sure the chuck jaws are clear of the cutter there"
                           + (f" — it reaches {chuck_x - xa:.1f} mm into the chuck face at X {chuck_x:g}" if chuck_x is not None and xa < chuck_x else ""))
    if xb > stock.xmax + 1e-6:
        op.warnings.append(f"the lead-in/out runs {xb - stock.xmax:.1f} mm past the stock's end at X {stock.xmax:g}: make sure the tailstock "
                           "/ live centre is clear of the cutter there"
                           + (f" — it reaches {xb - tail_x:.1f} mm into the tailstock at X {tail_x:g}" if tail_x is not None and xb > tail_x else ""))
    # ---- moves (every move carries A)
    def go(kind, x, y, a, f=0.0):
        pb._add(kind, x, y, z_tip, f, a)
        pb.cur_a = a

    def a_at(a_space: float, x: float) -> float:
        return a_space - k_helix * (x - x_lo)

    first = True
    for k in range(N):
        a_space = (float(a0) + k * pitch_a) % 360.0
        a_s = a_at(a_space, x_start) % 360.0             # each pass starts in [0, 360); a helical pass may run on past it
        a_e = a_s + (a_at(a_space, x_end) - a_at(a_space, x_start))
        if first:
            pb._add(RAPID, x_start, y_safe, setup.safe_z, 0.0, a_s)
            pb.cur_a = a_s
            go(RAPID, x_start, y_safe, a_s)              # down beside the blank, the disc clear of it
            first = False
        else:
            go(RAPID, x_start, y_safe, a_s)              # index with the cutter retracted
        for rk in radii:
            yk = sgn * (rk + R)
            go(RAPID, x_start, yk, a_s)                  # depth: a Y move at the lead-in, the disc clear of the face
            go(FEED, x_end, yk, a_e, pb.feed)            # one cut across the face (+ lead-in / lead-out)
            go(RAPID, x_end, y_safe, a_e)                # retract clear of the blank
            go(RAPID, x_start, y_safe, a_s)              # back for the next pass (never cut both ways)
    pb._add(RAPID, x_start, y_safe, setup.safe_z, 0.0, pb.cur_a)
    pb._add(RAPID, x_start, 0.0, setup.safe_z, 0.0, pb.cur_a)
    op.params = dict(module=m, teeth=N, pressure_angle=float(pressure_angle), r_pitch=round(r_p, 4), r_root=round(r_root, 4),
                     r_tip=round(r_tip, 4), passes=n_pass, spaces=N, helix=float(helix), side=side, stepdown=round(depth / n_pass, 3),
                     leave=float(leave), backlash=float(backlash), width=round(w, 3), x0=round(x_lo, 3), lead=round(lead, 2),
                     climb=bool(climb), feed_dir="-X" if dirx < 0 else "+X", y_cut=round(sgn * (r_final + R), 3),
                     surface_speed=round(vc, 1), a0=float(a0))
    return op
