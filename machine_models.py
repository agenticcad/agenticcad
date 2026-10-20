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
    door: str | None = None       # "hinge" (swings `open` degrees about the line through pivot along direction) | "slide" (moves `open` mm along direction)
    open: float = 0.0             # how far a door node moves when the doors are open


@dataclass
class MachineModel:
    key: str                      # z1 | z1_4axis | air | air_4axis | haas | router
    name: str
    nodes: list[Node]
    parts: list[Part]
    home: tuple[float, float, float]           # spindle axis X, Y and nose-bottom Z at machine zero (frame above)
    travel: tuple[float, float, float]
    clearance: float                           # nose bottom above the table at Z top (gantry clearance)
    nose: list[dict[str, Any]]                 # holder/nose profile from the nose bottom upwards: [{name, r, h}] or [{name, box, r, h}]
    table: dict[str, Any]                      # {x, y, t (thickness), ...} table surface extents, centred on the origin
    rotary: dict[str, Any] | None = None       # machine_script.normalize_rotary's dict (axis, chuck, jaws, tailstock) if a 4th axis is modelled
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
                           "pivot": list(n.pivot) if n.pivot else None, "direction": list(n.direction), "door": n.door, "open": n.open}
                          for n in self.nodes]}


# viewer materials: a small painted-metal / plastic / glass palette (PBR values), keyed by Part.material
MATERIALS = {
    "cast": {"color": "#5b6066", "metalness": 0.55, "roughness": 0.78},
    "paint_light": {"color": "#d9dbde", "metalness": 0.1, "roughness": 0.5},
    "paint_white": {"color": "#eceef0", "metalness": 0.05, "roughness": 0.45},
    "paint_dark": {"color": "#2f3338", "metalness": 0.2, "roughness": 0.55},
    "paint_blue": {"color": "#3b5a8f", "metalness": 0.15, "roughness": 0.5},
    "paint_red": {"color": "#c8102e", "metalness": 0.1, "roughness": 0.45},
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
    "led_blue": {"color": "#4f8dff", "metalness": 0.0, "roughness": 0.3, "emissive": "#3a7bff", "emissive_intensity": 2.2},   # status lights
    "lettering": {"color": "#8d9298", "metalness": 0.2, "roughness": 0.5},
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
    # atc: (pockets, "smtc" side-mount drum with an exchanger arm | "carousel" umbrella under the head)
    "Haas VF-2": {"table": (914.4, 355.6), "slots": (3, 16.0, 125.0), "nose": (101.6, 609.6), "travel": (762, 406, 508), "atc": (20, "carousel")},
    "Haas VF-2SS": {"table": (914.4, 355.6), "slots": (3, 16.0, 125.0), "nose": (101.6, 609.6), "travel": (762, 406, 508), "atc": (24, "smtc")},
    "Haas VF-4": {"table": (1320.8, 457.2), "slots": (5, 16.0, 80.0), "nose": (106.7, 741.7), "travel": (1270, 508, 635), "atc": (40, "smtc")},
    "Haas Mini Mill": {"table": (914.4, 304.8), "slots": (3, 16.0, 110.0), "nose": (101.6, 355.6), "travel": (406, 305, 254), "atc": (10, "carousel")},
    "Haas TM-1": {"table": (1212.9, 266.7), "slots": (3, 16.0, 101.6), "nose": (101.6, 508.0), "travel": (762, 305, 406), "atc": (10, "carousel")},
}



# =============================================================================== registry and payloads
def _bed_holes(key: str) -> list[list[float]]:
    p = ROOT / "machine_data" / "bed_holes.json"
    try:
        return [list(map(float, h)) for h in json.loads(p.read_text())[key]["holes"]]
    except Exception:  # noqa: BLE001
        return []


WORKSPACE: Path | None = None            # set by the agent; user machine scripts live in <workspace>/machines/


