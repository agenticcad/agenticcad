"""Structural framing: T-slot aluminium extrusions, corner brackets and T-nuts."""
from __future__ import annotations

from build123d import Align, Box, Cylinder, Face, Part, Pos, Rot, Vector, Wire, extrude

import threads as thr
from designkit.core import component

# series: (cell size a, slot opening w, lip thickness l, slot depth d, undercut width W, centre bore, screw)
TSLOT = {20: (20.0, 6.2, 1.8, 6.1, 11.0, 4.2, "M5"), 30: (30.0, 8.2, 2.2, 9.0, 16.5, 6.8, "M6"), 40: (40.0, 8.2, 4.3, 12.3, 20.0, 8.4, "M8")}
PROFILES = {"2020": (20, 1, 1), "2040": (20, 2, 1), "2060": (20, 3, 1), "2080": (20, 4, 1), "3030": (30, 1, 1), "3060": (30, 2, 1),
            "4040": (40, 1, 1), "4080": (40, 2, 1)}


def _poly(pts):
    return Face(Wire.make_polygon([Vector(x, y, 0) for x, y in pts], close=True))


def _slot(series: int) -> Face:
    """Slot cutter outline for a slot opening on +X at x = a/2 (local to one cell)."""
    a, w, l, d, W, _, _ = TSLOT[series]
    h = a / 2
    t = (W - w) / 2
    return _poly([(h + 1, w / 2), (h - l, w / 2), (h - l, W / 2), (h - d + t, W / 2), (h - d, w / 2),
                  (h - d, -w / 2), (h - d + t, -W / 2), (h - l, -W / 2), (h - l, -w / 2), (h + 1, -w / 2)])


@component("structural", "Cross-section Face of a T-slot extrusion, for sketches or a custom extrude",
           tags=["extrusion", "profile", "t-slot", "sketch"], related=["extrusion"])
def extrusion_profile(profile: str = "2020") -> Face:
    """The cross-section Face of a T-slot extrusion, centred on the origin (long side along X)."""
    if profile not in PROFILES:
        raise ValueError(f"profile must be one of {', '.join(PROFILES)}")
    series, nx, _ = PROFILES[profile]
    a, *_, bore, _ = TSLOT[series]
    face = _poly([(-nx * a / 2, -a / 2), (nx * a / 2, -a / 2), (nx * a / 2, a / 2), (-nx * a / 2, a / 2)])
    slot = _slot(series)
    for i in range(nx):
        cx = (i - (nx - 1) / 2) * a
        face -= Pos(cx, 0) * Rot(0, 0, 90) * slot            # slot on the +Y face of this cell
        face -= Pos(cx, 0) * Rot(0, 0, -90) * slot           # and on the -Y face
        face -= Pos(cx, 0) * _circle(bore / 2)               # centre bore (tap drill for the end screw)
    face -= Pos(-(nx - 1) / 2 * a, 0) * Rot(0, 0, 180) * slot  # end slots
    face -= Pos((nx - 1) / 2 * a, 0) * slot
    for i in range(nx - 1):                                  # core void between cells
        cx = (i + 1 - nx / 2) * a
        v = 0.3 * a
        face -= Pos(cx, 0) * _poly([(0, -v / 2), (v / 2, 0), (0, v / 2), (-v / 2, 0)])
    return face


def _circle(r: float, n: int = 48) -> Face:
    import math
    return _poly([(r * math.cos(2 * math.pi * k / n), r * math.sin(2 * math.pi * k / n)) for k in range(n)])


@component("structural", "T-slot aluminium extrusion (2020, 2040, 3030, 4040 ...) cut to length, with the centre bores",
           tags=["extrusion", "aluminium profile", "t-slot", "2020", "2040", "3030", "4040", "frame", "misumi"],
           example='rail = kit.extrusion("2040", 300)                       # along +Z from z = 0\n'
                   'beam = Pos(0, 0, 10) * Rot(0, 90, 0) * kit.extrusion("2020", 200)   # along +X',
           related=["corner_bracket", "t_nut", "structural/extrusion-frames"])
def extrusion(profile: str = "2020", length: float = 100.0) -> Part:
    """Extrusion along +Z from z = 0 to `length`, centred on the Z axis, long side of 2040/4080 along X.
    Slots are the common B-type shape (opening 6.2 mm on 20 series, 8.2 on 30/40); the centre bore is tap-drill
    size for the end screw (M5 / M6 / M8). Representative of Misumi/generic profiles; lips and radii vary by brand."""
    return extrude(extrusion_profile(profile), length)


@component("structural", "Cast corner bracket for T-slot extrusion with a gusset and a slotted hole in each leg",
           tags=["bracket", "corner bracket", "angle", "extrusion", "gusset"],
           example='b = Pos(0, 0, 10) * kit.corner_bracket(20)   # legs along +X and +Z, inner corner at the origin',
           related=["extrusion", "t_nut"])
def corner_bracket(series: int = 20) -> Part:
    """L bracket for the 20/30/40 series: legs along +X and +Z from the origin (the inner corner), width along Y,
    a slotted hole centred in each leg for the series' screw, and a triangular rib along each edge."""
    a, *_, screw = TSLOT[series]
    L, w, t = a, a - 2, max(3.0, 0.15 * a)
    part = Box(L, w, t, align=(Align.MIN, Align.CENTER, Align.MAX)) + Box(t, w, L, align=(Align.MAX, Align.CENTER, Align.MIN))
    g = min(2.5, 0.12 * a)
    rib = Rot(90, 0, 0) * extrude(_poly([(0, 0), (L - 1, 0), (0, L - 1)]), g)      # triangle in XZ, thickness along -Y
    for y in (w / 2, -w / 2 + g):
        part += Pos(0, y, 0) * rib
    d = thr.clearance_dia(screw)
    part -= Pos(0.6 * L, 0, -t / 2) * Box(1.6 * d, d, 4 * t)
    part -= Pos(-t / 2, 0, 0.6 * L) * Box(4 * t, d, 1.6 * d)
    return part


@component("structural", "Drop-in T-nut for a T-slot extrusion, with a tapped hole", tags=["t-nut", "slot nut", "hammer nut", "extrusion"],
           example='n = Pos(0, 0, 10) * Rot(0, 90, 0) * kit.t_nut(20)', related=["extrusion", "corner_bracket"])
def t_nut(series: int = 20) -> Part:
    """T-nut sitting in the slot: its top face (the neck) at z = 0 flush with the extrusion face, body below,
    long side along X, tapped for the series' screw (M5 / M6 / M8)."""
    a, w, l, d, W, _, screw = TSLOT[series]
    body = Pos(0, 0, -l) * Box(1.1 * W, W - 0.6, d - l - 0.4, align=(Align.CENTER, Align.CENTER, Align.MAX))
    neck = Box(1.1 * W, w - 0.4, l, align=(Align.CENTER, Align.CENTER, Align.MAX))
    nut = body + neck
    return thr.tap(nut, screw, at=(0, 0, 0), through=True)
