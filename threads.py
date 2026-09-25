"""
Threads for design scripts: ISO metric (M4, M6…) and Unified inch (UNC/UNF: 1/4-20, #10-32, 3/8-16…) tables
(pitch, tap drill, clearance), cosmetic or real (bd_warehouse IsoThread; the 60° form is shared by ISO and UN)
geometry for bolts, nuts, tapped holes, and a per-run registry so drawings can call out "M4×0.7 THRU" or
"1/4-20 UNC THRU" on the matching hole. Everything is stored in mm; inch tables are converted on lookup.
"""
from __future__ import annotations

import math
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import build123d as b3d
from build123d import Align, Cylinder, Mode, Part, Plane, Pos, RegularPolygon, Vector, extrude, chamfer

# size: (coarse pitch, clearance fine, medium, coarse)  — ISO 261 / ISO 273
ISO: dict[str, tuple[float, float, float, float]] = {
    "M1.6": (0.35, 1.7, 1.8, 2.0), "M2": (0.4, 2.2, 2.4, 2.6), "M2.5": (0.45, 2.7, 2.9, 3.1), "M3": (0.5, 3.2, 3.4, 3.6),
    "M4": (0.7, 4.3, 4.5, 4.8), "M5": (0.8, 5.3, 5.5, 5.8), "M6": (1.0, 6.4, 6.6, 7.0), "M8": (1.25, 8.4, 9.0, 10.0),
    "M10": (1.5, 10.5, 11.0, 12.0), "M12": (1.75, 13.0, 13.5, 14.5), "M14": (2.0, 15.0, 15.5, 16.5),
    "M16": (2.0, 17.0, 17.5, 18.5), "M20": (2.5, 21.0, 22.0, 24.0), "M24": (3.0, 25.0, 26.0, 28.0),
}
# hex head: (across flats, head height); socket head: (head dia, head height); nut: (af, height)  — ISO 4017/4762/4032
HEX = {"M2": (4, 1.4), "M2.5": (5, 1.7), "M3": (5.5, 2), "M4": (7, 2.8), "M5": (8, 3.5), "M6": (10, 4), "M8": (13, 5.3),
       "M10": (16, 6.4), "M12": (18, 7.5), "M14": (21, 8.8), "M16": (24, 10), "M20": (30, 12.5), "M24": (36, 15)}
SOCKET = {"M2": (3.8, 2), "M2.5": (4.5, 2.5), "M3": (5.5, 3), "M4": (7, 4), "M5": (8.5, 5), "M6": (10, 6), "M8": (13, 8),
          "M10": (16, 10), "M12": (18, 12), "M14": (21, 14), "M16": (24, 16), "M20": (30, 20), "M24": (36, 24)}
NUT = {"M2": (4, 1.6), "M2.5": (5, 2), "M3": (5.5, 2.4), "M4": (7, 3.2), "M5": (8, 4.7), "M6": (10, 5.2), "M8": (13, 6.8),
       "M10": (16, 8.4), "M12": (18, 10.8), "M14": (21, 12.8), "M16": (24, 14.8), "M20": (30, 18), "M24": (36, 21.5)}
WASHER = {"M2": (2.2, 5, 0.3), "M2.5": (2.7, 6, 0.5), "M3": (3.2, 7, 0.5), "M4": (4.3, 9, 0.8), "M5": (5.3, 10, 1), "M6": (6.4, 12, 1.6),
          "M8": (8.4, 16, 1.6), "M10": (10.5, 20, 2), "M12": (13, 24, 2.5), "M14": (15, 28, 2.5), "M16": (17, 30, 3), "M20": (21, 37, 3), "M24": (25, 44, 4)}

