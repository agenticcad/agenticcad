"""Shaft retention and location: circlips (DIN 471/472), E-clips (DIN 6799), parallel keys (DIN 6885), shaft
collars, dowel pins (ISO 8734), with the matching groove/keyway cutters. All on the Z axis unless noted."""
from __future__ import annotations

import math

from build123d import Align, Box, Cylinder, Part, Pos, Rot, SlotCenterToCenter, extrude

import threads as thr
from designkit.core import component

# DIN 471 external (shaft d1): (thickness s, groove dia d2, groove width m)
DIN471 = {3: (0.4, 2.8, 0.5), 4: (0.4, 3.8, 0.5), 5: (0.6, 4.8, 0.7), 6: (0.7, 5.7, 0.8), 7: (0.8, 6.7, 0.9), 8: (0.8, 7.6, 0.9),
          9: (1.0, 8.6, 1.1), 10: (1.0, 9.6, 1.1), 12: (1.0, 11.5, 1.1), 14: (1.0, 13.4, 1.1), 15: (1.0, 14.3, 1.1),
          16: (1.0, 15.2, 1.1), 17: (1.0, 16.2, 1.1), 18: (1.2, 17.0, 1.3), 20: (1.2, 19.0, 1.3), 22: (1.2, 21.0, 1.3),
          25: (1.2, 23.9, 1.3), 28: (1.5, 26.6, 1.6), 30: (1.5, 28.6, 1.6)}
# DIN 472 internal (bore d1): (thickness s, groove dia d2, groove width m)
DIN472 = {8: (0.8, 8.4, 0.9), 9: (0.8, 9.4, 0.9), 10: (1.0, 10.4, 1.1), 12: (1.0, 12.5, 1.1), 13: (1.0, 13.6, 1.1),
          14: (1.0, 14.6, 1.1), 15: (1.0, 15.7, 1.1), 16: (1.0, 16.8, 1.1), 17: (1.0, 17.8, 1.1), 18: (1.0, 19.0, 1.1),
          19: (1.0, 20.0, 1.1), 20: (1.0, 21.0, 1.1), 22: (1.0, 23.0, 1.1), 24: (1.2, 25.2, 1.3), 25: (1.2, 26.2, 1.3),
          26: (1.2, 27.2, 1.3), 28: (1.2, 29.4, 1.3), 30: (1.2, 31.4, 1.3), 32: (1.2, 33.7, 1.3), 35: (1.5, 37.0, 1.6)}
# DIN 6799 E-clip by groove dia: (shaft range lo, hi, outer dia d3, thickness s)
DIN6799 = {1.9: (2.0, 3.0, 4.5, 0.4), 2.3: (3.0, 4.0, 6.0, 0.6), 3.2: (4.0, 5.0, 7.3, 0.6), 4.0: (5.0, 7.0, 9.3, 0.7),
           5.0: (6.0, 8.0, 11.3, 0.7), 6.0: (7.0, 9.0, 12.3, 0.7), 7.0: (8.0, 11.0, 14.3, 0.9), 8.0: (9.0, 12.0, 16.3, 1.0),
           9.0: (10.0, 14.0, 18.8, 1.1), 10.0: (11.0, 15.0, 20.4, 1.2), 12.0: (13.0, 18.0, 23.4, 1.3), 15.0: (16.0, 24.0, 29.4, 1.5)}
# DIN 6885 A: shaft over..up to -> (key width b, height h, shaft keyway depth t1)
DIN6885 = [(6, 8, 2, 2, 1.2), (8, 10, 3, 3, 1.8), (10, 12, 4, 4, 2.5), (12, 17, 5, 5, 3.0), (17, 22, 6, 6, 3.5),
           (22, 30, 8, 7, 4.0), (30, 38, 10, 8, 5.0), (38, 44, 12, 8, 5.0), (44, 50, 14, 9, 5.5)]


