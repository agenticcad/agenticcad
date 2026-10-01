"""Power transmission: involute spur gears, internal ring gears and complete planetary stages."""
from __future__ import annotations

from build123d import Align, BuildSketch, Circle, Cylinder, Mode, Part, Pos, Rectangle, Rot, add, chamfer, extrude

import gears
from designkit.core import component

component("transmission", "Involute spur gear solid (Z up, tooth 0 on +X), optional bore, hub and keyway",
          tags=["gear", "spur", "involute"], standard="ISO 53 basic rack",
          example='g = Pos(0, 0, 5) * kit.spur_gear(1.0, 24, 8, bore=5)', related=["transmission/spur-gears", "gear_centre_distance"])(gears.spur_gear)
component("transmission", "Closed involute gear outline as a Face (for sketches, e.g. ring gear cutters)",
          tags=["gear", "profile", "sketch", "involute"], related=["ring_gear"])(gears.involute_gear_profile)
component("transmission", "Centre distance of two meshing external spur gears", tags=["gear", "layout"])(gears.gear_centre_distance)
component("transmission", "Pitch, outside, root and base diameters of a gear", tags=["gear", "dimensions"])(gears.gear_dims)
component("transmission", "Planetary layout: ring teeth, ratio, planet positions and mesh phasing (verified)",
          tags=["gear", "planetary", "epicyclic", "layout"], related=["planetary_stage", "transmission/planetary-gear-sets"])(gears.planetary_layout)


@component("transmission", "Internal (ring) gear: housing outline with involute internal teeth, sketched then extruded",
           tags=["gear", "ring gear", "internal gear", "annulus", "planetary"],
           example='ring = Pos(0, 0, 9) * kit.ring_gear(0.6, 54, 18, outside=42.3, square=True, corner_chamfer=3)',
           related=["planetary_stage", "transmission/planetary-gear-sets"])
def ring_gear(module: float, teeth: int, thickness: float, outside: float | None = None, square: bool = False,
              corner_chamfer: float = 0.0, backlash: float = 0.04, rotation: float = 0.0, pressure_angle: float = 20.0) -> Part:
    """Ring gear on the Z axis from z = 0 to `thickness`. The outline is a circle of diameter `outside` (default: root
    diameter + 6 modules) or a square of that side (square=True, optional corner chamfer). The tooth spaces are an
    external involute profile with addendum 1.25 (so the ring gets root clearance), opened by `backlash`.
    `rotation` turns the teeth (planetary_layout gives the right value as ring_rotation)."""
    r_root = module * (teeth / 2 + 1.25)
    outside = outside or 2 * r_root + 6 * module
    if (outside / 2 if not square else outside / 2) <= r_root + module:
        raise gears.GearError("outside is too small for the ring's tooth roots")
    cutter = Rot(0, 0, rotation) * gears.involute_gear_profile(module, teeth, pressure_angle, 0.0, -backlash, addendum=1.25)
    with BuildSketch() as sk:
        if square:
            Rectangle(outside, outside)
            if corner_chamfer:
                chamfer(sk.vertices(), corner_chamfer)
        else:
            Circle(outside / 2)
        add(cutter, mode=Mode.SUBTRACT)
    return extrude(sk.sketch, thickness)


@component("transmission", "Complete simple planetary stage: sun, n phased planets and ring, meshing without interference",
           tags=["gear", "planetary", "epicyclic", "gearbox", "reduction"],
           example='st = kit.planetary_stage(0.6, 18, 18, 3, face_width=8, ring_outside=42.3, ring_square=True)\n'
                   'result = {"Stage": kit.place(st, Pos(0, 0, 13))}',
           related=["planetary_layout", "ring_gear", "transmission/planetary-gear-sets"])
def planetary_stage(module: float, sun_teeth: int, planet_teeth: int, planets: int = 3, face_width: float = 8.0,
                    sun_bore: float = 0.0, planet_bore: float = 0.0, ring_thickness: float | None = None,
                    ring_outside: float | None = None, ring_square: bool = False, ring_chamfer: float = 0.0,
                    backlash: float = 0.04) -> dict[str, Part]:
    """Sun at the origin, planets on their centre circle, ring around them; gears from z = 0 to face_width, the ring
    centred on the same mid-plane. Returns {"Sun", "Planet1".., "Ring"}; the layout (ratio, centre distance) is in
    kit.planetary_layout(module, sun_teeth, planet_teeth, planets). Checked interference-free for even and odd
    tooth counts."""
    L = gears.planetary_layout(module, sun_teeth, planet_teeth, planets)
    out: dict[str, Part] = {"Sun": gears.spur_gear(module, sun_teeth, face_width, bore=sun_bore, backlash=backlash)}
    for i, p in enumerate(L["planets"], 1):
        out[f"Planet{i}"] = Pos(p["x"], p["y"], 0) * Rot(0, 0, p["rotation"]) * gears.spur_gear(module, planet_teeth, face_width,
                                                                                                  bore=planet_bore, backlash=backlash)
    t = ring_thickness or face_width
    out["Ring"] = Pos(0, 0, (face_width - t) / 2) * ring_gear(module, L["ring_teeth"], t, outside=ring_outside, square=ring_square,
                                                             corner_chamfer=ring_chamfer, backlash=backlash, rotation=L["ring_rotation"])
    return out