INCH = 25.4
# Unified inch threads (ASME B1.1 sizes; tap drills for ~75% engagement; clearance holes ASME B18.2.8 close/normal/loose).
# key "size-tpi": (major in, tpi, series, tap drill in, clearance close, normal, loose in)
UN: dict[str, tuple[float, int, str, float, float, float, float]] = {
    "#4-40": (0.112, 40, "UNC", 0.0890, 0.1285, 0.1360, 0.1495), "#6-32": (0.138, 32, "UNC", 0.1065, 0.1495, 0.1570, 0.1770),
    "#8-32": (0.164, 32, "UNC", 0.1360, 0.1770, 0.1875, 0.2010), "#10-24": (0.190, 24, "UNC", 0.1495, 0.2010, 0.2130, 0.2280),
    "#10-32": (0.190, 32, "UNF", 0.1590, 0.2010, 0.2130, 0.2280),
    "1/4-20": (0.250, 20, "UNC", 0.2010, 0.2570, 0.2660, 0.2810), "1/4-28": (0.250, 28, "UNF", 0.2130, 0.2570, 0.2660, 0.2810),
    "5/16-18": (0.3125, 18, "UNC", 0.2570, 0.3230, 0.3320, 0.3440), "5/16-24": (0.3125, 24, "UNF", 0.2720, 0.3230, 0.3320, 0.3440),
    "3/8-16": (0.375, 16, "UNC", 0.3125, 0.3860, 0.3970, 0.4060), "3/8-24": (0.375, 24, "UNF", 0.3320, 0.3860, 0.3970, 0.4060),
    "7/16-14": (0.4375, 14, "UNC", 0.3680, 0.4531, 0.4687, 0.4844), "7/16-20": (0.4375, 20, "UNF", 0.3906, 0.4531, 0.4687, 0.4844),
    "1/2-13": (0.500, 13, "UNC", 0.4219, 0.5156, 0.5312, 0.5625), "1/2-20": (0.500, 20, "UNF", 0.4531, 0.5156, 0.5312, 0.5625),
    "5/8-11": (0.625, 11, "UNC", 0.5312, 0.6406, 0.6562, 0.6875), "5/8-18": (0.625, 18, "UNF", 0.5781, 0.6406, 0.6562, 0.6875),
    "3/4-10": (0.750, 10, "UNC", 0.6562, 0.7656, 0.7812, 0.8125), "3/4-16": (0.750, 16, "UNF", 0.6875, 0.7656, 0.7812, 0.8125),
    "1-8": (1.000, 8, "UNC", 0.8750, 1.0156, 1.0312, 1.0625),
}
# inch hardware by base size (in): hex head (AF, height) ASME B18.2.1 / B18.6.3; socket head (dia, height) B18.3;
# hex nut (AF, thickness) B18.2.2; SAE flat washer (ID, OD, t)
HEX_IN = {"#4": (0.1875, 0.060), "#6": (0.250, 0.073), "#8": (0.250, 0.080), "#10": (0.3125, 0.110), "1/4": (0.4375, 0.1563),
          "5/16": (0.500, 0.2031), "3/8": (0.5625, 0.2344), "7/16": (0.625, 0.2813), "1/2": (0.750, 0.3125), "5/8": (0.9375, 0.3906),
          "3/4": (1.125, 0.4688), "1": (1.500, 0.6094)}
SOCKET_IN = {"#4": (0.183, 0.112), "#6": (0.226, 0.138), "#8": (0.270, 0.164), "#10": (0.312, 0.190), "1/4": (0.375, 0.250),
             "5/16": (0.469, 0.3125), "3/8": (0.5625, 0.375), "7/16": (0.656, 0.4375), "1/2": (0.750, 0.500), "5/8": (0.938, 0.625),
             "3/4": (1.125, 0.750), "1": (1.500, 1.000)}
NUT_IN = {"#4": (0.250, 0.098), "#6": (0.3125, 0.114), "#8": (0.34375, 0.130), "#10": (0.375, 0.130), "1/4": (0.4375, 0.2188),
          "5/16": (0.500, 0.2656), "3/8": (0.5625, 0.3281), "7/16": (0.6875, 0.375), "1/2": (0.750, 0.4375), "5/8": (0.9375, 0.5469),
          "3/4": (1.125, 0.6406), "1": (1.500, 0.8594)}
WASHER_IN = {"#4": (0.125, 0.312, 0.032), "#6": (0.156, 0.375, 0.049), "#8": (0.188, 0.438, 0.049), "#10": (0.219, 0.500, 0.049),
             "1/4": (0.281, 0.625, 0.065), "5/16": (0.344, 0.688, 0.065), "3/8": (0.406, 0.812, 0.065), "7/16": (0.469, 0.922, 0.065),
             "1/2": (0.531, 1.062, 0.095), "5/8": (0.656, 1.312, 0.095), "3/4": (0.812, 1.469, 0.134), "1": (1.062, 2.000, 0.134)}
_UN_BY_SIZE = {}
for _k in UN:
    _UN_BY_SIZE.setdefault(_k.split("-")[0], []).append(_k)


class ThreadError(Exception):
    pass


