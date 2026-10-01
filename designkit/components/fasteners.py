"""Fasteners: ISO socket head, button head and countersunk screws, set screws, hex bolts, nuts and washers.

Conventions (the same as threads.bolt): a screw's head sits ABOVE z = 0 and the shank runs down -Z, so
`Pos(x, y, surface_z) * kit.socket_screw("M3", 12)` seats the head on a surface. Countersunk heads and set screws
are flush: their top face is at z = 0. Nuts sit on z = 0 and go up +Z.
real=True models the ISO thread helix (slower; identical ones are built once and reused). Default is a plain
cylinder at the major diameter with the thread registered for drawings.
"""
from __future__ import annotations

import copy
import functools
import math

from build123d import Align, Axis, Cone, Cylinder, Face, Part, Pos, RegularPolygon, Rot, Vector, Wire, extrude, revolve

import threads as thr
from designkit.core import component

# size: (head dia dk, head height k, socket AF s, socket depth t)
ISO4762 = {"M2": (3.8, 2.0, 1.5, 1.0), "M2.5": (4.5, 2.5, 2.0, 1.1), "M3": (5.5, 3.0, 2.5, 1.3), "M4": (7.0, 4.0, 3.0, 2.0),
           "M5": (8.5, 5.0, 4.0, 2.5), "M6": (10.0, 6.0, 5.0, 3.0), "M8": (13.0, 8.0, 6.0, 4.0), "M10": (16.0, 10.0, 8.0, 5.0),
           "M12": (18.0, 12.0, 10.0, 6.0), "M16": (24.0, 16.0, 14.0, 8.0)}
ISO7380 = {"M3": (5.7, 1.65, 2.0, 1.04), "M4": (7.6, 2.2, 2.5, 1.3), "M5": (9.5, 2.75, 3.0, 1.56), "M6": (10.5, 3.3, 4.0, 2.08),
           "M8": (14.0, 4.4, 5.0, 2.6), "M10": (17.5, 5.5, 6.0, 3.12), "M12": (21.0, 6.6, 8.0, 4.16)}
ISO10642 = {"M3": (6.72, 1.86, 2.0, 1.1), "M4": (8.96, 2.48, 2.5, 1.5), "M5": (11.2, 3.1, 3.0, 1.9), "M6": (13.44, 3.72, 4.0, 2.2),
            "M8": (17.92, 4.96, 5.0, 3.0), "M10": (22.4, 6.2, 6.0, 3.6), "M12": (26.88, 7.44, 8.0, 4.3)}
ISO4026_SOCKET = {"M2": (0.9, 1.0), "M2.5": (1.3, 1.1), "M3": (1.5, 1.2), "M4": (2.0, 1.5), "M5": (2.5, 2.0), "M6": (3.0, 2.0),
                  "M8": (4.0, 3.0), "M10": (5.0, 4.0), "M12": (6.0, 4.8)}
# hex AF s, bolt head height k, nut height m (ISO 4017 / ISO 4032)
HEX = {"M2": (4.0, 1.4, 1.6), "M2.5": (5.0, 1.7, 2.0), "M3": (5.5, 2.0, 2.4), "M4": (7.0, 2.8, 3.2), "M5": (8.0, 3.5, 4.7),
       "M6": (10.0, 4.0, 5.2), "M8": (13.0, 5.3, 6.8), "M10": (16.0, 6.4, 8.4), "M12": (18.0, 7.5, 10.8), "M16": (24.0, 10.0, 14.8)}


def _cached(fn):
    """Build each distinct fastener once per process; hand out cheap copies (they share geometry, move independently).
    The thread mode is part of the key, so a draft (plain) build is never reused for a real-thread request."""
    @functools.lru_cache(maxsize=256)
    def cached(mode, *args, **kwargs):
        return fn(*args, **kwargs)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if kwargs.get("real") and thr.thread_mode() == "draft":
            thr.use_real(True)                      # count the downgrade; the inner helpers then build plain
        return copy.copy(cached(thr.thread_mode(), *args, **kwargs))
    wrapper.__wrapped__ = fn
    return wrapper


def _size(size: str, table: dict) -> str:
    s = thr.thread(size)["size"]
    if s not in table:
        raise thr.ThreadError(f"{s} not tabulated here; available: {', '.join(table)}")
    return s


def _iso_thread_length(d: float, length: float) -> float:
    b = 2 * d + 12                          # ISO 4762 / 4017-4014: thread length for l up to 125 mm
    return length if length <= b + 2 * d else b


