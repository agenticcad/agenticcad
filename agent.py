"""
Claude Agent SDK wrapper for AgenticCAD.

One long-lived ClaudeSDKClient session per server. Custom CAD tools run
in-process (SDK MCP server) so they can touch the kernel and the browser bus.
Also owns the "design" (a .py script file in workspace/designs) and the build history.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import shutil
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions, tool, create_sdk_mcp_server,
    AssistantMessage, UserMessage, SystemMessage, ResultMessage,
)
from claude_agent_sdk.types import StreamEvent, TextBlock, ToolUseBlock, ToolResultBlock

import cad_kernel as ck
import cam_kernel as cam
import drawing
from cam_data import Library
from library import PartLibrary, slug as lib_slug
import script_edit

Emit = Callable[[dict[str, Any]], Awaitable[None]]

# ---------------------------------------------------------------------------- Claude Code discovery
# Claude Code is not bundled with the desktop app (it is installed and signed in separately). GUI apps get a
# minimal PATH on macOS/Windows, so look in the places the official installers use as well as PATH.
def claude_cli_candidates() -> list[str]:
    home = Path.home()
    if sys.platform == "win32":
        cands = [home / ".local" / "bin" / "claude.exe",
                 Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local")) / "Programs" / "claude" / "claude.exe",
                 Path(os.environ.get("APPDATA", home / "AppData" / "Roaming")) / "npm" / "claude.exe"]
    else:
        cands = [home / ".local" / "bin" / "claude", Path("/opt/homebrew/bin/claude"), Path("/usr/local/bin/claude"),
                 home / ".npm-global" / "bin" / "claude", Path("/usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js"),
                 home / ".claude" / "local" / "claude"]
    return [str(c) for c in cands]


def find_claude_cli() -> str | None:
    """Path to a runnable Claude Code binary, or None. Env AGENTICCAD_CLAUDE overrides."""
    env = os.environ.get("AGENTICCAD_CLAUDE")
    if env and Path(env).is_file():
        return env
    exe = shutil.which("claude.exe" if sys.platform == "win32" else "claude")
    if exe and not exe.lower().endswith((".cmd", ".bat", ".ps1")):
        return exe
    for c in claude_cli_candidates():
        p = Path(c)
        if p.is_file() and os.access(p, os.X_OK) and not c.endswith(".js"):
            return str(p)
    return None


SYSTEM_PROMPT = """You are AgenticCAD, a CAD copilot. The user talks to you; you build and modify a
parametric 3D design by writing build123d (Python, OCCT-based) code and calling the `build_model`
tool with the COMPLETE script every time (there is one design script; each build replaces it).

Rules
- Units are millimetres, Z is up. Keep dimensions as named variables at the top of the script.
- Geometry is exact B-rep (OCCT). Circles, cylinders, fillets are true analytic surfaces; the viewer's
  triangles are display-only. STEP export is exact; STL is tessellated at export time with a chosen tolerance.
- The script must assign `result`. One body: `result = part`. Several bodies: a dict of name -> shape,
  and nested dicts make components (a Fusion-style browser tree):
      result = {"Bracket": {"Plate": plate, "Boss": boss}, "Pin": pin}
  Bodies are kept as separate solids (never fused). Inside one BuildPart everything fuses into one part,
  so build separate bodies in separate BuildPart contexts or with algebra mode, and position them with
  `Pos(x, y, z) * shape`, `Rot(...)`, or `shape.moved(Location((x, y, z)))`. A shape containing
  disjoint solids is split into bodies automatically.
- `from build123d import *` and `math` are pre-imported. Do not print large dumps.
- After every build the browser viewer updates automatically. Face ids are re-numbered after every
  build, so re-run `inspect_model` before referring to face ids from an earlier build.
- When the user's message includes "[Selected geometry]" it lists faces and/or bodies they clicked in
  the viewer, with body, type, centre, normal and size. Use those centres/normals to pick the same face
  in code, e.g. `part.faces().sort_by_distance((x, y, z))[0]` or
  `part.faces().filter_by(Axis.Z).sort_by(Axis.Z)[-1]`.
- Messages may start with "[Note]" lines from the app (design opened, script edited by hand, undo).
  When the script changed outside your control, call `get_code` before editing.
- Use `screenshot` to look at the design when a change is non-trivial, when the user asks how it looks,
  or when a build result is surprising. Use `highlight_faces` to confirm which face is which.
- The user may attach photos, sketches or drawings ("[Attached images: ...]"). Read the geometry, proportions
  and any dimension callouts from them; dimensions given in the text override what you estimate. Say which
  dimensions you estimated. If a critical dimension is missing, pick a sensible value, state it, and continue
  rather than stalling. The image files are also saved in the workspace (paths given) if you need to re-read them.
- If a build fails, read the traceback, fix the code and rebuild; do not ask the user to debug Python.
- Keep replies short: say what you changed and any assumption you made. No code in replies unless asked;
  the code lives in the tool call and the user can open it in the Code panel.
- Keep dimensions as top-level `name = number` assignments: they appear in the user's Parameters panel and
  `set_parameters` can change them without a rewrite. `import_step("file.step")` loads a STEP from
  workspace/imports (the user may have imported one; the script then already contains the line).
  `from_library("part name", param=value)` instantiates a library part. The part library is a first-class source of
  geometry: before modelling a standard or previously made part (fasteners, bearings, brackets, anything reusable),
  `library search` it and `library insert` it rather than re-creating it; when you build something reusable, offer to
  `library save_body` / `save_design` it.
  Use `measure` / `mass_properties` to check fits, gaps, wall thicknesses and weights instead of guessing;
  `make_drawings` produces shop drawings.
- Sketches: the user can draw 2D sketches in the UI; they appear in the script as blocks between
  `# sketch:NAME {...}` and `# /sketch:NAME` that assign a build123d Sketch to variable NAME (never edit or
  remove those blocks; the editor owns them, and the user can re-open them). Use them like any Sketch:
  `extrude(NAME, amount=10)` (along the sketch plane normal; negative goes the other way),
  `part - extrude(NAME, amount=-8)` to cut, `revolve(NAME, axis=Axis.Z)`, or `add(NAME)` inside a BuildPart.
  To change a sketch (add a hole, resize a rectangle, move an item) use the `sketch` tool with action=set —
  it edits the items in the same format the user's editor uses, so both of you can keep editing it. Use it
  too when you want to give the user something to tweak graphically. Plain BuildSketch code you write is
  fine for one-off geometry but the user cannot edit it in the sketch editor.
- The user can also model manually with the toolbar (primitives placed on faces, press/pull extrude of a
  face, holes (plain / counterbored / tapped), fillet, chamfer, shell, move/rotate, sketch extrude). Those
  appear as `[Note]`s and as expression rewrites in the result dict (e.g. `_bracket = bp.part` hoisted
  before `result`, then `_bracket.fillet(2, [...])`) — treat them like any other user edit and keep them.
  `hole(part, d, at=.., depth=.. | through=True, axis=.., counterbore=(d, depth))` is available to you too.
- Threads (ISO metric, pre-imported): `iso("M4")` -> pitch/tap_drill/clearance; `tap_drill("M4")`,
  `clearance_dia("M4", "medium")`; `tapped_hole("M4", depth, at=(x, y, surface_z), through=False)` returns a
  cosmetic cutter to SUBTRACT; `tap(part, "M4", at=(x, y, surface_z), depth=8 | through=True, real=False)` cuts
  (and with real=True actually models) a tapped hole into a part and registers it so drawings call it out
  "M4×0.7 ↧8"; `bolt("M4", 16, head="hex"|"socket"|"none", real=False)` (head above z=0, shank down −Z, so
  `Pos(x, y, surface_z) * bolt(...)` sits on a surface), `nut("M4")`, `washer("M4")`. Prefer cosmetic threads
  (fast, clean STEP); use real=True only when the user wants the helix (3D printing, visual).

build123d cheat sheet (builder mode)
```
with BuildPart() as bp:
    Box(60, 40, 8)                                # centred on origin by default
    with Locations((0, 0, 4)):
        Cylinder(12, 20, align=(Align.CENTER, Align.CENTER, Align.MIN))
    Hole(radius=3)                                # through hole (NB: Hole takes a RADIUS, Ø6 here)
    with Locations((-22, -12), (22, 12)):
        Hole(1.6)                                 # Ø3.2 holes
    with BuildSketch(bp.faces().sort_by(Axis.Z)[-1]) as sk:   # sketch on top face
        Rectangle(20, 10)
        with Locations((0, 6)): Circle(3, mode=Mode.SUBTRACT)
    extrude(amount=5)                             # or amount=-5, mode=Mode.SUBTRACT to cut
    fillet(bp.edges().filter_by(Axis.Z), 3)
    chamfer(bp.faces().sort_by(Axis.Z)[-1].edges(), 1)
    with Locations(Plane.XZ): Cylinder(5, 100)    # rotated cylinder along Y
    mirror(about=Plane.YZ)                        # mirror body
    with GridLocations(15, 15, 3, 2): Hole(2)
    with PolarLocations(20, 6): Hole(2)
result = bp.part
```
Algebra mode also works: `part = Box(10,10,10) - Cylinder(3, 20); part = fillet(part.edges().filter_by(Axis.Z), 1)`.
Useful: Sphere, Cone, Torus, Wedge, revolve(), sweep(), loft(), offset(), split(bisect_by=Plane.XY),
offset(amount=-1, openings=face) for hollowing, Text(), Polygon(), Slot*, RegularPolygon, Trapezoid,
Rot()/Pos() for algebra transforms, ShapeList.group_by(), .filter_by(GeomType.CYLINDER).
Sort helpers: sort_by(Axis.Z), sort_by(SortBy.AREA), sort_by_distance(pt), filter_by(Axis.Z) (faces parallel to Z-normal).
Docs: https://build123d.readthedocs.io (use WebFetch if unsure of an API).

