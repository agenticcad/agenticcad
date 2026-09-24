"""
CAD kernel wrapper around build123d (OCCT).

Executes agent-authored build123d scripts, splits the result into named bodies
(optionally nested in components), tessellates each body for display with exact
surface normals, and exports exact STEP / tessellated STL.

The script assigns `result`, which may be:
  - a single shape                          -> one body
  - a dict {name: shape | dict}             -> named bodies; nested dicts are components
  - a list/tuple of shapes                  -> Body1, Body2, ...
A shape containing several disjoint solids is split into separate bodies.
"""
from __future__ import annotations

import io
import math
from contextvars import ContextVar
import traceback
import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import build123d as b3d
from build123d import Compound, Face, Edge, Shape, Vector
import threads as thr

# OCP (the OCCT bindings bundled with build123d) is used for display meshing so we
# can pull exact surface normals and adaptive curve samples straight off the B-rep.
from OCP.BRepAdaptor import BRepAdaptor_Surface, BRepAdaptor_Curve
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRep import BRep_Tool
from OCP.BRepLib import BRepLib_ToolTriangulatedShape
from OCP.GCPnts import GCPnts_TangentialDeflection
from OCP.TopLoc import TopLoc_Location
from OCP.TopAbs import TopAbs_REVERSED
from OCP.BRepTools import BRepTools
from OCP.StlAPI import StlAPI_Writer

# Display-mesh quality presets: (linear deflection as fraction of bbox diagonal, angular deflection rad)
QUALITY = {
    "draft": (0.002, 0.25),
    "normal": (0.0008, 0.08),
    "fine": (0.0003, 0.03),
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class FaceInfo:
    id: int
    body: int                     # body id
    kind: str                     # PLANE, CYLINDER, CONE, SPHERE, TORUS, BSPLINE...
    area: float
    center: tuple[float, float, float]
    normal: tuple[float, float, float] | None
    bbox_min: tuple[float, float, float]
    bbox_max: tuple[float, float, float]
    radius: float | None = None
    edge_count: int = 0
    body_name: str = ""

    def summary(self) -> str:
        c = ", ".join(f"{v:.2f}" for v in self.center)
        size = ", ".join(f"{hi - lo:.2f}" for lo, hi in zip(self.bbox_min, self.bbox_max))
        s = f"face#{self.id} [{self.body_name}] {self.kind} area={self.area:.2f}mm² center=({c}) size=({size})"
        if self.normal is not None:
            n = ", ".join(f"{v:.2f}" for v in self.normal)
            s += f" normal=({n}) {axis_name(self.normal)}"
        if self.radius is not None:
            s += f" r={self.radius:.2f}"
        return s

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "body": self.body, "kind": self.kind, "area": round(self.area, 3),
            "center": [round(v, 3) for v in self.center],
            "normal": [round(v, 3) for v in self.normal] if self.normal else None,
            "bboxMin": [round(v, 3) for v in self.bbox_min],
            "bboxMax": [round(v, 3) for v in self.bbox_max],
            "radius": round(self.radius, 3) if self.radius is not None else None,
            "label": self.short_label(),
        }

    def short_label(self) -> str:
        size = [hi - lo for lo, hi in zip(self.bbox_min, self.bbox_max)]
        if self.kind == "PLANE":
            dims = sorted(size, reverse=True)[:2]
            return f"#{self.id} plane {axis_name(self.normal)} {dims[0]:.1f}×{dims[1]:.1f}"
        if self.kind == "CYLINDER" and self.radius:
            return f"#{self.id} cylinder r{self.radius:.1f}"
        return f"#{self.id} {self.kind.lower()}"


@dataclass
class Body:
    id: int
    name: str
    path: str                     # "Component/Sub/Body"
    shape: Shape
    face_ids: list[int] = field(default_factory=list)
    edge_ids: list[int] = field(default_factory=list)
    volume: float = 0.0
    bbox_min: tuple[float, float, float] = (0, 0, 0)
    bbox_max: tuple[float, float, float] = (0, 0, 0)

    def summary(self) -> str:
        size = [hi - lo for lo, hi in zip(self.bbox_min, self.bbox_max)]
        return (f"body#{self.id} '{self.path}' volume={self.volume:.1f}mm³ size=({fmt(size)}) "
                f"center=({fmt([(a + b) / 2 for a, b in zip(self.bbox_min, self.bbox_max)])}) "
                f"faces {self.face_ids[0]}..{self.face_ids[-1]}" if self.face_ids else
                f"body#{self.id} '{self.path}' (no faces)")


