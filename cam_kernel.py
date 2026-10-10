"""
CAM kernel for AgenticCAD — 2.5D / simple 3D toolpaths from the exact B-rep, GRBL post-processor.

Everything is in model coordinates (mm, Z up). The post subtracts the WCS origin.
The agent writes a CAM script (see agent.py CAM_PROMPT) that builds a `Program`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable

import numpy as np
from shapely.geometry import Polygon, MultiPolygon, LineString, Point, box as shp_box
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

import build123d as b3d
from build123d import Shape, Plane, Face, Vector
from OCP.BRepAdaptor import BRepAdaptor_Surface, BRepAdaptor_Curve
from OCP.BRepTools import BRepTools_WireExplorer
from OCP.GCPnts import GCPnts_TangentialDeflection
from OCP.TopAbs import TopAbs_REVERSED

import cad_kernel as ck


class CamError(Exception):
    pass


# =============================================================================
# Library data: tools, machines
# =============================================================================
@dataclass
class Tool:
    number: int
    name: str
    type: str = "flat"          # flat | ball | vbit | drill | chamfer
    diameter: float = 6.0
    flutes: int = 2
    flute_length: float = 20.0
    rpm: float = 12000
    feed: float = 1000          # XY feed, mm/min
    plunge: float = 300         # Z feed, mm/min
    stepdown: float = 2.0       # default axial depth per pass, mm
    stepover: float = 0.5       # default radial stepover, fraction of diameter
    angle: float = 0.0          # included angle for vbit/chamfer/drill point
    notes: str = ""

    @property
    def radius(self) -> float:
        return self.diameter / 2

    def label(self) -> str:
        return f"T{self.number} {self.name}"


@dataclass
class Machine:
    name: str
    controller: str = "grbl"
    units: str = "mm"
    travel: dict = field(default_factory=lambda: {"x": 300.0, "y": 180.0, "z": 45.0})
    max_feed: dict = field(default_factory=lambda: {"x": 2000.0, "y": 2000.0, "z": 500.0})
    rapid: float = 2000.0                   # mm/min, used for time estimates (GRBL G0 uses its own max rate)
    spindle: dict = field(default_factory=lambda: {"min": 1000.0, "max": 10000.0})
    tool_change: str = "pause"              # pause (M0 + message) | none (single tool assumed) | split (one file per tool) | manual (Makera M6 Tn) | atc (automatic changer: Tn M06)
    coolant: bool = False
    safe_z: float = 5.0                     # clearance above stock top for rapids
    clearance_z: float = 15.0               # height for tool changes / start / end
    program_start: list[str] = field(default_factory=list)
    program_end: list[str] = field(default_factory=list)
    arcs: bool = True                       # emit G2/G3 where the path is circular
    arc_tolerance: float = 0.05             # max deviation (mm) between polyline and fitted arc
    notes: str = ""
    post: str = "grbl"                      # grbl | makera (Makera Z1 / Carvera family) | haas (Haas NGC/Classic) | linuxcnc (rs274ngc)
    # optional rotary 4th axis: {"axis": "A", "about": "x", "max_diameter": mm, "max_length": mm,
    #   "max_speed": deg/min, "installed": bool}. The rotary axis is parallel to machine X.
    rotary: dict | None = None
    collet: float = 0.0                     # largest tool shank the spindle takes (mm); 0 = unchecked
    # stepdown caps for the feeds calculator, mm per pass by material ("*" = any other material); {} = no cap
    max_stepdown: dict = field(default_factory=dict)
    # machine model overrides (Settings ▸ Machines ▸ Model): {"nose": [{name, r, h}...], "spoilboard": mm, "clearance": mm, "stickout": mm}
    model: dict | None = None

    def stepdown_cap(self, material: str) -> float | None:
        caps = {MATERIAL_ALIASES.get(str(k).lower().strip(), str(k).lower().strip()): v for k, v in self.max_stepdown.items()}
        v = caps.get(material, caps.get("*"))
        return float(v) if v else None


# =============================================================================
# Setup: stock + WCS origin
# =============================================================================
@dataclass
class Stock:
    xmin: float; xmax: float; ymin: float; ymax: float; zmin: float; zmax: float

    @classmethod
    def from_model(cls, model, margin: float = 2.0, top: float = 0.0, bottom: float = 0.0) -> "Stock":
        """Bounding box of the model (or any shape / body) plus XY margin; `top`/`bottom` add extra
        material above/below."""
        if isinstance(model, ck.Model):
            (x0, y0, z0), (x1, y1, z1) = model.bbox_min, model.bbox_max
        elif isinstance(model, ck.Body):
            (x0, y0, z0), (x1, y1, z1) = model.bbox_min, model.bbox_max
        elif isinstance(model, Shape):
            bb = model.bounding_box()
            (x0, y0, z0), (x1, y1, z1) = (bb.min.X, bb.min.Y, bb.min.Z), (bb.max.X, bb.max.Y, bb.max.Z)
        else:
            raise CamError("Stock.from_model expects the model, a body, or a shape")
        return cls(x0 - margin, x1 + margin, y0 - margin, y1 + margin, z0 - bottom, z1 + top)

    from_shape = from_model

    @classmethod
    def block(cls, size_x: float, size_y: float, size_z: float, center_xy=(0.0, 0.0), top: float | None = None,
              bottom: float | None = None) -> "Stock":
        """Explicit block. Give either `top` (z of stock top) or `bottom`; default bottom = 0."""
        if top is None and bottom is None:
            bottom = 0.0
        if top is None:
            top = bottom + size_z
        bottom = top - size_z
        cx, cy = center_xy
        return cls(cx - size_x / 2, cx + size_x / 2, cy - size_y / 2, cy + size_y / 2, bottom, top)

    @classmethod
    def cylinder(cls, diameter: float, length: float, x0: float = 0.0, axis: tuple[float, float] = (0.0, 0.0)) -> "Stock":
        """Round bar for the 4th axis: axis parallel to X through model (y, z) = `axis`, from x0 to x0 + length."""
        y0, z0 = axis
        r = diameter / 2
        st = cls(x0, x0 + length, y0 - r, y0 + r, z0 - r, z0 + r)
        st.radius = r
        return st

    @property
    def is_cylinder(self) -> bool:
        return getattr(self, "radius", None) is not None

    @property
    def size(self) -> tuple[float, float, float]:
        return (self.xmax - self.xmin, self.ymax - self.ymin, self.zmax - self.zmin)

    @property
    def top(self) -> float:
        return self.zmax

    @property
    def bottom(self) -> float:
        return self.zmin

    def rect(self, expand: float = 0.0) -> Polygon:
        return shp_box(self.xmin - expand, self.ymin - expand, self.xmax + expand, self.ymax + expand)

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.is_cylinder:
            d["radius"] = self.radius
        return d


ORIGINS = {
    "stock-top-left": lambda s: (s.xmin, s.ymin, s.zmax),      # front-left corner, stock top (most common on routers)
    "stock-top-center": lambda s: ((s.xmin + s.xmax) / 2, (s.ymin + s.ymax) / 2, s.zmax),
    "stock-bottom-left": lambda s: (s.xmin, s.ymin, s.zmin),
    "stock-bottom-center": lambda s: ((s.xmin + s.xmax) / 2, (s.ymin + s.ymax) / 2, s.zmin),
    "model-origin": lambda s: (0.0, 0.0, 0.0),
    # 4th axis: on the rotary centreline, at the stock's left end or middle (setup frame: axis = X, Y = Z = 0)
    "rotary-axis-left": lambda s: (s.xmin, 0.0, 0.0),
    "rotary-axis-center": lambda s: ((s.xmin + s.xmax) / 2, 0.0, 0.0),
}


def _rx(deg: float) -> np.ndarray:
    c, s_ = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return np.array([[1, 0, 0], [0, c, -s_], [0, s_, c]], dtype=float)


def _ry(deg: float) -> np.ndarray:
    c, s_ = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return np.array([[c, 0, s_], [0, 1, 0], [-s_, 0, c]], dtype=float)


def _rz(deg: float) -> np.ndarray:
    c, s_ = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return np.array([[c, -s_, 0], [s_, c, 0], [0, 0, 1]], dtype=float)


# which model face points up at the spindle in each orientation, as (rx, ry, rz) rotations applied X then Y then Z
ORIENTS = {"top": (0, 0, 0), "bottom": (180, 0, 0), "front": (-90, 0, 0), "back": (90, 0, 0),
           "left": (0, 90, 0), "right": (0, -90, 0)}


@dataclass
class Setup:
    """One way the part is held on the machine. `stock` is given in MODEL coordinates; the setup has its own
    frame in which the spindle axis is +Z (the part is turned so the machined side faces up).
      orient: "top" (as modelled) | "bottom" (flipped about X) | "front" | "back" | "left" | "right" (that model face
              up) | (rx, ry, rz) degrees, applied X then Y then Z.
      rotary=True: the part is on the 4th axis. The rotary axis is the model line parallel to X through `axis`
              (y, z) (default: stock centre); in the setup frame it is the X axis (Y = Z = 0). `a` is the A angle the
              ops of this setup are cut at (3+1 indexed); continuous ops (rotary_*) drive A themselves.
    Use setup.view(shape) to get geometry in the setup frame for section(), holes(), stock_minus() etc. Toolpath
    moves are in the setup frame; the viewer maps them back onto the part, the post subtracts the WCS origin."""
    machine: Machine
    stock: Stock
    origin: Any = None                      # one of ORIGINS or an (x, y, z) tuple in SETUP coords; default by kind
    safe_z: float | None = None             # absolute setup-frame Z for rapids; default stock top + machine.safe_z
    clearance_z: float | None = None
    name: str = "Setup 1"
    orient: Any = "top"
    rotary: bool = False
    axis: tuple[float, float] | None = None
    a: float = 0.0
    wcs: str | None = None                  # G54..G59; Program assigns one per setup when None
    # how the work is held: a fixture by name (fixture_lib: vises, clamps, plates), where it sits on the table
    # (fixture_at = (x, y) from the table centre, fixture_rot degrees about Z) and its parameters (opening, parallel, ...).
    # The stock's bottom-centre sits at the fixture's work origin; None = stock centred on the bare table/spoilboard.
    fixture: str | None = None
    fixture_at: tuple[float, float] = (0.0, 0.0)
    fixture_rot: float = 0.0
    fixture_params: dict | None = None

    def __post_init__(self):
        self.model_stock = self.stock
        fx = _FIXTURES["table"].get(self.name)        # fixtures({...}) line in cam.py (the CAM tab's picker) wins over the script
        if fx is not None:
            self.fixture = fx.get("name", self.fixture) or None
            self.fixture_at = tuple(fx.get("at", self.fixture_at))
            self.fixture_rot = float(fx.get("rot", self.fixture_rot))
            self.fixture_params = fx.get("params", self.fixture_params)
        self._fixture_model = None
        if self.fixture and self.rotary:
            raise CamError("a fixture can't be combined with a rotary setup (the chuck holds the work)")
        if self.fixture:
            self.fixture_model()                        # validate now: unknown fixture / bad params fail at setup time
        if self.rotary:
            if self.machine.rotary is None:
                raise CamError(f"{self.machine.name} has no 4th axis defined (machine.rotary); can't make a rotary setup")
            if self.axis is None:
                self.axis = ((self.stock.ymin + self.stock.ymax) / 2, (self.stock.zmin + self.stock.zmax) / 2)
            y0, z0 = self.axis
            corners = [(y - y0, z - z0) for y in (self.stock.ymin, self.stock.ymax) for z in (self.stock.zmin, self.stock.zmax)]
            r = self.stock.radius if self.stock.is_cylinder else max(math.hypot(*c) for c in corners)
            self.max_radius = r
            st = Stock(self.stock.xmin, self.stock.xmax, -r, r, -r, r)
            st.radius = r
            self.stock = st
            self.origin = self.origin or "rotary-axis-left"
            if self.safe_z is None:
                self.safe_z = r + self.machine.safe_z
            if self.clearance_z is None:
                self.clearance_z = r + self.machine.clearance_z
        else:
            rot = ORIENTS.get(self.orient, self.orient) if isinstance(self.orient, str) else self.orient
            if isinstance(self.orient, str) and self.orient not in ORIENTS:
                raise CamError(f"unknown orient '{self.orient}'; use one of {list(ORIENTS)} or (rx, ry, rz)")
            self._rot = tuple(float(v) for v in rot)
            pts = np.array([[x, y, z] for x in (self.stock.xmin, self.stock.xmax) for y in (self.stock.ymin, self.stock.ymax)
                            for z in (self.stock.zmin, self.stock.zmax)])
            q = np.round(pts @ self._R().T, 9)
            lo, hi = q.min(axis=0), q.max(axis=0)
            self.stock = Stock(*(float(v) + 0.0 for v in (lo[0], hi[0], lo[1], hi[1], lo[2], hi[2])))
            self.origin = self.origin or "stock-top-left"
            if self.safe_z is None:
                self.safe_z = self.stock.top + self.machine.safe_z
            if self.clearance_z is None:
                self.clearance_z = self.stock.top + self.machine.clearance_z

    # ---- fixture (how the work is held)
    def fixture_model(self):
        """The fixture_lib.FixtureModel holding this setup's stock (None when the stock sits on the bare table)."""
        if not self.fixture:
            return None
        if self._fixture_model is None:
            import fixture_lib
            ms = self.model_stock
            stock = {"x": ms.xmax - ms.xmin, "y": ms.ymax - ms.ymin, "z": ms.zmax - ms.zmin}
            try:
                self._fixture_model = fixture_lib.model_for(self.fixture, self.fixture_params or {}, stock)
            except fixture_lib.FixtureScriptError as e:
                raise CamError(f"fixture {self.fixture!r}: {e}") from e
        return self._fixture_model

    def fixture_height(self) -> float:
        """How far the stock bottom sits above the table (0 on the bare table)."""
        fm = self.fixture_model()
        return float(fm.height) if fm is not None else 0.0

    def fixture_payload(self) -> dict | None:
        fm = self.fixture_model()
        if fm is None:
            return None
        return {"name": fm.name, "at": [float(self.fixture_at[0]), float(self.fixture_at[1])], "rot": float(self.fixture_rot),
                "params": fm.params, "work_origin": list(fm.work_origin), "height": fm.height, "clamp_axis": fm.clamp_axis,
                "max_opening": fm.max_opening}

    # ---- transforms (model -> setup frame)
    def _R(self) -> np.ndarray:
        rx, ry, rz = self._rot
        return _rz(rz) @ _ry(ry) @ _rx(rx)

    def matrix(self, a: float | None = None) -> np.ndarray:
        """4x4 model -> setup frame (for rotary setups at A = a, default the setup's index angle)."""
        M = np.eye(4)
        if self.rotary:
            y0, z0 = self.axis
            T = np.eye(4); T[1, 3] = -y0; T[2, 3] = -z0
            M[:3, :3] = _rx(self.a if a is None else a)
            return M @ T
        M[:3, :3] = self._R()
        return M

    def to_setup(self, pts, a: float | None = None) -> np.ndarray:
        P = np.atleast_2d(np.asarray(pts, dtype=float))
        M = self.matrix(a)
        return P @ M[:3, :3].T + M[:3, 3]

    def to_model(self, pts, a: float | None = None) -> np.ndarray:
        """Setup-frame points (at A = a for rotary setups) back to model coordinates."""
        P = np.atleast_2d(np.asarray(pts, dtype=float))
        M = np.linalg.inv(self.matrix(a))
        return P @ M[:3, :3].T + M[:3, 3]

    def view(self, shape, a: float | None = None):
        """The shape as the machine sees it in this setup (setup frame), for section/holes/stock_minus/parallel3d."""
        if self.rotary:
            y0, z0 = self.axis
            return b3d.Rot(self.a if a is None else a, 0, 0) * (b3d.Pos(0, -y0, -z0) * shape)
        rx, ry, rz = self._rot
        return b3d.Rot(0, 0, rz) * (b3d.Rot(0, ry, 0) * (b3d.Rot(rx, 0, 0) * shape))

    def origin_point(self) -> tuple[float, float, float]:
        if isinstance(self.origin, str):
            if self.origin not in ORIGINS:
                raise CamError(f"unknown origin '{self.origin}'; use one of {list(ORIGINS)} or an (x, y, z) tuple")
            return ORIGINS[self.origin](self.stock)
        x, y, z = self.origin
        return (float(x), float(y), float(z))

    @property
    def tool_axis_model(self) -> tuple[float, float, float]:
        """Direction from the part towards the spindle, in model coordinates (setup +Z), at the index angle."""
        d = self.to_model([[0, 0, 1]]) - self.to_model([[0, 0, 0]])
        return tuple(float(v) for v in d[0])

    def describe(self) -> str:
        kind = (f"4th axis, axis through model (y, z) = ({self.axis[0]:.2f}, {self.axis[1]:.2f}), A {self.a:g}°"
                if self.rotary else f"orient {self.orient}")
        return f"{self.name}: {kind}, WCS {self.wcs or '?'}, origin {self.origin}"