CAM (GRBL G-code)
There is a second script per design, the CAM script, built with `build_cam`. It runs with these names:
  model (the built design: model.shape = all bodies, model.bodies, model.get_face(id), model.bbox_min/max),
  part (= model.shape), bodies (dict name -> shape), tools (dict: by number, by name, by 'T1'),
  machines (dict name -> Machine), and everything from cam_kernel: Stock, Setup, Program, face, contour, pocket,
  drill, parallel3d, section, silhouette, stock_minus, holes, face_polygon, circle, rect.
It must assign `program` (a Program). Call `cam_context` first to see the machines/tools/model facts.
```
stock = Stock.from_model(model, margin=3, top=1.0)          # also accepts a body shape; or Stock.block(80, 50, 12, top=8)
setup = Setup(machines["Generic 3018"], stock, origin="stock-top-left")   # origins: stock-top-left|stock-top-center|
                                                            #   stock-bottom-left|stock-bottom-center|model-origin|(x,y,z)
t_flat, t_drill = tools[1], tools["3 mm drill"]
hs = holes(part, dmax=8)                                    # Hole(x, y, diameter, z_top, z_bottom, through)
program = Program(setup, name="bracket")
program.add(face(setup, t_flat, z_top=stock.top, z_bottom=4.0))                 # surface stock down to the part top
program.add(drill(setup, t_drill, [h for h in hs if h.diameter <= 3.5], peck="auto"))
program.add(pocket(setup, tools[2], circle(0, 0, 6), z_top=4, z_bottom=-4.5, name="Bore"))
program.add(contour(setup, t_flat, section(part, 0.0), z_top=4, z_bottom=-4.5, side="outside", tabs=4, tab_height=3))
program.add(parallel3d(setup, tools["6 mm ball endmill"], shape=part, z_bottom=4, region=rect(-15,-15,15,15), stepover=0.15))
```
program.add(adaptive(setup, t_flat, stock_minus(setup, part, 0.0, expand=t_flat.diameter), z_top=24, z_bottom=-4, stepover=0.15))  # HSM roughing:
   # true constant engagement: radial engagement = stepover×D on every pass (crescents/trochoids in slots), full flute
   # depth, helical entry, feed automatically reduced in corners. Leaves ≤ ae scallops: follow with contour(side="inside"/"outside")