@dataclass
class Model:
    bodies: list[Body]
    code: str
    faces: list[FaceInfo]
    mesh: dict[str, Any]          # JSON-serialisable payload for the browser
    bbox_min: tuple[float, float, float]
    bbox_max: tuple[float, float, float]
    volume: float
    stdout: str = ""
    warnings: list[str] = field(default_factory=list)
    threads: list[Any] = field(default_factory=list)      # threads.ThreadSpec registered by the script
    sketches: list[dict[str, Any]] = field(default_factory=list)   # top-level build123d Sketch objects

    @property
    def shape(self) -> Shape:
        """All bodies as one compound (exact; solids stay separate)."""
        if len(self.bodies) == 1:
            return self.bodies[0].shape
        return Compound(children=[b.shape for b in self.bodies])

    def get_face(self, fid: int) -> Face:
        """The exact B-rep Face for a global face id (as shown in the viewer / inspect_model)."""
        for b in self.bodies:
            if fid in b.face_ids:
                return b.shape.faces()[b.face_ids.index(fid)]
        raise CadError(f"no face #{fid}")

    def get_edge(self, eid: int) -> Edge:
        """The exact B-rep Edge for a global edge id (as used by the measure tool)."""
        for b in self.bodies:
            if eid in b.edge_ids:
                return b.shape.edges()[b.edge_ids.index(eid)]
        raise CadError(f"no edge #{eid}")

    def body_by_name(self, name: str) -> Body | None:
        for b in self.bodies:
            if b.name == name or b.path == name:
                return b
        return None

    def summary(self, max_faces: int = 40) -> str:
        size = [hi - lo for lo, hi in zip(self.bbox_min, self.bbox_max)]
        if not self.bodies:
            return "Model OK: empty design (no bodies yet). Assign shapes to `result` to add geometry." + (
                "\nwarnings: " + "; ".join(self.warnings) if self.warnings else "")
        lines = [
            f"Model OK: {len(self.bodies)} bod{'y' if len(self.bodies) == 1 else 'ies'}, "
            f"{len(self.faces)} faces, total volume={self.volume:.1f}mm³",
            f"bbox size X={size[0]:.2f} Y={size[1]:.2f} Z={size[2]:.2f}  "
            f"min=({fmt(self.bbox_min)}) max=({fmt(self.bbox_max)})",
        ]
        for b in self.bodies:
            lines.append("  " + b.summary())
        if self.sketches:
            lines.append("Sketches (build123d Sketch objects; extrude(sk, amount=h), or part - extrude(sk, amount=-h) to cut): "
                         + "; ".join(f"{sk['name']}: {sk['faces']} face(s), area {sk['area']:.1f}mm², on plane origin ({fmt(sk['origin'])}) normal ({fmt(sk['normal'])})" for sk in self.sketches))
        if self.threads:
            lines.append("Threads: " + "; ".join(f"{t.label()} at ({fmt(t.at)}) axis ({fmt(t.axis)})" for t in self.threads))
        if self.warnings:
            lines.append("warnings: " + "; ".join(self.warnings))
        if self.stdout.strip():
            lines.append("stdout:\n" + self.stdout.strip()[-2000:])
        if max_faces:
            faces = sorted(self.faces, key=lambda f: -f.area)
            lines.append(f"Largest faces (of {len(self.faces)}; use inspect_model for all):")
            for f in faces[:max_faces]:
                lines.append("  " + f.summary())
        return "\n".join(lines)


def fmt(v) -> str:
    return ", ".join(f"{x:.2f}" for x in v)


def axis_name(n) -> str:
    if n is None:
        return ""
    axes = {"+X": (1, 0, 0), "-X": (-1, 0, 0), "+Y": (0, 1, 0),
            "-Y": (0, -1, 0), "+Z": (0, 0, 1), "-Z": (0, 0, -1)}
    for name, a in axes.items():
        if sum(p * q for p, q in zip(n, a)) > 0.98:
            return name
    return "tilted"


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
class CadError(Exception):
    pass


def _unwrap(obj: Any) -> Any:
    if isinstance(obj, b3d.Builder):
        obj = obj._obj
    if hasattr(obj, "part") and not isinstance(obj, Shape):
        obj = obj.part
    if hasattr(obj, "sketch") and not isinstance(obj, Shape):
        obj = obj.sketch
    return obj


def coerce_bodies(obj: Any, prefix: str = "", out: list[tuple[str, Shape]] | None = None) -> list[tuple[str, Shape]]:
    """Flatten `result` into [(path, shape)]; dict keys become tree path segments."""
    if out is None:
        out = []
    obj = _unwrap(obj)
    if obj is None:
        return out                                   # `result = None` → empty design
    if isinstance(obj, dict):
        for k, v in obj.items():
            coerce_bodies(v, f"{prefix}{k}/", out)
        return out
    if isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            v = _unwrap(v)
            if isinstance(v, (dict, list, tuple)):
                coerce_bodies(v, f"{prefix}Component{i + 1}/", out)
            else:
                coerce_bodies(v, f"{prefix}Body{i + 1}/", out)
        return out
    if not isinstance(obj, Shape):
        raise CadError(f"`result` entries must be build123d shapes, got {type(obj).__name__} at '{prefix or '/'}'.")
    name = prefix.rstrip("/") or "Body1"
    # split a shape holding several disjoint solids into separate bodies (Fusion does the same)
    try:
        solids = obj.solids() if isinstance(obj, Compound) or obj.__class__.__name__ in ("Part", "Compound") else [obj]
    except Exception:
        solids = [obj]
    if len(solids) > 1:
        for i, s in enumerate(solids):
            out.append((f"{name} {i + 1}", s))
    elif len(solids) == 1 and isinstance(obj, Compound):
        out.append((name, solids[0]))
    else:
        out.append((name, obj))
    return out


