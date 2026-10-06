"""Joints, motion, exploded views and appearances, declared in the design script (code is the design).

    revolute("sun", ["Gearhead/Sun", "Motor/Shaft*"], axis=((0, 0, 0), (0, 0, 1)))       # turns about Z
    revolute("carrier", ["Gearhead/Carrier*"], axis="Z")
    revolute("planet1", ["Gearhead/Planet1", "Gearhead/Bush1"], axis=((12, 0, 0), (0, 0, 1)), parent="carrier")
    couple("carrier", "sun", ratio=0.2)                       # carrier = 0.2 × sun (+ offset)
    couple("planet1", "sun", ratio=-0.5)                      # relative to its parent (the carrier)
    slider("plunger", ["Plunger"], direction=(0, 0, 1), limits=(0, 12))
    drive("sun", 0, 360 * 5, seconds=6)                       # what Play animates
    explode({"Gearhead/RingGear": (0, 0, 30)}, axis="Z")     # explicit offsets (mm) + auto mode for the rest
    appearance({"*Screw*": "steel", "Motor/Stator": "black anodised", "Gearhead/Ring*": "brass"})

Bodies are body paths from `result` or glob patterns (`*`, `?`). Each body belongs to at most one joint; a joint
with `parent=` moves with its parent (its axis is given in the assembly as modelled). Values are degrees for
revolute joints and mm for sliders. Nothing here changes the geometry: the viewer poses, explodes and renders the
bodies, and check_motion() looks for collisions through the motion."""
from __future__ import annotations

import contextvars
import fnmatch
import math
from typing import Any

import numpy as np

_REG: contextvars.ContextVar[dict | None] = contextvars.ContextVar("motion_registry", default=None)

MATERIALS: dict[str, dict[str, Any]] = {
    # physically based appearances for the render mode: color (sRGB hex), metalness, roughness, optional clearcoat
    "steel": {"color": "#a9adb2", "metalness": 1.0, "roughness": 0.34},
    "stainless": {"color": "#b9bcc0", "metalness": 1.0, "roughness": 0.24},
    "black oxide": {"color": "#2a2b2e", "metalness": 0.85, "roughness": 0.45},
    "aluminium": {"color": "#9ca1a8", "metalness": 1.0, "roughness": 0.3},
    "brushed aluminium": {"color": "#a7abb1", "metalness": 1.0, "roughness": 0.55},
    "black anodised": {"color": "#26282b", "metalness": 0.7, "roughness": 0.42},
    "blue anodised": {"color": "#2f5f9e", "metalness": 0.75, "roughness": 0.38},
    "red anodised": {"color": "#a8323a", "metalness": 0.75, "roughness": 0.38},
    "brass": {"color": "#d8b25a", "metalness": 1.0, "roughness": 0.3},
    "bronze": {"color": "#b07a45", "metalness": 1.0, "roughness": 0.38},
    "copper": {"color": "#d0805a", "metalness": 1.0, "roughness": 0.28},
    "gold": {"color": "#e6c06a", "metalness": 1.0, "roughness": 0.2},
    "cast iron": {"color": "#55575b", "metalness": 0.8, "roughness": 0.65},
    "titanium": {"color": "#b3b0aa", "metalness": 1.0, "roughness": 0.4},
    "nickel": {"color": "#bcb7ad", "metalness": 1.0, "roughness": 0.28},
    "rubber": {"color": "#1d1d1f", "metalness": 0.0, "roughness": 0.92},
    "black plastic": {"color": "#202124", "metalness": 0.0, "roughness": 0.45, "clearcoat": 0.2},
    "white plastic": {"color": "#e8e6e1", "metalness": 0.0, "roughness": 0.4, "clearcoat": 0.2},
    "grey plastic": {"color": "#8d9096", "metalness": 0.0, "roughness": 0.45, "clearcoat": 0.15},
    "orange plastic": {"color": "#ff8c42", "metalness": 0.0, "roughness": 0.4, "clearcoat": 0.25},
    "blue plastic": {"color": "#3b7dd8", "metalness": 0.0, "roughness": 0.4, "clearcoat": 0.25},
    "red plastic": {"color": "#d0383d", "metalness": 0.0, "roughness": 0.4, "clearcoat": 0.25},
    "green plastic": {"color": "#3fa34d", "metalness": 0.0, "roughness": 0.4, "clearcoat": 0.25},
    "nylon": {"color": "#efe9dc", "metalness": 0.0, "roughness": 0.6},
    "pcb": {"color": "#1f6b3a", "metalness": 0.1, "roughness": 0.45, "clearcoat": 0.6},
    "glass": {"color": "#e9f2f5", "metalness": 0.0, "roughness": 0.05, "transmission": 0.9},
    "wood": {"color": "#b98a5a", "metalness": 0.0, "roughness": 0.7},
}

