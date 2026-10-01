"""Motors: NEMA hybrid stepper motors, outside-only or with full internals."""
from __future__ import annotations

import math

from build123d import (Align, BuildSketch, Box, Circle, Cylinder, Face, Mode, Part, Plane, Pos, Rectangle, RectangleRounded, Rot,
                       Vector, Wire, chamfer, extrude)

import threads as thr
from designkit.core import component

# frame: (square, hole spacing, hole size, through holes, pilot dia, pilot height, default shaft dia, cap corner chamfer)
NEMA = {8: (20.3, 16.0, "M2", False, 15.0, 1.5, 4.0, 1.5), 11: (28.2, 23.0, "M2.5", False, 22.0, 2.0, 5.0, 2.0),
        14: (35.2, 26.0, "M3", False, 22.0, 2.0, 5.0, 2.5), 17: (42.3, 31.0, "M3", False, 22.0, 2.0, 5.0, 3.0),
        23: (56.4, 47.14, "M5", True, 38.1, 1.6, 8.0, 4.0)}
BEARING_FOR_SHAFT = {4.0: "624", 5.0: "625", 8.0: "608"}


def _poly(pts: list[tuple[float, float]]) -> Face:
    return Face(Wire.make_polygon([Vector(x, y, 0) for x, y in pts], close=True))


def _sector(r0: float, r1: float, a0: float, a1: float, n: int = 16) -> Face:
    arc = lambda r, a, b: [(r * math.cos(math.radians(a + (b - a) * k / n)), r * math.sin(math.radians(a + (b - a) * k / n))) for k in range(n + 1)]  # noqa: E731
    return _poly(arc(r1, a0, a1) + arc(r0, a1, a0))


def _square(sq: float, ch: float, z0: float, t: float) -> Part:
    with BuildSketch() as s:
        Rectangle(sq, sq)
        chamfer(s.vertices(), ch)
    return Pos(0, 0, z0) * extrude(s.sketch, t)


@component("motors", "NEMA 8/11/14/17/23 hybrid stepper motor: outside only, or full internals (stator poles, coils, 50-tooth rotor, bearings)",
           tags=["stepper", "nema", "nema17", "motor", "hybrid stepper", "actuator"],
           standard="NEMA ICS 16 frame sizes",
           example='m = kit.nema_stepper(17, 40)                    # dict of parts, front face at z = 0, shaft up +Z\n'
                   'result = {"Motor": m}',
           related=["ball_bearing", "planetary_stage"])