@component("transmission", "GT2 timing pulley (2 mm pitch): toothed section, flanges, hub with two set-screw holes",
           tags=["pulley", "gt2", "timing belt", "belt drive", "3d printer"],
           example='p = kit.gt2_pulley(20, bore=5)   # on the Z axis, hub at the bottom (z = 0)',
           related=["transmission/belt-drives", "nema_stepper"])
def gt2_pulley(teeth: int = 20, bore: float = 5.0, belt_width: float = 6.0, hub: bool = True) -> Part:
    """GT2 pulley on the Z axis: hub (Ø16 × 7, two M3 radial set-screw holes at 90°) from z = 0, then a flange, the
    toothed section (belt width + 1 mm) and a top flange. Pitch diameter = teeth × 2 / π; outside diameter is
    0.508 mm smaller (pitch line). Tooth gaps are the GT2 radius form (R 0.555, 0.75 deep)."""
    import math
    pd = teeth * 2.0 / math.pi
    od = pd - 0.508
    tw = belt_width + 1.0
    fl_d, fl_t = od + 3.0, 1.0
    z0 = 7.0 if hub else 0.0
    teeth_solid = Pos(0, 0, z0 + fl_t) * Cylinder(od / 2, tw, align=(Align.CENTER, Align.CENTER, Align.MIN))
    for i in range(teeth):
        a = 2 * math.pi * i / teeth
        rc = od / 2 - 0.75 + 0.555
        teeth_solid -= Pos(rc * math.cos(a), rc * math.sin(a), z0 + fl_t - 1) * Cylinder(0.555, tw + 2, align=(Align.CENTER, Align.CENTER, Align.MIN))
    body = teeth_solid
    body += Pos(0, 0, z0) * Cylinder(fl_d / 2, fl_t, align=(Align.CENTER, Align.CENTER, Align.MIN))
    body += Pos(0, 0, z0 + fl_t + tw) * Cylinder(fl_d / 2, fl_t, align=(Align.CENTER, Align.CENTER, Align.MIN))
    if hub:
        body += Cylinder(8.0, z0, align=(Align.CENTER, Align.CENTER, Align.MIN))
    body -= Pos(0, 0, -1) * Cylinder(bore / 2, z0 + tw + 2 * fl_t + 2, align=(Align.CENTER, Align.CENTER, Align.MIN))
    if hub:
        import threads as thr
        for ang in (0, 90):
            a = math.radians(ang)
            body = thr.tap(body, "M3", at=(8.0 * math.cos(a), 8.0 * math.sin(a), 3.5), depth=8.0 - bore / 2 + 0.5,
                           axis=(-math.cos(a), -math.sin(a), 0))
    return body


@component("transmission", "T8 lead screw (8 mm, 2 mm pitch, 8 mm lead) with a brass flange nut",
           tags=["lead screw", "t8", "acme", "trapezoidal", "linear motion", "3d printer", "z axis"],
           example='ls = kit.lead_screw_t8(300)   # {"Screw", "Nut"}; screw along +Z from z = 0, nut at nut_z',
           related=["transmission/belt-drives", "rigid_coupler"])
def lead_screw_t8(length: float = 300.0, nut_z: float = 100.0) -> dict[str, Part]:
    """Lead screw along +Z (Ø8, cosmetic thread) and the standard brass flange nut: flange Ø22 × 3.5 with four
    Ø3.5 holes on a Ø16 circle, body Ø10.2, 15 mm overall, flange at z = nut_z facing +Z."""
    screw = Cylinder(4.0, length, align=(Align.CENTER, Align.CENTER, Align.MIN))
    nut = Pos(0, 0, nut_z) * Cylinder(11.0, 3.5, align=(Align.CENTER, Align.CENTER, Align.MIN))
    nut += Pos(0, 0, nut_z - 11.5) * Cylinder(5.1, 11.5, align=(Align.CENTER, Align.CENTER, Align.MIN))
    nut -= Pos(0, 0, nut_z - 12.5) * Cylinder(4.0, 17.0, align=(Align.CENTER, Align.CENTER, Align.MIN))
    import math
    for k in range(4):
        a = math.radians(45 + 90 * k)
        nut -= Pos(8.0 * math.cos(a), 8.0 * math.sin(a), nut_z - 1) * Cylinder(1.75, 6, align=(Align.CENTER, Align.CENTER, Align.MIN))
    return {"Screw": screw, "Nut": nut}