# =============================================================================
# Toolpath primitives
# =============================================================================
RAPID, FEED, PLUNGE = 0, 1, 2


@dataclass
class Op:
    name: str
    kind: str
    tool: Tool
    moves: list[list[float]] = field(default_factory=list)   # [kind, x, y, z, feed] or [kind, x, y, z, feed, a] (setup frame)
    color: str = "#4ea1ff"
    params: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    setup: Any = None                                         # the Setup the moves are in (set by PathBuilder)
    key: str = ""                                             # stable id for per-operation overrides ("Pocket", "Pocket#2")
    base_tool: Any = None                                     # the tool as the script passed it (before overrides)
    overrides: dict = field(default_factory=dict)             # applied per-op overrides {rpm, feed, plunge, stepdown, stepover}
    defaults: dict = field(default_factory=dict)              # what those values are without the overrides

    @property
    def uses_a(self) -> bool:
        return any(len(m) > 5 for m in self.moves)

    def _seg(self, prev, m) -> float:
        """Path length of one move: XYZ distance, plus the arc length swept by A at the tool's radius from the axis."""
        d = math.dist(prev[1:4], m[1:4])
        if len(m) > 5 and len(prev) > 5 and m[5] != prev[5]:
            r = max(math.hypot(m[2], m[3]), math.hypot(prev[2], prev[3]))
            d = math.hypot(d, math.radians(abs(m[5] - prev[5])) * r)
        return d

    def lengths(self) -> tuple[float, float]:
        cut = rapid = 0.0
        prev = None
        for m in self.moves:
            if prev is not None:
                d = self._seg(prev, m)
                if m[0] == RAPID:
                    rapid += d
                else:
                    cut += d
            prev = m
        return cut, rapid

    def time_minutes(self, machine: Machine) -> float:
        t = 0.0
        prev = None
        for m in self.moves:
            if prev is not None:
                d = self._seg(prev, m)
                f = machine.rapid if m[0] == RAPID else max(m[4], 1.0)
                t += d / f
            prev = m
        return t

    def bounds(self):
        xs = [m[1] for m in self.moves]; ys = [m[2] for m in self.moves]; zs = [m[3] for m in self.moves]
        return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


class PathBuilder:
    """Accumulates moves for one operation with a safe-Z discipline."""

    def __init__(self, setup: Setup, tool: Tool, op: Op):
        self.setup, self.tool, self.op = setup, tool, op
        op.setup = setup
        if setup.machine.collet and tool.diameter > setup.machine.collet + 1e-6 and tool.type in ("flat", "ball", "drill"):
            op.warnings.append(f"{tool.label()} Ø{tool.diameter} is larger than the {setup.machine.collet:g} mm collet: "
                               "check the shank fits (reduced-shank tool?)")
        self.pos: tuple[float, float, float] | None = None
        mf = setup.machine.max_feed
        self.feed = min(tool.feed, mf.get("x", tool.feed), mf.get("y", tool.feed))
        self.plunge = min(tool.plunge, mf.get("z", tool.plunge))
        if self.feed < tool.feed or self.plunge < tool.plunge:
            op.warnings.append(f"feed clamped to machine max ({self.feed:.0f}/{self.plunge:.0f} mm/min)")

    def _add(self, kind, x, y, z, f=0.0, a=None):
        mv = [kind, round(x, 4), round(y, 4), round(z, 4), round(f, 1)]
        if a is not None:
            mv.append(round(a, 4))
        self.op.moves.append(mv)
        self.pos = (x, y, z)

    def retract(self):
        if self.pos is not None and self.pos[2] < self.setup.safe_z - 1e-6:
            self._add(RAPID, self.pos[0], self.pos[1], self.setup.safe_z)

    def rapid_xy(self, x, y):
        """Rapid at safe Z to (x, y)."""
        self.retract()
        self._add(RAPID, x, y, self.setup.safe_z)

    def plunge_to(self, z, rapid_to: float | None = None):
        """Rapid down to just above `rapid_to` (or stock top), then plunge-feed to z."""
        x, y = self.pos[0], self.pos[1]
        approach = (rapid_to if rapid_to is not None else self.setup.stock.top) + 1.0
        if self.pos[2] > approach and z < approach:
            self._add(RAPID, x, y, approach)
        self._add(PLUNGE, x, y, z, self.plunge)

    def feed_to(self, x, y, z=None, f: float | None = None):
        z = self.pos[2] if z is None else z
        self._add(FEED, x, y, z, f or self.feed)

    def feed_path(self, pts: Iterable[tuple[float, float, float]]):
        for p in pts:
            self._add(FEED, p[0], p[1], p[2], self.feed)


# =============================================================================
# Geometry helpers (from the exact B-rep)
# =============================================================================
def _edge_points(edge_wrapped, tol: float = 0.02, ang: float = 0.1) -> list[tuple[float, float, float]]:
    curve = BRepAdaptor_Curve(edge_wrapped)
    disc = GCPnts_TangentialDeflection(curve, ang, tol, 2)
    return [(disc.Value(i).X(), disc.Value(i).Y(), disc.Value(i).Z()) for i in range(1, disc.NbPoints() + 1)]


def wire_points(wire, tol: float = 0.02) -> list[tuple[float, float]]:
    """Ordered XY points around a wire (respecting edge orientation)."""
    pts: list[tuple[float, float]] = []
    ex = BRepTools_WireExplorer(wire.wrapped)
    while ex.More():
        e = ex.Current()
        p = _edge_points(e, tol)
        if ex.Orientation() == TopAbs_REVERSED:
            p = p[::-1]
        for q in p:
            xy = (round(q[0], 5), round(q[1], 5))
            if not pts or pts[-1] != xy:
                pts.append(xy)
        ex.Next()
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts.pop()
    return pts


def face_polygon(face: Face, tol: float = 0.02) -> Polygon:
    """Planar-ish face -> shapely polygon (XY projection) with holes."""
    outer = wire_points(face.outer_wire(), tol)
    holes = [wire_points(w, tol) for w in face.inner_wires()]
    poly = Polygon(outer, [h for h in holes if len(h) >= 3])
    if not poly.is_valid:
        poly = poly.buffer(0)
    return poly


def section(shape: Shape, z: float, tol: float = 0.02) -> MultiPolygon:
    """Cross-section of the shape at height z (XY polygons with holes). Empty if nothing there."""
    try:
        sk = b3d.section(shape, section_by=Plane.XY.offset(z))
    except Exception as e:  # noqa: BLE001
        raise CamError(f"section at z={z} failed: {e}")
    polys = [face_polygon(f, tol) for f in sk.faces()] if sk is not None else []
    u = unary_union([p for p in polys if not p.is_empty])
    return _as_multi(u)


def silhouette(shape: Shape, levels: int = 12, tol: float = 0.02) -> MultiPolygon:
    """Top-down outline: union of sections at several heights."""
    bb = shape.bounding_box()
    zs = [bb.min.Z + (bb.max.Z - bb.min.Z) * (i + 0.5) / levels for i in range(levels)]
    return _as_multi(unary_union([section(shape, z, tol) for z in zs]))


def stock_minus(setup: Setup, shape: Shape, z: float, expand: float = 0.0, tol: float = 0.02) -> MultiPolygon:
    """Material to remove at level z: stock rectangle minus the part's section (2.5D roughing regions).
    `expand` grows the stock rectangle outward (use ≥ tool diameter when roughing around a part so the
    tool can run off the stock edge)."""
    return _as_multi(setup.stock.rect(expand).difference(section(shape, z, tol)))


@dataclass
class Hole:
    x: float; y: float; diameter: float; z_top: float; z_bottom: float; through: bool

    @property
    def depth(self) -> float:
        return self.z_top - self.z_bottom

    def __repr__(self):
        return f"Hole(x={self.x:.2f}, y={self.y:.2f}, d={self.diameter:.2f}, z {self.z_bottom:.2f}..{self.z_top:.2f}{', through' if self.through else ''})"


def holes(shape: Shape, dmin: float = 0.0, dmax: float = 1e9, tol: float = 1e-3) -> list[Hole]:
    """Vertical round holes found from concave cylindrical faces (axis ∥ Z)."""
    bb = shape.bounding_box()
    found: dict[tuple, Hole] = {}
    for face in shape.faces().filter_by(b3d.GeomType.CYLINDER):
        try:
            cyl = BRepAdaptor_Surface(face.wrapped).Cylinder()
        except Exception:
            continue
        d = cyl.Axis().Direction()
        if abs(abs(d.Z()) - 1.0) > 1e-6:
            continue
        loc = cyl.Axis().Location()
        r = cyl.Radius()
        if not (dmin <= 2 * r <= dmax):
            continue
        c = face.center()
        n = face.normal_at(c)
        radial = Vector(c.X - loc.X(), c.Y - loc.Y(), 0)
        if radial.length < 1e-9 or radial.dot(n) > 0:
            continue  # convex (a boss), not a hole
        fb = face.bounding_box()
        key = (round(loc.X(), 3), round(loc.Y(), 3), round(r, 3))
        h = found.get(key)
        if h:
            h.z_top = max(h.z_top, fb.max.Z); h.z_bottom = min(h.z_bottom, fb.min.Z)
        else:
            found[key] = Hole(loc.X(), loc.Y(), 2 * r, fb.max.Z, fb.min.Z, False)
    out = []
    sec_cache: dict[float, MultiPolygon] = {}

    def has_material(x, y, z):
        zk = round(z, 3)
        if zk not in sec_cache:
            sec_cache[zk] = section(shape, zk) if bb.min.Z < z < bb.max.Z else MultiPolygon([])
        return sec_cache[zk].covers(Point(x, y))

    for h in found.values():
        h.through = not has_material(h.x, h.y, h.z_top + 0.05) and not has_material(h.x, h.y, h.z_bottom - 0.05)
        out.append(h)
    return sorted(out, key=lambda h: (h.diameter, h.x, h.y))


def _as_multi(g) -> MultiPolygon:
    if g is None or g.is_empty:
        return MultiPolygon([])
    if isinstance(g, Polygon):
        return MultiPolygon([g])
    if isinstance(g, MultiPolygon):
        return g
    polys = [p for p in getattr(g, "geoms", []) if isinstance(p, Polygon) and not p.is_empty]
    return MultiPolygon(polys)


def _polys(x) -> list[Polygon]:
    """Accept Polygon / MultiPolygon / list / Face / list of (x,y) -> list of polygons."""
    if x is None:
        return []
    if isinstance(x, Polygon):
        return [x] if not x.is_empty else []
    if isinstance(x, MultiPolygon):
        return [p for p in x.geoms if not p.is_empty]
    if isinstance(x, Face):
        return [face_polygon(x)]
    if isinstance(x, Hole):
        return [Point(x.x, x.y).buffer(x.diameter / 2, 64)]
    if isinstance(x, (list, tuple)):
        if x and isinstance(x[0], (int, float)):
            raise CamError("expected polygons, got a bare number list")
        if x and isinstance(x[0], (list, tuple)) and len(x[0]) == 2 and isinstance(x[0][0], (int, float)):
            return [Polygon(x)]
        out = []
        for item in x:
            out.extend(_polys(item))
        return out
    if hasattr(x, "geoms"):
        return [p for p in x.geoms if isinstance(p, Polygon)]
    raise CamError(f"cannot interpret {type(x).__name__} as polygon(s)")