# name-based defaults when the script sets no appearance (first match wins)
AUTO_APPEARANCE = [
    ("*magnet*", "nickel"), ("*coil*", "copper"), ("*winding*", "copper"),
    ("*screw*", "black oxide"), ("*bolt*", "steel"), ("*nut*", "steel"), ("*washer*", "steel"),
    ("*pin*", "steel"), ("*shaft*", "stainless"), ("*key*", "steel"), ("*spring*", "steel"), ("*clip*", "steel"),
    ("*ball*", "steel"), ("*bearing*", "steel"), ("*innerring*", "steel"), ("*outerring*", "steel"), ("*cage*", "brass"),
    ("*bush*", "bronze"), ("*gear*", "steel"), ("*planet*", "steel"), ("*sun*", "steel"), ("*pinion*", "steel"),
    ("*connector*", "black plastic"), ("*pcb*", "pcb"), ("*board*", "pcb"), ("*tyre*", "rubber"), ("*tire*", "rubber"),
    ("*seal*", "rubber"), ("*o-ring*", "rubber"), ("*oring*", "rubber"), ("*gasket*", "rubber"),
    ("*stator*", "black anodised"), ("*cap*", "aluminium"), ("*housing*", "aluminium"), ("*plate*", "aluminium"),
    ("*cover*", "aluminium"), ("*case*", "aluminium"), ("*frame*", "aluminium"), ("*bracket*", "aluminium"),
    ("*adapter*", "aluminium"), ("*carrier*", "aluminium"), ("*spacer*", "aluminium"),
]


class MotionError(ValueError):
    pass


def begin() -> contextvars.Token:
    return _REG.set({"joints": {}, "order": [], "couplings": [], "drive": None, "explode": {}, "explode_auto": None,
                     "appearance": []})


def end(tok: contextvars.Token) -> dict:
    reg = _REG.get() or {}
    _REG.reset(tok)
    return reg


def _reg() -> dict:
    r = _REG.get()
    if r is None:                                    # used outside a design run (tests, the REPL)
        begin()
        r = _REG.get()
    return r


def _bodies(bodies) -> list[str]:
    if isinstance(bodies, str):
        return [bodies]
    return [str(b) for b in bodies]


def _axis(axis) -> tuple[list[float], list[float]]:
    if isinstance(axis, str):
        d = {"X": (1, 0, 0), "Y": (0, 1, 0), "Z": (0, 0, 1), "-X": (-1, 0, 0), "-Y": (0, -1, 0), "-Z": (0, 0, -1)}[axis.upper()]
        return [0.0, 0.0, 0.0], [float(v) for v in d]
    if hasattr(axis, "position") and hasattr(axis, "direction"):          # build123d Axis
        p, d = axis.position, axis.direction
        return [p.X, p.Y, p.Z], [d.X, d.Y, d.Z]
    o, d = axis
    n = math.sqrt(sum(float(v) ** 2 for v in d)) or 1.0
    return [float(v) for v in o], [float(v) / n for v in d]


def _add_joint(name: str, kind: str, bodies, origin, direction, parent, limits, value) -> str:
    r = _reg()
    if name in r["joints"]:
        raise MotionError(f"joint '{name}' is defined twice")
    r["joints"][name] = {"name": name, "type": kind, "patterns": _bodies(bodies), "origin": origin, "axis": direction,
                         "parent": parent, "limits": list(limits) if limits else None, "value": float(value)}
    r["order"].append(name)
    return name


def revolute(name: str, bodies, axis="Z", parent: str | None = None, limits=None, value: float = 0.0) -> str:
    """A joint turning `bodies` about `axis` (Axis, 'X'/'Y'/'Z', or ((origin), (direction))); degrees."""
    o, d = _axis(axis)
    return _add_joint(name, "revolute", bodies, o, d, parent, limits, value)


