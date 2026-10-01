"""Mechanisms: hinges, springs, knobs, pull handles, cams and link bars."""
from __future__ import annotations

import math

from build123d import (Align, Box, Circle, Cylinder, Face, FilletPolyline, Part, Plane, Pos, Rot, SlotCenterToCenter, Spline,
                       Torus, Vector, Wire, extrude, sweep)

import threads as thr
from designkit.core import component


def _poly(pts):
    return Face(Wire.make_polygon([Vector(x, y, 0) for x, y in pts], close=True))


def _sweep_wire(pts: list[Vector], wire_d: float, pieces: int) -> Part:
    """Sweep a round wire along a smooth spline through `pts`. The spline is split into `pieces` edges (same curve):
    one long swept B-spline face makes the mesher crawl or hang, a chain of shorter faces meshes in seconds."""
    full = Spline(*pts)
    pieces = max(1, pieces)
    path = Wire([full.trim(i / pieces, (i + 1) / pieces) for i in range(pieces)])
    out = sweep(Plane(origin=path @ 0, z_dir=path % 0) * Circle(wire_d / 2), path=path, is_frenet=True)
    if len(out.solids()) != 1 or not out.is_valid:
        raise ValueError("spring sweep failed; try a slightly different wire or diameter")
    return out


@component("mechanisms", "Butt hinge: two leaves with alternating knuckles, a pin and countersunk screw holes; opens to any angle",
           tags=["hinge", "butt hinge", "knuckle", "pin", "lid", "door", "print in place"],
           example='h = kit.hinge(40, 20, knuckles=5, angle=90)   # {"LeafA", "LeafB", "Pin"}, hinge axis along X',
           related=["mechanisms/hinges-and-print-in-place", "countersunk_screw"])
def hinge(length: float = 40.0, leaf_width: float = 20.0, thickness: float = 2.0, knuckles: int = 5, pin_d: float = 2.0,
          clearance: float = 0.2, angle: float = 180.0, screw: str | None = "M3", holes_per_leaf: int = 2) -> dict[str, Part]:
    """Hinge with its axis along X through the origin, from x = -length/2 to +length/2. LeafA lies flat on the -Y side
    (its mid-plane at z = 0); LeafB starts on +Y and is turned about the axis to `angle` (180 = flat open, 0 = closed).
    Knuckle outer diameter = 2 × thickness + pin_d; knuckles alternate between the leaves with `clearance` gaps, the
    pin runs the full length. clearance also opens the knuckle bores (0.2 for printed, 0.05 for machined)."""
    ko = pin_d / 2 + thickness
    seg = (length - (knuckles - 1) * clearance) / knuckles

    def leaf(side: int, own: list[int]) -> Part:
        body = Pos(0, side * ko * 0.6, 0) * Box(length, leaf_width, thickness,
                                                align=(Align.CENTER, Align.MIN if side > 0 else Align.MAX, Align.CENTER))
        for i in own:
            x0 = -length / 2 + i * (seg + clearance)
            body += Pos(x0, 0, 0) * Rot(0, 90, 0) * Cylinder(ko, seg, align=(Align.CENTER, Align.CENTER, Align.MIN))
        # clear the other leaf's knuckles
        for i in range(knuckles):
            if i in own:
                continue
            x0 = -length / 2 + i * (seg + clearance) - clearance
            body -= Pos(x0, 0, 0) * Rot(0, 90, 0) * Cylinder(ko + clearance, seg + 2 * clearance, align=(Align.CENTER, Align.CENTER, Align.MIN))
        body -= Rot(0, 90, 0) * Cylinder(pin_d / 2 + clearance / 2, length + 2)
        if screw and holes_per_leaf:
            d = thr.clearance_dia(screw)
            yc = side * (ko * 0.6 + leaf_width / 2)
            for k in range(holes_per_leaf):
                x = -length / 2 + length * (k + 0.5) / holes_per_leaf
                body = thr.hole(body, d, at=(x, yc, thickness / 2), through=True, countersink=2 * d)
        return body

    a = leaf(-1, [i for i in range(knuckles) if i % 2 == 0])
    b = leaf(1, [i for i in range(knuckles) if i % 2 == 1])
    b = Rot(180 - angle, 0, 0) * b
    pin = Rot(0, 90, 0) * Cylinder(pin_d / 2, length)
    return {"LeafA": a, "LeafB": b, "Pin": pin}


@component("mechanisms", "Helical compression spring swept along its real helix, with closed (and optionally ground) ends",
           tags=["spring", "compression spring", "coil spring", "helix"],
           example='s = kit.compression_spring(1.0, 10, 30, coils=8)   # on the Z axis from z = 0 to 30',
           related=["mechanisms/springs", "extension_spring"])
