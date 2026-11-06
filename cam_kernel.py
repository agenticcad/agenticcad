"""
CAM kernel for AgenticCAD — 2.5D / simple 3D toolpaths from the exact B-rep, GRBL post-processor.

Everything is in model coordinates (mm, Z up). The post subtracts the WCS origin.
The agent writes a CAM script (see agent.py CAM_PROMPT) that builds a `Program`.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable

import numpy as np
from shapely.geometry import Polygon, MultiPolygon, LineString, Point, GeometryCollection, box as shp_box
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

import shapely
import build123d as b3d
from build123d import Shape, Plane, Face, Vector
from OCP.BRepAdaptor import BRepAdaptor_Surface, BRepAdaptor_Curve
from OCP.BRepTools import BRepTools_WireExplorer
from OCP.GCPnts import GCPnts_TangentialDeflection
from OCP.TopAbs import TopAbs_REVERSED

import cad_kernel as ck


class CamError(Exception):
    pass


class BuildStopped(CamError):
    """The build was stopped (the assistant's Stop, or a newer build of the same design)."""


import contextvars
from collections import OrderedDict

# set by the app around a CAM build (asyncio.to_thread carries it into the worker thread); long loops call stop_point()
STOP: contextvars.ContextVar = contextvars.ContextVar("cam_stop", default=None)


def stop_point() -> None:
    ev = STOP.get()
    if ev is not None and ev.is_set():
        raise BuildStopped("CAM build stopped")


# set by the app around a CAM build or simulation (carried into the worker thread like STOP): called as the work goes,
# with the fraction done (None when the total isn't known), what is happening, and for a build the ops built so far
PROGRESS: contextvars.ContextVar = contextvars.ContextVar("cam_progress", default=None)


def progress(frac: float | None, text: str, n: int | None = None) -> None:
    cb = PROGRESS.get()
    if cb is not None:
        cb(frac, text, n)


# Sections are the slow part of most CAM scripts (an OCCT boolean each, 0.3-1 s on a few hundred faces) and the
# assistant rebuilds the same script against the same model many times: results are kept per (shape, z, tol). The
# shape is held in the entry, so its hash (TShape + location) cannot be reused by another shape while cached.
_SECTION_CACHE: OrderedDict = OrderedDict()
_VIEW_CACHE: OrderedDict = OrderedDict()


def _moved(shape, loc):
    """`loc * shape` without build123d's deepcopy of the whole shape tree (0.5 s on a 550-face part, per view)."""
    from build123d.topology.shape_core import downcast
    return b3d.Compound.cast(downcast(shape.wrapped.Moved(loc.wrapped)))


def _cached(cache: OrderedDict, key, keep, make, limit: int):
    hit = cache.get(key)
    if hit is not None:
        cache.move_to_end(key)
        return hit[1]
    v = make()
    cache[key] = (keep, v)
    while len(cache) > limit:
        cache.popitem(last=False)
    return v


def clear_caches() -> None:
    _SECTION_CACHE.clear(); _VIEW_CACHE.clear(); _MESH_CACHE.clear()


# =============================================================================
# Library data: tools, machines
# =============================================================================
@dataclass
class Tool:
    number: int
    name: str
    type: str = "flat"          # flat | ball | bull | vbit | chamfer | drill | taper_ball (tool_shape.TYPES)
    diameter: float = 6.0       # the widest the cutting part gets
    flutes: int = 2
    flute_length: float = 20.0  # length of the cutting edges from the tip
    rpm: float = 12000
    feed: float = 1000          # XY feed, mm/min
    plunge: float = 300         # Z feed, mm/min
    stepdown: float = 2.0       # default axial depth per pass, mm
    stepover: float = 0.5       # default radial stepover, fraction of diameter
    angle: float = 0.0          # included angle for vbit/chamfer/drill point
    notes: str = ""
    corner_radius: float = 0.0  # bull nose
    tip_diameter: float = 0.0   # V-bit / chamfer flat tip; the ball diameter of a tapered ball endmill
    sharp: bool = False         # V-bit / chamfer: the tip really is a point (tip_diameter 0 is right, not missing)
    taper: float = 0.0          # tapered ball: taper angle per side, degrees
    shank_diameter: float = 0.0 # 0 = the same as the diameter (a 1/8" shank on a 0.3 mm endmill: 3.175)
    neck_diameter: float = 0.0  # a relieved neck between the flutes and the shank (0 = none)
    reach: float = 0.0          # how far below the shank the tool stays thin (neck end); 0 = the flute length
    stickout: float = 0.0       # how far the tool projects from the collet / nut; 0 = estimated
    photo: str = ""             # a picture of the tool / its packet (workspace/tool_photos/<name>): shown at tool changes
    # gear cutter (type "gear_cutter": a disc-type involute module cutter on an arbor; `diameter` is the disc's outer Ø,
    # `shank_diameter` the arbor's shank in the collet, `stickout` from the collet to the nut's underside)
    module: float = 0.0         # gear module the cutter is made for (mm)
    pressure_angle: float = 20.0
    cutter_teeth_min: int = 0   # lowest tooth count of the cutter's range (No.1 = 12–13T -> 12); an exact-count cutter: that count
    thickness: float = 0.0      # disc width at the gear's tip line (the widest part of the form); 0 = from module and teeth
    nut_height: float = 4.0     # retaining nut under the disc
    nut_diameter: float = 10.0

    @property
    def radius(self) -> float:
        return self.diameter / 2

    @property
    def shape(self):
        """The tool's exact profile (tool_shape.ToolShape): heights, radii, bands, outline."""
        import tool_shape
        return tool_shape.shape(self)

    def rated_mrr(self) -> float:
        """Material removal rate (mm3/min) at the tool's own stepdown x stepover width x feed - the cut feeds() sized it
        for. A pass that removes much more than this (a ball buried in a slot, a plunge into a step) is one the tool
        was never meant to take. A V-bit / chamfer tool cuts laps of its stepdown with its cone: one lap removes a band
        stepdown x the cone's engaged height x tan(half angle) per flank, at most stepdown x (Ø - tip) / 2 on a chamfer's
        one flank (both flanks of a V groove: twice that) - what it is rated for (the tip Ø alone made the latch's 90°
        chamfer bit 12 mm3/min, while its 1.2 mm chamfers ran at 40-110 without trouble)."""
        if self.type in ("vbit", "chamfer"):
            w = max(self.diameter - self.tip_diameter, 0.2) * (0.5 if self.type == "chamfer" else 1.0)
        else:
            w = self.stepover * self.diameter
        return float(max(self.stepdown, 0.0) * max(w, 0.0) * max(self.feed, 0.0))

    def label(self) -> str:
        return f"T{self.number} {self.name}"

    def spec(self) -> str:
        """The tool's size the way its packet gives it: what the cut depends on, by type."""
        g = lambda v: f"{v:g}"
        sh = f" · shank Ø{g(self.shank_diameter)}" if self.shank_diameter and self.shank_diameter != self.diameter else ""
        fl = f" · flutes {g(self.flute_length)} mm"
        if self.type == "ball":
            return f"ball R{g(self.diameter / 2)} (Ø{g(self.diameter)}){fl}{sh}"
        if self.type == "bull":
            return f"bull nose Ø{g(self.diameter)} R{g(self.corner_radius)}{fl}{sh}"
        if self.type in ("vbit", "chamfer"):
            tip = f"tip Ø{g(self.tip_diameter)}" if self.tip_diameter else "sharp point" if self.sharp else "sharp tip (no tip Ø set)"
            return f"{'V' if self.type == 'vbit' else 'chamfer'} {g(self.angle)}° · {tip} · Ø{g(self.diameter)}{fl}{sh}"
        if self.type == "taper_ball":
            return f"tapered ball tip Ø{g(self.tip_diameter)} · {g(self.taper)}°/side · Ø{g(self.diameter)}{fl}{sh}"
        if self.type == "drill":
            return f"drill Ø{g(self.diameter)}" + (f" · {g(self.angle)}° point" if self.angle else "") + fl
        if self.type == "gear_cutter":
            import tool_shape
            n = tool_shape.cutter_number(self.cutter_teeth_min) if self.cutter_teeth_min else None
            who = (f"No.{n[0]} ({n[1]}–{n[2]}T)" if n and n[2] < 10 ** 5 else f"No.{n[0]} ({n[1]}T–rack)" if n
                   else f"{self.cutter_teeth_min}T cutter" if self.cutter_teeth_min else "teeth?")
            t = self.thickness
            if not t and self.module and self.cutter_teeth_min:
                try:
                    t = tool_shape.gear_form_width(self.module, self.cutter_teeth_min, self.pressure_angle or 20.0)
                except tool_shape.ToolShapeError:
                    t = 0.0
            return (f"m{g(self.module)} · {g(self.pressure_angle or 20)}° · {who} · Ø{g(self.diameter)} x {t:.2f}"
                    + (f" · arbor Ø{g(self.shank_diameter)}" if self.shank_diameter else ""))
        return f"flat Ø{g(self.diameter)}{fl}{sh}"


def tool_payload(tool: Tool) -> dict:
    """A tool as the UI gets it: its fields plus its shape (outline from the tip, flute length, stickout...)."""
    d = asdict(tool)
    try:
        d["shape"] = tool.shape.payload()
    except Exception as e:  # noqa: BLE001 — a bad definition shows as an error, the rest still works
        d["shape"] = {"error": str(e)}
    return d


# What each controller's G-code interpreter accepts, by post. Sources: the controllers' own source / manuals.
#   arc_formats   : arc centre forms it takes, preferred first ("ij" = I/J offsets from the start, "r" = radius word)
#   helical_arcs  : G2/G3 may also move Z
#   max_arc_sweep : degrees per arc the post writes in each form (R near 180 deg is ill-conditioned, so R arcs stay
#                   well short of it)
#   max_line      : longest line the controller reads (0 = no practical limit); comments_count: whether comment text
#                   counts towards it
CONTROLLERS = {
    # GRBL 1.1 (gnea/grbl): gcode.c takes I/J/K and R (negative R = the long way round), helical via mc_arc's linear
    # axis; rejects I/J arcs whose start and end radii differ by > 0.005 mm and > 0.1 % of the radius (the post puts
    # the centre on the chord's bisector so they match). protocol.h LINE_BUFFER_SIZE 80; comments are dropped as the
    # line is read, so they don't count.
    "grbl": {"name": "GRBL", "arc_formats": ("ij", "r"), "helical_arcs": True, "max_arc_sweep": {"ij": 180.0, "r": 135.0},
             "max_line": 80, "comments_count": False},
    # Makera Z1 / Carvera firmware (Smoothieware fork, MakeraZ1Firmware): Robot::compute_arc takes the radius from
    # I/J only, there is no R form; append_arc moves the linear axis (helical); segments arcs to 0.002 mm
    # (mm_max_arc_error default, not set in configZ1.default). Lines longer than 63 characters are truncated.
    "makera": {"name": "Makera (Smoothieware)", "arc_formats": ("ij",), "helical_arcs": True, "max_arc_sweep": {"ij": 180.0},
               "max_line": 63, "comments_count": True},
    # Haas NGC / Classic (Fanuc-style): I/J/K and R, helical G2/G3 with Z.
    "haas": {"name": "Haas", "arc_formats": ("ij", "r"), "helical_arcs": True, "max_arc_sweep": {"ij": 180.0, "r": 135.0},
             "max_line": 0, "comments_count": False},
    # LinuxCNC rs274ngc: centre (I/J) and radius format, helical; LINELEN 255.
    "linuxcnc": {"name": "LinuxCNC", "arc_formats": ("ij", "r"), "helical_arcs": True, "max_arc_sweep": {"ij": 180.0, "r": 135.0},
                 "max_line": 255, "comments_count": True},
}


# 3D probe defaults: MakeraStudio's tool number for the add-on 3D probe (the firmware powers the probe port after M6 T9999)
# and the M480 routines' default ball Ø (ATCHandler: D2)
PROBE_DEFAULTS = {"tool": 9999, "ball": 2.0, "stylus": 20.0, "feed": 100.0}


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
    arc_format: str = "ij"                  # "ij" (centre offsets I/J) | "r" (radius word); only what the controller takes
    helical_arcs: bool = True               # arcs may move Z (helical ramps); off = only flat arcs
    notes: str = ""
    post: str = "grbl"                      # grbl | makera (Makera Z1 / Carvera family) | haas (Haas NGC/Classic) | linuxcnc (rs274ngc)
    # optional rotary 4th axis: {"axis": "A", "about": "x", "max_diameter": mm, "max_length": mm,
    #   "max_speed": deg/min, "installed": bool}. The rotary axis is parallel to machine X.
    rotary: dict | None = None
    collet: float = 0.0                     # largest tool shank the spindle takes (mm); 0 = unchecked
    # stepdown caps for the feeds calculator, mm per pass by material ("*" = any other material); {} = no cap
    max_stepdown: dict = field(default_factory=dict)
    # depth per level for constant-engagement (adaptive) clearing, mm by material ("*" = any other): the side of the
    # flutes does the work at a light radial engagement, so it is not the slotting cap above; {} = ADAPTIVE_DOC x Ø
    adaptive_stepdown: dict = field(default_factory=dict)
    # machine model overrides (Settings ▸ Machines ▸ Model): {"nose": [{name, r, h}...], "spoilboard": mm, "clearance": mm, "stickout": mm}
    model: dict | None = None
    # the 3D probe the probe ops use (PROBE_DEFAULTS fill what is missing): {"tool": its T number, "ball": effective ball
    # Ø (mm, the calibrated one), "stylus": stylus length below the body (mm), "feed": touch feed (mm/min)}
    probe: dict | None = None

    def probe_spec(self) -> dict:
        return {**PROBE_DEFAULTS, **(self.probe or {})}

    def capabilities(self) -> dict:
        """What the post may emit for this machine: its controller's limits (CONTROLLERS, by post) combined with the
        machine's own choices. An arc format the controller doesn't take falls back to one it does (see warnings())."""
        ctl = CONTROLLERS.get(self.post, CONTROLLERS["grbl"])
        fmt = self.arc_format if self.arc_format in ctl["arc_formats"] else ctl["arc_formats"][0]
        return {"controller": ctl["name"], "arcs": bool(self.arcs) and bool(ctl["arc_formats"]), "arc_format": fmt,
                "arc_formats": ctl["arc_formats"], "helical_arcs": bool(self.helical_arcs) and ctl["helical_arcs"],
                "max_arc_sweep": ctl["max_arc_sweep"].get(fmt, 180.0), "max_line": ctl["max_line"],
                "comments_count": ctl["comments_count"]}

    def warnings(self) -> list[str]:
        ctl = CONTROLLERS.get(self.post, CONTROLLERS["grbl"])
        out = []
        if self.arc_format not in ("ij", "r"):
            out.append(f"{self.name}: arc_format must be 'ij' or 'r' (got {self.arc_format!r}); using I/J")
        elif self.arcs and self.arc_format not in ctl["arc_formats"]:
            out.append(f"{self.name}: the {ctl['name']} controller doesn't take {self.arc_format.upper()}-format arcs; "
                       f"the post writes {ctl['arc_formats'][0].upper()}")
        return out

    def adaptive_doc(self, tool: "Tool", material: str = "") -> float:
        """How deep one adaptive level may go with this tool: the machine's adaptive_stepdown for the material, else
        ADAPTIVE_DOC x the tool Ø; never more than the flutes less 1 mm (chips must clear the top of the cut)."""
        key = MATERIAL_ALIASES.get(str(material).lower().strip(), str(material).lower().strip())
        caps = {MATERIAL_ALIASES.get(str(k).lower().strip(), str(k).lower().strip()): v for k, v in (self.adaptive_stepdown or {}).items()}
        v = caps.get(key, caps.get("*"))
        d = float(v) if v else ADAPTIVE_DOC * tool.diameter
        fl = getattr(tool, "flute_length", 0.0) or 0.0
        return max(min(d, fl - 1.0) if fl > 1.5 else min(d, fl * 0.9 if fl > 0 else d), 0.1)

    def stepdown_cap(self, material: str) -> float | None:
        caps = {MATERIAL_ALIASES.get(str(k).lower().strip(), str(k).lower().strip()): v for k, v in self.max_stepdown.items()}
        v = caps.get(material, caps.get("*"))
        return float(v) if v else None


# =============================================================================
# Setup: stock + WCS origin
# =============================================================================
def _material(m: str) -> str:
    k = str(m or "").lower().strip()
    return MATERIAL_ALIASES.get(k, k)


@dataclass
class Stock:
    xmin: float; xmax: float; ymin: float; ymax: float; zmin: float; zmax: float
    material: str = ""          # e.g. "aluminium" (feeds / the simulation's tab and load thresholds); "" = not given

    @classmethod
    def from_model(cls, model, margin: float = 2.0, top: float = 0.0, bottom: float = 0.0, material: str = "") -> "Stock":
        """Bounding box of the model (or any shape / body) plus XY margin; `top`/`bottom` add extra
        material above/below. material: what the stock is ("aluminium", ...)."""
        if isinstance(model, ck.Model):
            (x0, y0, z0), (x1, y1, z1) = model.bbox_min, model.bbox_max
        elif isinstance(model, ck.Body):
            (x0, y0, z0), (x1, y1, z1) = model.bbox_min, model.bbox_max
        elif isinstance(model, Shape):
            bb = model.bounding_box()
            (x0, y0, z0), (x1, y1, z1) = (bb.min.X, bb.min.Y, bb.min.Z), (bb.max.X, bb.max.Y, bb.max.Z)
        else:
            raise CamError("Stock.from_model expects the model, a body, or a shape")
        return cls(x0 - margin, x1 + margin, y0 - margin, y1 + margin, z0 - bottom, z1 + top, _material(material))

    from_shape = from_model

    @classmethod
    def block(cls, size_x: float, size_y: float, size_z: float, center_xy=(0.0, 0.0), top: float | None = None,
              bottom: float | None = None, material: str = "") -> "Stock":
        """Explicit block. Give either `top` (z of stock top) or `bottom`; default bottom = 0. material: e.g. "aluminium"."""
        if top is None and bottom is None:
            bottom = 0.0
        if top is None:
            top = bottom + size_z
        bottom = top - size_z
        cx, cy = center_xy
        return cls(cx - size_x / 2, cx + size_x / 2, cy - size_y / 2, cy + size_y / 2, bottom, top, _material(material))

    @classmethod
    def cylinder(cls, diameter: float, length: float, x0: float = 0.0, axis: tuple[float, float] = (0.0, 0.0),
                 material: str = "") -> "Stock":
        """Round bar for the 4th axis: axis parallel to X through model (y, z) = `axis`, from x0 to x0 + length."""
        y0, z0 = axis
        r = diameter / 2
        st = cls(x0, x0 + length, y0 - r, y0 + r, z0 - r, z0 + r, _material(material))
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
    # the same part at several places (a sheet of badges): the ops are computed ONCE, on the part at its own place, and
    # the program runs every op at each tile in turn (all tiles with one tool before the next tool). tiles = MODEL-frame
    # offsets [(dx, dy[, dz]), ...] from that part, the first usually (0, 0); tiles_of(model, "badge") reads them from
    # a pattern() in the design. tile_wcs=True: each tile in its own work offset (G54, G55, ...) that the operator zeros;
    # by default one work offset and the tiles are offsets in the program (one file either way: no subprograms).
    tiles: Any = None
    tile_wcs: bool = False
    # how the work is held: a fixture by name (fixture_lib: vises, clamps, plates), where it sits on the table
    # (fixture_at = (x, y) from the table centre, default: the fixture's usual place; fixture_rot degrees about Z) and its
    # parameters (opening, seat, ...). The stock's bottom-centre sits at the fixture's work origin; None = stock centred on
    # the bare table/spoilboard. `fixtures` = more fixtures on the table at the same time, each placed and turned on its own:
    # [{"name": "Step clamps", "at": (x, y), "rot": deg, "params": {...}}, ...] — the simulator checks the tool against all.
    fixture: str | None = None
    fixture_at: tuple[float, float] | None = None
    fixture_rot: float = 0.0
    fixture_params: dict | None = None
    fixtures: list | None = None
    # how the stock really lies on the machine, measured after it was mounted (e.g. probing two register holes after a
    # flip): {"rot_deg": deg CCW from above, "dx": mm, "dy": mm}. The post turns every XY of this setup's ops (moves, arc
    # centres, probe points) by rot_deg about the work origin (0, 0) and then shifts it by (dx, dy); Z is untouched.
    # Set at run time (the machine side, from the probed alignment) - cam.py and the toolpaths stay as designed.
    align: dict | None = None

    def __post_init__(self):
        self.model_stock = self.stock
        self.tiles = _norm_tiles(self.tiles)
        if len(self.tiles) > 1 and self.rotary:
            raise CamError("tiles: a 4th-axis setup machines one part (tile a flat setup)")
        if self.tile_wcs and len(self.tiles) > 9:
            raise CamError(f"tile_wcs: {len(self.tiles)} tiles but a controller has 9 work offsets (G54..G59.3)")
        fx = _FIXTURES["table"].get(self.name)        # fixtures({...}) line in cam.py (the CAM tab's picker) wins over the script
        if fx is not None:
            self.fixture = fx.get("name", self.fixture) or None
            self.fixture_at = tuple(fx["at"]) if fx.get("at") is not None else (None if "name" in fx else self.fixture_at)
            self.fixture_rot = float(fx.get("rot", 0.0 if "name" in fx else self.fixture_rot))
            self.fixture_params = fx.get("params", None if "name" in fx else self.fixture_params)
            if "extra" in fx:
                self.fixtures = list(fx.get("extra") or [])
        self._fixture_model = None
        self._extra_models = None
        if (self.fixture or self.fixtures) and self.rotary:
            raise CamError("a fixture can't be combined with a rotary setup (the chuck holds the work)")
        for i, e in enumerate(self.fixtures or []):
            if not isinstance(e, dict) or not e.get("name"):
                raise CamError(f"fixtures[{i}]: each extra fixture is a dict with a name (and at=(x, y), rot, params)")
        if self.fixture:
            self.fixture_model()                        # validate now: unknown fixture / bad params fail at setup time
            if self.fixture_at is None:
                self.fixture_at = tuple(self._fixture_model.home) if self._fixture_model.home else (0.0, 0.0)
        if self.fixture_at is None:
            self.fixture_at = (0.0, 0.0)
        self.fixture_at = (float(self.fixture_at[0]), float(self.fixture_at[1]))
        if self.fixtures:
            self.extra_fixture_models()
        if self.rotary:
            if self.machine.rotary is None:
                raise CamError(f"{self.machine.name} has no 4th axis defined (machine.rotary); can't make a rotary setup")
            if self.axis is None:
                self.axis = ((self.stock.ymin + self.stock.ymax) / 2, (self.stock.zmin + self.stock.zmax) / 2)
            y0, z0 = self.axis
            corners = [(y - y0, z - z0) for y in (self.stock.ymin, self.stock.ymax) for z in (self.stock.zmin, self.stock.zmax)]
            r = self.stock.radius if self.stock.is_cylinder else max(math.hypot(*c) for c in corners)
            self.max_radius = r
            st = Stock(self.stock.xmin, self.stock.xmax, -r, r, -r, r, self.stock.material)
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
            self.stock = Stock(*(float(v) + 0.0 for v in (lo[0], hi[0], lo[1], hi[1], lo[2], hi[2])), self.stock.material)
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
            stock = self._stock_in_fixture_frame(self.fixture_rot)
            try:
                self._fixture_model = fixture_lib.model_for(self.fixture, self.fixture_params or {}, stock)
            except fixture_lib.FixtureScriptError as e:
                raise CamError(f"fixture {self.fixture!r}: {e}") from e
        return self._fixture_model

    def _stock_in_fixture_frame(self, rot: float) -> dict:
        """The stock's size as a fixture turned by `rot` degrees sees it (its X is the fixture's X): a vise turned 90° grips
        the stock's model-Y faces, so its opening defaults to the stock's Y size."""
        ms = self.model_stock
        sx, sy = ms.xmax - ms.xmin, ms.ymax - ms.ymin
        a = math.radians(rot or 0.0); c, s_ = abs(math.cos(a)), abs(math.sin(a))
        return {"x": round(c * sx + s_ * sy, 6), "y": round(s_ * sx + c * sy, 6), "z": ms.zmax - ms.zmin}

    def extra_fixture_models(self) -> list[dict]:
        """The other fixtures on the table: [{name, at, rot, params, model}] (models built with this setup's stock size,
        which vises use as their default opening)."""
        if self._extra_models is None:
            import fixture_lib
            out = []
            for i, e in enumerate(self.fixtures or []):
                try:
                    m = fixture_lib.model_for(e["name"], dict(e.get("params") or {}), self._stock_in_fixture_frame(float(e.get("rot") or 0.0)))
                except fixture_lib.FixtureScriptError as ex:
                    raise CamError(f"fixtures[{i}] {e['name']!r}: {ex}") from ex
                at = e.get("at")
                at = tuple(at) if at is not None else (tuple(m.home) if m.home else (0.0, 0.0))
                out.append({"name": m.name, "at": (float(at[0]), float(at[1])), "rot": float(e.get("rot") or 0.0),
                            "params": dict(e.get("params") or {}), "model": m})
            self._extra_models = out
        return self._extra_models

    def fixture_layout(self) -> list[dict]:
        """Every fixture on the table, the holding one first: [{name, holds, at, rot, params, model, origin_model}] where
        origin_model is the fixture frame's origin in MODEL coordinates (all fixtures stand on one plane: the table, or
        the bed when one of them replaces the spoilboard)."""
        import fixture_lib
        holder = self.fixture_model()
        ms = self.model_stock
        bc = ((ms.xmin + ms.xmax) / 2, (ms.ymin + ms.ymax) / 2, ms.zmin)
        st_t = fixture_lib.stock_on_table(holder, self.fixture_at, self.fixture_rot)
        out = []
        if holder is not None:
            out.append({"name": holder.name, "holds": True, "at": self.fixture_at, "rot": float(self.fixture_rot),
                        "params": holder.params, "model": holder, "origin_model": fixture_lib.frame_in_model(self.fixture_at, bc, st_t)})
        for e in self.extra_fixture_models():
            out.append({**e, "holds": False, "origin_model": fixture_lib.frame_in_model(e["at"], bc, st_t)})
        return out

    def replaces_spoilboard(self) -> bool:
        """A fixture here bolts to the bed with the spoilboard removed (then every fixture stands on the bed)."""
        return any(f["model"].replaces_spoilboard for f in self.fixture_layout())

    def fixture_height(self) -> float:
        """How far the stock bottom sits above the plane the fixtures stand on (0 on the bare table)."""
        fm = self.fixture_model()
        return float(fm.height) if fm is not None else 0.0

    def fixture_payload(self) -> dict | None:
        """The holding fixture (None on the bare table) plus `extra`: every other fixture on the table."""
        lay = self.fixture_layout() if (self.fixture or self.fixtures) else []
        bare_at = [float(self.fixture_at[0]), float(self.fixture_at[1])] if self.fixture_at else [0.0, 0.0]
        if not lay:
            if any(bare_at):                                  # bare stock placed off the table centre
                return {"name": None, "at": bare_at, "rot": 0.0, "params": {}, "work_origin": [0.0, 0.0, 0.0], "height": 0.0,
                        "clamp_axis": None, "max_opening": None, "replaces_spoilboard": False, "origin_model": None, "extra": []}
            return None
        rs = any(f["model"].replaces_spoilboard for f in lay)

        def one(f):
            m = f["model"]
            lo, hi = m.bbox()
            return {"name": m.name, "at": [float(f["at"][0]), float(f["at"][1])], "rot": float(f["rot"]), "params": m.params,
                    "work_origin": list(m.work_origin), "height": m.height, "top": float(hi[2]), "clamp_axis": m.clamp_axis,
                    "size": [round(float(hi[0] - lo[0]), 2), round(float(hi[1] - lo[1]), 2)],
                    "max_opening": m.max_opening, "replaces_spoilboard": m.replaces_spoilboard,
                    "origin_model": [float(v) for v in f["origin_model"]]}
        holder = next((f for f in lay if f["holds"]), None)
        base = one(holder) if holder else {"name": None, "at": bare_at, "rot": 0.0, "params": {}, "work_origin": [0.0, 0.0, 0.0],
                                            "height": 0.0, "clamp_axis": None, "max_opening": None, "replaces_spoilboard": False,
                                            "origin_model": None}
        base["extra"] = [one(f) for f in lay if not f["holds"]]
        base["replaces_spoilboard"] = rs
        return base

    def fixture_collision_shapes(self) -> list[tuple]:
        """[(shape, fixture name)] of every fixture's collision parts in MODEL coordinates (top setups)."""
        import fixture_lib
        out = []
        for f in self.fixture_layout():
            for p in f["model"].parts:
                if p.collision:
                    out.append((fixture_lib.placed_shape(p.shape, f["rot"], f["origin_model"]), f["name"]))
        return out

    def fixture_warnings(self) -> list[str]:
        """Fixtures that overlap the stock or each other, or that are made for another machine."""
        lay = self.fixture_layout() if (self.fixture or self.fixtures) else []
        if not lay:
            return []
        w = []
        for f in lay:
            mach = (f["model"].mount or {}).get("machines")
            if mach and not any(self.machine.name.startswith(x) for x in mach):
                w.append(f"fixture '{f['name']}' is made for {', '.join(mach)}, not {self.machine.name}")
        boxes = []                                           # per fixture: model-space bounding boxes of its collision parts
        import fixture_lib
        for f in lay:
            bb = []
            for p in f["model"].parts:
                if p.collision:
                    b = fixture_lib.placed_shape(p.shape, f["rot"], f["origin_model"]).bounding_box()
                    bb.append((b.min.X, b.min.Y, b.min.Z, b.max.X, b.max.Y, b.max.Z))
            boxes.append(bb)
        ms = self.model_stock
        sb = (ms.xmin, ms.ymin, ms.zmin, ms.xmax, ms.ymax, ms.zmax)

        def overlap(a, b, eps=0.05):
            return all(min(a[k + 3], b[k + 3]) - max(a[k], b[k]) > eps for k in range(3))
        for f, bb in zip(lay, boxes):
            if not f["holds"] and any(overlap(b, sb) for b in bb):
                w.append(f"fixture '{f['name']}' at {tuple(round(v, 1) for v in f['at'])} overlaps the stock: move it")
        for i in range(len(lay)):
            for j in range(i + 1, len(lay)):
                if any(overlap(a, b) for a in boxes[i] for b in boxes[j]):
                    w.append(f"fixtures '{lay[i]['name']}' and '{lay[j]['name']}' overlap on the table: move one")
        return w

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

    def tile_offsets(self) -> list[tuple[float, float, float]]:
        """The tiles as setup-frame offsets (the setup's rotation applied; a flipped setup mirrors Y)."""
        R = self.matrix()[:3, :3]
        return [tuple(float(v) for v in np.asarray(t, dtype=float) @ R.T) for t in self.tiles]

    def tile_wcs_names(self) -> list[str]:
        """tile_wcs: the work offset each tile runs in, from the setup's own (G54, G55, ... G59, G59.1 .. G59.3)."""
        names = [f"G{n}" for n in range(54, 60)] + ["G59.1", "G59.2", "G59.3"]
        k0 = names.index(self.wcs) if self.wcs in names else 0
        return names[k0:k0 + len(self.tiles)]

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
        """The shape as the machine sees it in this setup (setup frame), for section/holes/stock_minus/parallel3d.
        The same shape in the same frame gives the same view object, so its sections are reused across builds."""
        if self.rotary:
            y0, z0 = self.axis
            ang = self.a if a is None else a
            return _cached(_VIEW_CACHE, (hash(shape), "a", float(y0), float(z0), float(ang)), shape,
                           lambda: _moved(shape, b3d.Rot(ang, 0, 0) * b3d.Pos(0, -y0, -z0)), 64)
        rx, ry, rz = self._rot
        return _cached(_VIEW_CACHE, (hash(shape), "r", float(rx), float(ry), float(rz)), shape,
                       lambda: _moved(shape, b3d.Rot(0, 0, rz) * b3d.Rot(0, ry, 0) * b3d.Rot(rx, 0, 0)), 64)

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

    def align_xy(self):
        """The post's work-XY transform for `align` (None when there is none): (x, y) -> rotated about (0, 0), then shifted."""
        al = self.align
        if not al:
            return None
        if not isinstance(al, dict) or set(al) - {"rot_deg", "dx", "dy"}:
            raise CamError(f"{self.name}: align is a dict with rot_deg, dx, dy (got {al!r})")
        if self.rotary:
            raise CamError(f"{self.name}: align applies to flat setups only (a 4th-axis setup is aligned by its A zero)")
        rot, dx, dy = (float(al.get(k) or 0.0) for k in ("rot_deg", "dx", "dy"))
        if not all(math.isfinite(v) for v in (rot, dx, dy)):
            raise CamError(f"{self.name}: align values must be finite numbers (got {al!r})")
        if abs(rot) < 1e-9 and abs(dx) < 1e-9 and abs(dy) < 1e-9:
            return None
        c, s_ = math.cos(math.radians(rot)), math.sin(math.radians(rot))
        return lambda x, y: (x * c - y * s_ + dx, x * s_ + y * c + dy)

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
    cut_defaults: dict = field(default_factory=dict)          # the script's cut settings (CUT_FIELDS of this kind)
    cut_overrides: dict = field(default_factory=dict)         # cut settings changed in the CAM tab
    post_tol: float | None = None                              # arc-fit / line-merge tolerance for the post (None: by kind)
    color_override: bool = False                              # the colour comes from the CAM tab
    tiled: bool = True                                        # runs at each of its setup's tiles (False: once - the moves cover them all)
    sim_moves: list | None = None                             # probe ops: the moves the simulation checks for air (touches stopped short)

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
        """Feed moves at their feed; rapids at the machine's rapid rate, their Z part no faster than its Z limit; either
        no faster than the 4th axis can turn (a small bar at a normal feed asks for far more than it has)."""
        t = 0.0
        prev = None
        zmax = min(machine.rapid, (machine.max_feed or {}).get("z", machine.rapid)) or machine.rapid
        a_max = float((machine.rotary or {}).get("max_speed") or 0.0)
        for m in self.moves:
            if prev is not None:
                d = self._seg(prev, m)
                dt = max(d / machine.rapid, abs(m[3] - prev[3]) / zmax) if m[0] == RAPID else d / max(m[4], 1.0)
                if a_max and len(m) > 5 and len(prev) > 5:
                    dt = max(dt, abs(m[5] - prev[5]) / a_max)
                t += dt
            prev = m
        return t

    def bounds(self):
        xs = [m[1] for m in self.moves]; ys = [m[2] for m in self.moves]; zs = [m[3] for m in self.moves]
        return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))

    def a_rate_max(self) -> float:
        """The fastest the rotary axis is asked to turn on a feed move, deg/min (0 if A never moves while cutting)."""
        worst = 0.0
        prev = None
        for m in self.moves:
            if prev is not None and m[0] != RAPID and len(m) > 5 and len(prev) > 5 and m[5] != prev[5]:
                t = self._seg(prev, m) / max(m[4], 1.0)                 # minutes for this move at its feed
                if t > 1e-9:
                    worst = max(worst, abs(m[5] - prev[5]) / t)
            prev = m
        return worst