def _norm(size: str) -> str:
    """Canonical key: 'M4' (ISO) or '1/4-20' / '#10-32' (UN). Accepts 'm4', '4', 'M4x0.7', '1/4"-20', '.25-20', '1/4-20 UNC',
    '10-32' (a number size), '#10-32', '1/4' (coarse series assumed)."""
    raw = str(size).strip().upper().replace('"', "").replace("″", "")
    s = raw.replace(" ", "").replace("UNC", "").replace("UNF", "").replace("UN", "")
    if s.startswith("M") or s.replace(".", "").isdigit() and "-" not in s and "/" not in s and s in {k[1:] for k in ISO}:
        m = s if s.startswith("M") else "M" + s
        m = m.split("X")[0]
        if m.endswith(".0"):
            m = m[:-2]
        if m not in ISO:
            raise ThreadError(f"unknown thread size {size!r}; known: {', '.join(ISO)} and {', '.join(UN)}")
        return m
    base, _, tpi = s.partition("-")
    base = base.lstrip("#")
    if "/" not in base and "." in base:                       # decimal inch, e.g. .25 -> 1/4, 0.375 -> 3/8
        dec = float(base)
        base = next((k for k in _UN_BY_SIZE if not k.startswith("#") and abs(_frac(k) - dec) < 1e-4), base)
    if base.isdigit() and base not in _UN_BY_SIZE:           # number size given without '#'
        base = "#" + base
    if base not in _UN_BY_SIZE:
        raise ThreadError(f"unknown thread size {size!r}; known: {', '.join(ISO)} and {', '.join(UN)}")
    keys = _UN_BY_SIZE[base]
    if tpi:
        key = f"{base}-{int(float(tpi))}"
        if key not in UN:
            raise ThreadError(f"no {base} thread with {tpi} tpi; known: {', '.join(keys)}")
        return key
    return next(k for k in keys if UN[k][2] == "UNC")          # size only: coarse series


def _frac(k: str) -> float:
    if "/" in k:
        a, b = k.split("/"); return float(a) / float(b)
    return float(k.lstrip("#")) if not k.startswith("#") else -1


def thread(size: str) -> dict[str, Any]:
    """Thread data in mm for an ISO or Unified size: {size, system: 'iso'|'un', label, major, pitch, tpi, tap_drill,
    minor, clearance:{fine,medium,coarse}} (for UN the three fits are ASME close/normal/loose)."""
    s = _norm(size)
    if s in ISO:
        pitch, fine, med, coarse = ISO[s]
        major = float(s[1:])
        return {"size": s, "system": "iso", "label": f"{s}×{pitch:g}", "major": major, "pitch": pitch, "tpi": round(INCH / pitch, 2),
                "tap_drill": round(major - pitch, 2), "minor": round(major - 1.0825 * pitch, 3),
                "clearance": {"fine": fine, "medium": med, "coarse": coarse}}
    major_in, tpi, series, tap_in, close, normal, loose = UN[s]
    pitch = INCH / tpi
    return {"size": s, "system": "un", "label": f"{s} {series}", "series": series, "major": round(major_in * INCH, 3), "pitch": round(pitch, 4),
            "tpi": tpi, "tap_drill": round(tap_in * INCH, 3), "minor": round((major_in * INCH) - 1.0825 * pitch, 3),
            "clearance": {"fine": round(close * INCH, 3), "medium": round(normal * INCH, 3), "coarse": round(loose * INCH, 3)},
            "tap_drill_in": tap_in, "major_in": major_in}


iso = thread          # historical name; works for both systems


def _hardware(spec: dict[str, Any], kind: str) -> tuple:
    """(dims in mm) from the metric or inch hardware tables."""
    if spec["system"] == "iso":
        return {"hex": HEX, "socket": SOCKET, "nut": NUT, "washer": WASHER}[kind][spec["size"]]
    base = spec["size"].split("-")[0]
    tbl = {"hex": HEX_IN, "socket": SOCKET_IN, "nut": NUT_IN, "washer": WASHER_IN}[kind]
    if base not in tbl:
        raise ThreadError(f"no {kind} hardware table for {spec['size']}")
    return tuple(v * INCH for v in tbl[base])


def tap_drill(size: str) -> float:
    return iso(size)["tap_drill"]


def clearance_dia(size: str, fit: str = "medium") -> float:
    c = iso(size)["clearance"]
    if fit not in c:
        raise ThreadError("fit must be fine | medium | coarse")
    return c[fit]