def compression_spring(wire_d: float = 1.0, outer_d: float = 10.0, free_length: float = 30.0, coils: float = 8.0,
                       closed_ends: bool = True, ground: bool = True) -> Part:
    """Spring on the Z axis from z = 0 to free_length. `coils` is the total number of turns; with closed ends the
    first and last turn are pitched at one wire diameter (touching), and ground ends are flattened at z = 0 and z =
    free_length. Active coils = coils - 2 when closed. Rate: k = G d⁴ / (8 D³ n_active) (see the springs guide)."""
    r = (outer_d - wire_d) / 2
    ends = 1.0 if closed_ends else 0.0
    active = coils - 2 * ends
    if active <= 0:
        raise ValueError("need more than 2 coils with closed ends")
    base = 0.0 if ground else wire_d / 2
    span = free_length - (0.0 if ground else wire_d)
    pa = (span - 2 * ends * wire_d) / active
    if pa <= wire_d:
        raise ValueError("free length too short: the active coils would touch (solid height)")
    n = max(24, int(coils * 12))
    pts = []
    for i in range(n + 1):
        th = 2 * math.pi * coils * i / n
        t = th / (2 * math.pi)
        if t <= ends:
            z = t * wire_d
        elif t >= coils - ends:
            z = ends * wire_d + active * pa + (t - (coils - ends)) * wire_d
        else:
            z = ends * wire_d + (t - ends) * pa
        pts.append(Vector(r * math.cos(th), r * math.sin(th), base + z))
    s = _sweep_wire(pts, wire_d, int(math.ceil(coils)))
    return s & Box(2 * outer_d, 2 * outer_d, free_length, align=(Align.CENTER, Align.CENTER, Align.MIN))


@component("mechanisms", "Helical extension spring with close-wound body and a full loop at each end",
           tags=["spring", "extension spring", "tension spring", "helix", "hook"],
           example='s = kit.extension_spring(1.0, 8, body_length=20)   # loops above and below the body on Z',
           related=["compression_spring", "mechanisms/springs"])
def extension_spring(wire_d: float = 1.0, outer_d: float = 8.0, body_length: float = 20.0) -> Part:
    """Close-wound body on the Z axis from z = 0 to body_length, with a machine loop at each end (a 270° loop of the
    spring's diameter in a plane through the axis, joined by a short straight lead). The whole wire is one sweep
    along a single path, so the spring is one clean solid."""
    r = (outer_d - wire_d) / 2
    pitch = 1.04 * wire_d                          # a hair open: exactly touching coils defeat the kernel
    coils = (body_length - wire_d) / pitch
    lead = max(1.5 * wire_d, 0.3 * r)
    z0, z1 = wire_d / 2, wire_d / 2 + pitch * coils
    th_end = 2 * math.pi * coils
    pts: list[Vector] = []
    cz = z0 - lead - r                              # bottom loop centre (in the XZ plane, below the body)
    for deg in range(105, 361, 30):                 # from near the top of the loop, round the far side, to its right edge
        f = math.radians(deg)
        pts.append(Vector(r * math.cos(f), 0, cz + r * math.sin(f)))
    for k in (1, 2):
        pts.append(Vector(r, 0, cz + (z0 - cz) * k / 3))
    n = max(24, int(coils * 12))
    for i in range(n + 1):
        th = th_end * i / n
        pts.append(Vector(r * math.cos(th), r * math.sin(th), z0 + pitch * coils * i / n))
    ce, se = math.cos(th_end), math.sin(th_end)
    rot = lambda x, z: Vector(x * ce, x * se, z)   # noqa: E731  (the XZ plane turned to the last coil's angle)
    cz2 = z1 + lead + r
    for k in (1, 2):
        pts.append(rot(r, z1 + (cz2 - z1) * k / 3))
    for deg in range(0, 256, 30):
        f = math.radians(deg)
        pts.append(rot(r * math.cos(f), cz2 + r * math.sin(f)))
    return _sweep_wire(pts, wire_d, int(coils) + 4)


@component("mechanisms", "Fluted control knob with a D-shaft bore, a set screw and a pointer line",
           tags=["knob", "control knob", "potentiometer", "encoder", "handle", "dial"],
           example='k = Pos(0, 0, 8) * kit.knob(25, 16, shaft_d=6)', related=["set_screw", "pull_handle"])
def knob(diameter: float = 25.0, height: float = 16.0, flutes: int = 18, shaft_d: float = 6.0, d_flat: bool = True,
         pointer: bool = True) -> dict[str, Part]:
    """Knob on the Z axis from z = 0 to `height`: fluted grip, rounded top edge, a blind D bore (flat on +X) for a
    potentiometer/encoder shaft, and an M3 set screw from the side over the flat. Returns {"Knob", "SetScrew"}."""
    from designkit.components.fasteners import set_screw
    body = Cylinder(diameter / 2, height, align=(Align.CENTER, Align.CENTER, Align.MIN))
    top = [e for e in body.edges() if e.geom_type.name == "CIRCLE" and abs(e.center().Z - height) < 1e-6]
    body = body.fillet(min(0.12 * diameter, 0.3 * height), top)          # round the top first, then cut the flutes
    fr = 0.06 * diameter
    for k in range(flutes):
        a = 2 * math.pi * k / flutes
        body -= Pos((diameter / 2 + 0.35 * fr) * math.cos(a), (diameter / 2 + 0.35 * fr) * math.sin(a), 1.5) * Cylinder(fr, height, align=(Align.CENTER, Align.CENTER, Align.MIN))
    bore_depth = min(height - 3, 12.0)
    bore = Cylinder(shaft_d / 2 + 0.05, bore_depth, align=(Align.CENTER, Align.CENTER, Align.MIN))
    if d_flat:
        bore -= Pos(shaft_d / 2 + 5 - 0.5 * shaft_d * 0.25, 0, 0) * Box(10, 10, 3 * bore_depth)
    body -= Pos(0, 0, -0.01) * bore
    if pointer:
        body -= Pos(0.3 * diameter, 0, height) * Box(0.35 * diameter, 1.0, 1.6)
    zs = min(bore_depth / 2, height / 3)
    body = thr.tap(body, "M3", at=(diameter / 2, 0, zs), depth=diameter / 2 - shaft_d / 2 + 0.5, axis=(-1, 0, 0))
    ss_len = max(3, round(diameter / 2 - shaft_d / 2 - 1))
    screw = Pos(diameter / 2 - 1, 0, zs) * Rot(0, 90, 0) * set_screw("M3", ss_len)
    return {"Knob": body, "SetScrew": screw}


