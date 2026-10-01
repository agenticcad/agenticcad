"""Electronics packaging: development boards (mounting holes exact, main connectors as envelopes), fans, cells.

Boards lie on XY with the PCB's bottom face at z = 0 and its lower-left corner at the origin (the datasheet origin),
so a mounting hole's position in the design is just its datasheet coordinate. Connectors are keep-out envelopes
of the real parts: good for enclosure cut-outs and clearances, not for the connectors' internals."""
from __future__ import annotations

import math

from build123d import Align, Box, BuildSketch, Cylinder, Locations, Part, Pos, Rectangle, Rot, fillet, extrude

from designkit.core import component


def _block(x0: float, y0: float, z0: float, dx: float, dy: float, dz: float) -> Part:
    return Pos(x0, y0, z0) * Box(dx, dy, dz, align=(Align.MIN, Align.MIN, Align.MIN))


def _header(x0: float, y0: float, z0: float, n: int, rows: int = 1, male: bool = False, along_x: bool = True) -> Part:
    """0.1" header: a plastic block with square pins (male) or sockets (female)."""
    p = 2.54
    L, W, H = n * p, rows * p, (2.5 if male else 8.5)
    blk = _block(x0, y0, z0, L if along_x else W, W if along_x else L, H)
    for i in range(n):
        for j in range(rows):
            cx = x0 + (i + 0.5) * p if along_x else x0 + (j + 0.5) * p
            cy = y0 + (j + 0.5) * p if along_x else y0 + (i + 0.5) * p
            if male:
                blk += Pos(cx, cy, z0) * Box(0.64, 0.64, 11.0, align=(Align.CENTER, Align.CENTER, Align.MIN))
            else:
                blk -= Pos(cx, cy, z0 + H) * Box(1.0, 1.0, 6.0, align=(Align.CENTER, Align.CENTER, Align.CENTER))
    return blk


def _pcb(w: float, h: float, t: float, holes: list[tuple[float, float]], hole_d: float, corner_r: float = 0.0) -> Part:
    with BuildSketch() as s:
        with Locations((w / 2, h / 2)):          # not Pos(...) * Rectangle(...): inside a builder that adds at the origin
            Rectangle(w, h)
        if corner_r:
            fillet(s.vertices(), corner_r)
    pcb = extrude(s.sketch, t)
    for x, y in holes:
        pcb -= Pos(x, y, 0) * Cylinder(hole_d / 2, 3 * t)
    return pcb


@component("electronics", "Arduino Uno R3: 68.6 × 53.3 PCB with the four mounting holes, headers, USB-B and barrel jack",
           tags=["arduino", "uno", "board", "microcontroller", "pcb"],
           example='u = kit.place(kit.arduino_uno(), Pos(5, 5, 6))   # on 6 mm standoffs; holes at their datasheet coordinates',
           related=["electronics/pcb-mounting-and-venting", "standoff"])
def arduino_uno() -> dict[str, Part]:
    """Arduino Uno R3. Mounting holes Ø3.2 at (13.97, 2.54), (15.24, 50.8), (66.04, 7.62), (66.04, 35.56) from the
    lower-left corner. The USB-B and barrel jack stand 6.2 and 1.8 mm past the left edge (x < 0): leave openings."""
    t = 1.6
    holes = [(13.97, 2.54), (15.24, 50.8), (66.04, 7.62), (66.04, 35.56)]
    parts: dict[str, Part] = {"PCB": _pcb(68.58, 53.34, t, holes, 3.2)}
    parts["USB_B"] = _block(-6.2, 32.1, t, 16.0, 12.0, 10.9)
    parts["BarrelJack"] = _block(-1.8, 3.3, t, 14.5, 9.0, 11.0)
    parts["HeaderDigitalHigh"] = _header(20.07, 49.53, t, 10)
    parts["HeaderDigitalLow"] = _header(46.99, 49.53, t, 8)
    parts["HeaderPower"] = _header(24.13, 1.27, t, 8)
    parts["HeaderAnalog"] = _header(46.99, 1.27, t, 6)
    parts["MCU"] = _block(30.0, 12.5, t, 35.6, 7.6, 4.5)
    parts["ICSP"] = _header(63.5, 23.5, t, 3, rows=2, male=True, along_x=False)
    return parts


@component("electronics", "Raspberry Pi 4 Model B: 85 × 56 PCB, M2.5 holes on 58 × 49, GPIO header, USB/Ethernet stack, USB-C, micro-HDMI",
           tags=["raspberry pi", "pi 4", "board", "sbc", "pcb"],
           example='pi = kit.place(kit.raspberry_pi_4(), Pos(0, 0, 5))', related=["electronics/pcb-mounting-and-venting", "standoff", "fan"])
