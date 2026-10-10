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

import build123d as b3d
import cad_kernel as ck
import cam_kernel as cam
import drawing
from cam_data import Library
from library import PartLibrary, slug as lib_slug
import script_edit
import extensions as ext_mod
import slicer
import units as un

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
parametric 3D design by writing build123d (Python, OCCT-based) code. There is one design script.
`build_model` takes a COMPLETE script and replaces it: use it for the first version or a full rewrite.
`edit_model` changes the existing script in place (exact-text replacements and appended code, then a rebuild):
use it for every change to an existing design, including adding bodies. Do not resend a whole script to change
one line.

Rules
- Scripts are ALWAYS in millimetres internally (build123d), Z is up. The user's default units are {UNITS_LONG}: a
  number the user gives without a unit is in {UNITS}. `inch` (= 25.4) is pre-imported: write imperial dimensions
  as `2.5 * inch` (and parameters as `plate_l = 2.5 * inch`) so the script reads in the user's units and the
  Parameters panel shows inches; write metric as plain numbers. Mixing both in one script is fine. When you
  report sizes back, use {UNITS} (with the other unit in brackets if helpful). Keep dimensions as named variables
  at the top of the script.
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
- Build complex parts incrementally. Anything with more than 3 bodies, or that would take more than ~80
  lines, goes in stages: build the main body first, check the summary (and a screenshot if the shape matters),
  then add the next body or feature group with `edit_model` (append the code, add the body to `result`), so
  the earlier code is never retyped.
  Never deliver a 200-line multi-body script in one go: a failure deep in it costs the whole attempt, and the
  user sees nothing until the end. Say what you are building next in one short line between steps.
- If a build fails, read the traceback, fix the code and rebuild; do not ask the user to debug Python.
- Keep replies short: say what you changed and any assumption you made. No code in replies unless asked;
  the code lives in the tool call and the user can open it in the Code panel.
- Keep dimensions as top-level `name = number` assignments: they appear in the user's Parameters panel and
  `set_parameters` can change them without a rewrite. `import_step("file.step")` loads a STEP from
  workspace/imports (the user may have imported one; the script then already contains the line).
  Design Kit: scripts have a pre-imported `kit` with ready-made standard components (ISO screws, nuts, set screws,
  inserts and standoffs with optional real threads; ball bearings by designation with balls and cages; circlips,
  E-clips, keys, collars, dowels and couplings; spur/ring gears, planetary stages, GT2 pulleys and lead screws; NEMA
  steppers, servos and gear motors; Arduino/Raspberry Pi boards with exact mounting holes, fans; T-slot extrusions,
  brackets and T-nuts; printable enclosures, bosses, vents and snap fits; hinges, real helical springs, knobs,
  handles, cams and links; O-rings with groove cutters, tube and fittings) and design guides (gears, belts, bearings,
  springs, linkages, O-rings, fits, materials, threads and stock sizes, fasteners, motors, boards, frames, enclosures,
  FDM/resin printing, CNC, sheet metal, laser cutting, moulding, face selection, build123d pitfalls). Before hand-modelling a standard component or
  designing in an unfamiliar area, `kit search` it, `kit read` the entry, then call it: `kit.ball_bearing("688ZZ")`.
  Multi-part components return a dict of parts; move them with `kit.place(obj, Pos(...))` and put them in `result`
  as a component. When you learn something reusable (a fix, a gotcha, a good recipe), `kit note` it on the entry,
  or `kit save_guide` / `kit save_part` it into this workspace's kit.
- Configurations: a top-level `configurations = {"Small": {"plate_l": 40}, "Large": {"plate_l": 120}}` dict declares
  named variants as parameter overrides. The user picks one in the Parameters card; `configurations activate` builds
  it, `configurations export` writes STEP/STL for every variant. Expose the dimensions that vary as parameters first.
- Extensions let you extend the app itself: one Python file (`extension save`) can add script helpers (@helper),
  tools you can call (@tool), UI panels with live controls, pickers and tables (@panel) and checks that run after
  every build (@on_build). Read `extension api` first. Use `show_panel` for a one-off table or report in the viewer;
  save an extension when the user will want it again (a calculator, a report, a custom check or export). Built-in
  examples (Parameters, Hole report, Bolt pattern) are in the Extensions ribbon group; `extension read <module>`
  shows their code.
  `from_library("part name", param=value)` instantiates a library part. The part library is a first-class source of
  geometry: before modelling a standard or previously made part (fasteners, bearings, brackets, anything reusable),
  `library search` it and `library insert` it rather than re-creating it; when you build something reusable, offer to
  `library save_body` / `save_design` it.
  Use `measure` / `mass_properties` to check fits, gaps, wall thicknesses and weights instead of guessing;
  `make_drawings` produces shop drawings. For multi-body assemblies run `check_interference` before you report done
  (parts that overlap are a modelling error, except threads), and use `screenshot` with `section` to look inside
  housings and gearboxes. Script helpers: pattern_linear / pattern_circular (counts include the original; fused, or
  separate=True for a list), mirror_about(shape, origin, normal), path_wire(edges) and pipe(path, diameter, wall).
- Motion, exploded views and appearance are declared in the script after `result` (they never change geometry; the
  viewer poses / explodes / renders the bodies): `revolute(name, [body paths or globs], axis=((o), (d)) | "Z",
  parent=None, limits=None)` (degrees), `slider(name, bodies, direction, limits)` (mm), `couple(follower, leader,
  ratio, offset)` for gear trains / rack and pinion (a child joint's value is relative to its parent; external
  gears ratio = −z_leader/z_follower), `drive(joint, start, stop, seconds)` for Play, `explode({path: (dx, dy, dz)},
  axis="Z")`, `appearance({path or glob: "steel" | "aluminium" | "black anodised" | "brass" | "copper" |
  "black plastic" | ... | {"color": "#rrggbb", "metalness": 0..1, "roughness": 0..1}})`. A body moves with one joint;
  chain with parent=. After adding joints run `check_motion` (collisions through the motion). `screenshot` takes
  `pose` {joint: value}, `explode` 0..1 and `render` true for a lit, material render.
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
- Threads (ISO metric and Unified inch, pre-imported; sizes like "M4", "1/4-20", "#10-32", "3/8" = coarse): `thread("1/4-20")` /
  `iso("M4")` -> major/pitch/tpi/tap_drill/clearance in mm; `tap_drill("M4")`,
  `clearance_dia("M4", "medium")`; `tapped_hole("M4", depth, at=(x, y, surface_z), through=False)` returns a
  cosmetic cutter to SUBTRACT; `tap(part, "M4", at=(x, y, surface_z), depth=8 | through=True, real=False)` cuts
  (and with real=True actually models) a tapped hole into a part and registers it so drawings call it out
  "M4×0.7 ↧8"; `bolt("M4", 16, head="hex"|"socket"|"none", real=False)` (head above z=0, shank down −Z, so
  `Pos(x, y, surface_z) * bolt(...)` sits on a surface), `nut("M4")`, `washer("M4")`. Prefer cosmetic threads
  (fast, clean STEP); use real=True only when the user wants the helix (3D printing, visual).
