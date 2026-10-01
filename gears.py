"""Involute spur gears for scripts (pre-imported like the thread helpers).

Hand-rolled involute profiles are the classic way to get "Edges are disconnected": the flank, tip and root
pieces have to meet exactly. These helpers build the whole outline as one ordered point list and close it
with a single Polyline, so the face is always valid.

    spur_gear(module=2, teeth=20, thickness=10, bore=8)          -> Solid, Z up, tooth 0 on +X
    involute_gear_profile(module=2, teeth=20)                     -> Face on the XY plane
    gear_centre_distance(module=2, teeth_a=20, teeth_b=40)        -> 60.0 (mm)
    gear_dims(module=2, teeth=20) -> dict(pitch_d, outside_d, root_d, base_d, ...)
    planetary_layout(module=1, sun_teeth=12, planet_teeth=18)  -> ring teeth, ratio, planet positions + phase
"""
from __future__ import annotations

import math
from typing import Any

import build123d as b3d

from threads import builder_safe


class GearError(Exception):
    pass


def gear_dims(module: float, teeth: int, pressure_angle: float = 20.0, clearance: float = 0.25, addendum: float = 1.0) -> dict[str, float]:
    if teeth < 4:
        raise GearError("a gear needs at least 4 teeth")
    if module <= 0:
        raise GearError("module must be positive (mm per tooth of pitch diameter / pi)")
    r_p = module * teeth / 2
    return {"module": module, "teeth": teeth, "pressure_angle": pressure_angle,
            "pitch_d": 2 * r_p, "outside_d": 2 * (r_p + addendum * module), "root_d": 2 * (r_p - (addendum + clearance) * module),
            "base_d": 2 * r_p * math.cos(math.radians(pressure_angle)), "circular_pitch": math.pi * module}


def gear_centre_distance(module: float, teeth_a: int, teeth_b: int) -> float:
    """Centre distance for two meshing external spur gears of the same module."""
    return module * (teeth_a + teeth_b) / 2


@builder_safe
def involute_gear_profile(module: float, teeth: int, pressure_angle: float = 20.0, clearance: float = 0.25,
                          backlash: float = 0.0, addendum: float = 1.0, flank_points: int = 10, arc_points: int = 4) -> b3d.Face:
    """Closed involute tooth outline as one Face on XY (centre at the origin, tooth 0 centred on +X).
    `backlash` thins every tooth by that arc length at the pitch circle. Below the base circle the flank
    is a radial line to the root circle (standard for small tooth counts)."""
    d = gear_dims(module, teeth, pressure_angle, clearance, addendum)
    r_p, r_a, r_f, r_b = d["pitch_d"] / 2, d["outside_d"] / 2, d["root_d"] / 2, d["base_d"] / 2
    pa = math.radians(pressure_angle)
    inv = lambda a: math.tan(a) - a                                  # involute function
    # half tooth thickness angle at the pitch circle, then referred to the base circle
    half_pitch_ang = math.pi / (2 * teeth) - (backlash / 2) / r_p
    half_base_ang = half_pitch_ang + inv(pa)
    def half_ang(r: float) -> float:                                 # half tooth angle at radius r (r >= r_b)
        return half_base_ang - inv(math.acos(min(1.0, r_b / r)))
    r_start = max(r_b, r_f)
    radii = [r_start + (r_a - r_start) * k / (flank_points - 1) for k in range(flank_points)]
    pitch = 2 * math.pi / teeth
    pol = lambda r, a: (r * math.cos(a), r * math.sin(a))
    pts: list[tuple[float, float]] = []
    for i in range(teeth):
        phi = i * pitch
        if r_f < r_b:                                                # radial flank from root up to the base circle
            pts.append(pol(r_f, phi - half_base_ang))
        for r in radii:                                              # left flank (involute), root -> tip
            pts.append(pol(r, phi - half_ang(r)))
        ha = half_ang(r_a)                                           # tip arc
        for k in range(1, arc_points):
            pts.append(pol(r_a, phi - ha + 2 * ha * k / arc_points))
        for r in reversed(radii):                                    # right flank, tip -> root
            pts.append(pol(r, phi + half_ang(r)))
        if r_f < r_b:
            pts.append(pol(r_f, phi + half_base_ang))
        a0 = phi + (half_base_ang if r_f < r_b else half_ang(r_f))   # root arc to the next tooth
        a1 = phi + pitch - (half_base_ang if r_f < r_b else half_ang(r_f))
        for k in range(1, arc_points):
            pts.append(pol(r_f, a0 + (a1 - a0) * k / arc_points))
    # direct API (not builder objects), so this also works inside a BuildSketch / BuildPart context
    wire = b3d.Wire.make_polygon([b3d.Vector(x, y, 0) for x, y in pts], close=True)
    face = b3d.Face(wire)
    if face.area <= 0:
        raise GearError("gear profile has no area")
    return face


