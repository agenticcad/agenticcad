"""The shape of a cutting tool: its side profile (radius against height above the tip), from which everything that
needs the tool's geometry is derived — the viewer's 3D tool, the 3D finishing drop-cutter, the 4th-axis maps, the
material-removal simulation and the collision checks.

A tool is round, so its profile r(z) says everything: z is the height above the tip, r the radius there. It has three
parts, from the tip up:
  cutting  0 .. flute_length          the shape of the type (flat, ball, bull nose, V / engraver, chamfer, drill point,
                                      tapered ball), at most `diameter` wide
  neck     flute_length .. reach      non-cutting: `neck_diameter` (a relieved neck), or the cutting shape carried on
                                      (a V-bit's cone up to its full diameter, a plain endmill's flutes' diameter)
  shank    above reach                `shank_diameter`, reached through a 30° (half-angle) taper when it differs,
                                      up to the stickout (how far the tool projects from the collet / nut)
Heights are exact where the type has a closed form; the profile is sampled finely (≤ 5 µm near the tip) for lookups.
"""
from __future__ import annotations

import math
from functools import lru_cache

import numpy as np

TYPES = {
    "flat": "Flat endmill",
    "ball": "Ball endmill",
    "bull": "Bull-nose endmill (corner radius)",
    "vbit": "V-bit / engraver",
    "chamfer": "Chamfer mill",
    "drill": "Drill",
    "taper_ball": "Tapered ball endmill",
    "gear_cutter": "Gear cutter (involute module cutter on an arbor)",
}
TRANSITION_HALF_ANGLE = 30.0          # degrees: the taper from a neck to a bigger shank (typical of reduced-neck tools)
# Involute module cutters come in sets of 8, each cut for a range of tooth counts (DIN / metric numbering: No.1 is the
# 12–13 tooth cutter; the Brown & Sharpe inch sets number the same ranges the other way round).
CUTTER_RANGES = [(1, 12, 13), (2, 14, 16), (3, 17, 20), (4, 21, 25), (5, 26, 34), (6, 35, 54), (7, 55, 134), (8, 135, 10 ** 6)]


class ToolShapeError(ValueError):
    pass


def cutter_number(teeth_min: int) -> tuple[int, int, int] | None:
    """(number, lowest, highest tooth count) of the set cutter whose range starts at `teeth_min`, else None (a cutter
    made for one exact tooth count)."""
    for n, lo, hi in CUTTER_RANGES:
        if lo == int(teeth_min):
            return n, lo, hi
    return None


def cutter_range_for(teeth: int) -> tuple[int, int, int]:
    """The set cutter (number, lo, hi) that cuts a gear of `teeth` teeth."""
    for n, lo, hi in CUTTER_RANGES:
        if lo <= teeth <= hi:
            return n, lo, hi
    return CUTTER_RANGES[0]


def _inv(a: float) -> float:
    return math.tan(a) - a


def gear_space_form(module: float, teeth: int, pressure_angle: float = 20.0, clearance: float = 0.25, n: int = 200):
    """The tooth SPACE of a standard involute gear in the transverse plane, as (depth, half_width) pairs from the root
    (depth 0, the narrow end) up to the tip line (depth 2.25 m, the wide end), measured along the space's centre line
    and across it. A disc cutter that has this form around its periphery (periphery = the root) cuts the space.
    Also returns the root flat's half width (the cutter's periphery is a cylinder, not the root arc; the difference
    is a few microns). Returns (depth, half_width, root_half)."""
    if module <= 0:
        raise ToolShapeError("a gear cutter needs its module (mm)")
    N = int(teeth)
    if N < 4:
        raise ToolShapeError("a gear cutter needs the tooth count it is cut for (cutter_teeth_min: the lowest count of its "
                             "range, e.g. 12 for a No.1 12–13T cutter)")
    if not (0 < pressure_angle < 45):
        raise ToolShapeError("the pressure angle must be between 0 and 45 degrees")
    m = module
    r_p = m * N / 2
    r_b = r_p * math.cos(math.radians(pressure_angle))
    r_a = r_p + m
    r_f = r_p - (1.0 + clearance) * m
    half_base = math.pi / (2 * N) + _inv(math.radians(pressure_angle))

    def half_space(r: float) -> float:                  # half angle of the space at radius r
        half_tooth = half_base - (_inv(math.acos(min(1.0, r_b / r))) if r > r_b else 0.0)
        return math.pi / N - half_tooth

    rs = np.linspace(r_f, r_a, n)
    th = np.array([half_space(float(r)) for r in rs])
    depth = rs * np.cos(th) - r_f                       # along the centre line from the root flat
    half = rs * np.sin(th)
    # the flank is monotone in both (depth and width grow towards the tip); clean numerical noise at the root
    depth = np.maximum.accumulate(np.maximum(depth, 0.0))
    half = np.maximum.accumulate(half)
    return depth, half, float(half[0])