@component("mechanisms", "Bar pull handle (cabinet/drawer or equipment handle) with tapped feet",
           tags=["handle", "pull handle", "bar handle", "drawer", "grip"],
           example='h = Pos(0, 0, 10) * kit.pull_handle(96)   # feet on z = 0 at x = ±48', related=["knob"])
def pull_handle(centres: float = 96.0, height: float = 30.0, bar_d: float = 10.0, screw: str = "M4") -> Part:
    """Round-bar handle: feet on z = 0 at x = ±centres/2, rising to a bar at `height` (to the bar's centre line) with
    radiused bends, each foot tapped for `screw` from underneath."""
    r_bend = min(1.5 * bar_d, height / 2)
    path = FilletPolyline((-centres / 2, 0, 0), (-centres / 2, 0, height), (centres / 2, 0, height), (centres / 2, 0, 0), radius=r_bend)
    start = path @ 0
    bar = sweep(Plane(origin=start, z_dir=path % 0) * Circle(bar_d / 2), path=path)
    for sx in (-1, 1):
        bar = thr.tap(bar, screw, at=(sx * centres / 2, 0, 0), depth=min(1.5 * thr.thread(screw)["major"] * 2, height - bar_d), axis=(0, 0, 1))
    return bar


@component("mechanisms", "Disc cam with harmonic rise-dwell-return lift, hub bore and keyway-ready hub",
           tags=["cam", "disc cam", "follower", "lift", "mechanism"],
           example='c = kit.disc_cam(20, 8, rise=120, dwell=60, ret=120)   # on the Z axis, from z = 0',
           related=["mechanisms/linkages-and-cams"])
def disc_cam(base_radius: float = 20.0, lift: float = 8.0, rise: float = 120.0, dwell: float = 60.0, ret: float = 120.0,
             thickness: float = 8.0, bore: float = 8.0, follower_radius: float = 0.0) -> Part:
    """Cam profile r(θ) = base + lift × (1 - cos(π·t))/2 during rise (simple-harmonic), held for `dwell`, harmonic
    return, then base circle for the rest. Angles in degrees from +X, counter-clockwise. With a roller follower,
    give its radius: the pitch curve is offset inward by it (the profile is what the roller rides on)."""
    if rise + dwell + ret > 360:
        raise ValueError("rise + dwell + return must be ≤ 360°")
    pts = []
    n = 360
    for i in range(n):
        th = 360.0 * i / n
        if th < rise:
            s = lift * (1 - math.cos(math.pi * th / rise)) / 2
        elif th < rise + dwell:
            s = lift
        elif th < rise + dwell + ret:
            s = lift * (1 + math.cos(math.pi * (th - rise - dwell) / ret)) / 2
        else:
            s = 0.0
        r = base_radius + s - follower_radius
        pts.append((r * math.cos(math.radians(th)), r * math.sin(math.radians(th))))
    cam = extrude(_poly(pts), thickness)
    return cam - Pos(0, 0, -1) * Cylinder(bore / 2, thickness + 2, align=(Align.CENTER, Align.CENTER, Align.MIN))


@component("mechanisms", "Link bar for linkages: rounded ends with pivot holes at a given centre distance",
           tags=["link", "linkage", "four bar", "lever", "arm", "pivot"],
           example='l = kit.link_bar(60, width=10, thickness=4, hole_d=4.2)   # holes at x = 0 and x = 60',
           related=["mechanisms/linkages-and-cams", "dowel_pin", "hinge"])
def link_bar(centre_distance: float = 60.0, width: float = 10.0, thickness: float = 4.0, hole_d: float = 4.2) -> Part:
    """Flat link lying on z = 0 (up to `thickness`), pivot holes at (0, 0) and (centre_distance, 0)."""
    body = Pos(centre_distance / 2, 0, 0) * extrude(SlotCenterToCenter(centre_distance, width), thickness)
    for x in (0.0, centre_distance):
        body -= Pos(x, 0, -1) * Cylinder(hole_d / 2, thickness + 2, align=(Align.CENTER, Align.CENTER, Align.MIN))
    return body