def nema_stepper(frame: int = 17, length: float = 40, shaft_length: float = 24, shaft_d: float | None = None,
                 detail: str = "external", d_flat: bool = True, connector: bool = True, real: bool = False) -> dict[str, Part]:
    """Hybrid stepper motor on the Z axis: front (mounting) face at z = 0, pilot boss and shaft up +Z, body down to
    z = -length. Returns a dict of parts: FrontCap, Stator, RearCap, Shaft, Screw1-4 (rear assembly screws), a
    Connector {Housing, Pin1-6} (JST-PH, frames 14-23) and with detail="full" also Coil1-8, RotorCupA/B (50 teeth,
    offset half a tooth), RotorMagnet, FrontBearing and RearBearing (real ball bearings).
    The mounting holes are tapped (M2/M2.5/M3; real=True models the thread); NEMA 23 has Ø5.1 through holes.
    Proportions of the internals follow a typical 1.8° motor; outside dimensions follow the NEMA frame."""
    if frame not in NEMA:
        raise ValueError(f"NEMA frame must be one of {sorted(NEMA)}")
    sq, hs, hsize, through, pilot_d, pilot_h, shaft_def, ch = NEMA[frame]
    shaft_d = shaft_d or shaft_def
    k = sq / 42.3
    front_t, rear_t = round(0.19 * sq, 1), round(0.236 * sq, 1)
    st_len = length - front_t - rear_t
    if st_len < 8:
        raise ValueError(f"length {length} is too short for a NEMA {frame} (minimum ~{front_t + rear_t + 8:.0f} mm)")
    full = detail == "full"
    if detail not in ("full", "external"):
        raise ValueError("detail must be external | full")
    corners = [(sx * hs / 2, sy * hs / 2) for sx in (-1, 1) for sy in (-1, 1)]
    clear_r = 2.55 if through else thr.clearance_dia(hsize) / 2
    z_st0, z_st1 = -front_t - st_len, -front_t
    bearing = BEARING_FOR_SHAFT.get(float(shaft_d))
    if full and bearing is None:
        raise ValueError(f"detail='full' needs a {', '.join(f'{d:g}' for d in BEARING_FOR_SHAFT)} mm shaft")
    parts: dict[str, object] = {}

    # ---- front cap (flange): pilot boss, mounting holes, bearing seat, end-winding pocket
    front = _square(sq, ch, -front_t, front_t) + Cylinder(pilot_d / 2, pilot_h, align=(Align.CENTER, Align.CENTER, Align.MIN))
    front -= Cylinder(shaft_d / 2 + 0.5, 4 * length)
    for x, y in corners:
        if through:
            front -= Pos(x, y, 0) * Cylinder(2.55, 4 * length)
        else:
            front = thr.tap(front, hsize, at=(x, y, 0), depth=min(4.5 * k, front_t - 2), real=real)
            if frame in (14, 17):
                front = thr.tap(front, hsize, at=(x, y, -front_t), depth=min(3.0, front_t - 5), real=real, axis=(0, 0, 1))
    b_od = {"624": 13, "625": 16, "608": 22}.get(bearing or "", 0)
    b_w = {"624": 5, "625": 5, "608": 7}.get(bearing or "", 0)
    if full:
        front -= Pos(0, 0, -front_t) * (Cylinder(17 * k + 1, 3, align=(Align.CENTER, Align.CENTER, Align.MIN)) - Cylinder(b_od / 2 + 2, 6))
        front -= Pos(0, 0, -front_t) * Cylinder(b_od / 2, b_w, align=(Align.CENTER, Align.CENTER, Align.MIN))
    parts["FrontCap"] = front

    # ---- stator: laminated square with chamfered corners; full = 8 salient poles with 6 teeth each
    if full:
        with BuildSketch() as st:
            Rectangle(sq, sq)
            chamfer(st.vertices(), 5 * k)
            Circle(17 * k, mode=Mode.SUBTRACT)
        face = st.sketch
        for p in range(8):
            a = 45 * p
            pole = Rot(0, 0, a) * (Pos((12.0 * k + 17.2 * k) / 2, 0) * Rectangle(5.2 * k, 4.5 * k)) + _sector(11.1 * k, 12.3 * k, a - 19.8, a + 19.8)
            for j in range(5):
                c = a + 7.2 * (j - 2)
                pole = pole - _sector(11.1 * k - 0.1, 11.1 * k + 0.4, c - 1.8, c + 1.8, 4)
            face = face + pole
        stator = Pos(0, 0, z_st0) * extrude(face, st_len)
    else:
        stator = _square(sq, 5 * k, z_st0, st_len)
    for x, y in corners:
        stator -= Pos(x, y, 0) * Cylinder(clear_r, 4 * length)
    parts["Stator"] = stator

    # ---- rear cap + connector pocket
    rear = _square(sq, ch, -length, rear_t)
    for x, y in corners:
        rear -= Pos(x, y, 0) * Cylinder(clear_r, 4 * length)
    if full:
        rear -= Pos(0, 0, z_st0 - 3) * (Cylinder(17 * k + 1, 3, align=(Align.CENTER, Align.CENTER, Align.MIN)) - Cylinder(b_od / 2 + 2, 6))
        rear -= Pos(0, 0, z_st0 - b_w) * Cylinder(b_od / 2, b_w, align=(Align.CENTER, Align.CENTER, Align.MIN))
    con_ok = connector and frame >= 14
    if con_ok:
        cz = -length + min(rear_t / 2, 3.2)
        rear -= Pos(sq / 2 - 2.9, 0, cz) * Box(5.81, 14.1, 4.81)
        housing = Pos(sq / 2 - 2.9, 0, cz) * Box(5.8, 14.0, 4.8) - Pos(sq / 2 - 2.25 + 0.01, 0, cz) * Box(4.5, 12.4, 3.2)
        con = {"Housing": housing}
        for i in range(6):
            con[f"Pin{i + 1}"] = Pos(sq / 2 - 4.5 + 1.75, -5 + 2 * i, cz) * Box(3.5, 0.64, 0.64)
        parts["Connector"] = con
    parts["RearCap"] = rear

    # ---- shaft with D-flat
    z_shaft0 = (z_st0 - b_w) if full else -front_t
    shaft = Pos(0, 0, z_shaft0) * Cylinder(shaft_d / 2, shaft_length - z_shaft0, align=(Align.CENTER, Align.CENTER, Align.MIN))
    if d_flat:
        flat_len = min(15.0, shaft_length - pilot_h - 1)
        shaft -= Pos(shaft_d / 2 - 0.5 + 5, 0, shaft_length - flat_len) * Box(10, 10, flat_len + 1, align=(Align.CENTER, Align.CENTER, Align.MIN))
    parts["Shaft"] = shaft

    # ---- rear assembly screws into the front cap (frames 14/17)
    if frame in (14, 17):
        from designkit.components.fasteners import socket_screw
        L = length - front_t + min(3.0, front_t - 5) - 0.5
        L = math.floor(L)
        for i, (x, y) in enumerate(corners, 1):
            parts[f"Screw{i}"] = Pos(x, y, -length) * Rot(180, 0, 0) * socket_screw(hsize, L, real=real)

    if full:
        from designkit.components.bearings import ball_bearing
        from designkit.core import place
        for p in range(8):
            with BuildSketch(Plane.YZ) as cs:
                RectangleRounded(8.9 * k, st_len + 4.4 * k, 2.1 * k)
                Rectangle(4.5 * k, st_len, mode=Mode.SUBTRACT)
            coil = Pos(12.6 * k, 0, (z_st0 + z_st1) / 2) * extrude(cs.sketch, 3.6 * k)
            parts[f"Coil{p + 1}"] = Rot(0, 0, 45 * p) * coil
        cup_len = (st_len - 2 - 2 * k) / 2
        z0 = z_st0 + 1

        def cup(z: float, phase: float) -> Part:
            pts = []
            for i in range(50):
                a0 = 7.2 * i + phase
                for a, r in ((a0 - 1.8, 11.0 * k), (a0 + 1.8, 11.0 * k), (a0 + 1.8, 10.6 * k), (a0 + 5.4, 10.6 * k)):
                    pts.append((r * math.cos(math.radians(a)), r * math.sin(math.radians(a))))
            return Pos(0, 0, z) * (extrude(_poly(pts), cup_len) - Cylinder(shaft_d / 2, 3 * length))
        parts["RotorCupA"] = cup(z0, 0.0)
        parts["RotorMagnet"] = Pos(0, 0, z0 + cup_len) * (Cylinder(10 * k, 2 * k, align=(Align.CENTER, Align.CENTER, Align.MIN)) - Cylinder(shaft_d / 2, 3 * length))
        parts["RotorCupB"] = cup(z0 + cup_len + 2 * k, 3.6)
        parts["FrontBearing"] = place(ball_bearing(bearing), Pos(0, 0, -front_t))
        parts["RearBearing"] = place(ball_bearing(bearing), Pos(0, 0, z_st0 - b_w))
    return parts


