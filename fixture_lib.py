"""Fixtures: vises, clamps, tooling plates and pallets that hold the work on a machine. A fixture is a script, like a
machine model (`machine_script.py`):

    fixture_scripts/<key>.fixture.py            the built-ins (Makera low-profile vise, Carvera Air vise, 4" screwless vise,
                                                6" Kurt-style vise, tooling plate, step clamps)
    <workspace>/fixtures/<slug>.fixture.py      the user's own (written by hand or by the agent with build_fixture)

Fixture frame: X right, Y back, Z up, origin = where the fixture sits on the table (its footprint centre, z = 0 on the
table top). A script runs in the design-script namespace plus:

    part(name, shape, material="steel", collision=True)       a build123d shape in the fixture frame
    fixture(name, work_origin=(x, y, z), clamp_axis="y"|None, max_opening=mm, mount={...}, sources={...}, notes="",
            replaces_spoilboard=False, home=None)
        work_origin: where the STOCK's bottom-centre sits when it is held (e.g. the jaw floor between the jaws)
        clamp_axis: "y" when the jaws close along Y (the stock's Y size sets the opening), "x", or None (plates, clamps)
        replaces_spoilboard: the fixture bolts to the bed with the spoilboard removed (its z = 0 is the bed under the board)
        home: (x, y) where it usually sits on the table, from the table centre (the default placement)
    params: dict of this fixture's parameters — `opening` (jaw gap, defaults to the stock size along clamp_axis),
            plus whatever the script documents (jaw_h, parallel, size, ...); `stock`: {"x", "y", "z"} of the stock or None
    helpers: box, cyl_z, cyl_x, cyl_y (as in machine scripts), IN = 25.4, MATERIALS

The CAM Setup takes `fixture="name"` (+ fixture_at, fixture_rot, fixture_params) for the fixture that holds the stock and
`fixtures=[{"name", "at", "rot", "params"}, ...]` for more fixtures on the table at the same time (a second vise, clamps,
a plate), each placed and turned on its own; the simulator treats every fixture's collision parts as uncuttable (any
contact by the tool or holder is a collision) and the viewer shows them on the machine with the stock seated.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cad_kernel as ck
import machine_models as mm

ROOT = Path(__file__).parent
BUILTIN_DIR = ROOT / "fixture_scripts"
WORKSPACE: Path | None = None

# key -> display name (the fallback list; a user script with the same name replaces the built-in)
BUILTINS = {
    "makera_lowprofile_vise": "Makera low-profile vise",
    "carvera_air_vise": "Carvera Air vise",
    "screwless_vise_4in": '4" screwless vise',
    "kurt_vise_6in": '6" Kurt-style vise',
    "tooling_plate": "Tooling plate",
    "step_clamps": "Step clamps",
    "makera_l_bracket": "Makera L-bracket",
    "strap_clamp": "Strap clamp",
}


class FixtureScriptError(Exception):
    pass


@dataclass
class FixturePart:
    name: str
    shape: Any
    material: str = "steel"
    collision: bool = True


@dataclass
class FixtureModel:
    key: str
    name: str
    parts: list[FixturePart]
    work_origin: tuple[float, float, float]
    clamp_axis: str | None = None
    max_opening: float | None = None
    mount: dict[str, Any] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    notes: str = ""
    warnings: list[str] = field(default_factory=list)
    replaces_spoilboard: bool = False
    home: tuple[float, float] | None = None

    @property
    def height(self) -> float:
        return float(self.work_origin[2])

    def bbox(self):
        lo = [1e18] * 3; hi = [-1e18] * 3
        for p in self.parts:
            bb = p.shape.bounding_box()
            lo = [min(lo[0], bb.min.X), min(lo[1], bb.min.Y), min(lo[2], bb.min.Z)]
            hi = [max(hi[0], bb.max.X), max(hi[1], bb.max.Y), max(hi[2], bb.max.Z)]
        return lo, hi

    def to_payload(self) -> dict[str, Any]:
        lo, hi = self.bbox()
        return {"key": self.key, "name": self.name, "work_origin": list(self.work_origin), "clamp_axis": self.clamp_axis,
                "max_opening": self.max_opening, "mount": self.mount, "params": self.params, "sources": self.sources, "notes": self.notes,
                "warnings": list(self.warnings), "bbox": [lo, hi], "height": self.height,
                "replaces_spoilboard": self.replaces_spoilboard, "home": list(self.home) if self.home else None}


def slug(name: str) -> str:
    from cam_data import _slug
    return _slug(name or "")


def user_path(name: str, workspace: Path | None) -> Path | None:
    return (Path(workspace) / "fixtures" / f"{slug(name)}.fixture.py") if workspace else None


def _script_name(code: str) -> str | None:
    m = re.search(r"fixture\(\s*(?:name\s*=\s*)?([\"'])((?:(?!\1).)+)\1", code)       # names may contain the other quote: '3" vise'
    return m.group(2) if m else None


def list_fixtures(workspace: Path | None = None) -> list[dict[str, Any]]:
    """Every fixture available: the built-ins (unless a user script replaces them) and the user's scripts."""
    ws = workspace or WORKSPACE
    def home_of(code: str):                     # a fixed place on the table (fixture(..., home=(x, y))), read without running it
        m = re.search(r"home\s*=\s*\(\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\)", code)
        return [float(m.group(1)), float(m.group(2))] if m else None
    out: dict[str, dict[str, Any]] = {}
    for key, name in BUILTINS.items():
        out[name] = {"name": name, "key": key, "user": False, "home": home_of((BUILTIN_DIR / f"{key}.fixture.py").read_text())}
    if ws:
        for p in sorted((Path(ws) / "fixtures").glob("*.fixture.py")):
            code = p.read_text(errors="ignore")
            name = _script_name(code) or p.name[:-len(".fixture.py")]
            out[name] = {"name": name, "key": p.name[:-len(".fixture.py")], "user": True, "home": home_of(code)}
    return list(out.values())