# ---------------------------------------------------------------------------- registry (for drawings / inspect)
@dataclass
class ThreadSpec:
    size: str
    pitch: float
    at: tuple[float, float, float]        # start point (surface) in model coords
    axis: tuple[float, float, float]      # direction the hole goes
    depth: float | None
    through: bool
    real: bool
    external: bool = False

    def label(self, units: str = "mm") -> str:
        if self.through:
            d = "THRU"
        elif self.depth:
            d = f"↧{self.depth / INCH:.3f}".rstrip("0").rstrip(".") if units == "in" else f"↧{self.depth:g}"
        else:
            d = ""
        head = f"{self.size} {UN[self.size][2]}" if self.size in UN else f"{self.size}×{self.pitch:g}"
        return f"{head} {d}".strip()


_REGISTRY: ContextVar[list[ThreadSpec] | None] = ContextVar("agenticcad_threads", default=None)


def begin_registry() -> Any:
    return _REGISTRY.set([])


def end_registry(token) -> list[ThreadSpec]:
    out = _REGISTRY.get() or []
    _REGISTRY.reset(token)
    return out


def _register(spec: ThreadSpec) -> None:
    reg = _REGISTRY.get()
    if reg is not None:
        reg.append(spec)


# ---------------------------------------------------------------------------- geometry
def _iso_thread(major: float, pitch: float, length: float, external: bool, end_finishes=("fade", "fade")):
    from bd_warehouse.thread import IsoThread
    return IsoThread(major_diameter=major, pitch=pitch, length=length, external=external, end_finishes=end_finishes)


def bolt(size: str, length: float, head: str = "hex", real: bool = False, thread_length: float | None = None) -> Part:
    """A bolt with its head above z=0 and the shank going down −Z (so `Pos(x, y, surface_z) * bolt(...)`
    sits on a surface). head: hex | socket | none. real=True models the thread (slower, many faces)."""
    d = thread(size); s = d["size"]; major, pitch = d["major"], d["pitch"]
    tl = min(length, thread_length if thread_length is not None else length)
    shank_len = length - tl
    parts = []
    if head == "hex":
        af, hh = _hardware(d, "hex")
        hd = extrude(RegularPolygon(af / 2 / math.cos(math.pi / 6), 6), hh)
        parts.append(hd)
    elif head == "socket":
        hdia, hh = _hardware(d, "socket")
        hd = Cylinder(hdia / 2, hh, align=(Align.CENTER, Align.CENTER, Align.MIN))
        hd = hd - Pos(0, 0, hh) * extrude(RegularPolygon(0.5 * hdia / 2 / math.cos(math.pi / 6), 6), -hh * 0.6)  # hex socket
        parts.append(hd)
    elif head != "none":
        raise ThreadError("head must be hex | socket | none")
    if shank_len > 0:
        parts.append(Pos(0, 0, -shank_len) * Cylinder(major / 2, shank_len, align=(Align.CENTER, Align.CENTER, Align.MIN)))
    if real:
        core = Pos(0, 0, -length) * Cylinder(d["minor"] / 2 + 0.05, tl, align=(Align.CENTER, Align.CENTER, Align.MIN))
        thr = Pos(0, 0, -length) * _iso_thread(major, pitch, tl, True, ("fade", "fade" if shank_len > 0 else "fade"))
        parts.append(core + thr)
    else:
        parts.append(Pos(0, 0, -length) * Cylinder(major / 2, tl, align=(Align.CENTER, Align.CENTER, Align.MIN)))
    out = parts[0]
    for p in parts[1:]:
        out = out + p
    _register(ThreadSpec(s, pitch, (0, 0, 0), (0, 0, -1), length, False, real, external=True))
    return out


def nut(size: str, real: bool = False) -> Part:
    """Hex nut sitting on z=0 (bottom face) going +Z."""
    d = thread(size); s = d["size"]; af, h = _hardware(d, "nut")
    body = extrude(RegularPolygon(af / 2 / math.cos(math.pi / 6), 6), h)
    if real:
        body = body - Cylinder(d["major"] / 2, h * 3) + _iso_thread(d["major"], d["pitch"], h, False, ("fade", "fade"))
    else:
        body = body - Cylinder(d["tap_drill"] / 2, h * 3)
    return body