def raspberry_pi_4() -> dict[str, Part]:
    """Raspberry Pi 4B. Holes Ø2.7 at (3.5, 3.5), (61.5, 3.5), (3.5, 52.5), (61.5, 52.5); the Ethernet and USB stack
    overhangs the right edge by ~2 mm; USB-C, two micro-HDMI and audio face the bottom edge (y = 0)."""
    t = 1.4
    holes = [(3.5, 3.5), (61.5, 3.5), (3.5, 52.5), (61.5, 52.5)]
    parts: dict[str, Part] = {"PCB": _pcb(85.0, 56.0, t, holes, 2.7, corner_r=3.0)}
    parts["GPIO"] = _header(7.1, 50.0, t, 20, rows=2, male=True)
    parts["Ethernet"] = _block(66.0, 37.75, t, 21.3, 16.0, 13.5)
    parts["USB2"] = _block(70.0, 20.45, t, 17.3, 13.1, 16.0)
    parts["USB3"] = _block(70.0, 2.45, t, 17.3, 13.1, 16.0)
    parts["USB_C"] = _block(11.2 - 4.5, -1.2, t, 9.0, 7.5, 3.2)
    parts["MicroHDMI0"] = _block(26.0 - 3.5, -1.2, t, 7.0, 7.5, 3.0)
    parts["MicroHDMI1"] = _block(39.5 - 3.5, -1.2, t, 7.0, 7.5, 3.0)
    parts["Audio"] = Pos(53.5, 5.5, t + 3) * Rot(90, 0, 0) * Cylinder(3, 14.0) + _block(50.0, 0.0, t, 7.0, 12.0, 6.0)
    parts["SoC"] = _block(21.5, 25.0, t, 15.0, 15.0, 2.4)
    return parts


@component("electronics", "Raspberry Pi Pico: 51 × 21 PCB, four Ø2.1 holes, castellated pads, micro-USB, RP2040",
           tags=["raspberry pi", "pico", "rp2040", "board", "microcontroller", "pcb"],
           example='p = kit.place(kit.pi_pico(), Pos(10, 10, 4))', related=["electronics/pcb-mounting-and-venting"])
def pi_pico() -> dict[str, Part]:
    """Raspberry Pi Pico, lower-left corner at the origin. Holes Ø2.1, 4.8 mm from the short edges, 11.4 mm apart;
    20 castellated pads on each long edge (2.54 mm pitch); micro-USB overhangs the +X end by 1.3 mm."""
    t = 1.0
    holes = [(x, y) for x in (4.8, 51 - 4.8) for y in (10.5 - 5.7, 10.5 + 5.7)]
    pcb = _pcb(51.0, 21.0, t, holes, 2.1)
    for i in range(20):
        x = 51 / 2 + (i - 9.5) * 2.54
        for y in (0.0, 21.0):
            pcb -= Pos(x, y, 0) * Cylinder(0.5, 3 * t)
    return {"PCB": pcb, "MicroUSB": _block(51 - 5.4 + 1.3, 10.5 - 3.9, t, 5.4, 7.8, 2.8), "RP2040": _block(25.5 - 3.5 - 3, 10.5 - 3.5, t, 7.0, 7.0, 0.9),
            "BootSel": _block(12.0, 12.5, t, 4.5, 3.5, 2.0)}


# size: (hole spacing, hole dia, default thickness)
FANS = {25: (20.0, 2.8, 10), 30: (24.0, 3.2, 10), 40: (32.0, 3.4, 10), 50: (40.0, 3.4, 15), 60: (50.0, 3.4, 15),
        70: (60.0, 4.3, 15), 80: (71.5, 4.3, 25), 92: (82.5, 4.3, 25), 120: (105.0, 4.3, 25)}


@component("electronics", "Axial cooling fan 25-120 mm: square frame with mounting holes, hub on struts, 7 pitched blades",
           tags=["fan", "cooling", "axial fan", "40mm", "80mm", "120mm", "ventilation"],
           example='f = kit.place(kit.fan(40), Pos(0, 0, 20))   # centred on the origin, bottom at z = 0',
           related=["electronics/pcb-mounting-and-venting", "fan_grille_cutter"])
