# Changelog

All notable changes to AgenticCAD. Versions follow [SemVer](https://semver.org): pre-1.0, minor bumps add
features, patch bumps fix things. The version shown in the UI header comes from `version.py`.

## [Unreleased]

## [0.25.0] — 2026-10-10

### Added
- **Parametric configurations.** A top-level `configurations = {"Small": {"plate_l": 40}, "Large": {"plate_l": 120}}` dict
  in the script declares named variants as overrides of the numeric parameters. The Parameters card gets a selector
  (Default and every variant; switching rebuilds, the script's literals stay) and a **Manage…** table to add, edit and
  delete variants and **Export all**: every variant and the Default built with real threads and written as
  `exports/<design>-<name>.step` / `.stl`. Editing a parameter while a variant is active edits that variant. The agent's
  `configurations` tool lists, sets, deletes, activates and exports them; the active variant is remembered per workspace.
- **Haas and LinuxCNC posts.** New `haas` dialect (Fanuc-style: `%` and O-number, `( )` comments, every number with a
  decimal point, `Tn M06` + `G43 Hn`, `G53 G0 Z0.` retracts, `M00` operator stops, `G93` inverse-time feed for 4th-axis
  moves, `M30`) and `linuxcnc` dialect (rs274ngc: `G64 P0.01` blending, `Tn M6 G43 Hn`, `(MSG, …)`, `G93`, `M2`).
  Built-in machines: **Haas VF-2, VF-2SS, VF-4, Mini Mill, TM-1** (standard-configuration travels, spindles, rapids and
  tool changers) and **LinuxCNC mill** (3-axis and + 4th axis, generic envelopes to edit). Machines gain the
  `atc` tool-change mode (automatic changer, `Tn M06` without a stop). CAM remains experimental.

## [0.24.0] — 2026-10-09

### Added
- **Extensions: the agent can extend the app itself.** One Python file (`extension save`) adds script helpers
  (`@helper`), agent tools (`@tool`, callable at once through `extension call` and first-class `ext_<name>` tools after a
  restart), UI panels (`@panel`) and checks that run after every build (`@on_build`). Panels are declared, not coded:
  a JSON UI schema of sliders, numbers, selects, checkboxes, face/body pickers, tables (rows select the face they
  describe), key/values, badges and buttons that call a tool, ask the agent (`chat`) or act on the view. The browser
  renders them in a new **Extensions** ribbon group and keeps them live: a control change re-renders the panel, model
  edits go back through a result dict (`set_params`, `wrap`, `append`, `edits`, `code`, `select`, `highlight`…), and
  open panels follow every rebuild. `show_panel` puts a one-off table or report in the viewer without saving anything.
  Files hot-reload; a broken one shows as FAILED TO LOAD in Settings ▸ **Extensions** (enable, disable, view code,
  delete) and never breaks a design. Built-in examples: **Parameters** (every numeric parameter as a slider with live
  rebuild), **Hole report** (every round hole with Ø, depth, thread and position; click a row to select it; ask the
  agent for matching screws) and **Bolt pattern** (pick a face, set PCD / count / Ø / angle, Apply writes a
  `pattern_circular` cut into the script).

## [0.23.0] — 2026-10-06

### Added
- **Sketch constraints and dimensions.** Coincident, point on edge or circle, horizontal, vertical, parallel,
  perpendicular, tangent, equal, concentric, midpoint and fix. Driving dimensions cover length, distance (also
  point to line), radius, diameter and angle (`D`; double-click to edit). A solver keeps them satisfied while you
  drag or edit numbers, shows the remaining degrees of freedom, turns fully constrained items white, and refuses
  conflicting or redundant constraints. Drawing adds horizontal, vertical and coincident constraints automatically.
  Constraints are saved in the sketch header, and the agent's `sketch` tool reads and writes them.

## [0.22.1] — 2026-10-06

### Added
- **Render** toggle in the ribbon (Output group, `E`): studio render on and off in one click; it stays in sync with
  the Animate panel.

## [0.22.0] — 2026-10-06

### Added
- **Joints and motion.** Declared in the script, with `revolute`, `slider`, `couple` (gear ratios, rack and pinion)
  and `drive`, and child joints that ride on a parent. There's a Joint tool in a new Assemble ribbon group. ▶ Animate
  plays the drive and has a slider for every free joint.
  **Check collisions** / the agent's `check_motion` finds parts that hit each other through the motion; overlaps at
  rest such as threads don't count.
- **Exploded views**: automatic (sub-assemblies, then parts; along Z/X/Y or radial) or explicit `explode({...})`
  offsets, with trail lines.
- **Studio render**: physically based appearances (`appearance({...})`, about 30 materials, defaults from body names),
  environment lighting and soft shadows. **Save image** (high-resolution PNG) and **Record video** (spin, explode,
  motion) save to exports/renders. The agent's `screenshot` accepts `pose`, `explode` and `render`.
- The planetary gearhead reference now turns: the sun drives the carrier at 4:1, planets ride the carrier, and
  bearing balls run at cage speed. It has a full set of appearances, and no collisions through a full output turn.

## [0.21.0] — 2026-10-05

### Added
- **Section analysis** (Inspect ▸ Section, `S`): cut the model with a base plane and offset slider, or parallel to
  a flat face. The half facing the camera is removed, and cut faces show as solid hatched caps in each body's
  colour. It stays on until removed (bottom-bar chip). The agent's `screenshot` accepts `section` to look inside
  assemblies.
- **Interference check** (Inspect ▸ Interference, `J`, and the agent's `check_interference`): exact overlap volume
  for every pair of bodies, the overlap shown in red. Thread engagement is listed separately, and designs with real
  threads are checked on a draft-thread rebuild. The agent is told to run it on assemblies.
- **Revolve, Loft, Sweep** (with a round **Pipe** profile, along edges or around a face's outline), **Mirror** and
  **Pattern** (rectangular or circular; join, new body, or cut from another body for hole patterns) in the ribbon.
  They're written into the script as readable calls (`pattern_circular`, `pattern_linear`, `mirror_about`,
  `path_wire`, `pipe`, `revolve`, `loft`, `sweep`) that the agent can use as well.

### Fixed
- **Countersinks were cut upside down.** `hole(..., countersink=D)` made an undercut (about Ø(D+0.2) at the surface,
  widening to D+1 inside, then a step to the bore) instead of a 90° cone from Ø D at the surface. Countersunk screw
  heads therefore interfered with their seats; this affected the Design Kit's countersunk screw seats and hinges
  too. The interference check found it in the planetary gearhead reference.
- Planetary gearhead reference: the motor connector pins now stand on the housing's cavity floor instead of
  0.01 mm inside its back wall. The model is now interference-free apart from thread engagement.
- New bodies created from the ribbon never take a variable name that shadows a script helper.
- The interference check falls back to the real threads when a script can't build with draft threads, and tests
  pairs without threaded fasteners first, so the time budget goes to gears, shafts and housings.

## [0.20.0] — 2026-10-05

### Added
- **Sketch lines and arcs.** A Line / Arc tool draws one closed profile of lines and three-point arcs (switch
  between them mid-profile, click the start or press Enter to close). Stored as a `profile` sketch item
  (`BuildLine` + `Line` / `ThreePointArc` + `make_face`), and available to the agent's `sketch` tool.
- **Sketch snapping** to sketch points, the model's corners, circle centres, edge midpoints and in-plane edges,
  the origin, and horizontal/vertical alignment with existing points, with a marker and label showing what it
  snapped to (Alt places freely).
- **Typed dimensions while drawing**: length / angle for lines and slots, width / height for rectangles, diameter
  for circles.
- **Select tool in sketches**: drag corners, centres, radii and profile points; Delete removes the selected item;
  per-item add/subtract toggle and editable profile points in the item list.

### Changed
- The sketch toolbar is two rows, and the sketch name and plane moved into the item panel.

## [0.19.0] — 2026-10-05

### Added
- **Per-operation tool settings.** Click an operation in the CAM tab to see the spindle speed, feed, plunge, stepdown
  and stepover it runs with. Blank fields use the tool's values; typed values override them for that operation only,
  and a calculator fills them for a material. They are stored as one `overrides({...})` line at the top of `cam.py`,
  so they are saved with the design and the agent keeps them; overrides that no longer match an operation are flagged.
- `POST /api/cam/feeds` computes feeds for a tool's values without saving it to the library.
- **Stepdown caps per machine.** Machines have a `max_stepdown` by material (Settings ▸ Machines ▸ Max stepdown, e.g.
  `aluminium=0.8, *=1.5`) that the feeds calculator respects and reports. The Makera Z1 (with and without the 4th axis)
  caps aluminium at 0.8 mm, following Makera's "under 1 mm per pass", and the Generic 3018 gets aluminium 0.3 mm and
  1 mm for other materials, as its notes already said. Existing workspaces receive these defaults once; a value you
  change, even to blank, is kept.

### Changed
- **Machines and tools moved to Settings**, which is now a larger window with tabs (General, MCP servers, Drawings &
  printing, Machines, Tools). Machines get a form (travel, feeds, spindle, collet, tool change, 4th axis, start/end
  G-code, or JSON); tools an editable table with the feeds & speeds calculator. The CAM tab keeps the program:
  setup, operations, simulation and G-code.

## [0.18.0] — 2026-10-04

### Added
- **CAM simulation.** Simulate all visible operations or a single one (the **Simulate** button in the CAM bar, or
  the agent's `simulate_cam` tool): the stock is cut in step with the slider; on the last frame gouges show red and
  material left on the part amber. The summary reports removed volume, gouge depth/area and the op that caused it,
  material left, stock left outside the part and rapids through material. Height-map model for top/bottom setups,
  radial model for the 4th axis.
- **Multiple setups.** `Setup(..., orient="bottom")` (and front/back/left/right) for flipped and re-fixtured
  work, each with its own WCS (G54…); the post pauses for the operator between setups and restarts the spindle.
  `setup.view(part)` gives the part as the machine sees it.
- **4th axis**, indexed (3+1) and continuous: rotary setups on `Stock.cylinder`, `rotary_rough`, `rotary_finish`
  (lines, rings, spiral) and `rotary_wrap` for patterns wrapped onto a cylinder; swing/length/installed checks.
- **Makera Z1 and Carvera Air** (with and without the 4th axis) as built-in machines, added to existing workspaces
  once, with a `makera` post that follows the controller firmware: ≤ 63-character lines, no line numbers,
  `M6 Tn` manual tool change with automatic tool measuring, M600 between setups, G28 park, no arcs while A moves,
  and A-axis feed rates as the firmware interprets them. Tools larger than the collet are flagged.

### Fixed
- **File ▸ New** now starts clean: the chat is cleared, the agent starts a fresh conversation, and the CAM
  program, script and simulation are removed.
- The feeds & speeds calculator defaults to the CAM program's machine.

## [0.17.0] — 2026-10-02

### Added
- **Design Kit.** Scripts get a pre-imported `kit` of ready-made standard components, and the agent a `kit` tool to
  search and read them along with design guides. First set: ISO socket/button/countersunk screws, set screws, hex
  bolts, nuts and washers (optional real threads); ball bearings by designation (608, 688ZZ, 6204-2RS, MR…) with
  rings, raceways, balls, cages and shields; circlips, E-clips, keys, collars and dowels with their groove/keyway
  cutters; spur and internal ring gears and complete planetary stages; NEMA 8–23 steppers, with full internals for
  14/17/23; hobby servos, 28BYJ-48 and N20 gear motors; Arduino Uno, Raspberry Pi 4 and Pico boards with exact
  mounting holes; fans with grille cutters; 18650 cells; T-slot extrusions, corner brackets and T-nuts; GT2 pulleys
  and a T8 lead screw; rigid and jaw couplings; heat-set inserts and standoffs; a printable screw-lid enclosure,
  bosses, vent slots and snap fits; hinges, compression and extension springs swept along their real helix, knobs,
  pull handles, disc cams and link bars; O-rings with face/piston/rod groove cutters, tube, push-fit fittings and hose
  barbs (63 components). 27 guides: gears, belts and lead screws, bearings, shafts, springs, linkages and cams, hinges,
  O-ring grooves, fasteners and plastic fastening, motors, boards, extrusion frames, enclosures, FDM and resin
  printing, CNC machining, sheet metal, laser cutting, injection moulding, fits, materials, metric threads, stock
  sizes, face selection and build123d pitfalls. Each workspace has its own kit layer the agent adds notes, guides and components to; its
  table of contents is generated and ranks what the workspace uses most. See `designkit/README.md`.
- `planetary_layout(module, sun, planet, n)`: ring teeth, ratio, planet positions and mesh phasing, verified
  interference-free for even and odd tooth counts.
- Real-thread bolts are built once per process and reused; `thread_length` gives long screws a partial thread.
- **Draft threads while the agent works.** During an agent turn, `real=True` threads build as plain cylinders (a
  100-part gearhead rebuilds in 14 s instead of 72 s); when the turn ends the app builds the design once with the real
  helices. Settings key `draft_threads` turns it off.

### Fixed
- **Real threads could silently vanish.** Fusing a modelled thread into a part could return an empty shape (M2 over
  ~5 mm, M4, M5) or leave the thread as a loose solid, and through-holes left thread sticking out of the part. Threads
  are now trimmed to the part's material, joined with a checked union (plain, then fuzzy, then locally around the
  hole), and `tap` raises a clear error instead of ever returning an empty part.
- If the final real-thread build at the end of an agent turn fails, the agent gets the error and one chance to fix it.
- The gear and thread helpers (`spur_gear`, `involute_gear_profile`, `bolt`, `tap`, `hole`, `nut`, `washer`) now work
  inside `BuildPart` / `BuildSketch` / `Locations` blocks. Before, they raised errors or silently merged into the
  surrounding part.

## [0.16.1] — 2026-09-26

### Added
- **Licence acceptance at install.** The macOS download is now a standard `.pkg` installer (a `.dmg` can't carry a
  licence step) with a welcome page and the licence and terms behind Agree / Disagree; the Windows `.msi` shows the same
  text in its licence dialog. The text is `packaging/INSTALLER-TERMS.txt` (free for non-commercial use, A$99 + tax per
  user per year for commercial use, Claude usage is separate, consumer law) followed by the PolyForm licence. CI checks
  both installers contain it.
- `tools/promo/screens.py` regenerates the site screenshots from the real app; the promo recorder now shows slicing.

### Fixed
- Slicing failed with "No such file … model.stl" when the workspace path was relative: the slicer runs inside the
  slicing folder, so paths are now made absolute first.
- The Parameters card no longer labels plain numbers "mm" (they may be angles or counts); only `x * inch` parameters carry an "in" badge.

## [0.16.0] — 2026-09-25

### Added
- **Inches alongside millimetres.** Settings ▸ Units: automatic (US timezone → inches, else mm), or mm / in. Scripts
  stay in mm; `inch`, `IN`, `ft`, `thou`, `mm` are pre-imported so dimensions mix (`plate_l = 2.5 * inch`). The
  Parameters card shows each value in its own unit, viewer stats and measure readouts use the display units, shop
  drawings are dimensioned in them, and the agent reads bare numbers in the user's units.
- **Unified inch threads** (UNC/UNF: `1/4-20`, `#10-32`, `3/8-16`…, coarse series by default) next to ISO metric in
  `thread()` / `iso()`, `tap`, `tapped_hole`, `bolt`, `nut`, `washer`, with ASME hardware tables and drawing callouts.

## [0.15.3] — 2026-09-25

### Added
- Hovering a body row in the Browser highlights that body in the viewer.

## [0.15.2] — 2026-09-25

### Fixed
- The Settings button was hidden under the ribbon in the desktop window (the ribbon sat at a fixed offset). The ribbon
  now starts after the toolbar, the button is labelled "Settings" with an icon, and File ▸ Settings… opens it too.

## [0.15.1] — 2026-09-25

### Fixed
- `edit_model` applies its edits and rebuilds under the model lock, so several edit calls issued together (parallel
  tool use) are serialised on the latest script instead of overwriting each other.
- The agent session only sees the MCP servers configured in the app (the built-in `cad` server and Settings ▸ MCP);
  connectors attached to the user's claude.ai account no longer appear in the CAD session.

## [0.15.0] — 2026-09-25

### Added
- **`edit_model` tool**: the agent changes the existing script in place (exact-text replacements that must match
  once, plus code appended before `result`) and rebuilds; a failed build leaves the previous design untouched.
  `build_model` is now for new designs and full rewrites only. Measured on a 150-line, 19-body design with a two-line
  change request (3 runs each, Claude Opus 5.5): whole-script rebuild $0.32 / 33 s per change, `edit_model`
  $0.09 / 12 s — about 3.5× cheaper and 3× faster. On 30–60-line scripts the difference is within noise.
- Eval cases `cad_long_script_edit` (the measurement above) and an A/B switch `AGENTICCAD_LEGACY_BUILD=1` that
  restores the pre-0.15 tools and prompt for comparisons; `evals/run.py --ids a,b --label x`.

### Changed
- Incremental building of multi-body parts is now stated in the `build_model` tool description as well as the
  prompt; the seven-body gearbox eval passes 3/3 (first body with `build_model`, the rest via `edit_model`).

## [0.14.0] — 2026-09-25

### Added
- **Gears**: `spur_gear(module, teeth, thickness, bore, pressure_angle, hub_d, hub_h, keyway)`, `involute_gear_profile`,
  `gear_dims`, `gear_centre_distance` are pre-imported in scripts (`gears.py`). The outline is one closed Polyline, so
  it never produces build123d's "Edges are disconnected"; the build error now hints at the helpers when it happens.
- Eval cases: spur gear, meshing gear train, seven-body gearbox built incrementally (all passing on Claude Opus 5.5).

### Changed
- The agent builds complex parts **one body at a time** (more than 3 bodies or ~80 lines → several `build_model`
  calls with a one-line progress note), instead of one long script whose failure costs the whole attempt.
- The agent session exposes **no built-in shell or file tools** (it had been able to call Bash); only WebFetch /
  WebSearch when web tools are enabled, plus the CAD tools.

## [0.13.3] — 2026-09-25

### Changed
- The ⚙ Settings button sits next to the File menu in the viewer, so it is always reachable, including with the
  side panel hidden (⌘B).

## [0.13.2] — 2026-09-25

### Fixed
- Tool-call chips in a long chat collapsed into thin lines: the chat is a flex column and the chips (which clip their
  content) were allowed to shrink once the conversation overflowed. Chat items no longer shrink; the list scrolls.

## [0.13.1] — 2026-09-25

### Changed
- **The Design tab is gone.** Parameters live in a card under the Browser (shown only when the script has numeric
  parameters); Measure opens a panel over the viewer with the readout, density and Stop; shop-drawing files are listed
  as links in chat. The side panel is Chat, Code, CAM, Library.

## [0.13.0] — 2026-09-25

### Changed
- **Drawings and slicing are one-click ribbon buttons.** A new *Output* ribbon group has **Drawings** (`D`) and, when a
  slicer is installed, **Slice** (`P`). Their options moved into ⚙ Settings: *Shop drawings* (material, sheet) and
  *3D printing* (printer, quality, filament, layer height, infill, walls, supports, brim). The agent's `make_drawings`
  and `slice_for_printing` start from the same settings when called without arguments.
- The sliced-layer preview is a floating panel over the viewer (layer slider, feature legend, stats, G-code/3MF, ✕)
  instead of a Design-tab section. The Design tab keeps Parameters, Measure and the list of drawing files.
- Generating drawings on an empty design is a clean error instead of an empty result.

## [0.12.2] — 2026-09-25

### Fixed
- Workspaces created by versions before 0.11 still opened on the demo bracket. An unsaved, unedited demo working copy
  is now upgraded to a blank design on start (an edited script or a saved design is kept).

## [0.12.1] — 2026-09-25

### Fixed
- The 0.12.0 desktop installers shipped without `slicer.py` (the Briefcase source list was not updated), so the
  packaged app failed to start. Fixed, and a test now checks that every local module the app imports is packaged.

## [0.12.0] — 2026-09-25

### Added
- **3D printing via an installed slicer** (OrcaSlicer / Bambu Studio; nothing bundled). When one is detected the
  Design tab gets a 3D printing section (printer, quality, filament, body, layer height, infill, walls, supports,
  brim → Slice) and the agent gets `slicer_info` + `slice_for_printing`. The G-code is drawn in the viewer as
  coloured layers with a layer slider and feature legend; stats (layers, time, filament, warnings) and G-code/3MF
  downloads. `AGENTICCAD_SLICER` overrides detection. New module `slicer.py`, endpoints under `/api/slicer`.

### Changed
- CAM is labelled **experimental** on the site, in the docs and README: toolpaths are correct and machine-checked
  but not optimised (long, conservative paths, many retracts, no stock simulation).

### Fixed
- Press/Pull, Box, Cylinder, Sphere and Hole only accept flat faces (the ribbon says so and ignores curved picks;
  the server refuses too). Pressing a cylindrical face used to produce an empty body that rendered as nothing.
- A build whose body has no volume now fails with a clear error instead of showing an empty viewer.

## [0.11.1] — 2026-09-24

### Added
- **Sign-in handling.** The app now checks Claude Code's login state (`claude auth status`) before connecting and
  whenever the agent reports an authentication failure, and shows a banner with the two fixes: run `claude` and
  log in, or paste an Anthropic API key (Settings ▸ API key, or the setup page). The key is stored in
  `settings.json` with owner-only permissions, passed to Claude Code as `ANTHROPIC_API_KEY`, and never returned
  to the browser. `/api/agent/cli` reports `logged_in` / `auth_method`. Previously an expired login surfaced only
  as an opaque "Failed to authenticate" chat error, because the SDK runs the CLI headless and cannot prompt.

## [0.11.0] — 2026-09-24

### Changed
- Default agent model is **Claude Opus 5.5** (`claude-opus-5-5`); the Settings list is ordered Opus 5.5, Fable 5.1, Sonnet 5, Opus 5, Opus 4.8, Haiku 4.5.
- **First run starts empty.** A fresh workspace (and File ▸ New) opens a blank design instead of the demo bracket;
  `result = {}` / `result = None` are valid empty designs, the viewer frames the grid and the Browser explains
  how to add a body. An untouched empty design is not marked unsaved.

### Fixed
- `set_parameters` with an unknown name is refused (it was silently ignored, so the agent believed it had changed something).
- Measuring an unknown face id returns a clean error instead of crashing the request.
- The `library` tool no longer saves a part when given an unknown action.
- Deleting a sketch that the script still uses is refused with a hint, instead of breaking the build.
- Malformed sketch items report the missing field; a failed manual operation never drops the WebSocket connection.
- `feeds_speeds` accepted `T1` but crashed on unknown `T99`; tool lookup now handles numbers, `Tn` and names.
- Chat without a Claude session (no Claude Code installed, or the agent disabled) reports the situation instead of
  trying to start one; DELETE of a missing machine/tool/library part returns 404.

### Added
- Tests: 123 unit/API tests (was 74) covering every manual ribbon operation, the agent's MCP tools, the WebSocket
  protocol, HTTP endpoints, the desktop launcher, empty designs, and CAM/kernel edge cases. Tool handlers are
  exposed as `CadAgent.tool_handlers` for direct testing.

## [0.10.1] — 2026-09-23
### Changed
- Contact address for licensing, security and conduct is agenticcad@prodevelop.com.au (site, LICENSE notice, templates, installer metadata).

## [0.10.0] — 2026-09-23
### Added
- **Desktop app** (Briefcase): `AgenticCAD.app` (.dmg, Apple Silicon) and Windows .msi built by the `package`
  workflow on each release. Native window (pywebview), data in the OS user-data folder, free-port selection.
  Claude Code is **not bundled**: the app finds the user's own install (PATH and the official install
  locations) and shows a setup page (`/setup`) with install and sign-in steps when it is missing.
  `agent.find_claude_cli()`, `/api/agent/cli`, `agent_missing_cli` event. Unsigned builds for now.

## [0.9.2] — 2026-09-23
### Added
- Releases: `tools/release.sh` tags the version, builds `agenticcad-<version>.zip` (git archive) and publishes a GitHub release; the site links to the latest release zip.
- GitHub Pages site on the `site` branch (landing page, documentation, simulated tutorials); `tools/gen_site_tutorials.py` bakes the tutorial data from the real kernel.
- `tools/secure_repo.sh`: post-public repository hardening (branch protection, secret scanning, Dependabot, fork-PR approval).

## [0.9.1] — 2026-09-22
### Changed
- Renamed the app to **AgenticCAD** (repository `agenticcad/agenticcad`); identifiers, env vars
  (`AGENTICCAD_*`) and paths follow. Added `requirements.txt` and install notes.
### Added
- Licence: PolyForm Noncommercial 1.0.0 (free for non-commercial use; commercial licensing on request).
- README hero screenshot, status/gaps/roadmap section; CONTRIBUTING, SECURITY, code of conduct, issue and PR templates, CI (pytest on push and PRs); `?pane=` URL parameter.

## [0.9.0] — 2026-09-22
### Added
- Modelling ribbon: Create (Box, Cylinder, Sphere, Sketch), Modify (Press/Pull, Hole, Fillet, Chamfer,
  Shell, Move), Inspect (Measure) with SVG icons, tooltips and keyboard shortcuts; command dialog with
  step indicator, live prompt, pick chips and unit fields. Every operation is written into the script.
- Press/Pull drag handle with a ghost preview of the extrusion.
- Agent `sketch` tool (list/get/set/delete) so the agent edits UI sketches in the editor's own format.
- Manual body operations without an agent turn: primitives on faces, sketch extrude (new/join/cut),
  move/rotate, plain / counterbored / tapped holes.
- Version number in the header with this changelog behind it; `/api/version`.

## [0.8.0] — 2026-09-22
### Added
- Settings (⚙): model, effort, max steps, thinking summary, web tools, extra instructions, MCP servers
  (stdio/http/sse) with live status; save & restart the agent session.
- 2D sketch editor on faces or base planes (rect, circle, polygon, slot; subtract; snap) stored as
  editable `# sketch:` blocks in the script; sketches drawn in the viewer and listed in the Browser.
- ISO metric threads: `tap`, `tapped_hole`, `bolt`, `nut`, `washer`, tables; cosmetic or real (bd_warehouse);
  thread callouts on shop drawings.
- Unit test suite (pytest, 70+ tests) and agent evals (`evals/run.py`, 13 graded cases with cost tracking).
### Fixed
- Script namespace (`import_step`, `from_library`) is bound per run instead of process globals.
- Arc fitting in the GRBL post ran after collinear merging and lost circles; now fits arcs first.

## [0.7.0] — 2026-09-05
### Added
- Design tab: parameters panel, measure tool (vertex/edge/face snapping, distances, angles, relations),
  shop drawings (third-angle views, hidden lines, dimensions, hole table, title block; SVG + DXF),
  STEP import, part library (parametric scripts and STEP parts, thumbnails, insert with parameters).
- Library as a first-class citizen: Library tab, Browser "+ Part", body "Save to library…", agent tools.
- Reference images in chat (attach, drop, paste) for modelling from sketches and photos.
- Constant-engagement adaptive clearing (front-offset), rest machining, lead-in/out, feeds & speeds
  calculator, G2/G3 arcs in the post.

## [0.6.0] — 2026-09-04
### Added
- CAM: machines and tool libraries, facing, contour with tabs, pocket, drill, 3D parallel finishing,
  GRBL post with checks, CAM tab with toolpath rendering and simulation.

## [0.5.0] — 2026-09-04
### Added
- Multi-body designs with a Fusion-style Browser tree, design files (new/open/save/import), undo history,
  ViewCube navigation, resizable side panel, selection chips that stay with the question, rename/delete
  bodies from the tree, Code tab with syntax highlighting.

## [0.1.0] — 2026-09-04
### Added
- First prototype: Claude Agent SDK session writing build123d scripts, exact B-rep kernel with display
  meshing, three.js viewer with face picking, STEP/STL export, screenshot tool for the agent.