@builder_safe
def spur_gear(module: float, teeth: int, thickness: float, bore: float = 0.0, pressure_angle: float = 20.0,
              clearance: float = 0.25, backlash: float = 0.0, hub_d: float = 0.0, hub_h: float = 0.0,
              keyway: tuple[float, float] | None = None, **profile_kw: Any) -> b3d.Solid | b3d.Part:
    """Spur gear solid: profile on XY extruded +Z by `thickness`, optional bore, optional hub (Ø hub_d, hub_h tall
    on top) and a rectangular keyway (width, depth) cut into the bore on +X."""
    # direct API throughout (Solid.extrude / make_cylinder / cut), so calling this inside a BuildPart or
    # BuildSketch never adds the gear to the surrounding builder
    face = involute_gear_profile(module, teeth, pressure_angle, clearance, backlash, **profile_kw)
    body = b3d.Solid.extrude(face, b3d.Vector(0, 0, thickness))
    if hub_d and hub_h:
        if hub_d >= gear_dims(module, teeth, pressure_angle, clearance)["root_d"]:
            raise GearError("hub_d must be smaller than the root diameter")
        body = body.fuse(b3d.Solid.make_cylinder(hub_d / 2, hub_h, b3d.Plane.XY.offset(thickness)))
    total_h = thickness + (hub_h if hub_d and hub_h else 0)
    if bore:
        if bore >= gear_dims(module, teeth, pressure_angle, clearance)["root_d"] - 2 * module:
            raise GearError("bore too large for this gear (leave at least a module of rim under the roots)")
        body = body.cut(b3d.Solid.make_cylinder(bore / 2, total_h + 2, b3d.Plane.XY.offset(-1)))
        if keyway:
            w, dep = keyway
            body = body.cut(b3d.Solid.make_box(dep + 0.02, w, total_h + 2, b3d.Plane.XY.offset(-1).move(b3d.Location((bore / 2 - 0.01, -w / 2, 0)))))
    body = body.clean()
    return body.solids()[0] if len(body.solids()) == 1 else body


def planetary_layout(module: float, sun_teeth: int, planet_teeth: int, planets: int = 3) -> dict[str, Any]:
    """Layout for a simple planetary stage with the sun at the origin (sun tooth 0 on +X, as spur_gear builds it).
    Returns ring_teeth, ratio (carrier output with the ring fixed), centre_distance, planets as dicts
    {x, y, angle, rotation} (rotate each planet gear by `rotation` degrees about its own axis before moving it to
    x, y) and ring_rotation: rotate the ring's cutter profile, involute_gear_profile(m, ring_teeth, addendum=1.25,
    clearance=0), by this many degrees before subtracting it from the housing sketch. Verified interference-free
    for even and odd tooth counts."""
    zs, zp, n = sun_teeth, planet_teeth, planets
    zr = zs + 2 * zp
    if (zs + zr) % n:
        raise GearError(f"{n} equally spaced planets need (sun + ring teeth) divisible by {n}: {zs} + {zr} = {zs + zr}")
    a = gear_centre_distance(module, zs, zp)
    tip = gear_dims(module, zp)["outside_d"]
    if 2 * a * math.sin(math.pi / n) <= tip:
        raise GearError(f"{n} planets of {zp} teeth collide with each other at this centre distance")
    out = []
    for i in range(n):
        th = 360.0 / n * i
        out.append({"x": a * math.cos(math.radians(th)), "y": a * math.sin(math.radians(th)), "angle": th,
                    "rotation": (th * (1 + zs / zp) + 180 - 180 / zp) % 360})
    return {"ring_teeth": zr, "ratio": 1 + zr / zs, "centre_distance": a, "planets": out,
            "ring_rotation": 180 / zr if zp % 2 == 0 else 0.0}


def namespace() -> dict[str, Any]:
    return {"spur_gear": spur_gear, "involute_gear_profile": involute_gear_profile,
            "gear_centre_distance": gear_centre_distance, "gear_dims": gear_dims,
            "planetary_layout": planetary_layout}