WORKSPACE: Path | None = None      # process-wide fallbacks (single-session app)
LIBRARY = None
# Per-run bindings: run_script() sets these for the duration of the script, so several sessions in one
# process (tests, evals, future multi-project server) never see each other's imports/ or library/.
# ContextVars survive `asyncio.to_thread` and a script's own `from build123d import *`.
_CTX_WORKSPACE: ContextVar[Path | None] = ContextVar("agenticcad_workspace", default=None)
_CTX_LIBRARY: ContextVar[Any] = ContextVar("agenticcad_library", default=None)


def import_step(name: str) -> Shape:
    """Load a STEP file (from workspace/imports/, or an absolute path). Returns the shape (all solids)."""
    workspace = _CTX_WORKSPACE.get() or WORKSPACE
    path = Path(name)
    if not path.is_absolute():
        cands = [workspace / "imports" / name if workspace else None, Path(name)]
        for c in cands:
            if c is not None and c.exists():
                path = c
                break
    if not path.exists():
        raise CadError(f"STEP file not found: {name}" + (f" (looked in {workspace / 'imports'})" if workspace else ""))
    shape = _b3d_import_step(str(path))
    if isinstance(shape, Compound) and len(shape.solids()) == 1:
        return shape.solids()[0]
    return shape


def from_library(name: str, **params) -> Shape:
    """Instantiate a library part (script parts accept parameter overrides, e.g. from_library("m4 bolt", length=20))."""
    library = _CTX_LIBRARY.get() or LIBRARY
    if library is None:
        raise CadError("part library not available")
    return library.load_shape(name, **params)


# Scripts often start with `from build123d import *`, which would shadow the wrapper with build123d's own
# import_step; install the wrapper on the module so the star import hands out ours.
_b3d_import_step = b3d.import_step
b3d.import_step = import_step
try:
    import build123d.importers as _b3d_importers
    _b3d_importers.import_step = import_step
except Exception:
    pass


def script_namespace() -> dict[str, Any]:
    ns: dict[str, Any] = {"__name__": "__cad__"}
    exec("from build123d import *\nimport math\nfrom math import *\nimport build123d as b3d\n", ns)
    ns["import_step"] = import_step
    ns["from_library"] = from_library
    ns.update(thr.namespace())
    return ns


def run_script(code: str, quality: str = "normal", workspace: Path | None = None, library=None) -> Model:
    """Execute a build123d script. The script must assign `result`.
    `workspace` / `library` bind import_step() and from_library() for this run."""
    ns = script_namespace()
    buf = io.StringIO()
    tok_w = _CTX_WORKSPACE.set(workspace or _CTX_WORKSPACE.get() or WORKSPACE)
    tok_l = _CTX_LIBRARY.set(library or _CTX_LIBRARY.get() or LIBRARY)
    tok_t = thr.begin_registry()
    registered: list[Any] = []
    try:
        with contextlib.redirect_stdout(buf):
            exec(compile(code, "model.py", "exec"), ns)
    except Exception:
        tb = traceback.format_exc()
        lines = [ln for ln in tb.splitlines() if "cad_kernel.py" not in ln]
        raise CadError("\n".join(lines) + ("\nstdout:\n" + buf.getvalue() if buf.getvalue() else ""))
    finally:
        registered = thr.end_registry(tok_t)
        _CTX_WORKSPACE.reset(tok_w); _CTX_LIBRARY.reset(tok_l)

    if "result" not in ns:
        raise CadError("Script must assign the final shape(s) to a variable named `result`."
                       + ("\nstdout:\n" + buf.getvalue() if buf.getvalue() else ""))
    pairs = coerce_bodies(ns["result"])          # [] = an empty design (result = {} / None): allowed, nothing to show yet
    model = build_model(pairs, code, quality)
    model.stdout = buf.getvalue()
    model.threads = [t for t in registered if not t.external]
    model.sketches = collect_sketches(ns)
    model.mesh["sketches"] = model.sketches
    model.mesh["threads"] = [{"size": t.size, "pitch": t.pitch, "at": list(t.at), "axis": list(t.axis), "depth": t.depth,
                              "through": t.through, "real": t.real, "label": t.label()} for t in model.threads]
    return model