class PathBuilder:
    """Accumulates moves for one operation with a safe-Z discipline."""

    def __init__(self, setup: Setup, tool: Tool, op: Op):
        self.setup, self.tool, self.op = setup, tool, op
        op.setup = setup
        shank = getattr(tool, "shank_diameter", 0) or tool.diameter
        if setup.machine.collet and shank > setup.machine.collet + 1e-6:
            op.warnings.append(f"{tool.label()}: its Ø{shank:g} shank is larger than the {setup.machine.collet:g} mm collet "
                               "(set the tool's shank diameter if it is a reduced-shank tool)")
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

    def plunge_to(self, z, rapid_to: float | None = None, gap: float = 1.0):
        """Rapid down to `gap` mm above `rapid_to` (or the stock top), then plunge-feed to z."""
        x, y = self.pos[0], self.pos[1]
        approach = (rapid_to if rapid_to is not None else self.setup.stock.top) + gap
        if self.pos[2] > approach and z < approach:
            self._add(RAPID, x, y, approach)
        self._add(PLUNGE, x, y, z, self.plunge)

    def ramp_feed(self, angle_deg: float) -> float:
        """Feed for a ramp or helix descending at `angle_deg`: the descent rate never exceeds the plunge feed, and the
        feed along the ramp (a slot, full width) is at most RAMP_FEED of the cutting feed."""
        sa = math.sin(math.radians(max(angle_deg, 0.1)))
        return max(self.plunge, min(self.feed * RAMP_FEED, self.plunge / sa))

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
    stop_point()
    return _cached(_SECTION_CACHE, (hash(shape), round(float(z), 9), float(tol)), shape,
                   lambda: _section_any(shape, z, tol), 4000)


# Sections come from a fine triangulation of the shape, not an OCCT boolean: the plane cuts the triangles (numpy), the
# cut segments are stitched into rings and the rings nested into polygons. A few ms a section against 0.1-1 s for the
# boolean (the boat latch's 106 sections took 20 s of its 31 s build), and the mesh is built once per shape in ~0.1 s.
# Its 0.01 mm deflection is finer than the 0.02 mm the boolean's edges were sampled at (0.005 made the polygons 5-12 %
# denser). A shape whose mesh is not closed where the plane cuts it falls back to the boolean.
MESH_SECTIONS = True
MESH_DEFLECTION = 0.01
_MESH_CACHE: OrderedDict = OrderedDict()


def _section_any(shape: Shape, z: float, tol: float) -> MultiPolygon:
    if MESH_SECTIONS:
        try:
            r = _section_mesh(shape, z)
        except Exception:  # noqa: BLE001 - the boolean is the fallback for anything the mesh path cannot do
            r = None
        if r is not None:
            return r
    return _section(shape, z, tol)


def _mesh_of(shape: Shape, angular: float = 0.2) -> tuple[np.ndarray, np.ndarray]:
    """The shape's slicing triangulation: vertices (n x 3) and triangles (m x 3), kept per shape. angular: the angular
    deflection (rad) - 0.2 is plenty for flat-walled sections, a 3D finish over a curved surface needs the display
    "fine" 0.03 (at 0.2 the chords of a R20 dome sat 0.024 mm inside it and the tool followed them in)."""
    def make():
        from OCP.BRep import BRep_Tool
        from OCP.BRepMesh import BRepMesh_IncrementalMesh
        from OCP.BRepTools import BRepTools
        from OCP.TopLoc import TopLoc_Location
        BRepTools.Clean_s(shape.wrapped)
        BRepMesh_IncrementalMesh(shape.wrapped, MESH_DEFLECTION, False, angular, True)
        P: list[tuple[float, float, float]] = []
        I: list[tuple[int, int, int]] = []
        for f in shape.faces():
            loc = TopLoc_Location()
            tri = BRep_Tool.Triangulation_s(f.wrapped, loc)
            if tri is None:
                continue
            trsf, ident = loc.Transformation(), loc.IsIdentity()
            base = len(P)
            for i in range(1, tri.NbNodes() + 1):
                p = tri.Node(i) if ident else tri.Node(i).Transformed(trsf)
                P.append((p.X(), p.Y(), p.Z()))
            for i in range(1, tri.NbTriangles() + 1):
                a, b, c = tri.Triangle(i).Get()
                I.append((base + a - 1, base + b - 1, base + c - 1))
        return np.array(P, dtype=float).reshape(-1, 3), np.array(I, dtype=np.int64).reshape(-1, 3)
    return _cached(_MESH_CACHE, (hash(shape), "mesh", float(angular)), shape, make, 32)


def _slice_mesh(P: np.ndarray, I: np.ndarray, z: float) -> np.ndarray:
    """The segments (k x 2 x 2, XY) where the plane at z cuts the triangles."""
    s = P[:, 2] - z
    s = np.where(np.abs(s) < 1e-9, 1e-9, s)            # a vertex on the plane counts as above: no segment ends on a vertex
    S = s[I]
    pos = S > 0
    cross = pos.any(axis=1) & (~pos).any(axis=1)
    S, T = S[cross], I[cross]
    E = np.empty((len(T), 2, 2))
    cnt = np.zeros(len(T), dtype=np.int64)
    for i, j in ((0, 1), (1, 2), (2, 0)):               # exactly two edges of a straddling triangle cross the plane
        c = np.nonzero((S[:, i] > 0) != (S[:, j] > 0))[0]
        t = (S[c, i] / (S[c, i] - S[c, j]))[:, None]
        A, B = P[T[c, i], :2], P[T[c, j], :2]
        E[c, cnt[c]] = A + (B - A) * t
        cnt[c] += 1
    return E


