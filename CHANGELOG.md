# Changelog

All notable changes to AgenticCAD. Versions follow [SemVer](https://semver.org): pre-1.0, minor bumps add
features, patch bumps fix things. The version shown in the UI header comes from `version.py`.

## [Unreleased]

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