def build_model(pairs: list[tuple[str, Shape]] | Shape, code: str = "", quality: str = "normal") -> Model:
    """Tessellate the exact B-rep for DISPLAY ONLY. Bodies keep their exact shapes for export."""
    if isinstance(pairs, Shape):
        pairs = coerce_bodies(pairs)
    warnings: list[str] = []
    lin_frac, ang = QUALITY.get(quality, QUALITY["normal"])

    # global bbox + metadata first (build123d's bounding_box() drops any existing triangulation)
    bodies: list[Body] = []
    infos: list[FaceInfo] = []
    face_lists: list[list[Face]] = []
    gmin = [math.inf] * 3
    gmax = [-math.inf] * 3
    edge_counter = 0
    for bid, (path, shape) in enumerate(pairs):
        try:
            if not shape.is_valid():
                warnings.append(f"{path}: shape.is_valid() is False")
        except Exception:
            pass
        bb = shape.bounding_box()
        bmin = (bb.min.X, bb.min.Y, bb.min.Z)
        bmax = (bb.max.X, bb.max.Y, bb.max.Z)
        gmin = [min(a, b) for a, b in zip(gmin, bmin)]
        gmax = [max(a, b) for a, b in zip(gmax, bmax)]
        try:
            vol = float(shape.volume)
        except Exception:
            vol = 0.0
        body = Body(id=bid, name=path.split("/")[-1], path=path, shape=shape, volume=vol,
                    bbox_min=bmin, bbox_max=bmax)
        faces = list(shape.faces())
        for face in faces:
            fid = len(infos)
            infos.append(face_info(fid, bid, body.name, face))
            body.face_ids.append(fid)
        body.edge_ids = list(range(edge_counter, edge_counter + len(shape.edges())))
        edge_counter += len(body.edge_ids)
        bodies.append(body)
        face_lists.append(faces)

    for b in bodies:
        if b.volume <= 1e-9 or not b.shape.faces():
            raise CadError(f"body '{b.path}' has no volume (the last operation produced an empty or invalid solid; "
                           f"check that the face/edge it used was the right one and that the shape is not degenerate)")
    if not bodies:                                   # empty design: finite bbox so the viewer has something to frame
        gmin, gmax = [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]
    diag = math.dist(gmin, gmax) or 1.0
    lin = max(diag * lin_frac, 0.002)
    total_volume = sum(b.volume for b in bodies)

    body_payloads = []
    for body, faces in zip(bodies, face_lists):
        payload = tessellate_body(body, faces, lin, ang, warnings)
        body_payloads.append(payload)

    tree = build_tree(bodies)
    mesh = {
        "bodies": body_payloads,
        "tree": tree,
        "faces": [fi.to_dict() for fi in infos],
        "bboxMin": list(gmin), "bboxMax": list(gmax),
        "quality": quality,
        "exact": True,   # display mesh derived from an exact B-rep; export STEP for the true geometry
    }
    return Model(bodies=bodies, code=code, faces=infos, mesh=mesh,
                 bbox_min=tuple(gmin), bbox_max=tuple(gmax), volume=total_volume, warnings=warnings)


def collect_sketches(ns: dict[str, Any]) -> list[dict[str, Any]]:
    """Top-level variables holding a build123d Sketch (drawn in the UI or written by the agent)."""
    out = []
    for name, obj in list(ns.items()):
        if name.startswith("_") or not isinstance(obj, b3d.Sketch):
            continue
        try:
            faces = obj.faces()
            edges = [edge_polyline(e, 0.02, 0.1) for e in obj.edges()]
            bb = obj.bounding_box()
            try:
                f0 = faces[0]
                c = f0.center(); n = f0.normal_at(c)
                origin, normal = (c.X, c.Y, c.Z), (n.X, n.Y, n.Z)
            except Exception:
                origin, normal = (bb.center().X, bb.center().Y, bb.center().Z), (0.0, 0.0, 1.0)
            out.append({"name": name, "faces": len(faces), "area": round(float(sum(f.area for f in faces)), 3),
                        "edges": edges, "origin": [round(v, 4) for v in origin], "normal": [round(v, 4) for v in normal],
                        "bboxMin": [bb.min.X, bb.min.Y, bb.min.Z], "bboxMax": [bb.max.X, bb.max.Y, bb.max.Z]})
        except Exception:
            continue
    return out


def build_tree(bodies: list[Body]) -> list[dict[str, Any]]:
    """Fusion-style browser nodes: components (from path prefixes) and bodies (leaves)."""
    nodes: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    for b in bodies:
        parts = b.path.split("/")
        parent = ""
        for i, seg in enumerate(parts[:-1]):
            p = "/".join(parts[: i + 1])
            if p not in seen:
                seen[p] = p
                nodes.append({"id": "c:" + p, "kind": "component", "name": seg, "path": p,
                              "parent": ("c:" + parent) if parent else None})
            parent = p
        nodes.append({"id": "b:%d" % b.id, "kind": "body", "body": b.id, "name": b.name, "path": b.path,
                      "parent": ("c:" + parent) if parent else None,
                      "volume": round(b.volume, 2), "faces": len(b.face_ids)})
    return nodes