# ------------------------------------------------------------------------------- 4th-axis geometry overrides
# machine.model["rotary"] (Settings ▸ Machines ▸ Model, or the Machine page after probing) can override where the rotary
# axis is and how the work is mounted. Keys (all optional):
#   axis_y, axis_z     bed frame: mm from the bed centre (+Y back) and above the bed top (the model's frame)
#   axis_my, axis_mz   MACHINE coordinates of the axis (what the controller reports / its config holds), converted with
#                      bed_y = axis_my - anchor1_y + bed_anchor_y            (machine_link: machine XY - anchor1 + bed_anchor = bed XY)
#                      bed_z = axis_mz + clearance - probe_stickout           (machine Z 0 = home: the nose bottom `clearance`
#                                                                             above the bed; a probed value is the MPos Z with
#                                                                             the probing tool's tip `probe_stickout` below the nose)
#                      anchor1_y / bed_anchor_y may be given too (the live config's anchor beats the profile's default)
#   chuck_gap          stock end face in front of the chuck body face (the jaws grip jaw_reach - chuck_gap of the stock)
#   jaw_reach          how far the jaws stand proud of the chuck body face
#   tail               True/False: tailstock fitted;  tail_gap: live-centre tip beyond the stock's far end (negative: in a centre hole)
#   stock_length       nominal bar length: where the script draws the tailstock (the viewer moves it to the program's stock anyway)
#   source, probed     "config" | "probed" | "manual" and an ISO date, for the sources panel
ROTARY_OVERRIDE_KEYS = ("axis_y", "axis_z", "axis_my", "axis_mz", "probe_stickout", "anchor1_y", "bed_anchor_y",
                        "chuck_gap", "jaw_reach", "tail", "tail_gap", "stock_length", "source", "probed")


def _z1_anchors() -> tuple[float, float]:
    """(anchor1_y, bed_anchor_y) of the Makera Z1 from machine_link's profile (machine XY - anchor1 + bed_anchor = bed XY)."""
    try:
        import machine_link
        return float(machine_link.Z1.anchor1[1]), float(machine_link.Z1.bed_anchor[1])
    except Exception:  # noqa: BLE001 - the link module is optional for the model
        return -194.3, -88.0


def rotary_geometry(machine, clearance: float, anchors: tuple[float, float] | None = None, **defaults) -> dict:
    """The rotary mounting numbers a machine script should use: the script's defaults (axis_y, axis_z, chuck_gap, jaw_reach,
    tail, tail_gap, stock_length) with machine.model["rotary"] applied (see ROTARY_OVERRIDE_KEYS). Adds `source` (reference |
    config | probed | manual) and `source_text` (a sources-panel line) describing where the axis position came from.
    `anchors` = (anchor1_y, bed_anchor_y) for the machine-coordinate conversion; None = the Z1's when the machine is a Z1."""
    out = {"axis_y": 0.0, "axis_z": 50.0, "chuck_gap": 5.0, "jaw_reach": 0.0, "tail": True, "tail_gap": 0.0, "stock_length": None}
    out.update(defaults)
    ov = ((getattr(machine, "model", None) or {}).get("rotary")) or {}
    src, notes = "reference", []
    name = (getattr(machine, "name", "") or "").lower()
    if anchors is None and "z1" in name:
        anchors = _z1_anchors()
    a1y = float(ov["anchor1_y"]) if ov.get("anchor1_y") is not None else (anchors[0] if anchors else None)
    bay = float(ov["bed_anchor_y"]) if ov.get("bed_anchor_y") is not None else (anchors[1] if anchors else None)
    if ov.get("axis_my") is not None:
        if a1y is None or bay is None:
            notes.append("axis_my given but this machine's anchor offsets are unknown: add anchor1_y and bed_anchor_y (or give axis_y)")
        else:
            out["axis_y"] = round(float(ov["axis_my"]) - a1y + bay, 3); src = "config"
            notes.append(f"machine Y {float(ov['axis_my']):.2f} → bed Y {out['axis_y']:+.2f} (anchor1 Y {a1y}, bed anchor {bay})")
    elif ov.get("axis_y") is not None:
        out["axis_y"] = float(ov["axis_y"]); src = "manual"
    if ov.get("axis_mz") is not None and ov.get("bed_mz") is not None:
        # both touched with the same probe: the tool length cancels, this is exact
        out["axis_z"] = round(float(ov["axis_mz"]) - float(ov["bed_mz"]), 3); src = "probed"
        notes.append(f"axis at machine Z {float(ov['axis_mz']):.2f}, bed at {float(ov['bed_mz']):.2f} with the same probe → {out['axis_z']:.2f} above the bed")
    elif ov.get("axis_mz") is not None:
        stick = float(ov.get("probe_stickout") or 0.0)
        out["axis_z"] = round(float(ov["axis_mz"]) + float(clearance) - stick, 3); src = "config" if src == "reference" else src
        notes.append(f"machine Z {float(ov['axis_mz']):.2f} → {out['axis_z']:.2f} above the bed (nose {clearance} above the bed at Z 0"
                     + (f", probe tip {stick} below the nose" if stick else ", the probe's own length not allowed for: touch the bare bed "
                        "with it (Zero ▸ Bed height) for the exact height") + ")")
    elif ov.get("axis_z") is not None:
        out["axis_z"] = float(ov["axis_z"]); src = "manual" if src == "reference" else src
    if ov.get("source") in ("config", "probed", "manual") and src != "reference":
        src = ov["source"]
    if ov.get("probed") and src != "reference":
        src = "probed"
    for k in ("chuck_gap", "jaw_reach", "tail_gap", "stock_length"):
        if ov.get(k) is not None:
            out[k] = float(ov[k])
    if ov.get("tail") is not None:
        out["tail"] = bool(ov["tail"])
    out["axis_my"] = ov.get("axis_my"); out["axis_mz"] = ov.get("axis_mz")
    out["source"] = src
    where = {"reference": "reference: community simplified model — probe yours on the Machine page",
             "config": "user: from the machine's config", "manual": "user: set in Settings ▸ Machines ▸ Model",
             "probed": f"user: probed on {ov.get('probed')}" if ov.get("probed") else "user: probed on the machine"}[src]
    out["source_text"] = where + ("; " + "; ".join(notes) if notes else "")
    out["probed"] = ov.get("probed")
    return out