def _lookup(table: dict, key: float, what: str):
    k = min(table, key=lambda x: abs(x - key))
    if abs(k - key) > 1e-6:
        raise ValueError(f"no {what} for {key:g} mm; tabulated: {', '.join(f'{x:g}' for x in table)}")
    return table[k]


def _ring(r_in: float, r_out: float, t: float) -> Part:
    return Cylinder(r_out, t, align=(Align.CENTER, Align.CENTER, Align.MIN)) - Cylinder(r_in, 3 * t)


def _circlip(r_groove: float, r_other: float, s: float, lug_side: float, gap_ang: float) -> Part:
    """Tapered-section circlip outline: ring between the groove radius and r_other, opening on -X with two lugs."""
    r_in, r_out = sorted((r_groove, r_other))
    body = _ring(r_in, r_out, s)
    gap = 2 * r_out * math.sin(math.radians(gap_ang / 2))
    body = body - Pos(-r_out, 0, s / 2) * Box(r_out, gap, 3 * s)
    lug_r = 0.55 * abs(r_other - r_groove) + 0.35
    for sy in (-1, 1):
        a = math.radians(180 - sy * (gap_ang / 2 + 12))
        rc = lug_side
        cx, cy = rc * math.cos(a), rc * math.sin(a)
        lug = Pos(cx, cy, 0) * (Cylinder(lug_r, s, align=(Align.CENTER, Align.CENTER, Align.MIN)) - Cylinder(0.45 * lug_r, 3 * s))
        body = body + lug
    return body


@component("shafts", "DIN 471 external circlip (retaining ring for shafts), seated in its groove", tags=["circlip", "retaining ring", "shaft", "snap ring"],
           standard="DIN 471", example='c = Pos(0, 0, 38) * kit.circlip_external(8)   # on an 8 mm shaft, groove at z 38',
           related=["circlip_groove_external", "shafts/retention"])
def circlip_external(shaft_d: float = 8) -> Part:
    """External circlip for a shaft of diameter `shaft_d`, as installed (inner edge at the groove diameter), bottom
    face at z = 0. Cut the groove with circlip_groove_external. Thickness and groove per DIN 471; the tapered section
    and lug outline are representative."""
    s, d2, _ = _lookup(DIN471, shaft_d, "DIN 471 circlip")
    w = 0.1 * shaft_d + 0.9
    return _circlip(d2 / 2, d2 / 2 + w, s, d2 / 2 + 0.75 * w, 35)


@component("shafts", "Groove cutter for a DIN 471 external circlip (subtract from the shaft)", tags=["circlip", "groove", "cutter", "shaft"],
           standard="DIN 471", example='shaft = shaft - Pos(0, 0, 38) * kit.circlip_groove_external(8)', related=["circlip_external"])
def circlip_groove_external(shaft_d: float = 8) -> Part:
    """Annular cutter: removes the shaft material between the groove diameter and the shaft, groove width m, from z = 0."""
    s, d2, m = _lookup(DIN471, shaft_d, "DIN 471 circlip")
    return _ring(d2 / 2, shaft_d / 2 + 1, m)


@component("shafts", "DIN 472 internal circlip (retaining ring for bores), seated in its groove", tags=["circlip", "retaining ring", "bore", "housing"],
           standard="DIN 472", example='c = Pos(0, 0, 28) * kit.circlip_internal(16)', related=["circlip_groove_internal", "ball_bearing"])
def circlip_internal(bore_d: float = 16) -> Part:
    """Internal circlip for a bore of diameter `bore_d`, as installed (outer edge at the groove diameter), bottom face
    at z = 0. Cut the groove with circlip_groove_internal."""
    s, d2, _ = _lookup(DIN472, bore_d, "DIN 472 circlip")
    w = 0.1 * bore_d + 0.9
    return _circlip(d2 / 2, d2 / 2 - w, s, d2 / 2 - 0.75 * w, 35)


@component("shafts", "Groove cutter for a DIN 472 internal circlip (subtract from the housing)", tags=["circlip", "groove", "cutter", "bore"],
           standard="DIN 472", example='cover = cover - Pos(0, 0, 28) * kit.circlip_groove_internal(16)', related=["circlip_internal"])