def tessellate_body(body: Body, faces: list[Face], lin: float, ang: float, warnings: list[str]) -> dict[str, Any]:
    shape = body.shape
    BRepTools.Clean_s(shape.wrapped)
    BRepMesh_IncrementalMesh(shape.wrapped, lin, False, ang, True)

    positions: list[float] = []
    normals: list[float] = []
    indices: list[int] = []
    face_ranges: list[list[int]] = []  # [faceId, indexStart, indexCount]

    for fid, face in zip(body.face_ids, faces):
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face.wrapped, loc)
        if tri is None:
            warnings.append(f"face#{fid} has no triangulation")
            continue
        try:
            BRepLib_ToolTriangulatedShape.ComputeNormals_s(face.wrapped, tri)
            has_n = tri.HasNormals()
        except Exception:
            has_n = False
        trsf = loc.Transformation()
        identity = loc.IsIdentity()
        reversed_ = face.wrapped.Orientation() == TopAbs_REVERSED
        sgn = -1.0 if reversed_ else 1.0
        base = len(positions) // 3
        for i in range(1, tri.NbNodes() + 1):
            p = tri.Node(i)
            if not identity:
                p = p.Transformed(trsf)
            positions.extend((round(p.X(), 5), round(p.Y(), 5), round(p.Z(), 5)))
            if has_n:
                n = tri.Normal(i)
                if not identity:
                    n = n.Transformed(trsf)
                normals.extend((round(sgn * n.X(), 4), round(sgn * n.Y(), 4), round(sgn * n.Z(), 4)))
            else:
                normals.extend((0.0, 0.0, 0.0))
        start = len(indices)
        for i in range(1, tri.NbTriangles() + 1):
            a, b_, c = tri.Triangle(i).Get()
            if reversed_:
                b_, c = c, b_
            indices.extend((base + a - 1, base + b_ - 1, base + c - 1))
        face_ranges.append([fid, start, len(indices) - start])

    edges: list[list[float]] = []
    edge_info: list[dict[str, Any]] = []
    for eid, edge in zip(body.edge_ids, shape.edges()):
        try:
            pl = edge_polyline(edge, lin, ang)
        except Exception:
            pl = []
        edges.append(pl)
        edge_info.append(edge_meta(eid, edge))

    return {
        "id": body.id, "name": body.name, "path": body.path,
        "positions": positions, "normals": normals, "indices": indices,
        "faceRanges": face_ranges, "edges": edges, "edgeInfo": edge_info,
        "bboxMin": list(body.bbox_min), "bboxMax": list(body.bbox_max),
    }


def edge_meta(eid: int, edge: Edge) -> dict[str, Any]:
    """Compact description of an edge for the viewer's measure tool."""
    kind = edge.geom_type.name if hasattr(edge.geom_type, "name") else str(edge.geom_type)
    r3 = lambda v: [round(v.X, 4), round(v.Y, 4), round(v.Z, 4)]
    info: dict[str, Any] = {"id": eid, "kind": kind, "length": round(float(edge.length), 4)}
    try:
        info["p0"] = r3(edge.start_point()); info["p1"] = r3(edge.end_point())
    except Exception:
        pass
    if kind == "LINE":
        try:
            info["dir"] = r3(edge.tangent_at(0))
        except Exception:
            pass
    elif kind == "CIRCLE":
        try:
            info["center"] = r3(edge.arc_center); info["radius"] = round(float(edge.radius), 4)
            info["closed"] = bool(edge.is_closed)
            n = edge.normal() if hasattr(edge, "normal") else None
            if n is not None:
                info["axis"] = r3(n)
            if not edge.is_closed:
                info["sweep"] = round(math.degrees(float(edge.length) / float(edge.radius)), 2)
        except Exception:
            pass
    return info


def face_info(fid: int, bid: int, body_name: str, face: Face) -> FaceInfo:
    kind = face.geom_type.name if hasattr(face.geom_type, "name") else str(face.geom_type)
    try:
        c = face.center()
        center = (c.X, c.Y, c.Z)
    except Exception:
        center = (0.0, 0.0, 0.0)
    normal = None
    if kind == "PLANE":
        try:
            n = face.normal_at()
            normal = (n.X, n.Y, n.Z)
        except Exception:
            pass
    radius = None
    if kind in ("CYLINDER", "SPHERE"):
        try:
            ad = BRepAdaptor_Surface(face.wrapped)
            radius = ad.Cylinder().Radius() if kind == "CYLINDER" else ad.Sphere().Radius()
        except Exception:
            pass
    bb = face.bounding_box()
    return FaceInfo(
        id=fid, body=bid, body_name=body_name, kind=kind, area=float(face.area), center=center, normal=normal,
        bbox_min=(bb.min.X, bb.min.Y, bb.min.Z), bbox_max=(bb.max.X, bb.max.Y, bb.max.Z),
        radius=radius, edge_count=len(face.edges()),
    )