program.add(pocket(setup, tools[2], slot, z_top=4, z_bottom=2, rest_from=t_flat, name="Rest corners"))        # rest machining (2.5D)
program.add(parallel3d(setup, ball3, shape=part, z_bottom=4, region=..., rest_from=tools[4], name="3D rest"))  # 3D rest
t_alu = apply_feeds(tools[2], "aluminium", setup.machine)    # feeds & speeds calculator -> tool copy with rpm/feed/plunge/stepdown/stepover
info = feeds(tools[2], "plywood", setup.machine, radial_engagement=0.15)   # dict + notes (chip thinning, spindle limits)
```
Notes: all coordinates are model coordinates (Z up); the post subtracts the WCS origin.
Contours get tangential arc lead-in/out by default (lead=radius; lead=0 disables) and start mid-way along the
longest straight edge. The post fits G2/G3 arcs (machine.arcs, machine.arc_tolerance) and merges collinear moves.
Materials for feeds(): aluminium, brass, mild-steel, acrylic, hdpe, delrin, plywood, mdf, hardwood, softwood, foam, fr4, carbon-fibre. section(shape, z) returns
polygons with holes (holes = islands for pocket, inner profiles for contour side="outside"); stock_minus(setup,
shape, z, expand=tool.diameter) = what to clear at level z (expand lets the tool run off the stock edge); face_polygon(model.get_face(id)) uses a face the user clicked. Depths:
z_bottom below the stock bottom means cutting into the spoilboard (fine for through-cuts with a sacrificial
board, warn otherwise). Tool numbers matter for tool changes (GRBL: machine.tool_change='pause' emits M6/M0).
Prefer the tool library's feeds/stepdown; override per op only with a reason. After build_cam, use `screenshot`
to look at the toolpaths (they are drawn over the model; rapids red, cuts coloured per op).
`export_gcode` writes the .nc file. `save_machine` / `save_tool` edit the libraries (JSON dicts; see cam_context).
"""

SCREENSHOT_VIEWS = ["iso", "iso_back", "front", "back", "left", "right", "top", "bottom"]
SAFE_NAME = re.compile(r"[^A-Za-z0-9 _\-\.]+")


def safe_name(name: str) -> str:
    name = SAFE_NAME.sub("", name or "").strip().rstrip(".")
    return name[:80] or "untitled"


class CadAgent:
    def __init__(self, workspace: Path, emit: Emit,
                 screenshot_fn: Callable[[dict[str, Any]], Awaitable[str]]):
        self.workspace = workspace
        self.designs_dir = workspace / "designs"
        self.designs_dir.mkdir(parents=True, exist_ok=True)
        self.emit = emit                     # broadcast an event to browsers
        self.screenshot_fn = screenshot_fn   # ask a browser to render a PNG (base64)
        self.model: ck.Model | None = None
        self.client: ClaudeSDKClient | None = None
        self.busy = False
        self.session_id: str | None = None
        self.model_lock = asyncio.Lock()
        self.quality = os.environ.get("AGENTICCAD_QUALITY", "normal")   # display-mesh preset
        self.design_name: str | None = None   # None = unsaved/untitled
        self.saved_code: str | None = None    # code as of last save/open
        self.notes: list[str] = []            # out-of-band events to tell the agent on the next turn
        self.settings = self._load_settings()
        self.library = Library(workspace)
        self.parts = PartLibrary(workspace)
        ck.WORKSPACE = workspace
        ck.LIBRARY = self.parts
        (workspace / "imports").mkdir(exist_ok=True)
        self.tool_handlers: dict[str, Any] = {}   # filled by _make_server()
        self.cam_code: str = ""               # CAM script working copy (workspace/cam.py)
        self.program: cam.Program | None = None
        cam_path = workspace / "cam.py"
        if cam_path.exists():
            self.cam_code = cam_path.read_text()

    # ------------------------------------------------------------------ design files
    @property
    def dirty(self) -> bool:
        if self.model is None:
            return False
        if self.model.code != (self.saved_code or ""):
            return True
        cam_file = self.designs_dir / f"{self.design_name}.cam.py" if self.design_name else None
        saved_cam = cam_file.read_text() if cam_file and cam_file.exists() else ""
        return self.cam_code.strip() != saved_cam.strip()

    def design_state(self) -> dict[str, Any]:
        return {"type": "design", "name": self.design_name, "dirty": self.dirty}

    def list_designs(self) -> list[dict[str, Any]]:
        out = []
        for p in sorted(self.designs_dir.glob("*.py"), key=lambda p: -p.stat().st_mtime):
            out.append({"name": p.stem, "modified": int(p.stat().st_mtime), "size": p.stat().st_size})
        return out

    async def save_design(self, name: str | None = None) -> str:
        if self.model is None:
            raise ck.CadError("nothing to save")
        name = safe_name(name or self.design_name or "untitled")
        (self.designs_dir / f"{name}.py").write_text(self.model.code)
        cam_file = self.designs_dir / f"{name}.cam.py"
        if self.cam_code.strip():
            cam_file.write_text(self.cam_code)
        elif cam_file.exists():
            cam_file.unlink()
        self.design_name = name
        self.saved_code = self.model.code
        self._write_state()
        await self.emit(self.design_state())
        return name

    async def open_design(self, name: str) -> None:
        path = self.designs_dir / f"{safe_name(name)}.py"
        if not path.exists():
            raise ck.CadError(f"no design named '{name}'")
        code = path.read_text()
        await self.build(code, source="open", record=True)
        cam_file = self.designs_dir / f"{path.stem}.cam.py"
        await self.set_cam_code(cam_file.read_text() if cam_file.exists() else "", rebuild=cam_file.exists())
        self.design_name = path.stem
        self.saved_code = code
        self._write_state()
        self.notes.append(f"The user opened design '{path.stem}'; the script changed.")
        await self.emit(self.design_state())

    async def new_design(self, code: str | None = None) -> None:
        await self.build(code or ck.NEW_DESIGN_CODE, source="new", record=True)
        await self.set_cam_code("", rebuild=False)
        self.design_name = None
        self.saved_code = None
        self._write_state()
        self.notes.append("The user started a new, untitled design; the script was reset.")
        await self.emit(self.design_state())

    async def import_design(self, name: str, code: str) -> str:
        name = safe_name(name.removesuffix(".py"))
        await self.build(code, source="open", record=True)   # raises CadError if the script is broken
        (self.designs_dir / f"{name}.py").write_text(code)
        self.design_name = name
        self.saved_code = code
        self._write_state()
        self.notes.append(f"The user imported design '{name}' from a file; the script changed.")
        await self.emit(self.design_state())
        return name

    # ------------------------------------------------------------------ CAM
    async def set_cam_code(self, code: str, rebuild: bool = True, source: str = "agent") -> cam.Program | None:
        self.cam_code = code
        (self.workspace / "cam.py").write_text(code)
        if not code.strip() or not rebuild:
            self.program = None
            await self.emit({"type": "cam", "program": None, "code": code, "source": source})
            return None
        prog = await asyncio.to_thread(self._run_cam, code)
        self.program = prog
        await self.emit({"type": "cam", "program": prog.to_payload(), "code": code, "source": source,
                         "summary": prog.summary(), "gcode_lines": prog.gcode().count("\n")})
        return prog

    def _run_cam(self, code: str) -> cam.Program:
        if self.model is None:
            raise cam.CamError("no design model built")
        import io, contextlib, traceback
        ns: dict[str, Any] = {"__name__": "__cam__"}
        exec("from cam_kernel import *\nimport math\nfrom math import *\nimport cam_kernel as cam\n", ns)
        ns.update({"model": self.model, "part": self.model.shape,
                   "bodies": {b.path: b.shape for b in self.model.bodies},
                   "tools": self.library.tool_map(), "machines": self.library.machines()})
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                exec(compile(code, "cam.py", "exec"), ns)
        except Exception:
            tb = "\n".join(l for l in traceback.format_exc().splitlines() if "agent.py" not in l)
            raise cam.CamError(tb + ("\nstdout:\n" + buf.getvalue() if buf.getvalue() else ""))
        prog = ns.get("program")
        if not isinstance(prog, cam.Program):
            raise cam.CamError("CAM script must assign `program = Program(setup, ...)`")
        if not prog.ops:
            raise cam.CamError("program has no operations (use program.add(...))")
        return prog

    def cam_context(self) -> str:
        lines = [self.library.describe(), "Materials for feeds(): " + ", ".join(cam.MATERIALS),
                 "CAM script API:\n" + cam.api_reference()]
        if self.model:
            m = self.model
            size = [b - a for a, b in zip(m.bbox_min, m.bbox_max)]
            lines.append(f"Model: {len(m.bodies)} bodies, bbox min=({ck.fmt(m.bbox_min)}) max=({ck.fmt(m.bbox_max)}) "
                         f"size=({ck.fmt(size)}); top z={m.bbox_max[2]:.2f}, bottom z={m.bbox_min[2]:.2f}")
            for b in m.bodies:
                lines.append("  " + b.summary())
            try:
                hs = cam.holes(m.shape)
                lines.append(f"Vertical round holes ({len(hs)}): " + "; ".join(repr(h) for h in hs[:40]))
            except Exception as e:  # noqa: BLE001
                lines.append(f"hole detection failed: {e}")
            planes = [f for f in m.faces if f.kind == "PLANE" and f.normal and f.normal[2] > 0.99]
            zs = sorted({round(f.center[2], 3) for f in planes}, reverse=True)
            lines.append("Up-facing planar levels (z): " + ", ".join(f"{z:.2f}" for z in zs[:20]))
        if self.program:
            lines.append("Current program:\n" + self.program.summary())
        elif self.cam_code.strip():
            lines.append("A CAM script exists but did not build; call get_cam_code.")
        else:
            lines.append("No CAM script yet.")
        return "\n".join(lines)

    # ------------------------------------------------------------------ parameters
    def get_params(self) -> list[dict[str, Any]]:
        if self.model is None:
            return []
        try:
            return script_edit.params(self.model.code)
        except SyntaxError:
            return []

    async def set_params(self, values: dict[str, float], source: str = "params") -> None:
        if self.model is None:
            return
        code = script_edit.set_params(self.model.code, {k: float(v) for k, v in values.items()})
        if code == self.model.code:
            return
        try:
            await self.build(code, source=source)
        except ck.CadError as e:
            await self.emit({"type": "error", "text": f"parameter change failed to build: {e}"})
            return
        if source == "params":
            self.notes.append("The user changed parameter(s) in the Parameters panel: "
                              + ", ".join(f"{k}={v:g}" for k, v in values.items()) + " (script rewritten).")

    # ------------------------------------------------------------------ STEP import / bodies
    async def add_body_expr(self, var: str, expr: str, body_name: str, source: str = "insert") -> None:
        if self.model is None:
            return
        var = re.sub(r"[^a-z0-9_]", "_", var.lower()) or "part"
        if var[0].isdigit():
            var = "p_" + var
        existing = {b.name for b in self.model.bodies}
        base, k = body_name, 2
        while body_name in existing:
            body_name = f"{base} {k}"; k += 1
        code = script_edit.add_body(self.model.code, var, expr, body_name)
        await self.build(code, source=source)
        self.notes.append(f"The user inserted body '{body_name}' = {expr} into the script.")
        await self.emit({"type": "info", "text": f"added body '{body_name}'"})

    async def import_step(self, filename: str, data: bytes, mode: str = "add", description: str = "") -> dict[str, Any]:
        """mode: add (to current design) | new (new design) | library"""
        stem = safe_name(Path(filename).stem) or "part"
        if mode == "library":
            meta = self.parts.save_step(stem, data, description=description)
            await self.emit({"type": "library_parts", "parts": self.parts.list()})
            return {"library": meta["name"]}
        path = self.workspace / "imports" / f"{stem}.step"
        path.write_bytes(data)
        expr = f'import_step("{path.name}")'
        if mode == "new":
            code = f'# Imported from {filename}\n{re.sub(r"[^a-z0-9_]", "_", stem.lower())} = {expr}\nresult = {{"{stem}": {re.sub(r"[^a-z0-9_]", "_", stem.lower())}}}\n'
            await self.build(code, source="new")
            await self.set_cam_code("", rebuild=False)
            self.design_name = None; self.saved_code = None; self._write_state()
            self.notes.append(f"The user started a new design by importing STEP file {path.name}.")
            await self.emit(self.design_state())
        else:
            await self.add_body_expr(stem, expr, stem, source="insert")
        return {"file": path.name}

    async def insert_library_part(self, name: str, params: dict[str, Any] | None = None, body_name: str | None = None) -> None:
        meta = self.parts.get(name)
        if meta is None:
            raise ck.CadError(f"no library part '{name}'")
        args = ", ".join([f'"{meta["name"]}"'] + [f"{k}={float(v):g}" for k, v in (params or {}).items()])
        await self.add_body_expr(lib_slug(meta["name"]).replace("-", "_"), f"from_library({args})", body_name or meta["name"])

    async def add_to_library(self, name: str, body: str | None, description: str, tags: list[str], thumb_b64: str | None) -> dict[str, Any]:
        if self.model is None:
            raise ck.CadError("no model")
        if thumb_b64 is None:
            try:   # ask the browser for a thumbnail (only this body when saving a body)
                spec = {"view": "iso", "showEdges": True}
                if body:
                    spec["only"] = [body]
                big = await self.screenshot_fn(spec)
                thumb_b64 = _thumbnail(big, 160)
            except Exception:
                thumb_b64 = None
        if body:
            b_ = self.model.body_by_name(body)
            if b_ is None:
                raise ck.CadError(f"no body '{body}'")
            # store the body as a STEP part (exact) — scripts can't be sliced per body reliably
            import tempfile, os
            fd, tmp = tempfile.mkstemp(suffix=".step"); os.close(fd)
            b3d = __import__("build123d")
            shape = b_.shape if isinstance(b_.shape, b3d.Compound) else b3d.Compound(children=[b_.shape])
            b3d.export_step(shape, tmp)
            data = Path(tmp).read_bytes(); os.unlink(tmp)
            meta = self.parts.save_step(name, data, description=description, tags=tags, thumb_b64=thumb_b64)
        else:
            meta = self.parts.save_script(name, self.model.code, description=description, tags=tags, thumb_b64=thumb_b64)
        await self.emit({"type": "library_parts", "parts": self.parts.list()})
        return meta

    # ------------------------------------------------------------------ sketches (UI 2D editor)
    async def set_sketch(self, name: str, plane: dict[str, Any], items: list[dict[str, Any]], by_agent: bool = False) -> None:
        if self.model is None:
            return
        code = script_edit.set_sketch(self.model.code, name, plane, items)
        await self.build(code, source="sketch")
        desc = ", ".join(f"{it['type']}" + (" (subtract)" if it.get("mode") == "subtract" else "") for it in items) or "empty"
        if by_agent:
            await self.emit({"type": "info", "text": f"agent set sketch '{name}' ({desc})"})
            return
        self.notes.append(f"The user drew/edited sketch '{name}' in the 2D sketch editor ({desc}) on plane origin "
                          f"{plane.get('origin')} normal {plane.get('z_dir')} ({plane.get('label', '')}). It is the build123d "
                          f"Sketch variable `{name}` in the script. Do not rewrite its block; use it (extrude / cut / revolve) "
                          f"when they ask.")
        await self.emit({"type": "info", "text": f"sketch '{name}' saved to the script"})

    async def delete_sketch(self, name: str) -> None:
        if self.model is None:
            return
        try:
            code = script_edit.remove_sketch(self.model.code, name)
        except script_edit.Unsupported as e:
            await self.emit({"type": "error", "text": str(e)})
            return
        if re.search(rf"\b{re.escape(name)}\b", code):      # still used (e.g. extrude(sk1, ...)) → would break the build
            await self.emit({"type": "error", "text": f"sketch '{name}' is still used in the script; remove or replace those uses first (or ask the agent to)"})
            return
        await self.build(code, source="sketch")
        self.notes.append(f"The user deleted sketch '{name}'.")

    def sketch_defs(self) -> list[dict[str, Any]]:
        return script_edit.sketches(self.model.code) if self.model else []

    # ------------------------------------------------------------------ manual body operations (no agent turn)
    async def modify_body(self, path: str, template: str, what: str) -> None:
        """Rewrite one body's expression: join/cut an extrusion, move/rotate, etc."""
        if self.model is None:
            return
        try:
            code = script_edit.wrap_body_expr(self.model.code, path, template)
        except script_edit.Unsupported as e:
            if self.busy:
                await self.emit({"type": "error", "text": f"can't {what} on '{path}' directly ({e}) and the agent is busy"})
                return
            await self.emit({"type": "info", "text": f"can't {what} on '{path}' by editing the script directly ({e}); asking the agent"})
            await self.chat(f"{what} on body '{path}'. Keep everything else identical.")
            return
        await self.build(code, source="edit")
        self.notes.append(f"The user did '{what}' on body '{path}' via the UI (script edited directly).")
        await self.emit({"type": "info", "text": f"{what} on '{path}'"})

    async def extrude_sketch(self, name: str, amount: float, op: str = "new", body: str | None = None,
                             both: bool = False, body_name: str | None = None) -> None:
        """op: new (new body) | join (fuse into `body`) | cut (subtract from `body`)."""
        if self.model is None or not any(sk["name"] == name for sk in self.model.sketches):
            await self.emit({"type": "error", "text": f"no sketch '{name}'"}); return
        ext = f"extrude({name}, amount={amount:g}" + (", both=True" if both else "") + ")"
        if op == "new":
            await self.add_body_expr(f"{name}_body", ext, body_name or f"{name} extrude", source="edit")
        elif op == "join" and body:
            await self.modify_body(body, "({expr}) + " + ext, f"join extrude of {name} ({amount:g} mm)")
        elif op == "cut" and body:
            await self.modify_body(body, "({expr}) - " + ext, f"cut extrude of {name} ({amount:g} mm)")
        else:
            await self.emit({"type": "error", "text": "extrude: op must be new | join <body> | cut <body>"})

    async def add_primitive(self, kind: str, dims: dict[str, float], at: list[float], name: str | None = None) -> None:
        x, y, z = (float(v) for v in (at + [0, 0, 0])[:3])
        pos = f"Pos({x:g}, {y:g}, {z:g}) * " if (x or y or z) else ""
        if kind == "box":
            expr = f"{pos}Box({dims['x']:g}, {dims['y']:g}, {dims['z']:g})"
        elif kind == "cylinder":
            expr = f"{pos}Cylinder({dims['d'] / 2:g}, {dims['h']:g})"
        elif kind == "sphere":
            expr = f"{pos}Sphere({dims['d'] / 2:g})"
        else:
            await self.emit({"type": "error", "text": f"unknown primitive {kind}"}); return
        await self.add_body_expr(name or kind, expr, name or kind.capitalize(), source="edit")

    # ---- toolbar operations: face/edge based, all written as code on the body expression
    @staticmethod
    def _p(v) -> str:
        return "(" + ", ".join(f"{float(x):g}" for x in v) + ")"

    async def op_primitive(self, kind: str, dims: dict[str, float], at: list[float], normal: list[float] | None,
                           body: str | None, mode: str = "join", name: str | None = None) -> None:
        """Box/cylinder/sphere placed with its base at `at` (a clicked point on a face), oriented along `normal`.
        mode: join | cut | new (into `body`, or a new body when body is None)."""
        n = normal or [0, 0, 1]
        if kind == "box":
            prim = f"Box({dims['x']:g}, {dims['y']:g}, {dims['z']:g}, align=(Align.CENTER, Align.CENTER, Align.MIN))"
        elif kind == "cylinder":
            prim = f"Cylinder({dims['d'] / 2:g}, {dims['h']:g}, align=(Align.CENTER, Align.CENTER, Align.MIN))"
        elif kind == "sphere":
            prim = f"Sphere({dims['d'] / 2:g})"
        else:
            await self.emit({"type": "error", "text": f"unknown primitive {kind}"}); return
        expr = f"Plane(origin={self._p(at)}, z_dir={self._p(n)}) * {prim}"
        if mode == "new" or not body:
            await self.add_body_expr(name or kind, expr, name or kind.capitalize(), source="edit"); return
        op = "+" if mode == "join" else "-"
        await self.modify_body(body, f"({{expr}}) {op} {expr}", f"{mode} {kind} {dims}")

    async def op_extrude_face(self, body: str, point: list[float], normal: list[float], amount: float, mode: str = "join") -> None:
        """Press/pull: extrude the face under the clicked `point` of `body` along `normal` by `amount`
        (negative = into the body → cut)."""
        face = f"{{body}}.faces().sort_by_distance({self._p(point)})[0]"     # the clicked point lies ON the face
        cut = mode == "cut" or amount < 0
        n = [-v for v in normal] if cut else list(normal)
        tpl = f"{{body}} {'-' if cut else '+'} extrude({face}, amount={abs(amount):g}, dir={self._p(n)})"
        await self.modify_body(body, tpl, f"extrude face {amount:g} mm ({'cut' if cut else 'join'})")

    async def op_hole(self, body: str, at: list[float], normal: list[float], diameter: float, depth: float | None,
                      through: bool, thread: str | None = None, counterbore: list[float] | None = None) -> None:
        axis = self._p([-v for v in normal])
        if thread:
            tpl = f"tap({{body}}, \"{thread}\", at={self._p(at)}, " + ("through=True" if through else f"depth={depth:g}") + f", axis={axis})"
            what = f"{thread} tapped hole"
        else:
            extra = f", counterbore=({counterbore[0]:g}, {counterbore[1]:g})" if counterbore else ""
            tpl = f"hole({{body}}, {diameter:g}, at={self._p(at)}, " + ("through=True" if through else f"depth={depth:g}") + f", axis={axis}{extra})"
            what = f"Ø{diameter:g} hole"
        await self.modify_body(body, tpl, what + (" through" if through else f" {depth:g} deep"))

    async def op_edges(self, body: str, points: list[list[float]], radius: float, kind: str = "fillet",
                       face_centers: list[list[float]] | None = None) -> None:
        """Fillet/chamfer the edges nearest each point (and/or all edges of the faces nearest face_centers)."""
        sel = [f"{{body}}.edges().sort_by_distance({self._p(p)})[0]" for p in points or []]
        sel += [f"*{{body}}.faces().sort_by_distance({self._p(c)})[0].edges()" for c in face_centers or []]
        if not sel:
            await self.emit({"type": "error", "text": f"{kind}: pick at least one edge or face"}); return
        tpl = (f"{{body}}.fillet({radius:g}, [{', '.join(sel)}])" if kind == "fillet"
               else f"{{body}}.chamfer({radius:g}, None, [{', '.join(sel)}])")
        await self.modify_body(body, tpl, f"{kind} {radius:g} mm on {len(points or [])} edge(s)" + (f" + {len(face_centers)} face(s)" if face_centers else ""))

    async def op_shell(self, body: str, face_centers: list[list[float]], thickness: float) -> None:
        opens = ", ".join(f"{{body}}.faces().sort_by_distance({self._p(c)})[0]" for c in face_centers)
        tpl = f"offset({{body}}, amount=-{thickness:g}, openings=[{opens}])" if opens else f"offset({{body}}, amount=-{thickness:g})"
        await self.modify_body(body, tpl, f"shell {thickness:g} mm" + (f" open {len(face_centers)} face(s)" if face_centers else " (hollow)"))

    async def transform_body(self, path: str, move: list[float], rotate: list[float]) -> None:
        mx, my, mz = (float(v) for v in (move + [0, 0, 0])[:3]); rx, ry, rz = (float(v) for v in (rotate + [0, 0, 0])[:3])
        parts = []
        if mx or my or mz:
            parts.append(f"Pos({mx:g}, {my:g}, {mz:g})")
        if rx or ry or rz:
            parts.append(f"Rot({rx:g}, {ry:g}, {rz:g})")
        if not parts:
            return
        await self.modify_body(path, " * ".join(parts) + " * ({expr})", f"move/rotate {tuple(v for v in (mx, my, mz))} {tuple(v for v in (rx, ry, rz))}")

    # ------------------------------------------------------------------ measure / drawings
    def measure(self, faces=None, points=None, bodies=None) -> dict[str, Any]:
        if self.model is None:
            return {}
        return ck.measure(self.model, faces, points, bodies)

    async def make_drawings(self, material: str = "", density: float | None = None, sheet: str = "A4", notes: str = "") -> list[dict[str, Any]]:
        if self.model is None:
            raise ck.CadError("no model")
        name = self.design_name or "untitled"
        out_dir = self.workspace / "drawings" / safe_name(name)
        for old in out_dir.glob("*"):
            old.unlink()
        res = await asyncio.to_thread(drawing.drawings_for_model, self.model, out_dir, safe_name(name), material, density, sheet, notes)
        files = [{"body": r["body"], "svg": Path(r["svg"]).name, "dxf": Path(r["dxf"]).name if r.get("dxf") else None, "scale": r["scale"]} for r in res]
        await self.emit({"type": "drawings", "design": safe_name(name), "files": files})
        return res

    def library_payload(self) -> dict[str, Any]:
        from dataclasses import asdict
        return {"machines": [asdict(m) for m in self.library.machines().values()],
                "tools": [asdict(t) for t in self.library.tools()]}

    def _write_state(self) -> None:
        (self.workspace / "state.json").write_text(json.dumps({"design": self.design_name}))

    # ------------------------------------------------------------------ model
    async def build(self, code: str, source: str = "agent", record: bool = True) -> ck.Model:
        async with self.model_lock:
            model = await asyncio.to_thread(ck.run_script, code, self.quality, self.workspace, self.parts)
            self.model = model
            (self.workspace / "model.py").write_text(code)
            hid = None
            if record:
                hid = str(int(time.time() * 1000))
                (self.workspace / "history" / f"{hid}.py").write_text(code)
            await self.emit({"type": "model", "mesh": model.mesh, "code": code,
                             "summary": model.summary(6), "source": source, "history_id": hid,
                             "history_len": len(self._history_files())})
            await self.emit(self.design_state())
            if source == "user":
                self.notes.append("The user edited and rebuilt the script by hand in the Code panel.")
            return model

    async def set_quality(self, quality: str) -> None:
        """Re-tessellate the exact shapes for display at a different preset (no rebuild, no history)."""
        if quality not in ck.QUALITY:
            return
        self.quality = quality
        if self.model is None:
            return
        async with self.model_lock:
            m = self.model
            pairs = [(b.path, b.shape) for b in m.bodies]
            new = await asyncio.to_thread(ck.build_model, pairs, m.code, quality)
            new.stdout = m.stdout
            self.model = new
            await self.emit({"type": "model", "mesh": new.mesh, "code": new.code, "summary": new.summary(6),
                             "source": "requality", "history_id": None, "history_len": len(self._history_files())})

    async def edit_body(self, op: str, path: str, new_name: str = "") -> None:
        """Manual rename/delete from the Browser tree. Rewrites the script when the body is a literal
        dict key; otherwise hands the job to the agent."""
        if self.model is None:
            return
        code = self.model.code
        try:
            new_code = script_edit.rename(code, path, new_name) if op == "rename" else script_edit.delete(code, path)
        except script_edit.Refused as e:
            await self.emit({"type": "error", "text": f"can't {op} '{path}': {e}"})
            return
        except script_edit.Unsupported as e:
            if self.busy:
                await self.emit({"type": "error", "text": f"can't {op} '{path}' directly ({e}) and the agent is busy"})
                return
            await self.emit({"type": "info", "text": f"can't {op} '{path}' by editing the script directly ({e}); asking the agent"})
            ask = (f"Rename the body/component '{path}' to '{new_name}'. Keep everything else identical."
                   if op == "rename" else
                   f"Delete the body/component '{path}' from the design (remove it from `result` and drop code "
                   f"that only served it). Keep everything else identical.")
            await self.chat(ask)
            return
        except SyntaxError as e:
            await self.emit({"type": "error", "text": f"script has a syntax error: {e}"})
            return
        try:
            await self.build(new_code, source="edit")
        except ck.CadError as e:
            await self.emit({"type": "error", "text": f"{op} failed to build: {e}"})
            return
        what = f"renamed body '{path}' to '{new_name}'" if op == "rename" else f"deleted body '{path}'"
        self.notes.append(f"The user {what} via the Browser tree (script edited directly).")
        await self.emit({"type": "info", "text": what})

    def _history_files(self) -> list[Path]:
        return sorted((self.workspace / "history").glob("*.py"))

    async def undo(self) -> None:
        """Revert to the previous build; the undone script is moved to history/undone/."""
        files = self._history_files()
        if len(files) < 2:
            await self.emit({"type": "error", "text": "Nothing to undo."})
            return
        undone, prev = files[-1], files[-2]
        undone_dir = self.workspace / "history" / "undone"
        undone_dir.mkdir(exist_ok=True)
        undone.rename(undone_dir / undone.name)
        try:
            await self.build(prev.read_text(), source="revert", record=False)
            self.notes.append("The user pressed Undo; the script reverted to the previous build.")
        except ck.CadError as e:
            await self.emit({"type": "error", "text": f"undo failed: {e}"})

    def load_initial(self) -> None:
        state_path = self.workspace / "state.json"
        name = None
        if state_path.exists():
            try:
                name = json.loads(state_path.read_text()).get("design")
            except Exception:
                name = None
        # model.py is the working copy (may hold unsaved edits); the design file is what was last saved
        path = self.workspace / "model.py"
        code = path.read_text() if path.exists() else None
        if name and (self.designs_dir / f"{name}.py").exists():
            self.design_name = name
            self.saved_code = (self.designs_dir / f"{name}.py").read_text()
            code = code or self.saved_code
        fresh = code is None
        code = code or ck.NEW_DESIGN_CODE          # first run: an empty design, not a demo
        try:
            self.model = ck.run_script(code, self.quality, self.workspace, self.parts)
        except ck.CadError:
            code = ck.NEW_DESIGN_CODE
            self.model = ck.run_script(code, self.quality, self.workspace, self.parts)
            self.design_name, self.saved_code = None, None
        if fresh and self.design_name is None:
            self.saved_code = code                 # an untouched empty design is not unsaved work
        (self.workspace / "model.py").write_text(code)
        if self.cam_code.strip():          # rebuild the CAM program from the working copy
            try:
                self.program = self._run_cam(self.cam_code)
            except Exception:  # noqa: BLE001
                self.program = None
        if not self._history_files():
            (self.workspace / "history" / f"{int(time.time() * 1000)}.py").write_text(code)

    # ------------------------------------------------------------------ tools
    def _make_server(self):
        agent = self

        @tool("build_model",
              "Replace the design script with a complete build123d script and rebuild it. The script must "
              "assign `result` (a shape, or a dict of name -> shape / nested dict for bodies and components). "
              "Returns a geometry summary or the Python traceback.",
              {"code": str})
        async def build_model(args: dict[str, Any]) -> dict[str, Any]:
            try:
                model = await agent.build(args["code"])
            except ck.CadError as e:
                return {"content": [{"type": "text", "text": f"BUILD FAILED\n{e}"}], "is_error": True}
            except Exception as e:  # noqa: BLE001
                return {"content": [{"type": "text", "text": f"BUILD FAILED\n{type(e).__name__}: {e}"}],
                        "is_error": True}
            return {"content": [{"type": "text", "text": model.summary(25)}]}

        @tool("inspect_model",
              "List bodies and faces of the current design. Faces have id, body, type, area, centre, normal, "
              "size. Optional `body` (name or path) and `kind` (PLANE, CYLINDER, CONE, SPHERE, TORUS, "
              "BSPLINE...) filters. Optional `near` [x,y,z] sorts by distance to that point.",
              {"type": "object",
               "properties": {"body": {"type": "string"},
                              "kind": {"type": "string"},
                              "near": {"type": "array", "items": {"type": "number"}},
                              "limit": {"type": "integer", "default": 200}},
               "required": []})
        async def inspect_model(args: dict[str, Any]) -> dict[str, Any]:
            m = agent.model
            if m is None:
                return {"content": [{"type": "text", "text": "No model built yet."}], "is_error": True}
            faces = m.faces
            if args.get("body"):
                b = m.body_by_name(args["body"])
                if b is None:
                    return {"content": [{"type": "text", "text": f"no body '{args['body']}'. Bodies: "
                                         + ", ".join(x.path for x in m.bodies)}], "is_error": True}
                faces = [f for f in faces if f.body == b.id]
            if args.get("kind"):
                faces = [f for f in faces if f.kind == args["kind"].upper()]
            if args.get("near"):
                p = args["near"]
                faces = sorted(faces, key=lambda f: sum((a - b) ** 2 for a, b in zip(f.center, p)))
            else:
                faces = sorted(faces, key=lambda f: f.id)
            limit = int(args.get("limit") or 200)
            lines = [m.summary(0), f"{len(faces)} faces:"] + ["  " + f.summary() for f in faces[:limit]]
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        @tool("screenshot",
              "Render the current design in the browser viewer and return a PNG. `view` is one of "
              + ", ".join(SCREENSHOT_VIEWS) + ". `highlight_faces` (list of face ids) colours those faces "
              "orange so you can confirm which is which. `show_edges` defaults to true.",
              {"type": "object",
               "properties": {"view": {"type": "string", "enum": SCREENSHOT_VIEWS, "default": "iso"},
                              "highlight_faces": {"type": "array", "items": {"type": "integer"}},
                              "show_edges": {"type": "boolean", "default": True}},
               "required": []})
        async def screenshot(args: dict[str, Any]) -> dict[str, Any]:
            if agent.model is None:
                return {"content": [{"type": "text", "text": "No model built yet."}], "is_error": True}
            try:
                png_b64 = await agent.screenshot_fn({
                    "view": args.get("view") or "iso",
                    "highlight": args.get("highlight_faces") or [],
                    "showEdges": args.get("show_edges", True),
                })
            except Exception as e:  # noqa: BLE001
                return {"content": [{"type": "text", "text": f"screenshot unavailable: {e}"}], "is_error": True}
            return {"content": [
                {"type": "image", "data": png_b64, "mimeType": "image/jpeg"},
                {"type": "text", "text": f"view={args.get('view') or 'iso'} highlighted={args.get('highlight_faces') or []} "
                                         f"(X red, Y green, Z blue axis gizmo; grid is the XY plane)"},
            ]}

        @tool("export_model",
              "Export the design to the workspace exports folder. STEP is the exact B-rep (bodies stay separate "
              "solids). STL is tessellated at `tolerance` (max chordal deviation, mm, default 0.01) and "
              "`angular_tolerance` (rad, default 0.05 ≈ 3°). Optional `body` exports a single body.",
              {"type": "object",
               "properties": {"name": {"type": "string"},
                              "formats": {"type": "array", "items": {"type": "string"}, "default": ["step", "stl"]},
                              "tolerance": {"type": "number", "default": 0.01},
                              "angular_tolerance": {"type": "number", "default": 0.05},
                              "body": {"type": "string"}},
               "required": []})
        async def export_model(args: dict[str, Any]) -> dict[str, Any]:
            if agent.model is None:
                return {"content": [{"type": "text", "text": "No model built yet."}], "is_error": True}
            try:
                paths = await asyncio.to_thread(ck.export, agent.model, agent.workspace / "exports",
                                                args.get("name") or agent.design_name or "model",
                                                args.get("formats") or ["step", "stl"],
                                                float(args.get("tolerance") or 0.01),
                                                float(args.get("angular_tolerance") or 0.05),
                                                args.get("body") or None)
            except Exception as e:  # noqa: BLE001
                return {"content": [{"type": "text", "text": f"export failed: {e}"}], "is_error": True}
            await agent.emit({"type": "exported", "files": [p.name for p in paths]})
            return {"content": [{"type": "text", "text": "exported: " + ", ".join(str(p) for p in paths)}]}

        @tool("get_code", "Return the current design script.", {})
        async def get_code(args: dict[str, Any]) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": agent.model.code if agent.model else ""}]}

        @tool("save_design", "Save the current design script to the designs folder under `name` "
              "(defaults to the current design name). Only call when the user asks to save.",
              {"type": "object", "properties": {"name": {"type": "string"}}, "required": []})
        async def save_design(args: dict[str, Any]) -> dict[str, Any]:
            try:
                name = await agent.save_design(args.get("name"))
            except ck.CadError as e:
                return {"content": [{"type": "text", "text": str(e)}], "is_error": True}
            return {"content": [{"type": "text", "text": f"saved as '{name}'"}]}

        @tool("cam_context", "Machines, tool library, model facts for CAM (bbox, bodies, holes, planar levels) "
              "and the current CAM program summary. Call before writing a CAM script.", {})
        async def cam_context(args: dict[str, Any]) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": agent.cam_context()}]}

        @tool("build_cam", "Replace the CAM script with a complete script and build the toolpaths. The script must "
              "assign `program` (a cam_kernel Program). Returns the program summary (ops, times, warnings) or traceback.",
              {"code": str})
        async def build_cam(args: dict[str, Any]) -> dict[str, Any]:
            try:
                prog = await agent.set_cam_code(args["code"], rebuild=True)
            except cam.CamError as e:
                return {"content": [{"type": "text", "text": f"CAM BUILD FAILED\n{e}"}], "is_error": True}
            except Exception as e:  # noqa: BLE001
                return {"content": [{"type": "text", "text": f"CAM BUILD FAILED\n{type(e).__name__}: {e}"}], "is_error": True}
            return {"content": [{"type": "text", "text": prog.summary()}]}

        @tool("get_cam_code", "Return the current CAM script.", {})
        async def get_cam_code(args: dict[str, Any]) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": agent.cam_code or "(no CAM script yet)"}]}

        @tool("export_gcode", "Write the current program as GRBL G-code to workspace/exports/<name>.nc.",
              {"type": "object", "properties": {"name": {"type": "string"}}, "required": []})
        async def export_gcode(args: dict[str, Any]) -> dict[str, Any]:
            if agent.program is None:
                return {"content": [{"type": "text", "text": "no CAM program built"}], "is_error": True}
            name = safe_name(args.get("name") or agent.design_name or "program")
            path = agent.workspace / "exports" / f"{name}.nc"
            g = agent.program.gcode()
            path.write_text(g)
            await agent.emit({"type": "exported", "files": [path.name]})
            lines = g.splitlines()
            counts = {k: sum(1 for l in lines if l.startswith(k + " ")) for k in ("G0", "G1", "G2", "G3")}
            return {"content": [{"type": "text", "text": f"wrote {path} ({path.stat().st_size} bytes, {len(lines)} lines): "
                                 + ", ".join(f"{k} ×{v}" for k, v in counts.items())
                                 + f"; tool changes: {sum(1 for l in lines if l.startswith('M6'))}; est. {agent.program.time_minutes():.1f} min"}]}

        @tool("feeds_speeds", "Feeds & speeds calculator: rpm, feed, plunge, stepdown, stepover for a tool in a material "
              "on a machine (spindle/feed limits applied, radial chip thinning if `radial_engagement` < 0.5). "
              "`tool` is a tool number or name; `material` one of the known materials. Optionally `apply` to save into the tool library.",
              {"type": "object", "properties": {"tool": {"type": "string"}, "material": {"type": "string"},
                                                "machine": {"type": "string"}, "aggressiveness": {"type": "number", "default": 0.5},
                                                "radial_engagement": {"type": "number"}, "apply": {"type": "boolean", "default": False}},
               "required": ["tool", "material"]})
        async def feeds_speeds(args: dict[str, Any]) -> dict[str, Any]:
            tm = agent.library.tool_map()
            key = str(args["tool"]).strip()
            digits = key[1:] if key[:1] in "Tt" and key[1:].isdigit() else key
            t = tm.get(key) or tm.get(key.upper()) or (tm.get(int(digits)) if digits.isdigit() else None) \
                or next((x for x in tm.values() if getattr(x, "name", "").lower() == key.lower()), None)
            if t is None:
                return {"content": [{"type": "text", "text": f"unknown tool '{key}'. Tools: " + ", ".join(x.label() for x in agent.library.tools())}], "is_error": True}
            machines = agent.library.machines()
            m = machines.get(args.get("machine") or "") or (agent.program.setup.machine if agent.program else next(iter(machines.values()), None))
            try:
                f = cam.feeds(t, args["material"], m, float(args.get("aggressiveness") or 0.5), args.get("radial_engagement"))
            except cam.CamError as e:
                return {"content": [{"type": "text", "text": str(e)}], "is_error": True}
            txt = json.dumps(f, indent=1)
            if args.get("apply"):
                nt = cam.apply_feeds(t, args["material"], m, aggressiveness=float(args.get("aggressiveness") or 0.5),
                                     radial_engagement=args.get("radial_engagement"))
                from dataclasses import asdict
                agent.library.save_tool(asdict(nt))
                await agent.emit({"type": "library", **agent.library_payload()})
                txt += f"\napplied to {nt.label()} in the tool library"
            return {"content": [{"type": "text", "text": txt}]}

        @tool("save_machine", "Create or update a machine definition. `machine` is a JSON object with fields: name, "
              "controller, units, travel{x,y,z}, max_feed{x,y,z}, rapid, spindle{min,max}, tool_change (pause|none|split), "
              "coolant, safe_z, clearance_z, program_start[], program_end[], notes.",
              {"type": "object", "properties": {"machine": {"type": "object"}}, "required": ["machine"]})
        async def save_machine(args: dict[str, Any]) -> dict[str, Any]:
            try:
                m = agent.library.save_machine(args["machine"])
            except Exception as e:  # noqa: BLE001
                return {"content": [{"type": "text", "text": f"invalid machine: {e}"}], "is_error": True}
            await agent.emit({"type": "library", **agent.library_payload()})
            return {"content": [{"type": "text", "text": f"saved machine '{m.name}'"}]}

        @tool("save_tool", "Create or update a tool (same number replaces). `tool` fields: number, name, type "
              "(flat|ball|vbit|drill|chamfer), diameter, flutes, flute_length, rpm, feed, plunge, stepdown, stepover "
              "(fraction of diameter), angle, notes.",
              {"type": "object", "properties": {"tool": {"type": "object"}}, "required": ["tool"]})
        async def save_tool(args: dict[str, Any]) -> dict[str, Any]:
            try:
                t = agent.library.save_tool(args["tool"])
            except Exception as e:  # noqa: BLE001
                return {"content": [{"type": "text", "text": f"invalid tool: {e}"}], "is_error": True}
            await agent.emit({"type": "library", **agent.library_payload()})
            return {"content": [{"type": "text", "text": f"saved {t.label()}"}]}

        @tool("get_parameters", "The design's editable numeric parameters (top-level `name = number` assignments).", {})
        async def get_parameters(args: dict[str, Any]) -> dict[str, Any]:
            ps = agent.get_params()
            return {"content": [{"type": "text", "text": "\n".join(f"{p['name']} = {p['value']}" + (f"  # {p['comment']}" if p['comment'] else "") for p in ps) or "(no numeric parameters)"}]}

        @tool("set_parameters", "Change numeric parameters in place and rebuild (cheaper than rewriting the whole script). `values`: {name: number}.",
              {"type": "object", "properties": {"values": {"type": "object"}}, "required": ["values"]})
        async def set_parameters(args: dict[str, Any]) -> dict[str, Any]:
            try:
                await agent.set_params({k: float(v) for k, v in args["values"].items()}, source="agent")
            except Exception as e:  # noqa: BLE001
                return {"content": [{"type": "text", "text": f"failed: {e}"}], "is_error": True}
            return {"content": [{"type": "text", "text": agent.model.summary(6) if agent.model else "no model"}]}

        @tool("measure", "Measure between up to two entities: face ids, edge ids (from the viewer's measure tool or inspect), "
              "[x,y,z] points, and/or two body names. Returns entity properties (edge length/radius/centre, face area...), minimum "
              "distance with closest points, angle (line-line, line-plane, plane-plane) with parallel/perpendicular relation, "
              "circle centre spacing, plane gap, body mass properties and clearance/interference.",
              {"type": "object", "properties": {"faces": {"type": "array", "items": {"type": "integer"}},
                                                "edges": {"type": "array", "items": {"type": "integer"}},
                                                "points": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}},
                                                "bodies": {"type": "array", "items": {"type": "string"}}}, "required": []})
        async def measure_tool(args: dict[str, Any]) -> dict[str, Any]:
            if agent.model is None:
                return {"content": [{"type": "text", "text": "no model"}], "is_error": True}
            try:
                bodies = []
                for b in args.get("bodies") or []:
                    bb = agent.model.body_by_name(b)
                    if bb is None:
                        return {"content": [{"type": "text", "text": f"no body '{b}'"}], "is_error": True}
                    bodies.append(bb.id)
                res = ck.measure(agent.model, args.get("faces"), args.get("points"), bodies, args.get("edges"))
            except Exception as e:  # noqa: BLE001
                return {"content": [{"type": "text", "text": f"measure failed: {e}"}], "is_error": True}
            return {"content": [{"type": "text", "text": json.dumps(res, indent=1)}]}

        @tool("mass_properties", "Volume, mass, centre of mass, bbox and surface area of a body (or all). `density` in g/cm³ or a material name "
              "(aluminium, steel, stainless, brass, copper, titanium, pla, petg, abs, nylon, acrylic, hdpe, delrin, plywood, mdf, hardwood, softwood, foam).",
              {"type": "object", "properties": {"body": {"type": "string"}, "density": {"type": ["number", "string"], "default": "aluminium"}}, "required": []})
        async def mass_properties(args: dict[str, Any]) -> dict[str, Any]:
            if agent.model is None:
                return {"content": [{"type": "text", "text": "no model"}], "is_error": True}
            try:
                res = ck.mass_properties(agent.model, args.get("body"), args.get("density") or "aluminium")
            except Exception as e:  # noqa: BLE001
                return {"content": [{"type": "text", "text": f"failed: {e}"}], "is_error": True}
            return {"content": [{"type": "text", "text": json.dumps(res, indent=1)}]}

        @tool("make_drawings", "Generate shop drawings (SVG + DXF): third-angle front/top/right views + iso, hidden lines, overall "
              "dimensions, hole callouts and a hole table, title block. One sheet per body (+ assembly). Files go to workspace/drawings/<design>/.",
              {"type": "object", "properties": {"material": {"type": "string"}, "density": {"type": "number"},
                                                "sheet": {"type": "string", "enum": ["A4", "A3"], "default": "A4"}, "notes": {"type": "string"}}, "required": []})
        async def make_drawings(args: dict[str, Any]) -> dict[str, Any]:
            try:
                res = await agent.make_drawings(args.get("material") or "", args.get("density"), args.get("sheet") or "A4", args.get("notes") or "")
            except Exception as e:  # noqa: BLE001
                return {"content": [{"type": "text", "text": f"failed: {e}"}], "is_error": True}
            lines = [f"{r['body']}: {r['svg']} (scale {r['scale']}), views " + ", ".join(f"{k} {v['w']:.1f}×{v['h']:.1f} {v['holes']} holes" for k, v in r["views"].items())
                     + (f"; dxf {r['dxf']}" if r.get("dxf") else "") for r in res]
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        @tool("library", "The part library (reusable parts: parametric scripts or exact STEP bodies). Check it before modelling a "
              "standard or previously made part. action=list | search (query over name/description/tags) | get (name: metadata, "
              "parameters, script) | insert (name, params, body_name: add the part to the design as a new body — the script gets a "
              "from_library(...) line; position it afterwards with Pos/moved) | save_design (name, description, tags: save the whole "
              "current script as a parametric part) | save_body (name, body, description, tags: save one body as an exact STEP part) | delete (name).",
              {"type": "object", "properties": {"action": {"type": "string", "enum": ["list", "search", "get", "insert", "save_design", "save_body", "delete"]},
                                                "name": {"type": "string"}, "query": {"type": "string"}, "body": {"type": "string"},
                                                "body_name": {"type": "string"}, "params": {"type": "object"}, "description": {"type": "string"},
                                                "tags": {"type": "array", "items": {"type": "string"}}}, "required": ["action"]})
        async def library_tool(args: dict[str, Any]) -> dict[str, Any]:
            a = args["action"]
            try:
                if a == "list":
                    return {"content": [{"type": "text", "text": agent.parts.describe()}]}
                if a == "search":
                    q = (args.get("query") or "").lower()
                    hits = [m for m in agent.parts.list() if q in " ".join([m["name"], m.get("description", ""), " ".join(m.get("tags", [])), m["kind"]]).lower()]
                    return {"content": [{"type": "text", "text": ("\n".join(f"- {m['name']} [{m['kind']}]" + (f" params {m['params']}" if m.get('params') else "") + (f" — {m['description']}" if m.get('description') else "") for m in hits) or "no matching parts")}]}
                if a == "insert":
                    await agent.insert_library_part(args.get("name") or "", args.get("params") or None, args.get("body_name"))
                    return {"content": [{"type": "text", "text": agent.model.summary(0) if agent.model else "inserted"}]}
                if a == "get":
                    m = agent.parts.get(args.get("name") or "")
                    code = agent.parts.code_for(args.get("name") or "")
                    return {"content": [{"type": "text", "text": json.dumps(m, indent=1) + ("\n" + code if code else "")}]}
                if a == "delete":
                    ok = agent.parts.delete(args.get("name") or "")
                    await agent.emit({"type": "library_parts", "parts": agent.parts.list()})
                    return {"content": [{"type": "text", "text": "deleted" if ok else "not found"}]}
                if a not in ("save_design", "save_body"):
                    return {"content": [{"type": "text", "text": f"unknown library action {a!r}; use list | search | get | insert | save_design | save_body | delete"}], "is_error": True}
                meta = await agent.add_to_library(args.get("name") or "part", args.get("body") if a == "save_body" else None,
                                                  args.get("description") or "", args.get("tags") or [], None)
                return {"content": [{"type": "text", "text": f"saved library part '{meta['name']}' ({meta['kind']})" + (f" params {meta['params']}" if meta.get('params') else "")}]}
            except Exception as e:  # noqa: BLE001
                return {"content": [{"type": "text", "text": f"failed: {e}"}], "is_error": True}

        @tool("sketch", "Read or edit UI-editable sketches (the `# sketch:NAME {...}` blocks). action=list | get (name) | "
              "set (name, plane, items: creates or replaces the block; the user can then edit it graphically) | delete (name). "
              "plane = {origin:[x,y,z], x_dir:[..], z_dir:[..], label}; items = list of {type:'rect',cx,cy,w,h,angle} | "
              "{type:'circle',cx,cy,r} | {type:'polygon',pts:[[x,y],..]} | {type:'slot',x1,y1,x2,y2,w}, each with mode 'add'|'subtract'; "
              "coordinates are plane-local mm. Prefer this over rewriting sketch blocks by hand.",
              {"type": "object", "properties": {"action": {"type": "string", "enum": ["list", "get", "set", "delete"]},
                                                "name": {"type": "string"}, "plane": {"type": "object"},
                                                "items": {"type": "array", "items": {"type": "object"}}}, "required": ["action"]})
        async def sketch_tool(args: dict[str, Any]) -> dict[str, Any]:
            a = args["action"]
            try:
                if a == "list":
                    defs = agent.sketch_defs()
                    live = {sk["name"]: sk for sk in (agent.model.sketches if agent.model else [])}
                    lines = [f"{d['name']}: plane {d['plane']} · {len(d['items'])} item(s)" + (f" · {live[d['name']]['faces']} face(s), area {live[d['name']]['area']}" if d['name'] in live else " · (not built)") for d in defs]
                    code_only = [n for n in live if n not in {d['name'] for d in defs}]
                    if code_only:
                        lines.append("code-only Sketch variables (no editable block): " + ", ".join(code_only))
                    return {"content": [{"type": "text", "text": "\n".join(lines) or "no sketches"}]}
                if a == "get":
                    d = next((d for d in agent.sketch_defs() if d["name"] == args.get("name")), None)
                    return {"content": [{"type": "text", "text": json.dumps(d, indent=1) if d else f"no editable sketch '{args.get('name')}'"}]}
                if a == "delete":
                    await agent.delete_sketch(args.get("name") or "")
                    return {"content": [{"type": "text", "text": "deleted"}]}
                name = args.get("name") or "sketch1"
                plane = args.get("plane")
                if plane is None:
                    d = next((d for d in agent.sketch_defs() if d["name"] == name), None)
                    if d is None:
                        return {"content": [{"type": "text", "text": "set needs a plane for a new sketch"}], "is_error": True}
                    plane = d["plane"]
                for k in ("origin", "x_dir", "z_dir"):
                    if k not in plane:
                        return {"content": [{"type": "text", "text": f"plane needs {k}"}], "is_error": True}
                plane.setdefault("label", "agent")
                await agent.set_sketch(name, plane, list(args.get("items") or []), by_agent=True)
                sk = next((s for s in agent.model.sketches if s["name"] == name), None)
                return {"content": [{"type": "text", "text": f"sketch '{name}' set: " + (f"{sk['faces']} face(s), area {sk['area']}" if sk else "built with no faces (check item modes/overlaps)")}]}
            except (ck.CadError, script_edit.Unsupported) as e:
                return {"content": [{"type": "text", "text": f"sketch failed: {e}"}], "is_error": True}

        tools = [build_model, inspect_model, screenshot, export_model, get_code, save_design,
                 cam_context, build_cam, get_cam_code, export_gcode, feeds_speeds, save_machine, save_tool,
                 get_parameters, set_parameters, measure_tool, mass_properties, make_drawings, library_tool, sketch_tool]
        self.tool_handlers = {t.name: t.handler for t in tools}     # name -> async handler (tests call these directly)
        return create_sdk_mcp_server("cad", "0.4.0", tools=tools)

    # ------------------------------------------------------------------ settings (model, effort, MCP servers)
    DEFAULT_SETTINGS: dict[str, Any] = {
        "model": "claude-opus-5-5",        # default model; "" = the Claude Code default
        "effort": "",                      # "" = default; low | medium | high | xhigh | max
        "max_turns": 60,
        "thinking_display": "omitted",     # omitted | summarized (shows the thinking summary in chat)
        "web": True,                       # allow WebFetch / WebSearch
        "mcp_servers": {},                 # name -> {type: stdio|http|sse, command, args, env, url, headers, enabled}
        "extra_prompt": "",                # appended to the system prompt (house rules, machine notes...)
    }
    MODELS = ["claude-opus-5-5", "claude-fable-5-1", "claude-sonnet-5", "claude-opus-5", "claude-opus-4-8", "claude-haiku-4-5"]

    def _settings_path(self) -> Path:
        return self.workspace / "settings.json"

    def _load_settings(self) -> dict[str, Any]:
        st = dict(self.DEFAULT_SETTINGS)
        try:
            st.update(json.loads(self._settings_path().read_text()))
        except Exception:
            pass
        return st

    def save_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        st = {**self.settings, **{k: v for k, v in patch.items() if k in self.DEFAULT_SETTINGS}}
        if st.get("effort") not in ("", "low", "medium", "high", "xhigh", "max"):
            raise ValueError("effort must be one of low, medium, high, xhigh, max")
        st["max_turns"] = int(st.get("max_turns") or 60)
        for name, cfg in (st.get("mcp_servers") or {}).items():
            t = cfg.get("type") or ("http" if cfg.get("url") else "stdio")
            if t == "stdio" and not cfg.get("command"):
                raise ValueError(f"MCP server '{name}': stdio servers need a command")
            if t in ("http", "sse") and not cfg.get("url"):
                raise ValueError(f"MCP server '{name}': {t} servers need a url")
            cfg["type"] = t
        self.settings = st
        self._settings_path().write_text(json.dumps(st, indent=2))
        return st

    def _mcp_configs(self) -> dict[str, Any]:
        out: dict[str, Any] = {"cad": self._make_server()}
        for name, cfg in (self.settings.get("mcp_servers") or {}).items():
            if not cfg.get("enabled", True) or name == "cad":
                continue
            t = cfg.get("type", "stdio")
            if t == "stdio":
                c: dict[str, Any] = {"type": "stdio", "command": cfg["command"]}
                if cfg.get("args"): c["args"] = list(cfg["args"])
                if cfg.get("env"): c["env"] = dict(cfg["env"])
            else:
                c = {"type": t, "url": cfg["url"]}
                if cfg.get("headers"): c["headers"] = dict(cfg["headers"])
            out[name] = c
        return out

    async def agent_status(self) -> dict[str, Any]:
        st: dict[str, Any] = {"connected": self.client is not None, "busy": self.busy, "session_id": self.session_id, "cli": find_claude_cli(),
                              "model": self.settings.get("model") or "(default)", "effort": self.settings.get("effort") or "(default)",
                              "mcp": []}
        if self.client is not None:
            try:
                res = await asyncio.wait_for(self.client.get_mcp_status(), 5)
                for srv in res.get("mcpServers", []):
                    st["mcp"].append({"name": srv.get("name"), "status": srv.get("status"),
                                      "info": (srv.get("serverInfo") or {}).get("name"), "error": srv.get("error")})
            except Exception as e:  # noqa: BLE001
                st["mcp_error"] = f"{type(e).__name__}: {e}"
        return st

    async def restart(self) -> None:
        """Apply settings: tear the Claude session down and start a fresh one (conversation is lost)."""
        if self.busy:
            await self.interrupt()
        await self.stop()
        self.client = None
        await self.start()
        self.notes.append("The app restarted your session with new settings; the chat history before this point is not available to you.")
        await self.emit({"type": "agent_ready"})
        await self.emit({"type": "info", "text": f"agent restarted · model {self.settings.get('model') or 'default'}"
                         + (f" · effort {self.settings['effort']}" if self.settings.get("effort") else "")})

    # ------------------------------------------------------------------ session
    async def start(self) -> None:
        st = self.settings
        mcp = self._mcp_configs()
        allowed = ["mcp__cad__*"] + [f"mcp__{n}__*" for n in mcp if n != "cad"]
        if st.get("web", True):
            allowed += ["WebFetch", "WebSearch"]
        prompt = SYSTEM_PROMPT
        if len(mcp) > 1:
            prompt += "\n\nExternal MCP servers connected (their tools are prefixed mcp__<server>__): " + ", ".join(n for n in mcp if n != "cad") + "."
        if st.get("extra_prompt"):
            prompt += "\n\nAdditional instructions from the user:\n" + st["extra_prompt"]
        kw: dict[str, Any] = {}
        if st.get("effort"):
            kw["effort"] = st["effort"]
        if st.get("thinking_display") == "summarized":
            kw["thinking"] = {"type": "adaptive", "display": "summarized"}
        options = ClaudeAgentOptions(
            system_prompt=prompt,
            mcp_servers=mcp,
            allowed_tools=allowed,
            permission_mode="dontAsk",
            include_partial_messages=True,
            cwd=str(self.workspace),
            setting_sources=[],
            model=st.get("model") or os.environ.get("AGENTICCAD_MODEL") or None,
            max_turns=int(st.get("max_turns") or 60),
            # keep the CAD tools loaded up front instead of deferred behind ToolSearch
            env={"ENABLE_TOOL_SEARCH": "false"},
            max_buffer_size=8 * 1024 * 1024,   # screenshots + big tool results (default 1 MB is too small)
            **kw,
        )
        cli = find_claude_cli()                 # not bundled in the desktop app; GUI PATH is minimal
        if cli:
            options.cli_path = cli
        self.client = ClaudeSDKClient(options=options)
        await self.client.connect()

    async def stop(self) -> None:
        if self.client:
            await self.client.disconnect()

    async def interrupt(self) -> None:
        if self.client and self.busy:
            await self.client.interrupt()

    def _expand_selection(self, selection: Any) -> str:
        if not self.model or not selection:
            return ""
        if isinstance(selection, list):
            selection = {"faces": selection, "bodies": []}
        lines = []
        by_body = {b.id: b for b in self.model.bodies}
        for bid in selection.get("bodies") or []:
            if bid in by_body:
                lines.append(by_body[bid].summary())
        by_id = {f.id: f for f in self.model.faces}
        for fid in selection.get("faces") or []:
            if fid in by_id:
                lines.append(by_id[fid].summary())
        for name in selection.get("sketches") or []:
            for sk in self.model.sketches:
                if sk["name"] == name:
                    lines.append(f"sketch `{name}`: {sk['faces']} face(s), area {sk['area']:.1f}mm², plane origin ({ck.fmt(sk['origin'])}) "
                                 f"normal ({ck.fmt(sk['normal'])}) — a build123d Sketch variable in the script")
        return "\n\n[Selected geometry]\n" + "\n".join(lines) if lines else ""

    def _save_images(self, images: list[dict[str, Any]]) -> list[tuple[Path, str, str]]:
        """Persist uploaded images (base64 / data URLs) to workspace/images; returns (path, media_type, b64)."""
        out = []
        img_dir = self.workspace / "images"
        img_dir.mkdir(exist_ok=True)
        import base64
        for i, im in enumerate(images or []):
            data = im.get("data") or ""
            mime = im.get("mime") or "image/jpeg"
            if data.startswith("data:") and "," in data[:80]:
                header, data = data.split(",", 1)
                mime = header[5:].split(";")[0] or mime
            if mime not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
                mime = "image/jpeg"
            ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}[mime]
            name = safe_name(Path(im.get("name") or f"image{i + 1}").stem) or f"image{i + 1}"
            path = img_dir / f"{int(time.time() * 1000)}_{i}_{name}.{ext}"
            try:
                path.write_bytes(base64.b64decode(data))
            except Exception:
                continue
            out.append((path, mime, data))
        return out

    async def chat(self, text: str, selection: Any = None, images: list[dict[str, Any]] | None = None) -> None:
        if self.client is None:
            if os.environ.get("AGENTICCAD_NO_AGENT") == "1":
                await self.emit({"type": "error", "text": "The agent is disabled on this server (AGENTICCAD_NO_AGENT=1); manual tools still work."})
                return
            if find_claude_cli() is None:
                await self.emit({"type": "agent_missing_cli"})
                await self.emit({"type": "error", "text": "Claude Code is not installed, so the agent can't answer. Manual tools still work; see the setup page."})
                return
            try:
                await self.start()
            except Exception as e:  # noqa: BLE001
                await self.emit({"type": "error", "text": f"agent failed to start: {e}"})
                return
        if self.busy:
            await self.emit({"type": "error", "text": "Agent is busy; press Stop first."})
            return
        prompt = text + self._expand_selection(selection)
        saved = self._save_images(images or [])
        if saved:
            prompt += "\n\n[Attached images: " + ", ".join(f"{p.name} ({p})" for p, _, _ in saved) + \
                      " — shown below; use them as reference for shape and proportions, dimensions in the text take precedence]"
        if self.notes:
            prompt = "".join(f"[Note] {n}\n" for n in self.notes) + "\n" + prompt
            self.notes.clear()
        if self.design_name:
            prompt = f"[Design: '{self.design_name}']\n" + prompt
        parts = self.parts.list()
        if parts:
            prompt = "[Library parts available: " + ", ".join(m["name"] for m in parts[:30]) + " — use the `library` tool to search/insert]\n" + prompt
        self.busy = True
        await self.emit({"type": "status", "busy": True})
        try:
            if saved:
                content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
                for _, mime, b64 in saved:
                    content.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}})

                async def one_message():
                    yield {"type": "user", "message": {"role": "user", "content": content}, "parent_tool_use_id": None}

                await self.client.query(one_message())
            else:
                await self.client.query(prompt)
            await self._pump()
        except Exception as e:  # noqa: BLE001
            await self.emit({"type": "error", "text": f"{type(e).__name__}: {e}"})
        finally:
            self.busy = False
            await self.emit({"type": "status", "busy": False})

    async def _pump(self) -> None:
        assert self.client
        tool_names: dict[str, str] = {}
        async for msg in self.client.receive_response():
            if isinstance(msg, StreamEvent):
                ev = msg.event
                if ev.get("type") == "content_block_delta":
                    d = ev.get("delta", {})
                    if d.get("type") == "text_delta":
                        await self.emit({"type": "delta", "text": d.get("text", "")})
                    elif d.get("type") == "thinking_delta" and d.get("thinking"):
                        await self.emit({"type": "thinking", "text": d["thinking"]})
                elif ev.get("type") == "content_block_start":
                    blk = ev.get("content_block", {})
                    if blk.get("type") == "tool_use":
                        await self.emit({"type": "tool_start", "id": blk.get("id"), "name": short(blk.get("name", ""))})
            elif isinstance(msg, AssistantMessage):
                for blk in msg.content:
                    if isinstance(blk, TextBlock):
                        await self.emit({"type": "text", "text": blk.text})
                    elif isinstance(blk, ToolUseBlock):
                        tool_names[blk.id] = blk.name
                        await self.emit({"type": "tool_use", "id": blk.id, "name": short(blk.name),
                                         "input": blk.input})
            elif isinstance(msg, UserMessage):
                content = msg.content if isinstance(msg.content, list) else []
                for blk in content:
                    if isinstance(blk, ToolResultBlock):
                        await self.emit({"type": "tool_result", "id": blk.tool_use_id,
                                         "name": short(tool_names.get(blk.tool_use_id, "")),
                                         "text": result_text(blk.content), "is_error": bool(blk.is_error)})
            elif isinstance(msg, ResultMessage):
                self.session_id = msg.session_id
                await self.emit({"type": "result", "subtype": msg.subtype, "cost": msg.total_cost_usd,
                                 "duration_ms": msg.duration_ms, "turns": msg.num_turns,
                                 "session_id": msg.session_id})
            elif isinstance(msg, SystemMessage):
                pass


def _thumbnail(b64_jpeg: str, size: int) -> str | None:
    try:
        import base64, io
        from PIL import Image
        im = Image.open(io.BytesIO(base64.b64decode(b64_jpeg))).convert("RGB")
        w, h = im.size
        s = max(size / w, size / h)
        im = im.resize((max(1, int(w * s)), max(1, int(h * s))))
        left, top = (im.width - size) // 2, (im.height - size) // 2
        im = im.crop((left, top, left + size, top + size))
        buf = io.BytesIO(); im.save(buf, "JPEG", quality=80)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


def short(name: str) -> str:
    return name.replace("mcp__cad__", "")


def result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                if c.get("type") == "text":
                    parts.append(c.get("text", ""))
                elif c.get("type") == "image":
                    parts.append("[image]")
        return "\n".join(parts)
    return json.dumps(content)[:2000] if content is not None else ""