def slider(name: str, bodies, direction=(0, 0, 1), parent: str | None = None, limits=None, value: float = 0.0) -> str:
    """A joint sliding `bodies` along `direction`; mm."""
    o, d = _axis(((0, 0, 0), direction))
    return _add_joint(name, "slider", bodies, o, d, parent, limits, value)


def couple(follower: str, leader: str, ratio: float = 1.0, offset: float = 0.0) -> None:
    """follower = ratio × leader + offset (gears: ratio = −z_leader / z_follower; rack and pinion: mm per degree)."""
    _reg()["couplings"].append({"follower": follower, "leader": leader, "ratio": float(ratio), "offset": float(offset)})


def drive(joint: str, start: float | None = None, stop: float | None = None, seconds: float = 4.0) -> None:
    """The joint Play animates, from `start` to `stop` (default: its limits, or one turn) over `seconds`."""
    _reg()["drive"] = {"joint": joint, "start": start, "stop": stop, "seconds": float(seconds)}


def explode(offsets: dict | None = None, axis: str | None = None, scale: float = 1.0) -> None:
    """Exploded view: explicit offsets {body or glob: (dx, dy, dz)} and/or an automatic mode along `axis`
    ('X'/'Y'/'Z'; radial spread within sub-assemblies), scaled by `scale`."""
    r = _reg()
    for k, v in (offsets or {}).items():
        r["explode"][str(k)] = [float(x) for x in v]
    if axis or not offsets:
        r["explode_auto"] = {"axis": (axis or "Z").upper(), "scale": float(scale)}


def appearance(mapping: dict) -> None:
    """Render appearances {body or glob: material name | {color, metalness, roughness}}; later calls win."""
    for k, v in mapping.items():
        if isinstance(v, str) and v.lower() not in MATERIALS:
            raise MotionError(f"appearance: unknown material '{v}'. Known: {', '.join(MATERIALS)}; or give "
                              "{'color': '#rrggbb', 'metalness': 0..1, 'roughness': 0..1}")
        _reg()["appearance"].append((str(k), v))


def namespace() -> dict[str, Any]:
    return {"revolute": revolute, "slider": slider, "couple": couple, "drive": drive, "explode": explode,
            "appearance": appearance, "MATERIALS": MATERIALS}


# ---------------------------------------------------------------- resolution against the built bodies
def _match(pattern: str, paths: list[str]) -> list[str]:
    exact = [p for p in paths if p == pattern]
    if exact:
        return exact
    low = pattern.lower()
    return [p for p in paths if fnmatch.fnmatchcase(p.lower(), low) or fnmatch.fnmatchcase(p.split("/")[-1].lower(), low)]


