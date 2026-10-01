"""Enclosures and housings: a screw-lid box for 3D printing, screw bosses, vent slots and snap-fit hooks."""
from __future__ import annotations

import math

from build123d import Align, Box, BuildSketch, Face, Part, Pos, Rectangle, Rot, Vector, Wire, extrude, fillet

import threads as thr
from designkit.core import component
from designkit.components.fasteners import INSERTS, button_head_screw, heat_set_insert, insert_hole_cutter


def _rrect(l: float, w: float, r: float) -> Face:
    with BuildSketch() as s:
        Rectangle(l, w)
        if r > 0:
            fillet(s.vertices(), r)
    return s.sketch.faces()[0]


@component("enclosures", "3D-printable box with a screw-on lid: rounded walls, corner bosses with heat-set inserts, locating lip, screws",
           tags=["enclosure", "box", "case", "housing", "lid", "3d printing", "project box"],
           example='e = kit.enclosure(90, 60, 30)   # inner size; {"Base", "Lid", "Insert1-4", "Screw1-4"}, centred, floor at z = 0',
           related=["enclosures/enclosure-design", "screw_boss", "vent_slots_cutter", "heat_set_insert"])
def enclosure(length: float = 80.0, width: float = 60.0, height: float = 30.0, wall: float = 2.0, floor: float = 2.0,
              lid: float = 2.0, corner_r: float = 4.0, screw: str = "M3", fit: float = 0.2) -> dict[str, Part]:
    """Box with inner size length × width × height, centred on the Z axis, outside bottom at z = 0. Four corner bosses
    take heat-set inserts; the lid (at the top) has a locating lip with `fit` clearance and button-head screws into
    the inserts. Cut openings into Base/Lid afterwards (e.g. vent_slots_cutter, board cut-outs)."""
    od, ins_len, hole = INSERTS[screw]
    boss_r = hole / 2 + 1.6
    L, W = length + 2 * wall, width + 2 * wall
    top = floor + height
    base = extrude(_rrect(L, W, corner_r), top)
    base -= Pos(0, 0, floor) * extrude(_rrect(length, width, max(corner_r - wall, 0.5)), height + 1)
    pts = [(sx * (length / 2 - boss_r + 0.5), sy * (width / 2 - boss_r + 0.5)) for sx in (-1, 1) for sy in (-1, 1)]
    for x, y in pts:
        boss = Pos(x, y, floor) * extrude(_rrect(2 * boss_r, 2 * boss_r, boss_r - 0.01), height)
        corner = Pos(x + math.copysign(boss_r, x) / 2, y + math.copysign(boss_r, y) / 2, floor) * Box(boss_r, boss_r, height, align=(Align.CENTER, Align.CENTER, Align.MIN))
        base += (boss + corner) & Pos(0, 0, floor) * extrude(_rrect(length + 0.02, width + 0.02, max(corner_r - wall, 0.5)), height)
        base -= Pos(x, y, top) * insert_hole_cutter(screw)
    lid_plate = Pos(0, 0, top) * extrude(_rrect(L, W, corner_r), lid)
    lip_h = 2.0
    lip_outer = _rrect(length - 2 * fit, width - 2 * fit, max(corner_r - wall - fit, 0.5))
    lip_inner = _rrect(length - 2 * fit - 2.4, width - 2 * fit - 2.4, max(corner_r - wall - fit - 1.2, 0.3))
    lip = Pos(0, 0, top - lip_h) * extrude(lip_outer, lip_h) - Pos(0, 0, top - lip_h - 1) * extrude(lip_inner, lip_h + 2)
    for x, y in pts:
        lip -= Pos(x, y, top - lip_h - 1) * extrude(_rrect(2 * boss_r + 2 * fit + 1.0, 2 * boss_r + 2 * fit + 1.0, boss_r), lip_h + 2)
    lid_part = lid_plate + lip
    d = thr.clearance_dia(screw)
    parts: dict[str, Part] = {}
    screw_len = max(6, round(lid + ins_len * 0.8))
    for i, (x, y) in enumerate(pts, 1):
        lid_part -= Pos(x, y, top - 1) * _cyl(d / 2, lid + 2)
        parts[f"Insert{i}"] = Pos(x, y, top) * heat_set_insert(screw)
        parts[f"Screw{i}"] = Pos(x, y, top + lid) * button_head_screw(screw, screw_len)
    return {"Base": base, "Lid": lid_part, **parts}