def edge_polyline(edge: Edge, lin: float, ang: float) -> list[float]:
    """Sample the exact curve adaptively (angular + curvature deflection)."""
    curve = BRepAdaptor_Curve(edge.wrapped)
    disc = GCPnts_TangentialDeflection(curve, ang, lin, 2)
    pts: list[float] = []
    for i in range(1, disc.NbPoints() + 1):
        p = disc.Value(i)
        pts.extend((round(p.X(), 5), round(p.Y(), 5), round(p.Z(), 5)))
    return pts


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def export(model: Model, out_dir: Path, name: str, formats: list[str],
           tolerance: float = 0.01, angular_tolerance: float = 0.05,
           body: str | None = None) -> list[Path]:
    """STEP is exact B-rep (all bodies as separate solids). STL is tessellated here with the given
    chordal (mm) and angular (rad) deviation — independent of the display mesh.
    `body` limits the export to one body (by name or path)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    if not model.bodies:
        raise CadError("nothing to export: the design is empty")
    shape = model.shape
    if body:
        b = model.body_by_name(body)
        if b is None:
            raise CadError(f"no body named '{body}'. Bodies: {[x.path for x in model.bodies]}")
        shape = b.shape
        name = f"{name}_{b.name}".replace(" ", "_")
    written: list[Path] = []
    for f in formats:
        f = f.lower().lstrip(".")
        path = out_dir / f"{name}.{f}"
        if f in ("step", "stp"):
            # build123d's XCAF writer refuses a bare Solid; a Compound wrapper always works
            b3d.export_step(shape if isinstance(shape, Compound) else Compound(children=[shape]), str(path))
        elif f == "stl":
            export_stl(shape, path, tolerance, angular_tolerance)
        else:
            raise CadError(f"unsupported export format: {f}")
        written.append(path)
    return written


def export_stl(shape: Shape, path: Path, tolerance: float, angular_tolerance: float, ascii: bool = False) -> None:
    """Re-mesh the exact B-rep at the requested chordal/angular deviation and write binary STL.
    Any existing (display) triangulation is discarded first so the request is honoured."""
    BRepTools.Clean_s(shape.wrapped)
    BRepMesh_IncrementalMesh(shape.wrapped, tolerance, False, angular_tolerance, True)
    writer = StlAPI_Writer()
    writer.ASCIIMode = ascii
    if not writer.Write(shape.wrapped, str(path)):
        raise CadError(f"STL write failed: {path}")
    BRepTools.Clean_s(shape.wrapped)


DEFAULT_CODE = '''# Demo: a bracket with a separate pin — two bodies in a component tree.
plate_l, plate_w, plate_t = 60, 40, 8
boss_d, boss_h, bore_d = 24, 20, 6

with BuildPart() as bracket:
    Box(plate_l, plate_w, plate_t)
    with Locations((0, 0, plate_t / 2)):
        Cylinder(boss_d / 2, boss_h, align=(Align.CENTER, Align.CENTER, Align.MIN))
    Hole(bore_d / 2)                          # Hole() takes a radius
    with Locations((-22, -12), (22, -12), (-22, 12), (22, 12)):
        Hole(1.6)                                 # Ø3.2 mounting holes
    fillet(bracket.edges().filter_by(Axis.Z), 4)

pin = Pos(0, 0, plate_t / 2 + boss_h + 2) * Cylinder(bore_d / 2 - 0.1, 30)   # separate body, sits above the bore

result = {"Bracket": bracket.part, "Pin": pin}
'''

NEW_DESIGN_CODE = '''# New design — describe a part in the chat, use the ribbon, or write build123d here.
# Assign the final shape(s) to `result`:
#   one body:         result = part
#   several bodies:   result = {"Base": base, "Lid": lid}
#   components:       result = {"Assembly": {"Base": base, "Lid": lid}, "Screw": screw}
result = {}
'''


# ---------------------------------------------------------------------------
# Measurement / inspection
# ---------------------------------------------------------------------------
def _v(p) -> Vector:
    return Vector(float(p[0]), float(p[1]), float(p[2]))


def _ang(a: Vector, b: Vector) -> float:
    d = max(-1.0, min(1.0, a.normalized().dot(b.normalized())))
    return math.degrees(math.acos(d))


def entity_info(model: Model, ent: dict[str, Any]) -> tuple[dict[str, Any], Any]:
    """(description, build123d shape/point) for a measure entity {type, id | p}."""
    t = ent.get("type")
    if t == "face":
        fi = {f.id: f for f in model.faces}.get(int(ent["id"]))
        if fi is None:
            raise CadError(f"no face #{ent['id']} (ids change on every rebuild; inspect_model lists current ones)")
        face = model.get_face(fi.id)
        d = {**fi.to_dict(), "type": "face", "body": fi.body_name}
        return d, face
    if t == "edge":
        edge = model.get_edge(int(ent["id"]))
        d = edge_meta(int(ent["id"]), edge); d["type"] = "edge"
        for b in model.bodies:
            if int(ent["id"]) in b.edge_ids:
                d["body"] = b.path
        return d, edge
    if t in ("vertex", "point"):
        p = _v(ent["p"])
        return {"type": t, "p": [round(p.X, 4), round(p.Y, 4), round(p.Z, 4)]}, b3d.Vertex(p.X, p.Y, p.Z)
    raise CadError(f"unknown measure entity {t}")


def _direction(desc: dict[str, Any], shape) -> Vector | None:
    """A representative direction: line edge -> tangent; planar face -> normal; circle -> axis."""
    if desc["type"] == "edge":
        if desc.get("kind") == "LINE":
            return _v(desc["dir"]) if "dir" in desc else None
        if desc.get("kind") == "CIRCLE" and "axis" in desc:
            return _v(desc["axis"])
    if desc["type"] == "face" and desc.get("normal"):
        return _v(desc["normal"])
    return None


def measure_entities(model: Model, entities: list[dict[str, Any]]) -> dict[str, Any]:
    """Generic measure: one entity -> its properties; two -> min distance with closest points, angle and relation."""
    out: dict[str, Any] = {"items": []}
    shapes = []
    for ent in entities[:2]:
        d, shp = entity_info(model, ent)
        out["items"].append(d); shapes.append(shp)
    if len(shapes) == 2:
        a, b = shapes
        da, db = out["items"]
        try:
            dist, pa, pb = a.distance_to_with_closest_points(b)
            out["distance"] = round(float(dist), 4)
            out["closest"] = [[round(pa.X, 4), round(pa.Y, 4), round(pa.Z, 4)], [round(pb.X, 4), round(pb.Y, 4), round(pb.Z, 4)]]
            out["delta"] = [round(pb.X - pa.X, 4), round(pb.Y - pa.Y, 4), round(pb.Z - pa.Z, 4)]
        except Exception as e:  # noqa: BLE001
            out["distance_error"] = str(e)
        # angle between representative directions
        va, vb = _direction(da, a), _direction(db, b)
        kinds = (da["type"] + ("/" + da.get("kind", "") if da["type"] == "edge" else ""),
                 db["type"] + ("/" + db.get("kind", "") if db["type"] == "edge" else ""))
        if va is not None and vb is not None:
            ang = _ang(va, vb)
            line_a = da["type"] == "edge" and da.get("kind") == "LINE"
            line_b = db["type"] == "edge" and db.get("kind") == "LINE"
            plane_a = da["type"] == "face"; plane_b = db["type"] == "face"
            if (line_a and plane_b) or (plane_a and line_b):
                # line vs plane: angle between the line and the plane surface
                ang = abs(90.0 - ang)
                out["angle_kind"] = "line-to-plane"
            elif plane_a and plane_b:
                out["angle_kind"] = "plane-to-plane (between normals)"
            elif line_a and line_b:
                out["angle_kind"] = "line-to-line"
            else:
                out["angle_kind"] = "between axes/normals"
            ang = round(ang, 3)
            out["angle"] = ang
            out["angle_supplement"] = round(180.0 - ang, 3)
            rel = "none"
            if ang < 0.01 or abs(ang - 180) < 0.01:
                rel = "parallel"
            elif abs(ang - 90) < 0.01:
                rel = "perpendicular"
            out["relation"] = rel
            if line_a and line_b and rel != "parallel" and out.get("distance", 1) > 1e-6:
                out["relation"] = "skew (not intersecting)" if rel == "none" else rel + ", not intersecting"
        # circle-specific: centre spacing (hole spacing), concentric check
        if da.get("kind") == "CIRCLE" and db.get("kind") == "CIRCLE" and "center" in da and "center" in db:
            ca, cb = _v(da["center"]), _v(db["center"])
            out["center_distance"] = round((cb - ca).length, 4)
            out["center_delta"] = [round(cb.X - ca.X, 4), round(cb.Y - ca.Y, 4), round(cb.Z - ca.Z, 4)]
            if (cb - ca).length < 1e-6:
                out["relation"] = "concentric"
        elif da.get("kind") == "CIRCLE" and "center" in da and db["type"] in ("vertex", "point"):
            out["center_distance"] = round((_v(db["p"]) - _v(da["center"])).length, 4)
        elif db.get("kind") == "CIRCLE" and "center" in db and da["type"] in ("vertex", "point"):
            out["center_distance"] = round((_v(da["p"]) - _v(db["center"])).length, 4)
        # two planar faces: gap between the planes if parallel
        if da["type"] == "face" and db["type"] == "face" and da.get("normal") and db.get("normal") and out.get("relation") == "parallel":
            n = _v(da["normal"]); out["plane_gap"] = round(abs((_v(db["center"]) - _v(da["center"])).dot(n)), 4)
        # points: straight distance already; add per-axis for readability
        if da["type"] in ("vertex", "point") and db["type"] in ("vertex", "point"):
            out["relation"] = "point-to-point"
    return out


def measure(model: Model, faces: list[int] | None = None, points: list[list[float]] | None = None,
            bodies: list[int] | None = None, edges: list[int] | None = None,
            entities: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Distances / angles between up to two entities (faces / edges / vertices / points), plus body info."""
    ents = list(entities or [])
    if not ents:
        ents = [{"type": "face", "id": f} for f in (faces or [])] + [{"type": "edge", "id": e} for e in (edges or [])] \
             + [{"type": "point", "p": p} for p in (points or [])]
    out: dict[str, Any] = measure_entities(model, ents) if ents else {}
    bodies = bodies or []
    for bid in bodies[:2]:
        for b_ in model.bodies:
            if b_.id == bid:
                out.setdefault("bodies", []).append(mass_properties(model, b_.path))
    if len(bodies) == 2:
        out["clearance"] = clearance(model, bodies[0], bodies[1])
    return out