def fan(size: int = 40, thickness: float | None = None) -> dict[str, Part]:
    """Fan centred on the Z axis, bottom at z = 0, airflow along Z. Hole spacing and diameter per the common
    standard for that size (e.g. 40 mm: 32 mm on Ø3.4; 80 mm: 71.5 mm on Ø4.3). Returns {Frame, Rotor}."""
    if size not in FANS:
        raise ValueError(f"fan size must be one of {sorted(FANS)}")
    hs, hd, t0 = FANS[size]
    t = thickness or t0
    r_throat = 0.48 * size
    with BuildSketch() as s:
        Rectangle(size, size)
        fillet(s.vertices(), 0.08 * size)
    frame = extrude(s.sketch, t) - Cylinder(r_throat, 3 * t)
    for sx in (-1, 1):
        for sy in (-1, 1):
            frame -= Pos(sx * hs / 2, sy * hs / 2, 0) * Cylinder(hd / 2, 3 * t)
    r_hub = 0.21 * size
    motor_mount = Cylinder(r_hub, 0.12 * t, align=(Align.CENTER, Align.CENTER, Align.MIN))
    for k in range(4):                                             # struts from the motor mount to the frame (back face)
        motor_mount += Rot(0, 0, 45 + 90 * k) * Pos((r_hub + r_throat) / 2 + 0.5, 0, 0) * Box(r_throat - r_hub + 1.5, 0.05 * size + 0.8, 0.12 * t, align=(Align.CENTER, Align.CENTER, Align.MIN))
    frame += motor_mount
    gap = 0.03 * t + 0.3
    hub = Pos(0, 0, 0.12 * t + gap) * Cylinder(r_hub - 0.2, t - 0.12 * t - 2 * gap, align=(Align.CENTER, Align.CENTER, Align.MIN))
    blade_h = t - 0.12 * t - 2 * gap
    span = r_throat - 0.6 - (r_hub - 0.6)
    rotor = hub
    for k in range(7):
        blade = Box(span, 0.035 * size + 0.5, 0.9, align=(Align.MIN, Align.CENTER, Align.CENTER))
        blade = Pos(r_hub - 0.6, 0, 0.12 * t + gap + blade_h / 2) * Rot(35, 0, 0) * blade
        rotor += Rot(0, 0, 360 / 7 * k) * blade
    rotor = rotor & (Pos(0, 0, 0.12 * t + gap) * Cylinder(r_throat - 0.6, blade_h, align=(Align.CENTER, Align.CENTER, Align.MIN)))
    return {"Frame": frame, "Rotor": rotor}


@component("electronics", "Grille cutter for a fan: the swept circle minus concentric rings and spokes, to cut into a panel",
           tags=["fan", "grille", "vent", "cutter", "enclosure"],
           example='panel = panel - Pos(0, 0, -1) * kit.fan_grille_cutter(40, depth=5)', related=["fan", "vent_slots_cutter"])
def fan_grille_cutter(size: int = 40, depth: float = 5.0, bars: float = 1.6) -> Part:
    """Openings for a fan grille (concentric rings with 4 spokes), from z = 0 up `depth`, centred on the Z axis,
    plus the mounting holes. Subtract it from a panel."""
    hs, hd, _ = FANS[size]
    r = 0.47 * size
    cut = Cylinder(r, depth, align=(Align.CENTER, Align.CENTER, Align.MIN))
    pitch = max(3.0, 0.1 * size)
    rr = pitch
    while rr < r - bars:
        cut -= Cylinder(rr + bars / 2, depth, align=(Align.CENTER, Align.CENTER, Align.MIN)) - Cylinder(rr - bars / 2, depth, align=(Align.CENTER, Align.CENTER, Align.MIN))
        rr += pitch
    for k in range(4):
        cut -= Rot(0, 0, 45 + 90 * k) * Pos(r / 2, 0, depth / 2) * Box(r + 1, bars, depth)
    for sx in (-1, 1):
        for sy in (-1, 1):
            cut += Pos(sx * hs / 2, sy * hs / 2, 0) * Cylinder(hd / 2, depth, align=(Align.CENTER, Align.CENTER, Align.MIN))
    return cut


@component("electronics", "18650 Li-ion cell (Ø18.4 × 65.2) with positive button and insulator ring",
           tags=["battery", "18650", "li-ion", "cell"],
           example='c = kit.place(kit.cell_18650(), Pos(0, 0, 0) * Rot(0, 90, 0))   # lying along X')
def cell_18650(protected: bool = False) -> dict[str, Part]:
    """Cell on the Z axis, negative end at z = 0, positive button up. protected=True is 3 mm longer (PCB in the can)."""
    L = 65.2 + (3.0 if protected else 0.0)
    can = Cylinder(9.2, L - 0.9, align=(Align.CENTER, Align.CENTER, Align.MIN))
    can = can.fillet(0.6, [e for e in can.edges() if e.geom_type.name == "CIRCLE"])
    button = Pos(0, 0, L - 0.9) * Cylinder(3.0, 0.9, align=(Align.CENTER, Align.CENTER, Align.MIN))
    ring = Pos(0, 0, L - 1.2) * (Cylinder(8.6, 0.3, align=(Align.CENTER, Align.CENTER, Align.MIN)) - Cylinder(3.4, 1.0))
    return {"Can": can - Pos(0, 0, L - 1.2) * Cylinder(8.7, 1.0, align=(Align.CENTER, Align.CENTER, Align.MIN)), "Button": button, "Insulator": ring}