def _shank(size: str, length: float, real: bool, thread_length: float | None) -> Part:
    return thr.bolt(size, length, head="none", real=real, thread_length=thread_length)


def _hex_socket(af: float, depth: float, top_z: float) -> Part:
    r = af / 2 / math.cos(math.pi / 6)
    sock = Pos(0, 0, top_z - depth) * extrude(RegularPolygon(r, 6), depth + 0.01)
    cone_h = r * math.tan(math.radians(31))                             # 118° drill point at the socket bottom
    return sock + Pos(0, 0, top_z - depth - cone_h) * Cone(0, r, cone_h, align=(Align.CENTER, Align.CENTER, Align.MIN))


def _revolved(points: list[tuple[float, float]]) -> Part:
    """Solid of revolution about Z from an (r, z) outline."""
    face = Face(Wire.make_polygon([Vector(r, 0, z) for r, z in points], close=True))
    return revolve(face, Axis.Z)


@component("fasteners", "ISO 4762 / DIN 912 socket head cap screw", tags=["screw", "socket", "cap screw", "shcs", "metric"],
           standard="ISO 4762", example='s = Pos(0, 0, 10) * kit.socket_screw("M3", 16, real=True)',
           related=["button_head_screw", "countersunk_screw", "fasteners/choosing-fasteners"])
@_cached
def socket_screw(size: str = "M3", length: float = 10, real: bool = False, thread_length: float | None = None) -> Part:
    """Socket head cap screw. Head (with chamfered top edge and hex socket) above z = 0, shank down -Z for `length`.
    Longer screws are partly threaded per ISO (2d + 12 mm) unless thread_length is given."""
    s = _size(size, ISO4762)
    dk, k, af, t = ISO4762[s]
    d = thr.thread(s)["major"]
    tl = thread_length if thread_length is not None else _iso_thread_length(d, length)
    c = 0.08 * d
    head = _revolved([(0, 0), (dk / 2, 0), (dk / 2, k - c), (dk / 2 - c, k), (0, k)])
    head = head - _hex_socket(af, t, k)
    return head + _shank(s, length, real, tl)


@component("fasteners", "ISO 7380 button head socket screw", tags=["screw", "button", "socket", "metric"], standard="ISO 7380",
           example='s = Pos(0, 0, 3) * kit.button_head_screw("M4", 10)', related=["socket_screw"])
@_cached
def button_head_screw(size: str = "M4", length: float = 10, real: bool = False) -> Part:
    """Button head screw: domed head above z = 0 with a hex socket, fully threaded shank down -Z."""
    s = _size(size, ISO7380)
    dk, k, af, t = ISO7380[s]
    pts = [(0, 0), (dk / 2, 0), (dk / 2, 0.2 * k)]
    for i in range(1, 9):                                          # elliptical dome
        a = math.pi / 2 * i / 8
        pts.append((dk / 2 * math.cos(a), 0.2 * k + 0.8 * k * math.sin(a)))
    pts[-1] = (0, k)
    head = _revolved(pts) - _hex_socket(af, t, k)
    return head + _shank(s, length, real, None)


@component("fasteners", "ISO 10642 countersunk (flat head) socket screw, 90° head", tags=["screw", "countersunk", "flat head", "csk", "metric"],
           standard="ISO 10642", example='s = Pos(0, 0, 5) * kit.countersunk_screw("M3", 12)', related=["socket_screw"])
@_cached
def countersunk_screw(size: str = "M3", length: float = 10, real: bool = False) -> Part:
    """Countersunk socket screw. The head's top face is flush at z = 0 (length includes the head), cone and shank go
    down -Z. Cut its seat with hole(..., countersink=<head dia>)."""
    s = _size(size, ISO10642)
    dk, k, af, t = ISO10642[s]
    d = thr.thread(s)["major"]
    head = _revolved([(0, 0), (dk / 2, 0), (d / 2, -(dk - d) / 2), (0, -(dk - d) / 2)])
    head = head - _hex_socket(af, t, 0.0)
    return head + _shank(s, length, real, None)


@component("fasteners", "ISO 4026-4029 hex socket set screw (grub screw): flat, cup, cone or dog point",
           tags=["set screw", "grub screw", "socket", "metric"], standard="ISO 4026/4027/4028/4029",
           example='g = Pos(5, 0, 11) * Rot(0, 90, 0) * kit.set_screw("M3", 3, point="cup")', related=["shaft_collar"])