def _apply_rotary_overrides(mm: "MachineModel", machine) -> None:
    """After a script ran: machine.model["rotary"] onto the model's rotary dict (idempotent for the built-in scripts, which
    already read the overrides through rotary_cfg; a script that ignores them still gets the numbers the simulator uses)."""
    ov = ((getattr(machine, "model", None) or {}).get("rotary")) or {}
    if not ov or not mm.rotary:
        return
    rot = mm.rotary
    g = rotary_geometry(machine, mm.clearance, axis_y=rot.get("axis_y", 0.0), axis_z=rot.get("axis_z", 50.0),
                        chuck_gap=rot.get("chuck_gap", 5.0), jaw_reach=rot.get("jaw_reach", 0.0), tail=rot.get("tail", True),
                        tail_gap=rot.get("tail_gap", 0.0))
    for k in ("axis_y", "axis_z", "chuck_gap", "jaw_reach", "tail", "tail_gap"):
        rot[k] = g[k]
    rot["source"] = g["source"]
    if g["source"] != "reference":
        mm.sources["4th axis position"] = g["source_text"]
    for k in ("chuck_gap", "jaw_reach", "tail", "tail_gap"):
        if ov.get(k) is not None:
            mm.sources[f"4th axis {k.replace('_', ' ')}"] = "user: set in Settings ▸ Machines ▸ Model"


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
            import machine_script as _ms
            mm.nose = [_ms.normalize_level(l, i) for i, l in enumerate(ov["nose"]) if ("r" in l or "box" in l or "boxes" in l) and "h" in l] or mm.nose
            mm.sources["nose profile"] = "user: edited in Settings ▸ Machines ▸ Model"
        for k in ("spoilboard", "clearance", "stickout"):
            if ov.get(k) is not None:
                setattr(mm, k, float(ov[k])); mm.sources[k] = "user: edited in Settings ▸ Machines ▸ Model"
        _apply_rotary_overrides(mm, machine)
    return mm


def _record(name: str, post: str = "grbl", travel=(300, 180, 45), rotary: dict | None = None):
    import cam_kernel as cam
    return cam.Machine(name=name, post=post, travel={"x": travel[0], "y": travel[1], "z": travel[2]}, rotary=rotary)


def makera_z1(fourth: bool = False) -> MachineModel:
    """The built-in Z1 model (tests and tools)."""
    # the 4th-axis record matches cam_data's: the module takes 41 mm of Y travel, A up to 1800 deg/min
    return model_for(_record("Makera Z1" + (" + 4th axis" if fourth else ""), "makera", (200, 159, 100) if fourth else (200, 200, 100),
                             {"axis": "A", "about": "x", "max_diameter": 80, "max_length": 150, "max_speed": 1800, "installed": True} if fourth else None),
                     workspace=False)


def carvera_air(fourth: bool = False) -> MachineModel:
    return model_for(_record("Carvera Air" + (" + 4th axis" if fourth else ""), "makera", (300, 200, 130),
                             {"axis": "A", "about": "x", "max_diameter": 92, "max_length": 200, "max_speed": 2400, "installed": True} if fourth else None),
                     workspace=False)


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