def _cyl(r: float, h: float) -> Part:
    from build123d import Cylinder
    return Cylinder(r, h, align=(Align.CENTER, Align.CENTER, Align.MIN))


@component("enclosures", "Screw boss for plastic parts: a post with an insert or pilot hole and optional gussets",
           tags=["boss", "screw boss", "insert", "post", "3d printing", "moulding"],
           example='part = part + Pos(10, 10, 2) * kit.screw_boss("M3", 12)', related=["heat_set_insert", "enclosure", "enclosures/enclosure-design"])
def screw_boss(screw: str = "M3", height: float = 10.0, insert: bool = True, gussets: int = 4) -> Part:
    """Boss standing on z = 0 up to `height`: outer diameter about 2.2 × the insert hole (or 2.5 × the pilot for
    self-tapping), hole from the top, and triangular gussets (0, 3 or 4) at its foot. Fuse it onto a wall or floor."""
    od_ins, L, hole = INSERTS[screw]
    pilot = 0.85 * thr.thread(screw)["major"]
    hd = hole if insert else pilot
    r = 1.1 * hd
    boss = _cyl(r, height)
    g_h, g_l, g_t = 0.6 * height, 1.2 * r, max(1.2, 0.35 * r)
    for k in range(gussets):
        tri = Face(Wire.make_polygon([Vector(r - 0.2, 0, 0), Vector(r + g_l, 0, 0), Vector(r - 0.2, 0, g_h)], close=True))
        g = Pos(0, g_t / 2, 0) * _extrude_y(tri, g_t)
        boss += Rot(0, 0, 360 / gussets * k + 45) * g
    if insert:
        boss -= Pos(0, 0, height) * insert_hole_cutter(screw, extra_depth=1.0)
    else:
        boss -= Pos(0, 0, height - 1.5 * thr.thread(screw)["major"] * 2) * _cyl(pilot / 2, 3 * thr.thread(screw)["major"] + 1)
    return boss


def _extrude_y(face_xz: Face, t: float) -> Part:
    """Extrude a face lying in the XZ plane by t along -Y."""
    from build123d import extrude as ex
    return ex(face_xz, t, dir=(0, -1, 0))


@component("enclosures", "Cutter for a row of rounded vent slots (subtract from a wall or lid)", tags=["vent", "slots", "ventilation", "grille", "cutter"],
           example='lid = lid - Pos(0, 0, 30) * kit.vent_slots_cutter(8, 30, 2.5, 5, depth=5)', related=["fan_grille_cutter", "enclosure"])
def vent_slots_cutter(count: int = 6, length: float = 30.0, width: float = 2.5, pitch: float = 5.0, depth: float = 5.0) -> Part:
    """`count` slots along X, stacked along Y at `pitch`, centred on the origin, from z = -depth/2 to +depth/2.
    Keep bars (pitch - width) at least 1.5 mm for printing."""
    from build123d import SlotCenterToCenter
    out = None
    for i in range(count):
        y = (i - (count - 1) / 2) * pitch
        s = Pos(0, y, -depth / 2) * extrude(SlotCenterToCenter(length - width, width), depth)
        out = s if out is None else out + s
    return out


@component("enclosures", "Cantilever snap-fit hook for printed or moulded parts", tags=["snap fit", "clip", "latch", "cantilever", "3d printing"],
           example='hook = Pos(0, 20, 2) * kit.snap_fit_hook(10, 1.6, 6, 0.8)', related=["enclosures/enclosure-design"])
def snap_fit_hook(length: float = 10.0, thickness: float = 1.6, width: float = 6.0, overhang: float = 0.8,
                  entry_angle: float = 30.0) -> Part:
    """Beam standing on z = 0 (root) up +Z for `length`, width along Y, bending about Y; the hook at the tip overhangs
    +X by `overhang` with a sloped entry face and a square retaining face. Keep strain under ~2 % (PLA/PETG):
    overhang ≲ 0.67 × 0.02 × length² / thickness."""
    beam = Box(thickness, width, length, align=(Align.MIN, Align.CENTER, Align.MIN))
    hook_h = overhang / math.tan(math.radians(entry_angle)) + 0.8
    prof = Face(Wire.make_polygon([Vector(thickness, 0, length - hook_h), Vector(thickness + overhang, 0, length - hook_h),
                                   Vector(thickness + overhang, 0, length - hook_h + 0.8), Vector(thickness, 0, length)], close=True))
    hook = Pos(0, width / 2, 0) * _extrude_y(prof, width)
    return beam + hook