def circlip_groove_internal(bore_d: float = 16) -> Part:
    s, d2, m = _lookup(DIN472, bore_d, "DIN 472 circlip")
    return _ring(bore_d / 2 - 1, d2 / 2, m)


@component("shafts", "DIN 6799 E-clip (retaining washer) sized for a shaft, seated in its groove", tags=["e-clip", "retaining", "shaft"],
           standard="DIN 6799", example='e = Pos(0, 0, 39) * kit.e_clip(8)', related=["e_clip_groove"])
def e_clip(shaft_d: float = 8) -> Part:
    """E-clip for a shaft diameter, bottom face at z = 0, opening on -X, three contact lugs at the groove diameter."""
    size = next((g for g, (lo, hi, _, _) in DIN6799.items() if lo < shaft_d <= hi), None)
    if size is None:
        raise ValueError(f"no DIN 6799 E-clip for a {shaft_d:g} mm shaft")
    lo, hi, d3, s = DIN6799[size]
    r_o, r_g = d3 / 2, size / 2
    r_i = (shaft_d / 2 + r_g) / 2 + 0.35 * (r_o - shaft_d / 2)           # inner edge of the C, clear of the shaft
    body = _ring(r_i, r_o, s)
    opening = 2 * (shaft_d / 2 + 0.05)
    body = body - Pos(-r_o, 0, s / 2) * Box(2 * r_o, opening, 3 * s)
    for a in (0, 120, 240):                                           # lugs reaching into the groove
        lug = Rot(0, 0, a) * Pos((r_g + r_i + 0.4) / 2, 0, s / 2) * Box(r_i - r_g + 0.4, 0.35 * size + 0.5, s)   # overlaps the ring
        body = body + lug
    return body


@component("shafts", "Groove cutter for a DIN 6799 E-clip", tags=["e-clip", "groove", "cutter", "shaft"], standard="DIN 6799",
           example='shaft = shaft - Pos(0, 0, 39) * kit.e_clip_groove(8)', related=["e_clip"])
def e_clip_groove(shaft_d: float = 8) -> Part:
    size = next((g for g, (lo, hi, _, _) in DIN6799.items() if lo < shaft_d <= hi), None)
    if size is None:
        raise ValueError(f"no DIN 6799 E-clip for a {shaft_d:g} mm shaft")
    s = DIN6799[size][3]
    return _ring(size / 2, shaft_d / 2 + 1, s + 0.05)


def _key_size(shaft_d: float) -> tuple[float, float, float]:
    for lo, hi, b, h, t1 in DIN6885:
        if lo < shaft_d <= hi:
            return b, h, t1
    raise ValueError(f"no DIN 6885 key for a {shaft_d:g} mm shaft (6-50 mm tabulated)")


@component("shafts", "DIN 6885 form A parallel key (round ends), sized from the shaft diameter", tags=["key", "parallel key", "keyway", "shaft"],
           standard="DIN 6885", example='key = Pos(0, 0, 45) * kit.parallel_key(8, 12)   # seated on an Ø8 shaft along Z',
           related=["keyway_cutter", "shafts/retention"])
def parallel_key(shaft_d: float = 8, length: float = 12) -> Part:
    """Key seated in a keyway on the +X side of a shaft on the Z axis, running from z = 0 to z = length.
    Size (b × h) and keyway depth t1 per DIN 6885 for the shaft diameter."""
    b, h, t1 = _key_size(shaft_d)
    r_bottom = shaft_d / 2 - t1
    return Pos(r_bottom, 0, length / 2) * Rot(0, 90, 0) * Rot(0, 0, 90) * extrude(SlotCenterToCenter(length - b, b), h)


@component("shafts", "Keyway cutter for a DIN 6885 key (subtract from the shaft)", tags=["keyway", "cutter", "shaft"], standard="DIN 6885",
           example='shaft = shaft - Pos(0, 0, 45) * kit.keyway_cutter(8, 12)', related=["parallel_key"])