- Gears (pre-imported): `spur_gear(module, teeth, thickness, bore=0, pressure_angle=20, hub_d=0, hub_h=0, keyway=(w, depth))`
  returns a true involute spur gear solid (Z up, tooth 0 on +X); `involute_gear_profile(module, teeth)` gives the
  closed Face; `gear_centre_distance(module, za, zb)` and `gear_dims(module, teeth)` for meshing/layout. Always use
  these instead of hand-rolling involute flanks. Meshing gears share module and pressure angle; rotate one by half a
  tooth pitch (180/teeth degrees) so teeth interleave. Internal (ring) gear: sketch the housing outline and subtract
  `Rot(0, 0, L["ring_rotation"]) * involute_gear_profile(m, L["ring_teeth"], addendum=1.25, clearance=0)` (its
  tooth spaces are the ring's teeth), then extrude. Planetary: `L = planetary_layout(m, sun_teeth, planet_teeth, n)`
  gives ring_teeth, ratio, centre_distance and per planet x, y and `rotation` (turn the planet by that about its own
  axis, then move it to x, y); it raises if the planets can't be equally spaced or would collide.
  The gear helpers are safe inside BuildPart/BuildSketch (they never add anything to the builder).
- Closed profiles from your own points: build ONE ordered point list around the outline and close it with
  `make_face(Polyline(*pts, close=True))` (or `Spline` for smooth curves). Assembling a Wire from separately
  constructed Line/Arc/Spline pieces fails with "Edges are disconnected" as soon as two end points differ by
  a rounding error, so avoid that pattern.

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

CAM (G-code for GRBL routers and the Makera Z1 / Carvera Air)
There is a second script per design, the CAM script, built with `build_cam`. It runs with these names:
  model (the built design: model.shape = all bodies, model.bodies, model.get_face(id), model.bbox_min/max),
  part (= model.shape), bodies (dict name -> shape), tools (dict: by number, by name, by 'T1'),
  machines (dict name -> Machine), and everything from cam_kernel: Stock, Setup, Program, face, contour, pocket,
  drill, parallel3d, adaptive, rotary_rough, rotary_finish, rotary_wrap, section, silhouette, stock_minus, holes,
  face_polygon, circle, rect, feeds, apply_feeds.
It must assign `program` (a Program). Call `cam_context` first to see the machines/tools/model facts.
```
stock = Stock.from_model(model, margin=3, top=1.0)          # also accepts a body shape; or Stock.block(80, 50, 12, top=8)
setup = Setup(machines["Makera Z1"], stock, origin="stock-top-left")   # origins: stock-top-left|stock-top-center|
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
Multiple setups (flip / re-clamp): each Setup has its own frame where the spindle is +Z; build ops for it from
setup.view(part) (section(setup.view(part), z), holes(setup.view(part)), stock_minus(setup, setup.view(part), z)).
```
top = Setup(m, stock, name="Top")                           # stock is always given in MODEL coordinates
bot = Setup(m, stock, orient="bottom", name="Bottom")       # flipped about X; also front|back|left|right|(rx,ry,rz)
pv = bot.view(part)                                          # the part as it sits in the flipped setup
program = Program(top); program.add(face(top, t, ...)); program.add(pocket(bot, t, section(pv, -2), z_top=0, z_bottom=-3))
```
The program runs setups in op order; the post selects G54, G55… per setup and pauses for the operator (GRBL M0,
Makera M600) to flip and re-zero. A program runs on ONE machine (the first Setup's).
4th axis (rotary A about X; needs a machine with `rotary`, e.g. "Makera Z1 + 4th axis"): the part is turned so its
axis is the setup X axis (Y = Z = 0 on the centreline; the WCS origin defaults to "rotary-axis-left" on the axis,
which is where Makera's 4th-axis probing sets zero). Stock.cylinder(d, length, x0=..., axis=(y, z)) for bar stock.
```
st = Setup(machines["Makera Z1 + 4th axis"], Stock.cylinder(30, 80, x0=-2), rotary=True)
program = Program(st, name="shaft")
program.add(rotary_rough(st, tools[3], part, stepdown=1.0, mode="rings"))          # 1/8" flat; continuous A, leaves 0.3 mm
program.add(rotary_finish(st, ball, part, mode="spiral", stepover=0.1))             # helix advancing 0.1×D per turn; or lines|rings
program.add(rotary_wrap(st, vbit, text_or_polys, depth=0.4, kind="contour", side="on"))   # 2D (u = X, v = around) wrapped on the bar
for a in (0, 90, 180, 270):                                  # 3+1 indexed: ordinary ops on a rotary setup at an A angle
    sa = Setup(st.machine, st.model_stock, rotary=True, a=a, name=f"A{a}")          # same mounting -> just G0 A<a>, no pause
    v = sa.view(part)                                        # the part as the tool sees it at this A
    program.add(pocket(sa, tools[3], stock_minus(sa, v, 9.01, expand=4), z_top=sa.stock.top, z_bottom=9))  # mill the flat at z 9
    # (section(...) is the part's MATERIAL at that height: pocketing it would cut the part away; clear stock_minus instead)
```
A turns by the right-hand rule about +X (A+ brings model +Y towards +Z). Moves on rotary setups are in the setup
frame; the viewer wraps them back onto the part.
Stepover arguments are fractions of the tool diameter (0.1 = 0.1×D), not mm. A ball spiral/lines finish can't
reach square inside corners (shoulders, ends of flats): expect material left there in the simulation and clean them
with a flat `rotary_finish(..., mode="rings", stepover=0.1)` limited to the shoulder with x_range=(x0, x1).
After building a program, check it with `simulate_cam`: it removes material from the stock along every move and
reports gouges into the part (with the op that caused them), material left on the part, rapids through
material, and machine collisions: the collet nut / spindle nose / head against the remaining stock (narrow cuts deeper
than the flutes), the tool through the spoilboard into the bed, and the holder against the chuck or tailstock on
4th-axis jobs. Fix gouges, rapid hits and collisions before exporting (shorter tool stickout, a longer tool, a wider
pocket, or shallower depth); the user can scrub the simulation in the CAM tab and watch it on the machine model
(ribbon ▸ Machine).
Makera Z1 / Carvera Air post (machine.post == "makera"): M6 Tn runs the whole manual change (moves to the change
position, waits for the button, measures the tool length), lines ≤ 63 characters, no canned cycles or coolant,
feeds on A moves are converted to the firmware's own rule. The Z1 is light (150 W, 1/8" collet): aluminium < 1 mm
per pass; keep tools ≤ the 3.175 mm collet unless the user has another collet.
Notes: coordinates are setup-frame (= model coordinates for a plain top setup; Z up); the post subtracts the WCS origin.
Contours get tangential arc lead-in/out by default (lead=radius; lead=0 disables) and start mid-way along the
longest straight edge. The post fits G2/G3 arcs (machine.arcs, machine.arc_tolerance; never while A moves) and merges collinear moves.
Materials for feeds(): aluminium, brass, mild-steel, acrylic, hdpe, delrin, plywood, mdf, hardwood, softwood, foam, fr4, carbon-fibre. section(shape, z) returns
polygons with holes (holes = islands for pocket, inner profiles for contour side="outside"); stock_minus(setup,
shape, z, expand=tool.diameter) = what to clear at level z (expand lets the tool run off the stock edge); face_polygon(model.get_face(id)) uses a face the user clicked. Depths:
z_bottom below the stock bottom means cutting into the spoilboard (fine for through-cuts with a sacrificial
board, warn otherwise). Tool numbers matter for tool changes (GRBL: machine.tool_change='pause' emits M6/M0; Haas and LinuxCNC posts emit Tn M06 + G43 Hn, so tool offsets must exist on the control). Built-in machines include the Makera Z1 and Carvera Air, Haas VF-2 / VF-2SS / VF-4 / Mini Mill / TM-1, and generic LinuxCNC mills.
Prefer the tool library's feeds/stepdown; override per op only with a reason. The user's own per-op tweaks from
the CAM tab live in one line at the top of cam.py, `overrides({"Pocket": {"feed": 500, "stepover": 0.3}, "Face#2": {...}})`
(keys = op names, "#2" for the second op with the same name; fields rpm, feed, plunge, stepdown, stepover as a
fraction of Ø): always keep that line when you rewrite the script, and keep op names stable so it still matches. After build_cam, use `screenshot`
to look at the toolpaths (they are drawn over the model; rapids red, cuts coloured per op).
`export_gcode` writes the .nc file. `save_machine` / `save_tool` edit the libraries (JSON dicts; see cam_context).
"""

SCREENSHOT_VIEWS = ["iso", "iso_back", "front", "back", "left", "right", "top", "bottom"]
SAFE_NAME = re.compile(r"[^A-Za-z0-9 _\-\.]+")


AUTH_ERROR_RE = re.compile(r"failed to authenticate|oauth (session|token) (expired|invalid)|not logged in|invalid api key|"
                           r"authentication_error|please run /login|run `?claude login`?|401 unauthorized|api key.*(invalid|missing|required)", re.I)


def claude_auth_status(cli: str | None = None, api_key: str | None = None) -> dict[str, Any]:
    """Ask Claude Code how it is signed in. Returns {logged_in, auth_method, error}. An API key (settings or
    ANTHROPIC_API_KEY) counts as signed in. The app cannot show the login prompt itself: the SDK runs the CLI
    headless, so sign-in happens once in a terminal (`claude`) or via an API key."""
    import subprocess
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return {"logged_in": True, "auth_method": "api_key", "error": None}
    cli = cli or find_claude_cli()
    if not cli:
        return {"logged_in": False, "auth_method": "none", "error": "Claude Code not found"}
    try:
        r = subprocess.run([cli, "auth", "status", "--json"], capture_output=True, text=True, timeout=20,
                           env={**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})
        data = json.loads(r.stdout or "{}")
        return {"logged_in": bool(data.get("loggedIn")), "auth_method": data.get("authMethod") or "none", "error": None}
    except Exception as e:  # noqa: BLE001
        return {"logged_in": False, "auth_method": "unknown", "error": f"{type(e).__name__}: {e}"}


def safe_name(name: str) -> str:
    name = SAFE_NAME.sub("", name or "").strip().rstrip(".")
    return name[:80] or "untitled"


# A/B switch for evals: the pre-0.15 behaviour (no edit_model, whole-script rebuilds, no incremental rule).
LEGACY_BUILD = os.environ.get("AGENTICCAD_LEGACY_BUILD") == "1"
LEGACY_PROMPT_SWAPS = [
    ("""parametric 3D design by writing build123d (Python, OCCT-based) code. There is one design script.
`build_model` takes a COMPLETE script and replaces it: use it for the first version or a full rewrite.
`edit_model` changes the existing script in place (exact-text replacements and appended code, then a rebuild):
use it for every change to an existing design, including adding bodies. Do not resend a whole script to change
one line.
""", """parametric 3D design by writing build123d (Python, OCCT-based) code and calling the `build_model`
tool with the COMPLETE script every time (there is one design script; each build replaces it).
"""),
    ("""- Build complex parts incrementally. Anything with more than 3 bodies, or that would take more than ~80
  lines, goes in stages: build the main body first, check the summary (and a screenshot if the shape matters),
  then add the next body or feature group with `edit_model` (append the code, add the body to `result`), so
  the earlier code is never retyped.
  Never deliver a 200-line multi-body script in one go: a failure deep in it costs the whole attempt, and the
  user sees nothing until the end. Say what you are building next in one short line between steps.
""", ""),
]

def _build_hint(e: BaseException) -> str:
    """One-line pointers for failure modes the agent keeps hitting."""
    t = str(e).lower()
    if "disconnected" in t:
        return ("\nHINT: build closed profiles as one ordered point list with make_face(Polyline(*pts, close=True)); "
                "for gears use the pre-imported spur_gear() / involute_gear_profile().")
    return ""


_SCRIPT_NAMES: set[str] | None = None


def _script_names() -> set[str]:
    """Names the design script namespace defines (build123d, helpers): new variables must not shadow them."""
    global _SCRIPT_NAMES
    if _SCRIPT_NAMES is None:
        _SCRIPT_NAMES = set(ck.script_namespace()) | {"result", "kit", "math"}
    return _SCRIPT_NAMES


def _find_overrides(code: str) -> tuple[dict, tuple[int, int] | None]:
    """The table of the top-level `overrides({...})` call in a CAM script and its line span (1-based, inclusive)."""
    import ast
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return {}, None
    for node in tree.body:
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "overrides" and node.value.args):
            try:
                table = ast.literal_eval(node.value.args[0])
            except ValueError:
                raise cam.CamError("the overrides({...}) line in cam.py is not a plain dict; edit it in the Code tab")
            return dict(table or {}), (node.lineno, node.end_lineno)
    return {}, None


def _write_overrides(code: str, table: dict, span: tuple[int, int] | None) -> str:
    def num(v: float):
        return int(v) if float(v).is_integer() else round(float(v), 4)
    body = ", ".join(f"{k!r}: {{" + ", ".join(f"{f!r}: {num(v)}" for f, v in vals.items()) + "}" for k, vals in table.items())
    line = f"overrides({{{body}}})  # per-operation tool overrides (CAM tab)" if table else None
    lines = code.splitlines()
    if span:
        a, b = span
        lines[a - 1:b] = [line] if line else []
    elif line:
        lines.insert(0, line)
    return "\n".join(lines) + ("\n" if code.endswith("\n") or not code else "")


class CadAgent:
    def __init__(self, workspace: Path, emit: Emit,
                 screenshot_fn: Callable[[dict[str, Any]], Awaitable[str]]):
        self.workspace = workspace
        self._agent_turn = False              # True while the agent is answering (builds use draft threads)
        self.sim = None                       # last CAM simulation (cam_sim.SimResult)
        self.sim_ops: list[int] = []
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
        self.extensions = ext_mod.for_workspace(workspace)   # Python files that extend the app (ext_builtin + workspace/extensions)
        self.config: str | None = None        # active configuration (named parameter overrides declared in the script)
        ck.WORKSPACE = workspace
        ck.LIBRARY = self.parts
        (workspace / "imports").mkdir(exist_ok=True)
        self.tool_handlers: dict[str, Any] = {}   # filled by _make_server()
        self.auth_problem: str | None = None       # set when Claude Code is not signed in; shown as a banner in every tab
        self.cam_code: str = ""               # CAM script working copy (workspace/cam.py)
        self.slice_result: slicer.SliceResult | None = None   # last 3D-printing slice (only when a slicer is installed)
        self.slice_layers: dict[str, Any] | None = None       # its parsed G-code, viewer payload
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
        return {"type": "design", "name": self.design_name, "dirty": self.dirty, "config": self.config,
                "configurations": list(self.model.configurations) if self.model else []}

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
        self.config = None
        await self.build(code, source="open", record=True)
        cam_file = self.designs_dir / f"{path.stem}.cam.py"
        await self.set_cam_code(cam_file.read_text() if cam_file.exists() else "", rebuild=cam_file.exists())
        self.design_name = path.stem
        self.saved_code = code
        self._write_state()
        self.notes.append(f"The user opened design '{path.stem}'; the script changed.")
        await self.emit(self.design_state())

    async def new_design(self, code: str | None = None) -> None:
        """File ▸ New: a clean start: empty design, no CAM program or simulation, and a fresh conversation."""
        await self.reset_conversation()
        self.config = None
        await self.build(code or ck.NEW_DESIGN_CODE, source="new", record=True)
        await self.set_cam_code("", rebuild=False)
        self.design_name = None
        self.saved_code = None
        self._write_state()
        await self.emit(self.design_state())

    async def reset_conversation(self) -> None:
        """Forget the chat: stop any running turn, drop the Claude session (a new one starts with the next message)
        and the pending notes, and tell the UI to clear the transcript."""
        if self.busy:
            await self.interrupt()
            for _ in range(50):                            # let the turn wind down before disconnecting
                if not self.busy:
                    break
                await asyncio.sleep(0.1)
        if self.client is not None:
            try:
                await self.stop()
            except Exception:  # noqa: BLE001
                pass
            self.client = None
        self.session_id = None
        self.notes.clear()
        await self.emit({"type": "chat_reset"})

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
            self.sim = None
            await self.emit({"type": "cam", "program": None, "code": code, "source": source})
            return None
        prog = await asyncio.to_thread(self._run_cam, code)
        self.program = prog
        self.sim = None                                   # a new program invalidates the old simulation
        await self.emit({"type": "cam", "program": prog.to_payload(), "code": code, "source": source,
                         "summary": prog.summary(), "gcode_lines": prog.gcode().count("\n")})
        return prog

    async def _check_model(self) -> tuple[ck.Model, str | None]:
        """The model the interference / motion checks run on. Real (helical) threads make booleans very slow and
        fragile, so a design that has them is checked on a draft-thread rebuild (threads as plain cylinders; same
        bodies, names and joints), cached per script. Falls back to the real model when the script can't build with
        draft threads."""
        if self.model is None:
            raise ck.CadError("no model built")
        m = self.model
        if not (any(getattr(t, "real", False) for t in m.threads) and not m.draft_threads):
            return m, None
        key = (m.code, self.quality)
        if not (getattr(self, "_draft_check", None) and self._draft_check[0] == key):
            try:
                dm = await asyncio.to_thread(ck.run_script, m.code, "draft", self.workspace, self.parts, "draft")
            except ck.CadError:                  # a script that needs its real threads (e.g. it searches a thread phase)
                dm = None
            self._draft_check = (key, dm)
        dm = self._draft_check[1]
        if dm is None:
            return m, "checked with the real threads (this script doesn't build with draft threads), so it is slower"
        return dm, "real threads were checked as plain cylinders (fast); a bolt in a tapped hole shows as thread engagement"

    async def interference(self, paths: list[str] | None = None, mesh: bool = True) -> dict[str, Any]:
        import analysis
        m, note = await self._check_model()
        res = await asyncio.to_thread(analysis.interference, m, paths, 1e-3, 90.0, mesh)
        if note:
            res["notes"].insert(0, note)
        return res

    async def motion_check(self, steps: int = 12) -> dict[str, Any]:
        import analysis
        m, note = await self._check_model()
        res = await asyncio.to_thread(analysis.motion_interference, m, steps)
        if note:
            res["notes"].insert(0, note)
        return res

    async def set_op_override(self, key: str, values: dict | None) -> cam.Program | None:
        """CAM tab: set (or with empty/None values, clear) the tool overrides of one operation by rewriting the
        `overrides({...})` line of cam.py, then rebuild."""
        if not self.cam_code.strip():
            raise cam.CamError("no CAM script")
        table, span = _find_overrides(self.cam_code)
        vals = {k: float(v) for k, v in (values or {}).items() if v not in (None, "") and k in cam.OVERRIDE_FIELDS}
        if vals:
            table[key] = vals
        else:
            table.pop(key, None)
        code = _write_overrides(self.cam_code, table, span)
        prog = await self.set_cam_code(code, source="user")
        what = ", ".join(f"{k}={v:g}" for k, v in vals.items()) if vals else "cleared (tool defaults)"
        self.notes.append(f"The user set per-operation overrides in the CAM tab for '{key}': {what}. They live in the "
                          "`overrides({...})` line of cam.py; keep that line when you rewrite the script.")
        return prog

    async def simulate(self, ops: list[int] | None = None, resolution: float | None = None):
        """Run the material-removal simulation (0-based op indices) and show it in the viewer."""
        import cam_sim
        if self.program is None:
            raise cam.CamError("no CAM program built")
        res = await asyncio.to_thread(cam_sim.simulate, self.program, ops, self.model, resolution)
        self.sim = res
        self.sim_ops = ops if ops is not None else list(range(len(self.program.ops)))
        await self.emit({"type": "cam_sim", "sim": res.to_payload(), "ops": self.sim_ops})
        return res

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
        cam.reset_overrides()
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
        prog.unused_overrides = cam.unused_overrides()
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
        """Parameters with their EFFECTIVE values (the active configuration's overrides applied); `base` is the literal."""
        if self.model is None:
            return []
        try:
            ps = script_edit.params(self.model.code)
        except SyntaxError:
            return []
        over = self.configurations().get(self.config, {}) if self.config else {}
        for p in ps:
            p["base"] = p["value"]
            if p["name"] in over:
                p["value"] = over[p["name"]]; p["override"] = True
        return ps

    def configurations(self) -> dict[str, dict[str, float]]:
        if self.model is None:
            return {}
        try:
            return script_edit.configurations(self.model.code)
        except (script_edit.Unsupported, SyntaxError):
            return {}

    def config_state(self) -> dict[str, Any]:
        return {"active": self.config, "configurations": self.configurations(), "params": self.get_params()}

    async def set_configurations(self, cfgs: dict[str, dict[str, float]], source: str = "configs") -> None:
        """Replace the script's configurations dict (validated) and rebuild; the active one stays if it still exists."""
        if self.model is None:
            raise ck.CadError("no design yet")
        code = script_edit.set_configurations(self.model.code, {n: {k: float(v) for k, v in vals.items()} for n, vals in cfgs.items()})
        if self.config and self.config not in cfgs:
            self.config = None
        if code != self.model.code:
            await self.build(code, source=source)
        self._write_state()
        await self.emit(self.design_state())

    async def activate_configuration(self, name: str | None, source: str = "configs") -> None:
        """Switch the active configuration (None = the base values) and rebuild; nothing in the script changes."""
        if self.model is None:
            raise ck.CadError("no design yet")
        if name and name not in self.configurations():
            raise ck.CadError(f"no configuration named {name!r}; configurations: {', '.join(self.configurations()) or 'none'}")
        if name == self.config:
            return
        self.config = name or None
        self._write_state()
        await self.build(self.model.code, source="config", record=False)
        if source == "configs":
            self.notes.append(f"The user switched the active configuration to {name or 'the base values'}.")

    async def export_configurations(self, formats: list[str], names: list[str] | None = None, tolerance: float = 0.01,
                                    angular: float = 0.05) -> list[Path]:
        """Build every configuration (or the named ones; "" = the base) with real threads and export each as
        exports/<design>-<configuration>.<fmt>."""
        if self.model is None:
            raise ck.CadError("no design yet")
        cfgs = self.configurations()
        wanted = names if names is not None else ["", *cfgs]
        base = safe_name(self.design_name or "untitled")
        out: list[Path] = []
        for n in wanted:
            if n and n not in cfgs:
                raise ck.CadError(f"no configuration named {n!r}")
            model = await asyncio.to_thread(ck.run_script, self.model.code, self.quality, self.workspace, self.parts, "real", n or None)
            stem = f"{base}-{safe_name(n)}" if n else f"{base}-base"
            out += await asyncio.to_thread(ck.export, model, self.workspace / "exports", stem, formats, tolerance, angular, None)
        await self.emit({"type": "info", "text": f"exported {len(out)} file(s) for {len(wanted)} configuration(s): " + ", ".join(p.name for p in out)})
        return out

    async def set_params(self, values: dict[str, float], source: str = "params") -> None:
        if self.model is None:
            return
        if self.config:                                   # editing while a configuration is active edits ITS overrides
            cfgs = self.configurations()
            cur = dict(cfgs.get(self.config, {}))
            cur.update({k: float(v) for k, v in values.items()})
            cfgs[self.config] = cur
            await self.set_configurations(cfgs, source=source)
            if source == "params":
                self.notes.append(f"The user changed parameter(s) of configuration '{self.config}' in the Parameters panel: "
                                  + ", ".join(f"{k}={v:g}" for k, v in values.items()))
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
    async def set_sketch(self, name: str, plane: dict[str, Any], items: list[dict[str, Any]], by_agent: bool = False,
                         constraints: list[dict[str, Any]] | None = None) -> None:
        if self.model is None:
            return
        code = script_edit.set_sketch(self.model.code, name, plane, items, constraints)
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
        (negative = into the body → cut). Only planar faces can be pressed/pulled."""
        if self.model is not None:
            b_ = self.model.body_by_name(body)
            if b_ is not None:
                pt = b3d.Vector(*point)
                face = min(b_.shape.faces(), key=lambda f: f.distance_to(pt))
                if face.geom_type != b3d.GeomType.PLANE:
                    raise ck.CadError(f"press/pull needs a flat face; the point you clicked is on a {face.geom_type.name.lower()} face")
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

    # ---- feature tools (ribbon): mirror / pattern / revolve / loft / sweep, all written as code
    def _body_var(self, code: str, path: str) -> tuple[str, str]:
        """Bind a body's expression to a variable (hoisted before `result`) so other expressions can use it."""
        code = script_edit.wrap_body_expr(code, path, "{body}")
        return code, script_edit.body_expr(code, path)

    def _unique_body_name(self, name: str) -> str:
        existing = {b.name for b in self.model.bodies} | {b.path for b in self.model.bodies}
        base, k = name, 2
        while name in existing:
            name = f"{base} {k}"; k += 1
        return name

    async def _apply_feature(self, code: str, expr: str, op: str, target: str | None, what: str, name: str) -> None:
        """op new: add `expr` as a new body; join / cut: fuse into / subtract from `target` (expr may use {body} for
        the target's own variable)."""
        if op == "new" or not target:
            body_name = self._unique_body_name(name)
            var = re.sub(r"[^a-z0-9_]", "_", body_name.lower()).strip("_") or "feature"
            var = ("p_" + var) if var[0].isdigit() else var
            reserved = _script_names()
            base, k = var, 2
            while var in reserved or re.search(rf"\b{re.escape(var)}\b", code):   # never shadow pipe(), loft(), a variable...
                var = f"{base}_{k}" if base not in reserved or k > 2 else f"{base}_body"; k += 1
            code = script_edit.add_body(code, var, expr.replace("{body}", var), body_name)
            where = f"new body '{body_name}'"
        elif op in ("join", "cut"):
            code = script_edit.wrap_body_expr(code, target, "{body} " + ("+" if op == "join" else "-") + " " + expr)
            where = f"{'joined to' if op == 'join' else 'cut from'} '{target}'"
        else:
            raise ck.CadError(f"{what}: operation must be new | join | cut")
        await self.build(code, source="edit")
        self.notes.append(f"The user added a {what} via the ribbon ({where}); the script was edited directly.")
        await self.emit({"type": "info", "text": f"{what} · {where}"})

    async def op_mirror(self, body: str, origin: list[float], normal: list[float], mode: str = "join") -> None:
        """Mirror `body` in the plane through `origin` with `normal`: join (one symmetric body) or new (a separate
        mirrored body)."""
        code, var = self._body_var(self.model.code, body)
        expr = f"mirror_about({var}, origin={self._p(origin)}, normal={self._p(normal)})"
        if mode == "join":
            await self._apply_feature(code, expr, "join", body, "mirror", body)
        else:
            await self._apply_feature(code, expr, "new", None, "mirror", f"{body} mirror")

    async def op_pattern(self, body: str, kind: str, params: dict[str, Any], mode: str = "join", target: str | None = None) -> None:
        """Pattern `body`. kind linear: direction, count, spacing [, direction2, count2, spacing2]; circular: count,
        angle, axis_origin, axis_dir. mode: join (copies fused into the body) | new (one new body of the copies) |
        cut (subtract all copies from `target`; the source body is then only a tool and leaves the result)."""
        code, var = self._body_var(self.model.code, body)
        if kind == "linear":
            args = f"{self._p(params['direction'])}, count={int(params['count'])}, spacing={float(params['spacing']):g}"
            if params.get("direction2") and int(params.get("count2") or 1) > 1:
                args += f", direction2={self._p(params['direction2'])}, count2={int(params['count2'])}, spacing2={float(params['spacing2']):g}"
            call = "pattern_linear"
        elif kind == "circular":
            args = f"count={int(params['count'])}, angle={float(params.get('angle', 360)):g}, axis=({self._p(params.get('axis_origin') or [0, 0, 0])}, {self._p(params.get('axis_dir') or [0, 0, 1])})"
            call = "pattern_circular"
        else:
            raise ck.CadError("pattern: kind must be linear | circular")
        if mode == "join":
            code = script_edit.wrap_body_expr(code, body, f"{call}({{body}}, {args})")
            await self.build(code, source="edit")
            self.notes.append(f"The user patterned body '{body}' ({kind}) via the ribbon; the script was edited directly.")
            await self.emit({"type": "info", "text": f"{kind} pattern of '{body}'"})
        elif mode == "new":
            await self._apply_feature(code, f"{call}({var}, {args}, include_original=False)", "new", None, "pattern", f"{body} pattern")
        elif mode == "cut":
            if not target or target == body:
                raise ck.CadError("pattern cut: choose another body to cut the copies from")
            code = script_edit.delete(code, body)              # the source is a tool now; its variable stays hoisted
            await self._apply_feature(code, f"{call}({var}, {args})", "cut", target, f"{kind} pattern of '{body}'", target)
        else:
            raise ck.CadError("pattern: mode must be join | new | cut")

    def _sketch_ok(self, name: str) -> None:
        if self.model is None or not any(sk["name"] == name for sk in self.model.sketches):
            raise ck.CadError(f"no sketch '{name}'")

    async def op_revolve(self, sketch: str, axis_origin: list[float], axis_dir: list[float], angle: float = 360.0,
                         mode: str = "new", target: str | None = None) -> None:
        self._sketch_ok(sketch)
        expr = f"revolve({sketch}, axis=Axis({self._p(axis_origin)}, {self._p(axis_dir)}), revolution_arc={float(angle):g})"
        await self._apply_feature(self.model.code, expr, mode, target, f"revolve of {sketch}", f"{sketch} revolve")

    async def op_loft(self, sketches: list[str], ruled: bool = False, mode: str = "new", target: str | None = None) -> None:
        if len(sketches) < 2:
            raise ck.CadError("loft needs at least two sketches")
        for n in sketches:
            self._sketch_ok(n)
        expr = f"loft([{', '.join(sketches)}]" + (", ruled=True" if ruled else "") + ")"
        await self._apply_feature(self.model.code, expr, mode, target, f"loft of {', '.join(sketches)}", "Loft")

    async def op_sweep(self, path_body: str, points: list[list[float]], sketch: str | None = None, diameter: float | None = None,
                       wall: float | None = None, mode: str = "new", target: str | None = None,
                       face_point: list[float] | None = None) -> None:
        """Sweep a sketch (or a round pipe profile of `diameter`) along the connected edges of `path_body` nearest
        `points`, or around the outline of the face nearest `face_point`."""
        if not points and not face_point:
            raise ck.CadError("sweep: pick the path edges (or one face to go around its outline)")
        code, var = self._body_var(self.model.code, path_body)
        path = (f"{var}.faces().sort_by_distance({self._p(face_point)})[0].outer_wire()" if not points else
                f"path_wire([{', '.join(f'{var}.edges().sort_by_distance({self._p(p)})[0]' for p in points)}])")
        if sketch:
            self._sketch_ok(sketch)
            expr = f"sweep({sketch}, path={path}, transition=Transition.RIGHT)"
            label = f"sweep of {sketch}"
        elif diameter:
            expr = f"pipe({path}, diameter={float(diameter):g}" + (f", wall={float(wall):g}" if wall else "") + ")"
            label = f"Ø{float(diameter):g} pipe"
        else:
            raise ck.CadError("sweep: choose a sketch profile or a pipe diameter")
        if target == path_body and mode in ("join", "cut"):
            expr = expr.replace(f"{var}.edges()", "{body}.edges()").replace(f"{var}.faces()", "{body}.faces()")
        await self._apply_feature(code, expr, mode, target, label, "Pipe" if not sketch else f"{sketch} sweep")

    async def op_joint(self, name: str, kind: str, bodies: list[str], origin: list[float], direction: list[float],
                       parent: str | None = None, limits: list[float] | None = None, couple_to: str | None = None,
                       ratio: float = 1.0) -> None:
        """Joint tool: append a revolute()/slider() line (and an optional couple()) to the script."""
        if self.model is None:
            raise ck.CadError("no model")
        if not name.strip() or not bodies:
            raise ck.CadError("joint: give a name and at least one body")
        if self.model.motion and any(j["name"] == name for j in self.model.motion["joints"]):
            raise ck.CadError(f"joint '{name}' already exists; pick another name or edit it in the Code tab")
        extra = (f", parent={parent!r}" if parent else "") + (f", limits=({float(limits[0]):g}, {float(limits[1]):g})" if limits else "")
        if kind == "slider":
            line = f"slider({name!r}, {list(bodies)!r}, direction={self._p(direction)}{extra})"
        else:
            line = f"revolute({name!r}, {list(bodies)!r}, axis=({self._p(origin)}, {self._p(direction)}){extra})"
        lines = [line] + ([f"couple({name!r}, {couple_to!r}, ratio={float(ratio):g})"] if couple_to else [])
        code = self.model.code.rstrip("\n") + "\n" + "\n".join(lines) + "\n"
        await self.build(code, source="edit")
        self.notes.append(f"The user added joint '{name}' ({kind}) via the ribbon: {'; '.join(lines)}")
        await self.emit({"type": "info", "text": f"joint '{name}' added"})

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

    async def make_drawings(self, material: str = "", density: float | None = None, sheet: str = "", notes: str = "", units: str = "") -> list[dict[str, Any]]:
        """Empty material / sheet fall back to Settings ▸ Shop drawings (the ribbon button passes nothing)."""
        if self.model is None or not self.model.bodies:
            raise ck.CadError("nothing to draw: the design is empty")
        dd = self.settings.get("drawings") or {}
        material = material or dd.get("material") or ""
        sheet = sheet or dd.get("sheet") or "A4"
        units = units or self.units()
        name = self.design_name or "untitled"
        out_dir = self.workspace / "drawings" / safe_name(name)
        for old in out_dir.glob("*"):
            old.unlink()
        res = await asyncio.to_thread(drawing.drawings_for_model, self.model, out_dir, safe_name(name), material, density, sheet, notes, units)
        files = [{"body": r["body"], "svg": Path(r["svg"]).name, "dxf": Path(r["dxf"]).name if r.get("dxf") else None, "scale": r["scale"]} for r in res]
        await self.emit({"type": "drawings", "design": safe_name(name), "files": files})
        return res

    def library_payload(self) -> dict[str, Any]:
        from dataclasses import asdict
        return {"machines": [asdict(m) for m in self.library.machines().values()],
                "tools": [asdict(t) for t in self.library.tools()]}

    def _write_state(self) -> None:
        (self.workspace / "state.json").write_text(json.dumps({"design": self.design_name, "config": self.config}))

    # ------------------------------------------------------------------ model
    async def build(self, code: str, source: str = "agent", record: bool = True) -> ck.Model:
        async with self.model_lock:
            if self.config:
                try:
                    if self.config not in script_edit.configurations(code):
                        self.config = None
                except (script_edit.Unsupported, SyntaxError):
                    self.config = None
            model = await asyncio.to_thread(ck.run_script, code, self.quality, self.workspace, self.parts, self._thread_mode(), self.config)
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
            await self._after_build(model)
            if record:
                self._kit_used(code)
            if source == "user":
                self.notes.append("The user edited and rebuilt the script by hand in the Code panel.")
            return model

    async def edit_and_build(self, edits: list[dict[str, Any]], append: str = "", source: str = "agent") -> tuple[ck.Model, str]:
        """Apply exact-text edits to the CURRENT script and rebuild, atomically: concurrent edit calls (parallel tool
        use) are serialised and each one is applied to the latest script. Raises script_edit.Refused or
        ck.CadError; on failure the previous design is untouched."""
        async with self.model_lock:
            if self.model is None:
                raise ck.CadError("no design yet")
            code = script_edit.apply_edits(self.model.code, edits, append)
            model = await asyncio.to_thread(ck.run_script, code, self.quality, self.workspace, self.parts, self._thread_mode(), self.config)
            self.model = model
            (self.workspace / "model.py").write_text(code)
            hid = str(int(time.time() * 1000))
            (self.workspace / "history" / f"{hid}.py").write_text(code)
            await self.emit({"type": "model", "mesh": model.mesh, "code": code, "summary": model.summary(6), "source": source,
                             "history_id": hid, "history_len": len(self._history_files())})
            await self.emit(self.design_state())
            await self._after_build(model)
            self._kit_used(code)
            return model, code

    def _thread_mode(self) -> str:
        """Draft threads while the agent is working (4-5x faster rebuilds with real=True screws); real otherwise."""
        return "draft" if self._agent_turn and self.settings.get("draft_threads", True) else "real"

    async def finalize_threads(self) -> str | None:
        """After an agent turn: if the current design was built with draft threads, build it once with the real ones.
        Returns None when done (or nothing to do), else the build error (the design keeps its plain threads)."""
        if self.model is None or not getattr(self.model, "draft_threads", 0):
            return None
        await self.emit({"type": "info", "text": f"modelling {self.model.draft_threads} real thread(s)…"})
        try:
            await self.build(self.model.code, source="agent", record=False)
            return None
        except Exception as e:  # noqa: BLE001
            return str(e)

    def _kit_used(self, code: str) -> None:
        """Count Design Kit components this successfully built script calls (feeds the workspace kit's TOC)."""
        try:
            ck.design_kit(self.workspace).record_usage(code)
        except Exception:  # noqa: BLE001 - usage stats must never break a build
            pass

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
                st = json.loads(state_path.read_text())
                name, self.config = st.get("design"), st.get("config") or None
            except Exception:
                name = None
        # model.py is the working copy (may hold unsaved edits); the design file is what was last saved
        path = self.workspace / "model.py"
        code = path.read_text() if path.exists() else None
        if name and (self.designs_dir / f"{name}.py").exists():
            self.design_name = name
            self.saved_code = (self.designs_dir / f"{name}.py").read_text()
            code = code or self.saved_code
        if code is not None and self.design_name is None and code.strip() == ck.DEFAULT_CODE.strip():
            code = None                            # the demo seeded by versions before 0.11: upgrade to a blank start
        fresh = code is None
        code = code or ck.NEW_DESIGN_CODE          # first run: an empty design, not a demo
        try:
            self.model = ck.run_script(code, self.quality, self.workspace, self.parts, config=self.config)
        except ck.CadError:
            self.config = None
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

    # ------------------------------------------------------------------ extensions (ext_builtin + <workspace>/extensions)
    def ext_ctx(self, state: dict[str, Any] | None = None, event: str | None = None, selection: dict[str, Any] | None = None) -> ext_mod.Ctx:
        return ext_mod.Ctx(self.model, self.workspace, self.public_settings(), state, event, selection, self.units())

    async def ext_call(self, name: str, args: dict[str, Any] | None = None, state: dict[str, Any] | None = None, event: str | None = None,
                       selection: dict[str, Any] | None = None, panel: str | None = None) -> str:
        """Run an extension tool (in a thread) and apply what it returns. Returns a text summary for the caller."""
        ctx = self.ext_ctx(state, event, selection)
        res = await asyncio.to_thread(self.extensions.call, name, ctx, {**(state or {}), **(args or {})})
        return await self.ext_apply(res, panel=panel, state=ctx.state)

    async def ext_apply(self, res: Any, panel: str | None = None, state: dict[str, Any] | None = None) -> str:
        """Apply a RESULT dict (see extensions.API_DOC): model edits, selection, chat prompts, UI."""
        if res is None:
            return "ok"
        if not isinstance(res, dict):
            text = str(res)
            await self.emit({"type": "info", "text": text[:2000]})
            return text
        out: list[str] = []
        if res.get("error"):
            await self.emit({"type": "error", "text": str(res["error"])})
            out.append(f"error: {res['error']}")
        if res.get("notify"):
            await self.emit({"type": "info", "text": str(res["notify"])})
            out.append(str(res["notify"]))
        if self.model is not None:
            if res.get("set_params"):
                await self.set_params({k: float(v) for k, v in res["set_params"].items()}, source="extension")
                out.append("parameters set: " + ", ".join(f"{k}={v:g}" for k, v in res["set_params"].items()))
            if res.get("code"):
                await self.build(str(res["code"]), source="extension")
                out.append("script replaced and rebuilt")
            if res.get("wrap"):
                w = res["wrap"]
                code = script_edit.wrap_body_expr(self.model.code, w["body"], w["template"])
                await self.build(code, source="extension")
                out.append(f"body '{w['body']}' rewritten and rebuilt")
            if res.get("edits") or res.get("append"):
                await self.edit_and_build(list(res.get("edits") or []), str(res.get("append") or ""), source="extension")
                out.append("script edited and rebuilt")
        for key in ("select", "highlight", "fit"):
            if res.get(key):
                await self.emit({"type": "ext_view", "op": key, **(res[key] if isinstance(res[key], dict) else {})})
        if res.get("chat"):
            await self.emit({"type": "ext_chat", "text": str(res["chat"])})
            out.append("asked the agent: " + str(res["chat"]))
        if res.get("ui"):
            ui = res["ui"]
            ext_mod.validate_ui(ui)
            await self.emit({"type": "ext_panel", "id": panel or f"eph:{int(time.time() * 1000)}", "ui": ui,
                             "state": {**(state or {}), **(res.get("state") or {})}, "ephemeral": not panel})
            out.append("panel shown")
        elif res.get("state") is not None and panel:
            await self.ext_render(panel, {**(state or {}), **res["state"]})
        elif panel:
            await self.ext_render(panel, state or {})
        if res.get("open_panel"):
            await self.ext_render(str(res["open_panel"]), {})
        if res.get("table") and not res.get("ui"):
            out.append(str(res["table"]))
        if not out:
            out.append(str({k: v for k, v in res.items() if k not in ("ui", "state")}) if res else "ok")
        return "\n".join(out)

    async def ext_render(self, pid: str, state: dict[str, Any] | None = None, event: str | None = None,
                         selection: dict[str, Any] | None = None) -> None:
        """Render a panel and send it to the browsers (replaces its content if open, opens it otherwise)."""
        ctx = self.ext_ctx(state, event, selection)
        try:
            e = self.extensions.panel(pid)
            ui = await asyncio.to_thread(self.extensions.render, pid, ctx)
        except Exception as ex:  # noqa: BLE001
            await self.emit({"type": "ext_panel", "id": pid, "error": f"{type(ex).__name__}: {ex}", "state": ctx.state})
            return
        await self.emit({"type": "ext_panel", "id": e.id, "title": e.title, "ui": ui, "state": ctx.state})

    async def _after_build(self, model: ck.Model) -> None:
        """Run @on_build hooks; their warnings go to the chat and onto the model."""
        try:
            if not self.extensions.hooks():
                return
            warns = await asyncio.to_thread(self.extensions.run_hooks, self.ext_ctx())
        except Exception as ex:  # noqa: BLE001
            warns = [f"extension hooks failed: {ex}"]
        for w in warns:
            model.warnings.append(w)
            await self.emit({"type": "info", "text": "⚠ " + w})

    # ------------------------------------------------------------------ tools
    def _make_server(self):
        agent = self

        @tool("build_model",
              "Replace the design script with a complete build123d script and rebuild it. The script must "
              "assign `result` (a shape, or a dict of name -> shape / nested dict for bodies and components). "
              "Returns a geometry summary or the Python traceback. For a NEW design with more than 3 bodies, put "
              "only the first body (or sub-assembly) here and add the others one at a time with edit_model. To "
              "change an existing design use edit_model, not this.",
              {"code": str})
        async def build_model(args: dict[str, Any]) -> dict[str, Any]:
            try:
                model = await agent.build(args["code"])
            except ck.CadError as e:
                return {"content": [{"type": "text", "text": f"BUILD FAILED\n{e}{_build_hint(e)}"}], "is_error": True}
            except Exception as e:  # noqa: BLE001
                return {"content": [{"type": "text", "text": f"BUILD FAILED\n{type(e).__name__}: {e}{_build_hint(e)}"}],
                        "is_error": True}
            return {"content": [{"type": "text", "text": model.summary(25)}]}

        @tool("edit_model",
              "Edit the CURRENT design script in place and rebuild: the normal way to change an existing design "
              "(much cheaper and safer than resending the whole script). `edits`: list of {old, new} exact-text "
              "replacements; each `old` must occur exactly once in the current script (include enough surrounding "
              "lines to make it unique, keep indentation; `new` may be empty to delete). `append`: code inserted just "
              "before the final top-level `result = ...` line (new bodies, helper functions); add new bodies to "
              "`result` with an edit in the same call. Edits are applied in order, then the script is rebuilt. If the "
              "build fails nothing is applied and the previous design stays; the traceback is returned.",
              {"type": "object",
               "properties": {"edits": {"type": "array", "items": {"type": "object", "properties": {"old": {"type": "string"}, "new": {"type": "string"}},
                                                                    "required": ["old", "new"]}},
                              "append": {"type": "string"}},
               "required": []})
        async def edit_model(args: dict[str, Any]) -> dict[str, Any]:
            if agent.model is None:
                return {"content": [{"type": "text", "text": "No design yet: use build_model first."}], "is_error": True}
            try:
                model, code = await agent.edit_and_build(list(args.get("edits") or []), args.get("append") or "")
            except script_edit.Refused as e:
                return {"content": [{"type": "text", "text": f"EDIT REFUSED: {e}"}], "is_error": True}
            except ck.CadError as e:
                return {"content": [{"type": "text", "text": f"BUILD FAILED (previous design kept)\n{e}{_build_hint(e)}"}], "is_error": True}
            except Exception as e:  # noqa: BLE001
                return {"content": [{"type": "text", "text": f"BUILD FAILED (previous design kept)\n{type(e).__name__}: {e}{_build_hint(e)}"}],
                        "is_error": True}
            return {"content": [{"type": "text", "text": model.summary(25) + f"\n(script is now {code.count(chr(10)) + 1} lines)"}]}

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
              "orange so you can confirm which is which. `show_edges` defaults to true. `section` cuts the model "
              "with a plane to show the inside of assemblies and hollow parts: {plane: 'XY'|'XZ'|'YZ', offset: mm "
              "along the plane normal from the origin (default: through the model centre), flip: bool}; cut faces "
              "are drawn solid (hatched) in each body's colour.",
              {"type": "object",
               "properties": {"view": {"type": "string", "enum": SCREENSHOT_VIEWS, "default": "iso"},
                              "highlight_faces": {"type": "array", "items": {"type": "integer"}},
                              "show_edges": {"type": "boolean", "default": True},
                              "section": {"type": "object", "properties": {"plane": {"type": "string", "enum": ["XY", "XZ", "YZ"]},
                                                                           "offset": {"type": "number"}, "flip": {"type": "boolean"}}},
                              "pose": {"type": "object", "description": "joint values {name: degrees or mm}; coupled joints follow"},
                              "explode": {"type": "number", "description": "exploded view amount 0..1"},
                              "render": {"type": "boolean", "description": "studio-lit render with the bodies' appearances"}},
               "required": []})
        async def screenshot(args: dict[str, Any]) -> dict[str, Any]:
            if agent.model is None:
                return {"content": [{"type": "text", "text": "No model built yet."}], "is_error": True}
            try:
                png_b64 = await agent.screenshot_fn({
                    "view": args.get("view") or "iso",
                    "highlight": args.get("highlight_faces") or [],
                    "showEdges": args.get("show_edges", True),
                    "section": args.get("section") or None,
                    "pose": args.get("pose") or None,
                    "explode": args.get("explode"),
                    "render": bool(args.get("render")),
                })
            except Exception as e:  # noqa: BLE001
                return {"content": [{"type": "text", "text": f"screenshot unavailable: {e}"}], "is_error": True}
            return {"content": [
                {"type": "image", "data": png_b64, "mimeType": "image/jpeg"},
                {"type": "text", "text": f"view={args.get('view') or 'iso'} highlighted={args.get('highlight_faces') or []} "
                                         + (f"section={args.get('section')} " if args.get('section') else "")
                                         + "(X red, Y green, Z blue axis gizmo; grid is the XY plane)"},
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

        @tool("export_gcode", "Write the current program as G-code (the machine's post: GRBL or Makera) to workspace/exports/<name>.nc.",
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

        @tool("simulate_cam", "Simulate material removal for the CAM program (all ops, or `ops` = 1-based op numbers) and "
              "compare with the part: gouges into the part (depth, area, which op), material left on the part, stock left "
              "outside it, rapids through material, and MACHINE COLLISIONS: the collet nut / spindle nose / head against the "
              "remaining stock (deep narrow cuts), the tool through the spoilboard into the bed, and the holder against the "
              "4th-axis chuck or tailstock. Shows the result in the CAM tab (the Machine toggle plays it on the machine model). "
              "Flat setups (top/flipped) and 4th-axis setups are simulated separately.",
              {"type": "object", "properties": {"ops": {"type": "array", "items": {"type": "integer"}},
                                                "resolution": {"type": "number"}}, "required": []})
        async def simulate_cam(args: dict[str, Any]) -> dict[str, Any]:
            if agent.program is None:
                return {"content": [{"type": "text", "text": "no CAM program built"}], "is_error": True}
            ops = [int(i) - 1 for i in args["ops"]] if args.get("ops") else None
            try:
                res = await agent.simulate(ops, args.get("resolution"))
            except cam.CamError as e:
                return {"content": [{"type": "text", "text": f"simulation failed: {e}"}], "is_error": True}
            return {"content": [{"type": "text", "text": res.summary()}]}

        @tool("check_interference",
              "Exact interference check between bodies (OCCT booleans): every pair whose solids overlap, with the "
              "overlap volume, centre and extent. Touching faces don't count; overlaps on registered thread axes "
              "(a bolt in a tapped hole) are listed separately as expected. Use it after building or editing an "
              "assembly; fix real interference before reporting done. Optional `bodies` limits the check.",
              {"type": "object", "properties": {"bodies": {"type": "array", "items": {"type": "string"}}}, "required": []})
        async def check_interference(args: dict[str, Any]) -> dict[str, Any]:
            if agent.model is None:
                return {"content": [{"type": "text", "text": "no model built"}], "is_error": True}
            import analysis
            try:
                res = await agent.interference(args.get("bodies") or None, mesh=False)
            except ck.CadError as e:
                return {"content": [{"type": "text", "text": str(e)}], "is_error": True}
            return {"content": [{"type": "text", "text": analysis.summary(res)}]}

        @tool("check_motion",
              "Collision check through the design's motion: runs the driven joint (drive() in the script) through its "
              "range in `steps` poses and reports bodies that hit each other because of the motion (overlap beyond what "
              "they already share at rest). Use it after adding joints with revolute()/slider()/couple().",
              {"type": "object", "properties": {"steps": {"type": "integer", "default": 12}}, "required": []})
        async def check_motion(args: dict[str, Any]) -> dict[str, Any]:
            if agent.model is None:
                return {"content": [{"type": "text", "text": "no model built"}], "is_error": True}
            import analysis
            try:
                res = await agent.motion_check(int(args.get("steps") or 12))
            except ck.CadError as e:
                return {"content": [{"type": "text", "text": str(e)}], "is_error": True}
            return {"content": [{"type": "text", "text": analysis.motion_summary(res)}]}

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
              "controller, units, travel{x,y,z}, max_feed{x,y,z}, rapid, spindle{min,max}, tool_change (pause|atc|manual|none|split), post (grbl|makera|haas|linuxcnc), "
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

        @tool("configurations", "Named variants of the design: the script's `configurations = {name: {parameter: value}}` dict "
              "overrides top-level numeric parameters. action=list | set (name, values: create or replace one variant) | "
              "delete (name) | activate (name, or omit it for the base values: rebuilds the viewer with that variant, the script "
              "is unchanged) | export (formats e.g. [\"step\",\"stl\"], names?: every variant and the base when omitted; files are "
              "exports/<design>-<name>.<fmt>). You can also write the dict in the script directly.",
              {"type": "object", "properties": {"action": {"type": "string", "enum": ["list", "set", "delete", "activate", "export"]},
                                                "name": {"type": "string"}, "values": {"type": "object"},
                                                "formats": {"type": "array", "items": {"type": "string"}},
                                                "names": {"type": "array", "items": {"type": "string"}},
                                                "tolerance": {"type": "number"}}, "required": ["action"]})
        async def configurations_tool(args: dict[str, Any]) -> dict[str, Any]:
            a = args["action"]

            def ok(text: str) -> dict[str, Any]:
                return {"content": [{"type": "text", "text": text}]}
            try:
                if agent.model is None:
                    return {"content": [{"type": "text", "text": "no design yet"}], "is_error": True}
                if a == "list":
                    st = agent.config_state()
                    lines = [f"active: {st['active'] or '(base values)'}"]
                    for n, vals in st["configurations"].items():
                        lines.append(f"- {n}: " + ", ".join(f"{k}={v:g}" for k, v in vals.items()))
                    return ok("\n".join(lines) if st["configurations"] else "no configurations; base parameters: "
                              + ", ".join(f"{p['name']}={p['base']:g}" for p in st["params"]))
                if a == "set":
                    cfgs = agent.configurations(); cfgs[args["name"]] = {k: float(v) for k, v in (args.get("values") or {}).items()}
                    await agent.set_configurations(cfgs, source="agent")
                    return ok(f"configuration {args['name']!r} set; configurations: {', '.join(agent.configurations())}")
                if a == "delete":
                    cfgs = agent.configurations(); cfgs.pop(args["name"], None)
                    await agent.set_configurations(cfgs, source="agent")
                    return ok(f"deleted; configurations: {', '.join(agent.configurations()) or 'none'}")
                if a == "activate":
                    await agent.activate_configuration(args.get("name") or None, source="agent")
                    return ok(f"active configuration: {agent.config or '(base values)'}\n" + agent.model.summary(6))
                if a == "export":
                    paths = await agent.export_configurations([f.lower() for f in (args.get("formats") or ["step"])], args.get("names"),
                                                              float(args.get("tolerance") or 0.01))
                    return ok("exported:\n" + "\n".join(str(p) for p in paths))
                return {"content": [{"type": "text", "text": f"unknown action {a!r}"}], "is_error": True}
            except Exception as e:  # noqa: BLE001
                return {"content": [{"type": "text", "text": f"failed: {e}"}], "is_error": True}

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
                                                "sheet": {"type": "string", "enum": ["A4", "A3"], "default": "A4"}, "notes": {"type": "string"},
                                                "units": {"type": "string", "enum": ["mm", "in"], "description": "dimension units on the sheet (default: the user's units)"}}, "required": []})
        async def make_drawings(args: dict[str, Any]) -> dict[str, Any]:
            try:
                res = await agent.make_drawings(args.get("material") or "", args.get("density"), args.get("sheet") or "", args.get("notes") or "", args.get("units") or "")
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

        @tool("kit", "The Design Kit: ready-made standard components (call them in scripts as kit.<name>(...)) and design guides. "
              "action=toc (category?: the contents, most used first) | search (query, kind?: component|guide, category?, tags?) | "
              "read (id: a guide's text, or a component's signature, parameters, standard, example and notes) | "
              "note (id, text: add a dated lesson to an entry for next time) | save_guide (slug, title, category, summary, text, "
              "tags?, related?: a markdown guide in this workspace's kit) | save_part (module, code: a Python module whose "
              "functions are decorated @component(category=..., summary=..., tags=[...]) - build123d and helpers are pre-imported; "
              "they become kit.<function>). Categories: " + ", ".join(__import__("designkit").CATEGORIES) + ".",
              {"type": "object", "properties": {"action": {"type": "string", "enum": ["toc", "search", "read", "note", "save_guide", "save_part"]},
                                                "query": {"type": "string"}, "id": {"type": "string"}, "kind": {"type": "string"},
                                                "category": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}},
                                                "text": {"type": "string"}, "slug": {"type": "string"}, "title": {"type": "string"},
                                                "summary": {"type": "string"}, "related": {"type": "array", "items": {"type": "string"}},
                                                "module": {"type": "string"}, "code": {"type": "string"}}, "required": ["action"]})
        async def kit_tool(args: dict[str, Any]) -> dict[str, Any]:
            k = ck.design_kit(agent.workspace)
            a = args["action"]

            def ok(text: str) -> dict[str, Any]:
                return {"content": [{"type": "text", "text": text}]}
            try:
                if a == "toc":
                    return ok(k.toc(category=args.get("category")))
                if a == "search":
                    hits = k.search(args.get("query") or "", args.get("kind"), args.get("category"), args.get("tags"))
                    return ok("\n".join(e.line(k.usage()) for e in hits) or "nothing matches; try other words or `toc`")
                if a == "read":
                    return ok(k.read(args.get("id") or ""))
                if a == "note":
                    eid = k.note(args.get("id") or "", args.get("text") or "")
                    return ok(f"noted on {eid}")
                if a == "save_guide":
                    gid = await asyncio.to_thread(k.save_guide, args.get("slug") or args.get("title") or "guide", args.get("title") or "",
                                                  args.get("category") or "", args.get("summary") or "", args.get("text") or "",
                                                  args.get("tags"), args.get("related"))
                    return ok(f"saved guide {gid}; the workspace TOC is updated")
                if a == "save_part":
                    names = await asyncio.to_thread(k.save_part, args.get("module") or "parts", args.get("code") or "")
                    return ok("saved components: " + ", ".join(f"kit.{n}" for n in names) + "; the workspace TOC is updated")
                return {"content": [{"type": "text", "text": f"unknown kit action {a!r}"}], "is_error": True}
            except Exception as e:  # noqa: BLE001
                return {"content": [{"type": "text", "text": f"failed: {e}"}], "is_error": True}

        @tool("sketch", "Read or edit UI-editable sketches (the `# sketch:NAME {...}` blocks). action=list | get (name) | "
              "set (name, plane, items: creates or replaces the block; the user can then edit it graphically) | delete (name). "
              "plane = {origin:[x,y,z], x_dir:[..], z_dir:[..], label}; items = list of {type:'rect',cx,cy,w,h,angle} | "
              "{type:'circle',cx,cy,r} | {type:'polygon',pts:[[x,y],..]} | {type:'slot',x1,y1,x2,y2,w} | "
              "{type:'profile',start:[x,y],segs:[{to:[x,y]} (line) | {to:[x,y],via:[x,y]} (three-point arc through via), ..]} "
              "(a closed outline of lines and arcs; it closes back to start), each with mode 'add'|'subtract'; "
              "optional constraints = [{type, refs, value?}] solved before the code is written: coincident [pt, pt], "
              "on [pt, edge|circle], horizontal/vertical [edge] or [pt, pt], parallel/perpendicular [edge, edge], "
              "tangent, equal, concentric, midpoint [pt, edge], fix [pt] (value [x, y]) and dimensions length [edge], "
              "distance [pt, pt|edge], hdistance/vdistance [pt, pt], radius/diameter [circle], angle [edge, edge] (deg). "
              "refs: point {i, p} (rect c/p0..p3, circle c, slot a/b, polygon p0.., profile s / e<k> segment end / v<k> arc "
              "through point), edge {i, e: k} (rect side 0 bottom..3 left, polygon side, profile segment, slot 0), circle {i} "
              "(or {i, e: k} for a profile arc), i = item index; "
              "coordinates are plane-local mm. Prefer this over rewriting sketch blocks by hand.",
              {"type": "object", "properties": {"action": {"type": "string", "enum": ["list", "get", "set", "delete"]},
                                                "name": {"type": "string"}, "plane": {"type": "object"},
                                                "items": {"type": "array", "items": {"type": "object"}},
                                                "constraints": {"type": "array", "items": {"type": "object"}}}, "required": ["action"]})
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
                await agent.set_sketch(name, plane, list(args.get("items") or []), by_agent=True, constraints=list(args.get("constraints") or []))
                sk = next((s for s in agent.model.sketches if s["name"] == name), None)
                return {"content": [{"type": "text", "text": f"sketch '{name}' set: " + (f"{sk['faces']} face(s), area {sk['area']}" if sk else "built with no faces (check item modes/overlaps)")}]}
            except (ck.CadError, script_edit.Unsupported) as e:
                return {"content": [{"type": "text", "text": f"sketch failed: {e}"}], "is_error": True}

        @tool("extension", "Extensions: Python files that extend the app itself (script helpers, agent tools, UI panels, build "
              "checks). action=list | api (the full API reference: read it before writing one) | read (module) | save (module, code: "
              "validated; a file that does not load or registers nothing is refused) | delete (module) | call (name, args: run an "
              "extension tool now, no restart) | open (panel: show a panel in the viewer). Built-in examples: parameter_explorer, "
              "hole_report, bolt_pattern. New @tool functions also become first-class ext_<name> tools after a session restart.",
              {"type": "object", "properties": {"action": {"type": "string", "enum": ["list", "api", "read", "save", "delete", "call", "open"]},
                                                "module": {"type": "string"}, "code": {"type": "string"}, "name": {"type": "string"},
                                                "args": {"type": "object"}, "panel": {"type": "string"}}, "required": ["action"]})
        async def extension_tool(args: dict[str, Any]) -> dict[str, Any]:
            a = args["action"]

            def ok(text: str) -> dict[str, Any]:
                return {"content": [{"type": "text", "text": text}]}
            try:
                if a == "list":
                    return ok(agent.extensions.describe() + "\n\nPanels: " + (", ".join(e.id for e in agent.extensions.panels()) or "none") +
                              "\nTools: " + (", ".join(e.id for e in agent.extensions.tools(agent_only=True)) or "none"))
                if a == "api":
                    return ok(ext_mod.API_DOC)
                if a == "read":
                    return ok(agent.extensions.read(args.get("module") or ""))
                if a == "save":
                    r = await asyncio.to_thread(agent.extensions.save, args.get("module") or "", args.get("code") or "")
                    await agent.emit(agent.extensions.payload())
                    note = " New tools become first-class ext_<name> tools after a session restart; until then use `extension call`." if r["tools"] else ""
                    if r["panels"]:
                        ids = [e.id for e in agent.extensions.panels() if e.module == r["module"]]
                        note += f" The panel is in the Extensions ribbon group; show it now with `extension open {ids[0] if ids else r['panels'][0]}`."
                    return ok(f"saved extension {r['module']}: " + "; ".join(f"{k} {', '.join(v)}" for k, v in r.items() if k != "module" and v) + note)
                if a == "delete":
                    await asyncio.to_thread(agent.extensions.delete, args.get("module") or "")
                    await agent.emit(agent.extensions.payload())
                    return ok("deleted")
                if a == "call":
                    return ok(await agent.ext_call(args.get("name") or "", args.get("args") or {}))
                if a == "open":
                    await agent.ext_render(args.get("panel") or "", {})
                    return ok("panel opened")
                return {"content": [{"type": "text", "text": f"unknown extension action {a!r}"}], "is_error": True}
            except Exception as e:  # noqa: BLE001
                return {"content": [{"type": "text", "text": f"failed: {e}"}], "is_error": True}

        @tool("show_panel", "Show a one-off UI panel in the viewer (not saved): a table, key/values, text, images or buttons that "
              "ask you something (`chat`) or act on the view (`action`: select / highlight faces and bodies). `ui` is a UI schema "
              "(see `extension api`), e.g. {\"type\":\"panel\",\"children\":[{\"type\":\"table\",\"columns\":[..],\"rows\":[[..]]}]}. "
              "For something reusable with live controls, save an extension with a @panel instead.",
              {"type": "object", "properties": {"title": {"type": "string"}, "ui": {"type": "object"}}, "required": ["title", "ui"]})
        async def show_panel(args: dict[str, Any]) -> dict[str, Any]:
            try:
                ui = dict(args["ui"]); ui.setdefault("type", "panel"); ui["title"] = args["title"]
                ext_mod.validate_ui(ui)
                await agent.emit({"type": "ext_panel", "id": f"eph:{int(time.time() * 1000)}", "title": args["title"], "ui": ui, "state": {}, "ephemeral": True})
            except Exception as e:  # noqa: BLE001
                return {"content": [{"type": "text", "text": f"invalid panel: {e}"}], "is_error": True}
            return {"content": [{"type": "text", "text": "panel shown"}]}

        # extension @tool functions as first-class tools (ext_<name>): the set is fixed at connect, so new ones need a restart
        ext_tools = []
        for e in agent.extensions.tools(agent_only=True):
            def _mk(entry):
                async def h(args: dict[str, Any]) -> dict[str, Any]:
                    try:
                        return {"content": [{"type": "text", "text": await agent.ext_call(entry.id, args)}]}
                    except Exception as ex:  # noqa: BLE001
                        return {"content": [{"type": "text", "text": f"failed: {ex}"}], "is_error": True}
                return h
            ext_tools.append(tool(f"ext_{e.name}", f"[extension {e.module}] {e.description or e.name}", e.schema or {"type": "object", "properties": {}})(_mk(e)))

        # 3D printing: only offered when a slicer is installed on this machine (nothing is bundled)
        slicing_tools = []
        if slicer.find_slicer() is not None:
            @tool("slicer_info", "The installed 3D-printing slicer, the printers it knows and, for `machine`, the compatible process "
                  "(quality) and filament profiles plus the printer currently selected in the slicer. Call before slice_for_printing "
                  "when the user names a printer or material you have not seen.",
                  {"type": "object", "properties": {"machine": {"type": "string"}, "search": {"type": "string", "description": "filter printers by substring"}}})
            async def slicer_info(args: dict[str, Any]) -> dict[str, Any]:
                info = slicer.find_slicer()
                if info is None:
                    return {"content": [{"type": "text", "text": "no slicer installed"}], "is_error": True}
                out: dict[str, Any] = {"slicer": f"{info.name} {info.version}".strip(), "default_machine": slicer.default_machine(info)}
                q = (args.get("search") or "").lower()
                ms = [m for m in slicer.machines(info) if not q or q in m["name"].lower()]
                out["printers"] = [m["name"] for m in ms][:80] + ([f"... {len(ms) - 80} more, use search"] if len(ms) > 80 else [])
                if args.get("machine"):
                    try:
                        pf = slicer.profiles_for(info, args["machine"])
                    except slicer.SlicerError as e:
                        return {"content": [{"type": "text", "text": str(e)}], "is_error": True}
                    out["processes"] = [p["name"] for p in pf["processes"]]
                    out["filaments"] = [f["name"] for f in pf["filaments"]]
                    out["overrides"] = list(slicer.OVERRIDE_KEYS)
                return {"content": [{"type": "text", "text": json.dumps(out)}]}

            @tool("slice_for_printing", "Slice the design (or one `body`) for 3D printing with the installed slicer and show the layers in the "
                  "viewer. `machine` is a printer profile name (default: the printer selected in the slicer), `process` a quality profile "
                  "and `filament` a filament profile (defaults: the printer's defaults). Common overrides: layer_height (mm), "
                  "sparse_infill_density (percent), enable_support (bool), brim_type (no_brim|outer_only|outer_and_inner|auto_brim), "
                  "wall_loops, top_shell_layers, bottom_shell_layers. Returns layers, height, time and filament estimates; the "
                  "G-code and 3MF are saved in workspace/slicing.",
                  {"type": "object",
                   "properties": {"machine": {"type": "string"}, "process": {"type": "string"}, "filament": {"type": "string"},
                                  "body": {"type": "string"},
                                  "layer_height": {"type": "number"}, "sparse_infill_density": {"type": "number"},
                                  "enable_support": {"type": "boolean"}, "brim_type": {"type": "string"},
                                  "wall_loops": {"type": "integer"}, "top_shell_layers": {"type": "integer"}, "bottom_shell_layers": {"type": "integer"}},
                   "required": []})
            async def slice_for_printing(args: dict[str, Any]) -> dict[str, Any]:
                ov = {k: args[k] for k in slicer.OVERRIDE_KEYS if args.get(k) is not None}
                try:
                    res = await agent.slice_model(args.get("machine"), args.get("process"), args.get("filament"), ov, args.get("body"))
                except (slicer.SlicerError, ck.CadError) as e:
                    return {"content": [{"type": "text", "text": f"slicing failed: {e}"}], "is_error": True}
                return {"content": [{"type": "text", "text": res.summary() + f"\nG-code: {res.gcode}\n3MF: {res.three_mf}"}]}

            slicing_tools = [slicer_info, slice_for_printing]

        tools = [build_model, *([] if LEGACY_BUILD else [edit_model]), inspect_model, screenshot, export_model, get_code, save_design,
                 cam_context, build_cam, get_cam_code, export_gcode, simulate_cam, check_interference, check_motion, feeds_speeds, save_machine, save_tool,
                 get_parameters, set_parameters, measure_tool, mass_properties, make_drawings, library_tool, kit_tool, sketch_tool,
                 extension_tool, show_panel, configurations_tool] + ext_tools + slicing_tools
        self.tool_handlers = {t.name: t.handler for t in tools}     # name -> async handler (tests call these directly)
        return create_sdk_mcp_server("cad", "0.4.0", tools=tools)


    # ------------------------------------------------------------------ 3D printing (external slicer)
    def slicer_payload(self) -> dict[str, Any]:
        """What the UI needs to show (or hide) the 3D-printing section."""
        info = slicer.find_slicer()
        return {"type": "slicer", "installed": info is not None, "slicer": info.to_dict() if info else None,
                "default_machine": slicer.default_machine(info) if info else None,
                "result": self.slice_result.to_dict() | {"summary": self.slice_result.summary()} if self.slice_result else None}

    async def slice_model(self, machine: str | None = None, process: str | None = None, filament: str | None = None,
                          overrides: dict[str, Any] | None = None, body: str | None = None,
                          walls_only: bool = False) -> slicer.SliceResult:
        """Export the design (or one body) as STL, slice it with the installed slicer and publish the layers to
        the viewer (`sliced` event). Raises SlicerError / CadError."""
        info = slicer.find_slicer()
        if info is None:
            raise slicer.SlicerError("no slicer installed (OrcaSlicer or Bambu Studio)")
        if self.model is None or not self.model.bodies:
            raise ck.CadError("nothing to slice: the design is empty")
        pr = self.settings.get("print") or {}
        machine = machine or pr.get("machine") or slicer.default_machine(info)
        if not machine:
            raise slicer.SlicerError("no printer chosen and the slicer has no default; pass `machine`")
        if machine == pr.get("machine"):          # saved quality/filament only make sense for the saved printer
            process = process or pr.get("process") or None
            filament = filament or pr.get("filament") or None
        saved = {k: pr.get(k) for k in self.PRINT_OVERRIDE_KEYS if pr.get(k) not in (None, "", False)}
        overrides = {**saved, **{k: v for k, v in (overrides or {}).items() if v is not None}}
        out = self.workspace / "slicing"
        out.mkdir(exist_ok=True)
        paths = await asyncio.to_thread(ck.export, self.model, out, "model", ["stl"], 0.02, 0.1, body)
        res = await asyncio.to_thread(slicer.slice_file, info, paths[0], machine, process, filament, overrides, out)
        text = await asyncio.to_thread(Path(res.gcode).read_text, "utf-8", "replace")
        layers = await asyncio.to_thread(slicer.parse_gcode, text, res.translate, walls_only)
        self.slice_result, self.slice_layers = res, layers
        await self.emit({"type": "sliced", "result": res.to_dict() | {"summary": res.summary()}, "layers": layers})
        return res

    # ------------------------------------------------------------------ settings (model, effort, MCP servers)
    DEFAULT_SETTINGS: dict[str, Any] = {
        "model": "claude-opus-5-5",        # default model; "" = the Claude Code default
        "effort": "",                      # "" = default; low | medium | high | xhigh | max
        "max_turns": 60,
        "draft_threads": True,             # agent builds during a turn model real=True threads plain; real build at the end
        "thinking_display": "omitted",     # omitted | summarized (shows the thinking summary in chat)
        "web": True,                       # allow WebFetch / WebSearch
        "mcp_servers": {},                 # name -> {type: stdio|http|sse, command, args, env, url, headers, enabled}
        "extra_prompt": "",                # appended to the system prompt (house rules, machine notes...)
        "api_key": "",                     # Anthropic API key (alternative to the Claude Code login); kept in settings.json (0600)
        "units": "",                       # display/default units: "" = auto (US timezone → in, else mm) | mm | in
        "drawings": {"material": "", "sheet": "A4"},   # shop-drawing defaults (ribbon Drawings button, agent tool without args)
        "print": {"machine": "", "process": "", "filament": "", "layer_height": None, "sparse_infill_density": None,
                  "wall_loops": None, "enable_support": False, "brim_type": ""},   # 3D-printing defaults (ribbon Slice button)
    }
    PRINT_OVERRIDE_KEYS = ("layer_height", "sparse_infill_density", "wall_loops", "enable_support", "brim_type")
    MODELS = ["claude-opus-5-5", "claude-fable-5-1", "claude-sonnet-5", "claude-opus-5", "claude-opus-4-8", "claude-haiku-4-5"]

    def _settings_path(self) -> Path:
        return self.workspace / "settings.json"

    def _load_settings(self) -> dict[str, Any]:
        st = dict(self.DEFAULT_SETTINGS)
        try:
            st.update(json.loads(self._settings_path().read_text()))
        except Exception:
            pass
        for k in ("drawings", "print"):            # nested groups: keep new default keys when an older settings.json lacks them
            st[k] = {**self.DEFAULT_SETTINGS[k], **(st.get(k) or {})}
        return st

    def save_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        st = {**self.settings, **{k: v for k, v in patch.items() if k in self.DEFAULT_SETTINGS}}
        if st.get("effort") not in ("", "low", "medium", "high", "xhigh", "max"):
            raise ValueError("effort must be one of low, medium, high, xhigh, max")
        st["max_turns"] = int(st.get("max_turns") or 60)
        if st.get("units", "") not in ("", "mm", "in"):
            raise ValueError("units must be mm, in, or empty for automatic")
        st["drawings"] = {**self.DEFAULT_SETTINGS["drawings"], **{k: v for k, v in (st.get("drawings") or {}).items() if k in self.DEFAULT_SETTINGS["drawings"]}}
        if st["drawings"]["sheet"] not in ("A4", "A3"):
            raise ValueError("drawings.sheet must be A4 or A3")
        st["print"] = {**self.DEFAULT_SETTINGS["print"], **{k: v for k, v in (st.get("print") or {}).items() if k in self.DEFAULT_SETTINGS["print"]}}
        for k in ("layer_height", "sparse_infill_density", "wall_loops"):
            v = st["print"].get(k)
            st["print"][k] = None if v in (None, "") else float(v)
        for name, cfg in (st.get("mcp_servers") or {}).items():
            t = cfg.get("type") or ("http" if cfg.get("url") else "stdio")
            if t == "stdio" and not cfg.get("command"):
                raise ValueError(f"MCP server '{name}': stdio servers need a command")
            if t in ("http", "sse") and not cfg.get("url"):
                raise ValueError(f"MCP server '{name}': {t} servers need a url")
            cfg["type"] = t
        self.settings = st
        path = self._settings_path()
        path.write_text(json.dumps(st, indent=2))
        try:
            path.chmod(0o600)                  # may hold an API key
        except OSError:
            pass
        return st

    def units(self) -> str:
        """Display / default units: the setting, or the timezone-based guess when unset."""
        return self.settings.get("units") or un.detect_default_units()[0]

    def public_settings(self) -> dict[str, Any]:
        """Settings safe to send to the browser: the API key is never returned, only whether one is set."""
        st = {k: v for k, v in self.settings.items() if k != "api_key"}
        st["api_key_set"] = bool(self.settings.get("api_key"))
        guess, reason = un.detect_default_units()
        st["units_resolved"] = self.settings.get("units") or guess
        st["units_auto"] = guess; st["units_auto_reason"] = reason
        return st

    def auth_status(self) -> dict[str, Any]:
        return claude_auth_status(find_claude_cli(), self.settings.get("api_key") or None)

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
                              "auth_required": self.auth_problem,
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
        builtin: list[str] = []                    # no Bash/Edit/Write/Read: the agent works through the CAD tools only
        if st.get("web", True):
            allowed += ["WebFetch", "WebSearch"]; builtin = ["WebFetch", "WebSearch"]
        u = self.units()
        prompt = SYSTEM_PROMPT.replace("{UNITS_LONG}", "inches" if u == "in" else "millimetres").replace("{UNITS}", u)
        if LEGACY_BUILD:
            for new, legacy in LEGACY_PROMPT_SWAPS:
                assert new in prompt, "legacy swap out of date"
                prompt = prompt.replace(new, legacy)
        if len(mcp) > 1:
            prompt += "\n\nExternal MCP servers connected (their tools are prefixed mcp__<server>__): " + ", ".join(n for n in mcp if n != "cad") + "."
        if slicer.find_slicer() is not None:
            prompt += ("\n\n3D printing: a slicer is installed on this computer, so `slice_for_printing` (and `slicer_info`) are available. "
                       "When the user wants to print a part, slice it and report layers/time/filament; the layers appear in the viewer. "
                       "Do not invent printer or filament names: take them from `slicer_info`.")
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
            tools=builtin,
            strict_mcp_config=True,                # only the servers configured here (cad + Settings ▸ MCP), not the user's claude.ai connectors
            permission_mode="dontAsk",
            include_partial_messages=True,
            cwd=str(self.workspace),
            setting_sources=[],
            model=st.get("model") or os.environ.get("AGENTICCAD_MODEL") or None,
            max_turns=int(st.get("max_turns") or 60),
            # keep the CAD tools loaded up front instead of deferred behind ToolSearch
            env={"ENABLE_TOOL_SEARCH": "false", **({"ANTHROPIC_API_KEY": st["api_key"]} if st.get("api_key") else {})},
            max_buffer_size=8 * 1024 * 1024,   # screenshots + big tool results (default 1 MB is too small)
            **kw,
        )
        cli = find_claude_cli()                 # not bundled in the desktop app; GUI PATH is minimal
        if cli:
            options.cli_path = cli
        self.client = ClaudeSDKClient(options=options)
        await self.client.connect()
        self.auth_problem = None

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
            self._agent_turn = True
            await self._pump()
            for attempt in range(2):                     # real threads at the end; one chance for the agent to fix a failure
                self._agent_turn = False
                err = await self.finalize_threads()
                if err is None:
                    break
                if attempt == 1:
                    await self.emit({"type": "error", "text": f"The real threads failed to build, so the design keeps plain threads: {err}"})
                    break
                self._agent_turn = True
                await self.client.query("[App] Your builds used draft threads; the final build with the real threads (real=True) "
                                        f"failed:\n{err}\nFix the script so it also builds with real threads (for example make "
                                        "that one hole or screw real=False), then finish; the app rebuilds with real threads again.")
                await self._pump()
        except Exception as e:  # noqa: BLE001
            await self.emit({"type": "error", "text": f"{type(e).__name__}: {e}"})
        finally:
            self._agent_turn = False
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
                        if AUTH_ERROR_RE.search(blk.text or ""):
                            self.auth_problem = blk.text.strip()[:300]
                            await self.emit({"type": "agent_auth_required", "text": self.auth_problem})
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
                if msg.is_error and AUTH_ERROR_RE.search(str(getattr(msg, "result", "") or "")):
                    self.auth_problem = str(msg.result)[:300]
                    await self.emit({"type": "agent_auth_required", "text": self.auth_problem})
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