def _stitch_rings(E: np.ndarray, tol: float = 1e-6) -> list[list[tuple[float, float]]] | None:
    """Closed rings from unordered segments whose endpoints match within tol. Endpoints are first snapped to one
    coordinate per node (the same cut point is computed from each of the two triangles sharing an edge, a few ulps
    apart). Every node shared by exactly two segments: the rings are walked. Nodes with more (a real thread's solid
    overlapping or touching itself, a vertex on the plane): the snapped segments are noded and every enclosed face
    taken (shapely polygonize). An open end (one segment: the mesh is not closed there): None."""
    from scipy.spatial import cKDTree
    Q = E.reshape(-1, 2)                                # endpoints: segment i has 2i and 2i + 1
    n = len(Q)
    parent = np.arange(n)

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]; a = parent[a]
        return a
    for a, b in cKDTree(Q).query_pairs(tol, output_type="ndarray"):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
    node = np.array([find(a) for a in range(n)])
    Q = Q[node]                                         # one coordinate per node
    nodes, inv, counts = np.unique(node, return_inverse=True, return_counts=True)
    seg = inv.reshape(-1, 2)
    live = seg[:, 0] != seg[:, 1]                       # zero-length segments (a vertex on the plane) carry nothing
    if not live.all():
        return _stitch_rings(Q.reshape(-1, 2, 2)[live], tol) if live.any() else []
    if counts.min() == 1:
        return None
    if counts.max() != 2:
        from shapely.ops import polygonize
        from shapely.geometry import MultiLineString
        rs = np.round(Q, 6).reshape(-1, 2, 2)
        faces = polygonize(unary_union(MultiLineString([[tuple(p) for p in s_] for s_ in rs.tolist()])))
        return [list(f.exterior.coords)[:-1] for f in faces]
    at: dict[int, list[int]] = {}                       # node -> the two endpoint ids there
    for e, nd in enumerate(inv):
        at.setdefault(int(nd), []).append(e)
    seen = np.zeros(n // 2, dtype=bool)
    rings = []
    for start in range(n // 2):
        if seen[start]:
            continue
        ring, e = [], 2 * start                         # walk out of endpoint e's segment through its other end
        while True:
            sg = e // 2
            if seen[sg]:
                break
            seen[sg] = True
            ring.append((round(float(Q[e, 0]), 6), round(float(Q[e, 1]), 6)))
            other = e ^ 1
            pair = at[int(inv[other])]
            e = pair[0] if pair[1] == other else pair[1]
        if len(ring) >= 3:
            rings.append(ring)
    return rings


def _nest_rings(rings: list[list[tuple[float, float]]]) -> list[Polygon]:
    """Rings nested into polygons with holes: a ring inside an even number of others is an outer boundary."""
    polys = [Polygon(r) for r in rings]
    polys = sorted([p for p in polys if p.area > 1e-3], key=lambda p: p.area, reverse=True)   # slivers (thread crests) are not holes
    from shapely.prepared import prep
    prepared = [prep(p) for p in polys]
    par, depth = [-1] * len(polys), [0] * len(polys)
    for i, p in enumerate(polys):
        q = p.representative_point()
        for j in range(i - 1, -1, -1):                  # the smallest larger ring around it
            if prepared[j].contains(q):
                par[i], depth[i] = j, depth[j] + 1
                break
    out = []
    for i, p in enumerate(polys):
        if depth[i] % 2 == 0:
            g = Polygon(p.exterior.coords, [polys[j].exterior.coords for j in range(len(polys)) if par[j] == i])
            # the slice has a vertex at every triangle it crosses (collinear ones along a flat wall, two per strip of a
            # cylinder): without this the latch job's contours carried twice the moves
            g = g.simplify(MESH_DEFLECTION / 2, preserve_topology=True)
            out.append(g if g.is_valid else g.buffer(0))
    return out


def _section_mesh(shape: Shape, z: float) -> MultiPolygon | None:
    P, I = _mesh_of(shape)
    if not len(I):
        return MultiPolygon([])
    # the plane on mesh vertices (a flat face at z): both sides, like the boolean that keeps the coincident face
    zs = [z - 1e-5, z + 1e-5] if np.any(np.abs(P[:, 2] - z) < 1e-7) else [z]
    polys: list[Polygon] = []
    for zz in zs:
        E = _slice_mesh(P, I, zz)
        E = E[np.hypot(*(E[:, 0] - E[:, 1]).T) > 1e-9]        # a vertex on the plane leaves zero-length segments
        if not len(E):
            continue
        rings = _stitch_rings(E)
        if rings is None:
            return None
        polys += _nest_rings(rings)
    return _as_multi(unary_union(polys)) if polys else MultiPolygon([])


def _section(shape: Shape, z: float, tol: float) -> MultiPolygon:
    try:                                    # the shape ∩ a plane face covering it; non-destructive (see ck.common)
        bb = shape.bounding_box(optimal=False)
        size = 2 * (max(abs(v) for v in list(bb.min) + list(bb.max)) + bb.diagonal)
        sk = ck.common(shape, Face.make_rect(size, size, Plane.XY.offset(z)))
    except Exception as e:  # noqa: BLE001
        raise CamError(f"section at z={z} failed: {e}")
    polys = [face_polygon(f, tol) for f in sk.faces()]
    u = unary_union([p for p in polys if not p.is_empty])
    return _as_multi(u)


def silhouette(shape: Shape, levels: int = 12, tol: float = 0.02) -> MultiPolygon:
    """Top-down outline: union of sections at several heights."""
    bb = shape.bounding_box()
    zs = [bb.min.Z + (bb.max.Z - bb.min.Z) * (i + 0.5) / levels for i in range(levels)]
    return _as_multi(unary_union([section(shape, z, tol) for z in zs]))


def outline(shape: Shape, tol: float = 0.02) -> MultiPolygon:
    """The part's outer outline from above, without holes (silhouette's outer boundaries): e.g. to chamfer its edge."""
    return _as_multi(unary_union([Polygon(g.exterior) for g in silhouette(shape, tol=tol).geoms]))


def face_outline(face) -> Polygon:
    """A flat face's outer boundary as a polygon, without its holes: e.g. to chamfer only its outside edge."""
    return Polygon(face_polygon(face).exterior)


def outline_face(model, min_cover: float = 0.8):
    """The part's top face that its outer edge belongs to: the highest flat up-facing face whose outer boundary covers
    at least `min_cover` of the outline seen from above (a plate's top, also when its edge is rounded or a feature rises
    above it). Returns the model's face info (id, center...) or None."""
    ol = outline(model.shape)
    best = None
    for f in model.faces:
        if f.kind != "PLANE" or not f.normal or f.normal[2] < 0.99:
            continue
        try:
            fo = face_outline(model.get_face(f.id))
        except Exception:  # noqa: BLE001
            continue
        if fo.area >= min_cover * ol.area and (best is None or f.center[2] > best.center[2]):
            best = f
    return best


def stock_minus(setup: Setup, shape: Shape, z: float, expand: float = 0.0, tol: float = 0.02) -> MultiPolygon:
    """Material to remove at level z: stock rectangle minus the part's section (2.5D roughing regions).
    `expand` grows the stock rectangle outward (use ≥ tool diameter when roughing around a part so the
    tool can run off the stock edge)."""
    return _as_multi(setup.stock.rect(expand).difference(section(shape, z, tol)))


def free_region(shape: Shape, z_top: float, z_bottom: float, within=None, step: float = 0.5, tol: float = 0.02) -> MultiPolygon:
    """Where a pocket can clear EVERY level from z_top down to z_bottom without cutting the shape: `within` (default:
    the shape's outline seen from above) minus the shape's material anywhere in that band. Use it for a pocket's
    region instead of one section: a section at one height misses what changes inside the band — an island or boss
    that rises into it, a countersink cone that widens below it, a step — and pocketing that section over the whole
    band cuts them away (the boat latch's recess floor took 0.5 mm off its thrust boss; its M4 holes were bored to the
    countersink's diameter all the way through). The band is sliced at both ends, at every flat face inside it and
    every `step` mm between; sections are cached, so rebuilding is cheap."""
    if z_top < z_bottom:
        z_top, z_bottom = z_bottom, z_top
    eps = 0.01
    zs = {round(z_bottom + eps, 4), round(z_top - eps, 4)}
    n = max(1, int(math.ceil((z_top - z_bottom) / max(step, 0.05))))
    zs |= {round(z_bottom + (z_top - z_bottom) * k / n, 4) for k in range(1, n)}
    for f in shape.faces():                                 # flat faces inside the band: slice just above and below them
        try:
            if f.geom_type != b3d.GeomType.PLANE or abs(abs(f.normal_at().Z) - 1.0) > 1e-6:
                continue
        except Exception:  # noqa: BLE001
            continue
        z = f.center().Z
        for zz in (z - eps, z + eps):
            if z_bottom < zz < z_top:
                zs.add(round(zz, 4))
    material = unary_union([section(shape, z, tol) for z in sorted(zs)])
    area = unary_union(_polys(within)) if within is not None else outline(shape, tol)
    return _as_multi(area.difference(material))


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


def regular_polygon(sides: int, across_flats: float, center=(0.0, 0.0), rotation: float = 0.0) -> Polygon:
    """A regular polygon (hex, square, ...) by its across-flats size - e.g. the cross-section rotary_profile turns a
    round bar into. `rotation` in degrees; with 0 a flat faces +Y (sits on top at A = 0)."""
    n = int(sides)
    if n < 3 or across_flats <= 0:
        raise CamError("regular_polygon: sides >= 3 and across_flats > 0")
    rc = across_flats / 2 / math.cos(math.pi / n)
    a0 = math.radians(rotation) + math.pi / 2 + math.pi / n
    return Polygon([(center[0] + rc * math.cos(a0 + 2 * math.pi * k / n), center[1] + rc * math.sin(a0 + 2 * math.pi * k / n)) for k in range(n)])


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
RAMP_DEG = 3.0                      # default entry ramp angle along the path (0 = straight plunge)
ADAPTIVE_DOC = 1.5                  # adaptive depth per level, x the tool Ø, where the machine sets none (Machine.adaptive_doc)
TAB_RAMP_DEG = 15.0                 # tabs: the tool ramps onto and off each tab at this angle (outside the tab's flat)
RAMP_FEED = 0.6                     # ramps and helices run at up to this fraction of the cutting feed (see ramp_feed)
ENTRY_GAP = 0.05                    # ramps / helices start this far above the material the op cuts (rapid to 0.5 above it)
COLORS = {"face": "#7fd1ff", "contour": "#4ea1ff", "pocket": "#4cd37a", "drill": "#ffd166", "parallel3d": "#c58cff", "engrave": "#ff9f7a", "adaptive": "#ff9f43", "rest": "#8ef0d0",
          "chamfer": "#f2a65a", "vcarve": "#e07bd8", "probe_set": "#ff5c8a", "probe_check": "#ffb3c7"}


def face(setup: Setup, tool: Tool, z_top: float | None = None, z_bottom: float | None = None, region=None,
         stepover: float | None = None, stepdown: float | None = None, angle: float = 0.0, one_way: bool = False,
         name: str = "Face", finish: bool = True, finish_stepover: float | None = None) -> Op:
    """Surface the stock top down to z_bottom with parallel passes over `region` (default: stock), at `angle` degrees
    from X. Zigzag by default; one_way=True cuts every row in the same direction (climb throughout, one uniform lay on
    a finished face) and rapids back just above the face. Rows start and end with the tool clear of the region.
    finish (default True): the last pass is a finishing pass - 0.1 mm (less on small tools) left by the levels above
    it, one way at `finish_stepover` (x Ø, default 0.45) for one uniform lay; a face no deeper than 0.2 mm is that
    pass alone. False: every level as the levels above."""
    from shapely import affinity
    op = Op(name, "face", tool, color=COLORS["face"])
    pb = PathBuilder(setup, tool, op)
    z_top = setup.stock.top if z_top is None else z_top
    z_bottom = z_top - (stepdown or tool.stepdown) if z_bottom is None else z_bottom
    so = (stepover or tool.stepover) * tool.diameter
    polys = _polys(region) or [setup.stock.rect()]
    reg0 = unary_union(polys).buffer(tool.radius * 0.9, join_style=2)   # let the tool run past the edges (square corners: full-width rows)
    piv = (reg0.centroid.x, reg0.centroid.y)
    reg = affinity.rotate(reg0, -angle, origin=piv) if angle else reg0  # rows along X in a frame turned by -angle
    def back(x, y):
        if not angle:
            return x, y
        q = affinity.rotate(Point(x, y), angle, origin=piv)
        return q.x, q.y
    minx, miny, maxx, maxy = reg.bounds
    ext = tool.radius * 0.1 + 1.0                        # rows start and end with the tool clear of the stock: plunges in air
    op.params = dict(z_top=z_top, z_bottom=z_bottom, stepover=so, angle=angle, one_way=one_way)
    leave_f = _finish_leaves(tool, None, None)[1]
    fso = (finish_stepover or FINISH_STEPOVER) * tool.diameter
    if finish and z_top - z_bottom > 2 * leave_f + 1e-9:
        levels = _levels(z_top, z_bottom + leave_f, stepdown or tool.stepdown) + [z_bottom]
    else:
        levels = _levels(z_top, z_bottom, stepdown or tool.stepdown)
    fin_from = len(levels) - 1 if finish else len(levels)
    if finish:
        op.params.update(finish_floor=leave_f if len(levels) > 1 else 0.0, finish_stepover=round(fso, 4), finish_pattern="parallel")
    all_one_way = one_way
    for li, z in enumerate(levels):
        fin_lv = li >= fin_from
        one_way = all_one_way or fin_lv
        so_lv = fso if fin_lv else so
        if fin_lv:
            op.params["finish_moves"] = [[len(op.moves), None]]
        rows = []
        y = miny
        while y <= maxy + 1e-9:
            rows.append(y); y += so_lv
        if li % 2 and not one_way:
            rows.reverse()                                 # the next level starts where this one ended
        direction = 1
        first = True
        for y in rows:
            line = LineString([(minx - 1, y), (maxx + 1, y)]).intersection(reg)
            segs = [line] if isinstance(line, LineString) else list(getattr(line, "geoms", []))
            segs = [s for s in segs if isinstance(s, LineString) and s.length > 0]
            segs.sort(key=lambda s: s.coords[0][0] * direction)
            for s in segs:
                c = list(s.coords)
                if direction < 0:
                    c = c[::-1]
                (ax, ay), (bx, by) = c[0], c[-1]
                ln = math.hypot(bx - ax, by - ay) or 1.0
                ux, uy = (bx - ax) / ln, (by - ay) / ln
                c = [back(ax - ux * ext, ay - uy * ext)] + [back(*q) for q in c[1:-1]] + [back(bx + ux * ext, by + uy * ext)]
                if first or pb.pos is None:
                    pb.rapid_xy(*c[0]); pb.plunge_to(z); first = False
                elif pb.pos[2] > z + 1e-6:                 # a new level, starting where the last one ended
                    pb.rapid_xy(*c[0])
                    if region is None:                     # facing the whole stock: the row ends are in the air, come down to the last level
                        pb.plunge_to(z, rapid_to=levels[li - 1], gap=0.3)
                    else:                                  # a region inside the stock: its row ends may be in solid, approach from the top
                        pb.plunge_to(z)
                elif one_way:                              # back over the face just above it (the row ends are in the air)
                    pb._add(RAPID, pb.pos[0], pb.pos[1], z + 0.3)
                    pb._add(RAPID, c[0][0], c[0][1], z + 0.3)
                    pb._add(PLUNGE, c[0][0], c[0][1], z, pb.plunge)
                else:
                    pb.feed_to(c[0][0], c[0][1], z)
                for q in c[1:]:
                    pb.feed_to(q[0], q[1], z)
            if not one_way:
                direction *= -1
    pb.retract()
    if op.params.get("finish_moves"):
        op.params["finish_moves"][0][1] = len(op.moves)
    return op


def contour(setup: Setup, tool: Tool, polys, z_top: float, z_bottom: float, side: str = "outside",
            stepdown: float | None = None, tabs: int = 0, tab_height: float = 3.0, tab_width: float = 8.0,
            climb: bool = True, stock_to_leave: float = 0.0, lead: float | None = None,
            ramp: float = RAMP_DEG, name: str = "Contour", tab_ramp: float = TAB_RAMP_DEG,
            finish: bool | None = None) -> Op:
    """Profile around polygons. side: 'outside' (tool outside material: outer rings AND holes),
    'inside' (tool inside, e.g. finishing a pocket wall), 'on' (centre on the line).
    lead: radius of the tangential arc lead-in/out (default tool radius outside, half inside, 0 for 'on';
    pass 0 to disable). ramp: entry angle in degrees — each pass descends along the profile instead of plunging
    (0 = plunge); passes run on without retracting between them. Tabs sit on straight runs where they can; the tool
    ramps onto and off each one at tab_ramp degrees outside its width (never faster down than the plunge feed).
    finish: one pass at full depth (the fewest passes the flutes allow) for a light wall cut instead of stepdown laps;
    None = automatic: a wall the pockets / adaptives above it in this script roughed (stock_to_leave) is finished that
    way unless a stepdown is given; False = stepdown laps (a slot)."""
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
    tab_ang = tab_ramp if tab_ramp and tab_ramp > 0 else 90.0
    # a finishing pass (light radial cut on a wall a roughing op already cleared beside): full depth in the fewest passes
    # the flutes allow instead of stepdown laps (7-11 laps cutting air on the latch walls). Auto (finish=None, no
    # stepdown given) when the roughing ops of this script cleared everything the cutter sweeps but a skin < ¼ Ø.
    fl = max(getattr(tool, "flute_length", 0.0) or 0.0, 0.1)
    fin_levels = _levels(z_top, z_bottom, max(fl - 0.2, 0.1))
    cleared = _cleared_beside(setup, z_top, z_bottom) if (finish or (finish is None and stepdown is None)) and side != "on" else None
    n_fin = 0
    op.params = dict(z_top=z_top, z_bottom=z_bottom, side=side, tabs=tabs, passes=len(levels), lead=lead, ramp=ramp)
    if tabs > 0:
        op.params.update(tab_top=round(tab_top, 4), tab_height=tab_height, tab_width=tab_width, tab_ramp=tab_ang, tab_xy=[])
    gap_min = 2 * tool.diameter                             # full-depth cut left between one tab's ramp and the next
    tab_block = None
    if tabs > 0:
        bl = [c for st, c, zt, zb in _OV.get("rough", []) if st is setup and zb < tab_top - 1e-6 and zt > z_bottom + 1e-6]
        tab_block = unary_union(bl).buffer(-0.05) if bl else None
    for ring in rings:
        ring = _start_on_straight(ring)
        dense = _densify(ring + [ring[0]], 0.5)
        total = sum(math.dist(a, b) for a, b in zip(dense, dense[1:]))
        n_tabs, ang = tabs, tab_ang
        foot = tab_height / math.tan(math.radians(ang)) if ang < 90 else 0.0
        if n_tabs > 0 and total / n_tabs < tab_width + 2 * foot + gap_min:
            # tabs + ramps would leave no full-depth cut between them: steeper ramps (up to 45°), then fewer tabs
            fit = (total / n_tabs - tab_width - gap_min) / 2
            if fit >= tab_height - 1e-9:
                ang = math.degrees(math.atan2(tab_height, max(fit, 1e-9))); foot = fit
                op.warnings.append(f"tabs: ramps steepened to {ang:.0f}° on a {total:.0f} mm ring so {n_tabs} tabs keep "
                                   f"{gap_min:g} mm of full-depth cut between them")
            else:
                ang = max(ang, 45.0); foot = tab_height / math.tan(math.radians(ang))
                n_tabs = max(1, int(total // (tab_width + 2 * foot + gap_min)))
                op.warnings.append(f"tabs: only {n_tabs} of {tabs} fit on a {total:.0f} mm ring with {ang:.0f}° ramps and "
                                   f"{gap_min:g} mm of full-depth cut between them; lower or narrower tabs fit more")
        bad = None
        if n_tabs > 0 and tab_block is not None:          # where another op cleared the kerf below the tab top: no tab metal
            ln_d = LineString(dense)
            def bad(c, ln_d=ln_d, total=total):
                from shapely.ops import substring
                a_, b_ = c - tab_width / 2, c + tab_width / 2
                parts = [substring(ln_d, max(a_, 0.0), min(b_, total))] + ([substring(ln_d, a_ + total, total)] if a_ < 0 else []) \
                    + ([substring(ln_d, 0.0, b_ - total)] if b_ > total else [])
                foot_ = unary_union([g.buffer(tool.radius) for g in parts if not g.is_empty])
                return foot_.intersection(tab_block).area > 0.2 * tab_width * tool.radius
        tab_centres = _tab_centres(ring, n_tabs, tab_width, foot, gap_min, bad) if n_tabs > 0 else []
        if bad is not None and any(bad(c) for c in tab_centres):
            op.warnings.append("tabs: another op cleared the kerf below the tab top at some tabs (no metal there): "
                               "place the tabs yourself or cut that neighbour after the release")
        if tab_centres:
            ln_ = LineString(dense)
            op.params["tab_xy"].append([[round(q.x, 3), round(q.y, 3)] for q in (ln_.interpolate(c) for c in tab_centres)])
        lead_in, lead_out = _lead_arcs(dense, lead, climb) if lead > 0 else ([], [])
        path = dense[:-1]                                   # closed ring for the lap/ramp walker
        fin = bool(finish) or (cleared is not None and _skin_only(dense, tool.radius, cleared))
        lv = fin_levels if fin else levels
        n_fin += fin
        # one entry per ring: lead-in just above the stock, then every pass ramps down along the profile from where
        # the previous pass ended and runs a full lap (tabs lift the tool at any depth below the tab top). A finishing
        # pass whose lead-in starts in cleared ground goes straight down there and leads in at depth.
        z_air = z_top + ENTRY_GAP                           # entry starts just above where this op cuts (material above is already gone)
        start = lead_in[0] if lead_in else dense[0]
        down = fin and cleared is not None and lead_in and cleared.covers(Point(start).buffer(tool.radius * 1.02))
        z_in = lv[0] if down else z_air
        pb.rapid_xy(*start); pb.plunge_to(z_in, rapid_to=z_air if down else z_top, gap=0.5)
        for x, y in lead_in[1:]:
            pb.feed_to(x, y, z_in)
        s_at = 0.0
        tz = lambda z: _tab_lift(tab_centres, tab_width, total, tab_top, ang, z) if tab_centres and z < tab_top - 1e-9 else None
        for z in lv:
            s_at = _lap_ramp(pb, path, z, ramp, s_at, tz(z))
        # with a lead-out: back to the start point at depth (the remainder of a lap) so the arc leaves from where it was built
        if s_at > 1e-6 and lead_out:
            _lap_ramp(pb, path, lv[-1], ramp, s_at, tz(lv[-1]), lap=False, upto=total)
        # the lead-out arc swings one radius away from the wall into the waste: at full depth it would half-slot fresh
        # stock (the Outline on the Z1 bit 1.15 mm in one go there, 2026-10-22). The arc climbs out instead - up to just
        # above the top within its first 40 % - so the wall still gets a tangential exit and the waste a thin wedge.
        # (Climbing along the wall before the arc is not safe: the lap's own ramp may have left that stretch shallow.)
        if lead_out and math.dist(pb.pos[:2], dense[0]) < 1e-3:
            z_exit, z_from = z_top + 0.2, pb.pos[2]
            for k, (x, y) in enumerate(lead_out, 1):
                pb.feed_to(x, y, z_from + (z_exit - z_from) * min(1.0, 2.5 * k / len(lead_out)))
        pb.retract()
    pb.retract()
    if n_fin:
        op.params.update(finish=n_fin, passes=len(fin_levels) if n_fin == len(rings) else op.params["passes"],
                         ap=round((z_top - z_bottom) / len(fin_levels), 3))      # the planned depth of one finishing pass
    return op


def _cleared_beside(setup: Setup, z_top: float, z_bottom: float):
    """What the pockets / adaptives built so far in this script (same setup) cleared over the whole band z_bottom..z_top
    (their regions as the tool reaches them, without their stock_to_leave), or None."""
    hits = [c for st, c, zt, zb in _OV.get("rough", []) if st is setup and zb <= z_bottom + 1e-6 and zt >= z_top - 1e-6]
    return unary_union(hits) if hits else None


def _skin_only(dense, r: float, cleared) -> bool:
    """Does a pass along `dense` meet only a skin thinner than ¼ Ø beside `cleared` (a finishing cut)?"""
    if cleared is None or cleared.is_empty:
        return False
    cut = LineString(dense).buffer(r, quad_segs=8).difference(cleared)
    return cut.is_empty or cut.buffer(-r / 4, quad_segs=4).area < 1e-3


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


def _lap_ramp(pb: PathBuilder, ring: list[tuple[float, float]], z: float, ramp: float, s0: float = 0.0,
              zmod=None, lap: bool = True, upto: float | None = None) -> float:
    """Entry along the path instead of a plunge. The tool is on the closed `ring` at arc length `s0` (its current z);
    it descends to `z` along the ring at `ramp` degrees (going round again if the ring is shorter than the ramp)
    and then, with `lap`, cuts one full lap at `z` so the sloped ramp floor is cleared (`upto`: instead, on to that
    arc length). Returns the arc length (mod the ring length) where the tool ends. zmod(s, z) may lift points (tabs;
    zmod.breaks: arc lengths emitted exactly). ramp <= 0: plunge where the tool is. No move descends faster than the
    plunge feed."""
    n = len(ring)
    seg = [math.dist(ring[i], ring[(i + 1) % n]) for i in range(n)]
    total = sum(seg)
    z_from = pb.pos[2]
    z_here = zmod(s0 % total, z) if (zmod is not None and total > 1e-9) else z
    depth = max(0.0, z_from - z_here)            # on a tab the profile carries the tool down (at the tab ramp angle)
    if ramp <= 0 and depth > 0:
        zt = z_here
        if zt < z_from - 1e-9:
            pb.plunge_to(zt)
        z_from, depth = z, 0.0
    L = depth / math.tan(math.radians(ramp)) if depth > 0 else 0.0
    tan_r = math.tan(math.radians(ramp)) if ramp > 0 else 0.0
    end = s0 + L + (total if lap else 0.0) if upto is None else max(upto, s0 + L)
    if end <= s0 + 1e-9 or total <= 1e-9:
        return s0 % total if total > 1e-9 else 0.0
    step = 0.5 if (L > 0 or zmod is not None) else total + 1.0     # sample finely only where z varies
    breaks = sorted(getattr(zmod, "breaks", ()) or ())

    def next_break(s_):
        if not breaks:
            return math.inf
        k = math.floor(s_ / total)
        for kk in (k, k + 1):
            for b in breaks:
                if b + kk * total > s_ + 1e-9:
                    return b + kk * total
        return math.inf
    # walk the ring from s0 to end, emitting every vertex crossing, every `step` while z varies, and the ramp end
    i = 0; s_vert = 0.0
    while i < n and s_vert + seg[i] <= s0 + 1e-9:                   # segment containing s0
        s_vert += seg[i]; i += 1
    i %= n
    s = s0
    def point(i_seg, s_local):
        a, b = ring[i_seg], ring[(i_seg + 1) % n]
        t = s_local / seg[i_seg] if seg[i_seg] > 1e-12 else 0.0
        return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
    def emit(s_at, i_seg, s_local):
        x, y = point(i_seg, s_local)
        zz = max(z, z_from - (s_at - s0) * tan_r) if L > 0 else z
        if zmod is not None:
            zz = zmod(s_at % total, zz)
        ramping = L > 0 and s_at <= s0 + L + 1e-9                  # the ramp (to its end point) runs at the ramp feed
        f = pb.ramp_feed(ramp) if ramping else pb.feed
        dz = pb.pos[2] - zz
        if dz > 1e-9:                                               # coming down (a ramp, off a tab): at most the plunge rate
            f = min(f, max(pb.plunge * math.dist(pb.pos, (x, y, zz)) / dz, pb.plunge))
        pb.feed_to(x, y, zz, f)
    def corner(i_seg):                                                # does the path turn at the end of segment i?
        a, b, c = ring[i_seg], ring[(i_seg + 1) % n], ring[(i_seg + 2) % n]
        ux, uy, vx, vy = b[0] - a[0], b[1] - a[1], c[0] - b[0], c[1] - b[1]
        lu, lv = math.hypot(ux, uy), math.hypot(vx, vy)
        if lu < 1e-12 or lv < 1e-12:
            return False
        return (ux * vx + uy * vy) / (lu * lv) < math.cos(math.radians(8))
    s_last = s0
    while s < end - 1e-9:
        seg_end = s_vert + seg[i]                                     # arc length at the end of segment i
        nb = next_break(s)
        nxt = min(seg_end, end, nb)
        ramping = L > 0 and s < s0 + L - 1e-9
        if ramping and s0 + L < nxt - 1e-9:
            nxt = s0 + L                                              # stop exactly at the ramp end
        if ramping or zmod is not None:
            while s + step < nxt - 1e-9:
                s += step; emit(s, i, s - s_vert); s_last = s
        s = nxt
        at_vertex = abs(s - seg_end) < 1e-9
        # while ramping, fine vertices of a smooth curve are skipped (sampled by `step` instead); corners always emitted
        if not (ramping and at_vertex and s - s_last < step * 0.5 and not corner(i)) or s >= end - 1e-9 or abs(s - (s0 + L)) < 1e-9 \
                or abs(s - nb) < 1e-9:
            emit(s, i, s - s_vert); s_last = s
        if at_vertex:
            s_vert = seg_end; i = (i + 1) % n
            if i == 0:
                s_vert -= total; s -= total; end -= total; s0 -= total; s_last -= total   # wrap: keep arc lengths small
    return s % total


def _tab_centres(ring: list[tuple[float, float]], tabs: int, width: float, foot: float, gap_min: float = 0.5,
                 bad=None) -> list[float]:
    """Arc lengths of the tab centres along a closed ring: each tab's flat (and if they fit its ramps, `foot` either
    side) on a straight run, as near to an even spread as the straights allow; a tab with no straight long enough
    falls back to an even spread turned to put as many tabs on straights as it can. Tabs on corner arcs hold badly
    and the tool leaves them with a dig. bad(s): a tab centred there would have no metal (cleared by another op)."""
    n = len(ring)
    seg = [math.dist(ring[i], ring[(i + 1) % n]) for i in range(n)]
    total = sum(seg)
    if tabs <= 0 or total <= 1e-9:
        return []
    ideal = [total * (i + 0.5) / tabs for i in range(tabs)]
    runs, s = [], 0.0                                   # [s_start, s_end, heading]: segments within 2° of the run's first
    for i in range(n):
        if seg[i] > 1e-9:
            h = math.atan2(ring[(i + 1) % n][1] - ring[i][1], ring[(i + 1) % n][0] - ring[i][0])
            if runs and abs(runs[-1][1] - s) < 1e-9 and abs(math.remainder(h - runs[-1][2], 2 * math.pi)) <= math.radians(2):
                runs[-1][1] = s + seg[i]
            else:
                runs.append([s, s + seg[i], h])
        s += seg[i]
    if len(runs) > 1 and runs[0][0] < 1e-9 and abs(runs[-1][1] - total) < 1e-9 and \
            abs(math.remainder(runs[0][2] - runs[-1][2], 2 * math.pi)) <= math.radians(2):
        runs[0][0] = runs[-1][0] - total; runs.pop()      # the run through the ring's start
    gap = width + 2 * foot + gap_min
    cdist = lambda a, b: min(abs(a - b) % total, total - abs(a - b) % total)
    out: list[float] = []
    for c0 in ideal:
        pick = None
        for m in (foot, 0.0):
            best = None
            for a, b, _ in runs:
                lo, hi = a + width / 2 + m, b - width / 2 - m
                if hi < lo - 1e-9:
                    continue
                for k in range(int((hi - lo) / 0.5) + 2):
                    p = min(lo + 0.5 * k, hi) % total
                    if all(cdist(p, q) >= gap for q in out) and (best is None or cdist(p, c0) < cdist(best, c0)) \
                            and not (bad and bad(p)):
                        best = p
            if best is not None:
                pick = best; break
        if pick is None:
            break
        out.append(pick)
    if len(out) == tabs:
        return out
    # not every tab finds a straight with room: an even spread, turned to put as many flats on straights as it can
    def on_straight(c):
        return any(a + width / 2 - 1e-6 <= cc <= b - width / 2 + 1e-6 for a, b, _ in runs for cc in (c, c - total, c + total))
    step = total / tabs
    best_off = max((k * 0.5 for k in range(int(step / 0.5) + 1)),
                   key=lambda off: (-sum(bool(bad and bad((c + off) % total)) for c in ideal),
                                    sum(on_straight((c + off) % total) for c in ideal), -off))
    return [(c + best_off) % total for c in ideal]


def _tab_lift(centres: list[float], width: float, total: float, tab_top: float, angle: float, z: float):
    """zmod for one pass at z: the tool rides each tab's flat (`width` along the path) at tab_top and ramps on and off
    it at `angle` outside that width. `breaks`: the arc lengths where the lift profile bends (emitted exactly)."""
    k = math.tan(math.radians(angle)) if angle > 0 else math.inf
    foot = (tab_top - z) / k if k < math.inf else 0.0

    def zmod(s, zz):
        for c in centres:
            d = abs(s - c) % total
            d = min(d, total - d) - width / 2
            h = tab_top if d <= 1e-9 else tab_top - d * k
            if h > zz:
                zz = h
        return zz
    zmod.breaks = sorted({round((c + o) % total, 9) for c in centres for o in (-width / 2 - foot, -width / 2, width / 2, width / 2 + foot)})
    return zmod


def _in_tab(s: float, centres: list[float], width: float, total: float) -> bool:
    for c in centres:
        d = abs(s - c)
        d = min(d, total - d)
        if d <= width / 2:
            return True
    return False


def pocket(setup: Setup, tool: Tool, polys, z_top: float, z_bottom: float, stepdown: float | None = None,
           stepover: float | None = None, finish: bool | str = True, climb: bool = True, rest_from: Tool | None = None,
           stock_to_leave: float = 0.0, ramp: float = RAMP_DEG, name: str = "Pocket", strategy: str = "auto",
           finish_pattern: str = "parallel", finish_angle: float = 0.0, finish_stepover: float | None = None,
           finish_wall: float | None = None, finish_floor: float | None = None) -> Op:
    """Clear closed regions (holes in the polygons are islands) with concentric inward passes,
    innermost first, wall pass last. rest_from=<bigger tool>: only machine what that tool left.
    stock_to_leave: radial allowance left on the walls for a finishing contour (an explicit allowance makes this a
    roughing op: no finish unless finish="floor" / "walls" asks). ramp: entry angle in degrees — the tool ramps down
    along the innermost ring (a helix on a small ring) instead of plunging (0 = plunge).
    strategy: "auto" (a round hole 1.3-3x the tool Ø is helix-bored, see bore(); other regions get rings), "rings",
    "bore" (round regions bored, the rest rings), "adaptive" (constant engagement at the stepover, see adaptive(); its
    depth per level is the machine's adaptive_stepdown, default 1.5 x Ø, unless a stepdown is given). auto never picks
    adaptive: on the latch job it was no faster than rings at the same MRR limit and its load was no steadier.
    finish (default True): rough leaving `finish_wall` on the walls (default 0.2 mm, less for tools under Ø2.5) and
    `finish_floor` on the floor (0.1 mm), then finish each region: the floor with `finish_pattern` - "parallel" (one-way
    rows at `finish_angle`° from X, 0 = along X, all cut the same way (climb), the tool lifted just clear of the floor
    to go back), "offset" (rings out from the middle) or "spiral" (one continuous spiral out) - at `finish_stepover`
    (fraction of Ø, default 0.45), then the walls in one full-depth pass (contour finish) that also takes the floor
    edge. "walls" / "floor": only that part; False: one roughing strategy to size. A through-cut (z_bottom at or below
    the stock bottom) has no floor; bored holes are finished by their last circle."""
    if strategy not in POCKET_STRATEGIES:
        raise CamError(f"pocket: strategy must be one of {POCKET_STRATEGIES}")
    if finish not in (True, False, "walls", "floor"):
        raise CamError("pocket: finish is True, False, 'walls' or 'floor'")
    if finish_pattern not in FINISH_PATTERNS:
        raise CamError(f"pocket: finish_pattern must be one of {FINISH_PATTERNS}")
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
    # an earlier pocket's floor at this op's top whose finishing this op makes pointless (Program.add drops it there):
    # its finishing allowance is still standing, so this op starts that much higher
    z_top_given = z_top
    above = max([lf for st_, g_, zf, lf in _OV.get("floors", []) if st_ is setup and abs(zf - z_top) < 1e-6
                 and g_.intersects(region)] or [0.0])
    z_top = z_top + above
    # what gets finished: the walls unless an allowance is left for another op; the floor unless it's a through-cut
    explicit = stock_to_leave > 0
    # the default finish is a flat endmill's: a ball / bull / V tool's floor is shaped by its stepover (scallops), not
    # by a flat raster at 0.45 Ø - for those only finish="walls" / "floor" asks for it
    flat_tool = tool.type in ("flat", "drill")
    fin_w = rest_from is None and (finish == "walls" or (finish is True and not explicit and flat_tool))
    fin_f = rest_from is None and (finish == "floor" or (finish is True and not explicit and flat_tool)) and z_bottom > setup.stock.bottom + 1e-6
    leave_w, leave_f = _finish_leaves(tool, finish_wall, finish_floor)
    leave_w = leave_w if fin_w else 0.0
    if not fin_f or z_top - z_bottom <= 2 * leave_f + 1e-9:          # a skim this shallow is its own finish
        fin_f, leave_f = False, 0.0
    fso = (finish_stepover or FINISH_STEPOVER) * tool.diameter
    if not (0 < fso <= tool.diameter):
        raise CamError("pocket: finish_stepover must be (0, 1] × diameter")
    z_rough = z_bottom + leave_f
    if not _shells(region, tool.radius, so):
        if rest_from is not None:
            op.warnings.append(f"rest: {tool.label()} does not fit in what {rest_from.label()} left")
            return op
        raise CamError(f"pocket: tool Ø{tool.diameter} does not fit in the region")
    levels = _levels(z_top, z_rough, stepdown or tool.stepdown)
    op.params = dict(z_top=z_top_given, z_bottom=z_bottom, stepover=so, passes=len(levels), rings=len(_shells(region, tool.radius, so)),
                     ramp=ramp)
    if above:
        op.params["floor_stock_above"] = above
    z_air = z_top + ENTRY_GAP                               # entry starts just above where this op cuts
    if strategy == "adaptive" and rest_from is None:      # constant engagement at the stepover, the pocket's stepdown
        lw_all = leave_w if fin_w and not region.buffer(-(leave_w + tool.radius)).is_empty else 0.0
        ad = adaptive(setup, tool, region, z_top, z_rough, stepover=min(so / tool.diameter, 0.5),
                      stepdown=stepdown, climb=climb, name=name, stock_to_leave=lw_all)
        op.moves, op.warnings = ad.moves, op.warnings + ad.warnings
        pb.pos = tuple(op.moves[-1][1:4]) if op.moves else None
        op.params.update(strategy="adaptive", passes=ad.params.get("passes"), stepdown=ad.params.get("stepdown"), ap=ad.params.get("ap"), **{k: ad.params[k] for k in ("engagement", "links", "hops", "lifts", "retracts") if k in ad.params})
        if fin_w or fin_f:
            for R0 in _nn_order(_polys(region), lambda R: (R.centroid.x, R.centroid.y), pb.pos[:2] if pb.pos else None):
                _pocket_finish(pb, R0, lw_all, tool, z_top, z_bottom, leave_f, fin_w, fin_f, finish_pattern, finish_angle, fso,
                               climb, ramp)
        pb.retract()
        _note_cleared(setup, region, tool, z_top, z_bottom)
        _finish_params(op, leave_w if fin_w else 0.0, leave_f, finish_pattern if fin_f else None, finish_angle, fso)
        return op
    # separate regions (two pockets) are finished one after the other through all depths — not one level across all of
    # them — and visited nearest-neighbour; within a region the next level is plunged where the tool already is
    regions = _nn_order(_polys(region), lambda R: (R.centroid.x, R.centroid.y), pb.pos[:2] if pb.pos else None)
    op.params.update(regions=len(regions))
    bored = 0
    for R0 in regions:
        disc = _disc(R0) if strategy in ("auto", "bore") else None
        if disc and BORE_RANGE[0] <= disc[2] / tool.diameter <= BORE_RANGE[1] and z_top - z_bottom <= tool.flute_length + 1e-6:
            _helix_bore(pb, disc[0], disc[1], disc[2], z_top, z_bottom, tool, so, ramp, climb, stepdown or tool.stepdown)
            bored += 1                                      # its last full circle at the bottom is the finish
            continue
        if strategy == "bore":
            op.warnings.append(f"bore: a region {'Ø%.2f' % disc[2] if disc else 'that is not round'} is not "
                               f"{BORE_RANGE[0]:g}-{BORE_RANGE[1]:g}x the {tool.label()}'s Ø{tool.diameter:g} (or deeper than its "
                               "flutes): pocketed with rings instead")
        lw = leave_w if fin_w and not R0.buffer(-(leave_w + tool.radius)).is_empty else 0.0
        R = R0.buffer(-lw, join_style=1) if lw else R0
        shells_R = _shells(R, tool.radius, so)
        if not shells_R:
            continue
        _rings_ramped(pb, R, shells_R, tool, levels, climb, ramp, z_air)
        if (fin_w and lw > 0) or fin_f:
            _note_cleared(setup, R, tool, z_top, z_bottom)          # (the floor pass takes the last leave_f before the walls)
            _pocket_finish(pb, R0, lw, tool, z_top, z_bottom, leave_f, fin_w and lw > 0, fin_f, finish_pattern, finish_angle,
                           fso, climb, ramp)
        pb.retract()
    pb.retract()
    if bored:
        op.params.update(bored=bored)
    _finish_params(op, leave_w if fin_w else 0.0, leave_f, finish_pattern if fin_f else None, finish_angle, fso)
    _note_cleared(setup, region, tool, z_top, z_bottom)
    return op


FINISH_PATTERNS = ("parallel", "offset", "spiral")
FINISH_STEPOVER = 0.45                  # floor finish stepover, × Ø


def _finish_leaves(tool: Tool, wall: float | None, floor: float | None) -> tuple[float, float]:
    """What roughing leaves for the finish: walls 0.2 mm (0.08 Ø on small tools, at least 0.05), floor 0.1 mm (0.04 Ø)."""
    D = tool.diameter
    w = min(0.2, max(0.05, 0.08 * D)) if wall is None else max(float(wall), 0.0)
    f = min(0.1, max(0.03, 0.04 * D)) if floor is None else max(float(floor), 0.0)
    return round(w, 3), round(f, 3)


def _finish_params(op: Op, leave_w: float, leave_f: float, pattern: str | None, angle: float, fso: float) -> None:
    """op.params for the finishing passes: what was left for them, the pattern, and the move ranges (finish_moves for
    fit_feeds, ap_moves = the planned depth of the full-depth wall passes for the simulation's BITE check)."""
    rngs = getattr(op, "_finish_ranges", [])
    for k in ("finish_moves", "ap_moves"):
        op.params.pop(k, None)
    if not (leave_w or pattern) or not rngs:
        return
    op.params.update(finish_wall=leave_w, finish_floor=leave_f, finish_pattern=pattern, finish_angle=angle if pattern == "parallel" else None,
                     finish_stepover=round(fso, 4), finish_moves=[[g["k0"], g["k1"]] for g in rngs])
    ap = [[g["k0"], g["k1"], round(g["ap"], 3)] for g in rngs if g.get("ap") is not None]
    if ap:
        op.params["ap_moves"] = ap


def _shells(R, r: float, so: float) -> list[list[Polygon]]:
    """The concentric offsets of a region from the wall inward (tool-centre rings), outermost first."""
    shells = []
    d = r
    while True:
        ps = _polys(R.buffer(-d, join_style=1))
        if shells:                      # slivers (a neck just wide enough) lie within the ring outside them: no ring
            ps = [q for q in ps if q.area > 0.05 * so * so]
        if not ps:
            break
        shells.append(ps)
        d += so
    return shells


def _low_link(pb: PathBuilder, target, z: float, z_link: float, polys, ok) -> None:
    """From the tool's place on the floor to `target` at z: up to z_link (just clear of the stock left on the floor),
    across inside `ok` (the tool-centre area: straight or round the region), down. Retracts when there's no way inside."""
    pos = pb.pos[:2]
    if math.dist(pos, target) < 1e-6:
        if pb.pos[2] > z + 1e-6:
            pb._add(PLUNGE, target[0], target[1], z, pb.plunge)
        return
    route = _route_inside(pos, target, polys, ok) if pb.pos[2] <= z_link + 1e-6 else None
    if route is None:
        pb.rapid_xy(*target); pb.plunge_to(z, rapid_to=z_link, gap=0.3)
        return
    if pb.pos[2] < z_link:
        pb._add(RAPID, pos[0], pos[1], z_link)
    for x_, y_ in route:
        pb._add(RAPID, x_, y_, z_link)
    pb._add(PLUNGE, target[0], target[1], z, pb.plunge)


def _floor_rows(dom, so: float, angle: float, climb: bool, start=None) -> list[list[tuple[float, float]]]:
    """One-way rows over the tool-centre area `dom` at `angle`° from X, so apart: every row cut in the same direction
    with the next row on its left (climb). The rows are grouped into zones where each row has one row after it (a
    region round an island or with lobes splits into several), and each zone is cut row after row before the next
    (nearest first, from `start`), so going back for the next row is a short hop, never a trip round the region."""
    from shapely import affinity
    piv = dom.centroid
    D = affinity.rotate(dom, -angle, origin=piv) if angle else dom
    minx, miny, maxx, maxy = D.bounds
    h = maxy - miny
    n = max(1, math.ceil((h - 0.02) / so - 1e-9))
    ys = [miny + 0.01 + (h - 0.02) * k / n for k in range(n + 1)] if h > 0.02 else [(miny + maxy) / 2]
    sgn = 1.0 if climb else -1.0
    rows = []                                                       # per y: [(x_start, x_end, coords)]
    for y in ys:
        cut = LineString([(minx - 1, y), (maxx + 1, y)]).intersection(D)
        segs = [g for g in (cut.geoms if hasattr(cut, "geoms") else [cut]) if g.geom_type == "LineString" and g.length > 1e-3]
        out = []
        for g in segs:
            c = list(g.coords)
            c = c if (c[-1][0] - c[0][0]) * sgn > 0 else c[::-1]
            out.append((min(c[0][0], c[-1][0]), max(c[0][0], c[-1][0]), c))
        rows.append(sorted(out, key=lambda t_: sgn * t_[0]))
    ov = lambda a_, b_: min(a_[1], b_[1]) - max(a_[0], b_[0]) > -1e-6           # noqa: E731 - x ranges overlap
    succ = {(i, j): [k for k, t_ in enumerate(rows[i + 1]) if ov(rows[i][j], t_)] if i + 1 < len(rows) else []
            for i in range(len(rows)) for j in range(len(rows[i]))}
    pred: dict = {}
    for (i, j), ks in succ.items():
        for k in ks:
            pred.setdefault((i + 1, k), []).append(j)
    chains, seen = [], set()
    for i in range(len(rows)):
        for j in range(len(rows[i])):
            if (i, j) in seen:
                continue
            ch, cur = [], (i, j)
            while cur is not None and cur not in seen:
                seen.add(cur); ch.append(cur)
                nx_ = succ.get(cur) or []
                cur = (cur[0] + 1, nx_[0]) if len(nx_) == 1 and len(pred.get((cur[0] + 1, nx_[0]), [])) == 1 else None
            chains.append(ch)
    back = (lambda c: [(q.x, q.y) for q in (affinity.rotate(Point(p_), angle, origin=piv) for p_ in c)]) if angle else (lambda c: c)
    fwd = (lambda p_: (lambda q: (q.x, q.y))(affinity.rotate(Point(p_), -angle, origin=piv))) if angle else (lambda p_: p_)
    at = fwd(start) if start is not None else None
    order = []
    rest = list(chains)
    while rest:
        k = 0 if at is None else min(range(len(rest)), key=lambda q: math.dist(rows[rest[q][0][0]][rest[q][0][1]][2][0], at))
        ch = rest.pop(k)
        order.append(ch)
        at = rows[ch[-1][0]][ch[-1][1]][2][-1]
    return [back(rows[i][j][2]) for ch in order for i, j in ch]


def _offset_rings(dom, so: float, climb: bool):
    """Rings of `dom` from the middle out (innermost first), each climb (the uncut floor outside it on the left), as
    (level, polygon, [rings])."""
    out, k = [], 0
    while True:
        ps = _polys(dom.buffer(-k * so, join_style=1)) if k else _polys(dom)
        ps = [q for q in ps if q.area > 1e-4]
        if not ps:
            break
        for q in ps:
            q = orient(q, 1.0)
            rr = [_ring_coords(q.exterior, False)] + [_ring_coords(h_, True) for h_ in q.interiors]
            out.append((k, q, [r_[::-1] for r_ in rr] if not climb else rr))
        k += 1
    return out[::-1]


def _floor_block(pb: PathBuilder, dom, z: float, z_link: float, pattern: str, angle: float, fso: float, climb: bool,
                 ramp: float, lap: bool) -> None:
    """One self-contained floor finish over the tool-centre area `dom` at z (from safe Z, back to safe Z). lap: first
    once round the area's edge (ramping down onto it from just above the floor stock), so every row then starts in
    cleared ground; without it the edge was cleared already (a finished wall beside it). Rows ("parallel") run one
    way at `angle`, the next row on their left (climb); the tool lifts to z_link (just clear of the uncut floor) to go
    back. "offset" / "spiral": rings from the middle out, the first ramped in."""
    op = pb.op
    dpolys = _polys(dom)
    if not dpolys:
        return
    ok = dom.buffer(1e-3, join_style=1)
    rr_all = _offset_rings(dom, fso, climb)
    pb.retract()

    def enter_ramped(ring):                                 # onto a ring from above the floor stock, ramping down along it
        if pb.pos is not None and pb.pos[2] <= z_link + 1e-6:
            _low_link(pb, ring[0], z_link, z_link, dpolys, ok)
        else:
            pb.rapid_xy(*ring[0]); pb.plunge_to(z_link, rapid_to=z_link, gap=0.3)
        _lap_ramp(pb, ring, z, ramp if ramp > 0 else RAMP_DEG, 0.0)

    def go(pt):                                             # down in cleared ground
        if pb.pos is None or pb.pos[2] > z_link + 1e-6:
            pb.rapid_xy(*pt); pb.plunge_to(z, rapid_to=z_link, gap=0.3)
        else:
            _low_link(pb, pt, z, z_link, dpolys, ok)
    if pattern == "parallel":
        if lap:
            for _, _, rr in [x for x in rr_all if x[0] == 0]:
                for rg in rr:
                    enter_ramped(_ring_from(rg, pb.pos[:2]) if pb.pos is not None and pb.pos[2] <= z_link else rg)
        for row in _floor_rows(dom, fso, angle, climb, pb.pos[:2] if pb.pos is not None else None):
            go(row[0])
            for x_, y_ in row[1:]:
                pb.feed_to(x_, y_, z)
    else:
        spiral = pattern == "spiral" and all(not q.interiors for _, q, _ in rr_all) and len({k for k, _, _ in rr_all}) == len(rr_all)
        if pattern == "spiral" and not spiral and "finish_pattern spiral" not in " ".join(op.warnings):
            op.warnings.append("finish_pattern spiral: the floor splits or has islands - offset rings there instead")
        prev = None
        first = True
        for k, q, rr in rr_all:
            for rg in rr:
                if spiral and prev is not None:
                    # one lap blending from the inner ring (where the tool is) out onto this one: a continuous spiral
                    n = max(24, int(max(LineString(prev + [prev[0]]).length, LineString(rg + [rg[0]]).length) / 0.4))
                    A = _resample_closed(prev, pb.pos[:2], n)
                    B = _resample_closed(rg, A[0], n)
                    for j in range(1, n + 1):
                        t = j / n
                        a_, b_ = A[j % n], B[j % n]
                        pb.feed_to(a_[0] + (b_[0] - a_[0]) * t, a_[1] + (b_[1] - a_[1]) * t, z)
                    prev = _resample_closed(rg, pb.pos[:2], n)
                    continue
                ring = _ring_from(rg, pb.pos[:2]) if pb.pos is not None else rg
                if first or pb.pos[2] > z_link + 1e-6:
                    enter_ramped(ring)                      # the middle still carries the floor stock
                    first = False
                elif math.dist(pb.pos[:2], ring[0]) <= 1.5 * fso + 0.05 and ok.covers(LineString([pb.pos[:2], ring[0]])):
                    pb.feed_to(ring[0][0], ring[0][1], z)   # a step out onto the next ring
                    for x_, y_ in ring[1:] + [ring[0]]:
                        pb.feed_to(x_, y_, z)
                else:
                    enter_ramped(ring)
                prev = rg
        if spiral and prev is not None and len(rr_all) > 1:  # the last ring once round in full
            for x_, y_ in prev[1:] + [prev[0]]:
                pb.feed_to(x_, y_, z)
    pb.retract()


def _pocket_finish(pb: PathBuilder, R0, lw: float, tool: Tool, z_top: float, z_bottom: float, leave_f: float,
                   fin_w: bool, fin_f: bool, pattern: str, angle: float, fso: float, climb: bool, ramp: float) -> None:
    """Finish one roughed region: the walls in one full-depth contour pass (it also cuts the band of floor beside them,
    so the floor rows start in cleared ground), then the floor (a self-contained block, see _floor_block) stopping
    0.05 mm short of the finished walls; without a wall finish the floor block laps its edge first."""
    op, setup, r = pb.op, pb.setup, tool.radius
    rngs = op.__dict__.setdefault("_finish_ranges", [])
    if fin_w:
        wall = contour(setup, tool, R0, z_top=z_top, z_bottom=z_bottom, side="inside", climb=climb, ramp=ramp, finish=True,
                       name=op.name)
        op.warnings.extend(w_ for w_ in wall.warnings if w_ not in op.warnings)
        if wall.moves:
            pb.retract()
            k0 = len(op.moves)
            op.moves.extend(wall.moves)
            pb.pos = tuple(op.moves[-1][1:4])
            rngs.append({"k0": k0, "k1": len(op.moves), "ap": float(wall.params.get("ap") or (z_top - z_bottom))})
    if fin_f:
        dom = R0.buffer(-(r + (0.05 if fin_w else lw)), join_style=1)
        if dom.is_empty:
            return
        plan = dict(z=z_bottom, z_link=z_bottom + leave_f + 0.1, pattern=pattern, angle=angle, fso=fso, climb=climb,
                    ramp=ramp, lap=not fin_w, r=r, leave=leave_f)
        _OV.setdefault("floors", []).append((setup, dom.buffer(r), z_bottom, leave_f))
        k0 = len(op.moves)
        _floor_block(pb, dom, plan["z"], plan["z_link"], pattern, angle, fso, climb, ramp, plan["lap"])
        rngs.append({"k0": k0, "k1": len(op.moves), "ap": None, "floor": plan, "dom": dom})


def _trim_floor_finishes(prog, new_op: Op) -> None:
    """Program.add: a pocket floor finished earlier that this op cuts deeper is not finished there - its floor block
    is rebuilt over what is left (dropped when nothing is)."""
    st = prog.setup_of(new_op)
    if new_op.kind == "probe" or not new_op.moves:
        return
    foot_cache: dict = {}
    mv = new_op.moves
    P = np.array([m_[1:4] for m_ in mv], dtype=float)
    cutting = np.array([m_[0] != RAPID for m_ in mv])
    zmin_new = float(P[cutting, 2].min()) if cutting.any() else math.inf
    zt_new = (new_op.params or {}).get("z_top")
    above_new = (new_op.params or {}).get("floor_stock_above", 0.0)
    for op in prog.ops:
        rngs = getattr(op, "_finish_ranges", None)
        if not rngs or prog.setup_of(op) is not st or not any("floor" in g for g in rngs):
            continue
        changed = False
        for g in rngs:
            plan = g.get("floor")
            if not plan or zmin_new >= plan["z"] - 0.02 or zt_new is None:
                continue
            # only an op that knows the allowance is standing there (a pocket that started above it, see pocket())
            # may take the floor finish's place; anything else expects the floor at its z
            if not (new_op.kind in ("pocket", "rest") and (zt_new + above_new >= plan["z"] + plan["leave"] - 1e-6)):
                continue
            z = plan["z"]
            m_ = plan["r"] + new_op.tool.radius
            bx = g["dom"].bounds
            bx = (bx[0] - m_, bx[1] - m_, bx[2] + m_, bx[3] + m_)      # = dom.buffer(m_).bounds, without the buffer
            deep = cutting & (P[:, 2] < z - 0.02)
            pair = deep[1:] & deep[:-1]
            # only a deep move within the floor's reach can take anything off it: most (op, floor) pairs of a long
            # script are in different places, and the footprint below cost 6 ms a buffer x 10,000 on the latch job
            lo, hi = np.minimum(P[1:, :2], P[:-1, :2]), np.maximum(P[1:, :2], P[:-1, :2])
            if not (pair & (hi[:, 0] >= bx[0]) & (lo[:, 0] <= bx[2]) & (hi[:, 1] >= bx[1]) & (lo[:, 1] <= bx[3])).any():
                continue
            key = (z, tuple(round(v, 1) for v in bx))
            if key not in foot_cache:
                inb = ((P[:, 0] >= bx[0] - 50) & (P[:, 0] <= bx[2] + 50) & (P[:, 1] >= bx[1] - 50) & (P[:, 1] <= bx[3] + 50))
                pair &= inb[1:] | inb[:-1]
                lines, cur = [], []
                for i in range(1, len(P)):                      # runs of deep cutting moves -> polylines, per level
                    if pair[i - 1] and (not cur or abs(P[i, 2] - cur[1]) < 0.05):
                        if not cur:
                            cur = [[tuple(P[i - 1, :2])], float(P[i - 1, 2])]
                        cur[0].append(tuple(P[i, :2]))
                    elif cur:
                        lines.append(cur); cur = []
                        if pair[i - 1]:
                            cur = [[tuple(P[i - 1, :2]), tuple(P[i, :2])], float(P[i - 1, 2])]
                if cur:
                    lines.append(cur)
                # the footprint, buffered one level at a time then unioned: the same union as buffering every deep move
                # together, but GEOS then noded ten levels of the same rings against each other (55 s on the live latch
                # job's 31 footprints)
                by_z: dict = {}
                for l_, z_ in lines:
                    by_z.setdefault(round(z_, 2), []).append(l_)
                parts = []
                for ls in by_z.values():
                    geo = [(g if g.length > 1e-6 else Point(l_[0])) if len(l_) > 1 else Point(l_[0])
                           for l_ in ls for g in [LineString(l_).simplify(0.01) if len(l_) > 1 else None]]
                    parts.append(GeometryCollection(geo).buffer(new_op.tool.radius, quad_segs=6))
                foot_cache[key] = unary_union(parts) if parts else None
            F = foot_cache[key]
            dom = g["dom"]
            if F is None or F.is_empty or not F.intersects(dom.buffer(plan["r"])):
                continue
            keep = F.buffer(-plan["r"] + 1e-3, join_style=1)            # tool centres whose whole footprint is cut deeper later
            nd = dom.difference(keep) if not keep.is_empty else dom
            nd = unary_union([q for q in _polys(nd.buffer(-0.01, join_style=1).buffer(0.01, join_style=1)) if q.area > 0.25])
            if dom.area - nd.area < 0.1 * dom.area:             # most of it stays: leave the block as it is
                continue
            tmp = Op(op.name, op.kind, op.tool)
            pb = PathBuilder(st, op.tool, tmp)
            if not nd.is_empty:
                _floor_block(pb, nd, z, plan["z_link"], plan["pattern"], plan["angle"], plan["fso"], plan["climb"], plan["ramp"], True)
            k0, k1 = g["k0"], g["k1"]
            delta = len(tmp.moves) - (k1 - k0)
            op.moves[k0:k1] = tmp.moves
            for h in rngs:
                if h["k0"] >= k1:
                    h["k0"] += delta; h["k1"] += delta
            g["k1"] = k0 + len(tmp.moves); g["dom"] = nd
            g.setdefault("trimmed", round(dom.area - nd.area, 1))
            changed = True
        if changed:
            rngs[:] = [h for h in rngs if h["k1"] > h["k0"]]
            _finish_params(op, op.params.get("finish_wall") or 0.0, op.params.get("finish_floor") or 0.0,
                           op.params.get("finish_pattern"), op.params.get("finish_angle") or 0.0, op.params.get("finish_stepover") or 0.0)
            op.params["finish_trimmed_mm2"] = round(sum(h.get("trimmed", 0) for h in rngs if "floor" in h) or 0.0, 1)


POCKET_STRATEGIES = ("auto", "rings", "bore", "adaptive")
BORE_RANGE = (1.3, 3.0)                 # hole Ø as a multiple of the tool Ø that a helical bore takes


def _disc(R) -> tuple[float, float, float] | None:
    """(x, y, Ø) when the region is a plain disc (no islands), else None."""
    import shapely
    if not isinstance(R, Polygon) or R.interiors or R.is_empty:
        return None
    rb = float(shapely.minimum_bounding_radius(R))
    if rb <= 0 or R.area < 0.97 * math.pi * rb * rb:
        return None
    c = shapely.minimum_bounding_circle(R).centroid
    return c.x, c.y, 2 * rb


def _helix_bore(pb: PathBuilder, cx: float, cy: float, d: float, z_top: float, z_bottom: float, tool: Tool, so: float,
                ramp: float, climb: bool, stepdown: float):
    """A round hole by helices: the first (the only one up to 2x the tool Ø) with the tool over the centre, so no core
    pin is left; wider holes add helices out to the wall at most `so` apart. Each helix descends at the ramp angle
    (never more than the stepdown per turn, never faster down than the plunge feed) and ends with a full circle at the
    bottom so the floor is flat; the first runs at the ramp feed (it cuts full width), the others at the feed."""
    rt = tool.radius
    rh = d / 2 - rt
    r_in = min(rh, 0.9 * rt)
    n = math.ceil((rh - r_in) / so - 1e-9) if rh > r_in + 1e-6 else 0
    radii = [r_in + (rh - r_in) * k / n for k in range(n + 1)] if n else [rh]
    sgn = -1.0 if climb else 1.0                            # clockwise keeps the wall on the left: climb (see contour)
    depth = z_top - z_bottom
    z_air = z_top + ENTRY_GAP
    for k, r in enumerate(radii):
        C = 2 * math.pi * r
        ang = min(ramp if ramp > 0 else RAMP_DEG, math.degrees(math.atan2(max(stepdown, 0.05), C)))
        turns = max(1, math.ceil(depth / (C * math.tan(math.radians(ang))) - 1e-9))
        npts = 24 * turns
        sa = math.sin(math.radians(ang))
        f = pb.ramp_feed(ang) if k == 0 else min(pb.feed, max(pb.plunge / sa, pb.plunge))
        if k == 0:
            pb.rapid_xy(cx + r, cy); pb.plunge_to(z_air, rapid_to=z_top, gap=0.5)
        else:                                               # up inside the bore, over to the next radius just above the top
            pb._add(RAPID, pb.pos[0], pb.pos[1], z_top + 0.5)
            pb._add(RAPID, cx + r, cy, z_top + 0.5)
            pb._add(PLUNGE, cx + r, cy, z_air, pb.plunge)
        for i in range(1, npts + 1):
            a = sgn * 2 * math.pi * i / 24
            pb.feed_to(cx + r * math.cos(a), cy + r * math.sin(a), z_air - (z_air - z_bottom) * i / npts, f)
        for i in range(1, 25):                              # the floor: one flat circle at the bottom
            a = sgn * 2 * math.pi * i / 24
            pb.feed_to(cx + r * math.cos(a), cy + r * math.sin(a), z_bottom, pb.feed if k else f)
    pb.feed_to(cx, cy, z_bottom)
    pb.retract()


def bore(setup: Setup, tool: Tool, x: float, y: float, diameter: float, z_top: float, z_bottom: float,
         ramp: float = RAMP_DEG, stepover: float | None = None, climb: bool = True, name: str = "Bore") -> Op:
    """Helical bore of a round hole Ø`diameter` at (x, y) from z_top to z_bottom: helices at the ramp angle (descent
    never faster than the plunge feed, at most the stepdown per turn) and a full circle at the bottom; holes from
    1.3x to 3x the tool Ø (outside that, or deeper than the flutes: pocketed with rings, with a warning). Far faster
    than pocketing a hole level by level (the latch's four Ø8 register holes: 17.8 min as pockets)."""
    return pocket(setup, tool, circle(x, y, diameter), z_top=z_top, z_bottom=z_bottom, stepover=stepover, climb=climb,
                  ramp=ramp, name=name, strategy="bore")


# ---------------------------------------------------------------- register holes (multi-setup alignment)
def _flip_of(setup: Setup, later: Setup) -> str | None:
    """How `later` sees `setup`'s XY: "x" (turned over about X: Y mirrored), "y" (about Y: X mirrored) or None (same
    side up; an in-plane turn of a few degrees, e.g. a measured B_ROT, counts as none)."""
    if setup.rotary or later.rotary:
        return None
    Rr = later._R() @ setup._R().T
    if Rr[2, 2] > 0:
        return None
    return "x" if abs(Rr[0, 0]) >= abs(Rr[1, 1]) else "y"


def register_holes(setup: Setup, parts, dia: float = 8.0, inset: float | None = None, count: int = 4, flip: str | None = None,
                   tool: Tool | None = None, margin: float = 1.0, later: Setup | None = None) -> list[tuple[float, float]]:
    """Where to put register holes for a multi-setup job (drilled through in this setup, probed after the flip to align
    the next one): positions (setup frame) in the stock's waste near the corners, the hole edge at least `inset` from
    the stock edges (default 3 mm) and at least the profiling tool's Ø (`tool`, default 1/8") + `margin` from every part
    outline (`parts`: polygons in this setup's frame, or shapes - viewed in this setup). The set maps onto itself under
    the flip, so the same holes are where the program expects them on the other side: flip="x" (turned over front to
    back, about X: Y mirrored), "y" (left to right, about Y: X mirrored), None = derived from `later` (a Setup) or no
    constraint. count 4: the four corners; 3: a pair plus one on the mirror line (or a third corner) as a check;
    2: a pair (a diagonal when nothing is mirrored). CamError when the waste can't fit them."""
    from shapely import affinity
    from shapely.ops import nearest_points
    if count not in (2, 3, 4):
        raise CamError("register_holes: count is 2, 3 or 4")
    if setup.rotary:
        raise CamError("register_holes: a flat setup only")
    if flip is None and later is not None:
        flip = _flip_of(setup, later)
    if flip not in (None, "x", "y"):
        raise CamError("register_holes: flip is 'x' (about X, Y mirrored), 'y' (about Y, X mirrored) or None")
    polys = []
    for p in (parts if isinstance(parts, (list, tuple)) else [parts]):
        if p is None:
            continue
        polys += _polys(p) if hasattr(p, "geom_type") else _polys(outline(setup.view(p)))
    body = unary_union(polys) if polys else Polygon()
    st = setup.stock
    cx, cy = (st.xmin + st.xmax) / 2, (st.ymin + st.ymax) / 2
    edge = 3.0 if inset is None else float(inset)
    wt = tool.diameter if tool is not None else 3.175
    ok = st.rect().buffer(-(dia / 2 + edge), join_style=2)
    if not body.is_empty:
        ok = ok.difference(body.buffer(dia / 2 + wt + margin + 0.01, join_style=1, quad_segs=32))
    mir = {"x": lambda g: affinity.scale(g, 1.0, -1.0, origin=(cx, cy)), "y": lambda g: affinity.scale(g, -1.0, 1.0, origin=(cx, cy))}
    mp = {"x": lambda q: (q[0], 2 * cy - q[1]), "y": lambda q: (2 * cx - q[0], q[1]), None: lambda q: q}[flip]
    if flip:
        ok = ok.intersection(mir[flip](ok))
    if ok.is_empty:
        raise CamError(f"register_holes: no waste for Ø{dia:g} holes {edge:g} mm in from the stock edges and "
                       f"{wt + margin:g} mm clear of the parts{' that mirror onto each other' if flip else ''}: a bigger stock or smaller holes")
    hx, hy = (st.xmax - st.xmin) / 2 - dia / 2 - edge, (st.ymax - st.ymin) / 2 - dia / 2 - edge
    corner = lambda sx, sy: (cx + sx * hx, cy + sy * hy)                                   # noqa: E731

    def near(q, within=None):
        g = ok if within is None else ok.intersection(within)
        if g.is_empty:
            return None
        a = nearest_points(g, Point(q))[0]
        return (a.x, a.y)
    big = max(hx, hy) * 4 + 10
    if flip == "x":                     # pairs (x, ±y) from two corners that don't mirror onto each other; fixed on y = cy
        pairs, fixed = [(-1, -1), (1, -1)], (1, 0)
    else:
        pairs, fixed = [(-1, -1), (-1, 1)], (0, 1)
    out: list[tuple[float, float]] = []
    if flip:
        cand = [q for q in (near(corner(*c)) for c in pairs) if q is not None]
        if count < 4:                   # the pair the furthest apart
            cand = sorted(cand, key=lambda q: -math.dist(q, mp(q)))[:1]
        for q in cand:
            out += [q, mp(q)]
        if count == 3:
            axis = LineString([(cx - big, cy), (cx + big, cy)]) if flip == "x" else LineString([(cx, cy - big), (cx, cy + big)])
            q = near(corner(*fixed), axis.buffer(1e-6))
            if q is not None:
                out.append((q[0], cy) if flip == "x" else (cx, q[1]))
    else:
        fixed = None
        for sxy in ([(-1, -1), (1, 1), (1, -1), (-1, 1)][:count]):
            q = near(corner(*sxy))
            if q is not None:
                out.append(q)
    uniq: list[tuple[float, float]] = []
    for q in out:
        if all(math.dist(q, u) >= dia + 2.0 for u in uniq):
            uniq.append(q)
    if len(uniq) < min(count, 2) or (len(uniq) >= 2 and max(math.dist(a, b) for a in uniq for b in uniq) < 0.25 * math.hypot(2 * hx, 2 * hy)):
        raise CamError(f"register_holes: the waste fits only {len(uniq)} Ø{dia:g} hole(s) far enough apart "
                       f"(clear of the parts by {wt + margin:g} mm, {edge:g} mm in from the edges): a bigger stock or smaller holes")
    return [(round(x, 3), round(y, 3)) for x, y in uniq]


def register_hole_ops(setup: Setup, tool: Tool, holes, dia: float = 8.0, z_bottom: float | None = None,
                      name: str = "Register holes") -> Op:
    """One op that helix-bores the register holes (register_holes()) through the stock into the spoilboard by 0.2 mm
    (z_bottom), tagged params["register"] = {"holes": [(x, y), ...], "dia": dia} so Program.alignment_features() can
    tell the next setup where to probe them."""
    holes = [(float(x), float(y)) for x, y in holes]
    if not holes:
        raise CamError("register_hole_ops: no holes")
    zb = setup.stock.bottom - 0.2 if z_bottom is None else float(z_bottom)
    op = pocket(setup, tool, unary_union([circle(x, y, dia) for x, y in holes]), z_top=setup.stock.top, z_bottom=zb,
                name=name, strategy="bore")
    op.params["register"] = {"holes": holes, "dia": float(dia)}
    return op


def _note_cleared(setup: Setup, region, tool: Tool, z_top: float, z_bottom: float) -> None:
    """Remember what a roughing op cleared (the region as the tool reaches it) for the ops after it in this script."""
    reach = region.buffer(-tool.radius, join_style=1).buffer(tool.radius - 1e-3, join_style=1)
    if not reach.is_empty:
        _OV.setdefault("rough", []).append((setup, reach, max(z_top, z_bottom), min(z_top, z_bottom)))


def _rings_ramped(pb: PathBuilder, R, shells_R, tool: Tool, levels, climb: bool, ramp: float, z_air: float):
    """Cut the concentric rings of one region at every level, lobe by lobe: where the offsets split the region (narrow
    necks), each lobe is cut from its innermost ring outwards before the next, and the ring that joins them last.
    Each ring starts where the tool is nearest. One entry per lobe per level: it ramps down along the lobe's
    innermost ring that is long enough (from the previous level's floor, or from just above the stock on the first
    level). Links stay down when they are a step out onto the next ring or run through ground cleared at this level;
    otherwise the tool lifts just above the previous level's floor (cleared over the whole region) and moves inside
    the region; only a link that would cross the wall retracts."""
    inner_ok = R.buffer(-tool.radius + 1e-6)       # where the tool centre may travel at depth inside this region
    so = max((pb.op.params or {}).get("stepover") or tool.diameter * tool.stepover, 1e-3)
    floor = z_air                                   # z the region is cleared to so far
    # the offset tree: a shell's parent is the shell one offset further out that contains it
    nodes = []                                      # [poly, depth, parent index, children]
    prev_ids: list[int] = []
    for k, ps in enumerate(shells_R):
        ids = []
        for q in ps:
            par = next((j for j in prev_ids if nodes[j][0].covers(q.representative_point())), None)
            nodes.append([orient(q, 1.0), k, par, []])
            if par is not None:
                nodes[par][3].append(len(nodes) - 1)
            ids.append(len(nodes) - 1)
        prev_ids = ids
    def rings_of(i):
        q = nodes[i][0]
        rr = [_ring_coords(q.exterior, False)] + [_ring_coords(h, True) for h in q.interiors]
        return [r[::-1] for r in rr] if not climb else rr
    ring_len = lambda r: sum(math.dist(r[k], r[(k + 1) % len(r)]) for k in range(len(r)))
    min_len = max(tool.diameter * 1.5, 2.0)        # shorter: the ramp would be a wobbling plunge
    first = True
    from shapely.prepared import prep
    for z in levels:
        cleared = None                              # what this level has cut so far
        # stock the ops before this one in the script already cleared through this level's slab: rings there are air
        pre = _cleared_slab(pb.setup, z, floor if floor < z_air - 1e-9 else z_air - ENTRY_GAP)
        pre_p = prep(pre) if pre is not None else None
        def cut_ring(ring, entry):
            nonlocal first, cleared
            sweep = LineString(ring + [ring[0]]).buffer(tool.radius, quad_segs=8)
            if pre_p is not None and pre_p.covers(sweep):
                cleared = sweep if cleared is None else cleared.union(sweep)
                return False
            pos = pb.pos[:2] if pb.pos is not None else ring[0]
            ring = _ring_from(ring, pos)
            start = ring[0]
            link = LineString([pos, start]) if pb.pos is not None and math.dist(pos, start) > 1e-9 else None
            if first or pb.pos is None:
                pb.rapid_xy(*start); pb.plunge_to(floor, rapid_to=floor, gap=0.5); first = False
            elif link is None:
                pass
            elif not entry and inner_ok.covers(link) and (link.length if cleared is None else link.difference(
                    cleared.buffer(-tool.radius + 0.05)).length) <= 1.25 * so + 0.05:
                pb.feed_to(start[0], start[1], pb.pos[2])          # through cleared ground, then a step out onto the ring
            elif entry and abs(pb.pos[2] - floor) < 1e-6 and link.length <= 4 * tool.diameter and inner_ok.covers(link):
                pb.feed_to(start[0], start[1], floor)               # a new level: along the floor just cut to the entry
            elif (route := _route_inside(pos, start, roots_poly, inner_ok)) is not None:   # lift over the previous
                zl = floor + 0.3                                    # level's floor (cleared all over), stay in the region
                if pb.pos[2] < zl:
                    pb._add(RAPID, pos[0], pos[1], zl)
                for x_, y_ in route:
                    pb._add(RAPID, x_, y_, zl)
                pb._add(PLUNGE, start[0], start[1], floor, pb.plunge)
            else:
                pb.rapid_xy(*start); pb.plunge_to(floor, rapid_to=floor, gap=0.5)
            if entry and pre_p is not None and pb.pos[2] > z + 1e-9 and pre_p.covers(Point(start).buffer(tool.radius * 1.02)):
                pb._add(PLUNGE, start[0], start[1], z, pb.plunge)   # the entry is over cleared ground: straight down in the air
            _lap_ramp(pb, ring, z, ramp, 0.0)
            cleared = sweep if cleared is None else cleared.union(sweep)
            return True
        roots = [i for i, nd in enumerate(nodes) if nd[2] is None]
        # slivers at the wall (where the tool just fits a notch of the boundary) are not lobes worth an entry each
        notch = [i for i in roots if not nodes[i][3] and nodes[i][0].area <= 0.05 * so * so]
        if len(notch) < len(roots):
            roots = [i for i in roots if i not in notch]
        else:
            notch = []
        roots_poly = [nodes[i][0] for i in roots]
        def chains(i):
            """The cutting order of node i's subtree as chains: each chain starts with a new entry."""
            ch = nodes[i][3]
            if not ch:
                return [[i]]
            sub = []
            pos = pb.pos[:2] if pb.pos is not None else None
            for c in _nn_order(ch, lambda j: (nodes[j][0].centroid.x, nodes[j][0].centroid.y), pos):
                sub += chains(c)
            sub[-1].append(i)                       # the parent ring follows the last child's lobe without a new entry
            return sub
        order = []
        for rt in _nn_order(roots, lambda j: (nodes[j][0].centroid.x, nodes[j][0].centroid.y), pb.pos[:2] if pb.pos else None):
            order += chains(rt)

        for chain in order:
            rings = [r for i in chain for r in rings_of(i)]
            # the entry ramps along the innermost ring that is long enough; shorter rings inside it follow at depth
            ei = next((k for k, r in enumerate(rings) if ring_len(r) >= min_len), max(range(len(rings)), key=lambda k: ring_len(rings[k])))
            rings = [rings[ei]] + rings[:ei] + rings[ei + 1:]
            entry = True
            for ring in rings:
                if cut_ring(ring, entry):
                    entry = False
        floor = z


def _cleared_slab(setup: Setup, z_lo: float, z_hi: float):
    """Where the roughing ops built so far in this script (same setup) cleared the whole slab z_lo..z_hi, or None."""
    ents = [(c, zt, zb) for st, c, zt, zb in _OV.get("rough", []) if st is setup and zt > z_lo + 1e-6 and zb < z_hi - 1e-6]
    if not ents or z_hi <= z_lo + 1e-9:
        return None
    cuts = sorted({z_lo, z_hi} | {v for _, zt, zb in ents for v in (zt, zb) if z_lo < v < z_hi})
    area = None
    for lo, hi in zip(cuts, cuts[1:]):
        u = [c for c, zt, zb in ents if zb <= lo + 1e-6 and zt >= hi - 1e-6]
        if not u:
            return None
        g = unary_union(u)
        area = g if area is None else area.intersection(g)
        if area.is_empty:
            return None
    return area


def _route_inside(a, b, polys, ok):
    """A path from a to b (the points after a) inside `ok`: straight, or out to the outer ring of the offset polygon
    holding both, along it the shorter way round, and in again. None if there is none."""
    if ok.covers(LineString([a, b])):
        return [b]
    pa, pb_ = Point(a), Point(b)
    for q in polys:
        if not (q.buffer(1e-6).covers(pa) and q.buffer(1e-6).covers(pb_)):
            continue
        from shapely.ops import substring
        ext = LineString(q.exterior.coords)
        L = ext.length
        s1, s2 = ext.project(pa), ext.project(pb_)
        def arc(u, v):                                      # forward along the ring from u to v
            if v >= u:
                return list(substring(ext, u, v).coords)
            return list(substring(ext, u, L).coords) + list(substring(ext, 0.0, v).coords)
        pts = arc(s1, s2) if (s2 - s1) % L <= L / 2 else arc(s2, s1)[::-1]
        pts = pts + [b]
        if ok.covers(LineString([a] + pts)):
            return pts
    return None


def _ring_from(ring: list[tuple[float, float]], pos) -> list[tuple[float, float]]:
    """The closed ring re-started at its point nearest `pos` (a vertex is inserted there)."""
    ln = LineString(list(ring) + [ring[0]])
    s = ln.project(Point(pos))
    if s <= 1e-9 or s >= ln.length - 1e-9:
        return list(ring)
    acc, n = 0.0, len(ring)
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        d = math.dist(a, b)
        if acc + d >= s - 1e-9:
            t = (s - acc) / d if d > 1e-12 else 0.0
            p = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
            rest = list(ring[i + 1:]) + list(ring[:i + 1])
            if math.dist(p, rest[0]) < 1e-9:
                return rest
            return [p] + rest + ([] if math.dist(p, rest[-1]) < 1e-9 else [])
        acc += d
    return list(ring)


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
             max_seconds: float = 120.0, stay_down: float | None = None, lift: float = 0.5, hop: float = 0.25,
             name: str = "Adaptive") -> Op:
    """Constant-engagement (HSM / trochoidal) roughing, by construction:
    the cleared region K (everything outside the pocket counts as cleared, plus the helix entry) is kept
    as an exact polygon. Each pass runs the tool along the curve where the cutter protrudes from K by
    exactly the radial engagement ae = stepover × diameter (the inward offset of K by r − ae), clipped
    to where the tool centre may go; K then grows by the swept band and the next pass follows the new
    front. Near walls and in slots the fronts become successive crescents — the trochoidal motion —
    without the engagement ever exceeding ae except transiently at concave corners. Full axial depth
    by default (90% of flute length), helical entry, islands and rest material (rest_from) handled
    natively. Every pass is climb (decided along its whole length) and is trimmed to where the cutter actually
    touches material, and entered by a tangential quarter-arc lead-in from the cleared side so the engagement builds
    from nothing. Links run through cleared ground (straight, or back along the pass just cut, shortened): short ones
    (≤ stay_down, default 4 × Ø) at feed with the cutter down, longer ones at rapid `hop` mm above the floor; only when
    no cleared route exists does the cutter lift `lift` mm above this band's top and rapid across inside the pocket
    (round islands), and a full retract is the last resort. Rapids never run or come down into standing material.
    Each level ends with a climb pass along the walls wherever the crescents left scallops there, so nothing in the
    pocket stands above the level (stock_to_leave is kept); follow with a contour pass for a finished wall."""
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
    lim = setup.machine.adaptive_doc(tool, setup.model_stock.material or setup.stock.material)
    if stepdown and stepdown > lim + 1e-9:
        op.warnings.append(f"adaptive: stepdown {stepdown:g} is deeper than the {lim:g} mm this machine / tool allows for "
                           "constant-engagement cuts (machine adaptive_stepdown): using that")
    sd = min(stepdown, lim) if stepdown else max(min(lim, z_top - z_bottom), 0.1)
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
    op.params = dict(z_top=z_top, z_bottom=z_bottom, engagement=round(ae, 3), engagement_angle=round(phi, 1), stock_to_leave=stock_to_leave,
                     stepdown=round(sd, 3), passes=len(levels), feed=int(feed),
                     ap=round((z_top - z_bottom) / len(levels), 3))         # the planned depth of a level (the sim's BITE check)
    sign = 1.0 if climb else -1.0
    # engagement-aware feed: in corners the contact arc rises above the target even though the radial
    # protrusion stays ≤ ae; slow down there to keep the chip load per tooth constant
    NSA = 36
    ang_s = np.arange(NSA) * 2 * math.pi / NSA
    ring_x, ring_y = (r - 0.02) * np.cos(ang_s), (r - 0.02) * np.sin(ang_s)
    max_eng_seen = 0.0

    def eng_angle(mat_prep, x, y, hx, hy):
        lead = (ring_x * hx + ring_y * hy) > 0
        n = int(shapely.contains_xy(mat_prep.context, x + ring_x[lead], y + ring_y[lead]).sum())
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

    def oriented(pts, mat_prep):
        """Orient a pass so the material is on the left (climb) / right (conventional): a vote over the whole pass,
        probing the uncut material a little inside the cutter on each side (one probe at a wall can't flip it)."""
        if len(pts) < 2:
            return pts
        votes = 0
        n = len(pts) - 1
        for i in range(0, n, max(1, n // 24)):
            (ax_, ay_), (bx_, by_) = pts[i], pts[i + 1]
            tx, ty = bx_ - ax_, by_ - ay_
            ln = math.hypot(tx, ty)
            if ln < 1e-9:
                continue
            lx, ly = -ty / ln, tx / ln
            mx, my = (ax_ + bx_) / 2, (ay_ + by_) / 2
            for dd in (0.35 * r, 0.7 * r):
                votes += mat_prep.contains(Point(mx + lx * dd, my + ly * dd))
                votes -= mat_prep.contains(Point(mx - lx * dd, my - ly * dd))
        if (votes >= 0) != climb:
            pts = pts[::-1]
        return pts

    def engaged_runs(dense, mat_prep):
        """Split a densified pass into the runs where the cutter touches uncut material (plus one step either side
        to enter and leave in cleared ground); stretches that only cut air are dropped."""
        D = np.asarray(dense, dtype=float)                 # the centre and 18 rim probes per point, one call each
        hit = shapely.contains_xy(mat_prep.context, D[:, 0], D[:, 1])
        for k in range(0, NSA, 2):
            hit |= shapely.contains_xy(mat_prep.context, D[:, 0] + ring_x[k], D[:, 1] + ring_y[k])
        hit = hit.tolist()
        runs, i, n = [], 0, len(dense)
        while i < n:
            if not hit[i]:
                i += 1; continue
            j = i
            while j + 1 < n and hit[j + 1]:
                j += 1
            runs.append(dense[max(0, i - 1):min(n, j + 2)])
            i = j + 1
        merged = []
        for run in runs:                                  # rejoin runs separated by a hair of air
            if merged and math.dist(merged[-1][-1], run[0]) < 2 * step_len:
                merged[-1] = merged[-1] + run[1:]
            else:
                merged.append(run)
        return [r_ for r_ in merged if len(r_) >= 2 and sum(math.dist(a, b) for a, b in zip(r_, r_[1:])) > ae * 0.5]

    stay_max = float(stay_down) if stay_down is not None else 4.0 * tool.diameter
    lifts = hops = 0
    # links at lift height run inside the allowed centre region C (the cutter stays inside the pocket, where nothing
    # stands above this band's top): straight if C covers the line, else round islands on a visibility graph of C
    C_link = C.buffer(1e-3, join_style=1)                 # links: the cutter in cleared ground AND inside the pocket
    C_prep = prep(C.buffer(0.005, join_style=1))
    C_in = prep(C.buffer(-0.02, join_style=1))            # lifted rapids keep this clear of the walls          # pass ends lie on C's boundary: include it (5 µm)
    Cs = C.buffer(-0.05, join_style=1).simplify(0.03)      # detour corners just inside C
    nodes = [c for g in _polys(Cs) for ring in [g.exterior, *g.interiors] for c in list(ring.coords)[:-1]]
    nodes = [(x + 0.0, y + 0.0) for x, y in nodes]

    def trav_ok(p_, q_, trim_p, trim_q):
        """A lifted rapid from p_ to q_ inside the pocket, 0.02 mm clear of its walls (where uncut stock may stand
        to the top) except the last 0.15 mm at an end that is a pass end on the wall (the cutter goes straight up
        or down there)."""
        if not C_prep.covers(LineString([p_, q_])):
            return False
        L = math.dist(p_, q_)
        ta, tb = (0.15 if trim_p else 0.0), (0.15 if trim_q else 0.0)
        if L - ta - tb <= 1e-9:
            return True
        ux, uy = (q_[0] - p_[0]) / L, (q_[1] - p_[1]) / L
        return C_in.covers(LineString([(p_[0] + ux * ta, p_[1] + uy * ta), (q_[0] - ux * tb, q_[1] - uy * tb)]))

    def lifted_path(a, b):
        if trav_ok(a, b, True, True):
            return [b]
        import heapq
        pts = [a, b] + nodes
        if len(pts) > 600:
            return None
        vis = lambda i, j: trav_ok(pts[i], pts[j], i < 2, j < 2)
        dist = {0: 0.0}; prev_ = {}; pq = [(0.0, 0)]; seen = set()
        while pq:
            dcur, i = heapq.heappop(pq)
            if i in seen:
                continue
            seen.add(i)
            if i == 1:
                break
            for j in range(1, len(pts)):
                if j in seen:
                    continue
                nd = dcur + math.dist(pts[i], pts[j])
                if nd < dist.get(j, 1e18) and vis(i, j):
                    dist[j] = nd; prev_[j] = i; heapq.heappush(pq, (nd, j))
        if 1 not in prev_:
            return None
        path, k = [], 1
        while k != 0:
            path.append(pts[k]); k = prev_[k]
        return path[::-1]

    def route_in(a, b, Kc, back):
        """A route from a to b with the cutter fully in cleared ground: straight, or back along `back` (the pass just
        cut, which is cleared) shortened by string-pulling. Returns the points after a, or None."""
        if Kc.covers(LineString([a, b])):
            return [b]
        if not back:
            return None
        pts = [a] + list(back[::-1]) + [b]
        out, i, n = [], 0, len(pts)
        while i < n - 1:
            best = None
            for k in range(n - 1, i, -1):                 # farthest point still in sight through cleared ground
                if Kc.covers(LineString([pts[i], pts[k]])):
                    best = k; break
            if best is None:
                return None
            out.append(pts[best]); i = best
        return out

    def lead_in(dense, Kc):
        """How the cutter enters a pass: nothing extra if it starts in cleared ground (passes are trimmed to start a
        step before they cut); else a quarter-arc lead-in from the cleared side, tangent to the pass at its start, so
        the engagement builds up as the arc curves onto the pass; else a straight run-in along the pass. Returns the
        lead points (the first one is where the link goes), or None. The arc starts in cleared ground and stays in
        the pocket; it may run into material, which is the point: the cut builds from nothing to the stepover."""
        if Kc.covers(Point(dense[0])):
            return None
        (x0_, y0_), (x1_, y1_) = dense[0], dense[min(1, len(dense) - 1)]
        tx, ty = x1_ - x0_, y1_ - y0_
        ln = math.hypot(tx, ty)
        if ln < 1e-9:
            return None
        tx, ty = tx / ln, ty / ln
        side = -1.0 if climb else 1.0
        nx_, ny_ = side * (-ty), side * tx                    # away from the material (the cleared side)
        for rad in (ae + 0.1, 0.5 * (ae + 0.1), r):
            cx_, cy_ = x0_ + nx_ * rad, y0_ + ny_ * rad
            arc = []
            for k in range(8, 0, -1):                         # φ 90° → 0°: from behind-and-beside onto the pass
                ph = math.radians(90.0 * k / 8)
                arc.append((cx_ - rad * (math.cos(ph) * nx_ + math.sin(ph) * tx), cy_ - rad * (math.cos(ph) * ny_ + math.sin(ph) * ty)))
            if Kc.covers(Point(arc[0])) and all(C_prep.covers(Point(q)) for q in arc):
                return arc
        for back_ in (0.5 * r, r):
            q = (x0_ - tx * back_, y0_ - ty * back_)
            if Kc.covers(Point(q)):
                return [q]
        return None

    def link_to(pos, target, z, z_lift, Kc, Kr, back=None, z_open=None):
        """Move from pos to target at level z: through cleared ground (feed if short, else a low rapid hop), else
        lift-and-rapid inside the pocket, else retract. Kc is where the cutter centre can feed through cleared ground
        (its edge may graze the walls by 0.02 mm), Kr the stricter version for rapids (0.005 mm at most, the polygon model's own error):
        rapids only run or come down near z over Kr; a target off it is fed down to from above the stock. Returns
        the kind used."""
        d = math.dist(pos, target)
        if d < 1e-6:
            return "none"
        clear = Kr.covers(Point(target))
        route = route_in(pos, target, Kc, back)
        L = sum(math.dist(p_, q_) for p_, q_ in zip([pos] + route, route)) if route is not None else math.inf
        if L <= stay_max:
            for wx, wy in route:
                pb.feed_to(wx, wy, z, feed)
            return "stay"
        if route is not None and clear and Kr.covers(LineString([pos] + route).difference(Point(pos).buffer(0.1))):
            pb._add(RAPID, pos[0], pos[1], z + hop)
            for wx, wy in route:
                pb._add(RAPID, wx, wy, z + hop)
            pb._add(PLUNGE, route[-1][0], route[-1][1], z, pb.plunge)
            return "hop"
        path = lifted_path(pos, target)
        if path is not None:
            # a pass start sits on the wall: the lifted rapid stops where its footprint is still 0.02 mm clear of the
            # wall (uncut stock - or the skin a previous band left - stands there up to the stock top), comes down,
            # and the last fraction of a millimetre is a feed move at depth
            q = path[-1]; p_ = path[-2] if len(path) > 1 else pos
            stop = q
            if not C_in.covers(Point(q)):
                L_ = math.dist(p_, q)
                for t in np.arange(0.02, min(0.6, L_), 0.02):
                    cand = (q[0] + (p_[0] - q[0]) * t / L_, q[1] + (p_[1] - q[1]) * t / L_)
                    if C_in.covers(Point(cand)):
                        stop = cand; break
            pb._add(RAPID, pos[0], pos[1], z_lift)
            for wx, wy in path[:-1]:
                pb._add(RAPID, wx, wy, z_lift)
            pb._add(RAPID, stop[0], stop[1], z_lift)
            clear_stop = clear if stop is q else Kr.covers(Point(stop))
            if clear_stop and z_lift - z > 0.35:
                pb._add(RAPID, stop[0], stop[1], z + 0.3)
            elif z_open is not None and z_open + 0.05 < z_lift - 1e-6 and z_open + 0.05 > z + 1e-6:
                pb._add(RAPID, stop[0], stop[1], z_open + 0.05)      # the previous level cleared everything above z_open
            pb._add(PLUNGE, stop[0], stop[1], z, pb.plunge)
            if stop is not q:
                pb.feed_to(q[0], q[1], z, feed)
            return "lift"
        pb.rapid_xy(target[0], target[1]); pb.plunge_to(z)
        return "retract"

    z_prev = None
    for z in levels:
        if _time.time() - t_start > max_seconds:
            op.warnings.append(f"adaptive: time budget of {max_seconds:.0f}s exhausted; remaining levels skipped")
            break
        z_lift = min(max(z_top, z) + lift, setup.safe_z)
        # ---- helical entry
        z_h = (z_top if z_prev is None else z_prev) + ENTRY_GAP      # the bore above is already cut on later levels
        depth = z_h - z
        n_turns = max(1, math.ceil(depth / min(1.0, tool.diameter * 0.15)))
        n_pts = 24 * n_turns
        f_h = pb.ramp_feed(math.degrees(math.atan2(depth / n_turns, 2 * math.pi * hr)))
        pb.rapid_xy(ex + hr, ey)
        pb.plunge_to(z_h, rapid_to=z_h, gap=0.45)
        for i in range(1, n_pts + 1):
            a = sign * 2 * math.pi * i / 24
            zz = z_h - depth * i / n_pts
            pb.feed_to(ex + hr * math.cos(a), ey + hr * math.sin(a), zz, f_h)
        K = K0.union(Point(ex, ey).buffer(hr + r, 48))
        pos = (pb.pos[0], pb.pos[1])
        Kc_prev = None
        wall_done = False
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
                # fronts exhausted: one climb pass along the walls takes the scallops the crescents leave there
                # (trimmed to where material is left), so nothing in the pocket stands above this level
                if wall_done:
                    break
                wall_done = True
                segs = [LineString(rg.coords) for g_ in _polys(C) for rg in [g_.exterior, *g_.interiors]]
            Kc_poly = K.buffer(-r + 1e-3, join_style=1)
            Kc = prep(Kc_poly)                                   # tool fully inside cleared ground
            Kc_prev = Kc_poly.buffer(-1e-3)
            mat_prep = prep(P.difference(K))                     # uncut material at this level
            remaining = []
            for g in segs:                                       # climb passes, trimmed to where they cut
                remaining += engaged_runs(_densify(oriented(list(g.coords), mat_prep), step_len), mat_prep)
            if not remaining:
                break
            Klive = K                                         # cleared ground including the passes cut so far
            last_pass = None
            fresh = True
            while remaining:
                # stay in the area: the nearest pass start if it is within a stay-down link; otherwise, before leaving,
                # recompute the fronts (the passes just cut have opened new ones here) and take the pass nearest the
                # cutter anywhere along it, so an area is finished before the cutter moves on
                d_start = [math.dist(q[0], pos) for q in remaining]
                k = min(range(len(remaining)), key=d_start.__getitem__)
                if d_start[k] > stay_max:
                    if not fresh:
                        break
                    pt_ = Point(pos)
                    k = min(range(len(remaining)), key=lambda i: LineString(remaining[i]).distance(pt_))
                fresh = False
                dense = remaining.pop(k)
                Kc_live = prep(Klive.buffer(-r + 0.02, join_style=1).intersection(C_link))
                Kr_live = prep(Klive.buffer(-r + 0.005, join_style=1).intersection(C_link))
                lead = lead_in(dense, Kc_live)
                kind_ = link_to(pos, lead[0] if lead else dense[0], z, z_lift, Kc_live, Kr_live, last_pass,
                                z_open=(z_prev if z_prev is not None else z_top))
                if kind_ == "stay":
                    links += 1
                elif kind_ == "hop":
                    hops += 1
                elif kind_ == "lift":
                    lifts += 1
                elif kind_ == "retract":
                    retracts += 1
                if lead:
                    for lx_, ly_ in lead[1:]:                      # along the lead-in onto the pass
                        pb.feed_to(lx_, ly_, z, feed)
                px_, py_ = dense[0]
                for x, y in dense[1:]:
                    hx, hy = x - px_, y - py_
                    ln = math.hypot(hx, hy) or 1.0
                    e = eng_angle(mat_prep, x, y, hx / ln, hy / ln)
                    max_eng_seen = max(max_eng_seen, e)
                    f = feed if e <= phi * 1.15 else max(feed * 0.3, feed * phi / e)
                    if f < feed:
                        f = max(feed * 0.3, math.floor(f / (feed * 0.14)) * feed * 0.14)   # 5 steps between 30 % and 100 %
                    pb.feed_to(x, y, z, f)
                    px_, py_ = x, y
                pos = dense[-1]
                Klive = Klive.union(LineString(dense).buffer(r, 16))
                last_pass = dense
                passes_total += 1
            K = Klive.simplify(0.01)
        pb.retract()
        z_prev = z
        reachable = P.buffer(-r, join_style=1).buffer(r + 1e-6, join_style=1)     # what this tool can reach at all
        left = P.difference(K).intersection(reachable).area
        if material0 > 0 and left > 0.03 * material0:
            op.warnings.append(f"adaptive: ~{100 * left / material0:.0f}% of the reachable material was not cleared at z={z:.2f}")
    pb.retract()
    op.params.update(links=links, hops=hops, lifts=lifts, retracts=retracts, fronts=passes_total, max_engagement_angle=round(max_eng_seen, 1))
    _note_cleared(setup, P, tool, z_top, z_bottom)
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
    z_air = (levels[0] + (levels[0] - levels[1] if len(levels) > 1 else 1.0) if levels else pb.setup.stock.top) + ENTRY_GAP   # just above the first level's top
    _rings_ramped(pb, P, shells, tool, levels, climb, RAMP_DEG, z_air)
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


# =============================================================================
# Probing: the 3D probe sets or checks the work origin from inside the program (Makera firmware)
# =============================================================================
# A probe op runs with the machine's 3D probe in the spindle (a tool change to it, never the spindle) and posts as a
# block of firmware probe moves between "; PROBE BEGIN" / "; PROBE END" comments, the only place the Run panel lets
# G38 / G10 / M480 through. What the firmware gives (MakeraZ1Firmware, read 2026-11-02):
#   G38.2 <axis><distance> F: a probe move RELATIVE to where the head is; it halts the machine (PROBE_FAIL) when the
#     distance runs out with nothing touched, or when the probe is already triggered as it starts. G38.3: the same
#     without the "nothing touched" error. While the probe is fitted any X/Y move, or Z move down, that triggers it
#     halts the machine too (zprobe halt_on_probe_during_motion, on by default): a probe that meets material where the
#     program expects air stops the job.
#   G10 L20 P0 X Y Z: the current point becomes these coordinates in the active work offset (no arithmetic needed).
#   M480.9 X<d> Y<d> Z<depth> D<ball Ø>: Makera's pocket-centre routine (MakeraStudio sends it from the Probe dialog):
#     from inside a hole below its edge it touches both X walls, goes to the middle and sets X0 there, then the same
#     in Y. It pauses the file like a tool change (inner playing), so it runs from a program. The post follows it with
#     G10 L20 to give that centre its coordinates in the setup.
# The controller has no variables or arithmetic, so a program can set the origin from one feature and CHECK others (a
# check halts the machine when a face is not within `check` mm of where it should be) but cannot average two holes or
# turn the program to a measured angle (no G68): a plate that went down rotated is caught by a check, not corrected.
PROBE_KINDS = ("surface", "hole", "edge")


def probe_tool(machine: Machine) -> Tool:
    """The machine's 3D probe as a tool (Machine.probe / PROBE_DEFAULTS): a ball stylus; the spindle never runs it."""
    p = machine.probe_spec()
    ball = float(p["ball"])
    return Tool(int(p["tool"]), "3D probe", "ball", ball, 1, ball, rpm=0, feed=float(p["feed"]), plunge=float(p["feed"]),
                stepdown=0.0, stepover=0.0, neck_diameter=round(ball * 0.6, 3), reach=float(p["stylus"]),
                notes="probe ops: a tool change to the probe; the spindle never starts")


def _probe_setup(setup: Setup, what: str) -> tuple[Tool, dict]:
    stop_point()
    m = setup.machine
    if m.post != "makera":
        raise CamError(f"{what}: probe ops are posted for the Makera firmware (Z1 / Carvera) only; {m.name} uses the {m.post} post")
    if setup.rotary:
        raise CamError(f"{what}: probe ops on a 4th-axis setup are not supported (use Machine ▸ Zero on the 4th axis)")
    return probe_tool(m), m.probe_spec()


def _probe_check_tol(check) -> float | None:
    if check is None or check is False:
        return None
    tol = float(check)
    if not (0.02 <= tol <= 2.0):
        raise CamError("check: the tolerance is 0.02 .. 2 mm (how far the face may be from where it should be)")
    return tol


def _probe_op(setup: Setup, tool: Tool, name: str, mode: str) -> tuple[Op, PathBuilder]:
    op = Op(name, "probe", tool, color=COLORS["probe_set" if mode == "set" else "probe_check"])
    op.tiled = False
    op.key = name
    pb = PathBuilder(setup, tool, op)
    return op, pb


def _probe_down(pb: PathBuilder, x: float, y: float, z: float, f: float):
    """Over (x, y) at safe Z, then down to z at the touch feed (the firmware halts if the probe meets anything)."""
    pb.rapid_xy(x, y)
    pb._add(FEED, x, y, z, f)


def _probe_sim(op: Op, touches: list[int], back: float = 0.6):
    """The moves the simulation checks for air: each touch stopped `back` mm short of the face it should find (a probe
    meeting material anywhere else is the job halting, or the wrong feature)."""
    sim = [list(m) for m in op.moves]
    for k in touches:
        prev = sim[k - 1]
        d = [sim[k][i] - prev[i] for i in (1, 2, 3)]
        n = math.sqrt(sum(v * v for v in d))
        if n > 1e-9:
            sim[k][1:4] = [sim[k][1 + i] - d[i] / n * min(back, n) for i in range(3)]
    op.sim_moves = sim


def probe_surface(setup: Setup, x: float, y: float, z: float | None = None, check: float | None = None,
                  search: float = 3.0, name: str = "Probe surface") -> Op:
    """3D probe: touch a flat face from above at (x, y) and set Z (the face is at `z`, default the stock top), or
    with check=tol only verify it is within tol mm (the machine halts if not). Search: how far from z it may be."""
    tool, ps = _probe_setup(setup, name)
    tol = _probe_check_tol(check)
    z = setup.stock.top if z is None else float(z)
    if not (0.5 <= search <= 10):
        raise CamError(f"{name}: search is 0.5 .. 10 mm")
    if tol is not None and tol >= search:
        raise CamError(f"{name}: the check tolerance must be smaller than the search distance")
    f = float(ps["feed"])
    op, pb = _probe_op(setup, tool, name, "check" if tol else "set")
    start = z + search
    _probe_down(pb, x, y, start, f * 3)
    pb._add(FEED, x, y, z, f)
    pb.retract()
    _probe_sim(op, [len(op.moves) - 2])
    op.params.update(probe="surface", mode="check" if tol else "set", at=[x, y], z=z, search=search, sets=[] if tol else ["z"],
                     check=tol, start=[x, y, start], feed=f)
    return op


def probe_hole(setup: Setup, x: float, y: float, diameter: float, z_top: float | None = None, depth: float | None = None,
               check: float | None = None, search: float = 2.0, name: str = "Probe hole") -> Op:
    """3D probe: find the centre of a round hole (nominal centre x, y, Ø diameter, top z_top) from inside it, `depth`
    below its top, and set X/Y there; or with check=tol touch its four walls and halt the machine if any is more than
    tol from where it should be. Search: how far off the hole may be (the origin you zeroed by hand)."""
    tool, ps = _probe_setup(setup, name)
    tol = _probe_check_tol(check)
    z_top = setup.stock.top if z_top is None else float(z_top)
    rb = tool.diameter / 2
    gap = diameter / 2 - rb                       # ball centre to the wall, from the middle of the hole
    if gap < 0.5:
        raise CamError(f"{name}: a Ø{diameter:g} hole is too small for the Ø{tool.diameter:g} probe ball (needs 1 mm play)")
    if depth is None:
        depth = max(2.0, tool.diameter + 1.0)
    if not (0.5 <= depth <= 10):
        raise CamError(f"{name}: depth is 0.5 .. 10 mm below the top of the hole")
    if not (0.5 <= search <= 10):
        raise CamError(f"{name}: search is 0.5 .. 10 mm")
    if tol is not None and tol >= gap:
        raise CamError(f"{name}: the check tolerance must be smaller than the ball's play in the hole ({gap:.2f} mm)")
    f = float(ps["feed"])
    op, pb = _probe_op(setup, tool, name, "check" if tol else "set")
    zp = z_top - depth
    _probe_down(pb, x, y, zp, f * 3)
    touches = []
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):         # the routine's order: both X walls, then both Y walls
        pb._add(FEED, x + dx * gap, y + dy * gap, zp, f)
        touches.append(len(op.moves) - 1)
        pb._add(FEED, x, y, zp, f * 3)
    pb.retract()
    _probe_sim(op, touches)
    op.params.update(probe="hole", mode="check" if tol else "set", at=[x, y], diameter=diameter, z_top=z_top, depth=depth,
                     z=zp, gap=round(gap, 4), search=search, sets=[] if tol else ["x", "y"], check=tol, feed=f)
    return op


def probe_edge(setup: Setup, toward: str, wall: float, at: float, z: float, check: float | None = None,
               search: float = 3.0, name: str = "Probe edge") -> Op:
    """3D probe: touch a vertical face square to X or Y and set that axis. toward '+x' moves in +X onto the face at
    X = wall (the part's -X side); `at` is the Y (for x) or X (for y) where it touches, `z` the height. check=tol:
    only verify the face is within tol (the machine halts if not)."""
    tool, ps = _probe_setup(setup, name)
    tol = _probe_check_tol(check)
    if toward not in ("+x", "-x", "+y", "-y"):
        raise CamError(f"{name}: toward is '+x', '-x', '+y' or '-y' (the direction the probe moves onto the face)")
    if not (0.5 <= search <= 10):
        raise CamError(f"{name}: search is 0.5 .. 10 mm")
    if tol is not None and tol >= search:
        raise CamError(f"{name}: the check tolerance must be smaller than the search distance")
    s = 1.0 if toward[0] == "+" else -1.0
    ax = toward[1]
    rb = tool.diameter / 2
    start = wall - s * (rb + search)
    f = float(ps["feed"])
    op, pb = _probe_op(setup, tool, name, "check" if tol else "set")
    p0 = (start, at) if ax == "x" else (at, start)
    p1 = (wall - s * rb, at) if ax == "x" else (at, wall - s * rb)
    _probe_down(pb, p0[0], p0[1], z, f * 3)
    pb._add(FEED, p1[0], p1[1], z, f)
    pb._add(FEED, p0[0], p0[1], z, f * 3)
    pb.retract()
    _probe_sim(op, [len(op.moves) - 3])
    op.params.update(probe="edge", mode="check" if tol else "set", toward=toward, wall=wall, at=at, z=z, search=search,
                     sets=[] if tol else [ax], check=tol, start=list(p0), feed=f)
    return op


def _probe_sets(prog, st) -> list[str]:
    """The axes a setup's probe ops set (in order), before its first cutting op."""
    out: list[str] = []
    for o in prog.ops:
        if prog.setup_of(o) is not st:
            continue
        if o.kind != "probe":
            break
        out += [a for a in o.params.get("sets", []) if a not in out]
    return out


def _probe_gcode(op: Op, ox: float, oy: float, oz: float, safe_wcs: float, xf=None) -> list[str]:
    """The firmware lines of one probe op, in work coordinates (setup frame minus the origin, then the setup's `align`
    transform `xf`). The probe is already over its first point at a safe height."""
    p, out = op.params, [f"G0 Z{_fmt(safe_wcs)}"]
    f = p["feed"]
    tol = p.get("check")
    F = lambda v: _fmt(v)                                                        # noqa: E731
    # the setup-frame (x, y, z) -> work coordinates
    wz = lambda v: v - oz                                                        # noqa: E731

    def wxy(x, y):
        x, y = x - ox, y - oy
        return xf(x, y) if xf else (x, y)

    def touch(word: str, dist: float):
        """A touch `dist` mm along the axis (signed): G38.2 for the whole way, or for a check G38.3 short of the
        window and G38.2 across it - the first stops early (and the second then halts: already triggered) if the face
        is nearer than dist - tol; the second halts with nothing touched if it is further than dist + tol."""
        if tol is None:
            out.append(f"G38.2 {word}{F(dist)} F{F(f)}")
        else:
            s = 1.0 if dist > 0 else -1.0
            out.append(f"G38.3 {word}{F(dist - s * tol)} F{F(f)}")
            out.append(f"G38.2 {word}{F(2 * s * tol)} F{F(f / 2)}")

    if p["probe"] == "surface":
        x, y = p["at"]
        out.append(f"G1 Z{F(wz(p['z'] + p['search']))} F{F(3 * f)}")
        touch("Z", -(p["search"] + (p["search"] if tol is None else 0.0)))
        if tol is None:
            out.append(f"G10 L20 P0 Z{F(wz(p['z']))}")
    elif p["probe"] == "hole":
        x, y = p["at"]
        out.append(f"G1 Z{F(wz(p['z_top'] + 1.0))} F{F(3 * f)}")
        out.append(f"G1 Z{F(wz(p['z']))} F{F(f)}")
        if tol is None:
            d = p["gap"] + p["search"]
            out.append(f"M480.9 X{F(d)} Y{F(d)} Z{F(p['depth'])} D{F(op.tool.diameter)}")
            out.append(f"G10 L20 P0 X{F(wxy(x, y)[0])} Y{F(wxy(x, y)[1])}")
        else:
            for word, s in (("X", -1), ("X", 1), ("Y", -1), ("Y", 1)):
                touch(word, s * p["gap"])
                out.append(f"G0 X{F(wxy(x, y)[0])} Y{F(wxy(x, y)[1])}")
    else:
        ax = p["toward"][1].upper()
        s = 1.0 if p["toward"][0] == "+" else -1.0
        out.append(f"G1 Z{F(wz(p['z']))} F{F(3 * f)}")
        touch(ax, s * p["search"] * (2.0 if tol is None else 1.0))
        if tol is None:
            v = p["wall"] - s * op.tool.diameter / 2
            tp = wxy(v, p["at"]) if ax == "X" else wxy(p["at"], v)                 # the touched point
            out.append(f"G10 L20 P0 {ax}{F(tp[0] if ax == 'X' else tp[1])}")
        st = wxy(*p["start"])
        out.append(f"G0 {ax}{F(st[0] if ax == 'X' else st[1])}")
    out.append(f"G0 Z{F(safe_wcs)}")
    return out


def _cone(tool: Tool, what: str):
    """A V / chamfer tool's cone: (shape, tip radius, tan of the half angle) - or CamError for any other tool."""
    if tool.type not in ("vbit", "chamfer") or not (0 < tool.angle < 180):
        raise CamError(f"{what} needs a V-bit or chamfer tool with its included angle set ({tool.label()} is {tool.type})")
    sh = tool.shape
    return sh, float(sh.radius_at(np.array([0.0]))[0]), math.tan(math.radians(tool.angle / 2))


def chamfer_edges(setup: Setup, tool: Tool, polys, z_top: float, width: float, side: str = "outside",
                  tip_below: float = 0.2, stepdown: float | None = None, climb: bool = True, ramp: float = RAMP_DEG,
                  name: str = "Chamfer") -> Op:
    """Chamfer the top edges of polygons with a V / chamfer tool: a chamfer `width` mm wide (measured on the top face)
    at the tool's angle (a 90° tool: as deep as wide). side 'outside': the edges of a part, a boss, its holes (the
    tool runs outside the material); 'inside': the top edge of a pocket given as its region. The tool's axis runs at
    the offset that puts its cone exactly on the chamfer, its tip `tip_below` mm under the chamfer's bottom edge (so the
    edge is clean; the space beside the wall must be cut away that deep already). The tip size is part of the offset."""
    sh, tip_r, k = _cone(tool, "chamfer_edges")
    if not (width > 0):
        raise CamError("chamfer_edges: width must be > 0")
    if side not in ("outside", "inside"):
        raise CamError("chamfer_edges: side must be outside | inside")
    tip_below = max(0.0, float(tip_below))
    d = width / k                                          # chamfer height on the wall
    if d + tip_below > sh.flute_length + 1e-9:
        raise CamError(f"chamfer_edges: a {width:g} mm chamfer is {d:.2f} mm tall; with the tip {tip_below:g} mm below it "
                       f"that is more than {tool.label()}'s {sh.flute_length:g} mm cutting height")
    x = float(sh.radius_at(np.array([tip_below]))[0])     # the axis offset from the edge: the cone meets the wall there
    z_tip = z_top - d - tip_below
    op = Op(name, "chamfer", tool, color=COLORS["chamfer"])
    pb = PathBuilder(setup, tool, op)
    src = _polys(polys)
    if not src:
        raise CamError("chamfer_edges: no polygons given")
    rings = []
    for p in src:
        g = p.buffer(x if side == "outside" else -x, join_style=1)
        for q in _polys(g):
            q = orient(q, 1.0)
            if side == "outside":
                rings.append(_ring_coords(q.exterior, True)); rings += [_ring_coords(h, False) for h in q.interiors]
            else:
                rings.append(_ring_coords(q.exterior, False)); rings += [_ring_coords(h, True) for h in q.interiors]
    if not rings:
        raise CamError(f"chamfer_edges: nothing left after offsetting by {x:.3f} mm (the shape is too small for this tool)")
    if not climb:
        rings = [r[::-1] for r in rings]
    # deeper laps grow the chamfer: the cone at the same offset, the tip lower each time
    levels = _levels(z_top - tip_below, z_tip, stepdown or max(tool.stepdown, 0.05))   # shallower: the cone is still in the air
    op.params = dict(z_top=z_top, z_bottom=z_tip, width=width, height=round(d, 4), side=side, offset=round(x, 4), passes=len(levels))
    for ring in _nn_order(rings, lambda r: r[0]):
        ring = _start_on_straight(ring)
        path = _densify(ring + [ring[0]], 0.5)[:-1]
        pb.rapid_xy(*path[0]); pb.plunge_to(z_top + ENTRY_GAP, rapid_to=z_top, gap=0.5)
        s_at = 0.0
        for z in levels:
            s_at = _lap_ramp(pb, path, z, ramp, s_at, None)
        pb.retract()
    op.warnings.append(f"{tool.label()} ({tool.spec()}): a {width:g} mm chamfer, {d:.3f} mm down the wall; the axis runs "
                       f"{x:.3f} mm {'outside' if side == 'outside' else 'inside'} the edge with the tip {z_top - z_tip:.3f} mm "
                       f"below the top - " + ("beside the wall, so that must already be cut that deep" if side == "outside"
                                               else "inside the pocket"))
    return op


def vcarve(setup: Setup, tool: Tool, polys, z_top: float, max_depth: float | None = None, hop_region=None,
           flat_stepover: float | None = None, stepdown: float | None = None, sample: float | None = None,
           name: str = "V-carve") -> Op:
    """V-carve shapes (letters, a logo) with a V-bit: the tool runs along each shape's centreline (medial axis), as deep
    as the shape is wide there - its cone touches both sides, so the top edge lands exactly on the outline, corners come
    out sharp and narrow strokes are shallow. The tip size is part of the depth (a 0.1 mm tip goes 0.05 mm less deep per
    side). max_depth: no deeper than this - where a stroke is wider the bottom is flat at max_depth and cleared with
    rings `flat_stepover` apart (default: just under the tip's width). stepdown (default the tool's): the depth is taken
    in levels no deeper than this - each level cuts only what is deeper than the one before."""
    from scipy.spatial import Voronoi
    from shapely.ops import linemerge
    import shapely
    sh, tip_r, k = _cone(tool, "vcarve")
    cap = sh.flute_length - 1e-6 if max_depth is None else min(float(max_depth), sh.flute_length - 1e-6)
    if cap <= 0:
        raise CamError("vcarve: max_depth must be > 0")
    src = _polys(polys)
    if not src:
        raise CamError("vcarve: no polygons given")
    P = unary_union(src)
    op = Op(name, "vcarve", tool, color=COLORS["vcarve"])
    pb = PathBuilder(setup, tool, op)
    ds = sample or max(0.01, min(0.05, math.sqrt(P.area) / 400))
    r_cap = tip_r + cap * k                                 # wider than this (distance to the edge): the flat bottom
    chains: list[np.ndarray] = []
    for poly in _polys(P):
        rings = [poly.exterior] + list(poly.interiors)
        pts, ring_id, idx, tang = [], [], [], []
        for ri, ring in enumerate(rings):
            L = ring.length
            n = max(8, int(math.ceil(L / ds)))
            s = np.linspace(0, L, n, endpoint=False)
            xy = np.array([ring.interpolate(v).coords[0] for v in s])
            t = np.roll(xy, -1, 0) - np.roll(xy, 1, 0)
            t /= np.maximum(np.linalg.norm(t, axis=1)[:, None], 1e-12)
            pts.append(xy); ring_id += [ri] * n; idx += list(range(n)); tang.append(t)
        X = np.vstack(pts); T_ = np.vstack(tang); ring_id = np.array(ring_id); idx = np.array(idx)
        nring = {ri: (ring_id == ri).sum() for ri in set(ring_id.tolist())}
        vor = Voronoi(X)
        V = vor.vertices
        inside = shapely.contains_xy(poly, V[:, 0], V[:, 1])
        segs = []
        for (i, j), (a, b) in zip(vor.ridge_points, vor.ridge_vertices):
            if a < 0 or b < 0 or not (inside[a] and inside[b]):
                continue
            if ring_id[i] == ring_id[j]:
                n = nring[ring_id[i]]
                gap = abs(int(idx[i]) - int(idx[j])); gap = min(gap, n - gap)
                if gap <= 3 and float(np.dot(T_[i], T_[j])) > math.cos(math.radians(30)):
                    continue                                # a spur between neighbours on a smooth stretch of edge
            segs.append(LineString([V[a], V[b]]))
        if not segs:
            continue
        merged = linemerge(unary_union(segs))
        for ln in getattr(merged, "geoms", [merged]):
            if ln.length < ds * 0.5:
                continue
            m = max(2, int(math.ceil(ln.length / ds)))
            xy = np.array([ln.interpolate(v).coords[0] for v in np.linspace(0, ln.length, m + 1)])
            r = shapely.distance(shapely.points(xy[:, 0], xy[:, 1]), poly.boundary)
            depth = np.minimum(np.maximum(r - tip_r, 0.0) / k, cap)
            chains.append(np.column_stack([xy, z_top - depth]))
    if not chains:
        raise CamError("vcarve: found no centreline (are the shapes closed polygons?)")
    fs = flat_stepover or (1.8 * tip_r if tip_r > 0.005 else 0.05)
    sd = float(stepdown or tool.stepdown or cap)
    need = min(cap, max(float((z_top - c[:, 2]).max()) for c in chains))     # as deep as the widest stroke asks
    n_lv = max(1, math.ceil(need / sd - 1e-9))
    levels = [need * (i + 1) / n_lv for i in range(n_lv)]
    rings_flat = 0
    todo_by_level = []
    prev = 0.0
    for lv in levels:
        work = []
        for c in chains:                                    # the stretches deeper than the previous level, clamped to this one
            deep = (z_top - c[:, 2]) > prev + 1e-6
            if not deep.any():
                continue
            idx = np.flatnonzero(deep)
            for run in np.split(idx, np.flatnonzero(np.diff(idx) > 1) + 1):
                lo, hi = max(run[0] - 1, 0), min(run[-1] + 1, len(c) - 1)    # one point either side: the run starts at the surface of that level
                seg = c[lo:hi + 1].copy()
                seg[:, 2] = np.maximum(seg[:, 2], z_top - lv)
                if len(seg) >= 2:
                    work.append(seg)
        flat = P.buffer(-(tip_r + lv * k), join_style=1)      # strokes wider than the V at this level: flat at this level
        g, i = flat, 0
        while not g.is_empty and i < 2000:
            for q in _polys(g):
                for rg in [q.exterior] + list(q.interiors):
                    xy = np.array(rg.coords)[:, :2]
                    work.append(np.column_stack([xy, np.full(len(xy), z_top - lv)])); rings_flat += 1
            g = flat.buffer(-fs * (i + 1), join_style=1); i += 1
        todo_by_level.append(work)
        prev = lv
    if rings_flat and fs > 2 * tip_r + 1e-9:
        op.warnings.append(f"vcarve: strokes wider than the V get a flat bottom; its rings are {fs:.3f} mm apart but the tip is "
                           f"only Ø{2 * tip_r:.3f}, so ridges up to {(fs / 2 - tip_r) / k:.3f} mm stay on that floor")
    # cut: level by level, nearest stretch next, linked at depth where they meet (a junction, the next ring in), short hops
    # just above the surface over the letters, and on entry down fast through what the previous level already cut - the
    # plunge rate only for new depth; no descent ever outruns the plunge rate
    link = max(2.5 * fs, 0.06)
    hop_z = z_top + 0.3
    # links between strokes hop 0.3 mm over the face the text sits on: by default anywhere within the text block (its
    # convex hull, 1 mm out), which the caller can widen or narrow with hop_region (a polygon in setup XY)
    near = unary_union(_polys(hop_region)) if hop_region is not None else P.convex_hull.buffer(1.0)
    at = (0.0, 0.0)
    deepest = z_top
    for li, todo in enumerate(todo_by_level):
        pv = levels[li - 1] if li else 0.0
        while todo:
            j = min(range(len(todo)), key=lambda q: min(math.dist(at, todo[q][0, :2]), math.dist(at, todo[q][-1, :2])))
            c = todo.pop(j)
            closed = len(c) > 2 and math.dist(c[0, :2], c[-1, :2]) < 1e-9
            if closed:                                      # a ring: start at its point nearest the tool
                i0 = int(np.argmin(np.hypot(c[:-1, 0] - at[0], c[:-1, 1] - at[1])))
                c = np.vstack([c[i0:-1], c[:i0 + 1]])
            elif math.dist(at, c[-1, :2]) < math.dist(at, c[0, :2]):
                c = c[::-1]
            s = c[0]
            down = pb.pos is not None and pb.pos[2] < z_top - 1e-9
            if down and math.dist(pb.pos[:2], s[:2]) <= link:
                L = math.dist(pb.pos[:2], s[:2]); dz = max(pb.pos[2] - s[2], 0.0)
                pb.feed_to(s[0], s[1], s[2], pb.feed if dz < 1e-9 else min(pb.feed, pb.plunge * math.hypot(L, dz) / dz))
            else:
                if pb.pos is not None and near.covers(LineString([pb.pos[:2], s[:2]])):
                    if pb.pos[2] < hop_z:
                        pb._add(RAPID, pb.pos[0], pb.pos[1], hop_z)
                    pb._add(RAPID, s[0], s[1], hop_z)
                else:
                    pb.rapid_xy(s[0], s[1]); pb._add(RAPID, s[0], s[1], hop_z)
                gone = max(s[2], z_top - pv)                # the material is already cut away down to here
                pb.feed_to(s[0], s[1], gone)
                if s[2] < gone - 1e-9:
                    pb._add(PLUNGE, s[0], s[1], s[2], pb.plunge)
            for p0, p1 in zip(c[:-1], c[1:]):
                L = math.dist(p0[:2], p1[:2]); dz = abs(p1[2] - p0[2])
                f = pb.feed if dz < 1e-9 or p1[2] > p0[2] else min(pb.feed, pb.plunge * math.hypot(L, dz) / dz)
                pb.feed_to(p1[0], p1[1], p1[2], f)
            deepest = min(deepest, float(c[:, 2].min()))
            at = tuple(c[-1, :2])
    pb.retract()
    op.params = dict(z_top=z_top, z_bottom=deepest, max_depth=cap, centrelines=len(chains), flat_rings=rings_flat, levels=len(levels),
                     hop_z=round(hop_z, 3))
    op.warnings.append(f"{tool.label()} ({tool.spec()}): V-carved {len(chains)} centreline stretch{'es' if len(chains) != 1 else ''}, "
                       f"deepest {z_top - deepest:.3f} mm in {len(levels)} level{'s' if len(levels) != 1 else ''}"
                       + (f"; strokes wider than {2 * r_cap:.2f} mm have a flat bottom at {cap:g} mm" if rings_flat else ""))
    return op


def _drop_tip(zmap: np.ndarray, tool: Tool, res: float, depth: float) -> np.ndarray:
    """Lowest tip height over each height-map cell with no part of the tool below the surface. Only the parts of the
    tool within `depth` (the material's thickness) of the tip can touch, so wider parts higher up (a big shank) are
    left out of the footprint once they are above it."""
    sh = tool.shape
    rmax = sh.max_radius
    reach_r = float(np.max(sh.radius_at(np.linspace(0, max(depth, 1e-3), 200))))     # widest part that can touch
    r = min(rmax, max(reach_r, sh.cut_radius))
    k = int(math.ceil(r / res))
    ny, nx = zmap.shape
    ii, jj = np.mgrid[-k:k + 1, -k:k + 1]
    d = np.hypot(ii * res, jj * res)
    h = sh.height(d)
    tip = np.full_like(zmap, -np.inf)
    padded = np.pad(zmap, k, mode="edge")
    for i in range(2 * k + 1):
        for j in range(2 * k + 1):
            if np.isfinite(h[i, j]) and h[i, j] <= depth + 1e-9:
                tip = np.maximum(tip, padded[i:i + ny, j:j + nx] - h[i, j])
    return tip


def parallel3d(setup: Setup, tool: Tool, shape: Shape | None = None, model: ck.Model | None = None,
               z_top: float | None = None, z_bottom: float | None = None, region=None,
               stepover: float | None = None, angle: float = 0.0, resolution: float | None = None,
               rest_from: Tool | None = None, pattern: str = "parallel", one_way: bool = False,
               center: tuple | None = None, name: str = "Parallel 3D") -> Op:
    """3D finishing: raster passes following the surface (heightmap from the exact shape's triangulation, offset by the
    tool's whole profile: any type — flat, ball, bull nose, V, tapered ball — with its neck and shank kept out of the
    surface too). Cuts down to z_bottom outside the part. rest_from=<bigger tool>: only where that tool left material.
    The lay is part of the finish: pattern "parallel" (straight passes at `angle`), "radial" (rays from `center`, the
    region's centre by default) or "spiral" (one continuous spiral out from `center`); one_way=True cuts every pass in
    the same direction (climb throughout, one uniform lay) and rapids back just over the surface instead of zig-zagging.
    Links between passes never drag across the finished surface: they step over at the higher of the two heights, or
    hop above the taller of the two passes and come down to within 0.05 mm of the surface before feeding."""
    if pattern not in ("parallel", "radial", "spiral"):
        raise CamError("parallel3d: pattern must be parallel | radial | spiral")
    if shape is None and model is None:
        raise CamError("parallel3d: give shape= or model=")
    shape = shape or model.shape
    op = Op(name, "parallel3d", tool, color=COLORS["parallel3d"])
    pb = PathBuilder(setup, tool, op)
    fd = tool.tip_diameter if tool.type == "taper_ball" and tool.tip_diameter else tool.diameter    # what does the finishing
    so = (stepover or min(tool.stepover, 0.2)) * fd
    res = resolution or max(0.05, min(0.2, so / 2, fd / 6)) if fd < 1.2 else (resolution or max(0.2, min(so / 2, fd / 6)))
    regp = unary_union(_polys(region)) if region is not None else setup.stock.rect()
    minx, miny, maxx, maxy = regp.bounds
    z_top = setup.stock.top if z_top is None else z_top
    z_bottom = setup.stock.bottom if z_bottom is None else z_bottom
    # heightmap
    nx = int((maxx - minx) / res) + 2; ny = int((maxy - miny) / res) + 2
    zmap = np.full((ny, nx), z_bottom, dtype=np.float64)
    for _, sh in ck.coerce_bodies(shape):               # the slicing mesh (0.01 mm, kept per shape): a display-quality
        P, I = _mesh_of(sh, 0.03)                       # build_model("fine") re-meshed and measured the part on every call
        _raster_triangles(P, I, zmap, minx, miny, res)
    zmap = np.clip(zmap, z_bottom, None)
    # drop-cutter on the height map: the tip sits as low as it can with no part of the tool (cutting shape, neck,
    # shank) below the surface: tip(p) = max over |q - p| <= tool radius of zmap(q) - height_of_tool(|q - p|)
    tip = _drop_tip(zmap, tool, res, z_top - z_bottom)
    tip = np.clip(tip, z_bottom, z_top)
    mask = None
    if rest_from is not None:
        tip_prev = np.clip(_drop_tip(zmap, rest_from, res, z_top - z_bottom), z_bottom, z_top)
        mask = (tip_prev - tip) > 0.02          # previous tool sat higher: material left behind
        # grow the mask by one tool radius so passes overlap into the finished area
        g = max(1, int(round(tool.radius / res)))
        from numpy.lib.stride_tricks import sliding_window_view
        pm = np.pad(mask, g, mode="constant")
        mask = sliding_window_view(pm, (2 * g + 1, 2 * g + 1)).any(axis=(2, 3))
        if not mask.any():
            op.warnings.append(f"rest: nothing left for {tool.label()} after {rest_from.label()}")
            return op
    # the passes, as point sequences in XY (then clipped to the region / rest mask into runs)
    ca, sa = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    diag = math.hypot(maxx - minx, maxy - miny)
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    if center is not None:
        cx, cy = float(center[0]), float(center[1])
    paths: list = []                                               # each: f(direction) -> (xs, ys)
    if pattern == "parallel":
        n_lines = int(diag / so) + 1
        ts0 = np.arange(-diag / 2, diag / 2 + res, res)
        for li in range(-n_lines // 2, n_lines // 2 + 1):
            ox, oy = cx - sa * li * so, cy + ca * li * so           # line li: c + u*t + v*li*so, u=(ca,sa), v=(-sa,ca)
            paths.append((lambda d, ox=ox, oy=oy: (ox + ca * ts0 * d, oy + sa * ts0 * d)))   # the sign flips the sampling, as before
    elif pattern == "radial":
        R = max(math.hypot(x - cx, y - cy) for x, y in ((minx, miny), (maxx, miny), (minx, maxy), (maxx, maxy))) + res
        n_rays = max(8, int(math.ceil(2 * math.pi * R / so)))     # the outer ends are one stepover apart
        ts = np.arange(0.0, R + res, res)
        for k in range(n_rays):
            th = 2 * math.pi * k / n_rays
            paths.append((lambda d, th=th: ((cx + math.cos(th) * ts)[::d], (cy + math.sin(th) * ts)[::d])))
    else:                                                          # spiral: r = so * theta / 2pi, sampled every `res` along it
        R = max(math.hypot(x - cx, y - cy) for x, y in ((minx, miny), (maxx, miny), (minx, maxy), (maxx, maxy))) + res
        b_ = so / (2 * math.pi)
        th = [0.0]
        while b_ * th[-1] < R:
            r_ = max(b_ * th[-1], res)
            th.append(th[-1] + res / r_)
        th = np.array(th)
        paths.append((lambda d: ((cx + b_ * th * np.cos(th))[::d], (cy + b_ * th * np.sin(th))[::d])))
    direction = 1
    first = True
    prev_top = None                                                # the highest tip Z on the previous pass (for one-way hops)
    for path_fn in paths:
        xs, ys = path_fn(direction)
        inside = (xs >= minx) & (xs <= maxx) & (ys >= miny) & (ys <= maxy)
        if not inside.any():
            if not one_way: direction *= -1
            continue
        if region is not None and pattern == "parallel":
            line = LineString([(xs[inside][0], ys[inside][0]), (xs[inside][-1], ys[inside][-1])])
            if not regp.intersects(line):
                if not one_way: direction *= -1
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
            if not one_way: direction *= -1
            continue
        for ri, pts in enumerate(runs):
            pts = _simplify_z(pts)
            top = max(p[2] for p in pts)
            if first or pb.pos is None:
                pb.rapid_xy(pts[0][0], pts[0][1]); pb.plunge_to(pts[0][2]); first = False
            elif mask is not None or ri > 0 or one_way or pattern != "parallel":
                # a separate patch, or a one-way return: hop just above the taller of the two passes (the surface
                # between neighbouring passes is no higher), rapid back, come down to 0.05 above the surface, feed the rest
                hop_z = min(max(top, prev_top if prev_top is not None else top, pb.pos[2]) + 0.3, setup.safe_z)
                if math.dist(pb.pos[:2], pts[0][:2]) > 3 * so or mask is not None:
                    pb._add(RAPID, pb.pos[0], pb.pos[1], hop_z)
                    pb._add(RAPID, pts[0][0], pts[0][1], hop_z)
                    if pts[0][2] + 0.05 < hop_z - 1e-6:
                        pb._add(RAPID, pts[0][0], pts[0][1], pts[0][2] + 0.05)
                    pb._add(PLUNGE, pts[0][0], pts[0][1], pts[0][2], pb.plunge)
                else:
                    pb.feed_to(pts[0][0], pts[0][1], max(pts[0][2], pb.pos[2]))
                    pb.feed_to(pts[0][0], pts[0][1], pts[0][2], pb.plunge)
            else:
                pb.feed_to(pts[0][0], pts[0][1], max(pts[0][2], pb.pos[2]))   # step over at the higher of the two
                pb.feed_to(pts[0][0], pts[0][1], pts[0][2], pb.plunge)
            pb.feed_path(pts[1:])
            prev_top = top
        if not one_way:
            direction *= -1
    pb.retract()
    op.params = dict(stepover=so, resolution=res, z_top=z_top, z_bottom=z_bottom, grid=[nx, ny], pattern=pattern,
                     one_way=one_way, angle=angle)
    return op


def _simplify_z(pts, tol: float = 0.005):
    """Drop points along a raster line that the straight move replacing them passes within `tol` of — measured from
    the last point KEPT, so a gentle curve sampled finely is never flattened into one long chord (which would gouge)."""
    if len(pts) <= 2:
        return list(pts)
    P = np.asarray(pts, dtype=float)
    out = [pts[0]]
    a = 0
    i = 2
    while i < len(P):
        seg = P[i] - P[a]
        L = float(np.linalg.norm(seg)) or 1.0
        mid = P[a + 1:i] - P[a]
        proj = mid @ seg / L
        perp = np.sqrt(np.maximum((mid * mid).sum(axis=1) - proj * proj, 0.0))
        if perp.max() > tol:
            a = i - 1
            out.append(pts[a])
        i += 1
    out.append(pts[-1])
    return out


def _scatter_max(flat: np.ndarray, idx: np.ndarray, vals: np.ndarray) -> None:
    """flat[idx] = max(flat[idx], vals) with repeated indices (a sort + reduceat: np.maximum.at is 20x slower)."""
    if not len(idx):
        return
    o = np.argsort(idx, kind="stable")
    si, sv = idx[o], vals[o]
    starts = np.r_[0, np.nonzero(si[1:] != si[:-1])[0] + 1]
    u = si[starts]
    flat[u] = np.maximum(flat[u], np.maximum.reduceat(sv, starts))


def _raster_triangles(P: np.ndarray, I: np.ndarray, zmap: np.ndarray, minx: float, miny: float, res: float):
    """zmap = max(zmap, the triangles' height) over the cells whose centres they cover. The triangles are rasterised in
    batches of similar bounding-box size (one (n x h x w) barycentric test per batch; a loop of one meshgrid per
    triangle took 44 s on the latch job's parallel3d ops). Same arithmetic as that loop, so the same heights."""
    ny, nx = zmap.shape
    if not len(I):
        return
    A, B, C = P[I[:, 0]], P[I[:, 1]], P[I[:, 2]]
    d = (B[:, 1] - C[:, 1]) * (A[:, 0] - C[:, 0]) + (C[:, 0] - B[:, 0]) * (A[:, 1] - C[:, 1])
    x0 = np.maximum(np.trunc((np.minimum(np.minimum(A[:, 0], B[:, 0]), C[:, 0]) - minx) / res).astype(np.int64), 0)
    x1 = np.minimum(np.trunc((np.maximum(np.maximum(A[:, 0], B[:, 0]), C[:, 0]) - minx) / res).astype(np.int64) + 1, nx - 1)
    y0 = np.maximum(np.trunc((np.minimum(np.minimum(A[:, 1], B[:, 1]), C[:, 1]) - miny) / res).astype(np.int64), 0)
    y1 = np.minimum(np.trunc((np.maximum(np.maximum(A[:, 1], B[:, 1]), C[:, 1]) - miny) / res).astype(np.int64) + 1, ny - 1)
    keep = np.nonzero((x1 >= x0) & (y1 >= y0) & (np.abs(d) >= 1e-12))[0]
    if not len(keep):
        return
    w, h = x1[keep] - x0[keep] + 1, y1[keep] - y0[keep] + 1
    big = keep[w * h > 16384]                           # a few large flat triangles: straight onto their window
    for t in big:
        a, b, c = A[t], B[t], C[t]
        X, Y = np.meshgrid(minx + np.arange(x0[t], x1[t] + 1) * res, miny + np.arange(y0[t], y1[t] + 1) * res)
        l1 = ((b[1] - c[1]) * (X - c[0]) + (c[0] - b[0]) * (Y - c[1])) / d[t]
        l2 = ((c[1] - a[1]) * (X - c[0]) + (a[0] - c[0]) * (Y - c[1])) / d[t]
        l3 = 1 - l1 - l2
        m = (l1 >= -1e-6) & (l2 >= -1e-6) & (l3 >= -1e-6)
        if m.any():
            sub = zmap[y0[t]:y1[t] + 1, x0[t]:x1[t] + 1]
            np.maximum(sub, np.where(m, l1 * a[2] + l2 * b[2] + l3 * c[2], -np.inf), out=sub)
    keep = keep[w * h <= 16384]
    w, h = x1[keep] - x0[keep] + 1, y1[keep] - y0[keep] + 1
    order = keep[np.argsort(w * h, kind="stable")]
    flat = zmap.ravel()
    budget, i = 3_000_000, 0
    while i < len(order):
        rest = order[i:]
        cw, ch = np.maximum.accumulate(x1[rest] - x0[rest] + 1), np.maximum.accumulate(y1[rest] - y0[rest] + 1)
        n = max(1, int(np.searchsorted(np.arange(1, len(rest) + 1) * cw * ch, budget, side="right")))
        t = rest[:n]; W, H = int(cw[n - 1]), int(ch[n - 1])
        ox = np.arange(W)[None, None, :]; oy = np.arange(H)[None, :, None]
        a, b, c = A[t][:, :, None, None], B[t][:, :, None, None], C[t][:, :, None, None]
        tx0, ty0 = x0[t][:, None, None], y0[t][:, None, None]
        X = minx + (tx0 + ox) * res; Y = miny + (ty0 + oy) * res
        dd = d[t][:, None, None]
        l1 = ((b[:, 1] - c[:, 1]) * (X - c[:, 0]) + (c[:, 0] - b[:, 0]) * (Y - c[:, 1])) / dd
        l2 = ((c[:, 1] - a[:, 1]) * (X - c[:, 0]) + (a[:, 0] - c[:, 0]) * (Y - c[:, 1])) / dd
        l3 = 1 - l1 - l2
        m = (l1 >= -1e-6) & (l2 >= -1e-6) & (l3 >= -1e-6) & (ox <= (x1[t] - x0[t])[:, None, None]) & (oy <= (y1[t] - y0[t])[:, None, None])
        if m.any():
            Z = l1 * a[:, 2] + l2 * b[:, 2] + l3 * c[:, 2]
            _scatter_max(flat, ((ty0 + oy) * nx + (tx0 + ox) + 0 * m)[m], Z[m])
        i += n


PUBLIC_API = [
    "regular_polygon","Stock.from_model", "Stock.block", "Stock.cylinder", "Setup", "Setup.view", "Program", "Program.fit_feeds", "face", "contour", "pocket", "bore",
              "adaptive", "drill", "parallel3d", "chamfer_edges", "vcarve", "rest_region", "section", "silhouette", "outline", "face_outline", "stock_minus", "free_region", "holes", "face_polygon",
              "circle", "rect", "feeds", "apply_feeds", "rotary_rough", "rotary_finish", "rotary_wrap", "rotary_profile",
              "gear_teeth", "radial_map", "tiles_of", "probe_surface", "probe_hole", "probe_edge", "probe_tool",
              "register_holes", "register_hole_ops", "Program.alignment_features"]


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
    lines.append("Every cutting op also takes load= (0.3..2: the chip load Program.fit_feeds holds for it; softer < 1 < "
                 "harder) and bodies= (the body name(s) it cuts, for the simulation's checks).")
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
          radial_engagement: float | None = None, depth: float | None = None) -> dict:
    """Feeds & speeds from surface speed and chip load. aggressiveness 0..1 picks within the vc range.
    radial_engagement (fraction of diameter) < 0.5 applies radial chip thinning. Spindle limits from the
    machine clamp rpm (feed follows). Returns rpm, feed, plunge, stepdown, stepover, vc, chipload, notes.
    A tool that is not a flat endmill cuts with its width at the cut depth, not its body diameter: a V-bit / chamfer
    (default depth 0.5 mm, or `depth`) and a ball / bull / tapered ball when `depth` is given are sized by that width."""
    key = MATERIAL_ALIASES.get(material.lower().strip(), material.lower().strip())
    if key not in MATERIALS:
        raise CamError(f"unknown material '{material}'. Known: {', '.join(MATERIALS)}")
    mat = MATERIALS[key]
    notes: list[str] = []
    a = min(max(aggressiveness, 0.0), 1.0)
    vc = mat["vc"][0] + (mat["vc"][1] - mat["vc"][0]) * a
    d = tool.diameter
    if tool.type not in ("flat", "drill"):
        dd = depth if depth is not None else (min(0.5, tool.flute_length) if tool.type in ("vbit", "chamfer") else None)
        if dd is not None and dd > 0:
            try:
                w = tool.shape.width_at_depth(float(dd))
            except Exception:  # noqa: BLE001 - a bad shape: fall back to the diameter (the shape error shows elsewhere)
                w = d
            if 0 < w < d - 1e-9:
                notes.append(f"sized for its Ø{w:.3f} cutting width at {float(dd):g} mm deep (not the Ø{d:g} body)"
                             + ("" if depth is not None else "; give the engraving depth for exact numbers"))
                d = max(w, 0.05)
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
    if tool.type in ("ball", "taper_ball"):
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
FIT_SKIP_KINDS = ("probe", "drill", "vcarve", "chamfer", "gear_teeth")   # fit_feeds: no radial-engagement model for these


def _free_spot(stock, inset: float, keep_out, ref=None, max_cells: int = 1200):
    """A point on the stock's top at least `inset` in from its edges and clear of every keep-out: (A, B, r) = segments
    A[i]-B[i] (xy rows) that nothing within r of may touch. On a grid (cell ~ the stock's size / max_cells, 0.25 mm at
    the finest): the cells within r (+1.5 cells, so a cell centre that passes is clear of the exact segment too) of the
    samples along each segment are blocked; the free cell nearest `ref`, or the one furthest from anything blocked when
    no ref is given. None when nothing is free."""
    from scipy.ndimage import distance_transform_edt
    x0, y0, x1, y1 = stock.xmin + inset, stock.ymin + inset, stock.xmax - inset, stock.ymax - inset
    if x1 <= x0 or y1 <= y0:
        return None
    cell = max(0.25, max(x1 - x0, y1 - y0) / max_cells)
    nx, ny = int(math.ceil((x1 - x0) / cell)) + 1, int(math.ceil((y1 - y0) / cell)) + 1
    xs, ys = x0 + np.arange(nx) * cell, y0 + np.arange(ny) * cell
    blocked = np.zeros((ny, nx), bool)
    by_r: dict[float, list] = {}
    for A, B, r in keep_out:
        by_r.setdefault(round(float(r), 3), []).append((np.asarray(A, float), np.asarray(B, float)))
    for r, parts in by_r.items():
        pad = int(math.ceil(r / cell)) + 2           # cuts just outside the free area still block it
        hit = np.zeros((ny + 2 * pad, nx + 2 * pad), bool)
        for A_, B_ in parts:
            for c0 in range(0, len(A_), 50_000):        # samples a cell apart along each segment, a chunk at a time
                A, B = A_[c0:c0 + 50_000], B_[c0:c0 + 50_000]
                n = np.maximum(1, np.ceil(np.hypot(*(B - A).T) / cell).astype(int))
                seg = np.repeat(np.arange(len(n)), n + 1)
                t = (np.arange(len(seg)) - np.repeat(np.cumsum(n + 1) - (n + 1), n + 1)) / n[seg]
                S = A[seg] + (B - A)[seg] * t[:, None]
                ix = np.round((S[:, 0] - x0) / cell).astype(int) + pad
                iy = np.round((S[:, 1] - y0) / cell).astype(int) + pad
                ok = (ix >= 0) & (ix < nx + 2 * pad) & (iy >= 0) & (iy < ny + 2 * pad)
                hit[iy[ok], ix[ok]] = True
        if hit.any():
            d = distance_transform_edt(~hit)[pad:pad + ny, pad:pad + nx] * cell
            blocked |= d <= r + 1.5 * cell
    if blocked.all():
        return None
    if ref is not None:
        free = np.argwhere(~blocked)
        j = int(np.argmin((xs[free[:, 1]] - ref[0]) ** 2 + (ys[free[:, 0]] - ref[1]) ** 2))
        iy, ix = free[j]
    else:
        clear = distance_transform_edt(np.pad(~blocked, 1))[1:-1, 1:-1]
        iy, ix = np.unravel_index(int(np.argmax(clear)), clear.shape)
    return float(xs[ix]), float(ys[iy])


class Program:
    """Operations in run order. Each op carries the Setup it was made with; consecutive ops that share a setup run
    together. A change of setup is an operator step (flip or re-clamp the part, re-zero) unless both setups are on
    the 4th axis with the same mounting, in which case the post just turns A to the next index angle."""

    gcode_lines: int | None = None                # set by the app when it builds the program

    def __init__(self, setup: Setup, ops: Iterable[Op] = (), name: str = "program", load: float = 1.0):
        self.setup = setup
        self.ops: list[Op] = list(ops)
        self.name = name
        self.load = load                          # fit_feeds' default chip load scale (softer < 1 < harder); ops may set their own
        self.auto_fit = True                       # after_simulation() fits the feeds (False: the planned feeds stay)
        self.feeds_fitted: dict | None = None      # what the last fit_feeds did

    def add(self, op: Op, tiles: bool = True) -> Op:
        """Add an operation. tiles=False: with a tiled setup, this op runs ONCE (its moves already cover every tile:
        a face or roughing pass computed over the whole plate) instead of at each tile."""
        if op.setup is None:
            op.setup = self.setup
        op.tiled = bool(tiles)
        if not op.key:
            op.key = op.name
        _trim_floor_finishes(self, op)
        c = (_OV["table"].get(op.key) or {}).get("color")
        if isinstance(c, str) and not op.color_override:            # script-built ops: the colour is all the tab sets
            op.color, op.color_override = c, True
            _OV["used"].add(op.key)
        self.ops.append(op)
        progress(None, f"op {len(self.ops)} built: {op.name}", len(self.ops))
        return op

    def op_tiles(self, op: Op) -> list[tuple[float, float, float]]:
        """The setup-frame offsets an op runs at: its setup's tiles, or once (no offset) for tiles=False ops."""
        st = self.setup_of(op)
        return st.tile_offsets() if op.tiled else [(0.0, 0.0, 0.0)]

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
    def material(self) -> str:
        """What is being cut: Program.material_override if set, else the program setup's stock material ("" = unknown)."""
        return getattr(self, "material_override", None) or self.setup.model_stock.material or self.setup.stock.material

    @material.setter
    def material(self, value: str) -> None:
        self.material_override = _material(value)

    @property
    def machine(self) -> Machine:
        """A program runs on one machine: the program setup's. Setups give frames, orientation and work offsets."""
        return self.setup.machine

    def op_minutes(self, op: Op) -> float:
        """One op's time with its setup's tiles: once per tile, plus the lift-and-move between tiles."""
        st = self.setup_of(op)
        offs = self.op_tiles(op)
        t1 = op.time_minutes(self.machine)
        if len(offs) <= 1:
            return t1
        mv, _ = tile_moves(op, offs, st.clearance_z)
        tmp = Op(op.name, op.kind, op.tool, moves=mv)
        return tmp.time_minutes(self.machine)

    def time_minutes(self) -> float:
        return sum(self.op_minutes(op) for op in self.ops)

    def bounds(self, setup: Setup | None = None):
        ops = [op for op in self.ops if op.moves and (setup is None or self.setup_of(op) is setup)]
        if not ops:
            return None
        lo = [1e18] * 3; hi = [-1e18] * 3
        for op in ops:
            a, b = op.bounds()
            offs = self.op_tiles(op)
            for i in range(3):
                lo[i] = min(lo[i], a[i] + min(o[i] for o in offs)); hi[i] = max(hi[i], b[i] + max(o[i] for o in offs))
        return tuple(lo), tuple(hi)

    def check(self) -> list[str]:
        w: list[str] = []
        if not self.ops:
            return ["program has no operations"]
        self.assign_wcs()
        m = self.machine
        w += m.warnings()
        seen = set()
        for op in self.ops:
            t = op.tool
            if t.type in ("vbit", "chamfer") and not t.tip_diameter and not t.sharp and t.label() not in seen:
                seen.add(t.label())
                w.append(f"{t.label()} ({t.spec()}): no tip Ø set, so it is modelled as a perfectly sharp point; a real tip "
                         "cuts wider by that much at every depth. Set it in Settings ▸ Tools (e.g. 0.1 for a 0.1 mm V-bit), or "
                         "tick Sharp point there if it really has none")
        for st in self.setups:
            if len(st.tiles) > 1 and st.tile_wcs:
                if m.post == "makera":
                    w.append(MAKERA_TILE_WCS)
                else:
                    w.append(f"{st.name}: {len(st.tiles)} tiles in their own work offsets ({', '.join(st.tile_wcs_names())}): "
                             f"set each one on the machine before running (the G-code header lists them as {st.wcs} + offset)")
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
                    w.append(f"{tag}toolpath goes {st.stock.bottom - z0:.2f} mm below the stock bottom "
                             + ("(a through-cut: the simulation checks it against the fixture)" if st.fixture else "(into the spoilboard?)"))
            if not st.rotary and st.orient == "top":
                try:
                    w += [tag + x for x in st.fixture_warnings()]
                except Exception:  # noqa: BLE001 - a fixture check never stops the program check
                    pass
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
                # the work origin must sit on the rotary centreline: the post's A feeds and the simulation assume it
                oy, oz = st.origin_point()[1], st.origin_point()[2]
                if abs(oy - st.axis[0]) > 1e-3 or abs(oz - st.axis[1]) > 1e-3:
                    w.append(f"{tag}the work origin is {abs(oy - st.axis[0]):.2f} mm (Y) / {abs(oz - st.axis[1]):.2f} mm (Z) "
                             "off the rotary centreline: 4th-axis programs must be zeroed on the axis")
                # Y / Z reach from the axis (where the module sits in the travel), when the machine says
                if b and rot.get("y_reach"):
                    lo_y, hi_y = rot["y_reach"]
                    if y0 < lo_y - 1e-6 or y1 > hi_y + 1e-6:
                        w.append(f"{tag}toolpath Y {y0:.1f} .. {y1:.1f} from the rotary axis; {m.name} reaches "
                                 f"{lo_y:.1f} .. {hi_y:.1f} mm either side of it")
                if b and rot.get("axis_z") and z0 < -float(rot["axis_z"]) + 1e-6:
                    w.append(f"{tag}toolpath goes {-(z0 + float(rot['axis_z'])):.1f} mm below the bed (the axis is {rot['axis_z']} mm above it)")
        for op in self.ops:
            st = self.setup_of(op); t = op.tool
            if op.uses_a and not st.rotary:
                w.append(f"{op.name}: moves A but its setup is not on the 4th axis")
            if op.uses_a and st.rotary and (m.rotary or {}).get("max_speed"):
                ar = op.a_rate_max()
                if ar > float(m.rotary["max_speed"]) * 1.001:
                    w.append(f"{op.name}: asks the 4th axis for {ar:.0f} deg/min, above its {m.rotary['max_speed']:.0f} deg/min: "
                             "the controller will slow those moves (the time estimate allows for it)")
            if op.kind == "probe":
                cut_before = [o.name for o in self.ops[:self.ops.index(op)] if self.setup_of(o) is st and o.kind != "probe"]
                if op.params.get("sets") and cut_before:
                    w.append(f"{op.name}: sets {'/'.join(op.params['sets']).upper()} after {cut_before[-1]} already cut in {st.name} "
                             "with the old origin: probe at the start of the setup")
            elif not (m.spindle["min"] <= t.rpm <= m.spindle["max"]):
                w.append(f"{op.name}: {t.label()} rpm {t.rpm} outside spindle range {m.spindle}")
            w += [f"{op.name}: {x}" for x in op.warnings]
        # stacked roughing bands that leave different skins on their walls: a later band with less leave has the
        # earlier bands' skin standing inside its region above its top, where its lifted links run
        ad = [(op, self.setup_of(op)) for op in self.ops if op.kind == "adaptive" and "stock_to_leave" in op.params]
        for i, (op, st) in enumerate(ad):
            for op0, st0 in ad[:i]:
                if st0 is st and op0.params["z_bottom"] >= op.params["z_top"] - 1e-6 and op0.params["stock_to_leave"] > op.params["stock_to_leave"] + 1e-6:
                    w.append(f"{op.name}: leaves {op.params['stock_to_leave']:g} mm on its walls where {op0.name} above it left "
                             f"{op0.params['stock_to_leave']:g}: that skin stands inside {op.name}'s region above its top, so its "
                             "lifted links can brush it - use the same stock_to_leave on every band and finish the wall with a contour")
                    break
        for k in getattr(self, "unused_overrides", []):
            w.append(f"override for '{k}' matches no operation (renamed or removed?)")
        tools = {op.tool.number for op in self.ops}
        if len(tools) > 1 and m.tool_change == "none":
            w.append(f"program uses {len(tools)} tools but machine tool_change is 'none'")
        flat = [st for st in self.setups if not st.rotary]
        if len(flat) >= 2 and not any((op.params or {}).get("register") or (op.kind == "probe" and (op.params or {}).get("probe") == "hole")
                                      for op in self.ops):
            w.append("multi-setup program without alignment features: add register_hole_ops(...) in the first setup so "
                     "the flipped setup can be aligned (Align setup)")
        return w

    def alignment_features(self, depth: float = 3.0) -> list[dict]:
        """For each later flat setup, what to probe to align it: the register holes the earlier setups bored (ops tagged
        params["register"], see register_hole_ops) and the holes its own probe_hole ops target, in THAT setup's work
        frame, plus a "surface" work XY on an untouched spot of its top (clear of its cutting ops, of cuts from earlier
        setups that reach its top, and of the holes) for a Z touch. [] for a one-setup program.
        [{"setup", "wcs", "holes": [{"x", "y", "dia", "depth"}], "surface": (x, y) | None, "z_top"}]"""
        self.assign_wcs()
        flat = [st for st in self.setups if not st.rotary]
        if len(flat) < 2:
            return []
        out = []
        for k, st in enumerate(flat[1:], 1):
            ox, oy, oz = st.origin_point()
            ztop = st.stock.top
            holes: list[dict] = []

            def add(x, y, d, dep):
                if all(math.hypot(h["x"] - x, h["y"] - y) > 0.5 for h in holes):
                    holes.append({"x": round(float(x), 3), "y": round(float(y), 3), "dia": float(d), "depth": round(float(dep), 3)})
            for op in self.ops:
                s0 = self.setup_of(op)
                reg = (op.params or {}).get("register")
                if reg and any(s0 is e for e in flat[:k]):
                    zmid = (s0.stock.top + s0.stock.bottom) / 2
                    P = st.to_setup(s0.to_model([(x, y, zmid) for x, y in reg["holes"]]))
                    for q in P:
                        add(q[0] - ox, q[1] - oy, reg["dia"], min(depth, st.stock.top - st.stock.bottom))
                if s0 is st and op.kind == "probe" and (op.params or {}).get("probe") == "hole":
                    x, y = op.params["at"]
                    add(x - ox, y - oy, op.params["diameter"], op.params.get("depth", depth))
            # an untouched spot of this setup's top: away from its cuts, from earlier cuts that reach its top, and the holes.
            # Worked out on a grid: buffering every cutting move into one polygon sent GEOS into its snap-rounding fallback
            # for over an hour on the 88-op latch sheet (and froze the app: it ran on the event loop)
            keep_out = []                                 # (xy samples along the cuts, clearance radius)
            for op in self.ops:
                s0 = self.setup_of(op)
                if op.kind == "probe" or not op.moves or not any(s0 is e for e in flat[:k + 1]):
                    continue
                P = np.array([m_[1:4] for m_ in op.moves], dtype=float)
                cut = np.array([m_[0] != RAPID for m_ in op.moves])
                if s0 is not st:
                    P = st.to_setup(s0.to_model(P))
                near_top = P[:, 2] >= ztop - 0.5 if s0 is not st else np.ones(len(P), bool)
                seg = cut[1:] & (near_top[1:] | near_top[:-1])
                if seg.any():
                    keep_out.append((P[:-1, :2][seg], P[1:, :2][seg], op.tool.radius + 2.0))
            for h in holes:
                c = np.array([[h["x"] + ox, h["y"] + oy]])
                keep_out.append((c, c, h["dia"] / 2 + 3.0))
            ref = (holes[0]["x"] + ox, holes[0]["y"] + oy) if holes else None
            q = _free_spot(st.stock, 4.0, keep_out, ref)
            surf = (round(q[0] - ox, 3), round(q[1] - oy, 3)) if q is not None else None
            out.append({"setup": st.name, "wcs": st.wcs, "holes": holes, "surface": surf, "z_top": round(float(ztop - oz), 3)})
        return out

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
            if len(st.tiles) > 1:
                lines.append(f"  tiles: {len(st.tiles)} — every op computed once and run at each tile "
                             + ("in its own work offset " + ", ".join(st.tile_wcs_names()) if st.tile_wcs else f"in {st.wcs} with offsets")
                             + "; offsets " + ", ".join(f"({t[0]:g}, {t[1]:g})" for t in st.tiles[:6]) + (" …" if len(st.tiles) > 6 else ""))
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
                nt = len(self.op_tiles(op))
                lines.append(f"  {i}. {op.name} [{op.kind}] {op.tool.label()} ({op.tool.spec()}) — {len(op.moves)} moves, "
                             f"cut {cut:.0f} mm, {self.op_minutes(op):.1f} min{f' ({nt} tiles)' if nt > 1 else ''}, {zr}{aa} "
                             + " ".join(f"{k}={v}" for k, v in op.params.items()))
        w = self.check()
        if w:
            lines.append("WARNINGS: " + "; ".join(w))
        return "\n".join(lines)

    def to_payload(self, moves: bool = True) -> dict[str, Any]:
        """For the viewer: everything in MODEL coordinates (rotary moves wrapped back onto the part). moves=False leaves
        every op's "moves" as None and sets "moves_blob": the moves travel as one binary frame (moves_blob()) instead
        of 6-17 MB of JSON lists."""
        self.assign_wcs()
        setups = self.setups
        st0 = self.setup
        ox, oy, oz = st0.to_model([st0.origin_point()])[0]
        out_setups = []
        for st in setups:
            o = st.to_model([st.origin_point()])[0]
            out_setups.append({"name": st.name, "wcs": st.wcs, "rotary": st.rotary, "a": st.a, "orient": st.orient if not st.rotary else None,
                               "axis": list(st.axis) if st.rotary else None, "origin": [float(v) for v in o],
                               "mount": next(i for i, x in enumerate(setups) if x is st or self.same_mounting(x, st)),
                               "clear_z": round(float(st.clearance_z), 4),
                               "tool_axis": list(st.tool_axis_model), "stock": st.model_stock.to_dict(),
                               "max_radius": getattr(st, "max_radius", None), "fixture": st.fixture_payload(),
                               "origin_name": st.origin if isinstance(st.origin, str) else "custom",
                               "size": [round(float(st.stock.xmax - st.stock.xmin), 3), round(float(st.stock.ymax - st.stock.ymin), 3),
                                        round(float(st.stock.zmax - st.stock.zmin), 3)],
                               "safe_z_wcs": round(float(st.safe_z - st.origin_point()[2]), 3),
                               "tiles": [list(t) for t in st.tiles], "tile_wcs": st.tile_wcs,
                               "align": dict(st.align) if st.align else None,
                               "tile_wcs_names": st.tile_wcs_names() if st.tile_wcs else None,
                               "link_z": round(float(st.to_model([(0.0, 0.0, st.clearance_z)])[0][2]), 4)})
        ops = []
        for op in self.ops:
            st = self.setup_of(op)
            si = next(i for i, x in enumerate(setups) if x is st)
            mv = None
            if moves:
                M, A = self._model_moves(op, st)
                mv = [[m[0], round(float(q[0]), 4), round(float(q[1]), 4), round(float(q[2]), 4), m[4]] + ([m[5]] if len(m) > 5 else [])
                      for m, q in zip(op.moves, M)] if op.moves else []
            ops.append({"name": op.name, "kind": op.kind, "color": op.color, "tool": tool_payload(op.tool), "setup": si,
                        "key": op.key, "overrides": op.overrides, "defaults": op.defaults,
                        "cut": {"fields": {k: list(m) for k, m in CUT_FIELDS.get(op.kind, {}).items() if k in op.cut_defaults},
                                "values": op.cut_defaults, "overrides": op.cut_overrides},
                        "color_override": op.color_override,
                        "base_tool": asdict(op.base_tool) if op.base_tool is not None else None,
                        "moves": mv, "n_moves": len(op.moves), "time": round(self.op_minutes(op), 2), "time_one": round(op.time_minutes(self.machine), 2),
                        "tiles": len(self.op_tiles(op)), "tile_offsets": [list(t) for t in st.tiles] if op.tiled and len(st.tiles) > 1 else None,
                        "params": op.params, "uses_a": op.uses_a})
        return {
            "name": self.name, "machine": st0.machine.name,
            "stock": st0.model_stock.to_dict(), "origin": [float(ox), float(oy), float(oz)], "safe_z": st0.safe_z,
            "time": round(self.time_minutes(), 2), "warnings": self.check(), "setups": out_setups, "ops": ops,
            "moves_blob": not moves,
        }

    def _model_moves(self, op, st) -> tuple[np.ndarray, np.ndarray | None]:
        """An op's move end points in model coordinates (n x 3) and, for rotary moves, their A values."""
        moves = op.moves
        if not moves:
            return np.zeros((0, 3)), None
        P = np.array([m[1:4] for m in moves], dtype=float)
        if st.rotary:
            A = np.array([m[5] if len(m) > 5 else st.a for m in moves], dtype=float)
            M = np.empty_like(P)
            for av in np.unique(A):
                sel = A == av
                M[sel] = st.to_model(P[sel], a=float(av))
            return M, A
        return st.to_model(P), None

    MOVES_MAGIC = b"ACMV"

    def moves_blob(self) -> bytes:
        """Every op's moves as one little-endian binary frame for the viewer (the JSON lists were 6.5 MB for the latch,
        17 MB for the obsidian sheet, and parsed on every build and again after the feeds were fitted). Layout:
        magic "ACMV", uint32 version 1, uint32 n_ops, per op [uint32 n, uint32 flags (1 = has A)], then per op
        float32 xyz (n x 3, model coordinates), float32 feed (n) and float32 A (n) if flagged, and last the uint8
        kinds of every op in order (after the floats, so every float array stays 4-byte aligned for typed-array
        views). float32 keeps 7 significant digits: 1e-5 mm at 100 mm, within the 4 decimals the JSON path rounds to."""
        self.assign_wcs()
        head = [self.MOVES_MAGIC, np.array([1, len(self.ops)], dtype="<u4").tobytes()]
        body, kinds = [], []
        for op in self.ops:
            st = self.setup_of(op)
            M, A = self._model_moves(op, st)
            has_a = bool(op.moves) and len(op.moves[0]) > 5
            head.append(np.array([len(op.moves), 1 if has_a else 0], dtype="<u4").tobytes())
            body.append(np.ascontiguousarray(M, dtype="<f4").tobytes())
            body.append(np.array([m[4] for m in op.moves], dtype="<f4").tobytes())
            if has_a:
                body.append(np.array([m[5] for m in op.moves], dtype="<f4").tobytes())
            kinds.append(np.array([m[0] for m in op.moves], dtype=np.uint8).tobytes())
        return b"".join(head + body + kinds)

    @staticmethod
    def moves_from_blob(blob: bytes) -> list[list[list]]:
        """The lists to_payload(moves=True) would carry, decoded from moves_blob() (for clients without binary frames)."""
        if blob[:4] != Program.MOVES_MAGIC:
            raise CamError("not a moves blob")
        ver, n_ops = np.frombuffer(blob, dtype="<u4", count=2, offset=4)
        hdr = np.frombuffer(blob, dtype="<u4", count=2 * int(n_ops), offset=12).reshape(-1, 2)
        pos = 12 + 8 * int(n_ops)
        kpos = pos + sum(int(n) * (20 if flags & 1 else 16) for n, flags in hdr)   # 12 B xyz + 4 B feed (+ 4 B A) a move
        out = []
        for n, flags in hdr:
            n = int(n)
            M = np.frombuffer(blob, dtype="<f4", count=3 * n, offset=pos).reshape(-1, 3).astype(float); pos += 12 * n
            F = np.frombuffer(blob, dtype="<f4", count=n, offset=pos).astype(float); pos += 4 * n
            A = None
            if flags & 1:
                A = np.frombuffer(blob, dtype="<f4", count=n, offset=pos).astype(float); pos += 4 * n
            K = np.frombuffer(blob, dtype=np.uint8, count=n, offset=kpos); kpos += n
            out.append([[int(k), round(float(x), 4), round(float(y), 4), round(float(z), 4), float(f)] + ([float(a)] if A is not None else [])
                        for k, (x, y, z), f, a in zip(K, M, F, A if A is not None else [None] * n)])
        return out

    # ---------------------------------------------------------------- post
    def limit_feeds(self, sim, floor: float = 0.25, margin: float = 2.5, finishing_factor: float = 3.0) -> dict:
        """Material removal rate as part of the calculation: scale every feed move whose MRR (from the simulation's
        per-move numbers) exceeds its reference down to it, never below `floor` x the planned feed. The reference is
        `margin` x the tool's rated MRR (stepdown x stepover width x feed; 2.5x is a full slot at the rated chip load,
        so roughing entries and links are left alone) - and for finishing operations also `finishing_factor` x the
        operation's own average while cutting, because a skim that suddenly removes several times its average has
        found stock the roughing left (the R1 ball on the Z1, 2026-10-22: 195 mm3/min against an average of 38).
        Moves that would need less than the floor are left at the floor and reported: there the path itself must
        change (rough first, approach from above the stock) - a slower feed is not a fix for a cut the tool cannot take.
        Returns {"changed": n, "ops": {name: {"moves": n, "min_factor": f, "beyond": [move numbers]}}}."""
        out: dict = {"changed": 0, "ops": {}}
        per = getattr(sim, "per_move", None) or {}
        for i, op in enumerate(self.ops):
            pm = per.get(i)
            if not pm or op.kind in ("vcarve", "chamfer"):
                continue
            rated = op.tool.rated_mrr() * margin
            if rated <= 0:
                continue
            if op.kind in ("parallel3d", "contour", "rotary_finish", "rotary_profile"):
                b = next((x for x in (getattr(sim, "stats", {}) or {}).get("bites", []) if x.get("op") == op.name), None)
                if b and b.get("mrr_mean"):
                    rated = min(rated, finishing_factor * b["mrr_mean"])
            mrr = pm["mrr"]
            changed, beyond, fmin = 0, [], 1.0
            for k, m in enumerate(op.moves):
                if m[0] == RAPID or k >= len(mrr) or mrr[k] <= rated or not m[4]:
                    continue
                f = rated / float(mrr[k])
                if f < floor:
                    beyond.append(k + 1); f = floor
                m[4] = round(m[4] * f, 1); changed += 1; fmin = min(fmin, f)
            if changed:
                out["ops"][op.name] = {"moves": changed, "min_factor": round(fmin, 3), "beyond": beyond[:50], "beyond_count": len(beyond)}
                out["changed"] += changed
                op.params["feed_limited"] = {"moves": changed, "min_factor": round(fmin, 3), "beyond": len(beyond)}
        return out

    def after_simulation(self, sim) -> dict | None:
        """The default step after a simulation: fit_feeds on the simulated ops not fitted yet (unless auto_fit is off).
        Returns fit_feeds' result when it changed anything (then simulate again for MRR and times at the new feeds)."""
        if not getattr(self, "auto_fit", True):
            return None
        res = self.fit_feeds(sim, skip_fitted=True)
        return res if res["changed"] else None

    def fit_feeds(self, sim, load: float | None = None, max_factor: float = 2.0, floor: float = 0.5, slot: float = 1.0,
                  margin: float = 2.5, finish_factor: float = 1.0, skip_fitted: bool = False, smooth: float | None = None,
                  smooth_rate: float = 1.0, smooth_step: float = 0.02, smooth_mode: str = "mean") -> dict:
        """Feeds that follow the engagement ("constant load"), from a simulation of this program: per level or rising
        feed move, the radial engagement from the move's simulated removal rate and depth (ae = MRR / (ap x feed x Ø))
        sets the feed that keeps the tool's planned chip load - its feed at its own stepover, with radial chip thinning
        (x 1/sqrt(1-(1-2ae)^2) below half the Ø) - scaled by `load` (op "load" override / script value, else this
        argument, else Program.load; 0.5 softer .. 1.5 harder). `slot` < 1 runs a full slot at that fraction of the chip
        load (linear from ae 0.5; the latch job's slots at 737-843 mm3/min showed no spindle load, so 1 by default). Capped at `max_factor` x the planned feed, the machine's max feed and
        `margin` x the tool's rated MRR x load (limit_feeds' slot allowance); never below `floor` x the planned feed.
        Moves that cut nothing run at the cap; ramps, plunges and helices (anything descending) keep their feeds;
        finishing contours (contour finish=) are not sped up beyond `finish_factor` (wall finish, deflection). Ops with
        an explicit per-op feed override, probes, drills and V / chamfer cuts are left alone. Run it on a fresh
        simulation (the removal rates are at the feeds simulated; a second call with the same result reuses them);
        skip_fitted: leave ops an earlier fit already set. Smoothing (no bumping between neighbouring moves): along each
        run of fitted moves the feed is averaged over `smooth` mm of travel (default the tool Ø; 0 = off) and held
        under the rated-MRR limit of any heavy move in that window (it slows before a heavy corner, not on it), rises
        or falls by at most `smooth_rate` x the planned feed per mm and moves in steps of `smooth_step` x the planned
        feed (latch job: 45,000 -> 2,000 feed changes over 10 %, MRR peaks 830-870 -> ~740, 1.9 min faster).
        Returns {"changed": n, "ops": {name: {"moves", "load", "min", "max" (factors), "minutes_before", "minutes_after"}}}."""
        per = getattr(sim, "per_move", None) or {}
        at_sim = getattr(sim, "_feeds_at_sim", None)
        if at_sim is None:
            at_sim = {i: [m[4] for m in op.moves] for i, op in enumerate(self.ops)}
            try:
                sim._feeds_at_sim = at_sim
            except Exception:  # noqa: BLE001 - a result that takes no attributes: one call per simulation
                pass
        mx = self.machine.max_feed or {}
        fmax = min(mx.get("x", 1e9), mx.get("y", 1e9))
        rct = lambda a: 1.0 / math.sqrt(1.0 - (1.0 - 2.0 * a) ** 2) if 0 < a < 0.5 else 1.0
        out: dict = {"changed": 0, "ops": {}}
        for i, op in enumerate(self.ops):
            pm = per.get(i)
            if not pm or not op.moves or op.kind in FIT_SKIP_KINDS or "feed" in (op.overrides or {}) or (
                    skip_fitted and "feed_fit" in (op.params or {})):
                continue
            t = op.tool
            F0 = min(t.feed, fmax)
            if F0 <= 0 or t.diameter <= 0:
                continue
            ld = float((op.params or {}).get("load") or (load if load is not None else getattr(self, "load", 1.0)) or 1.0)
            so = min(max(t.stepover, 0.01), 1.0)
            rated = t.rated_mrr() * margin * ld
            cap = min(max_factor * F0, fmax)
            if (op.params or {}).get("finish") and op.kind == "contour":
                cap = min(cap, finish_factor * F0 * max(ld, 1e-3))
            mrr, ap = pm.get("mrr"), pm.get("ap")
            fs = at_sim.get(i) or [m[4] for m in op.moves]
            fin_mask = np.zeros(len(op.moves), dtype=bool)                  # pocket finishing passes: no faster than planned
            for a_, b_ in (op.params or {}).get("finish_moves") or []:
                fin_mask[a_:b_] = True
            fin_cap = finish_factor * F0 * max(ld, 1e-3)
            t_before = op.time_minutes(self.machine)
            n, fmin, fmax_seen = 0, math.inf, 0.0
            fit: dict[int, float] = {}
            hard: dict[int, float] = {}                                     # the rated-MRR limit of the heavy moves
            for k in range(1, min(len(op.moves), len(mrr))):
                m, prev = op.moves[k], op.moves[k - 1]
                if m[0] != FEED or not m[4] or m[3] < prev[3] - 1e-6 or k >= len(fs) or not fs[k]:
                    continue
                if mrr[k] <= 1e-3 or ap[k] <= 1e-3:
                    f = cap                                          # cuts nothing
                else:
                    ae_mm = float(mrr[k]) / (float(ap[k]) * fs[k])
                    ae = min(max(ae_mm / t.diameter, 0.005), 1.0)
                    der = 1.0 - (1.0 - slot) * min(max((ae - 0.5) / 0.5, 0.0), 1.0)
                    f = ld * F0 * rct(ae) / rct(so) * der
                    if rated > 0:
                        f = min(f, rated / (float(ap[k]) * ae_mm))
                        hard[k] = max(rated / (float(ap[k]) * ae_mm), floor * F0)
                    f = min(max(f, floor * F0), cap)
                if fin_mask[k]:
                    f = min(f, fin_cap)
                fit[k] = f
            sw = t.diameter if smooth is None else float(smooth)
            if sw > 0 and fit:
                fit = _smooth_feeds(op.moves, fit, sw, smooth_rate * F0, smooth_step * F0, floor * F0, hard, smooth_mode)
            for k, f in fit.items():
                m = op.moves[k]
                f = round(f, 1)
                if abs(f - m[4]) > 0.05:
                    m[4] = f; n += 1
                fmin, fmax_seen = min(fmin, f / F0), max(fmax_seen, f / F0)
            if n:
                out["ops"][op.name] = {"moves": n, "load": ld, "min": round(fmin, 2), "max": round(fmax_seen, 2),
                                       "minutes_before": round(t_before, 2), "minutes_after": round(op.time_minutes(self.machine), 2)}
                out["changed"] += n
                op.params["feed_fit"] = {"moves": n, "load": ld, "min": round(fmin, 2), "max": round(fmax_seen, 2)}
        self.feeds_fitted = {"changed": out["changed"], "ops": len(out["ops"])}
        return out

    def gcode(self) -> str:
        post = self.setup.machine.post
        if post == "makera":
            return post_makera(self)
        if post in ("haas", "linuxcnc"):
            return _emit(self, post)
        return post_grbl(self)


def _smooth_feeds(moves, fit: dict, window: float, rate: float, step: float, lo: float, hard: dict | None = None,
                  mode: str = "mean") -> dict:
    """fit_feeds' smoothing, along each run of consecutive fitted moves: the fitted feed averaged over +-window/2 of
    travel ("mean"; "min": the lowest there), never above the rated-MRR limit of a heavy move within that window (so
    it slows down before a heavy stretch, not on it), then a slope limit (`rate` mm/min per mm, both ways) and steps
    of `step` mm/min (rounded down, >= lo)."""
    hard = hard or {}
    ks = sorted(fit)
    runs, cur = [], []
    for k in ks:
        if cur and k != cur[-1] + 1:
            runs.append(cur); cur = []
        cur.append(k)
    if cur:
        runs.append(cur)
    out = {}
    for run in runs:
        d = np.array([math.dist(moves[k - 1][1:4], moves[k][1:4]) for k in run])
        f = np.array([fit[k] for k in run], dtype=float)
        hc = np.array([hard.get(k, np.inf) for k in run], dtype=float)
        mid = np.cumsum(d) - d / 2                                  # the middle of each move along the run
        g = f.copy()
        lo_i = np.searchsorted(mid, mid - window / 2, side="left")
        hi_i = np.searchsorted(mid, mid + window / 2, side="right")
        cw = np.concatenate([[0.0], np.cumsum(f * d)]); cd = np.concatenate([[0.0], np.cumsum(d)])
        for j in range(len(run)):
            a_, b_ = lo_i[j], max(hi_i[j], j + 1)
            if mode == "min":
                g[j] = f[a_:b_].min()
            else:
                g[j] = (cw[b_] - cw[a_]) / max(cd[b_] - cd[a_], 1e-9)
            g[j] = min(g[j], hc[a_:b_].min())
        for j in range(1, len(run)):                                # rise at most `rate` per mm
            g[j] = min(g[j], g[j - 1] + rate * d[j])
        for j in range(len(run) - 2, -1, -1):                       # and come down ahead of a slower stretch
            g[j] = min(g[j], g[j + 1] + rate * d[j + 1])
        if step > 0:                                                # steps (nearest), never over a heavy move's limit
            q = np.round(g / step) * step
            q = np.where(q > hc + 1e-6, np.floor(g / step + 1e-6) * step, q)
            g = np.maximum(q, np.minimum(lo, f))
        for k, v in zip(run, g):
            out[k] = float(v)
    return out


def _circle3(p1, p2, p3):
    """Circle through three points -> (cx, cy, r) or None if collinear."""
    ax, ay = p1; bx, by = p2; cx, cy = p3
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        return None
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    return ux, uy, math.hypot(ax - ux, ay - uy)


def _arc_ok(pts, tol, max_sweep: float = math.pi):
    """Do these XY points (>=3) lie on one arc within tol, with fine enough chords and consistent turning, sweeping
    at most max_sweep radians? Returns (cx, cy, cw) or None."""
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
    if sweep > max_sweep - 2e-3:                     # a margin: the posted (rounded) R / end point can add ~0.06°
        return None
    return cx, cy, sign < 0      # cw = clockwise (negative cross) in G17


def _arc_is_noise(pts, r, tol) -> bool:
    """A fitted 'arc' that is really a straight run with rounding noise: its radius dwarfs its chord, or every point is
    within a tenth of the tolerance of the chord. Judged on the finished candidate, never on a 3-point seed (a true arc's
    first few points are nearly straight too)."""
    chord = math.dist(pts[0], pts[-1])
    if chord < 1e-9:
        return False
    if r > 50.0 * chord:
        return True
    ux, uy = pts[-1][0] - pts[0][0], pts[-1][1] - pts[0][1]
    return all(abs((p[0] - pts[0][0]) * uy - (p[1] - pts[0][1]) * ux) / chord <= tol / 10 for p in pts)


def _merge_collinear(pts, tol):
    """Drop interior points that the straight move replacing them passes within tol of - every dropped point is tested
    against the chord that is actually emitted (anchor -> candidate end), so the error never creeps along a run; points
    must also lie between the chord's ends (no reversal)."""
    return _merge_chord(pts, tol, 2)


def _merge_chord(pts, tol, dims):
    """Greedy chords: from each kept point, the farthest point whose chord has every point in between within `tol`
    of it and between its ends. The farthest end is found by galloping (2, 4, 8 ... points ahead) then bisecting, each
    candidate checked against all the points it skips at once, so a long straight run of tiny segments costs
    O(n log n) rather than re-checking the whole run for every point added (which took hours on rest passes)."""
    n = len(pts)
    if n < 3:
        return list(pts)
    P = np.asarray([p[:dims] for p in pts], dtype=float)
    tol2 = tol * tol

    def ok(a, c):
        v = P[c] - P[a]
        ln = math.sqrt(float(v @ v))
        if ln < 1e-12:
            return False
        if c - a < 2:
            return True
        W = P[a + 1:c] - P[a]
        along = W @ v / ln
        if along.min() < -1e-9 or along.max() > ln + 1e-9:
            return False
        return bool(((W * W).sum(axis=1) - along * along).max() <= tol2)

    out = [pts[0]]
    a = 0
    while a < n - 1:
        c, step, bad = a + 1, 1, None
        while c < n - 1:
            t = min(c + step, n - 1)
            if ok(a, t):
                c, step = t, step * 2
            else:
                bad = t
                break
        if bad is not None:
            while bad - c > 1:
                mid = (c + bad) // 2
                if ok(a, mid):
                    c = mid
                else:
                    bad = mid
        out.append(pts[c])
        a = c
    return out


def _z_linear(pts3, tol):
    """Is z linear in the distance along these XYZ points (a flat arc, or a helix / ramp)?"""
    s = [0.0]
    for a, b in zip(pts3, pts3[1:]):
        s.append(s[-1] + math.dist(a[:2], b[:2]))
    if s[-1] < 1e-12:
        return False
    z0, z1 = pts3[0][2], pts3[-1][2]
    return all(abs(p[2] - (z0 + (z1 - z0) * si / s[-1])) <= tol for p, si in zip(pts3, s))


def _merge_collinear3(pts, tol):
    """XYZ version of _merge_collinear: ramps stay one line, a tab top plus its exit ramp never merge into one slope."""
    return _merge_chord(pts, tol, 3)


def _with_arcs(moves, tol, max_sweep: float = math.pi, helical: bool = True):
    """Yield moves, replacing runs of feed moves that lie on a circle — flat, or (helical) with z linear along the
    arc (ramp entries) — with ('arc', x, y, z, f, cx, cy, cw); straight stretches are merged into single lines.
    Arcs sweep at most max_sweep radians."""
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
        # candidate run: feed moves at one feed rate, starting from the previous position
        stop_point()
        f = m[4]
        j = i
        while j < n and moves[j][0] != RAPID and moves[j][4] == f:
            j += 1
        run = [(moves[i - 1][1], moves[i - 1][2], moves[i - 1][3])] + [(mv[1], mv[2], mv[3]) for mv in moves[i:j]]
        # greedy arcs on the RAW run first (collinear merging would coarsen fine arc chords past the
        # sagitta test); straight leftovers are merged afterwards
        items: list[tuple] = []
        xy = [q[:2] for q in run]
        k = 0   # index into run (run[0] is the start position, already emitted)

        def ok(e):                                   # the candidate run[k:e] as one arc
            res = _arc_ok(xy[k:e], tol, max_sweep)
            if res is None or not _z_linear(run[k:e], tol) or (not helical and abs(run[e - 1][2] - run[k][2]) > 1e-9):
                return None
            return res
        while k < len(run) - 1:
            # the longest arc from k: doubling steps out while the candidate still fits, then bisecting back to the
            # last that does (each test is O(points), so a long arc costs O(n log n) rather than O(n^2) tests: the
            # latch job's post spent 14 s testing every prefix of every arc one point at a time)
            best = None
            e = k + 3
            if e <= len(run) and (res := ok(e)) is not None:
                best, step = (e, res), 1
                while best[0] < len(run):
                    e2 = min(best[0] + step, len(run))
                    res2 = ok(e2)
                    if res2 is not None:
                        best, step = (e2, res2), step * 2
                        continue
                    lo, hi = best[0], e2
                    while hi - lo > 1:
                        mid = (lo + hi) // 2
                        rm = ok(mid)
                        if rm is None:
                            hi = mid
                        else:
                            lo, best = mid, (mid, rm)
                    break
            if best is not None:
                e_, (cx_, cy_, _) = best
                r_ = math.hypot(run[k][0] - cx_, run[k][1] - cy_)
                if _arc_is_noise([q[:2] for q in run[k:e_]], r_, tol):
                    best = None                                            # a straight run: merged into a line below
            if best is not None:
                e, (cx, cy, cw) = best
                x, y, z = run[e - 1]
                items.append(("arc", x, y, z, f, cx, cy, cw))
                k = e - 1
            else:
                x, y, z = run[k + 1]
                items.append((FEED, x, y, z, f))
                k += 1
        # merge straight stretches (in 3D, so a straight ramp is one line)
        start = run[0]
        pending: list[tuple[float, float, float]] = []
        for it in items:
            if it[0] == FEED:
                pending.append((it[1], it[2], it[3]))
                continue
            for x, y, z in _merge_collinear3([start] + pending, tol * 0.5)[1:] if pending else []:
                yield (FEED, x, y, z, f)
            pending = []
            yield it
            start = (it[1], it[2], it[3])
        for x, y, z in _merge_collinear3([start] + pending, tol * 0.5)[1:] if pending else []:
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
MAKERA_MAX_LINE = CONTROLLERS["makera"]["max_line"]
# finishing cuts (and anything with a V tool) are posted to this tolerance; roughing to the machine's arc_tolerance
FINE_POST_TOL = 0.005
FINE_POST_KINDS = {"parallel3d", "vcarve", "chamfer", "rotary_finish", "rotary_profile", "gear_teeth"}
MAKERA_CLEAR_Z = -3.0       # machine Z of the firmware's clearance position (coordinate.clearance_z, Z1 and Carvera)


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
    caps = m0.capabilities()
    max_line = caps["max_line"]
    out: list[str] = []
    g93 = False                                        # inverse-time feed mode is active (fan dialects, rotary moves)
    if mk and any(st.tile_wcs and len(st.tiles) > 1 for st in prog.setups):
        raise CamError(MAKERA_TILE_WCS)

    def comment(text: str):
        text = text.replace("(", "[").replace(")", "]")
        if mk:                                          # the dispatcher hoists G90/G91 even out of comments
            text = text.replace("G90", "G 90").replace("G91", "G 91")
        c = "(" + text.replace(";", ",") + ")" if fan else "; " + text
        out.append(c[:max_line] if max_line and caps["comments_count"] else c)

    def line(t: str):
        if max_line and len(t) > max_line:
            raise CamError(f"G-code line longer than {max_line} characters for the {caps['controller']} controller: {t}")
        out.append(t)

    def home_z():
        """Retract to the machine's Z home (Fanuc family) or the setup's clearance height."""
        return "G53 G0 Z0." if hs else "G53 G0 Z0"

    def lift(clear_wcs: float):
        """Go UP to a height that is safe anywhere on the table, without moving X/Y: machine coordinates where the
        controller has them (Makera: the firmware's clearance Z, -3; Fanuc family: Z home), else the setup's clearance."""
        if mk:
            line(f"G53 G0 Z{_fmt(MAKERA_CLEAR_Z)}")
        elif fan:
            line(home_z())
        else:
            line(f"G0 Z{_fmt(clear_wcs)}")

    axf = None                                         # the current setup's align transform (work XY -> machine work XY)

    def first_xy(op, ox, oy):
        for mv in op.moves:
            x, y = mv[1] - ox, mv[2] - oy
            return axf(x, y) if axf else (x, y)
        return None

    def approach(op, ox, oy, clear_wcs):
        """Before the spindle starts: up first, then over the operation's first point. Nothing may come down in Z
        while X/Y is wherever the head was parked - a Makera parks at its clearance spot right over the tool setter."""
        lift(clear_wcs)
        p = first_xy(op, ox, oy)
        if p is not None:
            line(f"G0 X{_fmt(p[0])} Y{_fmt(p[1])}")
        return p

    if fan:
        line("%")
        if hs:
            pname = "".join(ch for ch in prog.name.upper() if ch.isalnum() or ch in " -_")[:30].strip() or "AGENTICCAD"
            line(f"O01000 ({pname})")
    comment(f"AgenticCAD {dialect} post: {prog.name}")
    comment(f"machine: {m0.name}")
    comment(f"est. {prog.time_minutes():.1f} min, {len(prog.setups)} setup(s)")
    for _st in prog.setups:
        if len(_st.tiles) > 1:
            if _st.tile_wcs:
                comment(f"{_st.name}: {len(_st.tiles)} tiles, each in its own work offset - set them before running:")
                for _k, (_n, _t) in enumerate(zip(_st.tile_wcs_names(), _st.tile_offsets())):
                    comment(f"  {_n}: zero it as usual (the first tile)" if _k == 0 and not any(_t) else
                            f"  {_n} = {_st.wcs} + X{_fmt(_t[0])} Y{_fmt(_t[1])} Z{_fmt(_t[2])}")
            else:
                comment(f"{_st.name}: {len(_st.tiles)} tiles, all in {_st.wcs}; every operation runs at each tile")
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
    a_shift = 0.0            # whole turns the controller has dropped from A (Makera `G92.4 A0 S0` after a wound-up op)
    aw0 = (m0.rotary or {}).get("axis", "A")

    def a_out(a: float) -> float:
        return a - a_shift

    def a_shrink():
        """Makera only: drop the whole turns A has wound up (the firmware's G92.4 ... S0 keeps the work angle modulo 360),
        so the next index is a short move and the controller's A stays small. Nothing moves."""
        nonlocal a_shift
        if not mk:
            return
        cur = a_out(cur_a)
        if abs(cur) >= 360.0 - 1e-6:
            line(f"G92.4 {aw0}0 S0")
            a_shift += cur - math.fmod(cur, 360.0)

    def a_index(target: float, clear_wcs: float):
        """Turn A to `target` (an angle; the nearest equivalent turn is used) with the tool lifted first."""
        nonlocal cur_a
        eq = target + 360.0 * round((cur_a - target) / 360.0)
        if abs(eq - cur_a) < 1e-6:
            cur_a = eq
            return
        lift(clear_wcs)
        a_shrink()
        line(f"G0 {aw0}{_fmt(a_out(eq))}")
        cur_a = eq
    last: list = [None, None, None, None]
    spindle_on = False
    active_wcs = None                                  # the work offset the controller is in (tiles may change it)
    for op in prog.ops:
        st = prog.setup_of(op)
        m = m0
        ox, oy, oz = st.origin_point()
        axf = st.align_xy()
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
                    sets = _probe_sets(prog, st)
                    if sets:
                        comment(f"the probe ops then set {'/'.join(sets).upper()}: a zero within "
                                f"{min(o.params['search'] for o in prog.ops if prog.setup_of(o) is st and o.kind == 'probe'):g} mm will do")
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
                active_wcs = st.wcs
                if prev_setup is not None:
                    lift(clear)
            if st.rotary:
                if prev_setup is not None and Program.same_mounting(prev_setup, st):
                    a_index(st.a, clear)                   # same chuck: the shortest turn to this setup's angle
                else:
                    a_shrink()
                    cur_a = st.a                           # a fresh mounting: the operator zeroed A
                    line(f"G0 {aw}{_fmt(a_out(cur_a))}")
            else:
                cur_a = st.a
            prev_setup = st
            if axf:
                comment(f"{st.name}: aligned as measured - XY turned {st.align.get('rot_deg', 0) or 0:g} deg about the work "
                        f"zero, then shifted X{_fmt(st.align.get('dx', 0) or 0)} Y{_fmt(st.align.get('dy', 0) or 0)}")
            last = [None, None, None, cur_a]               # positions restart in the new setup's frame
            last_f = None
        t = op.tool
        rpm = min(max(t.rpm, m.spindle["min"]), m.spindle["max"])
        offs = prog.op_tiles(op)
        wnames = st.tile_wcs_names() if st.tile_wcs and len(offs) > 1 else None
        # the first tile's origin for the approach (tiles shift the moves; tile_wcs runs each tile in its own WCS)
        aox, aoy = (ox, oy) if wnames else (ox - offs[0][0], oy - offs[0][1])
        want0 = wnames[0] if wnames else st.wcs
        if active_wcs is not None and want0 != active_wcs:     # back from the last tile's work offset: up first, then switch
            lift(clear)
            line(want0)
            active_wcs = want0
            last = [None, None, None, last[3]]
        comment(f"{op.name} [{op.kind}] {t.label()} D{_fmt(t.diameter)}" + (f" x{len(offs)} tiles" if len(offs) > 1 else ""))
        if st.rotary and not op.uses_a:
            a_index(st.a, clear)                           # a 3+1 op after a continuous one: back to the setup's angle
            last[3] = cur_a
        if op.kind == "probe":
            # the 3D probe: a tool change to it (the firmware measures it like any tool) and NEVER the spindle
            if not mk:
                raise CamError(f"{op.name}: probe ops are posted for the Makera firmware only, not the {dialect} post")
            if spindle_on or (cur_tool is not None and cur_tool != t.number):
                line("M5")
            spindle_on = False
            if cur_tool != t.number:
                line(f"M6 T{t.number}")
                cur_tool = t.number
            approach(op, aox, aoy, clear)
            comment(f"PROBE BEGIN {op.name}")
            for pl in _probe_gcode(op, ox, oy, oz, safe, axf):
                line(pl)
            comment("PROBE END")
            last = [None, None, None, cur_a]
            last_f = None
            continue
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
                p = approach(op, aox, aoy, clear)
                line(f"S{int(rpm)} M3")
                spindle_on = True
                line(f"G43 H{t.number} Z{_fmt(safe)}")          # length offset of this tool, applied on the first Z move
                if m.coolant:
                    line("M8")
                cur_tool = t.number
                last = [p[0] if p else None, p[1] if p else None, safe, cur_a]
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
                p = approach(op, aox, aoy, clear)
                line(f"M3 S{int(rpm)}")
                spindle_on = True
                if m.coolant and not mk:
                    line("M8")
                line(f"G0 Z{_fmt(safe)}")
                cur_tool = t.number
                last = [p[0] if p else None, p[1] if p else None, safe, cur_a]
                last_f = None
        elif not spindle_on:                              # same tool after an operator pause: restart the spindle
            p = approach(op, aox, aoy, clear)
            line(f"M3 S{int(rpm)}")
            spindle_on = True
            line(f"G0 Z{_fmt(safe)}")
            last = [p[0] if p else None, p[1] if p else None, safe, cur_a]
            last_f = None
        use_arcs = caps["arcs"] and not op.uses_a
        moves = op.moves
        tol_op = op.post_tol if op.post_tol else (FINE_POST_TOL if op.kind in FINE_POST_KINDS or t.type in ("vbit", "chamfer")
                                                  else m.arc_tolerance)
        items = list(_with_arcs(moves, tol_op if use_arcs else None, math.radians(caps["max_arc_sweep"]), caps["helical_arcs"]))
        base_o = (ox, oy, oz)
        for ti, (tdx, tdy, tdz) in enumerate(offs):
          # ---- tile ti: the op's moves again, shifted (or in the tile's own work offset); between tiles: up, over
          if len(offs) > 1:
              comment(f"tile {ti + 1}/{len(offs)}" + (f" {wnames[ti]}" if wnames else f" at {_fmt(tdx)} {_fmt(tdy)}"))
          if ti:
              lift(clear)
              if wnames:
                  line(wnames[ti])
                  active_wcs = wnames[ti]
              ox, oy, oz = base_o if wnames else (base_o[0] - tdx, base_o[1] - tdy, base_o[2] - tdz)
              p0 = first_xy(op, ox, oy)
              if p0 is not None:
                  line(f"G0 X{_fmt(p0[0])} Y{_fmt(p0[1])}")
              last = [p0[0] if p0 else None, p0[1] if p0 else None, None, last[3]]
          else:
              ox, oy, oz = base_o if wnames else (base_o[0] - tdx, base_o[1] - tdy, base_o[2] - tdz)
          for item in items:
            if item[0] == "arc":
                _, x, y, z, f, cx, cy, cw = item
                x -= ox; y -= oy; z -= oz; cx -= ox; cy -= oy
                if axf:                                    # the measured alignment: end point and centre turn together
                    (x, y), (cx, cy) = axf(x, y), axf(cx, cy)
                sx = last[0] if last[0] is not None else x; sy = last[1] if last[1] is not None else y
                # the centre goes on the chord's perpendicular bisector, so the start and end radii are equal (GRBL
                # rejects I/J arcs whose radii differ by > 0.005 mm; an R arc is defined by that radius)
                mx, my = (sx + x) / 2, (sy + y) / 2
                ux, uy = -(y - sy), x - sx
                ul = math.hypot(ux, uy)
                if ul > 1e-9:
                    ux, uy = ux / ul, uy / ul
                    tpar = (cx - mx) * ux + (cy - my) * uy
                    cx, cy = mx + ux * tpar, my + uy * tpar
                fw = ""
                if f and f != last_f:
                    fw = f" F{_fmt(f)}"; last_f = f
                zw = f" Z{_fmt(z)}" if last[2] is None or _fmt(z) != _fmt(last[2]) else ""
                if caps["arc_format"] == "r":
                    line(f"{'G2' if cw else 'G3'} X{_fmt(x)} Y{_fmt(y)}{zw} R{_fmt(math.hypot(sx - cx, sy - cy))}{fw}")
                else:
                    line(f"{'G2' if cw else 'G3'} X{_fmt(x)} Y{_fmt(y)}{zw} I{_fmt(cx - sx)} J{_fmt(cy - sy)}{fw}")
                last = [x, y, z, last[3]]
                continue
            k, x, y, z, f = item[:5]
            a = item[5] if len(item) > 5 else None
            x -= ox; y -= oy; z -= oz
            if axf:
                x, y = axf(x, y)
            words = []
            # a word only when the value the controller reads changes (sub-micron jitter between moves is not a move)
            if last[0] is None or _fmt(x) != _fmt(last[0]): words.append(f"X{_fmt(x)}")
            if last[1] is None or _fmt(y) != _fmt(last[1]): words.append(f"Y{_fmt(y)}")
            if last[2] is None or _fmt(z) != _fmt(last[2]): words.append(f"Z{_fmt(z)}")
            da = 0.0
            if a is not None and (last[3] is None or abs(a - last[3]) > 1e-6):
                da = a - (last[3] if last[3] is not None else a)
                words.append(f"{aw}{_fmt(a_out(a))}")
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
        ox, oy, oz = base_o
        if op.uses_a:
            cur_a = last[3]
            if abs(a_out(cur_a)) >= 360.0:
                a_shrink()
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
from cam_rotary import gear_teeth, radial_map, rotary_finish, rotary_profile, rotary_rough, rotary_wrap  # noqa: E402,F401


# ---------------------------------------------------------------- per-operation overrides
# The CAM tab writes one line at the top of cam.py:  overrides({"Pocket": {"feed": 500}, "Face": {"rpm": 11000}})
# Every operation created afterwards looks itself up by key (its name, "#2", "#3"… for repeats) and runs with those
# values instead of the script's: tool values (rpm, feed, plunge, stepdown, stepover), cut settings (CUT_FIELDS, by
# operation kind: depths, sides, tabs, stock to leave, ...) and the display colour ("color": "#rrggbb", any operation,
# script-built ones too). Everything else comes from the tool and the script.
OVERRIDE_FIELDS = ("rpm", "feed", "plunge", "stepdown", "stepover")
# cut settings the CAM tab may change, per operation kind: field -> (label, unit, type, choices or None)
_Z = {"z_top": ("Top Z", "mm", "num", None), "z_bottom": ("Bottom Z", "mm", "num", None)}
_LEAVE = {"stock_to_leave": ("Stock to leave", "mm", "num", None)}
_CLIMB = {"climb": ("Climb milling", "", "bool", None)}
CUT_FIELDS = {
    "face": {**_Z, "angle": ("Pass angle", "°", "num", None), "one_way": ("One way (uniform lay)", "", "bool", None),
             "finish": ("Finishing pass (one way)", "", "bool", None), "finish_stepover": ("Finish stepover", "xØ", "num", None)},
    "contour": {**_Z, "side": ("Side", "", "choice", ["outside", "inside", "on"]), **_LEAVE, "tabs": ("Tabs", "", "int", None),
                "tab_height": ("Tab height", "mm", "num", None), "tab_width": ("Tab width", "mm", "num", None), **_CLIMB,
                "lead": ("Lead-in radius", "mm", "num", None), "ramp": ("Ramp angle", "°", "num", None),
                "tab_ramp": ("Tab ramp angle", "°", "num", None), "finish": ("Finish pass (full depth)", "", "bool", None)},
    "pocket": {**_Z, **_LEAVE, "finish": ("Finishing passes (floor + walls)", "", "bool", None), **_CLIMB, "ramp": ("Ramp angle", "°", "num", None),
               "strategy": ("Strategy", "", "choice", ["auto", "rings", "bore", "adaptive"]),
               "finish_pattern": ("Floor finish pattern", "", "choice", ["parallel", "offset", "spiral"]),
               "finish_angle": ("Floor finish angle", "°", "num", None), "finish_stepover": ("Floor finish stepover", "xØ", "num", None),
               "finish_wall": ("Wall allowance for the finish", "mm", "num", None),
               "finish_floor": ("Floor allowance for the finish", "mm", "num", None)},
    "adaptive": {**_Z, **_LEAVE, **_CLIMB, "helix_diameter": ("Helix diameter", "mm", "num", None),
                 "stay_down": ("Stay down between areas", "", "bool", None), "lift": ("Lift for linking", "mm", "num", None),
                 "hop": ("Hop height", "mm", "num", None), "chip_thinning": ("Chip-thinning feed", "", "bool", None)},
    "drill": {**_Z, "peck": ("Peck depth", "mm", "num", None), "retract": ("Retract", "mm", "num", None)},
    "parallel3d": {**_Z, "angle": ("Pass angle", "°", "num", None), "resolution": ("Resolution", "mm", "num", None),
                   "pattern": ("Pattern", "", "choice", ["parallel", "radial", "spiral"]), "one_way": ("One way (uniform lay)", "", "bool", None)},
    "chamfer": {"width": ("Chamfer width", "mm", "num", None), "tip_below": ("Tip below the chamfer", "mm", "num", None), **_CLIMB},
    "vcarve": {"max_depth": ("Max depth", "mm", "num", None), "flat_stepover": ("Flat-bottom stepover", "mm", "num", None)},
    "rotary_profile": {"leave": ("Stock to leave", "mm", "num", None), "res_a": ("A step", "°", "num", None)},
    "gear_teeth": {"module": ("Module", "mm", "num", None), "teeth": ("Teeth", "", "int", None), "width": ("Face width", "mm", "num", None),
                   "leave": ("Stock to leave (root)", "mm", "num", None), "helix": ("Helix angle", "°", "num", None),
                   "side": ("Cutter side", "", "choice", ["+y", "-y"]), "backlash": ("Backlash (extra depth)", "mm", "num", None),
                   **_CLIMB},
}
_LOAD = ("Load (feed fit)", "x", "num", None)   # fit_feeds' chip load scale for this op: 0.5 softer .. 1.5 harder (blank: program's)
LOAD_RANGE = (0.3, 2.0)
for _k in ("face", "contour", "pocket", "adaptive", "parallel3d", "rotary_rough", "rotary_finish", "rotary_profile"):
    CUT_FIELDS.setdefault(_k, {})["load"] = _LOAD
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
OP_FIELDS = {"face": OVERRIDE_FIELDS, "pocket": OVERRIDE_FIELDS, "adaptive": OVERRIDE_FIELDS,
             "contour": ("rpm", "feed", "plunge", "stepdown"), "drill": ("rpm", "plunge"),
             "parallel3d": ("rpm", "feed", "plunge", "stepover"), "rotary_rough": OVERRIDE_FIELDS,
             "rotary_finish": ("rpm", "feed", "plunge", "stepover"), "rotary_wrap": OVERRIDE_FIELDS,
             "rotary_profile": OVERRIDE_FIELDS, "gear_teeth": ("rpm", "feed", "plunge", "stepdown"),
             "chamfer": ("rpm", "feed", "plunge", "stepdown"), "vcarve": ("rpm", "feed", "plunge", "stepdown")}
_OV: dict[str, Any] = {"table": {}, "seen": {}, "depth": 0, "used": set(), "rough": [], "floors": []}


def overrides(table: dict | None = None) -> None:
    """Per-operation tool overrides, keyed by operation name ("Pocket", or "Pocket#2" for the second op with that name):
    {"Pocket": {"rpm": 11000, "feed": 600, "plunge": 150, "stepdown": 0.8, "stepover": 0.3}} (stepover = fraction of Ø).
    "load": 0.5 .. 1.5 scales the chip load Program.fit_feeds holds for that op (softer / harder; a "feed" wins).
    Written by the CAM tab; put it before the operations. Values not given come from the tool / the script."""
    bad = []
    cut_all: dict[str, tuple] = {}
    for d in CUT_FIELDS.values():
        for f_, m in d.items():
            if f_ in cut_all and m[2] == "choice" and cut_all[f_][2] == "choice":     # a field several kinds share (contour /
                cut_all[f_] = (*cut_all[f_][:3], list(cut_all[f_][3]) + [c for c in m[3] if c not in cut_all[f_][3]])   # gear side)
            else:
                cut_all.setdefault(f_, m)
    for k, v in (table or {}).items():
        if not isinstance(v, dict):
            raise CamError(f"overrides: '{k}' must map to a dict of {OVERRIDE_FIELDS}, cut settings or color")
        for f_, x in v.items():
            if f_ == "color":
                if not isinstance(x, str) or not COLOR_RE.match(x):
                    raise CamError(f"overrides: {k}.color = {x!r} (use '#rrggbb')")
            elif f_ in OVERRIDE_FIELDS:
                if isinstance(x, bool) or not isinstance(x, (int, float)) or x <= 0 or (f_ == "stepover" and x > 1):
                    raise CamError(f"overrides: {k}.{f_} = {x!r} (must be > 0{'; stepover is a fraction of Ø ≤ 1' if f_ == 'stepover' else ''})")
            elif f_ == "load":
                if isinstance(x, bool) or not isinstance(x, (int, float)) or not (LOAD_RANGE[0] <= x <= LOAD_RANGE[1]):
                    raise CamError(f"overrides: {k}.load = {x!r} (the feed fit's chip load scale, {LOAD_RANGE[0]:g} .. {LOAD_RANGE[1]:g}; 1 = the tool's)")
            elif f_ in cut_all:
                _, _, typ, choices = cut_all[f_]
                ok = (isinstance(x, bool) if typ == "bool" else x in choices if typ == "choice"
                      else isinstance(x, int) and not isinstance(x, bool) and x >= 0 if typ == "int"
                      else isinstance(x, (int, float)) and not isinstance(x, bool))
                if not ok:
                    raise CamError(f"overrides: {k}.{f_} = {x!r} is not a valid {cut_all[f_][0].lower()}"
                                   + (f" (one of {choices})" if choices else ""))
            else:
                bad.append(f"{k}.{f_}")
    if bad:
        raise CamError(f"overrides: unknown field(s) {bad}; use {OVERRIDE_FIELDS}, cut settings ({sorted(cut_all)}) or color")
    _OV["table"] = {k: dict(v) for k, v in (table or {}).items()}


def reset_overrides() -> None:
    """Start of a CAM script run: no overrides, op keys numbered from 1 again."""
    _OV.update(table={}, seen={}, depth=0, used=set(), rough=[], floors=[])
    _FIXTURES.update(table={})


_FIXTURES: dict = {"table": {}}


MAKERA_TILE_WCS = ("tile_wcs: not on a Makera: its firmware makes the fitted tool the length reference whenever a work "
                   "offset's Z is set (G10 L2 Pn Z), and only G54 survives a restart, so G55..G59 can't be set safely mid-job. "
                   "Leave tile_wcs off: the tiles run in G54 with offsets in the program (the same file size - there are no "
                   "subprograms either way)")


def _norm_tiles(tiles) -> list[tuple[float, float, float]]:
    if tiles is None:
        return [(0.0, 0.0, 0.0)]
    if hasattr(tiles, "offsets") and not isinstance(tiles, (list, tuple)):     # a cad_kernel.Pattern
        tiles = tiles.offsets
    if isinstance(tiles, dict) and "offsets" in tiles:                         # a model.patterns entry
        tiles = tiles["offsets"]
    out = []
    for t in tiles:
        t = tuple(float(v) for v in t)
        if len(t) not in (2, 3):
            raise CamError("tiles: each tile is (dx, dy) or (dx, dy, dz) from the part the ops are computed on")
        out.append(t + (0.0,) * (3 - len(t)))
    if not out:
        raise CamError("tiles: at least one tile")
    if len(set(out)) != len(out):
        raise CamError("tiles: two tiles at the same place")
    return out


def tiles_of(model, base: str | None = None) -> list[tuple[float, float, float]]:
    """The tile offsets of a pattern() in the design (by its base body's name; the only pattern when there is one), for
    Setup(tiles=...). Compute the ops on the base body (bodies[base]) and every copy is machined the same."""
    pats = getattr(model, "patterns", None) or []
    if not pats:
        raise CamError("tiles_of: the design has no pattern() (copies made by hand are separate bodies: use pattern() in the design)")
    if base is None:
        if len(pats) > 1:
            raise CamError("tiles_of: the design has several patterns: name one (" + ", ".join(p["base"] for p in pats) + ")")
        return [tuple(o) for o in pats[0]["offsets"]]
    for p in pats:
        if p["base"] == base or p["base"].split("/")[-1] == base:
            return [tuple(o) for o in p["offsets"]]
    raise CamError(f"tiles_of: no pattern with base '{base}' (patterns: {', '.join(p['base'] for p in pats)})")


def tile_moves(op, offs, link_z: float):
    """An op's moves at every tile, as the post runs them: tile k's moves shifted by offs[k], and between tiles a lift
    to link_z then over to the next tile's first point. Returns (moves, src): src[i] = the op move it came from, -1 for
    a link move."""
    base = op.moves
    if len(offs) <= 1 and not any(offs[0]):
        return [list(m) for m in base], list(range(len(base)))
    out, src = [], []
    for k, (dx, dy, dz) in enumerate(offs):
        if k and base and out:
            last = out[-1]
            out.append([RAPID, last[1], last[2], max(link_z, last[3]), 0.0] + list(last[5:]))
            src.append(-1)
            f = base[0]
            out.append([RAPID, f[1] + dx, f[2] + dy, max(link_z, last[3]), 0.0] + list(f[5:]))
            src.append(-1)
        for i, m in enumerate(base):
            out.append([m[0], m[1] + dx, m[2] + dy, m[3] + dz] + list(m[4:]))
            src.append(i)
    return out, src


def fixtures(table: dict | None = None) -> None:
    """Per-setup fixture choices, keyed by setup name: {"Top": {"name": "Makera low-profile vise", "at": [0, 10], "rot": 0,
    "params": {"seat": "ledge"}, "extra": [{"name": "Step clamps", "at": [80, 0], "rot": 90}]}}. Written by the CAM tab's
    fixture panel; put it before the setups. A setup named here uses these fixtures instead of the ones in its Setup(...)
    call ("name": null = the stock on the bare table; "extra" = more fixtures on the table, each placed and turned)."""
    for k, v in (table or {}).items():
        if not isinstance(v, dict):
            raise CamError(f"fixtures: '{k}' must map to a dict with name / at / rot / params")
    _FIXTURES["table"] = dict(table or {})


def _scallop(sh, s: float) -> float:
    """Height of the ridge a tool profile leaves between two passes s apart on a flat floor."""
    if s <= 0:
        return 0.0
    x = np.linspace(0.0, s, 201)
    h = np.minimum(sh.cut_height(np.abs(x)), sh.cut_height(np.abs(s - x)))
    return float(np.nanmax(np.where(np.isfinite(h), h, np.nan))) if np.isfinite(h).any() else float("inf")


def _floor_cleanup(op: Op, kind: str, args: dict, plan_tool: Tool, real_tool: Tool) -> list[str]:
    """A ball / bull / V tool only reaches the floor along its centreline: passes planned for a flat stand-in (rings of a
    pocket, an adaptive's fronts, face rows) can leave floor further from every pass than the stepover promises - the
    middle of a pocket and the corners of its rings (a Ø9 pocket with an R1 ball: 0.13 mm left where the stepover says
    0.046). Find that floor (the tool-centre area at the bottom farther than r_eff from every bottom pass, r_eff = where
    the profile is the promised ridge height above its tip), add short passes over it, and say what is left."""
    if real_tool.type == "flat" or kind not in ("face", "pocket", "adaptive") or not op.moves:
        return []
    try:
        sh = real_tool.shape
    except Exception:  # noqa: BLE001
        return []
    zb = (op.params or {}).get("z_bottom", args.get("z_bottom"))
    if zb is None:
        return []
    zb, st = float(zb), op.setup
    w = plan_tool.diameter
    if kind == "face":
        region = unary_union(_polys(args.get("region")) or [st.stock.rect()])
        C = region
    else:
        region = unary_union(_polys(args.get("polys")))
        C = region.buffer(-(w / 2 + float(args.get("stock_to_leave") or 0.0)) + 1e-3, join_style=1)
    if C.is_empty:
        return []
    so = float(args.get("stepover") or plan_tool.stepover) * w
    h = max(_scallop(sh, so), 0.003)
    rr = np.linspace(0.0, sh.cut_radius, 4001)
    ch = sh.cut_height(rr)
    ok = rr[np.isfinite(ch) & (ch <= h + 1e-9)]
    r_eff = float(ok.max()) if len(ok) else 0.0
    if r_eff < 0.005:
        return []

    def bottom_passes():
        out, prev = [], None
        for mv in op.moves:
            p = (mv[1], mv[2], mv[3])
            if prev is not None and mv[0] in (FEED, PLUNGE) and abs(p[2] - zb) < 1e-3 and abs(prev[2] - zb) < 1e-3:
                out.append(LineString([prev[:2], p[:2]]) if math.hypot(p[0] - prev[0], p[1] - prev[1]) > 1e-6 else Point(p[:2]))
            elif mv[0] == PLUNGE and abs(p[2] - zb) < 1e-3:
                out.append(Point(p[:2]))                  # a plunge to the floor touches it there (a clean-up visit)
            prev = p
        return unary_union(out) if out else None

    def worst(paths) -> float:                           # the largest distance from floor to the nearest bottom pass
        import shapely
        minx, miny, maxx, maxy = C.bounds
        step = max(r_eff / 3, 0.02, math.sqrt(max(C.area, 1e-9) / 150_000))     # ≤ ~150k samples on a big floor
        gx, gy = np.meshgrid(np.arange(minx, maxx + step, step), np.arange(miny, maxy + step, step))
        pts = shapely.points(gx.ravel(), gy.ravel())
        pts = pts[shapely.contains(C, pts)]
        return float(shapely.distance(pts, paths).max()) if len(pts) and paths is not None else 0.0

    paths = bottom_passes()
    if paths is None:
        return []
    before = worst(paths)
    if before <= r_eff + 0.01:
        return []
    from shapely.ops import polylabel
    pb = PathBuilder(st, real_tool, op)
    pb.feed = min(pb.feed, real_tool.feed); pb.pos = tuple(op.moves[-1][1:4]); n0 = len(op.moves)
    added = 0
    for _ in range(4):
        gaps = C.difference(paths.buffer(r_eff, quad_segs=16)).buffer(-0.01).buffer(0.01)
        polys = [g for g in getattr(gaps, "geoms", [gaps]) if not g.is_empty and g.area > 1e-6]
        if not polys:
            break
        todo, ordered = list(polys), []
        at = pb.pos[:2]
        while todo:
            g = min(todo, key=lambda g: math.hypot(g.centroid.x - at[0], g.centroid.y - at[1]))
            todo.remove(g); ordered.append(g); at = (g.centroid.x, g.centroid.y)
        for g in ordered:
            mrr = g.minimum_rotated_rectangle
            c = list(mrr.exterior.coords) if mrr.geom_type == "Polygon" else []
            if len(c) >= 4 and max(math.dist(c[0], c[1]), math.dist(c[1], c[2])) > 2 * r_eff:
                a, b_ = (c[0], c[1]) if math.dist(c[0], c[1]) < math.dist(c[1], c[2]) else (c[1], c[2])
                m1 = ((a[0] + b_[0]) / 2, (a[1] + b_[1]) / 2)
                o = c[2] if (a, b_) == (c[0], c[1]) else c[3]
                d = (o[0] - b_[0], o[1] - b_[1])
                seg = LineString([m1, (m1[0] + d[0], m1[1] + d[1])]).intersection(C)
                seg = seg if seg.geom_type == "LineString" and seg.length > 0 else Point(polylabel(g, 0.005).coords[0])
            else:
                seg = Point(polylabel(g, 0.005).coords[0]) if g.geom_type == "Polygon" else g.representative_point()
            pts = list(seg.coords)
            if len(pts) > 1 and math.dist(pb.pos[:2], pts[-1]) < math.dist(pb.pos[:2], pts[0]):
                pts.reverse()
            # stay down when the way there is over cleared floor (only ridges this low are left on it); else lift
            if abs(pb.pos[2] - zb) < 1e-6 and C.buffer(1e-6).covers(LineString([pb.pos[:2], pts[0]])):
                pb.feed_to(pts[0][0], pts[0][1], zb)
            else:
                pb.retract(); pb.rapid_xy(*pts[0]); pb.plunge_to(zb, rapid_to=zb + h, gap=1.0)
            for p in pts[1:]:
                pb.feed_to(p[0], p[1], zb)
            added += 1
        pb.retract()
        paths = bottom_passes()
    after = worst(paths)
    hb, ha = float(sh.cut_height(np.array([min(before, sh.cut_radius)]))[0]), float(sh.cut_height(np.array([min(after, sh.cut_radius)]))[0])
    lifts = sum(1 for mv in op.moves[n0:] if mv[0] == PLUNGE)
    return [f"{real_tool.label()}: the passes left floor up to {before:.2f} mm from any pass (the middle / ring corners: "
            f"{hb:.3f} mm of material); added {added} short clean-up pass{'es' if added != 1 else ''} there ({lifts} "
            f"plunge{'s' if lifts != 1 else ''}) - the floor is now within {ha:.3f} mm"]


def _shape_plan(kind: str, args: dict, sig) -> list[str]:
    """2.5D operations (face / pocket / adaptive / contour) with a tool that is not a flat endmill: plan the path with the
    tool's width at the cut depth (so the top edge of the cut lands on the boundary and nothing is overcut) by swapping
    in a flat stand-in of that width — the operation gets the real tool back afterwards — and say what the tool's shape
    leaves (sloped walls, a scalloped floor, a fillet). Any tool: warn when the cut is deeper than the flutes or deep
    enough for a wider shank / neck to reach the walls. Returns the notes for op.warnings."""
    from dataclasses import replace
    tool: Tool = args["tool"]
    try:
        sh = tool.shape
    except Exception as e:  # noqa: BLE001 — a bad tool definition is reported, the op still builds as before
        return [f"{tool.label()}: tool shape: {e}"]
    notes = []
    zt, zb = args.get("z_top"), args.get("z_bottom")
    depth = float(zt) - float(zb) if zt is not None and zb is not None else None
    fl = sh.flute_length
    if depth is not None and depth > fl + 1e-6:
        notes.append(f"{tool.label()}: the cut is {depth:.2f} mm deep but the flutes are {fl:g} mm long — the tool's body rubs above them")
    if depth is not None and sh.max_radius > sh.cut_radius + 1e-6:
        top = float(sh.height(np.array([sh.cut_radius + 1e-3]))[0])      # where the tool first gets wider than its cut
        if depth > top + 1e-6:
            notes.append(f"{tool.label()}: {depth:.2f} mm deep, but {top:.2f} mm above its tip the tool widens to "
                         f"Ø{2 * sh.max_radius:g} (shank / neck): it hits any wall within {sh.max_radius - sh.cut_radius:.2f} mm of the cut "
                         "— the simulator checks where")
    if tool.type == "flat":
        return notes
    w = sh.width_at_depth(depth) if depth is not None and depth > 0 else 2 * sh.cut_radius
    if kind == "contour" and args.get("side", "outside") == "on" and depth:
        notes.append(f"{tool.label()} ({tool.spec()}): on the line {depth:.3f} mm deep it cuts a groove {w:.3f} mm wide "
                     f"at the top ({w / 2:.3f} each side of the line)")
    if kind != "contour" or args.get("side", "outside") != "on":
        if w < tool.diameter - 1e-6:
            args["tool"] = replace(tool, diameter=round(w, 6), type="flat")
            notes.append(f"{tool.label()}: planned with its Ø{w:.3f} width at the {depth:.2f} mm depth so the top of the "
                         "cut stays on the boundary; the walls below follow the tool's shape" if depth else
                         f"{tool.label()}: planned with its Ø{w:.3f} cutting width")
    if kind in ("face", "pocket", "adaptive"):
        so_frac = args.get("stepover") or tool.stepover
        s = float(so_frac) * (w if w < tool.diameter else tool.diameter)
        sc = _scallop(sh, s)
        flat_w = 2 * float(np.max(np.where(sh.cut_height(np.linspace(0, sh.cut_radius, 400)) <= 1e-4,
                                           np.linspace(0, sh.cut_radius, 400), 0.0)))
        what = {"ball": "a ball end", "bull": f"a R{tool.corner_radius:g} corner", "taper_ball": "a tapered ball end",
                "vbit": "a V tip", "chamfer": "a chamfer tip", "drill": "a drill point"}.get(tool.type, tool.type)
        notes.append(f"{tool.label()} has {what}: the floor is cut by its Ø{flat_w:.3f} flat"
                     + (f" and left with {sc:.3f} mm ridges between passes {s:.3f} mm apart" if sc > 0.001 else " (passes overlap it: flat)")
                     + " — use a flat endmill for flat floors")
    return notes


def _plain(v):
    """A cut setting as the CAM tab shows it (numpy scalars -> float; anything else non-scalar -> None)."""
    if isinstance(v, bool) or v is None or isinstance(v, str):
        return v
    try:
        return round(float(v), 4)
    except (TypeError, ValueError):
        return None


def unused_overrides() -> list[str]:
    return [k for k in _OV["table"] if k not in _OV["used"]]


def _op_defaults(kind: str, tool: Tool, a: dict) -> dict:
    sd, so = a.get("stepdown"), a.get("stepover")
    if kind == "adaptive":
        so = 0.15 if so is None else so
        if not sd and a.get("z_top") is not None and a.get("z_bottom") is not None:
            st_ = a.get("setup")
            lim = st_.machine.adaptive_doc(tool, st_.model_stock.material or st_.stock.material) if st_ is not None else tool.flute_length * 0.9
            sd = max(min(lim, a["z_top"] - a["z_bottom"]), 0.1)
    elif kind == "parallel3d":
        so = so or min(tool.stepover, 0.2)
    elif kind in ("rotary_finish", "rotary_profile"):
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
        stop_point()
        load_script = kw.pop("load", None) if "load" not in sig.parameters else None
        bodies_ = kw.pop("bodies", None) if "bodies" not in sig.parameters else None
        if _OV["depth"]:                                   # an op built inside another (rotary_wrap -> pocket): pass through
            return fn(*args, **kw)
        b = sig.bind(*args, **kw)
        name = b.arguments.get("name", sig.parameters["name"].default)
        n = _OV["seen"][name] = _OV["seen"].get(name, 0) + 1
        key = name if n == 1 else f"{name}#{n}"
        tool = b.arguments["tool"]
        fields = OP_FIELDS.get(kind, OVERRIDE_FIELDS)
        defaults = _op_defaults(kind, tool, b.arguments)
        entry = _OV["table"].get(key) or {}
        ov = {k: float(v) for k, v in entry.items() if k in fields}
        cut_meta = CUT_FIELDS.get(kind, {})
        cut_ov = {k: v for k, v in entry.items() if k in cut_meta and k in sig.parameters}
        if key in _OV["table"]:
            _OV["used"].add(key)
        if ov:
            b.arguments["tool"] = replace(tool, **ov)
            for k in ("stepdown", "stepover"):
                if k in ov and k in sig.parameters:
                    b.arguments[k] = ov[k]
        cut_script = {k: b.arguments.get(k, sig.parameters[k].default) for k in cut_meta if k in sig.parameters}
        for k, v in cut_ov.items():
            b.arguments[k] = v
        real_tool = b.arguments["tool"]
        notes = _shape_plan(kind, b.arguments, sig) if kind in ("face", "pocket", "adaptive", "contour") else []
        _OV["depth"] += 1
        try:
            op = fn(*b.args, **b.kwargs)
        finally:
            _OV["depth"] -= 1
        plan_tool = op.tool
        if op.tool is not real_tool and kind in ("face", "pocket", "adaptive", "contour"):
            op.tool = real_tool                            # planned with the tool's width at the cut depth (see _shape_plan)
        notes += _floor_cleanup(op, kind, b.arguments, plan_tool, real_tool)
        op.warnings.extend(notes)
        op.key, op.base_tool, op.overrides, op.defaults = key, tool, ov, defaults
        op.cut_defaults = {k: (None if v is inspect.Parameter.empty else _plain(v)) for k, v in cut_script.items()}
        op.cut_overrides = dict(cut_ov)
        if bodies_:                                        # what the op cuts (the simulation checks one contour per body)
            bl = [bodies_] if isinstance(bodies_, str) or not isinstance(bodies_, (list, tuple, set)) else list(bodies_)
            op.params["bodies"] = [x if isinstance(x, str) else str(getattr(x, "path", None) or getattr(x, "name", x)) for x in bl]
        if "load" in cut_meta:                             # fit_feeds' chip load scale: not an argument of the op itself
            op.cut_defaults["load"] = _plain(load_script)
            ld = entry.get("load", load_script)
            if "load" in entry:
                op.cut_overrides["load"] = entry["load"]
            if ld is not None:
                if not (LOAD_RANGE[0] <= float(ld) <= LOAD_RANGE[1]):
                    raise CamError(f"{name}: load={ld!r} (the feed fit's chip load scale, {LOAD_RANGE[0]:g} .. {LOAD_RANGE[1]:g})")
                op.params["load"] = float(ld)
        if isinstance(entry.get("color"), str):
            op.color = entry["color"]
            op.color_override = True
        return op
    wrapper.__wrapped_op__ = True
    return wrapper


face = _overridable(face, "face")
contour = _overridable(contour, "contour")
pocket = _overridable(pocket, "pocket")
bore = _overridable(bore, "pocket")
adaptive = _overridable(adaptive, "adaptive")
drill = _overridable(drill, "drill")
parallel3d = _overridable(parallel3d, "parallel3d")
chamfer_edges = _overridable(chamfer_edges, "chamfer")
vcarve = _overridable(vcarve, "vcarve")
rotary_rough = _overridable(rotary_rough, "rotary_rough")
rotary_finish = _overridable(rotary_finish, "rotary_finish")
rotary_wrap = _overridable(rotary_wrap, "rotary_wrap")
rotary_profile = _overridable(rotary_profile, "rotary_profile")
gear_teeth = _overridable(gear_teeth, "gear_teeth")
PUBLIC_API.append("overrides")