def keyway_cutter(shaft_d: float = 8, length: float = 12) -> Part:
    b, h, t1 = _key_size(shaft_d)
    r_bottom = shaft_d / 2 - t1
    return Pos(r_bottom, 0, length / 2) * Rot(0, 90, 0) * Rot(0, 0, 90) * extrude(SlotCenterToCenter(length - b, b), t1 + 1)


@component("shafts", "Set-screw shaft collar with a tapped radial hole and a set screw", tags=["collar", "shaft collar", "set screw", "stop"],
           example='c = Pos(0, 0, 50) * kit.shaft_collar(8)', related=["set_screw"])
def shaft_collar(bore: float = 8, od: float | None = None, width: float | None = None, real: bool = False) -> dict[str, Part]:
    """Collar on the Z axis, bottom at z = 0, with an M3/M4/M5 set screw on +X (size from the bore). Returns
    {"Collar": ..., "SetScrew": ...}."""
    from designkit.components.fasteners import set_screw
    od = od or round(2 * bore + 6)
    width = width or max(6.0, round(0.8 * bore + 3))
    size = "M3" if bore <= 6 else "M4" if bore <= 12 else "M5" if bore <= 20 else "M6"
    collar = Cylinder(od / 2, width, align=(Align.CENTER, Align.CENTER, Align.MIN)) - Cylinder(bore / 2, 3 * width)
    collar = collar.chamfer(0.5, None, [e for e in collar.edges() if e.geom_type.name == "CIRCLE" and abs(e.radius - od / 2) < 1e-6])
    collar = thr.tap(collar, size, at=(od / 2, 0, width / 2), through=False, depth=(od - bore) / 2 + 0.2, axis=(-1, 0, 0), real=real)
    length = (od - bore) / 2 - 0.3
    screw = Pos(od / 2 - 0.3, 0, width / 2) * Rot(0, 90, 0) * set_screw(size, round(length), point="cup", real=real)
    return {"Collar": collar, "SetScrew": screw}


@component("shafts", "ISO 8734 dowel pin with chamfered and rounded ends", tags=["dowel", "pin", "location"], standard="ISO 8734",
           example='p = Pos(10, 0, -4) * kit.dowel_pin(4, 12)', related=[])
def dowel_pin(d: float = 4, length: float = 12) -> Part:
    """Hardened dowel pin on the Z axis from z = 0 to z = length: one end chamfered 15°, the other domed slightly."""
    c = 0.1 * d
    pin = Cylinder(d / 2, length, align=(Align.CENTER, Align.CENTER, Align.MIN))
    edges = [e for e in pin.edges() if e.geom_type.name == "CIRCLE"]
    top = [e for e in edges if e.center().Z > length / 2]
    bot = [e for e in edges if e.center().Z < length / 2]
    pin = pin.chamfer(c, None, bot)
    return pin.fillet(min(0.2 * d, 0.45 * d), top)


@component("shafts", "Rigid shaft coupler joining two shafts (e.g. 5 mm motor to 8 mm lead screw) with set screws",
           tags=["coupler", "coupling", "rigid coupler", "shaft", "lead screw", "5x8"],
           example='c = Pos(0, 0, 24) * kit.rigid_coupler(5, 8)   # bore1 below, bore2 above', related=["jaw_coupling", "lead_screw_t8"])
