"""Machine model scripts: a machine is a Python file, like a design (`design.py`) or a CAM program (`cam.py`).

    <workspace>/machines/<slug>.machine.py      the user's model of that library machine (written by hand or by the agent)
    machine_scripts/<key>.machine.py            the built-in models (Makera Z1, Carvera Air, Haas VMCs, gantry router)

A script runs in the design-script namespace (build123d, `import_step`, maths) plus a small kinematic API:

    node(name, parent=None, axis=None, mode="head", stock=False, pivot=None)
        axis x|y|z|a; mode "head" (moves with the tool) or "table" (carries the work the opposite way);
        stock=True on the one node the work rides on; pivot = a point on a rotary axis (machine frame).
    part(name, node, shape, material="cast", collision=False)
        any build123d shape in the machine frame (X right, Y back, Z up, origin = table top centre);
        collision=True for geometry the tool/holder must not hit (table, chuck, tailstock, head, walls).
    machine(home=(x, y, nose_z), travel=(x, y, z), clearance=mm, nose=[{name, r, h}...], table={x, y, t, ...},
            rotary=None | {axis_y, axis_z, chuck_face_x, chuck_r, tail_x, tail_r, max_diameter, max_length},
            spoilboard=0, stickout=None, sources={number: "verified|reference|estimated: where"}, notes="", key=None)
    helpers: box(x, y, z, at=(cx, cy, cz), zmin=None), cyl_z(r, h, at=(x, y), zmin), cyl_x(r, length, y, z, xmin),
             cyl_y(r, length, x, z, ymin), shell(outer, inner, at, zmin, inner_zmin, opening=("front", w, h)),
             bed_holes(key), import_step("file.step") (workspace/imports/), IN = 25.4, HAAS (spec table),
             machine_record (the library Machine this model is for), fourth (its 4th axis is installed).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import cad_kernel as ck
import machine_models as mm

ROOT = Path(__file__).parent
BUILTIN_DIR = ROOT / "machine_scripts"
AXES = (None, "x", "y", "z", "a")
MODES = ("head", "table")


class MachineScriptError(Exception):
    pass


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

    def node(name, parent=None, axis=None, mode="head", stock=False, pivot=None):
        if not isinstance(name, str) or not name:
            raise MachineScriptError("node(): name must be a non-empty string")
        if axis not in AXES:
            raise MachineScriptError(f"node({name!r}): axis must be one of x, y, z, a (got {axis!r})")
        if mode not in MODES:
            raise MachineScriptError(f"node({name!r}): mode must be 'head' or 'table' (got {mode!r})")
        if any(n.name == name for n in nodes):
            raise MachineScriptError(f"node({name!r}): a node with that name already exists")
        if axis == "a" and pivot is None:
            raise MachineScriptError(f"node({name!r}): a rotary (axis='a') node needs pivot=(x, y, z), a point on its axis")
        nodes.append(mm.Node(name, parent, axis, mode, bool(stock), tuple(map(float, pivot)) if pivot is not None else None))

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

    ns = ck.script_namespace()
    ns.update({"node": node, "part": part, "machine": machine_meta, "box": mm._box, "cyl_z": mm._cyl_z, "cyl_x": mm._cyl_x,
               "cyl_y": mm._cyl_y, "shell": mm._shell, "bed_holes": mm._bed_holes, "MATERIALS": list(mm.MATERIALS), "HAAS": mm.HAAS,
               "machine_record": machine, "fourth": _has_fourth(machine), "IN": mm.IN})
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
            nose.append({"name": str(lvl.get("name", f"level {i + 1}")), "r": float(lvl["r"]), "h": float(lvl["h"])})
        except Exception:
            raise MachineScriptError("machine(): nose is a list of {name, r, h} from the nose bottom upwards") from None
        if nose[-1]["r"] <= 0 or nose[-1]["h"] <= 0:
            raise MachineScriptError(f"machine(): nose level {nose[-1]['name']!r} needs r > 0 and h > 0")
    if not nose:
        raise MachineScriptError("machine(): nose needs at least one level (the collet nut or nose)")
    table = dict(meta["table"])
    if not (float(table.get("x", 0)) > 0 and float(table.get("y", 0)) > 0):
        raise MachineScriptError("machine(): table needs x and y (the table top extents, centred on the origin)")
    rotary = meta.get("rotary")
    if rotary is not None:
        need = {"axis_y", "axis_z", "chuck_face_x", "chuck_r", "tail_x", "tail_r"}
        if not need <= set(rotary):
            raise MachineScriptError(f"machine(): rotary needs {sorted(need)}")
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
        how = (f"{n.axis.upper()} {n.mode}" if n.axis else "fixed") + (" · carries the work" if n.stock else "")
        kids = [p.name for p in model.parts if p.node == n.name]
        lines.append(f"  {n.name}" + (f" < {n.parent}" if n.parent else "") + f": {how}; parts: {', '.join(kids) or '-'}")
    lines.append(f"home (spindle X, Y, nose Z) = {model.home}, travel {model.travel}, clearance {model.clearance} mm, "
                 f"table {model.table.get('x')} × {model.table.get('y')} mm, spoilboard {model.spoilboard} mm")
    lines.append("above the tool: " + " → ".join(f"{l['name']} Ø{2 * l['r']:.1f} × {l['h']}" for l in model.nose)
                 + (f" · stickout {model.stickout}" if model.stickout else " · stickout = flute length + 6"))
    if model.rotary:
        r = model.rotary
        lines.append(f"4th axis: axis at y {r['axis_y']}, z {r['axis_z']}; chuck face x {r['chuck_face_x']} (r {r['chuck_r']}); tailstock x {r['tail_x']}")
    coll = [p.name for p in model.parts if p.collision]
    lines.append(f"collision parts: {', '.join(coll) or 'none (mark the table, spindle and anything near the work with collision=True)'}")
    for w in getattr(model, "warnings", []):
        lines.append(f"warning: {w}")
    return "\n".join(lines)
