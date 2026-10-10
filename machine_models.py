"""Machine models: a kinematic, collision-aware 3D model of each supported machine, built in build123d from measured and
published dimensions. Used by the viewer (the Machine toggle poses it from the simulation) and by the simulator (the
holder / nose profile and the table, chuck and tailstock solids are what the tool must not hit).

Machine frame: X right, Y back (away from the operator), Z up; the ORIGIN is the centre of the table / bed top surface.
Stock sits on the table top (plus an optional fixture height) centred unless a placement says otherwise.

Kinematics: a tree of nodes. Each node may move along one axis in "head" mode (the node carries the spindle and moves
with the tool) or "table" mode (the node carries the work, so it moves the OPPOSITE way to keep the tool over the cut).
The node flagged `stock` is where the stock, the part, the toolpaths and the simulation mesh ride.

Accuracy tiers (recorded per number in `sources`):
  verified   : from the maker's published specification or a measured/official CAD reference
  reference  : from the Carvera Community simplified machine models (github.com/Carvera-Community/Carvera_Community_Profiles)
  estimated  : representative; edit it in Settings ▸ Machines ▸ Model when you have measured your machine
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from build123d import (Align, Axis, Box, Cone, Cylinder, Locations, Mode, Plane, Pos, Rectangle, Shape, Solid, Sphere,
                       extrude, BuildPart, BuildSketch, chamfer, fillet)

ROOT = Path(__file__).parent
IN = 25.4


@dataclass
class Part:
    name: str
    node: str
    shape: Shape
    material: str = "cast"        # a key of MATERIALS below (the viewer's palette)
    collision: bool = False       # exact geometry the tool/holder must not hit (table, chuck, tailstock, walls...)


@dataclass
class Node:
    name: str
    parent: str | None = None
    axis: str | None = None       # x | y | z | a
    mode: str = "head"            # head (moves with the tool) | table (moves the work, opposite sign)
    stock: bool = False           # the work rides here
    pivot: tuple[float, float, float] | None = None   # rotary nodes: a point on the axis (machine frame)
    direction: tuple[float, float, float] = (1.0, 0.0, 0.0)


@dataclass
class MachineModel:
    key: str                      # z1 | z1_4axis | air | air_4axis | haas | router
    name: str
    nodes: list[Node]
    parts: list[Part]
    home: tuple[float, float, float]           # spindle axis X, Y and nose-bottom Z at machine zero (frame above)
    travel: tuple[float, float, float]
    clearance: float                           # nose bottom above the table at Z top (gantry clearance)
    nose: list[dict[str, Any]]                 # holder/nose profile from the nose bottom upwards: [{name, r, h, kind}]
    table: dict[str, Any]                      # {x, y, t (thickness), ...} table surface extents, centred on the origin
    rotary: dict[str, Any] | None = None       # {axis_y, axis_z, chuck_face_x, chuck_r, tail_x, ...} if a 4th axis is modelled
    sources: dict[str, str] = field(default_factory=dict)
    notes: str = ""
    spoilboard: float = 0.0                    # thickness of sacrificial board on the table (cuts into it are allowed)
    stickout: float | None = None              # user-measured tool stickout below the nose (overrides flute length + 6)

    def to_payload(self) -> dict[str, Any]:
        return {"key": self.key, "name": self.name, "home": list(self.home), "travel": list(self.travel), "clearance": self.clearance,
                "nose": self.nose, "table": self.table, "rotary": self.rotary, "sources": self.sources, "notes": self.notes,
                "spoilboard": self.spoilboard, "stickout": self.stickout,
                "nodes": [{"name": n.name, "parent": n.parent, "axis": n.axis, "mode": n.mode, "stock": n.stock,
                           "pivot": list(n.pivot) if n.pivot else None, "direction": list(n.direction)} for n in self.nodes]}


# viewer materials: a small painted-metal / plastic / glass palette (PBR values), keyed by Part.material
MATERIALS = {
    "cast": {"color": "#5b6066", "metalness": 0.55, "roughness": 0.78},
    "paint_light": {"color": "#d9dbde", "metalness": 0.1, "roughness": 0.5},
    "paint_white": {"color": "#eceef0", "metalness": 0.05, "roughness": 0.45},
    "paint_dark": {"color": "#2f3338", "metalness": 0.2, "roughness": 0.55},
    "paint_blue": {"color": "#3b5a8f", "metalness": 0.15, "roughness": 0.5},
    "ground_steel": {"color": "#a3a8ae", "metalness": 0.95, "roughness": 0.32},
    "steel": {"color": "#8f949a", "metalness": 0.9, "roughness": 0.4},
    "stainless": {"color": "#c0c4c8", "metalness": 0.95, "roughness": 0.28},
    "alu": {"color": "#b9bcc0", "metalness": 0.85, "roughness": 0.42},
    "anodised": {"color": "#2a2c30", "metalness": 0.7, "roughness": 0.45},
    "mdf": {"color": "#c9a66b", "metalness": 0.0, "roughness": 0.9},
    "acrylic": {"color": "#9fd3ff", "metalness": 0.0, "roughness": 0.08, "opacity": 0.22},
    "panel": {"color": "#e6e8eb", "metalness": 0.1, "roughness": 0.5, "opacity": 0.42},      # enclosure sheet metal, ghosted so the work stays visible
    "rubber": {"color": "#1e1f22", "metalness": 0.0, "roughness": 0.95},
    "carbide": {"color": "#b4b8bd", "metalness": 0.9, "roughness": 0.35},
}


def _box(x: float, y: float, z: float, at=(0.0, 0.0, 0.0), zmin: float | None = None) -> Shape:
    """A box centred in X/Y at `at`, standing on z = zmin (or centred on at[2] when zmin is None)."""
    if zmin is None:
        return Pos(*at) * Box(x, y, z)
    return Pos(at[0], at[1], zmin) * Box(x, y, z, align=(Align.CENTER, Align.CENTER, Align.MIN))


def _cyl_z(r: float, h: float, at=(0.0, 0.0), zmin: float = 0.0) -> Shape:
    return Pos(at[0], at[1], zmin) * Cylinder(r, h, align=(Align.CENTER, Align.CENTER, Align.MIN))


def _cyl_x(r: float, length: float, y: float, z: float, xmin: float) -> Shape:
    return Plane(origin=(xmin, y, z), z_dir=(1, 0, 0)) * Cylinder(r, length, align=(Align.CENTER, Align.CENTER, Align.MIN))


def _cyl_y(r: float, length: float, x: float, z: float, ymin: float) -> Shape:
    return Plane(origin=(x, ymin, z), z_dir=(0, 1, 0)) * Cylinder(r, length, align=(Align.CENTER, Align.CENTER, Align.MIN))


def _shell(outer: tuple[float, float, float], inner: tuple[float, float, float], at=(0.0, 0.0, 0.0), zmin: float = 0.0,
           inner_zmin: float | None = None, opening: tuple[str, float, float] | None = None) -> Shape:
    """A box with a cavity (the enclosure). opening=("front", width, height) cuts a doorway in the -Y wall."""
    s = _box(*outer, at=at, zmin=zmin)
    cav = _box(*inner, at=at, zmin=inner_zmin if inner_zmin is not None else zmin + (outer[2] - inner[2]) / 2)
    s = s - cav
    if opening:
        side, w, h = opening
        y = at[1] - outer[1] / 2
        s = s - Pos(at[0], y, zmin + (outer[2] - inner[2]) / 2) * Box(w, 60, h, align=(Align.CENTER, Align.CENTER, Align.MIN))
    return s


# =============================================================================== Makera Z1 (+ 4th axis)
def makera_z1(fourth: bool = False) -> MachineModel:
    """Makera Z1 desktop CNC. Numbers: Makera spec page (work area 200×200×100, gantry clearance 115, machine
    350×470×450, 4th axis Ø80×150) and the Carvera Community simplified Z1 model + Makera's official Z1-MDF bed file
    (bed 206×206×6 with 36 counterbored M5 holes, spindle nose Ø16×20 below a 65×52 head plate, head 65×70×190,
    chuck Ø52/Ø48.7 with four jaws, axis 45 mm above the bed, tailstock Ø14 centre). The spindle nose/collet are
    `reference`: measure yours and edit Settings ▸ Machines ▸ Model."""
    parts: list[Part] = []
    nodes = [Node("base"), Node("bed", "base", axis="y", mode="table", stock=True), Node("bridge", "base"),
             Node("head", "bridge", axis="x"), Node("spindle", "head", axis="z")]
    # ---- bed: aluminium plate + MDF spoilboard with the real hole grid
    mdf_t, alu_t = 6.0, 12.0
    bed_alu = _box(206, 206, alu_t, zmin=-mdf_t - alu_t)
    mdf = _box(206, 206, mdf_t, zmin=-mdf_t)
    holes = _bed_holes("makera_z1_mdf")
    if holes:
        for (u, v) in holes:
            x, y = u - 103, v - 103
            mdf = mdf - _cyl_z(2.75, mdf_t + 1, at=(x, y), zmin=-mdf_t - 0.5) - _cyl_z(5.0, 3.0, at=(x, y), zmin=-3.0)
    parts += [Part("bed plate", "bed", bed_alu, "alu", collision=True), Part("spoilboard", "bed", mdf, "mdf", collision=True),
              Part("bed carriage", "bed", _box(180, 180, 26, zmin=-mdf_t - alu_t - 26), "paint_dark")]
    # Y rails and the base frame (fixed)
    for x in (-70, 70):
        parts.append(Part(f"Y rod {'L' if x < 0 else 'R'}", "base", _cyl_y(6, 420, x, -mdf_t - alu_t - 26 - 8, -130), "ground_steel"))
    parts.append(Part("base frame", "base", _box(300, 400, 20, at=(0, 100, 0), zmin=-98), "paint_dark"))
    # ---- rear bridge: two X rods and the beam the head rides on
    bridge_y = 142.0
    for z in (175, 300):
        parts.append(Part(f"X rod {'low' if z < 200 else 'high'}", "bridge", _cyl_x(10, 330, bridge_y, z, -165), "ground_steel"))
    parts.append(Part("bridge beam", "bridge", _box(330, 40, 60, at=(0, bridge_y, 0), zmin=305), "paint_white"))
    # ---- head: X carriage column + Z carriage + spindle (home: spindle axis x=-108, y=+101, nose bottom at z=116)
    hx, hy, nose_z = -108.0, 101.0, 116.0
    parts.append(Part("X carriage", "head", _box(83, 48, 196, at=(hx, bridge_y, 0), zmin=136), "paint_white"))
    head = _box(65, 70, 190, at=(hx, hy + 8, 0), zmin=nose_z + 20)                      # S05: 65×70 body above the nose plate
    head = head + Pos(hx, hy, nose_z + 20 + 190) * Cone(28.7, 22, 40, align=(Align.CENTER, Align.CENTER, Align.MIN))   # motor taper
    parts.append(Part("Z carriage", "spindle", head, "paint_white", collision=True))
    parts.append(Part("spindle nose", "spindle", _cyl_z(8, 20, at=(hx, hy), zmin=nose_z), "stainless", collision=True))
    # ---- enclosure: outer shell 355×435×449 around the bed, cavity 320×395×368, lid as acrylic (reference model extents)
    shell = _shell((355, 435, 449), (320, 395, 368), at=(0, 105, 0), zmin=-99, inner_zmin=-69, opening=("front", 310, 330))
    parts.append(Part("enclosure", "base", shell, "panel", collision=True))
    parts.append(Part("front door", "base", _box(310, 3, 330, at=(0, 105 - 435 / 2 + 1.5, 0), zmin=-69), "acrylic"))
    rotary = None
    if fourth:
        nodes.append(Node("chuck", "bed", axis="a", mode="table", stock=True, pivot=(0.0, -10.0, 45.0)))
        nodes[1].stock = False
        ay, az = -10.0, 45.0
        rail = _box(262, 52, 8.5, at=(3, ay, 0), zmin=0)
        parts.append(Part("4th axis rail", "bed", rail, "anodised", collision=True))
        parts.append(Part("4th axis housing", "bed", _box(35, 52, 57, at=(-110, ay, 0), zmin=8.5), "anodised", collision=True))
        chuck = (_cyl_x(26, 6, ay, az, -84) + _cyl_x(24.35, 11, ay, az, -78) + _cyl_x(15, 20, ay, az, -67))
        jaws = None
        for k in range(4):
            a = math.radians(90 * k)
            jaw = Pos(-60 + 15.5, ay + 13.2 * math.cos(a), az + 13.2 * math.sin(a)) * Box(31, 10.4 if k % 2 == 0 else 25.5, 25.5 if k % 2 == 0 else 10.4)
            jaws = jaw if jaws is None else jaws + jaw
        parts.append(Part("chuck", "chuck", chuck, "steel", collision=True))
        parts.append(Part("chuck jaws", "chuck", jaws, "anodised", collision=True))
        tail = (_box(34, 52, 57, at=(111, ay, 0), zmin=8.5) + _cyl_x(7.5, 4, ay, az, 93) + _cyl_x(7, 5, ay, az, 88)
                + Plane(origin=(88, ay, az), z_dir=(-1, 0, 0)) * Cone(7, 0, 8, align=(Align.CENTER, Align.CENTER, Align.MIN)))
        parts.append(Part("tailstock", "bed", tail, "anodised", collision=True))
        rotary = {"axis_y": ay, "axis_z": az, "chuck_face_x": -45.0, "chuck_r": 26.0, "tail_x": 80.0, "tail_r": 7.5, "max_diameter": 80, "max_length": 150}
    m = MachineModel("z1_4axis" if fourth else "z1", "Makera Z1" + (" + 4th axis" if fourth else ""), nodes, parts,
                     home=(hx, hy, nose_z), travel=(200, 200, 100), clearance=nose_z,
                     nose=[{"name": "collet / nose", "r": 8.0, "h": 20.0}, {"name": "head", "r": 36.0, "h": 190.0, "box": [65, 70]}],
                     table={"x": 206, "y": 206, "t": mdf_t + alu_t, "holes": holes, "hole_d": 5.5}, rotary=rotary, spoilboard=mdf_t,
                     sources={"work area": "verified: makera.com 200×200×100", "gantry clearance": "verified: makera.com 115 mm",
                              "bed 206×206 and hole grid": "verified: Makera Z1-MDF-v2.1 (official bed file)",
                              "spindle nose Ø16×20": "reference: community simplified model — measure yours",
                              "head 65×70×190": "reference: community simplified model", "enclosure 355×435×449": "reference: community simplified model",
                              "4th axis chuck Ø52, axis 45 above bed, tailstock Ø14": "reference: community simplified model; Ø80×150 verified: makera.com"},
                     notes="Moving bed in Y under a fixed rear bridge; the head moves X along the bridge and Z. Machine zero: head back-left, bed forward.")
    return m


# =============================================================================== Carvera Air (+ 4th axis)
def carvera_air(fourth: bool = False) -> MachineModel:
    """Makera Carvera Air. Numbers: makera.com (work area 300×200×130, gantry clearance 120, footprint 500×450×450,
    4th axis Ø92×200) and the Carvera Community simplified Air model (bed 306×222×15 with 66 Ø6 holes, spindle nose Ø16×21
    under a Ø33.6×8 collar, head block 120×98×200, bridge rods Ø20, chuck parts shared with the Z1)."""
    parts: list[Part] = []
    nodes = [Node("base"), Node("bed", "base", axis="y", mode="table", stock=True), Node("bridge", "base"),
             Node("head", "bridge", axis="x"), Node("spindle", "head", axis="z")]
    mdf_t, alu_t = 15.0, 15.0
    bed_alu = _box(306, 222, alu_t, zmin=-mdf_t - alu_t)
    mdf = _box(306, 222, mdf_t, zmin=-mdf_t)
    holes = _bed_holes("carvera_air_mdf")
    for (u, v) in holes:
        mdf = mdf - _cyl_z(3.0, mdf_t + 1, at=(u - 153, v - 111), zmin=-mdf_t - 0.5)
    parts += [Part("bed plate", "bed", bed_alu, "alu", collision=True), Part("spoilboard", "bed", mdf, "mdf", collision=True),
              Part("bed carriage", "bed", _box(260, 200, 30, zmin=-mdf_t - alu_t - 30), "paint_dark")]
    for x in (-100, 100):
        parts.append(Part(f"Y rod {'L' if x < 0 else 'R'}", "base", _cyl_y(8, 430, x, -mdf_t - alu_t - 30 - 10, -200), "ground_steel"))
    parts.append(Part("base frame", "base", _box(440, 400, 20, at=(0, 60, 0), zmin=-100), "paint_dark"))
    bridge_y = 196.0
    for z in (125, 214):
        parts.append(Part(f"X rod {'low' if z < 200 else 'high'}", "bridge", _cyl_x(10, 490, bridge_y, z, -245), "ground_steel"))
    parts.append(Part("bridge beam", "bridge", _box(490, 46, 50, at=(0, bridge_y, 0), zmin=236), "paint_light"))
    hx, hy, nose_z = -150.0, 100.0, 120.0
    parts.append(Part("X carriage", "head", _box(128, 60, 240, at=(hx, bridge_y - 20, 0), zmin=60), "paint_light"))
    head = _box(120, 98, 200, at=(hx, hy + 9, 0), zmin=nose_z + 21 + 8)
    parts.append(Part("Z carriage", "spindle", head, "paint_light", collision=True))
    parts.append(Part("spindle collar", "spindle", _cyl_z(16.8, 8, at=(hx, hy), zmin=nose_z + 21), "anodised", collision=True))
    parts.append(Part("spindle nose", "spindle", _cyl_z(8, 21, at=(hx, hy), zmin=nose_z), "stainless", collision=True))
    shell = _shell((500, 450, 450), (470, 420, 400), at=(0, 60, 0), zmin=-100, inner_zmin=-70, opening=("front", 450, 330))
    parts.append(Part("enclosure", "base", shell, "panel", collision=True))
    parts.append(Part("front door", "base", _box(450, 3, 330, at=(0, 60 - 450 / 2 + 1.5, 0), zmin=-70), "acrylic"))
    rotary = None
    if fourth:
        nodes.append(Node("chuck", "bed", axis="a", mode="table", stock=True, pivot=(0.0, 0.0, 46.0)))
        nodes[1].stock = False
        ay, az = 0.0, 46.0
        parts.append(Part("4th axis rail", "bed", _box(300, 52, 8.5, at=(0, ay, 0), zmin=0), "anodised", collision=True))
        parts.append(Part("4th axis housing", "bed", _box(100, 76, 100, at=(-110, ay, 0), zmin=8.5), "anodised", collision=True))
        chuck = (_cyl_x(26, 6, ay, az, -60) + _cyl_x(24.35, 11, ay, az, -54) + _cyl_x(15, 20, ay, az, -43))
        jaws = None
        for k in range(4):
            a = math.radians(90 * k)
            jaw = Pos(-36 + 15.5, ay + 13.2 * math.cos(a), az + 13.2 * math.sin(a)) * Box(31, 10.4 if k % 2 == 0 else 25.5, 25.5 if k % 2 == 0 else 10.4)
            jaws = jaw if jaws is None else jaws + jaw
        parts.append(Part("chuck", "chuck", chuck, "steel", collision=True))
        parts.append(Part("chuck jaws", "chuck", jaws, "anodised", collision=True))
        tail = (_box(40, 76, 100, at=(125, ay, 0), zmin=8.5) + _cyl_x(7.5, 4, ay, az, 101) + _cyl_x(7, 5, ay, az, 96)
                + Plane(origin=(96, ay, az), z_dir=(-1, 0, 0)) * Cone(7, 0, 8, align=(Align.CENTER, Align.CENTER, Align.MIN)))
        parts.append(Part("tailstock", "bed", tail, "anodised", collision=True))
        rotary = {"axis_y": ay, "axis_z": az, "chuck_face_x": -21.0, "chuck_r": 26.0, "tail_x": 88.0, "tail_r": 7.5, "max_diameter": 92, "max_length": 200}
    return MachineModel("air_4axis" if fourth else "air", "Carvera Air" + (" + 4th axis" if fourth else ""), nodes, parts,
                        home=(hx, hy, nose_z), travel=(300, 200, 130), clearance=nose_z,
                        nose=[{"name": "collet / nose", "r": 8.0, "h": 21.0}, {"name": "collar", "r": 16.8, "h": 8.0},
                              {"name": "head", "r": 55.0, "h": 200.0, "box": [120, 98]}],
                        table={"x": 306, "y": 222, "t": mdf_t + alu_t, "holes": holes, "hole_d": 6.0}, rotary=rotary, spoilboard=mdf_t,
                        sources={"work area": "verified: makera.com 300×200×130", "gantry clearance": "verified: makera.com 120 mm",
                                 "bed 306×222 and hole grid": "reference: community Carvera_Air_MDF-Bed model",
                                 "spindle nose Ø16×21, collar Ø33.6": "reference: community simplified model — measure yours",
                                 "head 120×98×200, bridge rods Ø20": "reference: community simplified model",
                                 "enclosure 500×450×450": "verified: makera.com footprint; cavity estimated",
                                 "4th axis": "reference: chuck parts from the community model; Ø92×200 verified: makera.com"},
                        notes="Moving bed in Y under a fixed rear bridge; the head moves X along the bridge rods and Z.")


# =============================================================================== Haas vertical machining centres
HAAS = {
    # table L×W (mm), T-slots (count, width, pitch), nose-to-table min/max, travel x/y/z, column/enclosure scale
    "Haas VF-2": {"table": (914.4, 355.6), "slots": (3, 16.0, 125.0), "nose": (101.6, 609.6), "travel": (762, 406, 508)},
    "Haas VF-2SS": {"table": (914.4, 355.6), "slots": (3, 16.0, 125.0), "nose": (101.6, 609.6), "travel": (762, 406, 508)},
    "Haas VF-4": {"table": (1320.8, 457.2), "slots": (5, 16.0, 80.0), "nose": (106.7, 741.7), "travel": (1270, 508, 635)},
    "Haas Mini Mill": {"table": (914.4, 304.8), "slots": (3, 16.0, 110.0), "nose": (101.6, 355.6), "travel": (406, 305, 254)},
    "Haas TM-1": {"table": (1212.9, 266.7), "slots": (3, 16.0, 101.6), "nose": (101.6, 508.0), "travel": (762, 305, 406)},
}


def haas_vmc(name: str = "Haas VF-2") -> MachineModel:
    """Haas VMC: table with T-slots (verified: Haas spec sheets), CAT40 spindle and ER32 holder (verified: ASME/vendor
    dims — flange Ø63.5, ER32 nut Ø50, gauge length 101.6), nose-to-table range (verified); castings and enclosure are
    representative (estimated)."""
    spec = HAAS.get(name, HAAS["Haas VF-2"])
    L, W = spec["table"]; n_slots, sw, pitch = spec["slots"]; nose_min, nose_max = spec["nose"]; tx, ty, tz = spec["travel"]
    tt = 60.0
    parts: list[Part] = []
    nodes = [Node("base"), Node("saddle", "base", axis="y", mode="table"), Node("table", "saddle", axis="x", mode="table", stock=True),
             Node("column", "base"), Node("spindle", "column", axis="z")]
    table = _box(L, W, tt, zmin=-tt)
    for k in range(n_slots):
        y = (k - (n_slots - 1) / 2) * pitch
        table = table - _box(L + 2, sw, 14, at=(0, y, 0), zmin=-14) - _box(L + 2, sw + 10, 10, at=(0, y, 0), zmin=-24)
    parts.append(Part("table", "table", table, "ground_steel", collision=True))
    parts.append(Part("saddle", "saddle", _box(min(L * 0.55, 520), W + 120, 140, at=(0, 0, 0), zmin=-tt - 140), "paint_light"))
    base_w, base_d = L + 500, W + 900
    parts.append(Part("base casting", "base", _box(base_w, base_d, 700, at=(0, 150, 0), zmin=-tt - 140 - 700), "paint_light"))
    col_y = W / 2 + 260
    parts.append(Part("column", "column", _box(min(L * 0.5, 620), 520, nose_max + 900, at=(0, col_y, 0), zmin=-tt - 140), "paint_light"))
    # head: spindle housing at Z top, CAT40 nose Ø90 and the ER32 holder
    nose_z = nose_max
    head = _box(360, 480, 640, at=(0, col_y - 380, 0), zmin=nose_z + 20)
    parts.append(Part("spindle head", "spindle", head, "paint_light", collision=True))
    parts.append(Part("spindle nose", "spindle", _cyl_z(45, 20, zmin=nose_z), "steel", collision=True))
    holder = (_cyl_z(31.75, 20, zmin=nose_z - 20) + Pos(0, 0, nose_z - 20) * Cone(24, 22, 56.6, align=(Align.CENTER, Align.CENTER, Align.MAX))
              + _cyl_z(25, 25, zmin=nose_z - 101.6))
    parts.append(Part("CAT40 ER32 holder", "spindle", holder, "stainless", collision=True))
    # enclosure: full splash guard with a front window (representative)
    enc_w, enc_d, enc_h = base_w + 300, base_d + 200, nose_max + 1400
    enc_zmin = -tt - 140 - 700 - 60
    shell = _shell((enc_w, enc_d, enc_h), (enc_w - 40, enc_d - 40, enc_h - 40), at=(0, 150, 0), zmin=enc_zmin, inner_zmin=enc_zmin + 20)
    door_w, door_h, door_z = enc_w * 0.72, nose_max + 500, -tt - 300                      # sliding doors from below the table to above the head
    shell = shell - _box(door_w, 80, door_h, at=(0, 150 - enc_d / 2, 0), zmin=door_z)
    parts.append(Part("enclosure", "base", shell, "panel", collision=True))
    parts.append(Part("front doors", "base", _box(door_w, 4, door_h, at=(0, 150 - enc_d / 2 + 2, 0), zmin=door_z), "acrylic"))
    parts.append(Part("control pendant", "base", _box(80, 300, 600, at=(enc_w / 2 + 60, -100, 0), zmin=200), "paint_dark"))
    return MachineModel("haas", name, nodes, parts, home=(0.0, 0.0, nose_z), travel=(tx, ty, tz), clearance=nose_max,
                        nose=[{"name": "ER32 nut", "r": 25.0, "h": 25.0}, {"name": "holder body", "r": 24.0, "h": 56.6},
                              {"name": "CAT40 flange", "r": 31.75, "h": 20.0}, {"name": "spindle nose", "r": 45.0, "h": 20.0},
                              {"name": "spindle head", "r": 180.0, "h": 640.0, "box": [360, 480]}],
                        table={"x": L, "y": W, "t": tt, "slots": {"count": n_slots, "width": sw, "pitch": pitch}},
                        sources={"table size and T-slots": "verified: Haas spec sheet (via dealer listings)",
                                 "nose-to-table range": "verified: Haas spec sheet", "travel": "verified: haascnc.com",
                                 "CAT40 flange Ø63.5, ER32 nut Ø50, gauge length 101.6": "verified: ASME B5.50 / Techniks catalogue",
                                 "castings, column, enclosure": "estimated: representative proportions"},
                        notes="Table moves X on the saddle, saddle moves Y, spindle moves Z. Machine zero: spindle at Z top over the table centre "
                              "(G54 is wherever you set it; the stock placement puts the work on the table).")


# =============================================================================== generic gantry router (3018, Shapeoko, LinuxCNC)
def router(name: str, travel: tuple[float, float, float], spindle_d: float = 52.0, nut_d: float = 19.0, rotary: dict | None = None) -> MachineModel:
    """Parametric gantry router sized from the machine's travel: fixed bed with the gantry moving in Y, the head in X and Z.
    Everything here is `estimated` except the travel."""
    tx, ty, tz = travel
    bed_x, bed_y, bed_t = tx + 80, ty + 80, 18.0
    parts: list[Part] = []
    nodes = [Node("base"), Node("gantry", "base", axis="y"), Node("head", "gantry", axis="x"), Node("spindle", "head", axis="z")]
    nodes_stock = Node("bed", "base", stock=True); nodes.insert(1, nodes_stock)
    bed = _box(bed_x, bed_y, bed_t, zmin=-bed_t)
    for k in range(int(bed_x // 60) or 1):
        x = (k - (int(bed_x // 60) - 1) / 2) * 60
        bed = bed - _box(8, bed_y + 2, 6, at=(x, 0, 0), zmin=-6)
    parts.append(Part("bed", "bed", bed, "mdf" if spindle_d < 60 else "alu", collision=True))
    parts.append(Part("frame", "base", _box(bed_x + 80, bed_y + 80, 40, zmin=-bed_t - 40), "anodised"))
    for x in (-bed_x / 2 - 20, bed_x / 2 + 20):
        parts.append(Part(f"Y rail {'L' if x < 0 else 'R'}", "base", _box(40, bed_y + 80, 40, at=(x, 0, 0), zmin=0), "anodised"))
    clear = tz + 20
    gantry = (_box(40, 50, clear + 60, at=(-bed_x / 2 - 20, 0, 0), zmin=40) + _box(40, 50, clear + 60, at=(bed_x / 2 + 20, 0, 0), zmin=40)
              + _box(bed_x + 80, 40, 60, at=(0, 0, 0), zmin=clear + 40))
    parts.append(Part("gantry", "gantry", gantry, "anodised", collision=True))
    hx, hy, nose_z = -tx / 2, ty / 2, clear
    parts.append(Part("X carriage", "head", _box(90, 60, 120, at=(hx, 30, 0), zmin=clear + 10), "anodised"))
    parts.append(Part("spindle body", "spindle", _cyl_z(spindle_d / 2, 95, at=(hx, 0), zmin=nose_z + 20), "alu", collision=True))
    parts.append(Part("collet nut", "spindle", _cyl_z(nut_d / 2, 20, at=(hx, 0), zmin=nose_z), "steel", collision=True))
    nose = [{"name": "collet nut", "r": nut_d / 2, "h": 20.0}, {"name": "spindle", "r": spindle_d / 2, "h": 95.0}]
    rot = None
    if rotary:
        az = float(rotary.get("max_diameter", 80)) / 2 + 8
        nodes.append(Node("chuck", "bed", axis="a", mode="table", stock=True, pivot=(0.0, 0.0, az))); nodes_stock.stock = False
        parts.append(Part("chuck", "chuck", _cyl_x(26, 18, 0, az, -tx / 2 - 10), "steel", collision=True))
        parts.append(Part("tailstock", "bed", _box(40, 60, az + 20, at=(tx / 2 + 20, 0, 0), zmin=0) + _cyl_x(6, 15, 0, az, tx / 2 - 15), "anodised", collision=True))
        rot = {"axis_y": 0.0, "axis_z": az, "chuck_face_x": -tx / 2 + 8, "chuck_r": 26.0, "tail_x": tx / 2 - 15, "tail_r": 6.0,
               "max_diameter": rotary.get("max_diameter", 80), "max_length": rotary.get("max_length", tx)}
    return MachineModel("router", name, nodes, parts, home=(hx, hy, nose_z), travel=travel, clearance=clear, nose=nose,
                        table={"x": bed_x, "y": bed_y, "t": bed_t}, rotary=rot, spoilboard=bed_t if spindle_d < 60 else 0.0,
                        sources={"travel": "verified: machine record", "everything else": "estimated: parametric gantry router"},
                        notes="Representative gantry router: fixed bed, gantry moves Y, head moves X and Z.")


# =============================================================================== registry and payloads
def _bed_holes(key: str) -> list[list[float]]:
    p = ROOT / "machine_data" / "bed_holes.json"
    try:
        return [list(map(float, h)) for h in json.loads(p.read_text())[key]["holes"]]
    except Exception:  # noqa: BLE001
        return []


def model_for(machine) -> MachineModel:
    """The MachineModel for a cam_data Machine (by name / post / rotary), falling back to the parametric router, with the
    machine's `model` overrides (Settings ▸ Machines ▸ Model: nose profile, spoilboard, clearance, stickout) applied."""
    mm = _base_model(machine)
    ov = getattr(machine, "model", None) or {}
    if ov:
        if ov.get("nose"):
            mm.nose = [{"name": str(l.get("name", f"level {i + 1}")), "r": float(l["r"]), "h": float(l["h"])} for i, l in enumerate(ov["nose"]) if "r" in l and "h" in l] or mm.nose
            mm.sources["nose profile"] = "user: edited in Settings ▸ Machines ▸ Model"
        for k in ("spoilboard", "clearance", "stickout"):
            if ov.get(k) is not None:
                setattr(mm, k, float(ov[k])); mm.sources[k] = "user: edited in Settings ▸ Machines ▸ Model"
    return mm