def fixture_code(name: str, workspace: Path | None = None) -> tuple[str, bool]:
    """(script, is_user) for a fixture name: the user's script when there is one, else the built-in of that name."""
    ws = workspace or WORKSPACE
    p = user_path(name, ws)
    if p is not None and p.exists():
        return p.read_text(), True
    if ws:
        for q in (Path(ws) / "fixtures").glob("*.fixture.py"):
            code = q.read_text(errors="ignore")
            if _script_name(code) == name:
                return code, True
    for key, nm in BUILTINS.items():
        if nm == name or key == name or nm.lower() == (name or "").lower():
            return (BUILTIN_DIR / f"{key}.fixture.py").read_text(), False
    raise FixtureScriptError(f"no fixture named {name!r}; available: {', '.join(f['name'] for f in list_fixtures(ws))}")


def run(code: str, params: dict[str, Any] | None = None, stock: dict[str, float] | None = None, workspace: Path | None = None,
        filename: str = "fixture.py") -> FixtureModel:
    parts: list[FixturePart] = []
    meta: dict[str, Any] = {}
    params = dict(params or {})

    def part(name, shape, material="steel", collision=True):
        if not isinstance(name, str) or not name:
            raise FixtureScriptError("part(): name must be a non-empty string")
        if shape is None or not hasattr(shape, "faces"):
            raise FixtureScriptError(f"part({name!r}): shape must be a build123d shape (got {type(shape).__name__})")
        if material not in mm.MATERIALS:
            raise FixtureScriptError(f"part({name!r}): unknown material {material!r}; use one of {', '.join(mm.MATERIALS)}")
        if any(p.name == name for p in parts):
            raise FixtureScriptError(f"part({name!r}): a part with that name already exists")
        parts.append(FixturePart(name, shape, material, bool(collision)))

    def fixture(name=None, **kw):
        if name is not None:
            kw["name"] = name
        meta.update(kw)

    ns = ck.script_namespace()
    ns.update({"part": part, "fixture": fixture, "box": mm._box, "cyl_z": mm._cyl_z, "cyl_x": mm._cyl_x, "cyl_y": mm._cyl_y,
               "params": params, "stock": stock, "MATERIALS": list(mm.MATERIALS), "IN": mm.IN})
    token = ck._CTX_WORKSPACE.set(Path(workspace) if workspace else None)
    try:
        exec(compile(code, filename, "exec"), ns)
    except FixtureScriptError:
        raise
    except ValueError as e:                                # a script's own "can't hold that" message: pass it on as written
        raise FixtureScriptError(str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise FixtureScriptError(f"{type(e).__name__}: {e}") from e
    finally:
        ck._CTX_WORKSPACE.reset(token)
    if not parts:
        raise FixtureScriptError("no parts: add the fixture's body and jaws with part(...)")
    if not meta.get("name"):
        raise FixtureScriptError("fixture(name=...) was not called")
    if "work_origin" not in meta:
        raise FixtureScriptError("fixture(): give work_origin=(x, y, z), where the stock's bottom-centre sits when held")
    wo = tuple(float(v) for v in meta["work_origin"])
    if len(wo) != 3:
        raise FixtureScriptError("fixture(): work_origin is an (x, y, z) triple")
    ca = meta.get("clamp_axis")
    if ca not in (None, "x", "y"):
        raise FixtureScriptError("fixture(): clamp_axis is 'x', 'y' or None")
    home = meta.get("home")
    if home is not None:
        home = tuple(float(v) for v in home)
        if len(home) != 2:
            raise FixtureScriptError("fixture(): home is an (x, y) pair, from the table centre")
    warnings = []
    for p in parts:
        try:
            ok = p.shape.is_valid
            ok = ok() if callable(ok) else ok
        except Exception:  # noqa: BLE001
            ok = True
        if not ok:
            warnings.append(f"part {p.name!r}: invalid solid (check overlapping booleans)")
    model = FixtureModel(str(meta.get("key") or slug(meta["name"])), str(meta["name"]), parts, wo, ca,
                         float(meta["max_opening"]) if meta.get("max_opening") is not None else None,
                         dict(meta.get("mount") or {}), params, {str(k): str(v) for k, v in (meta.get("sources") or {}).items()},
                         str(meta.get("notes") or ""), warnings, bool(meta.get("replaces_spoilboard")), home)
    return model


def model_for(name: str, params: dict[str, Any] | None = None, stock: dict[str, float] | None = None, workspace: Path | None = None) -> FixtureModel:
    ws = workspace or WORKSPACE
    code, is_user = fixture_code(name, ws)
    model = run(code, params, stock, ws, filename=f"{slug(name)}.fixture.py" if is_user else "built-in")
    if is_user:
        model.sources.setdefault("model", f"user: fixtures/{slug(name)}.fixture.py")
    return model


def summary(model: FixtureModel) -> str:
    lo, hi = model.bbox()
    lines = [f"Fixture '{model.name}': {len(model.parts)} parts, footprint {hi[0] - lo[0]:.0f} × {hi[1] - lo[1]:.0f} mm, height {hi[2]:.0f} mm",
             f"work origin (stock bottom-centre) at {model.work_origin}" + (f", clamps along {model.clamp_axis.upper()}" if model.clamp_axis else ", no clamping axis (plate / clamps)")
             + (f", max opening {model.max_opening:g} mm" if model.max_opening else ""),
             "parts: " + ", ".join(p.name + ("" if p.collision else " (no collision)") for p in model.parts)]
    if model.params:
        lines.append("params: " + json.dumps(model.params))
    for w in model.warnings:
        lines.append(f"warning: {w}")
    return "\n".join(lines)


def mesh_payload(model: FixtureModel, quality: str = "normal") -> dict[str, Any]:
    from build123d import Face  # noqa: F401
    lin = {"draft": 0.6, "normal": 0.25, "fine": 0.1}.get(quality, 0.25)
    out = []; warnings: list[str] = []
    for i, p in enumerate(model.parts):
        faces = list(p.shape.faces())
        body = ck.Body(id=i, name=p.name, path=p.name, shape=p.shape, face_ids=list(range(len(faces))))
        try:
            mesh = ck.tessellate_body(body, faces, lin, 0.35, warnings)
        except Exception as ex:  # noqa: BLE001
            warnings.append(f"{p.name}: {ex}"); continue
        out.append({"name": p.name, "material": p.material, "collision": p.collision, "positions": mesh["positions"],
                    "normals": mesh.get("normals", []), "indices": mesh["indices"], "edges": mesh.get("edges", [])})
    return {"fixture": model.to_payload(), "materials": mm.MATERIALS, "parts": out, "warnings": warnings}


_CACHE: dict[str, dict[str, Any]] = {}


def clear_cache() -> None:
    _CACHE.clear()


def cached_payload(name: str, params: dict[str, Any] | None, stock: dict[str, float] | None, quality: str = "normal",
                   workspace: Path | None = None) -> dict[str, Any]:
    ws = workspace or WORKSPACE
    up = user_path(name, ws)
    stamp = f"user:{up.stat().st_mtime_ns}" if up is not None and up.exists() else "builtin"
    key = f"{name}|{json.dumps(params or {}, sort_keys=True)}|{json.dumps(stock or {}, sort_keys=True)}|{quality}|{stamp}"
    if key not in _CACHE:
        _CACHE[key] = mesh_payload(model_for(name, params, stock, ws), quality)
    return _CACHE[key]


def transform_to_model(shape, rot_deg: float, stock_bc: tuple[float, float, float], work_origin: tuple[float, float, float]):
    """A fixture-frame shape in model coordinates: model = stock_bottom_centre + Rz(rot) · (p − work_origin)."""
    from build123d import Pos, Rot
    wx, wy, wz = work_origin
    return Pos(*stock_bc) * Rot(0, 0, rot_deg) * Pos(-wx, -wy, -wz) * shape


def _rz(rot_deg: float, x: float, y: float) -> tuple[float, float]:
    a = math.radians(rot_deg)
    return math.cos(a) * x - math.sin(a) * y, math.sin(a) * x + math.cos(a) * y


def stock_on_table(holder: FixtureModel | None, at: tuple[float, float], rot: float) -> tuple[float, float, float]:
    """Where the stock's bottom-centre sits in the table frame (x, y from the table centre; z above the plane the fixtures
    stand on): the holding fixture's work origin, placed at `at` and turned by `rot`; on the bare table, `at` itself."""
    if holder is None:                                        # bare stock: its bottom-centre sits at `at`
        return (float(at[0]), float(at[1]), 0.0) if at is not None else (0.0, 0.0, 0.0)
    wx, wy = _rz(rot, holder.work_origin[0], holder.work_origin[1])
    return (float(at[0]) + wx, float(at[1]) + wy, float(holder.work_origin[2]))


def frame_in_model(at: tuple[float, float], stock_bc_model: tuple[float, float, float], stock_table: tuple[float, float, float]):
    """Model coordinates of a fixture frame's origin placed at `at` on the table, given where the stock sits in the model
    and on the table (every fixture stands on the same plane)."""
    return (stock_bc_model[0] + float(at[0]) - stock_table[0], stock_bc_model[1] + float(at[1]) - stock_table[1],
            stock_bc_model[2] - stock_table[2])


def placed_shape(shape, rot_deg: float, origin_model: tuple[float, float, float]):
    """A fixture-frame shape turned by rot about its frame origin and moved so that origin lands at origin_model."""
    from build123d import Pos, Rot
    return Pos(*origin_model) * Rot(0, 0, rot_deg) * shape
