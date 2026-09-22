"""
ISO metric threads for design scripts: tables (pitch, tap drill, clearance), cosmetic or real
(bd_warehouse IsoThread) geometry for bolts, nuts, tapped holes, and a per-run registry so
drawings can call out "M4×0.7 THRU" on the matching hole.
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


class ThreadError(Exception):
    pass


def _norm(size: str) -> str:
    s = str(size).upper().replace(" ", "")
    if not s.startswith("M"):
        s = "M" + s
    if s.endswith(".0"):
        s = s[:-2]
    if s not in ISO:
        raise ThreadError(f"unknown thread size {size!r}; known: {', '.join(ISO)}")
    return s


def iso(size: str) -> dict[str, Any]:
    """Thread data: {size, major, pitch, tap_drill, minor, clearance:{fine,medium,coarse}}."""
    s = _norm(size)
    pitch, fine, med, coarse = ISO[s]
    major = float(s[1:])
    return {"size": s, "major": major, "pitch": pitch, "tap_drill": round(major - pitch, 2),
            "minor": round(major - 1.0825 * pitch, 3), "clearance": {"fine": fine, "medium": med, "coarse": coarse}}


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

    def label(self) -> str:
        d = "THRU" if self.through else (f"↧{self.depth:g}" if self.depth else "")
        return f"{self.size}×{self.pitch:g} {d}".strip()


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
    d = iso(size); s = d["size"]; major, pitch = d["major"], d["pitch"]
    tl = min(length, thread_length if thread_length is not None else length)
    shank_len = length - tl
    parts = []
    if head == "hex":
        af, hh = HEX[s]
        hd = extrude(RegularPolygon(af / 2 / math.cos(math.pi / 6), 6), hh)
        parts.append(hd)
    elif head == "socket":
        hdia, hh = SOCKET[s]
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
    d = iso(size); s = d["size"]; af, h = NUT[s]
    body = extrude(RegularPolygon(af / 2 / math.cos(math.pi / 6), 6), h)
    if real:
        body = body - Cylinder(d["major"] / 2, h * 3) + _iso_thread(d["major"], d["pitch"], h, False, ("fade", "fade"))
    else:
        body = body - Cylinder(d["tap_drill"] / 2, h * 3)
    return body


def washer(size: str) -> Part:
    d = iso(size)["size"]; inner, outer, t = WASHER[d]
    return Cylinder(outer / 2, t, align=(Align.CENTER, Align.CENTER, Align.MIN)) - Cylinder(inner / 2, t * 3)


def tapped_hole(size: str, depth: float, at=(0, 0, 0), through: bool = False, axis=(0, 0, -1)) -> Part:
    """A cosmetic CUTTER for a tapped hole (tap-drill diameter) starting at model point `at` going along `axis`.
    Subtract it: `part -= tapped_hole("M4", 10, at=(x, y, top_z))`. For real thread geometry use tap(part, ...)."""
    d = iso(size)
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
    through=True cuts all the way. Registers the thread so drawings call it out (e.g. 'M4×0.7 THRU')."""
    d = iso(size)
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
    return {"iso": iso, "hole": hole, "tap_drill": tap_drill, "clearance_dia": clearance_dia, "bolt": bolt, "nut": nut,
            "washer": washer, "tapped_hole": tapped_hole, "tap": tap, "ISO_THREADS": ISO}