def _base_model(machine) -> MachineModel:
    name = (machine.name or "").lower()
    has_rotary = bool(machine.rotary and machine.rotary.get("installed", True))
    if "z1" in name:
        return makera_z1(has_rotary)
    if "carvera" in name or "air" in name.split():
        return carvera_air(has_rotary)
    if machine.post == "haas" or name.startswith("haas"):
        key = next((k for k in HAAS if k.lower() in name), "Haas VF-2")
        return haas_vmc(key)
    tr = machine.travel or {}
    travel = (float(tr.get("x", 300)), float(tr.get("y", 180)), float(tr.get("z", 45)))
    big = machine.post == "linuxcnc" or travel[0] > 500
    return router(machine.name, travel, spindle_d=80.0 if big else (65.0 if travel[0] > 350 else 52.0), nut_d=25.0 if big else 19.0,
                  rotary=machine.rotary if has_rotary else None)


def mesh_payload(mm: MachineModel, quality: str = "normal") -> dict[str, Any]:
    """Tessellate every part (like a design body) for the viewer: {parts: [{name, node, material, collision, positions, normals, indices}]}."""
    import cad_kernel as ck
    from build123d import Face
    lin = {"draft": 0.6, "normal": 0.25, "fine": 0.1}.get(quality, 0.25)
    out = []
    warnings: list[str] = []
    for i, p in enumerate(mm.parts):
        faces = list(p.shape.faces())
        body = ck.Body(id=i, name=p.name, path=p.name, shape=p.shape, face_ids=list(range(len(faces))))
        try:
            mesh = ck.tessellate_body(body, faces, lin, 0.35, warnings)
        except Exception as ex:  # noqa: BLE001
            warnings.append(f"{p.name}: {ex}"); continue
        out.append({"name": p.name, "node": p.node, "material": p.material, "collision": p.collision,
                    "positions": mesh["positions"], "normals": mesh.get("normals", []), "indices": mesh["indices"],
                    "edges": mesh.get("edges", [])})
    return {"machine": mm.to_payload(), "materials": MATERIALS, "parts": out, "warnings": warnings}


_CACHE: dict[str, dict[str, Any]] = {}


def cached_payload(machine, quality: str = "normal") -> dict[str, Any]:
    key = f"{machine.name}|{bool(machine.rotary and machine.rotary.get('installed', True))}|{machine.post}|{quality}|{json.dumps(getattr(machine, 'model', None), sort_keys=True)}"
    if key not in _CACHE:
        _CACHE[key] = mesh_payload(model_for(machine), quality)
    return _CACHE[key]