# ---------------------------------------------------------------- hobby servos
# name: (body L, W, H, tab length, tab thickness, tab bottom z, hole spacing along L, hole dia, holes per tab across W,
#        output offset from body centre along L, top cap dia, top cap h, spline dia, spline h)
SERVOS = {
    "SG90": (22.8, 12.2, 22.7, 32.3, 2.5, 15.9, 27.8, 2.0, 1, 5.3, 11.8, 4.0, 4.8, 3.2),
    "MG90S": (22.8, 12.2, 22.7, 32.3, 2.5, 15.9, 27.8, 2.0, 1, 5.3, 11.8, 4.0, 4.8, 3.2),
    "MG996R": (40.7, 19.7, 37.2, 54.5, 2.5, 26.6, 49.5, 4.5, 2, 10.2, 13.5, 1.5, 5.9, 4.2),
    "DS3218": (40.0, 20.0, 40.5, 54.0, 2.5, 28.0, 49.5, 4.5, 2, 10.0, 14.0, 1.5, 5.9, 4.5),
}


@component("motors", "Hobby RC servo (SG90, MG90S, MG996R, DS3218): case, mounting tabs with holes, gear cap and splined output",
           tags=["servo", "rc servo", "sg90", "mg996r", "actuator", "robot"],
           example='s = kit.servo("MG996R")   # dict: Case, Output; output axis +Z at the origin, case below',
           related=["motors/mounting-motors-and-servos"])
