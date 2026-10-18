# AgenticCAD — agent-native CAD/CAM

![AgenticCAD in a real session: describe a part to the Assistant, the exact model appears, click a face, tweak it from the Design toolbar](docs/demo.gif)

Describe the part; a Claude agent writes [build123d](https://build123d.readthedocs.io) code, the exact
OCCT kernel builds it, and the browser shows it live. Click faces, edges and bodies to reference them in
chat, or use the Fusion-style toolbar to model by hand — every manual operation is written into the same
script, so the design is always one reproducible Python file. Then generate GRBL toolpaths (adaptive
clearing, contours with tabs, drilling, 3D finishing), shop drawings, STEP/STL and G-code, and run the program on a
Makera Z1 from the same window. CAM and machine control are **experimental**.

> Website, docs and simulated tutorials: **https://agenticcad.github.io/agenticcad/**

> Free for non-commercial use under the PolyForm Noncommercial 1.0.0 licence. Commercial use is A$99 per user per year — see [Licence](#licence).

```
browser (three.js viewer · toolbar · sketch editor · chat) <-- websocket -->  server.py (FastAPI)
                                                                               |-- agent.py       Claude Agent SDK session + in-process MCP tools
                                                                               |-- cad_kernel.py  build123d exec, exact B-rep, display meshing, export
                                                                               |-- cam_kernel.py  2.5D/3D toolpaths, GRBL post
                                                                               |-- drawing.py / threads.py / library.py / script_edit.py
                                                                               `-- workspace/     designs, library, machines, tools, history, exports
```

## Run

**Desktop app** (macOS Apple Silicon `.pkg`, Windows `.msi`; both show the licence and terms to accept during install): download from the
[website](https://agenticcad.github.io/agenticcad/#download) or the [releases](https://github.com/agenticcad/agenticcad/releases).
The app **requires [Claude Code](https://docs.anthropic.com/en/docs/claude-code/setup) installed separately**
(it is Anthropic's tool and is not bundled); the app shows a setup page with the install and sign-in steps if
it is missing, and a "not signed in" banner with two fixes (run `claude` and log in, or paste an API key in Settings) when the login has expired. Builds are unsigned for now (macOS: if the installer is blocked, right-click → Open or System Settings → Privacy & Security → Open Anyway; Windows: SmartScreen → More info → Run anyway).
Data lives in the OS user-data folder (`~/Library/Application Support/AgenticCAD`, `%LOCALAPPDATA%\AgenticCAD`).

**From source:**

```bash
cd agenticcad
.venv/bin/python server.py          # http://127.0.0.1:8765
```

Auth: the Agent SDK's bundled Claude Code binary uses your Claude Code login; set `ANTHROPIC_API_KEY`
to use an API key instead. The default model is Claude Opus 5.5 (`claude-opus-5-5`); change it in Settings ⚙ or with `AGENTICCAD_MODEL=<model id>`.

Setup from scratch: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`.

**Building the desktop app** ([Briefcase](https://briefcase.beeware.org), config in `pyproject.toml`):

```bash
.venv/bin/pip install briefcase==0.4.5
.venv/bin/briefcase create macOS && .venv/bin/briefcase build macOS && .venv/bin/briefcase package macOS --adhoc-sign
```
The `package` GitHub workflow does this for macOS and Windows on every published release and attaches the
installers. `cleanup_paths` strips the Claude Code binary that the Agent SDK wheel ships, so the app never
redistributes it; `agent.find_claude_cli()` locates the user's own install.

## Agent tools (in-process MCP server `cad`)

| tool | purpose |
|---|---|
| `build_model(code)` | replace the script, rebuild, return summary (bbox, volume, largest faces) or traceback |
| `edit_model(edits?, append?)` | change the existing script in place (exact-text replacements + code appended before `result`), rebuild; nothing applied if the build fails |
| `inspect_model(kind?, near?, limit?)` | list faces: id, type, area, centre, normal, size, radius |
| `screenshot(view, highlight_faces?, show_edges?)` | browser renders a 1024×768 PNG returned as an image block |
| `export_model(name?, formats?)` | STEP / STL into `workspace/exports/` |
| `get_code()` | current script |
| `save_design(name?)` | save the script into `workspace/designs/` (only when the user asks) |
| `kit(action, ...)` | the Design Kit: `toc`, `search`, `read` components and guides; `note`, `save_guide`, `save_part` into the workspace's kit |
| `slicer_info(machine?)`, `slice_for_printing(...)` | **only when a slicer is installed**: printers/profiles; slice for 3D printing and show the layers |

## UI

![AgenticCAD 1.0: the top bar with the File menu and the Design · CAM · Machine workspaces, the Design toolbar, Browser and Parameters docked on the left, Inspector · Assistant · Code on the right](docs/screenshot.jpg)

- **Layout**: one top bar with the file (its name is the File menu), the workspaces **Design · CAM · Machine**
  (CAM and Machine experimental), the machine's status (click it for the Machine workspace), **Assistant** and Settings.
  The right column is **Inspector · Assistant · Code** in every workspace; the Inspector shows what you are working on
  (the active command or the selection in Design, the selected operation or setup in CAM, the machine's steps in
  Machine). Design has a one-row toolbar (less-used tools under *More*, Output and Extensions as menus) with the
  Browser and Parameters docked on the left.
- Click a face → chip in the composer; the message gets a `[Selected geometry]` block with the face's
  type/centre/normal/size so the agent can select the same face in code. Shift-click for several.
- **Reference images**: attach photos, sketches or drawings with 📎, drag-and-drop onto the composer or
  viewer, or paste from the clipboard. They are downscaled in the browser (≤1600 px JPEG), sent inline with
  the message (the agent sees them directly), kept as thumbnails in the bubble, and saved to
  `workspace/images/` so the agent can re-read them. Dimensions in your text override what it estimates.
- The sent message keeps its selection chips ("with Bracket · #16 plane +Z 24×24"). The faces stay lit
  (dimmer orange) while the agent works, until the next rebuild or click. Hover a chip later to see a
  marker where that face was; click it to select the same face again (matched by type and centre,
  since ids are renumbered on every rebuild).
- Navigation: Fusion-style **ViewCube** top-right — click a face, edge or corner to look from there
  (animated, keeps zoom), drag it to orbit, `⌂` for the home/fit view. Left-drag orbits, right-drag (or ⌘/Ctrl/⇧ + left-drag on a trackpad) pans, wheel zooms.
- Bottom bar of the viewer: Fit, Edges, display-mesh quality, Undo (revert to previous build).
- File menu (the design's name in the top bar): New / Open / Save / Save as / Import .py / Import STEP / Download .py /
  **Export STEP** / **Export STL…** (dialog: resolution + all-or-one body) / Shop drawings / Part library.
- Right column: drag the divider to resize (double-click resets), `⟩` or ⌘B hides it, `⟨` on the right edge shows it again. Remembered per browser.
- Code (right column): the current script with Python syntax highlighting (CodeMirror); edit and Run (⌘↩) to rebuild without the agent.
- Every successful build is saved to `workspace/history/<ms>.py`.

## Designs (files)

A design **is** its build123d script. `File ▸ Save / Save as…` writes `workspace/designs/<name>.py`;
`Open…` lists them; `New` starts an empty design (a fresh workspace starts empty too); `Import .py…` uploads a script from disk;
`Download .py` gives you the current one. The File menu in the top bar shows the design name and a `•` when unsaved.
The last-open design is reopened on restart. The agent gets a `[Note]` when you open/import/undo or
edit the script by hand, so it re-reads the code before editing.

## Bodies and the Browser tree

`result` may be a single shape, a dict `{name: shape}`, or nested dicts for components:

```python
result = {"Base": {"Bracket": bracket.part, "Washer": washer}, "Pin": pin}
```

Bodies stay separate solids (STEP keeps them separate). A shape containing disjoint solids is split
into bodies automatically. The **Browser** panel (top-left) shows the tree: click a body to select it
(chip in chat), the dot toggles visibility, double-click zooms to it. Faces are numbered globally and
each face knows its body; `inspect_model(body=...)` filters by body and `export_model(body=...)`
exports one body.

**Rename / delete a body manually**: right-click a row in the Browser (or its `⋯`) → Rename…, Delete,
Hide/Show, Zoom to, Export STEP/STL of that body. Rename and Delete rewrite the script directly
([script_edit.py](script_edit.py), AST-based) when the body is a literal key in
`result = {...}` — instant, no agent turn, and Undo restores it. If the script doesn't name bodies
that way (e.g. `result = part`, or auto-split solids) the request is handed to the agent instead.
Deleting the last body at a level is refused.

## Parameters, measure, drawings, library

- **Parameters**: every top-level `name = number` in the script is an editable field in the Parameters card under the Browser (↑↓ ±1, ⇧ ±10, ⌥ ±0.1).
  Changing one rewrites that literal in the script and rebuilds; the agent is told. Agent tools:
  `get_parameters`, `set_parameters`.
- **Measure** (toolbar ▸ Inspect ▸ Measure, or `I`): click up to two faces/points in the viewer → point distance with Δxyz, minimum
  face distance, plane gap, centre distance, angle between normals, hole axis spacing; click bodies in the
  Browser for volume/mass/centre of mass, two bodies for clearance or interference volume. Agent tools:
  `measure`, `mass_properties` (material names or g/cm³).
- **Shop drawings** (toolbar ▸ Output ▸ Drawings, `D`, or File ▸ Shop drawings…; material and sheet in ⚙ Settings): third-angle front/top/right + iso with hidden
  lines, overall dimensions, hole callouts (n× Ø, THRU/depth), lettered holes with a coordinate table,
  title block (size, mass, scale, date). SVG per body + assembly, plus DXF of the view geometry
  ([drawing.py](drawing.py)). Agent tool: `make_drawings`.
- **STEP import** (File ▸ Import STEP…, or drop a .step on the viewer): as a new body in the current design
  (`import_step("file.step")` is added to the script), as a new design, or straight into the library.
- **Part library** (File ▸ Part library…, which opens it in the right column, and **+ Part** in the Browser header): parts are parametric scripts (whole design saved with
  its parameters) or exact STEP bodies, with description, tags and thumbnail, in `workspace/library/<part>/`.
  Browser ▸ **+ Part** inserts one (adds `from_library("name", param=value)` as a new body); right-click a
  body ▸ **Save to library…**; the Library pane: search, insert, delete, + Design (parametric), + STEP.
  The agent sees the part names in every turn and has `library` (list/search/get/insert/save_design/
  save_body/delete); it is told to check the library before modelling standard or reusable parts.
- **Measure** snaps: vertex > edge > face point, with a live readout (line length, arc radius and sweep,
  circle Ø). Two picks give distance with closest points drawn and labelled in the viewer, Δxyz, and the
  angle (line-line, line-plane, plane-plane) with parallel/perpendicular flags; two circles give centre
  spacing. Backspace/right-click removes the last pick, Esc clears. Agent `measure` accepts edge ids too.

## Install

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python server.py        # http://127.0.0.1:8765 (PORT env overrides)
```
Auth: the Claude Agent SDK uses your Claude Code login (or `ANTHROPIC_API_KEY`).

## Fidelity model

- The **kernel geometry is exact B-rep** (OCCT): planes, cylinders, cones, spheres, tori, B-splines.
  `inspect_model` reports the analytic face type and radius; the viewer's status line lists them.
- The **viewer mesh is display-only**. It is generated from the exact shape with exact per-vertex surface
  normals (so cylinders shade perfectly round) at a preset chordal/angular deviation — `Mesh: draft /
  normal / fine` in the toolbar re-tessellates without rebuilding. Face picking maps triangles back to
  B-rep faces, so a click always selects the true face.
- **STEP export is exact** (true `CYLINDRICAL_SURFACE` etc.; no triangles).
- **STL is tessellated only at export time**, at the deviation you choose (toolbar dropdown, or the
  agent's `export_model(tolerance, angular_tolerance)`); it never reuses the display mesh.

## CAM (GRBL, Makera Carvera, Haas, LinuxCNC) — experimental

> **Experimental.** Toolpaths are geometrically correct, machine-checked and can be simulated, but not optimised
> (long, conservative paths, many retracts). Inspect and simulate every program before running it. Modelling,
> export and drawings are the stable part today.

Agent-native, like the CAD side: a second script per design, `cam.py`, written by the agent against
[cam_kernel.py](cam_kernel.py) and built with the `build_cam` tool. Saved with the design as `<name>.cam.py`.

- **Libraries**: `workspace/machines/*.json` (travel, max feeds, rapid, spindle range, safe/clearance Z,
  tool-change policy `pause|manual|none|split`, collet, 4th axis, `max_stepdown` per material for the feeds
  calculator, start/end G-code blocks) and `workspace/tools.json` (feeds, plunge, stepdown, stepover and the tool's
  shape). Edit them in **Settings ▸ Machines** (a form per machine, or JSON) and **Settings ▸ Tools** (a list and an
  editor that draws the tool to scale, with the feeds & speeds calculator; see [New in 1.0](#new-in-10-cam-and-machine--experimental)), or ask the agent
  (`save_machine`, `save_tool`).
- **Tool shapes** ([tool_shape.py](tool_shape.py)): flat, ball, bull nose (corner radius), V-bit / engraver (angle + tip
  Ø), chamfer, drill (point angle) and tapered ball (ball Ø + taper per side), each with its flute length, an optional
  neck (Ø and reach), the shank Ø (a 0.3 mm endmill on a 1/8" shank) and the stickout. One profile (radius against
  height above the tip) feeds everything: the tool drawn in the viewer, the simulation and machine model; 3D finishing
  drops the whole tool (cutting shape, neck, shank) onto the surface, so any type follows it without gouging; the 4th
  axis maps; 2.5D operations plan a non-flat tool with its width at the cut depth (the top of the cut lands on the
  boundary) and say what its shape leaves (scallop height, sloped walls); the simulator cuts with the real profile up
  to the flute length and reports the body above the flutes, a neck or a wider shank meeting the stock as collisions.
- **Per-operation tool settings**: click an operation in the CAM tree; the Inspector's *Tool & feeds* shows its tool's spindle speed, feed, plunge,
  stepdown and stepover. Blank fields use the tool's values; anything you type overrides them for that operation
  only (with a calculator to fill them for a material). Overrides are one line at the top of `cam.py`,
  `overrides({"Pocket": {"feed": 500, "stepover": 0.3}})`, keyed by operation name (`"Face#2"` for the second op
  with the same name), so they are saved with the design and the agent keeps them.
- **Geometry from the exact B-rep**: `section(part, z)`, `silhouette`, `stock_minus(setup, part, z)`,
  `holes(part)` (vertical round holes from concave cylindrical faces, with through detection),
  `face_polygon(model.get_face(id))` for a clicked face.
- **Operations**: `face`, `contour` (outside/inside/on, depth passes, tabs, climb), `pocket` (concentric,
  islands), `drill` (peck), `parallel3d` (heightmap raster finishing, ball or flat). All in model coords.
- **Post**: `Program.gcode()` → GRBL dialect (G21 G90 G94 G17, G54, M3 S, G0/G1, F on change, M6/M0 tool
  change pauses, M30). WCS origin choices: stock-top-left / stock-top-center / stock-bottom-left /
  stock-bottom-center / model-origin / (x, y, z). Checks: machine travel, spindle range, feeds clamped,
  cutting below stock bottom, multi-tool with `tool_change=none`.
- **UI**: the CAM workspace. The program, its setups, operations (colour/visibility) and the tools they use are a
  tree on the left, with warnings, *Export G-code* and a G-code preview; the Inspector edits the selected operation
  (Tool & feeds · Cut · Display) or setup (stock & origin, workholding, machine view). Toolpaths + stock + WCS origin
  are drawn in the viewer (rapids red dashed) with *Selected op / All* and *Toolpath / Stock / Machine* toggles, and a
  timeline of op-coloured blocks with Simulate, play, speed and a tool marker; `cam.py` is in Code (right column).
  *Send to machine →* opens Machine ▸ Run with the setup's operations.
- **Arcs**: the post fits G2/G3 to circular runs (chord-sagitta checked; `machine.arcs`, `machine.arc_tolerance`) in the form the machine's controller takes: `machine.arc_format` `"ij"` (centre offsets, every controller) or `"r"` (radius word; GRBL, Haas, LinuxCNC, not the Makera firmware), `machine.helical_arcs` for ramps. `cam.CONTROLLERS` lists what each post's controller accepts (arc forms, helical arcs, line length) with its source; collinear moves are merged. **Lead-in/out**: tangential quarter arcs on contours
  (`lead=`), start mid-way along the longest straight edge. **Adaptive**: see above. **Rest machining**:
  `rest_from=` on `pocket` / `adaptive` (2.5D, morphological opening) and `parallel3d` (3D tip-map difference).
  **Feeds & speeds**: `feeds(tool, material, machine)` / `apply_feeds(...)`, 13 materials, spindle and feed
  clamping, radial chip thinning; calculator in Settings ▸ Tools ("Apply to T*n*") and per operation in the CAM Inspector; agent tool `feeds_speeds`.
- **Setups**: a program can have several `Setup`s (`orient="top" | "bottom" | "front" | "back" | "left" |
  "right"`), each with its own WCS (G54, G55, …). Moves are in the setup frame; `setup.view(part)` gives the part
  as the machine sees it for `section`/`holes`. Between setups the post stops the spindle, parks and pauses for the
  operator to flip or re-fixture (Makera M600, GRBL M0), then restarts the spindle.
- **4th axis** (rotation about X, A in degrees): `Setup(..., rotary=True)` on a machine with `machine.rotary`.
  Indexed 3+1 (`Setup(..., rotary=True, a=90)`; setups on the same mounting share one WCS and just turn A) and
  continuous 4-axis: `rotary_rough` (rings or lines, from a radial height map), `rotary_finish` (lines, rings or a
  continuous spiral, ball or flat), `rotary_wrap` (pocket/contour a 2D pattern wrapped onto a cylinder).
  `Stock.cylinder(d, length)` for bar stock; checks for swing diameter, length and a missing/uninstalled axis.
- **Makera Carvera** (Z1 and Carvera Air, with and without the 4th axis, as built-in machines): a `makera` post
  written against the controller firmware: lines ≤ 63 characters, no N numbers, `M6 Tn` on one line (the machine
  measures the tool after each manual change), M3 only after a tool is active, G28 park, M600 between setups,
  9 work offsets, no arcs while A moves, and A-axis feeds computed the way the firmware interprets F on mixed
  linear + rotary moves. Collet size is checked (1/8" standard).
- **Rendered simulation**: *Rendered* on the CAM timeline plays the program as a studio render: metallic stock machined
  live in front of you (the viewer stamps the tool along every move), a chrome holder with a spinning fluted tool, the
  camera following the cut with depth of field (Settings ▸ General to turn it off), and a HUD with the step, tool
  changes and the speed factor.
- **Voxel stock**: the stock you watch being machined is a signed-distance voxel solid in a Web Worker
  ([static/voxel_stock.js](static/voxel_stock.js)), stamped with every move and meshed with surface nets, so walls
  are smooth and floors exact; Settings ▸ General ▸ *Simulation stock* falls back to a lighter height map.
- **Playback**: the CAM timeline plays the program at the machine's real pace (1×) scaled by a speed control (¼× to 100×,
  on the timeline; the choice is remembered): moves take their
  real time at the programmed feed and the machine's rapid rate, the tool moves continuously, ⏮ / ⏭ jump between
  operations, the CAM tree shows setup headers (▶ *Play from here* / *Play this setup* in the Inspector), and on
  the Machine view a flipped setup turns the work over on the table.
- **Entries**: pocket and contour passes ramp into the material at 3° (`ramp=`, 0 to plunge) — pockets along the
  innermost ring that is long enough (a helix on a small ring), contours along the profile with one entry per ring
  and no retract between passes — and face passes plunge clear of the stock; helical ramps post as G2/G3 arcs.
- **Simulation**: `simulate_cam` (agent) or **Simulate** on the CAM timeline runs a material-removal simulation of all
  visible ops or one: a two-sided height map for top/bottom setups, a radial map for 4th-axis setups. The viewer
  shows the stock being cut in step with the slider, with gouges in red and material left in amber on the last
  frame; the summary reports removed volume, gouges (depth, area, which op), material left on the part, stock
  left outside it, and rapids through material. Side setups (front/back/left/right) are not simulated yet.
- **Machine models and collision checks** ([machine_models.py](machine_models.py)): every built-in machine has a
  kinematic 3D model built in build123d from published and measured numbers — Makera Z1 and Carvera Air (bed with the
  real hole grid, moving bed in Y under the rear bridge, head in X/Z, 4th-axis chuck and tailstock, enclosure), Haas
  VF-2 / VF-2SS / VF-4 / Mini Mill / TM-1 (table with the real T-slots, saddle, column, CAT40 + ER32 holder, nose-to-
  table range) and a parametric gantry router for everything else. **Machine** in the CAM toolbar (`A`) or the viewer's *Machine* toggle shows the
  program's machine around the stock and plays the simulation on it: head, table and chuck move as they would, the
  tool hangs from the spindle, and any part the simulator saw collide flashes red at that move. The simulation itself
  checks the whole stack above the flutes (shank, collet nut, nose, collar, head) against the remaining stock, the
  tool tip against the bed under the spoilboard, and the holder against the chuck and tailstock on 4th-axis jobs;
  contacts are listed in the summary with the op, move, position and depth. Every number carries its source and
  accuracy tier (`verified` / `reference` / `estimated`) in **Settings ▸ Machines ▸ Model**, where measured values
  (nose profile, stickout, spoilboard, clearance) can be entered per machine.
- **Model your own machine** ([machine_script.py](machine_script.py)): a machine's model is a script,
  `workspace/machines/<slug>.machine.py`, in the design-script namespace plus `node(name, parent, axis, mode, stock,
  pivot)`, `part(name, node, shape, material, collision)` and `machine(home, travel, clearance, nose, table, rotary,
  spoilboard, sources, notes)`, with helpers (`box`, `cyl_z/x/y`, `shell`, `bed_holes`, `import_step` for vendor CAD).
  The built-ins live in [machine_scripts/](machine_scripts/) and are the fallback for any machine without its own
  script, so **Code ▸ machine.py** always has a starting point: edit, Run, and the machine has its own model.
  The **Machine view** section of a setup's Inspector (CAM workspace) picks any library machine, shows or hides it and
  opens or closes its doors; door nodes (`node(..., door="hinge"|"slide", ...)`) animate in the viewer. **Settings ▸ Machines ▸
  Model** has *Show in viewer* and *Edit as script…*; the agent's
  `get_machine_code` / `build_machine` do the same from chat ("add my Shapeoko 5 Pro to the library and model it"), and
  photos dropped into the chat are used as references: the built-in Z1, Carvera Air and Haas VF-4 models were made that
  way from the makers' product photos, every number tagged verified / reference / estimated.
- **Fixtures** ([fixture_lib.py](fixture_lib.py)): `Setup(..., fixture="6\" Kurt-style vise", fixture_params={"parallel": 20})`
  holds the stock in a vise, on clamps or on a plate; the stock sits at the fixture's work origin, the viewer draws
  it around the stock and on the machine, and the simulator reports the tool or holder touching any fixture part as a
  collision. Built-ins: Makera low-profile vise, Carvera Air vise, 4" screwless vise, 6" Kurt-style vise, tooling
  plate, step clamps. Fixtures are scripts (`fixture_scripts/`, `workspace/fixtures/<slug>.fixture.py`) like machine
  models: `part(...)` + `fixture(name, work_origin, clamp_axis, ...)`, editable in **Code ▸ fixture.py**, listed in
  **Settings ▸ Fixtures**, picked per setup in the setup Inspector's *Workholding*, and written by the agent with `build_fixture`. Several
  fixtures can be on the table at once (`fixtures=[{"name", "at", "rot", "params"}]` or the setup Inspector's
  *Workholding*: pick, move, turn, add, remove); every one is drawn and collision-checked, and overlaps are warned about. The
  Makera Z1 low-profile vise is modelled from Makera's dimensions (226 × 103 mm, jaws along X to 130 mm, thin stock
  on 20 mm ledges) and bolts to the bed with the spoilboard removed. The Z1's spindle head comes from Makera's own
  simplified CAD, and a machine's nose stack can describe an off-centre head as boxes about the spindle axis, so the
  collision check sees it where it really is.
- Not yet: drilling cycles (GRBL has none), thread milling, side-setup simulation.

## Machine control (Makera Z1 over Wi-Fi) — experimental

The **Machine** workspace (Design · CAM · Machine in the top bar) connects to a networked machine: live status and
positions, the machine's camera, Hold / STOP, homing, jogging, go-to, the probe laser, dust collector and light, tool
changes and the auto Z probe. A status strip (state, position, tool, Control, Hold / Pause / Stop program, STOP) sits
over the camera with the jog pad on it, and the Inspector takes you through four steps: **Prepare** (connect, home,
tool change, accessories), **Zero** (what the setup wants next to what set the machine's origin, the 3D probe,
*Find the block*), **Run** and **Manual**.
First driver: the Makera Z1 (TCP 2222 framed protocol, UDP 3333 discovery, camera on `ws://<ip>:82/ws_video`;
[machine_link.py](machine_link.py) documents what was read from the firmware and checked on a real machine). Other
machines later.

**The machine's own routines do the real work; the app wraps them with checks** (before: homed, Idle, Control on,
position and limits; after: the firmware's `[PRB:]` results, the reported tool and length, no alarm):
- **Change tool** = Makera's `M6 Tn`: Z up, park, wait (fit the tool, press the front button or Confirm = `M490.2`),
  then the machine measures it on its tool setter and stores the length. The same tool number again re-measures with
  `M491` (M6 to the same number does nothing). **Every tool change is measured, the probe (T0) included, and the app
  refuses jogs and go-to moves until the fitted tool has been measured in this session.**
- **Origin = Anchor 1** = `G10 L2 P0 X Y` at the L-bracket's inside corner, as MakeraStudio does (Z untouched).
- **Auto Z probe** = Makera's `M495 X Y O0 F0` at the head's position: probe on, touch (500 then 100 mm/min), work Z
  zero on that surface, probe off. Makera's routines switch the wired probe on and check it fired; probing without
  them is how a dead probe gets driven into the stock (it happened once in testing; the e-stop caught it).
- **3D probe** (experimental) = Makera's `M480.n X Y Z D` from where the head is: outside corners (.1–.4: back left,
  back right, front right, front left), inside corners (.5–.8), pocket / bore centre (.9) and boss centre (.10). The
  firmware moves, probes and sets the work origin; the app requires the 3D probe (T9999) fitted and measured, checks
  the routine's travel against the limits, and sends a feed hold if the head leaves that area or Z drops more than
  10 mm before the first touch. **Lower to N mm above Z0** brings the probe to its start height (down only).
- **Machine-specific values come from the machine's live config** (MakeraStudio's downloaded `ControllerConfig.txt`):
  this Z1's Anchor 1 was -191.28, -194.45 against the firmware default -192.4, -194.3.

Safety rules in `machine_link`:
- **Connecting is monitor-only.** Status, camera, Hold and STOP; Hold and STOP always work. **Control** must be on
  for anything that moves the machine; it turns itself off on disconnect, alarm, lost status, STOP or 5 minutes unused.
- **Home first** (Z goes up first), Idle, one move at a time; jogs of at most 10 mm (Z down 1 mm, slower); go-to
  moves XY only with Z at the top; every target inside the travel box and a Z floor in work coordinates.
- **Never sent:** bare `M490` (on the Z1 it drives the Z motor's pins through a tool-changer routine the machine
  doesn't have) and `M498.2` (erases the stored tool lengths and G54). No free-form G-code except one line at a time
  from Machine ▸ Manual's console (each line confirmed; the commands above, machine settings, tool changes and probing
  refused), and no spindle control buttons.
- **Running operations** (experimental; tried on a real Z1 with air cuts and first jobs — always air-cut first, run one tool at a time and stay at the machine): Machine ▸
  **Run** lists the CAM program's setups and operations under a readiness checklist (CAM's *Send to machine →* opens it
  with the setup's operations ticked); tick some (one setup at a time) and *Run…*. The
  app builds those operations' G-code, removes comments, refuses lines over 62 characters (the Z1 plays lines from
  64-byte slots), relative moves, probing and offset changes, checks the program's X/Y reach against the travel from
  the machine's current work origin, then uploads it to the machine's SD the way MakeraStudio does (a pull protocol
  in 8 KB packets with an MD5) and starts it with `play`. Progress comes from the machine's status (`|P:` line,
  percent, time) and is shown per operation, on the phone too; tool changes inside the program stop for you and are
  measured by the machine. *Air cut first* runs it N mm higher first. Pause (`suspend`), Resume and *Stop program*
  (`abort`, spindle off) stay available, as do Hold and STOP.
- Alarms show their reason and the way out (Unlock, release the e-stop, reset, or power-cycle); positions are shown
  as unknown while held or in alarm (the Z1 then reports the planned position, not where the head stopped).
- The agent reads the machine (`machine_status`: state, positions, a camera frame) and asks before it acts on it: see
  *assistant approval cards* below.

**Phone control** (Settings ▸ Phone control, off by default): a phone page for the Machine workspace — live camera, STOP,
Hold / Resume / Unlock, Control, jog pad, tool change with a pinned Confirm bar while the machine waits, origin and
auto Z probe, laser / dust / light. Turning it on starts a second listener on the local network (the desktop app stays
on 127.0.0.1) that serves only that page and the machine endpoints, each call checked against a random key carried in
the QR code's link; *New link* locks out old phones. The key can be turned off (anyone on the network can then use the
page — your choice, and the pane says so). The phone uses the same MachineControl, so every rule above
applies unchanged. Plain HTTP on the local network: turn it off when you don't need it.

`tools/z1_emu.py` is a Z1 emulator (protocol, homing, jogs, hold/halt, the M6 / M491 / M495 / M480 routines with a tool
setter, a stock block and an optional pocket, the camera) used by `tests/test_machine_link.py`; like the real firmware it does not stop
a jog past the end, so the tests prove the guard does.

## New in 1.0 (CAM and machine) — experimental

- **Add operation** (CAM tree ▸ *+ Add operation*, no chat or code): face, adaptive clearing, pocket (floors picked in
  the viewer), contour, drill, 3D finish, **chamfer edges** and **V-carve**, each written as one `program.add(...)` line
  in cam.py. *Chamfer edges* puts a chamfer of a given width on the top edges of picked faces or the part's outline,
  with a V-bit or chamfer tool, the tip size included in the offset; bigger chamfers take several laps. *V-carve* cuts
  letters or a logo along their centrelines, as deep as each stroke is wide, so the top edge follows the outline and
  corners come out sharp; *Max depth* gives wide strokes a flat bottom; taken in levels, linked at depth.
- **Settings ▸ Tools** is a list and an editor that describes each tool the way its packet does (V 30° · tip Ø0.1 ·
  Ø3.175, ball R1, Ø2 · 6 mm flutes). A V-bit has its included angle, **Tip Ø**, body Ø and cutting height; a ball
  nose its **Ball radius R**; a bull nose its corner radius. Type the packet's label (`0.1mm x 3.175mm 30deg`) and
  **Fill** sets them; **Sharp point** marks a true zero tip. A V tool without a tip Ø warns instead of being treated as
  sharp.
- **Tool photos** (Settings ▸ Tools ▸ *Add photo…*, or drop / paste): crop each tool (or its packet) out of a photo;
  the picture shows wherever you pick a tool up: the tool-change card, Prepare's *Change to*, the CAM tools list and
  the phone.
- **Find the block** (Machine ▸ Zero, Z1 3D probe): the front-left corner of a block in the L-bracket can't be probed,
  so probe the other three outside corners, then *Set X0 Y0* puts X0 Y0 at the front-left corner at the measured
  faces, with the block size checked against the setup.
- **Manual** (Machine ▸ Manual): go to (Z goes up first), set the work origin at the head or by typing, any 3D-probe
  routine, a one-line console (each line confirmed; blocked commands refused) and *Run a file* (any .nc) with the same
  checks as a program.
- **Assistant approval cards**: the assistant can work the machine (`machine_control`). Hold, pause, stop program and
  STOP run at once; everything else is checked, then posted as a card in the chat and on the Machine page and runs
  only when you click **Approve** (typed text never approves). It can also edit operations and workholding and add
  operations (`cam_edit`).
- **Spindle fan** on the Z1 (Prepare ▸ Accessories, the phone, the assistant), on/off with a speed; accessory states
  are read back from the machine.

## 3D printing (external slicer)

AgenticCAD does not bundle a slicer. If **OrcaSlicer** (or Bambu Studio, same CLI) is installed on the computer
the toolbar's Output menu gets **Slice** (`P`), ⚙ Settings gets a **3D printing** section and the agent gets two extra tools; otherwise none of them exist.

- **Detection**: `/Applications/OrcaSlicer.app` (macOS), `Program Files\OrcaSlicer` (Windows), `orca-slicer` on PATH
  (Linux); override with `AGENTICCAD_SLICER=/path/to/exe[::/path/to/profiles]`. Printer, quality and filament
  lists come from the slicer's own profiles (system vendors + your user presets); the printer selected in the
  slicer is preselected.
- **Options** (Settings ▸ 3D printing, set once): printer, quality (process) profile, filament, layer height, infill %,
  wall loops, supports, brim. Anything else stays as the chosen profile says. Slice then takes one click; the agent uses
  the same defaults and can override them per request: *"slice this for my Ender 3 at 0.28 mm with supports"*.
- **How**: the design is exported as STL (0.02 mm), the profiles' `inherits` chains are flattened (the CLI needs
  flat JSON), your overrides applied, and the slicer is run headless (`--slice 0 --arrange 1 --export-3mf`).
  Outputs land in `workspace/slicing/` (G-code + 3MF, downloadable from the section).
- **Preview**: the G-code is parsed into extrusion segments per layer, mapped back from bed to model coordinates
  using the 3MF placement, coloured by feature (walls, infill, top/bottom, support, skirt...) and drawn in the
  viewer with a floating panel: layer slider, per-feature legend, stats (layers, height, estimated time, filament
  weight/length, slicer warnings) and G-code / 3MF downloads; the model hides while previewing (toggle).
- Not a slicer UI: for per-object settings, modifiers, painting or multi-plate work, open the 3MF in the slicer.

## Settings (the gear at the right of the top bar)

Model (default Claude Opus 5.5; any Claude model id, or the Claude Code default), effort (low…max), max steps per turn, thinking
summary on/off, web tools on/off, extra standing instructions, and **MCP servers** (stdio / http / sse
configs as JSON, enable toggles, live connection status from the session). Saved to
`workspace/settings.json`. Tabs: General, MCP servers, Extensions, Drawings & printing, Machines, Fixtures, Tools (the CAM libraries), Phone control. "Save & restart agent" starts a fresh session with the new options (the agent
is told its earlier chat history is gone). API: `GET/POST /api/settings`, `GET /api/agent/status`,
`POST /api/agent/restart`.

## Sketches

Fusion-style 2D sketches on a face or base plane, drawn in the viewer: select a planar face (or none for
XY/XZ/YZ + offset) → **✎ Sketch**. The camera snaps square to the plane. Tools (keys in brackets):

- **Line (L) / Arc (A)**: one closed profile of lines and three-point arcs. Click points; switch between line
  and arc at any time (an arc takes its end point, then a point it passes through); click the first point, or
  press Enter, to close.
- **Rectangle (R)**, **Circle (C)**, **Slot (S)** by two clicks; **Select (V)** shows handles to drag corners,
  centres, radii and profile points, and Delete removes the selected item.
- **Snapping**: sketch points, the model's corners, circle centres and edge midpoints, edges lying in the sketch
  plane (drawn dashed), the origin, horizontal/vertical alignment with existing points (dashed guides), then the
  grid. A marker and label show what it snapped to; hold Alt to place freely, or untick "model".
- **Typed dimensions**: while a shape is in progress just type a number: length and angle for lines and slots,
  width and height for rectangles, diameter for circles (Tab to switch, Enter to place).
- The item list edits numbers directly (profiles list their points), toggles add/subtract per item, and removes
  items.

- **Constraints**: pick points, lines and circles with Select (shift-click to add more), then use the Constrain row:
  coincident, point on edge or circle, horizontal, vertical, parallel, perpendicular, tangent, equal, concentric,
  midpoint and fix. **Dim (D)** dimensions what's picked: a line's length, a circle's diameter, an arc's radius,
  the distance between two points or from a point to a line, or the angle between two lines. Type the value, and
  double-click a dimension to change it later.
- Drawing adds the obvious constraints for you: horizontal and vertical segments, and points snapped onto existing
  sketch points.
- The solver keeps everything satisfied while you drag or edit numbers. The status line shows the remaining
  degrees of freedom, and fully constrained items turn white. A constraint that conflicts with the others, or
  is already implied by them, is refused with a message. The constraint list removes them (× or Delete), and
  deleting an item removes its constraints.

Finish writes the sketch into the script as a block

```python
# sketch:sketch1 {"plane": {...}, "items": [...], "constraints": [...]}
with BuildSketch(Plane(origin=..., x_dir=..., z_dir=...)) as _sketch1:
    with Locations((5, 0)):
        Rectangle(20, 10)
    ...
sketch1 = _sketch1.sketch
# /sketch:sketch1
```

so it is a normal build123d `Sketch` variable the agent can `extrude` / cut / `revolve`, and the JSON
header lets the editor reopen it (Browser ▸ Sketches ▸ double-click or ⋯). Sketches render as cyan
outlines and can be selected as chat chips. The **agent edits sketches through the `sketch` tool**
(list/get/set/delete, same item and constraint format), so anything it draws stays editable by you and vice
versa. Constraints are solved on save, and a conflicting set is refused.

## Extensions

The agent can extend the app itself. An extension is one Python file in `<workspace>/extensions/` (the agent writes it
with `extension save`, which refuses a file that doesn't load or registers nothing); build123d, the script helpers
and `kit` are pre-imported, plus four decorators:

```python
@helper                                   # callable from any design script
def wall_thickness(shape, point): ...

@tool("hole_report", "Every hole with diameter, depth and thread")   # agent-callable; ext_hole_report after a restart
def hole_report(ctx, body: str = None): ...                          # (ctx, **args); the JSON schema comes from the signature

@panel("Bolt pattern", icon="✣", description="Holes on a pitch circle")
def bolt_pattern(ctx):                    # re-rendered whenever one of its controls changes
    s = ctx.state
    return ui.panel(ui.picker("face", "Face", what="face"),
                    ui.number("pcd", "PCD", s.get("pcd", 40), min=1, unit="mm"),
                    ui.button("Apply", call="apply", primary=True))

@on_build                                 # after every successful build; return warnings
def check(ctx): ...
```

Panels are declared, not coded: a JSON schema of sliders, numbers, selects, checkboxes, face/body pickers (click →
next click in the viewer fills it), tables whose rows select the face they describe, key/values, badges, images and
buttons. The browser renders them from the **Extensions** menu in the Design toolbar with the app's own styling, so they keep working
across releases. Everything a panel or tool does to the model goes back through a result dict (`set_params`, `wrap`
a body's expression, `append` / `edits`, `code`, `select`, `highlight`, `chat` to ask the agent, `ui` to show more),
so extension code stays plain synchronous Python with read access to the model (`ctx.model`, `ctx.bodies`,
`ctx.faces`, `ctx.params()`, `ctx.holes()`, `ctx.selection`). `show_panel` shows a one-off table or report without
saving anything. Files hot-reload; Settings ▸ **Extensions** lists them (enable, disable, view code, delete) and a
broken file shows as FAILED TO LOAD instead of breaking a design. The full API reference is `extension api`.

Built-in examples, also the reference the agent reads: **Parameters** (every numeric parameter as a slider with live
rebuild), **Hole report** (Ø, depth, thread and position of every round hole; click a row to select it) and **Bolt
pattern** (pick a face, set PCD / count / Ø / angle, Apply writes a `pattern_circular` cut into the script).

### Parametric configurations

Named variants of a design are parameter overrides declared in the script:

```python
plate_l, plate_w, wall = 60, 40, 8
configurations = {"Small": {"plate_l": 40, "plate_w": 30}, "Large": {"plate_l": 120, "wall": 10}}
```

The Parameters card shows a selector (Default plus every variant); choosing one rebuilds the viewer with those values
while the script's literals stay as they are, and editing a number while a variant is active edits that variant.
**Manage…** opens a table to add, edit and delete variants and to **Export all**: every variant and the Default, built
with real threads, as `exports/<design>-<name>.step` / `.stl`. The agent has the same through its `configurations`
tool (list / set / delete / activate / export), and can write the dict directly. The active variant is remembered per
workspace.

### Modelling toolbar (Fusion-style, no agent turn)
One row above the viewer in the Design workspace: **Create** Box `B` / Cylinder `C` / Sphere `O` / Sketch `K` /
Revolve `R` / Loft `G` / Sweep `W`, **Modify** Press/Pull `Q` / Hole `H` / Fillet `F` / Chamfer `X` / Shell `L` /
Move `V` / Mirror `M` / Pattern `N`, **Assemble** Joint `T`, **Inspect** Measure `I` / Section `S` / Interference `J`;
the less-used tools sit under each group's *More*, and **Output** (Render `E`, Drawings `D`, Slice `P`) and
**Extensions** are menus. Hover for a tooltip with the shortcut and what to click. A tool opens its command in the
Inspector (right column), which walks you through it: step indicator, a live prompt ("Click a face…"), chips for what you picked (× to drop),
fields with units (↑↓ ±1, ⇧ ±10, ⌥ ±0.1), ↵ OK, ⌫ removes the last pick, Esc cancels. Every operation is written as code on the body's
expression in `result` — e.g. `_bracket = bp.part` hoisted before `result`, then
`"Bracket": _bracket + extrude(_bracket.faces().sort_by_distance((9, 0, 24))[0], amount=5, dir=(0, 0, 1))`
— so the design stays a script, Undo works, and the agent sees a `[Note]` of what you did.
Primitives are placed with their base on the clicked face along its normal; holes can be plain,
counterbored, or tapped (M2…M12); fillet/chamfer take edges or whole faces; shell opens the picked faces.

- **Revolve** a sketch about its own X/Y axis, a world axis or a straight edge you click; **Loft** through two to
  four sketches in order (optionally ruled); **Sweep** a sketch, or a round pipe (with an optional wall), along
  connected model edges or around the outline of a face you click. Each makes a new body or joins/cuts an existing
  one.
- **Mirror** a body about a base plane (with an offset) or a flat face you click: join for one symmetric body, or a
  separate mirrored body. **Pattern** a body rectangularly (one or two directions, or along a clicked edge) or
  circularly (about a world axis or a clicked circular edge): join the copies, make a new body of them, or cut them
  from another body, which is how you make hole patterns (a cylinder body patterned and cut from a plate). The
  script gets readable calls: `pattern_circular(peg, count=6, angle=360, axis=...)`, `mirror_about(...)`,
  `revolve(...)`, `loft([...])`, `pipe(path_wire([...]), diameter=6)`, usable by the agent too.
- **Section** cuts the model with a plane (XY / XZ / YZ with an offset slider, or parallel to a flat face you
  click). The half facing you is removed, and cut faces are drawn solid and hatched in each body's colour. The
  section stays on (a chip in the bottom bar) until you remove it. The agent's `screenshot` takes a `section` too.
- **Joints and motion** (Assemble ▸ Joint `T`, or in the script): `revolute(name, bodies, axis, parent=, limits=)`,
  `slider(...)`, `couple(follower, leader, ratio)` for gear trains and rack and pinion, and `drive(joint, start, stop,
  seconds)`. A joint with `parent=` rides on its parent, like planets on a carrier. The Joint tool takes a circular
  edge (revolute) or a straight edge (slider) and writes the line for you. **▶ Animate** in the bottom bar plays
  the drive, gives every free joint a slider, and **Check collisions** runs the drive through its range looking for
  parts that hit each other because of the motion. Overlaps that already exist at rest, such as thread engagement,
  don't count. The agent has the same check as `check_motion`.
- **Exploded views**: the Animate panel's Explode slider spreads sub-assemblies apart and then their parts, along
  Z, X or Y or radially, with dashed trail lines. `explode({"Lid": (0, 0, 40)}, axis="Z")` in the script sets
  explicit offsets and the default mode.
- **Studio render**: physically based materials under a studio environment, with soft shadows. Appearances come
  from `appearance({"*Screw*": "black oxide", "Housing": "black anodised", ...})` in the script, about 30 named
  materials or `{color, metalness, roughness}`, with sensible defaults guessed from body names (screws, coils,
  magnets, housings, PCBs…). **Save image** writes a high-resolution PNG; **Record video** captures a spin,
  explode-and-return and/or the motion as WebM (MP4 where the browser only records that). Both are saved in
  exports/renders. The agent's `screenshot` takes `pose`, `explode` and `render`.
- **Interference** finds every pair of bodies whose solids overlap (exact OCCT booleans after a bounding-box
  filter), with the overlap volume. Click a pair to see the overlap in red with the rest ghosted. Touching
  faces don't count. A bolt in a tapped hole is listed separately as thread engagement, and designs with real
  helical threads are checked on a draft-thread rebuild, which is far faster and more robust. The 113-body
  planetary gearhead takes about 20 s. Agent tool: `check_interference`, which the agent is told to run on
  assemblies.

### Other manual modelling
- Browser ▸ **+ Body**: box / cylinder / sphere with dimensions and centre.
- Sketches ▸ ⋯ ▸ **Extrude…**: distance, direction, and new body / join to body / cut from body.
- Body ▸ ⋯ ▸ **Move / rotate…**: wraps the body expression in `Pos(...) * Rot(...) * (...)`.
All of these rewrite the script (`script_edit.add_body` / `wrap_body_expr`), so the design stays one
script and the agent is told what you did.

## Units ([units.py](units.py))

Scripts, files and the kernel are always millimetres. **Settings ▸ Units** picks how numbers are shown and how the
agent reads a bare number from you: *automatic* follows the computer's timezone (US zones → inches, everything else
mm), or force mm / in. `inch` (= 25.4), `IN`, `ft`, `thou` and `mm` are pre-imported, so a script mixes freely:
`plate_l = 2.5 * inch` next to `plate_t = 6`. The Parameters card shows each parameter in its own unit (inch
parameters are edited in inches and written back as `x * inch`), the viewer stats and measure readouts use the
display units, and shop drawings are dimensioned in them (`make_drawings(units="in")` overrides per sheet).

## Threads ([threads.py](threads.py)) — ISO metric and Unified inch

Sizes are ISO (`M4`, `M6`) or Unified (`1/4-20`, `#10-32`, `3/8-16`; a bare `3/8` means the coarse series, decimal
`.25-20` works too). `thread(size)` / `iso(size)` return major, pitch, tpi, tap drill and clearance holes in mm (UN
clearance = ASME close/normal/loose); `tap`, `tapped_hole`, `bolt`, `nut`, `washer` take either kind, with inch hex/socket/
nut/washer tables from ASME B18. Drawings call out `1/4-20 UNC THRU` alongside `M4×0.7 ↧8`.

ISO metric tables (pitch, tap drill, clearance fine/medium/coarse, hex/socket/nut/washer sizes) and
script helpers: `iso("M4")`, `tap_drill`, `clearance_dia`, `tapped_hole(size, depth, at=..)` (cosmetic
cutter), `tap(part, size, at=.., depth=.. | through=True, real=False)` (cuts, and with `real=True` models
the helix via bd_warehouse), `bolt(size, length, head=hex|socket|none, real=False)`, `nut`, `washer`.
Tapped holes are registered per build: the model summary lists them and shop drawings call them out
("M4×0.7 THRU" instead of "Ø3.3").
Real threads are trimmed to the part's material and joined with a checked union, so a modelled tapped hole never
silently empties the part. While the agent works, `real=True` threads build plain ("draft threads", about 5× faster
rebuilds on a thread-heavy assembly) and the app models the real helices once at the end of the turn; the
`draft_threads` setting turns that off.

## Gears ([gears.py](gears.py))

Pre-imported in scripts: `spur_gear(module, teeth, thickness, bore=0, pressure_angle=20, hub_d=0, hub_h=0,
keyway=(w, depth))` → involute spur gear solid (Z up, tooth 0 on +X); `involute_gear_profile(module, teeth)` → closed
Face; `gear_dims(module, teeth)` (pitch, outside, root, base diameters); `gear_centre_distance(module, za, zb)`.
The outline is one closed Polyline, so it never hits build123d's "Edges are disconnected" (the classic failure of
hand-rolled involutes, which the build error now hints about). The agent is told to build complex parts one body per
`build_model` call and to use these helpers rather than derive tooth flanks itself. `planetary_layout(module, sun, planet, n)` gives the
ring tooth count, ratio, planet positions and the mesh phasing (checked interference-free for even and odd tooth
counts); sketch a ring gear as an outline minus `involute_gear_profile(m, z_ring, addendum=1.25, clearance=0)`, or use
`kit.ring_gear` / `kit.planetary_stage`. All gear and thread helpers are safe inside `BuildPart`/`BuildSketch` blocks.

## Design Kit ([designkit/](designkit/README.md))

Scripts have a pre-imported `kit` of ready-made standard components, and the agent a `kit` tool to search and read
them alongside design guides. 63 components (ISO fasteners with optional real threads, ball bearings by designation
with balls and cages, circlips, keys and couplings, gears and planetary stages, GT2 pulleys and lead screws, NEMA
steppers with full internals, servos, boards with exact mounting holes, fans, T-slot extrusions, printable enclosures,
real helical springs, hinges, knobs, O-rings with groove cutters, fittings) and 27 guides (gears, bearings, fits,
materials, threads, motors, boards, frames, enclosures, springs, linkages, O-rings, FDM/resin printing, CNC, sheet
metal, laser cutting, moulding, build123d pitfalls). Each workspace has its own kit layer (`<workspace>/kit/`) where
the agent adds notes, guides and components; its `TOC.md` is generated and lists what that workspace uses most.

```python
motor = kit.nema_stepper(17, 40, detail="full")               # dict of parts, front face at z = 0
stage = kit.planetary_stage(0.6, 18, 18, 3, face_width=8)     # sun, phased planets, ring
result = {"Motor": motor, "Stage": kit.place(stage, Pos(0, 0, 13)),
          "Bearing": kit.place(kit.ball_bearing("688ZZ"), Pos(0, 0, 29))}
```

## Versioning

`version.py` holds the version (shown next to the AgenticCAD name at the left of the top bar; click it for the changelog). Every
feature or fix gets a line in `CHANGELOG.md` under the next version, and a release is a bump in
`version.py` plus a git tag:

```bash
# after editing version.py and CHANGELOG.md
git commit -am "Release 0.10.0" && git tag v0.10.0
```

Minor bump for features, patch bump for fixes, major bump when scripts or workspaces need changing. `/api/version` serves both to the UI.

## Tests and evals

```bash
.venv/bin/python -m pytest -q            # 123 unit/API tests, ~20 s, no Claude calls
.venv/bin/python evals/run.py            # agent evals, 34 cases (the 3 showcase assemblies are long: run them with --filter showcase); needs a Claude login or API key
.venv/bin/python evals/run.py --filter cam --model claude-sonnet-5 --repeat 3
```

- **A/B evals**: `AGENTICCAD_LEGACY_BUILD=1 .venv/bin/python evals/run.py --ids cad_long_script_edit --repeat 3 --label legacy`
  vs the same without the variable measures whole-script rebuilds against `edit_model` (cost and wall time per case are
  in the result JSON/MD under `evals/results/`).
- **Windows install test** (`.github/workflows/smoke-windows.yml`, after every release build or by hand): installs the
  released `.msi` on a clean Windows runner, installs Claude Code with Anthropic's installer, starts the installed app
  and checks it through its API (`tools/ci/smoke.py`: build with inch threads and a gear, STEP/STL, inch drawings), then
  gives it the `ANTHROPIC_API_KEY` repo secret through Settings and has the real agent build a part
  (`tools/ci/agent_smoke.py`, a few cents per run). Forks never get the secret and skip the agent step.
- **Unit tests** (`tests/`): kernel (bodies, ids, exact normals, exports, measurement), script editing
  (params, rename/delete/add), CAM (holes, sections, every op, arc post, machine checks, feeds, and an
  independent fine-grid engagement audit of the adaptive op), drawings, library, and the FastAPI/WebSocket
  API with the Claude session disabled (`AGENTICCAD_NO_AGENT=1`, `AGENTICCAD_WORKSPACE=<tmp>`).
- **Evals** (`evals/`): the real agent runs headless in a throwaway workspace (the screenshot tool is an
  offscreen matplotlib render), one fresh session per case, graded by deterministic geometry / program /
  answer checks (`harness.py` helpers: bbox, bodies, holes, volume, params, script contents, op params,
  G-code validity, tools used, answer regex). Each run writes `evals/results/<stamp>.json|.md` with pass
  rate, cost, steps and time per case. Exit code 1 on any failure (`--allow-fail` to override).
  Add a case by appending a `Case(...)` to `evals/cases.py`.

## Status, gaps and roadmap

**Status (v1.0):** working end to end on a single machine for a single user — design by chat, by
hand, or both; multi-body designs and assemblies; constrained sketches; threads; measurement; drawings; part library;
extensions; CAM with GRBL, Makera, Haas and LinuxCNC posts; Makera Z1 control; unit tests and graded agent evals. The
modelling kernel is exact and the toolpaths are checked and simulated, but **CAM and machine control are
experimental**: they have been run on a real machine only by the author so far.

### Known gaps
- **One project, one session.** A single global design and one agent session per server; two browsers
  or two people will interfere. Chat history is not persisted across server restarts.
- **CAM simulation checks run on a height map.** They find gouges, leftover material, rapids through stock and holder /
  nose / head contacts with the stock and bed (the viewer's voxel stock is for watching, not for the checks), but the
  machine models' castings and enclosures are representative
  (the work-area numbers are verified; see Settings ▸ Machines ▸ Model). Side setups and mixed 4th-axis + flat
  programs are simulated separately. Time estimates ignore acceleration.
- **Adaptive corners rely on feed reduction**, not on geometry: the engagement still rises at sharp concave
  corners and around small islands, where the feed drops instead. No trochoidal slotting op, no rest for 3D, no Z-level (waterline) finishing.
- **Sketches** have constraints and dimensions but no sketch fillet, trim or offset.
- **Manual tools are typed, not dragged** (except Press/Pull): no drag handles for primitives,
  no mates/joints for positioning library parts. Patterns copy whole bodies (no feature or face patterns).
- **Drawings** have overall dimensions and hole tables only; no section views, no feature
  dimensions, no PDF.
- **Library parts land at the origin** and are moved afterwards; there is no standard-parts seed
  (fasteners, bearings, inserts) yet.
- **Face and edge ids change on every rebuild**; references in the script use clicked points, which
  survive edits but can pick a neighbour if geometry moves under them.
- Display: cylinder/sphere seam edges are drawn; screenshots for the agent are JPEG (size-capped).

### Roadmap (rough order)
1. **Projects and sessions** — a project per folder, one agent session per project, persisted chat via
   SDK session resume. Prerequisite for deploying as a shared service.
2. **CAM** — side-setup simulation, realistic time estimates, fixture/vice models in the machine view. Then Z-level
   finishing, trochoidal slotting, thread milling, ramp entries.
3. **Assembly positioning** — mate-style placement (face-to-face, concentric) for library parts, and a
   seeded standard-parts library (ISO fasteners, nuts, washers, bearings, heat-set inserts).
4. **Sketch constraints** — driven dimensions and geometric constraints kept by a solver; sketch fillet and trim.
5. **Drag handles everywhere** — primitives, holes, fillet radius, sketch items.
6. **G-code sender** — WebSerial to GRBL: jog, DRO, probing, streaming with a progress marker.
7. **Drawings** — section views, feature dimensions, PDF; **imports** — DXF/SVG profiles.
8. **Deployment** — Docker image, auth, multi-user, GitHub Pages site.
9. **Other posts** (LinuxCNC, Marlin) and 3D-printing exports (3MF, orientation analysis).

Contributions and issues welcome at https://github.com/agenticcad/agenticcad.

## Licence

AgenticCAD is **free for non-commercial use** under the
[PolyForm Noncommercial License 1.0.0](LICENSE): personal projects, research, education, hobby making,
and use by charities, schools and public institutions. See the LICENSE file for the exact terms.

**Commercial use** (using it in a business, in products or services, or as part of paid work) needs a
commercial licence: **A$99 per user per year**, one licence per person who uses it, same software.
Buy online for the number of users you need; the receipt is your licence record and there is nothing to
enter in the app. Volume or site licences: **agenticcad@prodevelop.com.au**. Details: https://agenticcad.github.io/agenticcad/#licence