def gear_form_width(module: float, teeth: int, pressure_angle: float = 20.0) -> float:
    """The natural width of a module cutter's form: the space width at the gear's tip line."""
    _, half, _ = gear_space_form(module, teeth, pressure_angle)
    return 2 * float(half[-1])


def _g(tool, k, default=0.0):
    v = tool.get(k, default) if isinstance(tool, dict) else getattr(tool, k, default)
    return default if v is None else v


def params(tool) -> dict:
    """The tool's geometry with every default resolved (works on a Tool or its dict)."""
    typ = str(_g(tool, "type", "flat") or "flat")
    if typ not in TYPES:
        raise ToolShapeError(f"unknown tool type {typ!r}; use one of {', '.join(TYPES)}")
    D = float(_g(tool, "diameter", 6.0))
    if D <= 0:
        raise ToolShapeError("tool diameter must be > 0")
    R = D / 2
    if typ == "gear_cutter":
        return _gear_params(tool, R)
    fl = max(float(_g(tool, "flute_length", 10.0)), 0.01)
    angle = float(_g(tool, "angle", 0.0))
    tip = float(_g(tool, "tip_diameter", 0.0))
    rc = float(_g(tool, "corner_radius", 0.0))
    taper = float(_g(tool, "taper", 0.0))
    if typ == "drill" and not angle:
        angle = 118.0
    if typ in ("vbit", "chamfer") and not (0 < angle < 180):
        raise ToolShapeError(f"a {TYPES[typ].lower()} needs its included angle (0..180°)")
    if typ in ("vbit", "chamfer", "taper_ball") and not (0 <= tip < D):
        raise ToolShapeError("the tip diameter must be smaller than the tool diameter")
    if typ == "bull" and not (0 < rc <= R + 1e-9):
        raise ToolShapeError("a bull-nose endmill needs a corner radius between 0 and its radius")
    if typ == "taper_ball":
        if not (0 < tip < D):
            raise ToolShapeError("a tapered ball endmill needs its ball (tip) diameter, smaller than its diameter")
        if not (0 < taper < 45):
            raise ToolShapeError("a tapered ball endmill needs its taper angle per side (0..45°)")
    shank = float(_g(tool, "shank_diameter", 0.0)) or D
    neck = float(_g(tool, "neck_diameter", 0.0))
    reach = float(_g(tool, "reach", 0.0))
    if neck and neck > D + 1e-9:
        raise ToolShapeError("the neck must not be wider than the cutting diameter")
    p = {"type": typ, "R": R, "fl": fl, "angle": angle, "tip": tip, "rc": min(rc, R), "taper": taper,
         "shank_r": shank / 2, "neck_r": neck / 2, "reach": reach, "cut_z0": 0.0}
    p["form_top"] = _form_top(p)                     # where the type's shape reaches its full diameter
    p["reach"] = max(reach, fl, p["form_top"] if not neck else fl)
    stick = float(_g(tool, "stickout", 0.0))
    shank_start = p["reach"] + _transition(p)
    p["stickout"] = max(stick, shank_start + 1.0) if stick else shank_start + 6.0
    p["shank_start"] = shank_start
    return p