def circle(x: float, y: float, diameter: float) -> Polygon:
    return Point(x, y).buffer(diameter / 2, 64)


def rect(x0: float, y0: float, x1: float, y1: float) -> Polygon:
    return shp_box(x0, y0, x1, y1)


def _densify(coords, step: float = 0.5):
    out = [coords[0]]
    for a, b in zip(coords, coords[1:]):
        d = math.dist(a, b)
        n = max(1, int(d / step))
        for i in range(1, n + 1):
            t = i / n
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    return out


def _ring_coords(ring, climb_ccw: bool) -> list[tuple[float, float]]:
    coords = list(ring.coords)
    if coords[0] == coords[-1]:
        coords = coords[:-1]
    # shapely: is_ccw available on LinearRing
    ccw = ring.is_ccw
    if ccw != climb_ccw:
        coords = coords[::-1]
    return coords


def _nn_order(items: list, xy, start=None) -> list:
    """Greedy nearest-neighbour order of `items` by xy(item), starting nearest `start` (or the first item)."""
    rest = list(items)
    if not rest:
        return []
    out = []
    cur = start
    while rest:
        if cur is None:
            nxt = rest.pop(0)
        else:
            i = min(range(len(rest)), key=lambda k: (xy(rest[k])[0] - cur[0]) ** 2 + (xy(rest[k])[1] - cur[1]) ** 2)
            nxt = rest.pop(i)
        out.append(nxt); cur = xy(nxt)
    return out


def _levels(z_top: float, z_bottom: float, stepdown: float) -> list[float]:
    if z_bottom > z_top:
        raise CamError(f"z_bottom ({z_bottom}) is above z_top ({z_top})")
    depth = z_top - z_bottom
    if depth <= 1e-9:
        return [z_bottom]
    n = max(1, math.ceil(depth / stepdown - 1e-9))
    step = depth / n
    return [z_top - step * (i + 1) for i in range(n)]


# =============================================================================
# Operations
# =============================================================================
COLORS = {"face": "#7fd1ff", "contour": "#4ea1ff", "pocket": "#4cd37a", "drill": "#ffd166", "parallel3d": "#c58cff", "engrave": "#ff9f7a", "adaptive": "#ff9f43", "rest": "#8ef0d0"}


def face(setup: Setup, tool: Tool, z_top: float | None = None, z_bottom: float | None = None, region=None,
         stepover: float | None = None, stepdown: float | None = None, angle: float = 0.0, name: str = "Face") -> Op:
    """Surface the stock top down to z_bottom with parallel zigzag passes over `region` (default: stock)."""
    op = Op(name, "face", tool, color=COLORS["face"])
    pb = PathBuilder(setup, tool, op)
    z_top = setup.stock.top if z_top is None else z_top
    z_bottom = z_top - (stepdown or tool.stepdown) if z_bottom is None else z_bottom
    so = (stepover or tool.stepover) * tool.diameter
    polys = _polys(region) or [setup.stock.rect()]
    reg = unary_union(polys).buffer(tool.radius * 0.9)   # let the tool run past the edges
    minx, miny, maxx, maxy = reg.bounds
    op.params = dict(z_top=z_top, z_bottom=z_bottom, stepover=so)
    for z in _levels(z_top, z_bottom, stepdown or tool.stepdown):
        y = miny
        direction = 1
        first = True
        while y <= maxy + 1e-9:
            line = LineString([(minx - 1, y), (maxx + 1, y)]).intersection(reg)
            segs = [line] if isinstance(line, LineString) else list(getattr(line, "geoms", []))
            segs = [s for s in segs if isinstance(s, LineString) and s.length > 0]
            segs.sort(key=lambda s: s.coords[0][0] * direction)
            for s in segs:
                c = list(s.coords)
                if direction < 0:
                    c = c[::-1]
                if first or pb.pos is None or pb.pos[2] > z + 1e-6:
                    pb.rapid_xy(*c[0]); pb.plunge_to(z); first = False
                else:
                    pb.feed_to(c[0][0], c[0][1], z)
                for q in c[1:]:
                    pb.feed_to(q[0], q[1], z)
            y += so
            direction *= -1
        pb.retract()
    pb.retract()
    return op


def contour(setup: Setup, tool: Tool, polys, z_top: float, z_bottom: float, side: str = "outside",
            stepdown: float | None = None, tabs: int = 0, tab_height: float = 3.0, tab_width: float = 8.0,
            climb: bool = True, stock_to_leave: float = 0.0, lead: float | None = None,
            name: str = "Contour") -> Op:
    """Profile around polygons. side: 'outside' (tool outside material: outer rings AND holes),
    'inside' (tool inside, e.g. finishing a pocket wall), 'on' (centre on the line).
    lead: radius of the tangential arc lead-in/out (default tool radius outside, half inside, 0 for 'on';
    pass 0 to disable)."""
    op = Op(name, "contour", tool, color=COLORS["contour"])
    if lead is None:
        lead = tool.radius if side == "outside" else (tool.radius / 2 if side == "inside" else 0.0)
    pb = PathBuilder(setup, tool, op)
    src = _polys(polys)
    if not src:
        raise CamError("contour: no polygons given")
    off = tool.radius + stock_to_leave
    rings = []  # (coords, climb_ccw)
    for p in src:
        if side == "outside":
            g = p.buffer(off, join_style=1)
            for q in _polys(g):
                q = orient(q, 1.0)
                rings.append(_ring_coords(q.exterior, True))          # exterior CCW = climb
                rings += [_ring_coords(h, False) for h in q.interiors]  # holes CW = climb
        elif side == "inside":
            g = p.buffer(-off, join_style=1)
            for q in _polys(g):
                q = orient(q, 1.0)
                rings.append(_ring_coords(q.exterior, False))         # wall: CW = climb
                rings += [_ring_coords(h, True) for h in q.interiors]   # islands CCW = climb
        elif side == "on":
            q = orient(p, 1.0)
            rings.append(_ring_coords(q.exterior, True))
            rings += [_ring_coords(h, False) for h in q.interiors]
        else:
            raise CamError("side must be outside | inside | on")
    if not climb:
        rings = [r[::-1] for r in rings]
    levels = _levels(z_top, z_bottom, stepdown or tool.stepdown)
    tab_top = z_bottom + tab_height
    op.params = dict(z_top=z_top, z_bottom=z_bottom, side=side, tabs=tabs, passes=len(levels), lead=lead)
    for ring in rings:
        ring = _start_on_straight(ring)
        dense = _densify(ring + [ring[0]], 0.5)
        # distance along ring for tab placement
        cum = [0.0]
        for a, b in zip(dense, dense[1:]):
            cum.append(cum[-1] + math.dist(a, b))
        total = cum[-1]
        tab_centres = [total * (i + 0.5) / tabs for i in range(tabs)] if tabs > 0 else []
        lead_in, lead_out = _lead_arcs(dense, lead, climb) if lead > 0 else ([], [])
        for z in levels:
            use_tabs = tabs > 0 and z < tab_top - 1e-9
            z0 = max(z, tab_top) if use_tabs and _in_tab(0.0, tab_centres, tab_width, total) else z
            start = lead_in[0] if lead_in else dense[0]
            pb.rapid_xy(*start); pb.plunge_to(z0)
            for x, y in lead_in[1:]:
                pb.feed_to(x, y, z0)
            for (x, y), s in zip(dense[1:], cum[1:]):
                zz = z
                if use_tabs and _in_tab(s, tab_centres, tab_width, total):
                    zz = max(z, tab_top)
                pb.feed_to(x, y, zz, pb.plunge if zz > pb.pos[2] + 1e-9 or zz < pb.pos[2] - 1e-9 else None)
            for x, y in lead_out:
                pb.feed_to(x, y, pb.pos[2])
        pb.retract()
    pb.retract()
    return op


