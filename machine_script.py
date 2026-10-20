"""Machine model scripts: a machine is a Python file, like a design (`design.py`) or a CAM program (`cam.py`).

    <workspace>/machines/<slug>.machine.py      the user's model of that library machine (written by hand or by the agent)
    machine_scripts/<key>.machine.py            the built-in models (Makera Z1, Carvera Air, Haas VMCs, gantry router)

A script runs in the design-script namespace (build123d, `import_step`, maths) plus a small kinematic API:

    node(name, parent=None, axis=None, mode="head", stock=False, pivot=None, door=None, direction=(1, 0, 0), open=0)
        axis x|y|z|a; mode "head" (moves with the tool) or "table" (carries the work the opposite way);
        stock=True on the one node the work rides on; pivot = a point on a rotary axis (machine frame).
        door="hinge" swings the node's parts `open` degrees about the line through pivot along direction when the
        doors are opened in the viewer (a lid hinged at the back: pivot on the hinge line, direction (1, 0, 0),
        open=-80 lifts the front); door="slide" moves them `open` mm along direction (a sliding door: direction
        (-1, 0, 0), open=700). Door nodes carry the parts that move with the door and animate in the viewer.
    part(name, node, shape, material="cast", collision=False)
        any build123d shape in the machine frame (X right, Y back, Z up, origin = table top centre);
        collision=True for geometry the tool/holder must not hit (table, chuck, tailstock, head, walls).
    machine(home=(x, y, nose_z), travel=(x, y, z), clearance=mm, nose=[{name, r, h}...], table={x, y, t, ...},
            (a level that is not round about the spindle — an off-centre head — is {name, box: [x0, x1, y0, y1], h} in
             the machine's X / Y about the spindle axis, or boxes: [[...], [...]] for a union, e.g. a head + a probe on its side)
            rotary=None | {axis_y, axis_z, chuck_face_x, chuck_r, [chuck_gap=5, jaw_reach=0, jaw_r=chuck_r, jaw_w, jaw_count=4,
                           tail=True, tail_x, tail_r, tail_gap=0, tail_cone={half_angle: 30, length}, tail_barrel=0,
                           tail_block={half_w, z0, z1, length} (bed-frame z), max_diameter, max_length, source]},
            spoilboard=0, stickout=None, sources={number: "verified|reference|estimated: where"}, notes="", key=None)
        rotary: the simulator's solids. chuck_face_x = the chuck BODY face; the jaws stand jaw_reach proud of it (a ring of
        radius jaw_r); the stock's end face sits chuck_gap in front of the body face (inside the jaws). The tailstock's live
        centre: tip at tail_x (cone of half_angle opening to length), then a barrel of radius tail_r for tail_barrel, then
        the block. The viewer and the simulator put the tip at the program's stock end + tail_gap.
    helpers: box(x, y, z, at=(cx, cy, cz), zmin=None), cyl_z(r, h, at=(x, y), zmin), cyl_x(r, length, y, z, xmin),
             cyl_y(r, length, x, z, ymin), shell(outer, inner, at, zmin, inner_zmin, opening=("front", w, h)),
             bed_holes(key), import_step("file.step") (workspace/imports/), IN = 25.4, HAAS (spec table),
             machine_record (the library Machine this model is for), fourth (its 4th axis is installed),
             rotary_cfg(clearance=nose_z, axis_y=.., axis_z=.., chuck_gap=.., jaw_reach=.., tail=.., tail_gap=.., stock_length=..)
               → the same numbers with machine.model["rotary"] applied (axis probed / from the config / set by hand), plus
               `source` and `source_text` for the sources table (machine_models.rotary_geometry).
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

import cad_kernel as ck
import machine_models as mm

ROOT = Path(__file__).parent
BUILTIN_DIR = ROOT / "machine_scripts"
AXES = (None, "x", "y", "z", "a")
MODES = ("head", "table")


def normalize_level(lvl: dict, i: int = 0) -> dict:
    """One level of the holder / nose stack: {name, r, h} round about the spindle axis, or {name, box, h} where box =
    [x0, x1, y0, y1] is the level's footprint relative to the spindle axis in the machine's X / Y (a head that sits
    off-centre, e.g. mostly in front of the spindle). A box level also gets r = its farthest corner from the axis."""
    name = str(lvl.get("name", f"level {i + 1}"))
    h = float(lvl["h"])
    raw = lvl.get("boxes") if lvl.get("boxes") is not None else ([lvl["box"]] if lvl.get("box") is not None else None)
    if raw is not None:                                    # one box, or several (their union: a head with a probe on its side)
        boxes = []
        for bx in raw:
            x0, x1, y0, y1 = (float(v) for v in bx)
            if not (x0 < x1 and y0 < y1):
                raise MachineScriptError(f"machine(): nose level {name!r}: a box is [x0, x1, y0, y1] with x0 < x1 and y0 < y1")
            boxes.append([x0, x1, y0, y1])
        if not boxes:
            raise MachineScriptError(f"machine(): nose level {name!r}: boxes is empty")
        r = max((x * x + y * y) ** 0.5 for b in boxes for x in b[:2] for y in b[2:])
        out = {"name": name, "r": round(r, 3), "h": h, "boxes": boxes,
               "box": [min(b[0] for b in boxes), max(b[1] for b in boxes), min(b[2] for b in boxes), max(b[3] for b in boxes)]}
    else:
        out = {"name": name, "r": float(lvl["r"]), "h": h}
    if out["r"] <= 0 or h <= 0:
        raise MachineScriptError(f"machine(): nose level {name!r} needs r > 0 (or a box) and h > 0")
    return out


class MachineScriptError(Exception):
    pass


ROTARY_REQUIRED = ("axis_y", "axis_z", "chuck_face_x", "chuck_r")


def normalize_rotary(rot: dict) -> dict:
    """The rotary dict a script gives machine(), validated, with defaults for the keys older scripts don't have so the
    simulator and the viewer can rely on every key being there."""
    if not isinstance(rot, dict):
        raise MachineScriptError("machine(): rotary is a dict (see the module docstring)")
    missing = [k for k in ROTARY_REQUIRED if k not in rot]
    if missing:
        raise MachineScriptError(f"machine(): rotary needs {list(ROTARY_REQUIRED)} (missing {missing})")
    out = dict(rot)

    def num(k, default=None, lo=None):
        v = out.get(k, default)
        if v is None:
            return None
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise MachineScriptError(f"machine(): rotary.{k} must be a number (got {v!r})") from None
        if lo is not None and v < lo:
            raise MachineScriptError(f"machine(): rotary.{k} must be ≥ {lo} (got {v})")
        return v

    for k in ROTARY_REQUIRED:
        out[k] = num(k)
    if out["chuck_r"] <= 0:
        raise MachineScriptError("machine(): rotary.chuck_r must be > 0")
    out["chuck_gap"] = num("chuck_gap", 5.0, 0.0)
    out["jaw_reach"] = num("jaw_reach", 0.0, 0.0)
    out["jaw_r"] = num("jaw_r", out["chuck_r"], 0.0)
    out["jaw_w"] = num("jaw_w", None, 0.0)
    out["jaw_count"] = int(out.get("jaw_count") or 4)
    tail = out.get("tail")
    out["tail"] = bool(tail) if tail is not None else out.get("tail_x") is not None
    out["tail_x"] = num("tail_x") if out["tail"] else None
    out["tail_r"] = num("tail_r", 7.0, 0.0)
    out["tail_gap"] = num("tail_gap", 0.0)
    if out["tail"] and out["tail_x"] is None:
        raise MachineScriptError("machine(): rotary.tail is on but tail_x (the live centre's tip X) is missing")
    cone = out.get("tail_cone") or {}
    if not isinstance(cone, dict):
        raise MachineScriptError("machine(): rotary.tail_cone is {half_angle: deg, length: mm}")
    half = float(cone.get("half_angle", 30.0))
    if not 1.0 <= half < 90.0:
        raise MachineScriptError("machine(): rotary.tail_cone.half_angle must be between 1 and 90 degrees")
    length = float(cone.get("length") or (out["tail_r"] / math.tan(math.radians(half))))
    out["tail_cone"] = {"half_angle": half, "length": max(length, 0.0)}
    out["tail_barrel"] = num("tail_barrel", 0.0, 0.0)
    blk = out.get("tail_block")
    if blk is not None:
        try:
            out["tail_block"] = {"half_w": float(blk["half_w"]), "z0": float(blk["z0"]), "z1": float(blk["z1"]), "length": float(blk.get("length", 40.0))}
        except (KeyError, TypeError, ValueError):
            raise MachineScriptError("machine(): rotary.tail_block is {half_w, z0, z1, [length]} (bed-frame z)") from None
    else:
        out["tail_block"] = None
    out["source"] = str(out.get("source") or "reference")
    return out


def slug(name: str) -> str:
    from cam_data import _slug
    return _slug(name or "")


def builtin_key(machine) -> str:
    """Which built-in script models a library machine: by name, then post."""
    name = (machine.name or "").lower()
    if "z1" in name:
        return "makera_z1"
    if "carvera" in name or "air" in name.split():
        return "carvera_air"
    if machine.post == "haas" or name.startswith("haas"):
        if "vf-4" in name and "ss" not in name.replace("vf-4", ""):
            return "haas_vf4"                  # photo-matched VF-4; the other Haas models use the parametric VMC
        return "haas_vmc"
    return "router"


def builtin_code(key: str) -> str:
    p = BUILTIN_DIR / f"{key}.machine.py"
    if not p.exists():
        raise MachineScriptError(f"no built-in machine script {key!r}")
    return p.read_text()


def user_path(name: str, workspace: Path | None) -> Path | None:
    return (Path(workspace) / "machines" / f"{slug(name)}.machine.py") if workspace else None


def machine_code(machine, workspace: Path | None) -> tuple[str, bool]:
    """(script, is_user): the user's script for this machine when there is one, else the built-in it would use."""
    p = user_path(machine.name, workspace)
    if p is not None and p.exists():
        return p.read_text(), True
    return builtin_code(builtin_key(machine)), False