def rigid_coupler(bore1: float = 5.0, bore2: float = 8.0, od: float = 18.0, length: float = 25.0) -> Part:
    """Set-screw rigid coupler on the Z axis from z = 0 to `length`: bore1 in the lower half, bore2 in the upper, two
    M3 radial set screws per half (tapped, 90° apart)."""
    body = Cylinder(od / 2, length, align=(Align.CENTER, Align.CENTER, Align.MIN))
    body -= Pos(0, 0, -1) * Cylinder(bore1 / 2, length / 2 + 1, align=(Align.CENTER, Align.CENTER, Align.MIN))
    body -= Pos(0, 0, length / 2) * Cylinder(bore2 / 2, length / 2 + 1, align=(Align.CENTER, Align.CENTER, Align.MIN))
    body = body.chamfer(0.5, None, [e for e in body.edges() if e.geom_type.name == "CIRCLE" and abs(e.radius - od / 2) < 1e-6])
    for z, bore in ((length / 4, bore1), (3 * length / 4, bore2)):
        for ang in (0, 90):
            a = math.radians(ang)
            body = thr.tap(body, "M3", at=(od / 2 * math.cos(a), od / 2 * math.sin(a), z), depth=(od - bore) / 2 + 0.3,
                           axis=(-math.cos(a), -math.sin(a), 0))
    return body


@component("shafts", "Jaw (spider) flexible coupling: two three-jaw hubs and an elastomer spider, with clamp set screws",
           tags=["coupling", "jaw coupling", "spider", "l-type", "flexible coupling", "shaft"],
           example='j = kit.jaw_coupling(5, 8)   # {"HubA", "Spider", "HubB"}, on the Z axis from z = 0', related=["rigid_coupler"])
def jaw_coupling(bore1: float = 5.0, bore2: float = 8.0, od: float = 25.0, hub_len: float = 10.0) -> dict[str, Part]:
    """L-type jaw coupling on the Z axis: HubA (bore1) from z = 0, jaws interleaving with HubB's (bore2) through an
    elastomer Spider; overall length 2 × hub_len + 10."""
    jaw_h, sp = 8.0, 2.0

    def hub(bore: float, rot: float) -> Part:
        h = Cylinder(od / 2, hub_len, align=(Align.CENTER, Align.CENTER, Align.MIN))
        for k in range(3):
            h += Rot(0, 0, rot + 120 * k) * (Pos(0, 0, hub_len) * _sector_solid(0.3 * od, od / 2, -27, 27, jaw_h))
        h -= Pos(0, 0, -1) * Cylinder(bore / 2, hub_len + jaw_h + 2, align=(Align.CENTER, Align.CENTER, Align.MIN))
        return thr.tap(h, "M3", at=(od / 2, 0, hub_len / 2), depth=(od - bore) / 2 + 0.3, axis=(-1, 0, 0))
    a = hub(bore1, 0.0)
    b = Pos(0, 0, 2 * hub_len + jaw_h + sp) * Rot(180, 0, 0) * hub(bore2, 60.0)
    spider = Pos(0, 0, hub_len) * Cylinder(0.3 * od - 0.2, jaw_h + sp, align=(Align.CENTER, Align.CENTER, Align.MIN))
    for k in range(6):
        spider += Rot(0, 0, 30 + 60 * k) * (Pos(0, 0, hub_len) * _sector_solid(0.3 * od - 0.5, od / 2 - 0.3, -2.5, 2.5, jaw_h + sp))
    spider -= Pos(0, 0, hub_len - 1) * Cylinder(max(bore1, bore2) / 2 + 0.5, jaw_h + sp + 2, align=(Align.CENTER, Align.CENTER, Align.MIN))
    spider = spider - a - b
    return {"HubA": a, "Spider": spider, "HubB": b}


def _sector_solid(r0: float, r1: float, a0: float, a1: float, h: float) -> Part:
    from build123d import Face, Vector, Wire
    n = 12
    pts = [(r1 * math.cos(math.radians(a0 + (a1 - a0) * k / n)), r1 * math.sin(math.radians(a0 + (a1 - a0) * k / n))) for k in range(n + 1)]
    pts += [(r0 * math.cos(math.radians(a1 - (a1 - a0) * k / n)), r0 * math.sin(math.radians(a1 - (a1 - a0) * k / n))) for k in range(n + 1)]
    return extrude(Face(Wire.make_polygon([Vector(x, y, 0) for x, y in pts], close=True)), h)