def _gear_params(tool, R: float) -> dict:
    """A disc-type involute gear cutter on an arbor, from the tip (the retaining nut's underside) up: the nut, the disc
    (its radius following the tooth-space form: the periphery cuts the gear's root, the form narrows the disc towards
    its edges down to the gullet radius at the gear's tip line), then the arbor's shank up to the stickout."""
    m = float(_g(tool, "module", 0.0))
    pa = float(_g(tool, "pressure_angle", 20.0)) or 20.0
    N = int(_g(tool, "cutter_teeth_min", 0) or 0)
    depth, half, root_half = gear_space_form(m, N, pa)           # validates module / teeth / angle
    t_nat = 2 * float(half[-1])
    t = float(_g(tool, "thickness", 0.0)) or t_nat
    if t < 2 * root_half + 0.05:
        raise ToolShapeError(f"a {t:g} mm thick disc is narrower than the root of a module {m:g} tooth space "
                             f"({2 * root_half:.2f} mm): it cannot cut this module")
    gullet = R - float(np.interp(t / 2, half, depth, left=0.0, right=float(depth[-1])))   # disc radius at its faces
    nut_h = float(_g(tool, "nut_height", 4.0))
    nut_d = float(_g(tool, "nut_diameter", 10.0))
    shank = float(_g(tool, "shank_diameter", 0.0))
    if shank <= 0:
        raise ToolShapeError("a gear cutter needs its arbor's shank diameter (what goes in the collet: 3.175, 6 or 6.35)")
    if nut_h < 0 or nut_d <= 0:
        raise ToolShapeError("the retaining nut needs a height and a diameter")
    if nut_d < shank - 1e-9:
        raise ToolShapeError(f"the Ø{nut_d:g} nut is smaller than the Ø{shank:g} arbor shank")
    if gullet <= nut_d / 2 + 0.5:
        raise ToolShapeError(f"a Ø{2 * R:g} disc is too small for module {m:g}: its gullet (Ø{2 * gullet:.1f}) would be "
                             f"inside the Ø{nut_d:g} nut")
    fl = nut_h + t
    stick = float(_g(tool, "stickout", 0.0))
    p = {"type": "gear_cutter", "R": R, "fl": fl, "angle": pa, "tip": 0.0, "rc": 0.0, "taper": 0.0, "shank_r": shank / 2,
         "neck_r": 0.0, "reach": fl, "cut_z0": nut_h, "form_top": fl, "shank_start": fl,
         "module": m, "teeth": N, "thickness": t, "nut_h": nut_h, "nut_r": nut_d / 2, "gullet": gullet,
         "form_width": t_nat}
    p["stickout"] = max(stick, fl + 1.0) if stick else fl + 10.0
    return p


def _gear_form(p, z: np.ndarray) -> np.ndarray:
    """Radius of the gear cutter at height z above its tip (nut, disc form, shank)."""
    depth, half, _ = gear_space_form(p["module"], p["teeth"], p["angle"])
    nut_h, t, R = p["nut_h"], p["thickness"], p["R"]
    h = np.abs(z - (nut_h + t / 2))                                  # distance from the disc's mid-plane
    # the form: half width `half` at radial depth `depth` below the periphery -> radius R - depth at height h = half
    rho = R - np.interp(h, half, depth, left=0.0, right=float(depth[-1]))
    rho = np.where(h > t / 2, p["shank_r"], rho)                     # above / below the disc: nut or shank (set below)
    r = np.where(z < nut_h, p["nut_r"], rho)
    return r


def _form_top(p) -> float:
    R, t = p["R"], p["tip"] / 2
    if p["type"] == "ball":
        return R
    if p["type"] == "bull":
        return p["rc"]
    if p["type"] in ("vbit", "chamfer", "drill"):
        return (R - t) / math.tan(math.radians(p["angle"] / 2))
    if p["type"] == "taper_ball":
        rb, th = t, math.radians(p["taper"])
        zt = rb * (1 - math.sin(th))
        return zt + (R - rb * math.cos(th)) / math.tan(th)
    return 0.0


def _form(p, z: np.ndarray) -> np.ndarray:
    """Radius of the type's cutting shape at height z (capped at the cutting radius)."""
    R, t = p["R"], p["tip"] / 2
    typ = p["type"]
    if typ == "flat":
        return np.full_like(z, R)
    if typ == "ball":
        zz = np.minimum(z, R)
        return np.sqrt(np.maximum(R * R - (R - zz) ** 2, 0.0))
    if typ == "bull":
        rc = p["rc"]; zz = np.minimum(z, rc)
        return (R - rc) + np.sqrt(np.maximum(rc * rc - (rc - zz) ** 2, 0.0))
    if typ in ("vbit", "chamfer", "drill"):
        return np.minimum(t + z * math.tan(math.radians(p["angle"] / 2)), R)
    # tapered ball: sphere of radius rb at the tip up to the tangent point, then the cone
    rb, th = t, math.radians(p["taper"])
    zt = rb * (1 - math.sin(th))
    sph = np.sqrt(np.maximum(rb * rb - (rb - np.minimum(z, rb)) ** 2, 0.0))
    cone = rb * math.cos(th) + (z - zt) * math.tan(th)
    return np.minimum(np.where(z <= zt, sph, cone), R)