@_cached
def set_screw(size: str = "M3", length: float = 4, point: str = "cup", real: bool = False) -> Part:
    """Headless set screw, top face at z = 0 with a hex socket, body down -Z. point: flat | cup | cone | dog."""
    s = _size(size, ISO4026_SOCKET)
    af, t = ISO4026_SOCKET[s]
    d = thr.thread(s)["major"]
    body = _shank(s, length, real, None)
    body = body - _hex_socket(af, min(t, 0.6 * length), 0.0)
    tip = Pos(0, 0, -length)
    if point == "cup":
        body = body - tip * Cone(0.25 * d, 0.0, 0.25 * d, align=(Align.CENTER, Align.CENTER, Align.MIN))
    elif point == "cone":
        body = body - tip * (Cylinder(d, 0.5 * d, align=(Align.CENTER, Align.CENTER, Align.MIN))
                             - Cone(0, d / 2, 0.5 * d, align=(Align.CENTER, Align.CENTER, Align.MIN)))
    elif point == "dog":
        body = body - tip * (Cylinder(d, 0.3 * d, align=(Align.CENTER, Align.CENTER, Align.MIN)) - Cylinder(0.33 * d, 0.3 * d, align=(Align.CENTER, Align.CENTER, Align.MIN)))
    elif point != "flat":
        raise ValueError("point must be flat | cup | cone | dog")
    return body


def _chamfered_hex(af: float, h: float, both: bool) -> Part:
    e = af / math.cos(math.pi / 6)
    c = (e - 0.95 * af) / 2
    dz = c * math.tan(math.radians(30))
    pts = [(0, 0), (0.95 * af / 2, 0), (e / 2, dz) if both else (e / 2, 0), (e / 2, h - dz), (0.95 * af / 2, h), (0, h)]
    return extrude(RegularPolygon(e / 2, 6), h) & _revolved(pts)


@component("fasteners", "ISO 4017 / 4014 hex head bolt with chamfered head", tags=["bolt", "hex", "metric"], standard="ISO 4017 / ISO 4014",
           example='b = Pos(0, 0, 8) * kit.hex_bolt("M6", 30)', related=["hex_nut", "washer"])
@_cached
def hex_bolt(size: str = "M6", length: float = 20, real: bool = False, thread_length: float | None = None) -> Part:
    """Hex bolt: chamfered hex head above z = 0, shank down -Z. Fully threaded (ISO 4017) unless long enough for a
    plain shank (ISO 4014) or thread_length is given."""
    s = _size(size, HEX)
    af, k, _ = HEX[s]
    d = thr.thread(s)["major"]
    tl = thread_length if thread_length is not None else _iso_thread_length(d, length)
    return _chamfered_hex(af, k, both=False) + _shank(s, length, real, tl)


@component("fasteners", "ISO 4032 hex nut, chamfered both sides", tags=["nut", "hex", "metric"], standard="ISO 4032",
           example='n = Pos(0, 0, -12) * kit.hex_nut("M6")', related=["hex_bolt", "washer"])
@_cached
def hex_nut(size: str = "M6", real: bool = False) -> Part:
    """Hex nut sitting on z = 0, going up +Z. real=True cuts the internal thread; otherwise the bore is the tap drill."""
    s = _size(size, HEX)
    af, _, m = HEX[s]
    d = thr.thread(s)
    real = thr.use_real(real)
    body = _chamfered_hex(af, m, both=True)
    if real:
        return thr._union(body - Pos(0, 0, -1) * Cylinder(d["major"] / 2, m + 2, align=(Align.CENTER, Align.CENTER, Align.MIN)),
                          thr._iso_thread(d["major"], d["pitch"], m, False, ("fade", "fade")))
    return body - Pos(0, 0, -1) * Cylinder(d["tap_drill"] / 2, m + 2, align=(Align.CENTER, Align.CENTER, Align.MIN))


@component("fasteners", "ISO 7089 plain washer", tags=["washer", "metric"], standard="ISO 7089",
           example='w = Pos(0, 0, 5) * kit.washer("M6")', related=["hex_bolt", "hex_nut"])
def washer(size: str = "M6") -> Part:
    """Flat washer sitting on z = 0, going up +Z."""
    return thr.washer(size)


# heat-set inserts (typical "standard" brass inserts for 3D prints): size: (outer dia, length, recommended hole dia)
INSERTS = {"M2": (3.6, 4.0, 3.2), "M2.5": (4.0, 5.7, 3.6), "M3": (4.6, 5.7, 4.0), "M4": (6.3, 8.1, 5.6), "M5": (7.1, 9.5, 6.4)}