def _has_fourth(machine) -> bool:
    return bool(machine.rotary and machine.rotary.get("installed", True))


def run(code: str, machine, workspace: Path | None = None, filename: str = "machine.py") -> mm.MachineModel:
    """Run a machine script for a library machine and return the validated MachineModel."""
    nodes: list[mm.Node] = []
    parts: list[mm.Part] = []
    meta: dict[str, Any] = {}

    def node(name, parent=None, axis=None, mode="head", stock=False, pivot=None, door=None, direction=(1.0, 0.0, 0.0), open=0.0):
        if not isinstance(name, str) or not name:
            raise MachineScriptError("node(): name must be a non-empty string")
        if door is not None:
            if door not in ("hinge", "slide"):
                raise MachineScriptError(f"node({name!r}): door must be 'hinge' or 'slide' (got {door!r})")
            if axis is not None:
                raise MachineScriptError(f"node({name!r}): a door node has no axis (it moves with the doors toggle, not with the tool)")
            if door == "hinge" and pivot is None:
                raise MachineScriptError(f"node({name!r}): a hinged door needs pivot=(x, y, z), a point on its hinge line")
            try:
                d = tuple(float(v) for v in direction)
                assert len(d) == 3 and sum(v * v for v in d) > 0
            except Exception:
                raise MachineScriptError(f"node({name!r}): direction must be a non-zero (x, y, z) vector") from None
            if not float(open):
                raise MachineScriptError(f"node({name!r}): a door needs open=<degrees or mm> (how far it moves when opened)")
        if axis not in AXES:
            raise MachineScriptError(f"node({name!r}): axis must be one of x, y, z, a (got {axis!r})")
        if mode not in MODES:
            raise MachineScriptError(f"node({name!r}): mode must be 'head' or 'table' (got {mode!r})")
        if any(n.name == name for n in nodes):
            raise MachineScriptError(f"node({name!r}): a node with that name already exists")
        if axis == "a" and pivot is None:
            raise MachineScriptError(f"node({name!r}): a rotary (axis='a') node needs pivot=(x, y, z), a point on its axis")
        nodes.append(mm.Node(name, parent, axis, mode, bool(stock), tuple(map(float, pivot)) if pivot is not None else None,
                             tuple(float(v) for v in direction), door, float(open or 0.0)))

    def part(name, node_name, shape, material="cast", collision=False):
        if not isinstance(name, str) or not name:
            raise MachineScriptError("part(): name must be a non-empty string")
        if shape is None or not hasattr(shape, "faces"):
            raise MachineScriptError(f"part({name!r}): shape must be a build123d shape (got {type(shape).__name__})")
        if material not in mm.MATERIALS:
            raise MachineScriptError(f"part({name!r}): unknown material {material!r}; use one of {', '.join(mm.MATERIALS)}")
        if any(p.name == name for p in parts):
            raise MachineScriptError(f"part({name!r}): a part with that name already exists")
        parts.append(mm.Part(name, node_name, shape, material, bool(collision)))

    def machine_meta(**kw):
        meta.update(kw)

    def rotary_cfg(clearance, anchors=None, **defaults):
        return mm.rotary_geometry(machine, float(clearance), anchors, **defaults)

    ns = ck.script_namespace()
    ns.update({"node": node, "part": part, "machine": machine_meta, "box": mm._box, "cyl_z": mm._cyl_z, "cyl_x": mm._cyl_x,
               "cyl_y": mm._cyl_y, "shell": mm._shell, "bed_holes": mm._bed_holes, "MATERIALS": list(mm.MATERIALS), "HAAS": mm.HAAS,
               "machine_record": machine, "fourth": _has_fourth(machine), "IN": mm.IN, "rotary_cfg": rotary_cfg})
    token = ck._CTX_WORKSPACE.set(Path(workspace) if workspace else None)
    try:
        exec(compile(code, filename, "exec"), ns)
    except MachineScriptError:
        raise
    except Exception as e:  # noqa: BLE001 - surfaced to the agent/user as the script's own error
        raise MachineScriptError(f"{type(e).__name__}: {e}") from e
    finally:
        ck._CTX_WORKSPACE.reset(token)
    return _assemble(machine, nodes, parts, meta)