def _transition(p) -> float:
    """Length of the taper from the top of the neck (or cutting part) to the shank."""
    top_r = p["neck_r"] or float(_form(p, np.array([p["reach"]]))[0])
    return abs(p["shank_r"] - top_r) / math.tan(math.radians(TRANSITION_HALF_ANGLE))


def radius_at(p, z: np.ndarray) -> np.ndarray:
    """The tool's radius at height z above the tip (all three parts)."""
    z = np.asarray(z, dtype=float)
    if p["type"] == "gear_cutter":
        return _gear_form(p, z)
    fl, reach = p["fl"], p["reach"]
    r = _form(p, z)
    if p["neck_r"]:
        r = np.where(z > fl, p["neck_r"], r)
    top_r = p["neck_r"] or float(_form(p, np.array([reach]))[0])
    k = math.tan(math.radians(TRANSITION_HALF_ANGLE))
    tr = top_r + np.sign(p["shank_r"] - top_r) * (z - reach) * k
    tr = np.minimum(tr, p["shank_r"]) if p["shank_r"] >= top_r else np.maximum(tr, p["shank_r"])
    r = np.where(z > reach, tr, r)
    return r


@lru_cache(maxsize=256)
def _table(key: tuple) -> dict:
    p = dict(key)
    top = p["stickout"]
    near = min(top, max(p["fl"], p["form_top"], p["reach"]) + 0.5)
    z = np.concatenate([np.arange(0.0, near, 0.002), np.arange(near, top + 0.05, 0.05)])
    r = radius_at(p, z)
    cut = (z <= p["fl"] + 1e-9) & (z >= p.get("cut_z0", 0.0) - 1e-9)
    return {"z": z, "r": r, "cum": np.maximum.accumulate(r), "zc": z[cut], "rc": r[cut], "cumc": np.maximum.accumulate(r[cut])}


def _key(p) -> tuple:
    return tuple(sorted((k, v) for k, v in p.items()))


def _lookup(z, cum, d):
    d = np.asarray(d, dtype=float)
    i = np.searchsorted(cum, d, side="left")
    out = np.full(d.shape, np.inf)
    ok = i < len(cum)
    i = np.clip(i, 0, len(cum) - 1)
    # linear interpolation between the bracketing samples
    i0 = np.maximum(i - 1, 0)
    c0, c1 = cum[i0], cum[i]
    f = np.where(c1 > c0, (d - c0) / np.where(c1 > c0, c1 - c0, 1.0), 0.0)
    zz = np.where(i == 0, z[0], z[i0] + np.clip(f, 0, 1) * (z[i] - z[i0]))
    out[ok] = zz[ok]
    return out