def washer(size: str) -> Part:
    inner, outer, t = _hardware(thread(size), "washer")
    return Cylinder(outer / 2, t, align=(Align.CENTER, Align.CENTER, Align.MIN)) - Cylinder(inner / 2, t * 3)


def tapped_hole(size: str, depth: float, at=(0, 0, 0), through: bool = False, axis=(0, 0, -1)) -> Part:
    """A cosmetic CUTTER for a tapped hole (tap-drill diameter) starting at model point `at` going along `axis`.
    Subtract it: `part -= tapped_hole("M4", 10, at=(x, y, top_z))`. For real thread geometry use tap(part, ...)."""
    d = thread(size)
    ax = Vector(*axis).normalized()
    length = depth + (0.3 if not through else 2.0)
    pl = Plane(origin=Vector(*at) - ax * 0.5, z_dir=ax * -1)
    cutter = pl * Cylinder(d["tap_drill"] / 2, length + 0.5, align=(Align.CENTER, Align.CENTER, Align.MAX))
    _register(ThreadSpec(d["size"], d["pitch"], tuple(float(v) for v in at), tuple(float(v) for v in ax),
                         None if through else depth, through, False))
    return cutter


def tap(part: Part, size: str, at, depth: float | None = None, through: bool = False, real: bool = False,
        axis=(0, 0, -1), clearance_depth: float = 0.0) -> Part:
    """Cut (and, if real, thread) a tapped hole into `part` starting at model point `at`, going along `axis`.
    through=True cuts all the way. Registers the thread so drawings call it out (e.g. 'M4×0.7 THRU', '1/4-20 UNC THRU')."""
    d = thread(size)
    ax = Vector(*axis).normalized()
    bb = part.bounding_box()
    span = (bb.max - bb.min).length + 2
    length = span if through else float(depth or 0) + 0.3
    if not through and not depth:
        raise ThreadError("tap(): give depth= or through=True")
    pl = Plane(origin=Vector(*at) - ax * 0.5, z_dir=ax * -1)          # origin 0.5 outside the surface, normal points OUT
    hole_d = d["major"] if real else d["tap_drill"]
    cutter = pl * Cylinder(hole_d / 2, length + 0.5, align=(Align.CENTER, Align.CENTER, Align.MAX))
    out = part - cutter
    if real:
        thr_len = length - 0.3 if not through else span
        thr = _iso_thread(d["major"], d["pitch"], thr_len, False, ("fade", "fade"))
        out = out + pl * (Pos(0, 0, -thr_len - 0.5) * thr)
    _register(ThreadSpec(d["size"], d["pitch"], tuple(float(v) for v in at), tuple(float(v) for v in ax),
                         None if through else float(depth), through, real))
    return out


def hole(part: Part, diameter: float, at, depth: float | None = None, through: bool = False, axis=(0, 0, -1),
         counterbore: tuple[float, float] | None = None, countersink: float | None = None) -> Part:
    """Plain (untapped) hole from surface point `at` along `axis`. through=True cuts all the way.
    counterbore=(dia, depth); countersink=dia (90°)."""
    ax = Vector(*axis).normalized()
    bb = part.bounding_box()
    span = (bb.max - bb.min).length + 2
    if not through and not depth:
        raise ThreadError("hole(): give depth= or through=True")
    length = span if through else float(depth)
    pl = Plane(origin=Vector(*at) - ax * 0.5, z_dir=ax * -1)
    out = part - pl * Cylinder(diameter / 2, length + 0.5, align=(Align.CENTER, Align.CENTER, Align.MAX))
    if counterbore:
        cd, cdepth = counterbore
        out = out - pl * Cylinder(cd / 2, cdepth + 0.5, align=(Align.CENTER, Align.CENTER, Align.MAX))
    if countersink:
        from build123d import Cone
        h = (countersink - diameter) / 2
        out = out - pl * (Pos(0, 0, 0.5) * Cone(countersink / 2 + 0.5, diameter / 2, h + 0.5, align=(Align.CENTER, Align.CENTER, Align.MAX)))
    return out


def namespace() -> dict[str, Any]:
    return {"iso": iso, "thread": thread, "unc": thread, "hole": hole, "tap_drill": tap_drill, "clearance_dia": clearance_dia, "bolt": bolt, "nut": nut,
            "washer": washer, "tapped_hole": tapped_hole, "tap": tap, "ISO_THREADS": ISO, "UN_THREADS": UN, "inch": INCH, "IN": INCH}