def _start_on_straight(ring: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Rotate the ring so it starts in the middle of its longest edge (better lead-in, no corner marks)."""
    n = len(ring)
    if n < 3:
        return ring
    best, bi = -1.0, 0
    for i in range(n):
        d = math.dist(ring[i], ring[(i + 1) % n])
        if d > best:
            best, bi = d, i
    a, b = ring[bi], ring[(bi + 1) % n]
    mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    return [mid] + ring[bi + 1:] + ring[:bi + 1]


def _lead_arcs(dense, r: float, climb: bool):
    """Quarter-circle tangential lead-in ending at dense[0] and lead-out starting at dense[0] (closed ring).
    Material is on the left when climb milling, so the lead swings to the right."""
    p0 = dense[0]; p1 = dense[1]
    tx, ty = p1[0] - p0[0], p1[1] - p0[1]
    ln = math.hypot(tx, ty) or 1.0
    tx, ty = tx / ln, ty / ln
    nx, ny = (ty, -tx) if climb else (-ty, tx)         # away from the material
    cx, cy = p0[0] + nx * r, p0[1] + ny * r              # arc centre
    n = 8
    a0 = math.atan2(p0[1] - cy, p0[0] - cx)               # angle of p0 around centre
    # lead-in sweeps 90° ending tangent to +T at p0; direction of sweep: sign of (N x T)
    sgn = 1.0 if (nx * ty - ny * tx) > 0 else -1.0
    lead_in = [(cx + r * math.cos(a0 - sgn * (math.pi / 2) * (1 - i / n)), cy + r * math.sin(a0 - sgn * (math.pi / 2) * (1 - i / n))) for i in range(n + 1)]
    # lead-out: leaves p0 tangent to the incoming direction (ring end == p0) and swings away
    pe = dense[-2]
    tx2, ty2 = p0[0] - pe[0], p0[1] - pe[1]
    ln2 = math.hypot(tx2, ty2) or 1.0
    tx2, ty2 = tx2 / ln2, ty2 / ln2
    nx2, ny2 = (ty2, -tx2) if climb else (-ty2, tx2)
    cx2, cy2 = p0[0] + nx2 * r, p0[1] + ny2 * r
    a1 = math.atan2(p0[1] - cy2, p0[0] - cx2)
    sgn2 = 1.0 if (nx2 * ty2 - ny2 * tx2) > 0 else -1.0
    lead_out = [(cx2 + r * math.cos(a1 + sgn2 * (math.pi / 2) * (i / n)), cy2 + r * math.sin(a1 + sgn2 * (math.pi / 2) * (i / n))) for i in range(1, n + 1)]
    return lead_in, lead_out


def _in_tab(s: float, centres: list[float], width: float, total: float) -> bool:
    for c in centres:
        d = abs(s - c)
        d = min(d, total - d)
        if d <= width / 2:
            return True
    return False


def pocket(setup: Setup, tool: Tool, polys, z_top: float, z_bottom: float, stepdown: float | None = None,
           stepover: float | None = None, finish: bool = True, climb: bool = True, rest_from: Tool | None = None,
           stock_to_leave: float = 0.0, name: str = "Pocket") -> Op:
    """Clear closed regions (holes in the polygons are islands) with concentric inward passes,
    innermost first, wall pass last. rest_from=<bigger tool>: only machine what that tool left.
    stock_to_leave: radial allowance left on the walls for a finishing contour."""
    op = Op(name, "pocket" if rest_from is None else "rest", tool, color=COLORS["pocket" if rest_from is None else "rest"])
    pb = PathBuilder(setup, tool, op)
    src = _polys(polys)
    if not src:
        raise CamError("pocket: no polygons given")
    region = unary_union(src)
    if stock_to_leave > 0:
        region = region.buffer(-stock_to_leave, join_style=1)
    if rest_from is not None:
        region = unary_union(_polys(rest_region(region, rest_from, tool)))
        if region.is_empty:
            op.warnings.append(f"nothing left for {tool.label()} after {rest_from.label()}")
            return op
    so = (stepover or tool.stepover) * tool.diameter
    if so <= 0 or so > tool.diameter:
        raise CamError("stepover must be (0, 1] × diameter")
    # concentric offsets from the wall inward
    shells = []
    d = tool.radius
    while True:
        g = region.buffer(-d, join_style=1)
        ps = _polys(g)
        if not ps:
            break
        shells.append(ps)
        d += so
    if not shells:
        if rest_from is not None:
            op.warnings.append(f"rest: {tool.label()} does not fit in what {rest_from.label()} left")
            return op
        raise CamError(f"pocket: tool Ø{tool.diameter} does not fit in the region")
    levels = _levels(z_top, z_bottom, stepdown or tool.stepdown)
    # separate regions (two pockets) are finished one after the other through all depths — not one level across all of
    # them — and visited nearest-neighbour; within a region the next level is plunged where the tool already is
    regions = _nn_order(_polys(region), lambda R: (R.centroid.x, R.centroid.y), pb.pos[:2] if pb.pos else None)
    op.params = dict(z_top=z_top, z_bottom=z_bottom, stepover=so, passes=len(levels), rings=len(shells), regions=len(regions))
    for R in regions:
        shells_R = [[q for q in ps if R.covers(q.representative_point())] for ps in shells]
        shells_R = [ps for ps in shells_R if ps]
        if not shells_R:
            continue
        inner_ok = R.buffer(-tool.radius + 1e-6)   # where the tool centre may travel at depth inside this region
        first = True
        for z in levels:
            for ps in reversed(shells_R):        # innermost first
                for q in ps:
                    q = orient(q, 1.0)
                    rings = [_ring_coords(q.exterior, False)] + [_ring_coords(h, True) for h in q.interiors]
                    if not climb:
                        rings = [r[::-1] for r in rings]
                    for ring in rings:
                        start = ring[0]
                        if first or pb.pos is None or not inner_ok.covers(LineString([pb.pos[:2], start])):
                            pb.rapid_xy(*start); pb.plunge_to(z); first = False
                        else:
                            if pb.pos[2] > z + 1e-6:
                                pb.plunge_to(z)                      # next level: straight down where the tool is (cleared floor)
                            pb.feed_to(start[0], start[1], z)
                        for x, y in ring[1:]:
                            pb.feed_to(x, y, z)
                        pb.feed_to(start[0], start[1], z)
        pb.retract()
    pb.retract()
    return op


def rest_region(polys, prev_tool: Tool, tool: Tool, overlap: float | None = None) -> MultiPolygon:
    """Where a previous (bigger) tool could not reach inside `polys` (corners, narrow slots): the region
    minus its morphological opening by the previous tool radius, grown by `overlap` (default: this
    tool's diameter — enough for the tool to enter the corners) so the small tool blends into the
    cleared area."""
    P = unary_union(_polys(polys))
    if P.is_empty:
        return MultiPolygon([])
    reach = P.buffer(-prev_tool.radius, join_style=1).buffer(prev_tool.radius + 1e-6, join_style=1)
    rest = P.difference(reach)
    ov = tool.diameter if overlap is None else overlap
    rest = rest.buffer(ov, join_style=1).intersection(P)
    return _as_multi(rest)


def adaptive(setup: Setup, tool: Tool, polys, z_top: float, z_bottom: float, stepover: float = 0.15,
             stepdown: float | None = None, helix_diameter: float | None = None, climb: bool = True,
             rest_from: Tool | None = None, stock_to_leave: float = 0.0, chip_thinning: bool = True,
             max_seconds: float = 120.0, name: str = "Adaptive") -> Op:
    """Constant-engagement (HSM / trochoidal) roughing, by construction:
    the cleared region K (everything outside the pocket counts as cleared, plus the helix entry) is kept
    as an exact polygon. Each pass runs the tool along the curve where the cutter protrudes from K by
    exactly the radial engagement ae = stepover × diameter (the inward offset of K by r − ae), clipped
    to where the tool centre may go; K then grows by the swept band and the next pass follows the new
    front. Near walls and in slots the fronts become successive crescents — the trochoidal motion —
    without the engagement ever exceeding ae except transiently at concave corners. Full axial depth
    by default (90% of flute length), helical entry, islands and rest material (rest_from) handled
    natively, links stay inside cleared ground at feed. Leaves radial scallops of ≤ ae on the walls:
    follow with a contour finishing pass."""
    op = Op(name, "adaptive", tool, color=COLORS["adaptive"])
    pb = PathBuilder(setup, tool, op)
    src = _polys(polys)
    if not src:
        raise CamError("adaptive: no polygons given")
    P = unary_union(src)
    if stock_to_leave > 0:
        P = P.buffer(-stock_to_leave, join_style=1)
    if not (0 < stepover <= 0.5):
        raise CamError("adaptive: stepover (radial engagement) must be in (0, 0.5] × diameter")
    r = tool.radius
    ae = stepover * tool.diameter
    delta = r - ae
    sd = stepdown or max(min(tool.flute_length * 0.9, z_top - z_bottom), 0.1)
    levels = _levels(z_top, z_bottom, sd)
    C = P.buffer(-r, join_style=1)                        # allowed tool-centre region
    if C.is_empty:
        op.warnings.append(f"adaptive: {tool.label()} does not fit in the region")
        return op
    from shapely.ops import polylabel, linemerge
    from shapely.prepared import prep
    from shapely import set_precision
    B = P.envelope.buffer(3 * r)
    notP = B.difference(P)
    K0 = notP
    if rest_from is not None:
        K0 = K0.union(P.buffer(-rest_from.radius, join_style=1).buffer(rest_from.radius + 1e-6, join_style=1))
        if P.difference(K0).area <= 1e-4 * P.area + 1e-6:     # polygonal offsets leave slivers; treat as empty
            op.warnings.append(f"nothing left for {tool.label()} after {rest_from.label()}")
            return op
    rct = 1.0 / math.sqrt(1.0 - (1.0 - 2.0 * stepover) ** 2) if (chip_thinning and stepover < 0.5) else 1.0
    feed = min(pb.feed * rct, min(setup.machine.max_feed.get("x", 1e9), setup.machine.max_feed.get("y", 1e9)))
    phi = math.degrees(math.acos(max(-1.0, min(1.0, 1 - ae / r))))
    op.params = dict(z_top=z_top, z_bottom=z_bottom, engagement=round(ae, 3), engagement_angle=round(phi, 1),
                     stepdown=round(sd, 3), passes=len(levels), feed=int(feed))
    sign = 1.0 if climb else -1.0
    # engagement-aware feed: in corners the contact arc rises above the target even though the radial
    # protrusion stays ≤ ae; slow down there to keep the chip load per tooth constant
    NSA = 36
    ang_s = np.arange(NSA) * 2 * math.pi / NSA
    ring_x, ring_y = (r - 0.02) * np.cos(ang_s), (r - 0.02) * np.sin(ang_s)
    max_eng_seen = 0.0

    def eng_angle(mat_prep, x, y, hx, hy):
        lead = (ring_x * hx + ring_y * hy) > 0
        n = 0
        for k in np.nonzero(lead)[0]:
            if mat_prep.contains(Point(x + ring_x[k], y + ring_y[k])):
                n += 1
        return 360.0 * n / NSA
    step_len = max(0.15, min(0.5, r / 6))
    import time as _time
    t_start = _time.time()
    links = retracts = passes_total = 0
    Cbig = max(_polys(C), key=lambda g: g.area)
    try:
        ep = polylabel(Cbig, tolerance=0.05)
    except Exception:
        ep = Cbig.representative_point()
    ex, ey = ep.x, ep.y
    hr = min((helix_diameter / 2) if helix_diameter else r * 0.6, max(0.2, Cbig.boundary.distance(ep) * 0.9))
    material0 = P.difference(K0).area

    def oriented(seg, K_inner):
        """Orient a front segment so the material is on the left (climb) / right (conventional)."""
        pts = list(seg.coords)
        if len(pts) < 2:
            return pts
        i = len(pts) // 2
        a, b = pts[max(0, i - 1)], pts[min(len(pts) - 1, i + 1)]
        tx, ty = b[0] - a[0], b[1] - a[1]
        ln = math.hypot(tx, ty) or 1.0
        lx, ly = -ty / ln, tx / ln                           # left normal
        probe = Point(pts[i][0] + lx * 0.3, pts[i][1] + ly * 0.3)
        left_is_cleared = K_inner.covers(probe)
        material_left = not left_is_cleared
        if material_left != climb:
            pts = pts[::-1]
        return pts

    for z in levels:
        if _time.time() - t_start > max_seconds:
            op.warnings.append(f"adaptive: time budget of {max_seconds:.0f}s exhausted; remaining levels skipped")
            break
        # ---- helical entry
        depth = z_top - z
        n_turns = max(1, math.ceil(depth / min(1.0, tool.diameter * 0.15)))
        n_pts = 24 * n_turns
        pb.rapid_xy(ex + hr, ey)
        pb.plunge_to(z_top + 0.5, rapid_to=z_top)
        for i in range(1, n_pts + 1):
            a = sign * 2 * math.pi * i / 24
            zz = z_top + 0.5 - (z_top + 0.5 - z) * i / n_pts
            pb.feed_to(ex + hr * math.cos(a), ey + hr * math.sin(a), zz, pb.plunge)
        K = K0.union(Point(ex, ey).buffer(hr + r, 48))
        pos = (pb.pos[0], pb.pos[1])
        Kc_prev = None
        recent = []
        it = 0
        while it < 5000:
            it += 1
            if _time.time() - t_start > max_seconds:
                op.warnings.append(f"adaptive: time budget of {max_seconds:.0f}s exhausted; stopping")
                break
            inner = K.buffer(-delta, join_style=1)
            if inner.is_empty:
                break
            # the pass runs where the cutter protrudes from K by ae; where that curve leaves the allowed
            # centre region it follows the wall instead (there the protrusion is < ae)
            allowed = inner.intersection(C)
            if allowed.is_empty:
                break
            front = allowed.boundary.difference(K.buffer(-r + 1e-3, join_style=1).boundary)   # drop air-cutting stretches
            front = front.difference(Kc_prev) if Kc_prev is not None else front
            segs = []
            if not front.is_empty:
                merged = linemerge(front) if front.geom_type == "MultiLineString" else front
                geoms = list(merged.geoms) if hasattr(merged, "geoms") else [merged]
                segs = [g for g in geoms if g.geom_type == "LineString" and g.length > ae * 0.5]
            if not segs:
                break
            Kc_poly = K.buffer(-r + 1e-3, join_style=1)
            Kc = prep(Kc_poly)                                   # tool fully inside cleared ground
            Kc_prev = Kc_poly.buffer(-1e-3)
            remaining = [oriented(g, inner) for g in segs]
            swept = []
            while remaining:
                # nearest segment start from the current position
                k = min(range(len(remaining)), key=lambda i: (remaining[i][0][0] - pos[0]) ** 2 + (remaining[i][0][1] - pos[1]) ** 2)
                pts = remaining.pop(k)
                dense = _densify(pts, step_len)
                sx, sy = dense[0]
                link_line = LineString([pos, (sx, sy)])
                if Kc.covers(link_line):
                    pb.feed_to(sx, sy, z, feed); links += 1
                else:
                    via = None
                    from shapely.ops import nearest_points
                    mid = Point((pos[0] + sx) / 2, (pos[1] + sy) / 2)
                    cands = []
                    try:
                        npt = nearest_points(Kc_poly, mid)[0]; cands.append((npt.x, npt.y))
                    except Exception:
                        pass
                    cands += [(ex, ey)] + recent[-6:]
                    for wx, wy in cands:
                        if Kc.covers(LineString([pos, (wx, wy)])) and Kc.covers(LineString([(wx, wy), (sx, sy)])):
                            via = (wx, wy); break
                    if via:
                        pb.feed_to(via[0], via[1], z, feed); pb.feed_to(sx, sy, z, feed); links += 1
                    else:
                        pb.rapid_xy(sx, sy); pb.plunge_to(z); retracts += 1
                mat_prep = prep(P.difference(K))
                px_, py_ = dense[0]
                for x, y in dense[1:]:
                    hx, hy = x - px_, y - py_
                    ln = math.hypot(hx, hy) or 1.0
                    e = eng_angle(mat_prep, x, y, hx / ln, hy / ln)
                    max_eng_seen = max(max_eng_seen, e)
                    f = feed if e <= phi * 1.15 else max(feed * 0.3, feed * phi / e)
                    pb.feed_to(x, y, z, f)
                    px_, py_ = x, y
                pos = dense[-1]
                recent.append(dense[len(dense) // 2]); recent = recent[-12:]
                swept.append(LineString(dense).buffer(r, 16))
                passes_total += 1
            K = unary_union([K] + swept).simplify(0.01)
        pb.retract()
        reachable = P.buffer(-r, join_style=1).buffer(r + 1e-6, join_style=1)     # what this tool can reach at all
        left = P.difference(K).intersection(reachable).area
        if material0 > 0 and left > 0.03 * material0:
            op.warnings.append(f"adaptive: ~{100 * left / material0:.0f}% of the reachable material was not cleared at z={z:.2f}")
    pb.retract()
    op.params.update(links=links, retracts=retracts, fronts=passes_total, max_engagement_angle=round(max_eng_seen, 1))
    if max_eng_seen > 2.2 * phi:
        op.warnings.append(f"adaptive: engagement rises to {max_eng_seen:.0f}° in corners (target {phi:.0f}°); feed is reduced there")
    return op


def _resample_closed(ring, start_near, n: int | None = None, step: float = 0.5):
    """Closed ring -> n points by arc length, starting at the vertex nearest `start_near`."""
    pts = list(ring)
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    k = min(range(len(pts)), key=lambda i: (pts[i][0] - start_near[0]) ** 2 + (pts[i][1] - start_near[1]) ** 2)
    pts = pts[k:] + pts[:k]
    closed = pts + [pts[0]]
    cum = [0.0]
    for a, b in zip(closed, closed[1:]):
        cum.append(cum[-1] + math.dist(a, b))
    total = cum[-1] or 1.0
    n = n or max(8, int(total / step))
    out = []
    j = 0
    for i in range(n):
        s = total * i / n
        while j < len(cum) - 2 and cum[j + 1] < s:
            j += 1
        a, b = closed[j], closed[j + 1]
        seg = cum[j + 1] - cum[j] or 1.0
        t = (s - cum[j]) / seg
        out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    return out


def _concentric_full_depth(pb: PathBuilder, P: Polygon, tool: Tool, so: float, levels, climb: bool):
    shells = []
    d = tool.radius
    while True:
        ps = _polys(P.buffer(-d, join_style=1))
        if not ps:
            break
        shells.append(ps)
        d += so
    inner_ok = P.buffer(-tool.radius + 1e-6)
    for z in levels:
        first = True
        for ps in reversed(shells):
            for q in ps:
                q = orient(q, 1.0)
                rings = [_ring_coords(q.exterior, False)] + [_ring_coords(h, True) for h in q.interiors]
                if not climb:
                    rings = [r[::-1] for r in rings]
                for ring in rings:
                    start = ring[0]
                    if first or pb.pos is None or pb.pos[2] > z + 1e-6 or not inner_ok.covers(LineString([pb.pos[:2], start])):
                        pb.rapid_xy(*start); pb.plunge_to(z); first = False
                    else:
                        pb.feed_to(start[0], start[1], z)
                    for x, y in ring[1:]:
                        pb.feed_to(x, y, z)
                    pb.feed_to(start[0], start[1], z)
        pb.retract()


def drill(setup: Setup, tool: Tool, points, z_top: float | None = None, z_bottom: float | None = None,
          peck: float | None = None, retract: float = 1.0, name: str = "Drill") -> Op:
    """Drill at Hole objects or (x, y) points. Depths default from Hole.z_top/z_bottom.
    peck: depth per peck (None = single plunge, 'auto' = 2×diameter)."""
    op = Op(name, "drill", tool, color=COLORS["drill"])
    pb = PathBuilder(setup, tool, op)
    items = list(points) if isinstance(points, (list, tuple)) else [points]
    if not items:
        raise CamError("drill: no points")
    # one visit per location: holes() reports a counterbore and its through hole as two Hole objects at the same XY —
    # merge them into one z range so the hole is drilled once, to full depth, with all its pecks before moving on
    locs: list[list] = []                       # [x, y, z_top, z_bottom, through, min_dia]
    for it in items:
        if isinstance(it, Hole):
            x, y = it.x, it.y
            zt = it.z_top if z_top is None else z_top
            zb = it.z_bottom if z_bottom is None else z_bottom
            if it.diameter < tool.diameter - 1e-6:
                op.warnings.append(f"hole Ø{it.diameter:.2f} at ({x:.1f},{y:.1f}) is smaller than tool Ø{tool.diameter}")
            thr, dia = bool(it.through), it.diameter
        else:
            x, y = float(it[0]), float(it[1])
            if z_top is None or z_bottom is None:
                raise CamError("drill: z_top and z_bottom are required for (x, y) points")
            zt, zb, thr, dia = z_top, z_bottom, False, tool.diameter
        same = next((L for L in locs if abs(L[0] - x) < 0.05 and abs(L[1] - y) < 0.05), None)
        if same is not None:
            same[2] = max(same[2], zt); same[3] = min(same[3], zb); same[4] = same[4] or thr; same[5] = min(same[5], dia)
        else:
            locs.append([x, y, zt, zb, thr, dia])
    if z_bottom is None:
        for L in locs:
            if L[4]:
                L[3] -= tool.diameter * 0.3 + 0.5    # break through cleanly
    locs = _nn_order(locs, lambda L: (L[0], L[1]), pb.pos[:2] if pb.pos else None)   # shortest hop to the next hole
    count = 0
    for x, y, zt, zb, _thr, _dia in locs:
        pk = (tool.diameter * 2) if peck == "auto" else peck
        pb.rapid_xy(x, y)
        pb._add(RAPID, x, y, zt + retract)
        if pk and pk > 0 and zt - zb > pk:
            z = zt
            while z > zb + 1e-9:
                z = max(zb, z - pk)
                pb._add(PLUNGE, x, y, z, pb.plunge)
                pb._add(RAPID, x, y, zt + retract)
                if z > zb + 1e-9:
                    pb._add(RAPID, x, y, z + 0.5)
        else:
            pb._add(PLUNGE, x, y, zb, pb.plunge)
            pb._add(RAPID, x, y, zt + retract)
        count += 1
    pb.retract()
    op.params = dict(holes=count, peck=peck, merged=len(items) - len(locs))
    return op


def parallel3d(setup: Setup, tool: Tool, shape: Shape | None = None, model: ck.Model | None = None,
               z_top: float | None = None, z_bottom: float | None = None, region=None,
               stepover: float | None = None, angle: float = 0.0, resolution: float | None = None,
               rest_from: Tool | None = None, name: str = "Parallel 3D") -> Op:
    """3D finishing: parallel raster passes following the surface (heightmap from the exact shape's
    triangulation, offset by the tool shape: ball or flat). Cuts down to z_bottom outside the part.
    rest_from=<bigger tool>: only where that tool left material (its tip map sits higher)."""
    if shape is None and model is None:
        raise CamError("parallel3d: give shape= or model=")
    shape = shape or model.shape
    op = Op(name, "parallel3d", tool, color=COLORS["parallel3d"])
    pb = PathBuilder(setup, tool, op)
    so = (stepover or min(tool.stepover, 0.2)) * tool.diameter
    res = resolution or max(0.2, min(so / 2, tool.diameter / 6))
    regp = unary_union(_polys(region)) if region is not None else setup.stock.rect()
    minx, miny, maxx, maxy = regp.bounds
    z_top = setup.stock.top if z_top is None else z_top
    z_bottom = setup.stock.bottom if z_bottom is None else z_bottom
    # heightmap
    pairs = ck.coerce_bodies(shape)
    tmp = ck.build_model(pairs, "", "fine")
    nx = int((maxx - minx) / res) + 2; ny = int((maxy - miny) / res) + 2
    zmap = np.full((ny, nx), z_bottom, dtype=np.float64)
    for bd in tmp.mesh["bodies"]:
        P = np.array(bd["positions"], dtype=np.float64).reshape(-1, 3)
        I = np.array(bd["indices"], dtype=np.int64).reshape(-1, 3)
        _raster_triangles(P, I, zmap, minx, miny, res)
    zmap = np.clip(zmap, z_bottom, None)
    # tool offset (footprint max)
    r = tool.radius
    k = int(math.ceil(r / res))
    off = np.full((2 * k + 1, 2 * k + 1), -np.inf)
    for i in range(-k, k + 1):
        for j in range(-k, k + 1):
            d = math.hypot(i * res, j * res)
            if d <= r + 1e-9:
                off[i + k, j + k] = (math.sqrt(max(r * r - d * d, 0.0)) - r) if tool.type == "ball" else 0.0
    tip = np.full_like(zmap, -np.inf)
    padded = np.pad(zmap, k, mode="edge")
    for i in range(2 * k + 1):
        for j in range(2 * k + 1):
            if off[i, j] > -np.inf:
                tip = np.maximum(tip, padded[i:i + ny, j:j + nx] + off[i, j])
    tip = np.clip(tip, z_bottom, z_top)
    mask = None
    if rest_from is not None:
        rp = rest_from.radius
        kp = int(math.ceil(rp / res))
        tip_prev = np.full_like(zmap, -np.inf)
        padded_p = np.pad(zmap, kp, mode="edge")
        for i in range(2 * kp + 1):
            for j in range(2 * kp + 1):
                d = math.hypot((i - kp) * res, (j - kp) * res)
                if d <= rp + 1e-9:
                    o = (math.sqrt(max(rp * rp - d * d, 0.0)) - rp) if rest_from.type == "ball" else 0.0
                    tip_prev = np.maximum(tip_prev, padded_p[i:i + ny, j:j + nx] + o)
        tip_prev = np.clip(tip_prev, z_bottom, z_top)
        mask = (tip_prev - tip) > 0.02          # previous tool sat higher: material left behind
        # grow the mask by one tool radius so passes overlap into the finished area
        g = max(1, int(round(tool.radius / res)))
        from numpy.lib.stride_tricks import sliding_window_view
        pm = np.pad(mask, g, mode="constant")
        mask = sliding_window_view(pm, (2 * g + 1, 2 * g + 1)).any(axis=(2, 3))
        if not mask.any():
            op.warnings.append(f"rest: nothing left for {tool.label()} after {rest_from.label()}")
            return op
    # raster passes (angle 0 = along X)
    ca, sa = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    diag = math.hypot(maxx - minx, maxy - miny)
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    n_lines = int(diag / so) + 1
    direction = 1
    first = True
    for li in range(-n_lines // 2, n_lines // 2 + 1):
        # line li: points c + u*t + v*li*so, u=(ca,sa), v=(-sa,ca)
        ox, oy = cx - sa * li * so, cy + ca * li * so
        ts = np.arange(-diag / 2, diag / 2 + res, res) * direction
        xs = ox + ca * ts; ys = oy + sa * ts
        inside = (xs >= minx) & (xs <= maxx) & (ys >= miny) & (ys <= maxy)
        if not inside.any():
            direction *= -1
            continue
        line = LineString([(xs[inside][0], ys[inside][0]), (xs[inside][-1], ys[inside][-1])])
        if region is not None and not regp.intersects(line):
            direction *= -1
            continue
        runs: list[list] = [[]]
        for x, y, ok in zip(xs, ys, inside):
            if not ok or (region is not None and not regp.covers(Point(x, y))):
                if runs[-1]: runs.append([])
                continue
            ix = min(int((x - minx) / res), nx - 1); iy = min(int((y - miny) / res), ny - 1)
            if mask is not None and not mask[iy, ix]:
                if runs[-1]: runs.append([])
                continue
            runs[-1].append((float(x), float(y), float(tip[iy, ix])))
        runs = [r for r in runs if len(r) >= 2]
        if not runs:
            direction *= -1
            continue
        for ri, pts in enumerate(runs):
            pts = _simplify_z(pts)
            if first or pb.pos is None or mask is not None and ri == 0 and len(runs) > 0 and False:
                pb.rapid_xy(pts[0][0], pts[0][1]); pb.plunge_to(pts[0][2]); first = False
            elif mask is not None or ri > 0:
                pb.rapid_xy(pts[0][0], pts[0][1]); pb.plunge_to(pts[0][2])   # separate patches: retract between
            else:
                pb.feed_to(pts[0][0], pts[0][1], max(pts[0][2], pb.pos[2]))   # step over at the higher of the two
                pb.feed_to(pts[0][0], pts[0][1], pts[0][2], pb.plunge)
            pb.feed_path(pts[1:])
        direction *= -1
    pb.retract()
    op.params = dict(stepover=so, resolution=res, z_top=z_top, z_bottom=z_bottom, grid=[nx, ny])
    return op


def _simplify_z(pts, tol: float = 0.005):
    """Drop collinear points along a raster line."""
    out = [pts[0]]
    for a, b, c in zip(pts, pts[1:], pts[2:]):
        # keep b if it deviates from the line a-c
        t = (b[0] - a[0], b[1] - a[1], b[2] - a[2]); u = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
        lu = math.sqrt(u[0] ** 2 + u[1] ** 2 + u[2] ** 2) or 1.0
        proj = (t[0] * u[0] + t[1] * u[1] + t[2] * u[2]) / lu
        perp = math.sqrt(max(t[0] ** 2 + t[1] ** 2 + t[2] ** 2 - proj * proj, 0.0))
        if perp > tol:
            out.append(b)
    out.append(pts[-1])
    return out


def _raster_triangles(P: np.ndarray, I: np.ndarray, zmap: np.ndarray, minx: float, miny: float, res: float):
    ny, nx = zmap.shape
    for tri in I:
        a, b, c = P[tri[0]], P[tri[1]], P[tri[2]]
        x0 = max(int((min(a[0], b[0], c[0]) - minx) / res), 0); x1 = min(int((max(a[0], b[0], c[0]) - minx) / res) + 1, nx - 1)
        y0 = max(int((min(a[1], b[1], c[1]) - miny) / res), 0); y1 = min(int((max(a[1], b[1], c[1]) - miny) / res) + 1, ny - 1)
        if x1 < x0 or y1 < y0:
            continue
        xs = minx + np.arange(x0, x1 + 1) * res; ys = miny + np.arange(y0, y1 + 1) * res
        X, Y = np.meshgrid(xs, ys)
        d = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
        if abs(d) < 1e-12:
            continue
        l1 = ((b[1] - c[1]) * (X - c[0]) + (c[0] - b[0]) * (Y - c[1])) / d
        l2 = ((c[1] - a[1]) * (X - c[0]) + (a[0] - c[0]) * (Y - c[1])) / d
        l3 = 1 - l1 - l2
        m = (l1 >= -1e-6) & (l2 >= -1e-6) & (l3 >= -1e-6)
        if not m.any():
            continue
        Z = l1 * a[2] + l2 * b[2] + l3 * c[2]
        sub = zmap[y0:y1 + 1, x0:x1 + 1]
        np.maximum(sub, np.where(m, Z, -np.inf), out=sub)


PUBLIC_API = ["Stock.from_model", "Stock.block", "Stock.cylinder", "Setup", "Setup.view", "Program", "face", "contour", "pocket",
              "adaptive", "drill", "parallel3d", "rest_region", "section", "silhouette", "stock_minus", "holes", "face_polygon",
              "circle", "rect", "feeds", "apply_feeds", "rotary_rough", "rotary_finish", "rotary_wrap", "radial_map"]


def api_reference() -> str:
    """Signatures + first doc line of the CAM script API (given to the agent so it need not probe)."""
    import inspect
    lines = []
    for name in PUBLIC_API:
        obj = globals()
        for part_ in name.split("."):
            obj = obj[part_] if isinstance(obj, dict) else getattr(obj, part_)
        try:
            sig = str(inspect.signature(obj)).replace("cam_kernel.", "").replace("cad_kernel.", "")
        except (TypeError, ValueError):
            sig = "(...)"
        doc = (inspect.getdoc(obj) or "").strip().splitlines()
        lines.append(f"{name}{sig}" + (f"\n    {doc[0]}" if doc else ""))
    lines.append("Hole fields: x, y, diameter, z_top, z_bottom, through, depth. Polygons are shapely (Polygon/MultiPolygon: "
                 ".buffer(d), .difference(g), .union(g), .intersection(g), .area, .bounds, .geoms).")
    return "\n".join(lines)


# =============================================================================
# Feeds & speeds
# =============================================================================
# vc: surface speed m/min (carbide, dry / air blast); chipload mm per tooth by tool diameter (mm);
# stepdown as × diameter; stepover fraction of diameter for roughing.
MATERIALS: dict[str, dict] = {
    "aluminium": dict(vc=(150, 300), chipload={1: 0.008, 2: 0.012, 3: 0.02, 4: 0.03, 6: 0.05, 8: 0.06, 10: 0.08, 12: 0.10},
                      stepdown=0.5, stepover=0.4, plunge=0.3, notes="6061/5083; use air blast or mist, single/two flute for small routers"),
    "brass": dict(vc=(120, 250), chipload={1: 0.006, 2: 0.01, 3: 0.015, 4: 0.025, 6: 0.04, 8: 0.05, 12: 0.08}, stepdown=0.5, stepover=0.4, plunge=0.3),
    "mild-steel": dict(vc=(40, 90), chipload={1: 0.004, 2: 0.006, 3: 0.01, 4: 0.015, 6: 0.025, 8: 0.035, 12: 0.05}, stepdown=0.3, stepover=0.3, plunge=0.2,
                       notes="needs a rigid machine and low rpm; most hobby routers cannot do this"),
    "acrylic": dict(vc=(250, 500), chipload={1: 0.02, 2: 0.03, 3: 0.05, 4: 0.07, 6: 0.1, 8: 0.12, 12: 0.15}, stepdown=1.0, stepover=0.5, plunge=0.4,
                    notes="single-flute O-flute; keep chips big to avoid melting"),
    "hdpe": dict(vc=(300, 600), chipload={1: 0.03, 2: 0.05, 3: 0.08, 4: 0.1, 6: 0.15, 8: 0.2, 12: 0.25}, stepdown=1.5, stepover=0.5, plunge=0.5),
    "delrin": dict(vc=(250, 500), chipload={1: 0.02, 2: 0.04, 3: 0.06, 4: 0.08, 6: 0.12, 8: 0.15, 12: 0.2}, stepdown=1.0, stepover=0.5, plunge=0.4),
    "plywood": dict(vc=(300, 700), chipload={1: 0.02, 2: 0.04, 3: 0.06, 4: 0.09, 6: 0.15, 8: 0.2, 12: 0.28}, stepdown=1.5, stepover=0.5, plunge=0.5,
                    notes="compression or down-cut bit for clean faces"),
    "mdf": dict(vc=(300, 700), chipload={1: 0.02, 2: 0.04, 3: 0.07, 4: 0.1, 6: 0.18, 8: 0.25, 12: 0.33}, stepdown=2.0, stepover=0.5, plunge=0.5),
    "hardwood": dict(vc=(250, 600), chipload={1: 0.015, 2: 0.03, 3: 0.05, 4: 0.07, 6: 0.12, 8: 0.16, 12: 0.22}, stepdown=1.0, stepover=0.45, plunge=0.4),
    "softwood": dict(vc=(300, 700), chipload={1: 0.02, 2: 0.04, 3: 0.07, 4: 0.1, 6: 0.18, 8: 0.25, 12: 0.33}, stepdown=2.0, stepover=0.5, plunge=0.5),
    "foam": dict(vc=(400, 900), chipload={1: 0.05, 2: 0.08, 3: 0.12, 4: 0.15, 6: 0.25, 8: 0.3, 12: 0.4}, stepdown=3.0, stepover=0.6, plunge=0.8),
    "fr4": dict(vc=(100, 200), chipload={1: 0.008, 2: 0.012, 3: 0.02, 4: 0.03, 6: 0.04, 8: 0.05, 12: 0.06}, stepdown=0.5, stepover=0.4, plunge=0.3,
                notes="abrasive: carbide only, dust extraction"),
    "carbon-fibre": dict(vc=(100, 250), chipload={1: 0.008, 2: 0.012, 3: 0.02, 4: 0.03, 6: 0.04, 8: 0.05, 12: 0.06}, stepdown=0.5, stepover=0.4, plunge=0.3,
                         notes="abrasive + hazardous dust: diamond-coated tools, extraction"),
}
MATERIAL_ALIASES = {"aluminum": "aluminium", "alu": "aluminium", "al": "aluminium", "steel": "mild-steel", "pom": "delrin",
                    "acetal": "delrin", "pe": "hdpe", "polyethylene": "hdpe", "ply": "plywood", "wood": "hardwood",
                    "pine": "softwood", "oak": "hardwood", "pmma": "acrylic", "perspex": "acrylic", "plexiglass": "acrylic",
                    "eps": "foam", "xps": "foam", "pcb": "fr4", "cf": "carbon-fibre", "cfrp": "carbon-fibre"}


def _interp_chipload(table: dict, d: float) -> float:
    ks = sorted(table)
    if d <= ks[0]:
        return table[ks[0]]
    if d >= ks[-1]:
        return table[ks[-1]]
    for a, b in zip(ks, ks[1:]):
        if a <= d <= b:
            t = (d - a) / (b - a)
            return table[a] + (table[b] - table[a]) * t
    return table[ks[-1]]


def feeds(tool: Tool, material: str, machine: Machine | None = None, aggressiveness: float = 0.5,
          radial_engagement: float | None = None) -> dict:
    """Feeds & speeds from surface speed and chip load. aggressiveness 0..1 picks within the vc range.
    radial_engagement (fraction of diameter) < 0.5 applies radial chip thinning. Spindle limits from the
    machine clamp rpm (feed follows). Returns rpm, feed, plunge, stepdown, stepover, vc, chipload, notes."""
    key = MATERIAL_ALIASES.get(material.lower().strip(), material.lower().strip())
    if key not in MATERIALS:
        raise CamError(f"unknown material '{material}'. Known: {', '.join(MATERIALS)}")
    mat = MATERIALS[key]
    notes: list[str] = []
    a = min(max(aggressiveness, 0.0), 1.0)
    vc = mat["vc"][0] + (mat["vc"][1] - mat["vc"][0]) * a
    d = tool.diameter
    rpm = 1000 * vc / (math.pi * d)
    if machine is not None:
        lo, hi = machine.spindle["min"], machine.spindle["max"]
        if rpm > hi:
            notes.append(f"spindle-limited: wanted {rpm:.0f} rpm for vc {vc:.0f} m/min, max is {hi:.0f}")
            rpm = hi
        elif rpm < lo:
            notes.append(f"rpm raised to spindle minimum {lo:.0f} (vc becomes {math.pi * d * lo / 1000:.0f} m/min)")
            rpm = lo
    cl = _interp_chipload(mat["chipload"], d) * (0.7 + 0.6 * a)
    flutes = max(1, tool.flutes)
    if tool.type == "drill":
        cl *= 0.6; flutes = 2
    ae = radial_engagement if radial_engagement is not None else mat["stepover"]
    rct = 1.0
    if 0 < ae < 0.5:
        rct = 1.0 / math.sqrt(1.0 - (1.0 - 2.0 * ae) ** 2)   # radial chip thinning
        notes.append(f"chip thinning ×{rct:.2f} at {ae * 100:.0f}% engagement")
    feed = rpm * flutes * cl * rct
    if machine is not None:
        mx = min(machine.max_feed.get("x", 1e9), machine.max_feed.get("y", 1e9))
        if feed > mx:
            notes.append(f"feed clamped to machine max {mx:.0f} mm/min (chip load will be thinner)")
            feed = mx
    plunge = feed * mat["plunge"] if tool.type != "drill" else feed
    if machine is not None and plunge > machine.max_feed.get("z", 1e9):
        plunge = machine.max_feed["z"]
    stepdown = round(min(mat["stepdown"] * d, tool.flute_length), 3)
    if tool.type == "ball":
        stepdown = round(stepdown * 0.6, 3)
    cap = machine.stepdown_cap(key) if machine is not None else None
    if cap and stepdown > cap:
        notes.append(f"stepdown capped at {cap:g} mm for {key} on {machine.name} (wanted {stepdown:g}; machine setting)")
        stepdown = round(cap, 3)
    if mat.get("notes"):
        notes.append(mat["notes"])
    if key == "mild-steel" and machine is not None and machine.spindle["min"] > 3000:
        notes.append("this machine's spindle floor is too fast for steel with this tool")
    return {"material": key, "tool": tool.label(), "vc": round(vc, 1), "rpm": int(round(rpm, -2)),
            "chipload": round(cl, 4), "feed": int(round(feed, -1)), "plunge": int(round(plunge, -1)),
            "stepdown": stepdown, "stepover": mat["stepover"], "notes": notes}


def apply_feeds(tool: Tool, material: str, machine: Machine | None = None, **kw) -> Tool:
    """Copy of the tool with rpm/feed/plunge/stepdown/stepover set from feeds()."""
    from dataclasses import replace
    f = feeds(tool, material, machine, **kw)
    t = replace(tool, rpm=f["rpm"], feed=f["feed"], plunge=f["plunge"], stepdown=f["stepdown"], stepover=f["stepover"])
    t.notes = (tool.notes + " " if tool.notes else "") + f"[{material}: vc {f['vc']} m/min, fz {f['chipload']} mm]"
    return t


# =============================================================================
# Program + GRBL post
# =============================================================================
class Program:
    """Operations in run order. Each op carries the Setup it was made with; consecutive ops that share a setup run
    together. A change of setup is an operator step (flip or re-clamp the part, re-zero) unless both setups are on
    the 4th axis with the same mounting, in which case the post just turns A to the next index angle."""

    def __init__(self, setup: Setup, ops: Iterable[Op] = (), name: str = "program"):
        self.setup = setup
        self.ops: list[Op] = list(ops)
        self.name = name

    def add(self, op: Op) -> Op:
        if op.setup is None:
            op.setup = self.setup
        self.ops.append(op)
        return op

    def setup_of(self, op: Op) -> Setup:
        return op.setup or self.setup

    @property
    def setups(self) -> list[Setup]:
        out: list[Setup] = []
        for op in self.ops:
            st = self.setup_of(op)
            if not any(st is x for x in out):
                out.append(st)
        return out or [self.setup]

    @staticmethod
    def same_mounting(a: Setup, b: Setup) -> bool:
        """Two setups the operator doesn't touch between: both on the 4th axis with the same stock and axis."""
        return (a.rotary and b.rotary and a.machine is b.machine and a.axis == b.axis
                and a.model_stock.to_dict() == b.model_stock.to_dict() and a.origin_point() == b.origin_point())

    def assign_wcs(self) -> None:
        used = [s.wcs for s in self.setups if s.wcs]
        free = [f"G{n}" for n in range(54, 60) if f"G{n}" not in used]
        groups: list[Setup] = []
        for st in self.setups:
            twin = next((g for g in groups if self.same_mounting(g, st)), None)
            if st.wcs is None:
                st.wcs = twin.wcs if twin is not None and twin.wcs else (free.pop(0) if free else "G54")
            groups.append(st)

    @property
    def machine(self) -> Machine:
        """A program runs on one machine: the program setup's. Setups give frames, orientation and work offsets."""
        return self.setup.machine

    def time_minutes(self) -> float:
        return sum(op.time_minutes(self.machine) for op in self.ops)

    def bounds(self, setup: Setup | None = None):
        ops = [op for op in self.ops if op.moves and (setup is None or self.setup_of(op) is setup)]
        if not ops:
            return None
        lo = [1e18] * 3; hi = [-1e18] * 3
        for op in ops:
            a, b = op.bounds()
            lo = [min(x, y) for x, y in zip(lo, a)]; hi = [max(x, y) for x, y in zip(hi, b)]
        return tuple(lo), tuple(hi)

    def check(self) -> list[str]:
        w: list[str] = []
        if not self.ops:
            return ["program has no operations"]
        self.assign_wcs()
        m = self.machine
        for st in self.setups:
            tag = f"{st.name}: " if len(self.setups) > 1 else ""
            b = self.bounds(st)
            if b:
                (x0, y0, z0), (x1, y1, z1) = b
                axes = (("x", x0, x1),) if st.rotary else (("x", x0, x1), ("y", y0, y1), ("z", z0, z1))
                for axis, lo, hi in axes:
                    if hi - lo > m.travel.get(axis, 1e9) + 1e-6:
                        w.append(f"{tag}{axis.upper()} extent {hi - lo:.1f} mm exceeds machine travel {m.travel[axis]} mm")
                if not st.rotary and z0 < st.stock.bottom - 1e-6:
                    w.append(f"{tag}toolpath goes {st.stock.bottom - z0:.2f} mm below the stock bottom (into the spoilboard?)")
            if st.machine is not m and st.machine.name != m.name:
                w.append(f"{tag}set up for {st.machine.name} but the program runs on {m.name} (one machine per program)")
            if st.rotary:
                rot = m.rotary or {}
                if not m.rotary:
                    w.append(f"{tag}a 4th-axis setup, but {m.name} has no 4th axis")
                elif not rot.get("installed", True):
                    w.append(f"{tag}{m.name}: the 4th axis module is not marked installed")
                d = 2 * st.max_radius
                if rot.get("max_diameter") and d > rot["max_diameter"] + 1e-6:
                    w.append(f"{tag}stock swing Ø{d:.1f} mm exceeds the 4th axis capacity Ø{rot['max_diameter']} mm")
                L = st.stock.xmax - st.stock.xmin
                if rot.get("max_length") and L > rot["max_length"] + 1e-6:
                    w.append(f"{tag}stock length {L:.1f} mm exceeds the 4th axis capacity {rot['max_length']} mm")
        for op in self.ops:
            st = self.setup_of(op); t = op.tool
            if op.uses_a and not st.rotary:
                w.append(f"{op.name}: moves A but its setup is not on the 4th axis")
            if not (m.spindle["min"] <= t.rpm <= m.spindle["max"]):
                w.append(f"{op.name}: {t.label()} rpm {t.rpm} outside spindle range {m.spindle}")
            w += [f"{op.name}: {x}" for x in op.warnings]
        for k in getattr(self, "unused_overrides", []):
            w.append(f"override for '{k}' matches no operation (renamed or removed?)")
        tools = {op.tool.number for op in self.ops}
        if len(tools) > 1 and m.tool_change == "none":
            w.append(f"program uses {len(tools)} tools but machine tool_change is 'none'")
        return w

    def summary(self) -> str:
        self.assign_wcs()
        m = self.setup.machine
        lines = [f"Program '{self.name}' on {m.name} ({m.post} post): {len(self.ops)} ops in {len(self.setups)} setup(s), "
                 f"est. {self.time_minutes():.1f} min"]
        for st in self.setups:
            stk = st.stock
            ox, oy, oz = st.origin_point()
            kind = (f"4th axis A {st.a:g}°, stock swing Ø{2 * st.max_radius:.1f} × {stk.xmax - stk.xmin:.1f} mm"
                    if st.rotary else f"orient {st.orient}, stock {stk.size[0]:.1f} × {stk.size[1]:.1f} × {stk.size[2]:.1f} mm, top z={stk.top:.2f}")
            lines.append(f"{st.name} [{st.wcs}]: {kind}; WCS origin {st.origin} = setup ({ox:.2f}, {oy:.2f}, {oz:.2f}); "
                         f"safe z={st.safe_z:.2f}")
            for i, op in enumerate(self.ops, 1):
                if self.setup_of(op) is not st:
                    continue
                cut, rapid = op.lengths()
                b = op.bounds() if op.moves else None
                zr = f"z {b[0][2]:.2f}..{b[1][2]:.2f}" if b else "no moves"
                aa = ""
                if op.uses_a:
                    av = [mv[5] for mv in op.moves if len(mv) > 5]
                    aa = f" A {min(av):.0f}..{max(av):.0f}°"
                lines.append(f"  {i}. {op.name} [{op.kind}] {op.tool.label()} Ø{op.tool.diameter} — {len(op.moves)} moves, "
                             f"cut {cut:.0f} mm, {op.time_minutes(self.machine):.1f} min, {zr}{aa} "
                             + " ".join(f"{k}={v}" for k, v in op.params.items()))
        w = self.check()
        if w:
            lines.append("WARNINGS: " + "; ".join(w))
        return "\n".join(lines)

    def to_payload(self) -> dict[str, Any]:
        """For the viewer: everything in MODEL coordinates (rotary moves wrapped back onto the part)."""
        self.assign_wcs()
        setups = self.setups
        st0 = self.setup
        ox, oy, oz = st0.to_model([st0.origin_point()])[0]
        out_setups = []
        for st in setups:
            o = st.to_model([st.origin_point()])[0]
            out_setups.append({"name": st.name, "wcs": st.wcs, "rotary": st.rotary, "a": st.a, "orient": st.orient if not st.rotary else None,
                               "axis": list(st.axis) if st.rotary else None, "origin": [float(v) for v in o],
                               "tool_axis": list(st.tool_axis_model), "stock": st.model_stock.to_dict(),
                               "max_radius": getattr(st, "max_radius", None), "fixture": st.fixture_payload()})
        ops = []
        for op in self.ops:
            st = self.setup_of(op)
            si = next(i for i, x in enumerate(setups) if x is st)
            moves = op.moves
            if moves:
                P = np.array([m[1:4] for m in moves], dtype=float)
                if st.rotary:
                    A = np.array([m[5] if len(m) > 5 else st.a for m in moves], dtype=float)
                    M = np.empty_like(P)
                    for av in np.unique(A):
                        sel = A == av
                        M[sel] = st.to_model(P[sel], a=float(av))
                else:
                    M = st.to_model(P)
                moves = [[m[0], round(float(q[0]), 4), round(float(q[1]), 4), round(float(q[2]), 4), m[4]] + ([m[5]] if len(m) > 5 else [])
                         for m, q in zip(op.moves, M)]
            ops.append({"name": op.name, "kind": op.kind, "color": op.color, "tool": asdict(op.tool), "setup": si,
                        "key": op.key, "overrides": op.overrides, "defaults": op.defaults,
                        "base_tool": asdict(op.base_tool) if op.base_tool is not None else None,
                        "moves": moves, "time": round(op.time_minutes(self.machine), 2), "params": op.params, "uses_a": op.uses_a})
        return {
            "name": self.name, "machine": st0.machine.name,
            "stock": st0.model_stock.to_dict(), "origin": [float(ox), float(oy), float(oz)], "safe_z": st0.safe_z,
            "time": round(self.time_minutes(), 2), "warnings": self.check(), "setups": out_setups, "ops": ops,
        }

    # ---------------------------------------------------------------- post
    def gcode(self) -> str:
        post = self.setup.machine.post
        if post == "makera":
            return post_makera(self)
        if post in ("haas", "linuxcnc"):
            return _emit(self, post)
        return post_grbl(self)


def _circle3(p1, p2, p3):
    """Circle through three points -> (cx, cy, r) or None if collinear."""
    ax, ay = p1; bx, by = p2; cx, cy = p3
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        return None
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    return ux, uy, math.hypot(ax - ux, ay - uy)


def _arc_ok(pts, tol):
    """Do these XY points (>=3) lie on one arc within tol, with fine enough chords and consistent turning?
    Returns (cx, cy, cw) or None."""
    c = _circle3(pts[0], pts[len(pts) // 2], pts[-1])
    if c is None:
        return None
    cx, cy, r = c
    if r < 0.05 or r > 5000:
        return None
    for p in pts:
        if abs(math.hypot(p[0] - cx, p[1] - cy) - r) > tol:
            return None
    # chord sagitta (polyline vs arc deviation) and turning consistency, sweep <= 180°
    sign = 0.0
    sweep = 0.0
    for a, b in zip(pts, pts[1:]):
        ch = math.dist(a, b)
        if ch > 2 * r:
            return None
        if r - math.sqrt(max(r * r - ch * ch / 4, 0.0)) > tol:
            return None
        cr = (a[0] - cx) * (b[1] - cy) - (a[1] - cy) * (b[0] - cx)
        if abs(cr) < 1e-12:
            return None
        if sign == 0.0:
            sign = 1.0 if cr > 0 else -1.0
        elif (cr > 0) != (sign > 0):
            return None
        sweep += 2 * math.asin(min(1.0, ch / (2 * r)))
    if sweep > math.pi + 1e-6:
        return None
    return cx, cy, sign < 0      # cw = clockwise (negative cross) in G17


def _merge_collinear(pts, tol):
    """Drop interior points that lie on the straight line between their neighbours (within tol)."""
    if len(pts) < 3:
        return pts
    out = [pts[0]]
    anchor = pts[0]
    for k in range(1, len(pts) - 1):
        a, b, c = anchor, pts[k], pts[k + 1]
        vx, vy = c[0] - a[0], c[1] - a[1]
        ln = math.hypot(vx, vy)
        if ln < 1e-12:
            continue
        dev = abs((b[0] - a[0]) * vy - (b[1] - a[1]) * vx) / ln
        # also require b to lie between a and c along the line (no reversal)
        along = ((b[0] - a[0]) * vx + (b[1] - a[1]) * vy) / ln
        if dev <= tol and 0 <= along <= ln:
            continue
        out.append(b); anchor = b
    out.append(pts[-1])
    return out


def _with_arcs(moves, tol):
    """Yield moves, replacing runs of coplanar feed moves that lie on a circle with ('arc', x, y, z, f, cx, cy, cw)."""
    if tol is None:
        for m in moves:
            yield tuple(m)
        return
    n = len(moves)
    i = 0
    while i < n:
        m = moves[i]
        if m[0] == RAPID or i == 0:
            yield tuple(m); i += 1; continue
        # candidate run: feed moves at constant z and feed, starting from the previous position
        z, f = m[3], m[4]
        j = i
        while j < n and moves[j][0] != RAPID and abs(moves[j][3] - z) < 1e-9 and moves[j][4] == f:
            j += 1
        run = [(moves[i - 1][1], moves[i - 1][2])] + [(mv[1], mv[2]) for mv in moves[i:j]]
        # greedy arcs on the RAW run first (collinear merging would coarsen fine arc chords past the
        # sagitta test); straight leftovers are merged afterwards
        items: list[tuple] = []
        k = 0   # index into run (run[0] is the start position, already emitted)
        while k < len(run) - 1:
            best = None
            e = k + 3
            while e <= len(run):
                res = _arc_ok(run[k:e], tol)
                if res is None:
                    break
                best = (e, res)
                e += 1
            if best is not None:
                e, (cx, cy, cw) = best
                x, y = run[e - 1]
                items.append(("arc", x, y, z, f, cx, cy, cw))
                k = e - 1
            else:
                x, y = run[k + 1]
                items.append((FEED, x, y, z, f))
                k += 1
        # merge straight stretches
        start_xy = run[0]
        pending: list[tuple[float, float]] = []
        for it in items:
            if it[0] == FEED:
                pending.append((it[1], it[2]))
                continue
            for x, y in _merge_collinear([start_xy] + pending, tol * 0.5)[1:] if pending else []:
                yield (FEED, x, y, z, f)
            pending = []
            yield it
            start_xy = (it[1], it[2])
        for x, y in _merge_collinear([start_xy] + pending, tol * 0.5)[1:] if pending else []:
            yield (FEED, x, y, z, f)
        i = j


def _fmt(v: float) -> str:
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


# =============================================================================
# Posts. One emitter, four dialects:
#   grbl   : GRBL 1.1 (and A as a plain linear-style axis for grblHAL-type 4-axis controllers)
#   haas   : Haas NGC / Classic (Fanuc-style): % wrapper, O-number, ( ) comments, every number with a decimal point,
#            Tn M06 + G43 Hn tool length comp, G53 G0 Z0. retracts, M00 operator stops, G93 inverse-time feed for
#            moves with the rotary axis, M30.
#   linuxcnc: LinuxCNC rs274ngc: % wrapper, ( ) comments and (MSG, ...) operator messages, G64 P path blending,
#            Tn M6 G43 Hn (hal_manualtoolchange prompts on M6 when there is no ATC), G53 G0 Z0, G93 for rotary, M2.
#   makera : Makera Z1 / Carvera family firmware (Smoothieware-based, read from MakeraZ1Firmware):
#            lines <= 63 characters (longer lines are truncated by the controller), no N numbers, ';' comments,
#            M6 Tn runs the whole manual change (move, wait for the button, measure tool length) or the ATC,
#            no canned cycles / coolant / G93; arcs must not move A; G28 goes to the clearance position;
#            M600 suspends the file. Feed for moves with A follows the firmware's own rule (_makera_feed).
# =============================================================================
MAKERA_MAX_LINE = 63


def _makera_perimeter(y: float, z: float) -> float:
    r = math.hypot(y, z)
    return 2 * math.pi * (r if r > 1 else 1.0) + 30.0


def _makera_feed(dxyz: float, da: float, y: float, z: float, t_min: float, a_max: float) -> float:
    """F word so the Makera firmware takes `t_min` minutes for this move (Robot.cpp: path length is XYZ only; an
    A-only move reads F as deg/min, raised by 360/perimeter below a 360 mm perimeter; on mixed moves A's surface
    speed, A rate x perimeter / 360, is held to F). perimeter = 2*pi*r + 30 with r from WCS Y/Z."""
    per = _makera_perimeter(y, z)
    da = abs(da)
    if da < 1e-9:
        return dxyz / t_min
    if dxyz < 1e-9:                                    # A only: deg/min, with the firmware's small-radius boost
        f = da / t_min / max(1.0, 360.0 / per)
        return min(f, a_max)
    s_a = da * per / 360.0
    return max(dxyz, s_a) / t_min


def _fmt_dec(v: float) -> str:
    """Fanuc-style number: always carries a decimal point (Haas reads "X10" as ten least-increments, not 10 mm)."""
    t = _fmt(v)
    return t if "." in t else t + "."


def _emit(prog: Program, dialect: str) -> str:
    mk = dialect == "makera"
    fan = dialect in ("haas", "linuxcnc")              # Fanuc-style family
    hs = dialect == "haas"
    _fmt = _fmt_dec if fan else globals()["_fmt"]      # noqa: F841 - local shadow: numbers in this post carry a decimal point
    prog.assign_wcs()
    m0 = prog.setup.machine
    out: list[str] = []
    g93 = False                                        # inverse-time feed mode is active (fan dialects, rotary moves)

    def comment(text: str):
        text = text.replace("(", "[").replace(")", "]")
        if mk:                                          # the dispatcher hoists G90/G91 even out of comments
            text = text.replace("G90", "G 90").replace("G91", "G 91")
            out.append(("; " + text)[:MAKERA_MAX_LINE])
        elif fan:
            out.append("(" + text.replace(";", ",") + ")")
        else:
            out.append("; " + text)

    def line(t: str):
        if mk and len(t) > MAKERA_MAX_LINE:
            raise CamError(f"G-code line longer than {MAKERA_MAX_LINE} characters for the Makera controller: {t}")
        out.append(t)

    def home_z():
        """Retract to the machine's Z home (Fanuc family) or the setup's clearance height."""
        return "G53 G0 Z0." if hs else "G53 G0 Z0"

    if fan:
        line("%")
        if hs:
            pname = "".join(ch for ch in prog.name.upper() if ch.isalnum() or ch in " -_")[:30].strip() or "AGENTICCAD"
            line(f"O01000 ({pname})")
    comment(f"AgenticCAD {dialect} post: {prog.name}")
    comment(f"machine: {m0.name}")
    comment(f"est. {prog.time_minutes():.1f} min, {len(prog.setups)} setup(s)")
    for t in sorted({op.tool.label() + f" D{_fmt(op.tool.diameter)}" for op in prog.ops}):
        comment("tool " + t)
    for w in prog.check():
        comment("WARNING: " + w)
    if mk:
        line("G21 G90 G17")
    elif fan:
        line("G21 G17 G40 G49 G80 G90 G94")             # metric, XY plane, no comp, no length offset, no canned cycle
        if dialect == "linuxcnc":
            line("G64 P0.01")                           # path blending with a 0.01 mm tolerance
    else:
        line("G21 G90 G94 G17")
    out.extend(m0.program_start)
    cur_tool = None
    prev_setup: Setup | None = None
    last_f = None
    cur_a = 0.0
    last: list = [None, None, None, None]
    spindle_on = False
    for op in prog.ops:
        st = prog.setup_of(op)
        m = m0
        ox, oy, oz = st.origin_point()
        safe = st.safe_z - oz
        clear = st.clearance_z - oz
        aw = (m.rotary or {}).get("axis", "A")
        a_max = float((m.rotary or {}).get("max_speed", 3600.0))
        # ---- setup change
        if prev_setup is None or st is not prev_setup:
            if prev_setup is not None and Program.same_mounting(prev_setup, st):
                line(f"G0 Z{_fmt(clear)}")
                comment(f"{st.name}: index A to {_fmt(st.a)}")
            else:
                if prev_setup is not None:
                    line("M5")
                    spindle_on = False
                    if mk:
                        line("G28")
                    elif fan:
                        if m.coolant:
                            line("M9")
                        line(home_z())
                    else:
                        line(f"G0 Z{_fmt(prev_setup.clearance_z - prev_setup.origin_point()[2])}")
                    msg = (f"{st.name}: mount the part on the 4th axis" if st.rotary else
                           f"{st.name}: {'flip' if st.orient == 'bottom' else 're-clamp'} part, orient {st.orient}")
                    comment(msg)
                    comment(f"zero {st.wcs} at {st.origin}, then resume")
                    if mk:
                        line("M600")
                    elif hs:
                        line("M00")
                    elif dialect == "linuxcnc":
                        line(f"(MSG, {msg} - zero {st.wcs} then resume)")
                        line("M0")
                    else:
                        line(f"(MSG, {msg} - zero {st.wcs} then resume)")
                        line("M0")
                else:
                    comment(f"{st.name}: zero {st.wcs} at {st.origin}" + (" on the rotary centreline" if st.rotary else ""))
                line(st.wcs)
                if prev_setup is not None:
                    line(f"G0 Z{_fmt(clear)}")
            if st.rotary:
                line(f"G0 {aw}{_fmt(st.a)}")
            prev_setup = st
            cur_a = st.a
            last = [None, None, None, cur_a]               # positions restart in the new setup's frame
            last_f = None
        t = op.tool
        rpm = min(max(t.rpm, m.spindle["min"]), m.spindle["max"])
        comment(f"{op.name} [{op.kind}] {t.label()} D{_fmt(t.diameter)}")
        if cur_tool != t.number:
            if mk:
                if cur_tool is not None:
                    line("M5")
                line(f"M6 T{t.number}")                          # firmware: clearance, change/measure, return
            elif fan:
                if g93:
                    line("G94"); g93 = False
                if cur_tool is not None:
                    line("M5")
                    if m.coolant:
                        line("M9")
                    line(home_z())
                if m.tool_change == "pause":                     # no changer: stop so the operator can swap the tool
                    comment(f"change tool to {t.label()} D{_fmt(t.diameter)}")
                    if dialect == "linuxcnc":
                        line(f"(MSG, Change tool to {t.label()} D{_fmt(t.diameter)})")
                    line("M00" if hs else "M0")
                elif m.tool_change == "none" and cur_tool is not None:
                    comment(f"tool change to T{t.number} required: machine has tool_change=none")
                line(f"T{t.number} M6" if dialect == "linuxcnc" else f"T{t.number} M06")
                line(f"S{int(rpm)} M3")
                spindle_on = True
                line(f"G43 H{t.number} Z{_fmt(safe)}")          # length offset of this tool, applied on the first Z move
                if m.coolant:
                    line("M8")
                cur_tool = t.number
                last = [None, None, safe, cur_a]
                last_f = None
            else:
                if cur_tool is not None:
                    line("M5")
                    line(f"G0 Z{_fmt(clear)}")
                    if m.tool_change == "pause":
                        line(f"(MSG, Change tool to {t.label()} D{_fmt(t.diameter)})")
                        line(f"M6 T{t.number}")
                        line("M0")
                    elif m.tool_change == "split":
                        comment(f"TOOLCHANGE T{t.number}: split file here")
                        line(f"M6 T{t.number}")
                    else:
                        comment(f"tool change to T{t.number} required: machine has tool_change=none")
                else:
                    line(f"T{t.number}")
            if not fan:
                line(f"M3 S{int(rpm)}")
                spindle_on = True
                if m.coolant and not mk:
                    line("M8")
                line(f"G0 Z{_fmt(safe)}")
                cur_tool = t.number
                last = [None, None, safe, cur_a]
                last_f = None
        elif not spindle_on:                              # same tool after an operator pause: restart the spindle
            line(f"M3 S{int(rpm)}")
            spindle_on = True
            line(f"G0 Z{_fmt(safe)}")
            last = [None, None, safe, cur_a]
            last_f = None
        use_arcs = m.arcs and not op.uses_a
        moves = op.moves
        for item in _with_arcs(moves, m.arc_tolerance if use_arcs else None):
            if item[0] == "arc":
                _, x, y, z, f, cx, cy, cw = item
                x -= ox; y -= oy; z -= oz; cx -= ox; cy -= oy
                sx = last[0] if last[0] is not None else x; sy = last[1] if last[1] is not None else y
                fw = ""
                if f and f != last_f:
                    fw = f" F{_fmt(f)}"; last_f = f
                zw = f" Z{_fmt(z)}" if last[2] is None or abs(z - last[2]) > 1e-6 else ""
                line(f"{'G2' if cw else 'G3'} X{_fmt(x)} Y{_fmt(y)}{zw} I{_fmt(cx - sx)} J{_fmt(cy - sy)}{fw}")
                last = [x, y, z, last[3]]
                continue
            k, x, y, z, f = item[:5]
            a = item[5] if len(item) > 5 else None
            x -= ox; y -= oy; z -= oz
            words = []
            if last[0] is None or abs(x - last[0]) > 1e-6: words.append(f"X{_fmt(x)}")
            if last[1] is None or abs(y - last[1]) > 1e-6: words.append(f"Y{_fmt(y)}")
            if last[2] is None or abs(z - last[2]) > 1e-6: words.append(f"Z{_fmt(z)}")
            da = 0.0
            if a is not None and (last[3] is None or abs(a - last[3]) > 1e-6):
                da = a - (last[3] if last[3] is not None else a)
                words.append(f"{aw}{_fmt(a)}")
            if not words:
                continue
            if k == RAPID:
                line("G0 " + " ".join(words))
            else:
                fw = ""
                if a is not None and abs(da) > 1e-9:
                    # feed so the real tool path (XYZ plus arc swept by A at this radius) runs at f mm/min
                    px = [last[i] if last[i] is not None else v for i, v in enumerate((x, y, z))]
                    dxyz = math.dist(px, (x, y, z))
                    r = max(math.hypot(y, z), math.hypot(px[1], px[2]))
                    true_len = math.hypot(dxyz, math.radians(abs(da)) * r)
                    t_min = max(true_len / max(f, 1.0), 1e-6)
                    if mk:
                        fv = _makera_feed(dxyz, da, y, z, t_min, a_max)
                    elif fan:
                        if not g93:
                            line("G93"); g93 = True                       # inverse time: F = 1 / minutes for this move
                        fv = 1.0 / t_min
                    else:
                        fv = math.hypot(dxyz, abs(da)) / t_min          # grblHAL-style: degrees count as length
                    fv = round(fv, 3 if fan else 1)
                    if fan or fv != last_f:                              # G93 needs an F word on every block
                        fw = f" F{_fmt(fv)}"; last_f = fv
                else:
                    if g93:
                        line("G94"); g93 = False; last_f = None
                    if f and f != last_f:
                        fw = f" F{_fmt(f)}"; last_f = f
                line("G1 " + " ".join(words) + fw)
            last = [x, y, z, a if a is not None else last[3]]
        if op.uses_a:
            cur_a = last[3]
    if g93:
        line("G94")
    line("M5")
    if mk:
        line("G28")
    elif fan:
        if m0.coolant:
            line("M9")
        line(home_z())
    else:
        if m0.coolant:
            line("M9")
        st = prog.setup_of(prog.ops[-1]) if prog.ops else prog.setup
        line(f"G0 Z{_fmt(st.clearance_z - st.origin_point()[2])}")
    out.extend(m0.program_end)
    if dialect == "linuxcnc":
        line("M2")
    else:
        line("M30")
    if fan:
        line("%")
    return "\n".join(out) + "\n"


def post_grbl(prog: Program) -> str:
    return _emit(prog, "grbl")


def post_makera(prog: Program) -> str:
    return _emit(prog, "makera")


# 4th-axis toolpaths live in cam_rotary (imported last: it builds on everything above)
from cam_rotary import radial_map, rotary_finish, rotary_rough, rotary_wrap  # noqa: E402,F401


# ---------------------------------------------------------------- per-operation overrides
# The CAM tab writes one line at the top of cam.py:  overrides({"Pocket": {"feed": 500}, "Face": {"rpm": 11000}})
# Every operation created afterwards looks itself up by key (its name, "#2", "#3"… for repeats) and runs with those
# tool values instead of the tool's (or the script's explicit stepdown/stepover). Everything else comes from the tool.
OVERRIDE_FIELDS = ("rpm", "feed", "plunge", "stepdown", "stepover")
OP_FIELDS = {"face": OVERRIDE_FIELDS, "pocket": OVERRIDE_FIELDS, "adaptive": OVERRIDE_FIELDS,
             "contour": ("rpm", "feed", "plunge", "stepdown"), "drill": ("rpm", "plunge"),
             "parallel3d": ("rpm", "feed", "plunge", "stepover"), "rotary_rough": OVERRIDE_FIELDS,
             "rotary_finish": ("rpm", "feed", "plunge", "stepover"), "rotary_wrap": OVERRIDE_FIELDS}
_OV: dict[str, Any] = {"table": {}, "seen": {}, "depth": 0, "used": set()}


def overrides(table: dict | None = None) -> None:
    """Per-operation tool overrides, keyed by operation name ("Pocket", or "Pocket#2" for the second op with that name):
    {"Pocket": {"rpm": 11000, "feed": 600, "plunge": 150, "stepdown": 0.8, "stepover": 0.3}} (stepover = fraction of Ø).
    Written by the CAM tab; put it before the operations. Values not given come from the tool / the script."""
    bad = []
    for k, v in (table or {}).items():
        if not isinstance(v, dict):
            raise CamError(f"overrides: '{k}' must map to a dict of {OVERRIDE_FIELDS}")
        for f_, x in v.items():
            if f_ not in OVERRIDE_FIELDS:
                bad.append(f"{k}.{f_}")
            elif not isinstance(x, (int, float)) or x <= 0 or (f_ == "stepover" and x > 1):
                raise CamError(f"overrides: {k}.{f_} = {x!r} (must be > 0{'; stepover is a fraction of Ø ≤ 1' if f_ == 'stepover' else ''})")
    if bad:
        raise CamError(f"overrides: unknown field(s) {bad}; use {OVERRIDE_FIELDS}")
    _OV["table"] = {k: dict(v) for k, v in (table or {}).items()}


def reset_overrides() -> None:
    """Start of a CAM script run: no overrides, op keys numbered from 1 again."""
    _OV.update(table={}, seen={}, depth=0, used=set())
    _FIXTURES.update(table={})


_FIXTURES: dict = {"table": {}}


def fixtures(table: dict | None = None) -> None:
    """Per-setup fixture choices, keyed by setup name: {"Top": {"name": "Makera low-profile vise", "at": [0, 0], "rot": 0,
    "params": {"opening": 40}}}. Written by the CAM tab's fixture picker; put it before the setups. A setup named here uses
    this fixture instead of the one in its Setup(...) call ("name": null = bare table)."""
    for k, v in (table or {}).items():
        if not isinstance(v, dict):
            raise CamError(f"fixtures: '{k}' must map to a dict with name / at / rot / params")
    _FIXTURES["table"] = dict(table or {})


def unused_overrides() -> list[str]:
    return [k for k in _OV["table"] if k not in _OV["used"]]


def _op_defaults(kind: str, tool: Tool, a: dict) -> dict:
    sd, so = a.get("stepdown"), a.get("stepover")
    if kind == "adaptive":
        so = 0.15 if so is None else so
        if not sd and a.get("z_top") is not None and a.get("z_bottom") is not None:
            sd = max(min(tool.flute_length * 0.9, a["z_top"] - a["z_bottom"]), 0.1)
    elif kind == "parallel3d":
        so = so or min(tool.stepover, 0.2)
    elif kind == "rotary_finish":
        so = so or (0.15 if tool.type == "ball" else tool.stepover)
    d = {"rpm": tool.rpm, "feed": tool.feed, "plunge": tool.plunge, "stepdown": sd or tool.stepdown, "stepover": so or tool.stepover}
    return {k: round(float(d[k]), 4) for k in OP_FIELDS.get(kind, OVERRIDE_FIELDS)}


def _overridable(fn, kind: str):
    import functools
    import inspect
    from dataclasses import replace
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args, **kw):
        if _OV["depth"]:                                   # an op built inside another (rotary_wrap -> pocket): pass through
            return fn(*args, **kw)
        b = sig.bind(*args, **kw)
        name = b.arguments.get("name", sig.parameters["name"].default)
        n = _OV["seen"][name] = _OV["seen"].get(name, 0) + 1
        key = name if n == 1 else f"{name}#{n}"
        tool = b.arguments["tool"]
        fields = OP_FIELDS.get(kind, OVERRIDE_FIELDS)
        defaults = _op_defaults(kind, tool, b.arguments)
        ov = {k: float(v) for k, v in (_OV["table"].get(key) or {}).items() if k in fields}
        if key in _OV["table"]:
            _OV["used"].add(key)
        if ov:
            b.arguments["tool"] = replace(tool, **ov)
            for k in ("stepdown", "stepover"):
                if k in ov and k in sig.parameters:
                    b.arguments[k] = ov[k]
        _OV["depth"] += 1
        try:
            op = fn(*b.args, **b.kwargs)
        finally:
            _OV["depth"] -= 1
        op.key, op.base_tool, op.overrides, op.defaults = key, tool, ov, defaults
        return op
    wrapper.__wrapped_op__ = True
    return wrapper


face = _overridable(face, "face")
contour = _overridable(contour, "contour")
pocket = _overridable(pocket, "pocket")
adaptive = _overridable(adaptive, "adaptive")
drill = _overridable(drill, "drill")
parallel3d = _overridable(parallel3d, "parallel3d")
rotary_rough = _overridable(rotary_rough, "rotary_rough")
rotary_finish = _overridable(rotary_finish, "rotary_finish")
rotary_wrap = _overridable(rotary_wrap, "rotary_wrap")
PUBLIC_API.append("overrides")