def _assemble(machine, nodes, parts, meta) -> mm.MachineModel:
    if not nodes:
        raise MachineScriptError("no nodes: declare at least node('base') and a spindle node with axis='z'")
    if not parts:
        raise MachineScriptError("no parts: add the table/bed and the spindle with part(...)")
    names = {n.name for n in nodes}
    for n in nodes:
        if n.parent is not None and n.parent not in names:
            raise MachineScriptError(f"node {n.name!r}: parent {n.parent!r} is not a node")
    roots = [n for n in nodes if n.parent is None]
    if len(roots) != 1:
        raise MachineScriptError(f"exactly one root node (parent=None) is needed; got {[n.name for n in roots]}")
    stock = [n for n in nodes if n.stock]
    if len(stock) != 1:
        raise MachineScriptError(f"exactly one node must carry the work (stock=True); got {[n.name for n in stock]}")
    if not any(n.axis == "z" and n.mode == "head" for n in nodes):
        raise MachineScriptError("no spindle node: one node needs axis='z' (mode 'head') so the tool can move up and down")
    for p in parts:
        if p.node not in names:
            raise MachineScriptError(f"part {p.name!r}: node {p.node!r} is not a node")
    if not meta:
        raise MachineScriptError("machine(...) was not called: give home, travel, clearance, nose and table")
    for k in ("home", "travel", "clearance", "nose", "table"):
        if k not in meta:
            raise MachineScriptError(f"machine(): missing {k}")
    home = tuple(float(v) for v in meta["home"]); travel = tuple(float(v) for v in meta["travel"])
    if len(home) != 3 or len(travel) != 3:
        raise MachineScriptError("machine(): home and travel are (x, y, z) triples")
    nose = []
    for i, lvl in enumerate(meta["nose"]):
        try:
            nose.append(normalize_level(lvl, i))
        except MachineScriptError:
            raise
        except Exception:
            raise MachineScriptError("machine(): nose is a list of {name, r, h} (or {name, box: [x0, x1, y0, y1], h} for a head "
                                     "that is not round about the spindle) from the nose bottom upwards") from None
    if not nose:
        raise MachineScriptError("machine(): nose needs at least one level (the collet nut or nose)")
    table = dict(meta["table"])
    if not (float(table.get("x", 0)) > 0 and float(table.get("y", 0)) > 0):
        raise MachineScriptError("machine(): table needs x and y (the table top extents, centred on the origin)")
    rotary = meta.get("rotary")
    if rotary is not None:
        rotary = normalize_rotary(rotary)
        if not any(n.axis == "a" for n in nodes):
            raise MachineScriptError("machine(): rotary given but no node has axis='a'")
    warnings: list[str] = []
    for p in parts:
        try:
            ok = p.shape.is_valid
            ok = ok() if callable(ok) else ok
        except Exception:  # noqa: BLE001
            ok = True
        if not ok:
            warnings.append(f"part {p.name!r}: invalid solid (check overlapping booleans)")
    sources = {str(k): str(v) for k, v in (meta.get("sources") or {}).items()}
    key = str(meta.get("key") or slug(machine.name))
    model = mm.MachineModel(key, machine.name, nodes, parts, home=home, travel=travel, clearance=float(meta["clearance"]), nose=nose,
                            table=table, rotary=rotary, sources=sources, notes=str(meta.get("notes") or ""),
                            spoilboard=float(meta.get("spoilboard") or 0.0),
                            stickout=float(meta["stickout"]) if meta.get("stickout") else None)
    model.warnings = warnings
    return model