def _measure_legacy(model: Model, faces: list[int] | None = None, points: list[list[float]] | None = None,
            bodies: list[int] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    faces = faces or []
    points = points or []
    bodies = bodies or []
    if len(points) == 2:
        (x0, y0, z0), (x1, y1, z1) = points[0][:3], points[1][:3]
        d = (x1 - x0, y1 - y0, z1 - z0)
        out["points"] = {"distance": round(math.sqrt(sum(v * v for v in d)), 4),
                         "dx": round(d[0], 4), "dy": round(d[1], 4), "dz": round(d[2], 4)}
    infos = {f.id: f for f in model.faces}
    fshapes = []
    for fid in faces[:2]:
        if fid in infos:
            fi = infos[fid]
            fshapes.append(model.get_face(fid))
            out.setdefault("faces", []).append({**fi.to_dict(), "body": fi.body_name, "perimeter": round(sum(e.length for e in model.get_face(fid).edges()), 3)})
    if len(fshapes) == 2:
        a, b = fshapes
        try:
            out["face_distance"] = round(float(a.distance(b)), 4)
        except Exception:
            pass
        na = infos[faces[0]].normal; nb = infos[faces[1]].normal
        ca = infos[faces[0]].center; cb = infos[faces[1]].center
        out["center_distance"] = round(math.dist(ca, cb), 4)
        out["center_delta"] = [round(q - p_, 4) for p_, q in zip(ca, cb)]
        if na and nb:
            dot = max(-1.0, min(1.0, sum(p_ * q for p_, q in zip(na, nb))))
            out["angle_between_normals"] = round(math.degrees(math.acos(dot)), 3)
            # for parallel planes: the normal gap
            if abs(abs(dot) - 1) < 1e-6:
                out["plane_gap"] = round(abs(sum((q - p_) * n for p_, q, n in zip(ca, cb, na))), 4)
        ra, rb = infos[faces[0]].radius, infos[faces[1]].radius
        if ra and rb and infos[faces[0]].kind == "CYLINDER" and infos[faces[1]].kind == "CYLINDER":
            out["axis_distance_xy"] = round(math.hypot(cb[0] - ca[0], cb[1] - ca[1]), 4)
    for bid in bodies[:2]:
        for b_ in model.bodies:
            if b_.id == bid:
                out.setdefault("bodies", []).append(mass_properties(model, b_.path))
    if len(bodies) == 2:
        out["clearance"] = clearance(model, bodies[0], bodies[1])
    return out


DENSITIES = {"aluminium": 2.70, "steel": 7.85, "stainless": 7.9, "brass": 8.5, "copper": 8.96, "titanium": 4.43,
             "pla": 1.24, "petg": 1.27, "abs": 1.04, "nylon": 1.15, "acrylic": 1.18, "hdpe": 0.95, "delrin": 1.41,
             "plywood": 0.6, "mdf": 0.75, "hardwood": 0.7, "softwood": 0.5, "foam": 0.03}


def mass_properties(model: Model, body: str | None = None, density: float | str = "aluminium") -> dict[str, Any]:
    """Volume, mass (density g/cm³ or a material name), centre of mass, bbox, surface area."""
    if isinstance(density, str):
        key = density.lower()
        if key not in DENSITIES:
            raise CadError(f"unknown material '{density}'; known: {', '.join(DENSITIES)} (or give g/cm³)")
        rho = DENSITIES[key]
    else:
        rho = float(density)
    if body:
        b_ = model.body_by_name(body)
        if b_ is None:
            raise CadError(f"no body '{body}'")
        shape, label = b_.shape, b_.path
    else:
        shape, label = model.shape, "all bodies"
    vol = float(shape.volume)
    c = shape.center(b3d.CenterOf.MASS)
    bb = shape.bounding_box()
    area = float(sum(f.area for f in shape.faces()))
    return {"body": label, "volume_mm3": round(vol, 2), "mass_g": round(vol / 1000 * rho, 2), "density_g_cm3": rho,
            "center_of_mass": [round(c.X, 3), round(c.Y, 3), round(c.Z, 3)],
            "bbox_size": [round(bb.size.X, 3), round(bb.size.Y, 3), round(bb.size.Z, 3)],
            "bbox_min": [round(bb.min.X, 3), round(bb.min.Y, 3), round(bb.min.Z, 3)],
            "surface_area_mm2": round(area, 1)}


def clearance(model: Model, a: str | int, b: str | int) -> dict[str, Any]:
    """Minimum distance between two bodies, or their interference volume if they overlap."""
    def find(x):
        for b_ in model.bodies:
            if b_.id == x or b_.name == x or b_.path == x:
                return b_
        raise CadError(f"no body '{x}'")
    ba, bb_ = find(a), find(b)
    d = float(ba.shape.distance(bb_.shape))
    res = {"a": ba.path, "b": bb_.path, "min_distance": round(d, 4)}
    if d < 1e-6:
        try:
            inter = ba.shape.intersect(bb_.shape)
            items = inter if isinstance(inter, list) else [inter]
            vol = sum(float(getattr(i, "volume", 0.0)) for i in items)
            res["interference_volume_mm3"] = round(vol, 3)
            res["status"] = "INTERFERING" if vol > 1e-6 else "touching"
        except Exception:
            res["status"] = "touching/interfering"
    else:
        res["status"] = "clear"
    return res