def servo(model: str = "SG90") -> dict[str, Part]:
    """Servo with its output spline on the Z axis: the spline's top face is at z = 0, the gear cap and case are below it,
    and the case extends towards -X from the output (which sits off-centre, as on the real part). Returns {"Case": case + tabs + gear cap, "Output": splined output shaft}. Hole positions and
    body sizes follow typical datasheets for that model; brands vary by a few tenths, so check critical fits."""
    if model.upper() not in SERVOS:
        raise ValueError(f"servo model must be one of {', '.join(SERVOS)}")
    L, W, H, tabL, tab_t, tab_z, hs, hd, nh, off, cap_d, cap_h, sp_d, sp_h = SERVOS[model.upper()]
    z_top = -sp_h - cap_h - H                             # case bottom
    case = Pos(-off, 0, z_top) * Box(L, W, H, align=(Align.CENTER, Align.CENTER, Align.MIN))
    case = case.fillet(0.6, [e for e in case.edges() if abs(e.center().Z - (z_top + H / 2)) < H / 2 - 0.01 and e.length > H - 0.1])
    tab = Pos(-off, 0, z_top + tab_z) * Box(tabL, W, tab_t, align=(Align.CENTER, Align.CENTER, Align.MIN))
    across = [0.0] if nh == 1 else [-W / 4, W / 4]
    for sx in (-1, 1):
        for y in across:
            tab -= Pos(-off + sx * hs / 2, y, z_top + tab_z) * Cylinder(hd / 2, 3 * tab_t)
        if nh == 1:                                        # the SG90's tabs are slotted to the edge
            tab -= Pos(-off + sx * (hs / 2 + (tabL - hs) / 4 + 0.5), 0, z_top + tab_z + tab_t / 2) * Box((tabL - hs) / 2, 1.0, 3 * tab_t)
    cap = Pos(0, 0, -sp_h - cap_h) * Cylinder(cap_d / 2, cap_h, align=(Align.CENTER, Align.CENTER, Align.MIN))
    body = case + tab + cap
    teeth = 21 if sp_d < 5.5 else 25
    pts = []
    for i in range(teeth):
        a = 2 * math.pi * i / teeth
        for da, r in ((-0.35, sp_d / 2), (0.35, sp_d / 2), (0.5, sp_d / 2 - 0.25), (1.0 - 0.5, sp_d / 2 - 0.25)):
            ang = a + da * 2 * math.pi / teeth
            pts.append((r * math.cos(ang), r * math.sin(ang)))
    spline = Pos(0, 0, -sp_h) * extrude(_poly(pts), sp_h)
    spline -= Pos(0, 0, -sp_h) * Cylinder(0.45 * sp_d / 2 + 0.4, sp_h + 1, align=(Align.CENTER, Align.CENTER, Align.MIN))   # horn screw hole
    return {"Case": body, "Output": spline}


@component("motors", "28BYJ-48 geared 5 V stepper: Ø28 can, mounting ears, offset flatted output shaft, wiring cover",
           tags=["stepper", "28byj-48", "geared stepper", "motor", "uln2003"],
           example='m = kit.stepper_28byj48()   # front face at z = 0, can centre at the origin', related=["nema_stepper"])