@component("fasteners", "Brass heat-set threaded insert for 3D-printed parts, knurled", tags=["insert", "heat-set", "threaded insert", "3d printing", "brass"],
           example='part = part - Pos(0, 0, 10) * kit.insert_hole_cutter("M3")\ni = Pos(0, 0, 10) * kit.heat_set_insert("M3")',
           related=["insert_hole_cutter", "screw_boss", "manufacturing/fdm-3d-printing"])
def heat_set_insert(size: str = "M3") -> Part:
    """Insert with its top face at z = 0 (flush with the part surface) going down -Z: two knurled bands, a plain lead-in,
    and the threaded bore (tap-drill diameter). Sizes follow common standard-length inserts; check your supplier."""
    s = _size(size, INSERTS)
    od, L, _ = INSERTS[s]
    d = thr.thread(s)
    n = 18
    pts = []
    for i in range(2 * n):
        a = math.pi * i / n
        r = od / 2 if i % 2 == 0 else od / 2 - 0.25
        pts.append((r * math.cos(a), r * math.sin(a)))
    knurl = extrude(Face(Wire.make_polygon([Vector(x, y, 0) for x, y in pts], close=True)), L * 0.35)
    body = Pos(0, 0, -L * 0.4) * knurl + Pos(0, 0, -L * 0.85) * Rot(0, 0, 360 / (2 * n)) * knurl
    body += Pos(0, 0, -L) * Cylinder(od / 2 - 0.35, L, align=(Align.CENTER, Align.CENTER, Align.MIN))
    body -= Pos(0, 0, -L - 1) * Cylinder(d["tap_drill"] / 2, L + 2, align=(Align.CENTER, Align.CENTER, Align.MIN))
    return body


@component("fasteners", "Hole cutter for a heat-set insert (recommended hole plus lead-in chamfer and relief below)",
           tags=["insert", "heat-set", "hole", "cutter", "3d printing"], related=["heat_set_insert", "screw_boss"])
def insert_hole_cutter(size: str = "M3", extra_depth: float = 1.5) -> Part:
    """Cutter from z = 0 (the surface) down: insert hole of the recommended diameter, 0.4 mm lead-in chamfer, and
    `extra_depth` of relief below the insert for displaced plastic."""
    s = _size(size, INSERTS)
    od, L, hole = INSERTS[s]
    cut = Pos(0, 0, -L - extra_depth) * Cylinder(hole / 2, L + extra_depth + 0.01, align=(Align.CENTER, Align.CENTER, Align.MIN))
    return cut + Pos(0, 0, -0.4) * Cone(hole / 2, hole / 2 + 0.4, 0.41, align=(Align.CENTER, Align.CENTER, Align.MIN))


STANDOFF_AF = {"M2": 4.0, "M2.5": 5.0, "M3": 5.5, "M4": 7.0}


@component("fasteners", "Hex standoff (spacer) M-F, F-F or M-M, for PCBs and panels", tags=["standoff", "spacer", "pcb", "hex", "brass", "nylon"],
           example='s = Pos(3.5, 3.5, 0) * kit.standoff("M2.5", 6, kind="MF")', related=["arduino_uno", "raspberry_pi_4", "electronics/pcb-mounting-and-venting"])
def standoff(size: str = "M3", length: float = 10.0, kind: str = "FF") -> Part:
    """Hex standoff on the Z axis from z = 0 to `length` (hex body). kind: FF (tapped both ends), MF (male stud below
    z = 0, tapped top) or MM (studs both ends). Stud length = one diameter + 1 mm."""
    s = _size(size, STANDOFF_AF)
    af = STANDOFF_AF[s]
    d = thr.thread(s)
    body = extrude(RegularPolygon(af / 2 / math.cos(math.pi / 6), 6), length)
    stud = d["major"] + 1
    tap_d = min(length / 2 - 0.3, 1.5 * d["major"])
    if kind in ("FF", "MF"):
        body -= Pos(0, 0, length - tap_d) * Cylinder(d["tap_drill"] / 2, tap_d + 1, align=(Align.CENTER, Align.CENTER, Align.MIN))
    if kind == "FF":
        body -= Pos(0, 0, -1) * Cylinder(d["tap_drill"] / 2, tap_d + 1, align=(Align.CENTER, Align.CENTER, Align.MIN))
    if kind in ("MF", "MM"):
        body += Pos(0, 0, -stud) * Cylinder(d["major"] / 2, stud, align=(Align.CENTER, Align.CENTER, Align.MIN))
    if kind == "MM":
        body += Pos(0, 0, length) * Cylinder(d["major"] / 2, stud, align=(Align.CENTER, Align.CENTER, Align.MIN))
    if kind not in ("FF", "MF", "MM"):
        raise ValueError("kind must be FF | MF | MM")
    return body