def resolve(reg: dict, paths: list[str]) -> dict[str, Any]:
    """Validate the registry against the body paths and return the viewer payload
    {motion: {joints, couplings, drive} | None, explode: {...}, appearance: {path: material}}."""
    joints = [dict(reg["joints"][n]) for n in reg.get("order", [])]
    owner: dict[str, str] = {}
    for j in joints:
        found: list[str] = []
        for pat in j.pop("patterns"):
            m = _match(pat, paths)
            if not m:
                raise MotionError(f"joint '{j['name']}': no body matches '{pat}' (bodies: {', '.join(paths[:30])}"
                                  + (" …" if len(paths) > 30 else "") + ")")
            found += [p for p in m if p not in found]
        for p in found:
            if p in owner:
                raise MotionError(f"body '{p}' is in joints '{owner[p]}' and '{j['name']}'; a body moves with one joint "
                                  "(use parent= to chain joints)")
            owner[p] = j["name"]
        j["bodies"] = found
    names = {j["name"] for j in joints}
    for j in joints:
        seen, p = {j["name"]}, j["parent"]
        while p:
            if p not in names:
                raise MotionError(f"joint '{j['name']}': parent '{p}' is not a joint")
            if p in seen:
                raise MotionError(f"joint '{j['name']}': parent chain loops")
            seen.add(p)
            p = next(x for x in joints if x["name"] == p)["parent"]
    couplings = reg.get("couplings", [])
    followers: dict[str, dict] = {}
    for c in couplings:
        for k in ("follower", "leader"):
            if c[k] not in names:
                raise MotionError(f"couple: '{c[k]}' is not a joint")
        if c["follower"] in followers:
            raise MotionError(f"couple: joint '{c['follower']}' is coupled twice")
        followers[c["follower"]] = c
    for f in followers:                                     # coupling chains must end at a free joint
        seen, cur = set(), f
        while cur in followers:
            if cur in seen:
                raise MotionError(f"couple: the couplings through '{f}' form a loop")
            seen.add(cur)
            cur = followers[cur]["leader"]
    drv = reg.get("drive")
    if drv:
        if drv["joint"] not in names:
            raise MotionError(f"drive: '{drv['joint']}' is not a joint")
        if drv["joint"] in followers:
            raise MotionError(f"drive: '{drv['joint']}' is coupled to another joint; drive the leader instead")
    elif joints:
        free = [j for j in joints if j["name"] not in followers]
        if free:
            drv = {"joint": free[0]["name"], "start": None, "stop": None, "seconds": 4.0}
    if drv:
        j = next(x for x in joints if x["name"] == drv["joint"])
        lim = j["limits"]
        if drv["start"] is None:
            drv["start"] = lim[0] if lim else 0.0
        if drv["stop"] is None:
            drv["stop"] = lim[1] if lim else (360.0 if j["type"] == "revolute" else 20.0)
    explode_off: dict[str, list[float]] = {}
    for pat, v in reg.get("explode", {}).items():
        m = _match(pat, paths)
        if not m:
            raise MotionError(f"explode: no body matches '{pat}'")
        for p in m:
            explode_off[p] = v
    look: dict[str, Any] = {}
    for p in paths:                                                  # name-based defaults
        name = p.lower().replace(" ", "")
        for pat, mat in AUTO_APPEARANCE:
            if fnmatch.fnmatchcase(name, pat) or fnmatch.fnmatchcase(name.split("/")[-1], pat):
                look[p] = mat
                break
    for pat, v in reg.get("appearance", []):
        m = _match(pat, paths)
        if not m:
            raise MotionError(f"appearance: no body matches '{pat}'")
        for p in m:
            look[p] = v
    materials = {p: (MATERIALS[v.lower()] | {"name": v.lower()}) if isinstance(v, str) else dict(v) for p, v in look.items()}
    return {"motion": {"joints": joints, "couplings": couplings, "drive": drv} if joints else None,
            "explode": {"offsets": explode_off, "auto": reg.get("explode_auto")},
            "appearance": materials}


# ---------------------------------------------------------------- kinematics (same maths as the viewer)
def joint_values(motion: dict, overrides: dict[str, float] | None = None) -> dict[str, float]:
    vals = {j["name"]: float(j.get("value", 0.0)) for j in motion["joints"]}
    vals.update(overrides or {})
    fol = {c["follower"]: c for c in motion["couplings"]}

    def val(n, depth=0):
        if n in fol and depth < 64:
            c = fol[n]
            return c["ratio"] * val(c["leader"], depth + 1) + c["offset"]
        return vals[n]
    return {n: val(n) for n in vals}


def _rot(axis_o, axis_d, deg) -> np.ndarray:
    d = np.asarray(axis_d, float); d /= (np.linalg.norm(d) or 1.0)
    a = math.radians(deg); c, s = math.cos(a), math.sin(a)
    K = np.array([[0, -d[2], d[1]], [d[2], 0, -d[0]], [-d[1], d[0], 0]])
    R = np.eye(3) + s * K + (1 - c) * (K @ K)
    o = np.asarray(axis_o, float)
    M = np.eye(4); M[:3, :3] = R; M[:3, 3] = o - R @ o
    return M


def joint_matrices(motion: dict, values: dict[str, float]) -> dict[str, np.ndarray]:
    """World 4×4 matrix of each joint at `values` (a child joint's motion is applied in its parent's moved frame)."""
    by = {j["name"]: j for j in motion["joints"]}
    out: dict[str, np.ndarray] = {}

    def world(n):
        if n in out:
            return out[n]
        j = by[n]
        if j["type"] == "revolute":
            local = _rot(j["origin"], j["axis"], values[n])
        else:
            local = np.eye(4); local[:3, 3] = np.asarray(j["axis"], float) * values[n]
        out[n] = (world(j["parent"]) @ local) if j["parent"] else local
        return out[n]
    for n in by:
        world(n)
    return out
