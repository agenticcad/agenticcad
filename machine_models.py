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
    warnings: list[str] = field(default_factory=list)   # script validation warnings (invalid solids...)

    def to_payload(self) -> dict[str, Any]:
        return {"key": self.key, "name": self.name, "home": list(self.home), "travel": list(self.travel), "clearance": self.clearance,
                "nose": self.nose, "table": self.table, "rotary": self.rotary, "sources": self.sources, "notes": self.notes,
                "spoilboard": self.spoilboard, "stickout": self.stickout, "warnings": list(getattr(self, "warnings", [])),
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


# =============================================================================== Haas vertical machining centres
HAAS = {
    # table L×W (mm), T-slots (count, width, pitch), nose-to-table min/max, travel x/y/z, column/enclosure scale
    "Haas VF-2": {"table": (914.4, 355.6), "slots": (3, 16.0, 125.0), "nose": (101.6, 609.6), "travel": (762, 406, 508)},
    "Haas VF-2SS": {"table": (914.4, 355.6), "slots": (3, 16.0, 125.0), "nose": (101.6, 609.6), "travel": (762, 406, 508)},
    "Haas VF-4": {"table": (1320.8, 457.2), "slots": (5, 16.0, 80.0), "nose": (106.7, 741.7), "travel": (1270, 508, 635)},
    "Haas Mini Mill": {"table": (914.4, 304.8), "slots": (3, 16.0, 110.0), "nose": (101.6, 355.6), "travel": (406, 305, 254)},
    "Haas TM-1": {"table": (1212.9, 266.7), "slots": (3, 16.0, 101.6), "nose": (101.6, 508.0), "travel": (762, 305, 406)},
}



# =============================================================================== registry and payloads
def _bed_holes(key: str) -> list[list[float]]:
    p = ROOT / "machine_data" / "bed_holes.json"
    try:
        return [list(map(float, h)) for h in json.loads(p.read_text())[key]["holes"]]
    except Exception:  # noqa: BLE001
        return []


WORKSPACE: Path | None = None            # set by the agent; user machine scripts live in <workspace>/machines/


def model_for(machine, workspace: Path | None = None) -> MachineModel:
    """The MachineModel for a cam_data Machine: the user's `<slug>.machine.py` in the workspace when there is one, else the
    built-in script (Makera Z1 / Carvera Air / Haas VMC / parametric router), with the machine's `model` overrides
    (Settings ▸ Machines ▸ Model: nose profile, spoilboard, clearance, stickout) applied."""
    import machine_script
    ws = None if workspace is False else (workspace or WORKSPACE)
    code, is_user = machine_script.machine_code(machine, ws)
    mm = machine_script.run(code, machine, ws, filename=f"{machine_script.slug(machine.name)}.machine.py" if is_user else "built-in")
    if is_user:
        mm.sources.setdefault("model", f"user: machines/{machine_script.slug(machine.name)}.machine.py")
    ov = getattr(machine, "model", None) or {}
    if ov:
        if ov.get("nose"):
            mm.nose = [{"name": str(l.get("name", f"level {i + 1}")), "r": float(l["r"]), "h": float(l["h"])} for i, l in enumerate(ov["nose"]) if "r" in l and "h" in l] or mm.nose
            mm.sources["nose profile"] = "user: edited in Settings ▸ Machines ▸ Model"
        for k in ("spoilboard", "clearance", "stickout"):
            if ov.get(k) is not None:
                setattr(mm, k, float(ov[k])); mm.sources[k] = "user: edited in Settings ▸ Machines ▸ Model"
    return mm


def _record(name: str, post: str = "grbl", travel=(300, 180, 45), rotary: dict | None = None):
    import cam_kernel as cam
    return cam.Machine(name=name, post=post, travel={"x": travel[0], "y": travel[1], "z": travel[2]}, rotary=rotary)


def makera_z1(fourth: bool = False) -> MachineModel:
    """The built-in Z1 model (tests and tools)."""
    return model_for(_record("Makera Z1" + (" + 4th axis" if fourth else ""), "makera", (200, 200, 100),
                             {"axis": "A", "about": "x", "max_diameter": 80, "max_length": 150, "installed": True} if fourth else None), workspace=False)


def carvera_air(fourth: bool = False) -> MachineModel:
    return model_for(_record("Carvera Air" + (" + 4th axis" if fourth else ""), "makera", (300, 200, 130),
                             {"axis": "A", "about": "x", "max_diameter": 92, "max_length": 200, "installed": True} if fourth else None), workspace=False)


def haas_vmc(name: str = "Haas VF-2") -> MachineModel:
    spec = HAAS.get(name, HAAS["Haas VF-2"])
    return model_for(_record(name, "haas", spec["travel"]), workspace=False)


def router(name: str, travel, spindle_d: float = 52.0, nut_d: float = 19.0, rotary: dict | None = None) -> MachineModel:
    return model_for(_record(name, "linuxcnc" if spindle_d >= 80 else "grbl", travel, rotary), workspace=False)


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


def clear_cache() -> None:
    _CACHE.clear()


def cached_payload(machine, quality: str = "normal", workspace: Path | None = None) -> dict[str, Any]:
    import machine_script
    ws = workspace or WORKSPACE
    up = machine_script.user_path(machine.name, ws)
    stamp = f"user:{up.stat().st_mtime_ns}" if up is not None and up.exists() else "builtin"
    key = f"{machine.name}|{bool(machine.rotary and machine.rotary.get('installed', True))}|{machine.post}|{quality}|{stamp}|{json.dumps(getattr(machine, 'model', None), sort_keys=True)}"
    if key not in _CACHE:
        _CACHE[key] = mesh_payload(model_for(machine, ws), quality)
    return _CACHE[key]