def summary(model: mm.MachineModel) -> str:
    """What the agent sees after build_machine."""
    lines = [f"Machine model '{model.name}': {len(model.parts)} parts on {len(model.nodes)} nodes"]
    for n in model.nodes:
        how = (f"{n.axis.upper()} {n.mode}" if n.axis else (f"door ({n.door}, open {n.open:g})" if n.door else "fixed")) + (" · carries the work" if n.stock else "")
        kids = [p.name for p in model.parts if p.node == n.name]
        lines.append(f"  {n.name}" + (f" < {n.parent}" if n.parent else "") + f": {how}; parts: {', '.join(kids) or '-'}")
    lines.append(f"home (spindle X, Y, nose Z) = {model.home}, travel {model.travel}, clearance {model.clearance} mm, "
                 f"table {model.table.get('x')} × {model.table.get('y')} mm, spoilboard {model.spoilboard} mm")
    lines.append("above the tool: " + " → ".join(f"{l['name']} Ø{2 * l['r']:.1f} × {l['h']}" for l in model.nose)
                 + (f" · stickout {model.stickout}" if model.stickout else " · stickout = flute length + 6"))
    if model.rotary:
        r = model.rotary
        lines.append(f"4th axis: axis at y {r['axis_y']}, z {r['axis_z']} ({r.get('source', 'reference')}); chuck body face x {r['chuck_face_x']} "
                     f"(r {r['chuck_r']}), jaws {r.get('jaw_reach', 0):g} proud (r {r.get('jaw_r', r['chuck_r']):g}), stock {r.get('chuck_gap', 5):g} in front of the face; "
                     + (f"tailstock tip x {r['tail_x']} (cone {r['tail_cone']['half_angle']:g}°, r {r['tail_r']})" if r.get("tail", True) else "no tailstock"))
    coll = [p.name for p in model.parts if p.collision]
    lines.append(f"collision parts: {', '.join(coll) or 'none (mark the table, spindle and anything near the work with collision=True)'}")
    for w in getattr(model, "warnings", []):
        lines.append(f"warning: {w}")
    return "\n".join(lines)