def stepper_28byj48() -> dict[str, Part]:
    """28BYJ-48 on the Z axis: mounting face at z = 0, can (Ø28 × 19, with its ears: Ø4.2 holes 35 mm apart along X)
    below, output shaft (Ø5 with 3 mm flats, 9 mm long) 8 mm off the can centre on +Y, blue wiring cover on -Y.
    Returns {Can, WiringCover, Shaft}."""
    can = Pos(0, 0, -19) * Cylinder(14, 19, align=(Align.CENTER, Align.CENTER, Align.MIN))
    ears = extrude(_poly([(-21, -3.5), (-17.5, -3.5), (-12, -7), (12, -7), (17.5, -3.5), (21, -3.5), (21, 3.5), (17.5, 3.5),
                          (12, 7), (-12, 7), (-17.5, 3.5), (-21, 3.5)]), -0.8)
    for sx in (-1, 1):
        ears -= Pos(sx * 17.5, 0, 0) * Cylinder(2.1, 5)
    boss = Pos(0, 8, 0) * Cylinder(4.5, 1.5, align=(Align.CENTER, Align.CENTER, Align.MIN))
    cover = Pos(0, -14 - 1.5, -17.5) * Box(16.5, 5, 16, align=(Align.CENTER, Align.CENTER, Align.MIN))
    shaft = Pos(0, 8, 1.5) * Cylinder(2.5, 9, align=(Align.CENTER, Align.CENTER, Align.MIN))
    for sx in (-1, 1):
        shaft -= Pos(sx * (1.5 + 2), 8, 1.5 + 3) * Box(4, 6, 7, align=(Align.CENTER, Align.CENTER, Align.MIN))
    body = can + ears + boss                                   # the ears are the can's pressed front plate
    return {"Can": body, "WiringCover": cover - can, "Shaft": shaft}


@component("motors", "N20 micro metal gear motor: gearbox, flatted motor can, end cap with terminals, D-shaft",
           tags=["n20", "gear motor", "dc motor", "micro motor", "robot"],
           example='m = kit.n20_gearmotor(motor_length=15)   # output face at z = 0, shaft up +Z', related=["servo"])
def n20_gearmotor(motor_length: float = 15.0, shaft_length: float = 10.0) -> dict[str, Part]:
    """N20 gear motor on the Z axis: gearbox output face at z = 0 with the Ø3 D-shaft (2.5 across the flat) up +Z,
    gearbox 12 × 10 × 9 below, the flatted Ø12 motor can (10 across the flats) below that, and the end cap with
    two terminals. Two M1.6 holes in the gearbox face, 9 mm apart."""
    gb = Pos(0, 0, -9) * Box(12, 10, 9, align=(Align.CENTER, Align.CENTER, Align.MIN))
    for sx in (-1, 1):
        gb -= Pos(sx * 4.5, 0, -2.5) * Cylinder(0.65, 3, align=(Align.CENTER, Align.CENTER, Align.MIN))
    boss = Cylinder(2, 0.6, align=(Align.CENTER, Align.CENTER, Align.MIN))
    can = Pos(0, 0, -9 - motor_length) * (Cylinder(6, motor_length, align=(Align.CENTER, Align.CENTER, Align.MIN))
                                          & Box(12, 10, 4 * motor_length))
    cap = Pos(0, 0, -9 - motor_length - 2) * (Cylinder(5.5, 2, align=(Align.CENTER, Align.CENTER, Align.MIN)) & Box(11, 9.5, 10))
    terms = {f"Terminal{i + 1}": Pos(sx * 3.5, 0, -9 - motor_length - 2 - 2.5) * Box(1.6, 0.3, 2.5, align=(Align.CENTER, Align.CENTER, Align.MIN))
             for i, sx in enumerate((-1, 1))}
    shaft = Pos(0, 0, 0.6) * Cylinder(1.5, shaft_length, align=(Align.CENTER, Align.CENTER, Align.MIN))
    shaft -= Pos(1.0 + 2, 0, 1.6) * Box(4, 4, shaft_length, align=(Align.CENTER, Align.CENTER, Align.MIN))
    return {"Gearbox": gb + boss, "Motor": can, "EndCap": cap, "Shaft": shaft, **terms}