class ToolShape:
    """Geometry queries for one tool."""

    def __init__(self, tool):
        self.p = params(tool)
        self._t = _table(_key(self.p))

    @property
    def cut_radius(self) -> float:
        """The widest the cutting part gets (within the flute length)."""
        return float(self._t["cumc"][-1])

    @property
    def max_radius(self) -> float:
        return float(self._t["cum"][-1])

    @property
    def flute_length(self) -> float:
        return self.p["fl"]

    @property
    def stickout(self) -> float:
        return self.p["stickout"]

    def height(self, d) -> np.ndarray:
        """Lowest point of the whole tool (cutting part, neck, shank) at radial distance d from its axis, above the tip:
        inf beyond the tool. The drop-cutter uses it so no part of the tool goes into the surface."""
        return _lookup(self._t["z"], self._t["cum"], d)

    def cut_height(self, d) -> np.ndarray:
        """Lowest point of the CUTTING part at radial distance d (inf where only non-cutting parts reach)."""
        return _lookup(self._t["zc"], self._t["cumc"], d)

    def radius_at(self, z) -> np.ndarray:
        return radius_at(self.p, np.asarray(z, dtype=float))

    @property
    def cut_from(self) -> float:
        """Height above the tip where the cutting part starts (0 for endmills; a gear cutter's nut sits under its disc)."""
        return float(self.p.get("cut_z0", 0.0))

    def cut_slabs(self, dr: float = 0.02) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """The cutting part as horizontal slabs (bottom, top, radius) from the tip up: each slab's radius is the largest in
        it and slabs are split so the radius varies by at most `dr` within one, so the stack of cylinders never
        under-states the tool (and over-states it by at most dr plus the radius change over one 2 µm sample, which matters
        only where the profile runs almost radially). For any profile, monotone or not."""
        return _slabs(_key(self.p), round(dr, 4))

    def width_at_depth(self, depth: float) -> float:
        """Widest the cutting part is within `depth` of the tip (what a cut that deep is as wide as at the top)."""
        z = self._t["zc"]
        k = np.searchsorted(z, min(depth, self.p["fl"]), side="right")
        return 2 * float(self._t["cumc"][max(k - 1, 0)])

    def bands(self, z_from: float, n_max: int = 8) -> list[tuple[float, float, float]]:
        """The tool above height z_from as (bottom, top, radius) bands, each with the largest radius in it (so a band
        never under-states the tool), split where the radius changes."""
        z, r = self._t["z"], self._t["r"]
        sel = z >= z_from
        z, r = z[sel], r[sel]
        if len(z) < 2:
            return []
        out = []
        start = 0
        for i in range(1, len(z)):
            if abs(r[i] - r[start]) > max(0.05, 0.1 * r[start]) or i == len(z) - 1:
                out.append((float(z[start]), float(z[i]), float(r[start:i + 1].max())))
                start = i
        while len(out) > n_max:                         # merge the two most similar neighbours
            j = min(range(len(out) - 1), key=lambda k: abs(out[k][2] - out[k + 1][2]))
            a, b = out[j], out[j + 1]
            out[j:j + 2] = [(a[0], b[1], max(a[2], b[2]))]
        return out

    def outline(self, tol: float = 0.01) -> list[list[float]]:
        """[[z, r], ...] from the tip to the stickout, simplified to `tol` (for drawing)."""
        z, r = self._t["z"], self._t["r"]
        keep = [0]
        for i in range(1, len(z) - 1):
            a, b = keep[-1], i + 1
            # keep i if the straight line from the last kept point to i+1 misses the samples in between by > tol
            if z[b] - z[a] <= 0:
                continue
            t = (z[a:b + 1] - z[a]) / (z[b] - z[a])
            err = np.abs(r[a] + t * (r[b] - r[a]) - r[a:b + 1]).max()
            if err > tol:
                keep.append(i)
        keep.append(len(z) - 1)
        pts = [[round(float(z[i]), 4), round(float(r[i]), 4)] for i in keep]
        if pts[0][1] > 0:                               # a flat bottom: start on the axis
            pts.insert(0, [0.0, 0.0])
        return pts

    def payload(self) -> dict:
        out = {"outline": self.outline(), "flute_length": self.p["fl"], "stickout": round(self.p["stickout"], 3),
               "cut_radius": round(self.cut_radius, 4), "max_radius": round(self.max_radius, 4),
               "reach": round(self.p["reach"], 3), "shank_start": round(self.p["shank_start"], 3),
               "cut_from": round(self.cut_from, 3)}
        if self.p["type"] == "gear_cutter":
            out["gear"] = {"thickness": round(self.p["thickness"], 3), "form_width": round(self.p["form_width"], 3),
                           "gullet_diameter": round(2 * self.p["gullet"], 3)}
        return out


@lru_cache(maxsize=256)
def _slabs(key: tuple, dr: float):
    t = _table(key)
    z, r = t["zc"], t["rc"]
    if len(z) < 2:
        return np.zeros(0), np.zeros(0), np.zeros(0)
    b0, b1, rad = [], [], []
    i0 = 0
    lo = hi = float(r[0])
    for i in range(1, len(z)):
        v = float(r[i])
        lo2, hi2 = min(lo, v), max(hi, v)
        if hi2 - lo2 > dr:
            b0.append(float(z[i0])); b1.append(float(z[i])); rad.append(hi2)     # the slab runs up to sample i: include it
            i0 = i
            lo = hi = v
        else:
            lo, hi = lo2, hi2
    b0.append(float(z[i0])); b1.append(float(z[-1]) + 1e-9); rad.append(hi)
    return np.array(b0), np.array(b1), np.array(rad)


def shape(tool) -> ToolShape:
    return ToolShape(tool)
