"""
CAM kernel for AgentCAD — 2.5D / simple 3D toolpaths from the exact B-rep, GRBL post-processor.

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
    tool_change: str = "pause"              # pause (M0 + message) | none (single tool assumed) | split (one file per tool)
    coolant: bool = False
    safe_z: float = 5.0                     # clearance above stock top for rapids
    clearance_z: float = 15.0               # height for tool changes / start / end
    program_start: list[str] = field(default_factory=list)
    program_end: list[str] = field(default_factory=list)
    arcs: bool = True                       # emit G2/G3 where the path is circular
    arc_tolerance: float = 0.05             # max deviation (mm) between polyline and fitted arc
    notes: str = ""


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
        return asdict(self)


ORIGINS = {
    "stock-top-left": lambda s: (s.xmin, s.ymin, s.zmax),      # front-left corner, stock top (most common on routers)
    "stock-top-center": lambda s: ((s.xmin + s.xmax) / 2, (s.ymin + s.ymax) / 2, s.zmax),
    "stock-bottom-left": lambda s: (s.xmin, s.ymin, s.zmin),
    "stock-bottom-center": lambda s: ((s.xmin + s.xmax) / 2, (s.ymin + s.ymax) / 2, s.zmin),
    "model-origin": lambda s: (0.0, 0.0, 0.0),
}


@dataclass
class Setup:
    machine: Machine
    stock: Stock
    origin: Any = "stock-top-left"          # one of ORIGINS or an (x, y, z) tuple in model coords
    safe_z: float | None = None             # absolute model Z for rapids; default stock top + machine.safe_z
    clearance_z: float | None = None
    name: str = "Setup 1"

    def __post_init__(self):
        if self.safe_z is None:
            self.safe_z = self.stock.top + self.machine.safe_z
        if self.clearance_z is None:
            self.clearance_z = self.stock.top + self.machine.clearance_z

    def origin_point(self) -> tuple[float, float, float]:
        if isinstance(self.origin, str):
            if self.origin not in ORIGINS:
                raise CamError(f"unknown origin '{self.origin}'; use one of {list(ORIGINS)} or an (x, y, z) tuple")
            return ORIGINS[self.origin](self.stock)
        x, y, z = self.origin
        return (float(x), float(y), float(z))


# =============================================================================
# Toolpath primitives
# =============================================================================
RAPID, FEED, PLUNGE = 0, 1, 2


@dataclass
class Op:
    name: str
    kind: str
    tool: Tool
    moves: list[list[float]] = field(default_factory=list)   # [kind, x, y, z, feed]
    color: str = "#4ea1ff"
    params: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def lengths(self) -> tuple[float, float]:
        cut = rapid = 0.0
        prev = None
        for m in self.moves:
            if prev is not None:
                d = math.dist(prev[1:4], m[1:4])
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
                d = math.dist(prev[1:4], m[1:4])
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
        self.pos: tuple[float, float, float] | None = None
        mf = setup.machine.max_feed
        self.feed = min(tool.feed, mf.get("x", tool.feed), mf.get("y", tool.feed))
        self.plunge = min(tool.plunge, mf.get("z", tool.plunge))
        if self.feed < tool.feed or self.plunge < tool.plunge:
            op.warnings.append(f"feed clamped to machine max ({self.feed:.0f}/{self.plunge:.0f} mm/min)")

    def _add(self, kind, x, y, z, f=0.0):
        self.op.moves.append([kind, round(x, 4), round(y, 4), round(z, 4), round(f, 1)])
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
    inner_ok = region.buffer(-tool.radius + 1e-6)   # where the tool centre may travel at depth
    levels = _levels(z_top, z_bottom, stepdown or tool.stepdown)
    op.params = dict(z_top=z_top, z_bottom=z_bottom, stepover=so, passes=len(levels), rings=len(shells))
    for z in levels:
        first = True
        for ps in reversed(shells):        # innermost first
            for q in ps:
                q = orient(q, 1.0)
                rings = [_ring_coords(q.exterior, False)] + [_ring_coords(h, True) for h in q.interiors]
                if not climb:
                    rings = [r[::-1] for r in rings]
                for ring in rings:
                    start = ring[0]
                    if first or pb.pos is None or pb.pos[2] > z + 1e-6 or \
                            not inner_ok.covers(LineString([pb.pos[:2], start])):
                        pb.rapid_xy(*start); pb.plunge_to(z); first = False
                    else:
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
    count = 0
    for it in items:
        if isinstance(it, Hole):
            x, y = it.x, it.y
            zt = it.z_top if z_top is None else z_top
            zb = it.z_bottom if z_bottom is None else z_bottom
            if it.through and z_bottom is None:
                zb -= tool.diameter * 0.3 + 0.5    # break through cleanly
            if it.diameter < tool.diameter - 1e-6:
                op.warnings.append(f"hole Ø{it.diameter:.2f} at ({x:.1f},{y:.1f}) is smaller than tool Ø{tool.diameter}")
        else:
            x, y = float(it[0]), float(it[1])
            if z_top is None or z_bottom is None:
                raise CamError("drill: z_top and z_bottom are required for (x, y) points")
            zt, zb = z_top, z_bottom
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
    op.params = dict(holes=count, peck=peck)
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


PUBLIC_API = ["Stock.from_model", "Stock.block", "Setup", "Program", "face", "contour", "pocket", "adaptive", "drill",
              "parallel3d", "rest_region", "section", "silhouette", "stock_minus", "holes", "face_polygon", "circle",
              "rect", "feeds", "apply_feeds"]


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
    def __init__(self, setup: Setup, ops: Iterable[Op] = (), name: str = "program"):
        self.setup = setup
        self.ops: list[Op] = list(ops)
        self.name = name

    def add(self, op: Op) -> Op:
        self.ops.append(op)
        return op

    def time_minutes(self) -> float:
        return sum(op.time_minutes(self.setup.machine) for op in self.ops)

    def bounds(self):
        if not self.ops:
            return None
        lo = [1e18] * 3; hi = [-1e18] * 3
        for op in self.ops:
            if not op.moves:
                continue
            a, b = op.bounds()
            lo = [min(x, y) for x, y in zip(lo, a)]; hi = [max(x, y) for x, y in zip(hi, b)]
        return tuple(lo), tuple(hi)

    def check(self) -> list[str]:
        w: list[str] = []
        m = self.setup.machine
        if not self.ops:
            return ["program has no operations"]
        b = self.bounds()
        if b:
            (x0, y0, z0), (x1, y1, z1) = b
            for axis, lo, hi in (("x", x0, x1), ("y", y0, y1), ("z", z0, z1)):
                if hi - lo > m.travel.get(axis, 1e9) + 1e-6:
                    w.append(f"{axis.upper()} extent {hi - lo:.1f} mm exceeds machine travel {m.travel[axis]} mm")
            st = self.setup.stock
            if z0 < st.bottom - 1e-6:
                w.append(f"toolpath goes {st.bottom - z0:.2f} mm below the stock bottom (into the spoilboard?)")
        for op in self.ops:
            t = op.tool
            if not (m.spindle["min"] <= t.rpm <= m.spindle["max"]):
                w.append(f"{op.name}: {t.label()} rpm {t.rpm} outside spindle range {m.spindle}")
            w += [f"{op.name}: {x}" for x in op.warnings]
        tools = {op.tool.number for op in self.ops}
        if len(tools) > 1 and m.tool_change == "none":
            w.append(f"program uses {len(tools)} tools but machine tool_change is 'none'")
        return w

    def summary(self) -> str:
        st = self.setup.stock
        ox, oy, oz = self.setup.origin_point()
        lines = [f"Program '{self.name}' on {self.setup.machine.name} ({self.setup.machine.controller}): "
                 f"{len(self.ops)} ops, est. {self.time_minutes():.1f} min",
                 f"stock {st.size[0]:.1f} × {st.size[1]:.1f} × {st.size[2]:.1f} mm, top z={st.top:.2f}; "
                 f"WCS origin {self.setup.origin} = model ({ox:.2f}, {oy:.2f}, {oz:.2f}); safe z={self.setup.safe_z:.2f}"]
        for i, op in enumerate(self.ops, 1):
            cut, rapid = op.lengths()
            b = op.bounds() if op.moves else None
            zr = f"z {b[0][2]:.2f}..{b[1][2]:.2f}" if b else "no moves"
            lines.append(f"  {i}. {op.name} [{op.kind}] {op.tool.label()} Ø{op.tool.diameter} — {len(op.moves)} moves, "
                         f"cut {cut:.0f} mm, {op.time_minutes(self.setup.machine):.1f} min, {zr} "
                         + " ".join(f"{k}={v}" for k, v in op.params.items()))
        w = self.check()
        if w:
            lines.append("WARNINGS: " + "; ".join(w))
        return "\n".join(lines)

    def to_payload(self) -> dict[str, Any]:
        ox, oy, oz = self.setup.origin_point()
        return {
            "name": self.name, "machine": self.setup.machine.name,
            "stock": self.setup.stock.to_dict(), "origin": [ox, oy, oz], "safe_z": self.setup.safe_z,
            "time": round(self.time_minutes(), 2), "warnings": self.check(),
            "ops": [{"name": op.name, "kind": op.kind, "color": op.color, "tool": asdict(op.tool),
                     "moves": op.moves, "time": round(op.time_minutes(self.setup.machine), 2),
                     "params": op.params} for op in self.ops],
        }

    # ---------------------------------------------------------------- post
    def gcode(self) -> str:
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


def post_grbl(prog: Program) -> str:
    m = prog.setup.machine
    ox, oy, oz = prog.setup.origin_point()
    st = prog.setup.stock
    out: list[str] = []
    out.append(f"; AgentCAD GRBL post — {prog.name} — machine: {m.name}")
    out.append(f"; stock {st.size[0]:.1f} x {st.size[1]:.1f} x {st.size[2]:.1f} mm, WCS origin: {prog.setup.origin}")
    out.append(f"; est. time {prog.time_minutes():.1f} min; tools: " + ", ".join(sorted({op.tool.label() for op in prog.ops})))
    for w in prog.check():
        out.append(f"; WARNING: {w}")
    out.append("G21 G90 G94 G17")   # mm, absolute, feed/min, XY plane
    out.append("G54")
    out += m.program_start
    safe = prog.setup.safe_z - oz
    clear = prog.setup.clearance_z - oz
    cur_tool = None
    last_f = None
    last = [None, None, None]
    for op in prog.ops:
        t = op.tool
        rpm = min(max(t.rpm, m.spindle["min"]), m.spindle["max"])
        out.append(f"; --- {op.name} [{op.kind}] {t.label()} D{_fmt(t.diameter)}")
        if cur_tool != t.number:
            if cur_tool is not None:
                out.append("M5")
                out.append(f"G0 Z{_fmt(clear)}")
                if m.tool_change == "pause":
                    out.append(f"(MSG, Change tool to {t.label()} D{_fmt(t.diameter)})")
                    out.append(f"M6 T{t.number}")
                    out.append("M0")
                elif m.tool_change == "split":
                    out.append(f"; TOOLCHANGE T{t.number} — split file here")
                    out.append(f"M6 T{t.number}")
                else:
                    out.append(f"; tool change to T{t.number} required — machine has tool_change=none")
            else:
                out.append(f"T{t.number}")
            out.append(f"M3 S{int(rpm)}")
            if m.coolant:
                out.append("M8")
            out.append(f"G0 Z{_fmt(safe)}")
            last = [None, None, safe]
            cur_tool = t.number
        for item in _with_arcs(op.moves, m.arc_tolerance if m.arcs else None):
            if item[0] == "arc":
                _, x, y, z, f, cx, cy, cw = item
                x -= ox; y -= oy; z -= oz; cx -= ox; cy -= oy
                sx = last[0] if last[0] is not None else x; sy = last[1] if last[1] is not None else y
                fw = ""
                if f and f != last_f:
                    fw = f" F{_fmt(f)}"; last_f = f
                zw = f" Z{_fmt(z)}" if last[2] is None or abs(z - last[2]) > 1e-6 else ""
                out.append(f"{'G2' if cw else 'G3'} X{_fmt(x)} Y{_fmt(y)}{zw} I{_fmt(cx - sx)} J{_fmt(cy - sy)}{fw}")
                last = [x, y, z]
                continue
            k, x, y, z, f = item
            x -= ox; y -= oy; z -= oz
            words = []
            if last[0] is None or abs(x - last[0]) > 1e-6: words.append(f"X{_fmt(x)}")
            if last[1] is None or abs(y - last[1]) > 1e-6: words.append(f"Y{_fmt(y)}")
            if last[2] is None or abs(z - last[2]) > 1e-6: words.append(f"Z{_fmt(z)}")
            if not words:
                continue
            if k == RAPID:
                out.append("G0 " + " ".join(words))
            else:
                fw = ""
                if f and f != last_f:
                    fw = f" F{_fmt(f)}"; last_f = f
                out.append("G1 " + " ".join(words) + fw)
            last = [x, y, z]
    out.append("M5")
    if m.coolant:
        out.append("M9")
    out.append(f"G0 Z{_fmt(clear)}")
    out += m.program_end
    out.append("M30")
    return "\n".join(out) + "\n"
