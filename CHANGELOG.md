# Changelog

All notable changes to AgenticCAD. Versions follow [SemVer](https://semver.org): pre-1.0, minor bumps add
features, patch bumps fix things. The version shown in the UI header comes from `version.py`.

## [Unreleased]

## [0.48.0] — 2026-10-18 (local build of the v2-ui branch, not released)

### Changed
- **New layout (v2).** One top bar with the file (its name is the File menu), the workspaces **Design · CAM · Machine**,
  the machine's status, the Assistant and Settings. The right column is **Inspector · Assistant · Code** in every
  workspace: the Inspector shows what you are working on (the active command or the selection in Design, the selected
  operation or setup in CAM, the machine's steps in Machine). Design has a one-row toolbar (less-used tools under *More*,
  Output and Extensions as menus) with the Browser and Parameters docked on the left, and shows the part alone.
- **CAM workspace.** The program, its setups, operations and the tools they use are a tree on the left; the Inspector
  edits the selected operation (Tool & feeds · Cut · Display) or setup (stock, origin, workholding, machine view); the
  viewer has *Selected op / All* and *Toolpath / Stock / Machine* toggles and a timeline of op-coloured blocks with
  Simulate, play and speed. *Send to machine* opens Machine ▸ Run with the setup's operations.
- **Machine workspace.** A status strip (state, position, tool, Control, Hold / Pause / Stop program, STOP) over the
  camera with the jog pad on it, and four steps: **Prepare** (connect, home, tool change, accessories), **Zero** (what the
  setup wants next to what set the machine's origin this session, a guided 3D-probe sequence, *Find the block* for a
  block in the L-bracket), **Run** (a readiness checklist, air cut first, progress op by op) and **Manual**.

### Added
- **Add operation** in CAM: face, adaptive clearing, pocket (floors picked in the viewer), contour, drill and 3D finish,
  each written as one `program.add(...)` line in cam.py (left as it was if the program doesn't build).
- **Find the block**: probe back-right, back-left and front-right, then X0 Y0 goes to the front-left corner at the
  measured faces; the measured block size is compared with the setup's. Or move X0 Y0 from a probed corner by the
  block size. The probe's step-out is fitted to the room the travel leaves.
- **Manual**: go to (Z goes up first), set the origin at the head or by typing, any 3D-probe routine, a one-line console
  (each line confirmed; blocked commands refused) and *Run a file* for any .nc file with the same checks as a program.
- **The assistant can work the machine**: stopping (hold, pause, stop program, STOP) at once; everything else as an
  approval card in the chat and on the Machine page that runs only when you click Approve. It can also change an
  operation's settings, the workholding and add operations, as the CAM page does.

### Fixed
- An origin set before another setup's program ran is no longer shown as right for the next setup (re-zero after
  re-clamping).

## [0.47.0] — 2026-10-18

### Added
- **Tool shapes.** Tools are now described by their real shape: flat, ball, **bull nose** (corner radius), **V-bit /
  engraver** with its tip diameter, chamfer, drill and **tapered ball**, plus the **shank diameter** (a 0.3 mm endmill on
  a 1/8" shank), an optional **neck** (diameter and reach) and the **stickout**. Settings ▸ Tools has a *Shape* editor per
  tool with the fields its type uses and a to-scale drawing of the profile (cutting part, body, shank). The viewer,
  the simulation and the machine model draw each tool from that profile.
- **Toolpaths use the shape.** 3D finishing drops the whole tool (cutting shape, neck and shank) onto the surface, so
  ball, bull-nose, V and tapered-ball tools all follow it without gouging, and a wide shank never dips into it (checked
  against exact geometry for every type). The 4th-axis maps do the same. 2.5D operations with a non-flat tool plan with
  its width at the cut depth, so the top of the cut lands exactly on the boundary, and they say what the shape leaves:
  the floor's ridge height between passes, sloped walls, a cut deeper than the flutes, or a shank that will meet walls.
- **Collisions from the shape.** The simulator cuts with the real profile up to the flute length; material met above it
  is reported ("tool above its 2 mm flutes"), as are a neck or a shank wider than the cut meeting the stock ("tool
  shank"), and the whole tool (not just its tip diameter) against fixtures.

### Fixed
- 3D finishing could flatten a gently curved pass into one straight move and cut into the part: points along a raster
  line were dropped by comparing each with its immediate neighbours only. Every dropped point is now checked against
  the move that replaces it.
- The Machine tab's *Program panel* switch is a normal checkbox again (it was stretched to the address field's width).

## [0.46.0] — 2026-10-17

### Added
- **Run CAM operations on the machine** (experimental; not yet tried on a real Z1 — start with an air cut). The
  Machine tab has a *Program panel* (toggle it in the connection bar): the CAM program's setups and operations; tick
  the ones to run (one setup at a time) and press Run. A confirmation shows the operations, tools, lines, Z range,
  the machine's work origin and the checks; the app then uploads the program to the machine's SD card (Makera's
  own transfer: 8 KB packets checked by MD5) and plays it. While it runs the panel and the phone show the operation,
  line, percent and time; tool changes in the program wait for you and are measured by the machine; *Pause*,
  *Resume* and *Stop program* are there, and Hold / STOP as always. *Air cut* lifts the whole program N mm.
  Before sending: Control on, homed, Idle, the first tool measured if it is already fitted, every line within the
  Z1's 62 characters, no relative moves / probing / offset changes, and the program's X/Y reach inside the travel
  from the current work origin. The Z1 emulator plays programs too (upload, play, progress, pause / resume / abort).
- **Workholding that matches a real setup.** Bare stock can sit anywhere on the bed: its front-left corner is set in
  bed millimetres (shown as machine X/Y too), with *Centre*, *Into the L-bracket* and *From machine* (reads the connected
  machine's probed work origin and places the stock to match). New fixtures: **Makera L-bracket** (thin / thick, at its
  dowel position from Makera's bed CAD; a stop, checked for tool collisions) and **Strap clamp** (strap, cap screw and
  packing block on the stock edge, placed and turned like any fixture). Fixtures with a fixed home go there when picked.

### Changed
- **CAM tab reorganised around setups.** A program header (machine, setups, operations, time, G-code download and
  preview, warnings folded away), then one card per setup holding its stock, work origin, safe height, workholding and
  its own operations. The machine view is one row at the top; all simulation controls (Simulate, Rendered, the
  transport, the time scrubber and the playback speed) and the simulation results are in one dock at the bottom.
- **Operation settings in tabs:** *Tool & feeds* (as before), *Cut* (depths, side, tabs, stock to leave, climb, ramp,
  helix, peck, pass angle... per operation type, the script's values greyed) and *Display* (colour, shown or hidden).
  Click a colour swatch to recolour an operation. All of it is saved in cam.py's `overrides({...})` line, so the
  script stays the source; edits survive tab switches until Apply or Reset, and hidden operations stay hidden across
  rebuilds.

## [0.45.0] — 2026-10-17

### Added
- **Machine tab: 3D probe** (experimental, not yet run on a real machine), using Makera's own routines as MakeraStudio
  sends them (`M480.n X Y Z D`): outside corners, inside corners, pocket / bore centre and boss centre. The machine
  touches the top, steps out past the edges, probes back in and sets the work origin itself; the app checks before
  (the 3D probe fitted and measured, homed, Control on, the routine's whole travel inside the limits) and watches while
  it runs: a feed hold if the head leaves the routine's area or Z drops more than 10 mm without touching anything.
  The result is reported (corner position from Anchor 1, or the measured width for centres). On the phone's Probe tab
  too. The 3D probe uses tool number 9999.
- **Lower to N mm above work Z0**: a slow, down-only Z move to a probing start height.
- **Phone control without a key** (Settings ▸ Phone control ▸ Key, on by default): turned off, the phone page is a
  plain link you can bookmark, with no QR rescans; anyone on the network can then use it, and the pane says so.

## [0.44.0] — 2026-10-17

### Added
- **Phone control.** Settings ▸ Phone control turns on a page for your phone, opened by scanning a QR code: the
  Machine tab's camera, STOP, Hold / Resume / Unlock, Control, a thumb-sized jog pad, tool change (with a Confirm bar
  pinned to the bottom while the machine waits for you), origin at Anchor 1, the auto Z probe, and the laser, dust
  collector and light. It is off by default; when on, a second listener on your local network serves only that page
  and the machine endpoints, and every call must carry the random key in the QR code's link (New link locks out old
  ones). The desktop app stays private to your computer, and the phone goes through the same safety rules.

## [0.43.0] — 2026-10-17

### Added
- **Machine tab: Makera's own tool change, tool measurement and auto Z probe**, run from the app and checked on a
  real Z1. *Change tool* sends `M6 Tn`: the machine parks, waits for you to fit the tool and press its button (or
  Confirm here), then measures it on its tool setter. The same number again re-measures with `M491`. *Origin =
  Anchor 1* sets work X/Y zero at the L-bracket's inside corner (`G10 L2`, as MakeraStudio does). *Auto Z probe here*
  runs `M495` at the head's position: probe on, touch the surface, set work Z zero there, probe off. The app checks
  before each (homed, Idle, Control on, limits) and after (the touch results, the tool and its length, no alarm).
- **Every tool change is measured, the probe included**: the app refuses jogs and go-to moves until the fitted tool
  has been measured in this session.
- **Go to machine X/Y** (with Z at the top), **raise Z to the top**, the **probe laser** switch (to check positions
  from a safe height) and a **pin check** (probe, tool setter and e-stop inputs).
- The Z1's calibrated positions (Anchor 1, tool-change position) are read from the machine's own config as
  MakeraStudio downloads it, not the firmware defaults (this Z1's anchor differs by 1.1 mm).

### Fixed
- Alarms show their reason ("manual stop", "probe failed", "emergency stop", ...) and the way out: Unlock, release
  the e-stop first, reset the machine, or power-cycle it.
- Positions are shown as unknown while the machine is held or in alarm: the Z1 then reports its planned position, not
  where the head stopped.
- The app never sends bare `M490` (on the Z1 it drives the Z motor's pins through a tool-changer routine the machine
  doesn't have) or `M498.2` (erases the stored tool lengths and G54).

## [0.42.1] — 2026-10-17 (pre-release)

### Fixed
- **Machine tab: an Alarm says why and what to do.** The status's halt reason is shown on the badge ("manual stop",
  "probe failed", "hard limit", ...), and refusals in Alarm say whether to press Unlock (the reasons the firmware
  clears with $X), reset the machine (limits, motor errors, spindle stall) or switch it off and on (spindle alarm).
  It used to say "clear it on the machine (or Unlock)", which sent you to the machine's button for a stop Unlock
  clears.

## [0.42.0] — 2026-10-17 (pre-release: machine motion awaiting a supervised test)

### Added
- **Machine tab: control a Makera Z1 over Wi-Fi (experimental).** Find the machine on the network, connect, and see
  its status, positions, tool and spindle, with its camera live beside them. Hold and STOP are always available.
  Turning on **Control** unlocks Home, a jog pad, dust collector and light. Every move is checked first: homed in
  this session, Idle, one jog at a time, small steps (Z down 1 mm and slowly), inside the machine's travel and above
  a Z floor in work coordinates. The Z1's own soft limits are off, so these checks are what stop a jog running into
  the end of travel or the tool into the bed. Control turns itself off on disconnect, alarm, lost status, STOP or
  5 minutes unused. The agent can look at the machine (status and a camera frame) but can't command it.
  Status and camera have been checked on a real Z1. Motion so far only on the included emulator.

## [0.41.0] — 2026-10-17

### Added
- **Machines know what their controller takes.** `cam.CONTROLLERS` describes each post's controller, from its source
  or manual: which arc forms it accepts, whether arcs may move Z, and its line-length limit. Two new machine
  settings (Settings ▸ Machines, or the machine JSON) choose within that: **Arc format**, I/J centre offsets (the
  default, every controller) or an R radius word, and **Helical arcs** (off writes flat arcs only). R arcs are kept
  to 135° each, well short of the half circle where a radius is ambiguous.
- The Makera Z1 / Carvera firmware works out an arc's radius from I/J only and has no R form (read from
  MakeraZ1Firmware, Robot::compute_arc), so a Makera machine set to R still gets I/J, and the program check says so.
  The Z1's G-code is unchanged: it has always been I/J.

### Fixed
- **Arc centres sit exactly between the arc's ends.** GRBL refuses an I/J arc whose start and end radii differ by
  more than 0.005 mm (and 0.1 % of the radius). The fitted centre is now moved onto the chord's bisector, so the
  only difference left is the 3-decimal rounding (≤ 0.002 mm). On a 15,000-arc program the worst was 0.0021 mm
  before as well, so no existing program was rejected.
- GRBL lines are held to its 80-character buffer and LinuxCNC's to 255, like the Makera's 63 (comments count only
  where the controller keeps them).

## [0.40.0] — 2026-10-17

### Fixed
- **Adaptive links can no longer cross an island.** Cleared ground for links was "everything outside the pocket
  plus what has been cut", and an island is outside the pocket: once the ground all round it was cut, a stay-down
  link or a low hop could run straight across it. Links are now confined to where the cutter may go in the pocket.
  On an L-shaped island test, 109 tool positions reached into the island; now none.
- **Adaptive finishes the area it is in before moving on.** It used to cut one ring of fronts all over the part,
  then the next ring, hopping back and forth. When the nearest pass is more than a stay-down link away, it now first
  looks at what the passes just cut have opened up where the cutter is. On a logo plaque's four roughing bands:
  long cross-part links 7.1 → 3.5 m, lifts 298 → 202, 22.2 → 19.5 min; faster or equal on every test shape.
- **Adaptive leaves no slivers standing at the walls.** Each level ends with a climb pass along the walls wherever
  the crescents left scallops, and lifted rapids keep 0.02 mm clear of the walls.
- **Pocket and contour entries no longer crawl through air.** They rapided to 1 mm above the original stock top and
  fed down at the plunge rate, then started the ramp 0.5 mm above the material. They now rapid to 0.5 mm above
  where the op cuts and start ramps (and adaptive helices) 0.05 mm above it; later adaptive levels helix from the
  level above. The engraved text on the plaque: 16.4 → 5.8 min.
- **Ramps and helices run at a sensible feed**: at most 60 % of the cutting feed, and never descending faster than
  the plunge feed (they ran at the plunge feed, so a 3° ramp sank at 5 % of it).
- **Time estimates count slow Z.** Rapids' vertical part is timed at the machine's Z limit (600 mm/min on the Z1,
  half its XY rate), so estimates no longer flatter programs with many retracts.

## [0.39.0] — 2026-10-17

### Fixed
- **Adaptive roughing moves like a machinist's toolpath.** On a real job (a 1/8" cutter roughing a logo plaque in four
  depth bands) the old linker spent over half its feed travel re-cutting cleared ground, cut 330 mm conventional,
  flipped direction between neighbouring passes and retracted to the safe height 31–101 times per band. Now:
  - every pass is climb, decided by a vote along its whole length (not one probe at its start), and is trimmed to
    where the cutter actually touches material, so it no longer runs along walls and floors that are already cut;
  - each pass is entered on a tangential quarter-arc lead-in from the cleared side, so the engagement builds from
    nothing instead of the cutter being dropped or swung straight into the cut;
  - links go through cleared ground to the nearest next pass: short ones stay down at feed, longer ones hop 0.25 mm
    above the floor, and only when no cleared route exists does the cutter lift 0.5 mm above the stock and rapid
    across inside the pocket. A full retract to the safe height is the last resort (once per band on that job);
  - rapids never run or come down into standing material (the simulator found 47 such moves on the job above;
    there are none now).

  On that job: 34.1 → 22.2 min for the four roughing bands; in the first band conventional cutting 332 → 1.3 mm and
  feed in air 2.8 m → 0.11 m; the same material removed, no gouges or collisions.

## [0.38.0] — 2026-10-17

### Changed
- **The Makera Z1 spindle head is modelled from Makera's own CAD.** It was a guess with an exposed finned motor at
  the bottom and a magnetic dust shoe the Z1 doesn't have. It is now Makera's simplified Z1 model
  (MakeraInc/CarveraProfiles), mapped into the machine frame and checked solid against solid (99.2 % volume overlap):
  the Ø16 × 20 nose, the 65 × 70 head cover running from the top of the nose to 286 mm, sitting 52 mm in front of the
  spindle axis and 18 mm behind it with its chamfered front-bottom edge, the finned motor cap on top, and the X carriage
  block behind it. From Makera's quick start: the quick-change lever, the status LED and fan cooling instead of a dust
  shoe. The wired probe stored on the head's right side is included from Makera's photo (estimated size).
- **Heads that aren't round are checked as they are.** A machine's holder / nose stack can now describe a level as a
  box (or several) about the spindle axis — `{"name": "head", "box": [x0, x1, y0, y1], "h": ...}` — so the collision
  check sees the Z1's head 52 mm in front of the tool and only 18 mm behind it. It used to be a Ø72 circle, which
  missed 16 mm of head in front and invented 18 mm behind. The 4th-axis chuck and tailstock checks use the head's real
  X extents too.

### Fixed
- **The Z1 vise sits where Makera's pins put it.** The measured Z1 aluminium-bed hole pattern (cinetronixlabs, checked
  against Makera's bed file) has the two Ø4 locating-pin holes at x ±95.5 mm and M5 holes that match the vise's
  mounting holes (103.5 mm and 67 mm apart): the vise is centred in X and fills the front half of the bed (centre
  y −51.5), not 10 mm behind the centre as estimated from the install drawing. Makera's "17.5 mm" label is the left
  end to the locating pins (17.5 + 191 + 17.5 = 226).


## [0.37.0] — 2026-10-17

### Added
- **Several fixtures on the table at once.** Besides the fixture that holds the stock, a setup can have any number of
  others, each placed (X / Y from the table centre) and turned on its own: `Setup(..., fixtures=[{"name": "Step
  clamps", "at": (x, y), "rot": deg, "params": {...}}])`, or from the CAM tab's new Fixtures panel (pick, move, turn
  90°, add, remove; each change lands in cam.py's `fixtures({...})` line). The viewer draws them all on the machine,
  the simulator checks the tool and holder against every one and names the fixture it hit (which flashes red on the
  machine), and the program check warns when a fixture overlaps the stock or another fixture, or is made for another
  machine. Fixtures now get their own collision grid, so a big vise never coarsens the stock's simulation grid.

### Changed
- **The Makera Z1 low-profile vise is modelled from Makera's own dimensions.** It was an estimate that looked nothing
  like the real one. Now: the 226 × 103 mm T-slot extrusion, the removable fixed-jaw block at −X, the moving jaw, its
  body and the jaw carrier on the T-slots, screw 2 at the end, four counterbored M5 mounting holes at 105 / 208.5 mm,
  jaws closing along X up to the labelled 130 mm, and the slotted soft jaws: thin stock sits on the jaw ledges 20 mm
  above the rails (gripped by the rebate walls), stock taller than the 22.6 mm jaws on the rails (`seat='ledge'` /
  `'rails'`, automatic by default). Labelled values come from Makera's dimensioned product photos and the Z1 vise
  quick start; heights and block lengths are measured from those photos (±0.5 mm, taken at the upper end so the
  collision check errs on the safe side) and can be overridden with caliper readings (`base_h`, `jaw_h`, `ledge`, ...).
- **Fixtures that replace the spoilboard.** The Z1 vise bolts to the aluminium bed with the MDF removed, as Makera's
  guide says: it stands 6 mm lower, the spoilboard is not drawn, and the table check uses the bed. A fixture can say
  where it usually sits (`home`); the Z1 vise defaults to its place on the bed.
- A fixture turned on the table now sees the stock in its own frame, so a vise turned 90° opens to the stock's Y size.

### Fixed
- The simulator's table check counted the fixture height twice, so a tool going into the bed under a fixture was
  reported too late.


## [0.36.4] — 2026-10-14

### Fixed
- **Ticks around circular rims in the rendered stock.** About one rim triangle in twenty spans the fold between the
  top face and a curved wall, and it was shaded with its own 45° facet. Every vertex now keeps the exact surface
  planes found by the edge solve, and each triangle corner shades with the plane nearest its own orientation, so a
  fold-spanning triangle blends from face to wall instead of catching the light; around a Ø16 pocket rim no corner
  carries a stray normal any more (105 did).


## [0.36.3] — 2026-10-14

### Fixed
- **Beaded chamfers and striped walls on longer programs.** Programs over 20 000 moves still built the voxel stock
  from a server keyframe (whose walls and slopes carry the height map's cell steps) on load and on every seek back.
  The worker stamps about 40 000 moves a second, so it now restamps from the untouched stock for programs up to
  250 000 moves, in batches so the stock is seen being re-cut; a chamfer measures within 0.001 mm of its plane.
  Vertices carry a continuous tangent frame (brush direction along X on faces, around the wall elsewhere), so the
  anisotropic highlight no longer tears at 45°, and the metal is toned down from the washed-out 0.36.2 look.
- **Sharp edges on the voxel stock.** Every stamp now records the outward normal of the surface nearest each voxel
  (Hermite data), and each mesh vertex is placed by a least-squares fit to the planes through its edge crossings
  (dual contouring), so pocket rims, hole edges and the chamfer's borders land on the true edge instead of a one-cell
  bevel that caught the light as a row of dots; on a Ø16 pocket rim the vertices sit within 0.015 mm of the edge.


## [0.36.2] — 2026-10-13

### Changed
- **The rendered stock looks like aluminium.** The voxel mesh now carries split-edge normals (milled edges stay sharp,
  curved walls stay smooth) and planar UVs, and the rendered material is a bright metal with a satin, lightly brushed
  roughness map and anisotropic highlights. The untouched box stock is built from its exact dimensions rather than
  the height map, so its outer faces are flat instead of finely striped.


## [0.36.1] — 2026-10-13

### Fixed
- **Striped pocket walls and a flash of the finished part during playback.** Forward playback reloaded a server
  keyframe every time it crossed one, which replaced the exactly stamped voxel walls with walls rebuilt from the
  height map (striped at its cell size) and emptied the stock for a moment while the field was rebuilt; while a
  keyframe was in flight, every animation frame requested it again. Playback now keeps stamping forward without
  reloads, a seek backwards restamps from the untouched stock in the worker (exact walls; a keyframe only for very
  long programs), the previous stock stays on screen until the new one is meshed, keyframe requests are shared, and
  the end of the program tints gouges and material left without a rebuild. Walls that do come from a keyframe use a
  smoother lateral distance.


## [0.36.0] — 2026-10-13

### Changed
- **Toolpaths ramp into the material instead of plunging.** Pocket passes descend along the innermost ring that is
  long enough to ramp on (a helix on a small ring) at 3° (`ramp=`, 0 to plunge), from the previous level's floor; the
  rings inside it follow at depth. Contour passes get one entry per ring: the lead-in just above the stock, then
  every pass ramps down along the profile from where the previous pass ended and runs a full lap, with no retract
  between passes; tabs still lift the tool. Face passes start and end with the tool clear of the stock, so their
  plunges are in air. Adaptive already entered on a helix.
- **Helical arcs in the G-code.** Ramps on circular paths post as G2/G3 with Z (every supported controller
  interpolates helices), and straight ramps merge into single lines, so programs stay as short as before.

### Fixed
- The holder and fixture collision checks reported the first contact in each batch of moves rather than the deepest,
  which understated a collision that grows along a ramp. They now keep the deepest contact.


## [0.35.2] — 2026-10-13

### Fixed
- **Depth of field blurred the part.** The bokeh pass takes its aperture per world unit and the scene is in
  millimetres, so anything beyond about 50 mm from the focus plane was at full blur, which was most of the part. The
  blur now grows with depth relative to the focus distance (full blur at 1.5× the distance behind the cut, or at the
  camera in front) with a smaller maximum, so the part around the cut stays crisp and only the far machine and the
  near fixture soften.


## [0.35.1] — 2026-10-13

### Changed
- **Playback runs at the machine's real pace by default** (1×, was 10×), and the CAM tab has a *Playback* section
  with a clear speed control (¼× to 100×) that says how long the program takes on the machine and how long it takes
  to watch at the chosen speed. The bar's speed menu stays and the two are in step; the choice is remembered.


## [0.35.0] — 2026-10-13

### Added
- **Voxel stock.** The simulated stock in the viewer is now a true 3D solid: a signed-distance voxel field in a Web
  Worker (`static/voxel_stock.js`) that every tool move is stamped into as a swept cylinder with the tool's end
  profile, meshed per chunk with surface nets. Walls are smooth and land between voxels (the lateral distance to the
  cut gives sub-voxel positions), floors are exact, and the field resets from the server's keyframes on a seek. About
  2.5 million voxels at most, so the cell size follows the stock (≈0.25 mm on a 66 × 46 × 13 mm plate). Settings ▸
  General ▸ *Simulation stock* switches back to the lighter height map (stepped walls) for a weak PC.
- **Depth of field in the rendered simulation.** A bokeh post-process keeps the cut in focus and softens the
  foreground and background, matched to the plain renderer's tone mapping. Settings ▸ General ▸ *Rendered
  simulation* turns it off; it also switches itself off when a GPU takes too long per frame.

### Fixed
- The stock outline and the work-origin triad no longer show through the machined stock in the rendered simulation.


## [0.34.0] — 2026-10-13

### Added
- **Rendered simulation.** *Rendered* in the CAM bar (and ribbon ▸ CAM) plays the program as a studio render: metallic
  stock machined in front of you, the part hidden until the stock becomes it, a chrome holder stack built from the
  machine's nose profile with a spinning fluted tool, toolpaths and wireframes hidden, a dark backdrop with a
  vignette, a camera that follows the tool while keeping your orbit, and a HUD with the machine, the step being
  cut, tool changes, and the speed factor. Works with the Machine view and its doors.
- **Continuous material removal.** The simulated stock is now a live height map in the viewer: the tool is stamped
  along every move as playback runs (the server's keyframes are seek points), so the stock changes in step with the
  tool instead of jumping between snapshots; surfaces are flat-shaded for crisp machined floors and walls.

## [0.33.1] — 2026-10-11

### Fixed
- **Drilling finishes each hole.** `holes()` reports a counterbore and its through hole as two entries at the same
  position; `drill` drilled them as two visits, so a pecked hole looked like "one peck, next hole, come back".
  Same-position holes are now merged into one visit to full depth with all its pecks, and holes are visited in
  nearest-neighbour order.
- **Pockets finish one region before the next.** `pocket` cut every region one level at a time (hopping between
  pockets at each depth); it now takes each region through all its depths, plunging the next level where the tool
  already is instead of retracting to safe Z, and visits regions nearest-neighbour.

## [0.33.0] — 2026-10-11

### Added
- **Fixtures: vises, clamps, plates.** How the work is held is now part of a CAM setup: `Setup(..., fixture="Makera
  low-profile vise", fixture_at=(x, y), fixture_rot=deg, fixture_params={...})` seats the stock's bottom-centre at the
  fixture's work origin (thin stock is raised on parallels automatically), the viewer draws the fixture around the
  stock and on the machine's bed, and the simulator treats every fixture part as uncuttable: the tool or holder
  touching a jaw, a clamp bar or a plate is reported as a collision with that fixture. Built-ins: Makera low-profile
  vise (Z1), Carvera Air vise, 4" screwless vise, 6" Kurt-style vise, tooling plate (parametric), step clamps.
  A fixture is a script ([fixture_lib.py](fixture_lib.py), `fixture_scripts/`, `workspace/fixtures/<slug>.fixture.py`)
  in the same spirit as machines: `part(...)` shapes plus `fixture(name, work_origin, clamp_axis, max_opening, mount,
  sources, notes)`, with `params` and the stock size available. **CAM tab ▸ Setup** has a fixture picker (it lands in
  a `fixtures({...})` line in cam.py, which wins over the script); **Settings ▸ Fixtures** lists them with *Show in
  viewer* and *Edit as script…*; **Code tab ▸ fixture.py** edits and builds them; the agent gets `build_fixture` /
  `get_fixture_code` and a brief: use the fixture you name, otherwise recommend one and say so. `GET /api/cam/fixtures`,
  `/api/cam/fixture_model`, `/api/cam/fixture_code`; WebSocket `set_fixture`, `run_fixture`, `get_fixture_code`.

## [0.32.0] — 2026-10-10

### Changed
- **A proper header.** The floating File / Settings buttons and the floating ribbon are replaced by one header that
  spans the viewer: brand mark and **File** menu on the left, real tabs (**Design · CAM · Output · Extensions**) with
  an indicator, **Settings** on the right, and a tool band of fixed height beneath. The band never changes size: labels
  drop to icons when the row would overflow, then groups fold into dropdowns from the right. The Browser tree and
  the ViewCube sit under the header instead of being pushed around by it.

## [0.31.0] — 2026-10-10

### Changed
- **Tabbed ribbon.** The modelling ribbon is now tabbed like Fusion: **Design** (Create, Modify, Assemble, Inspect),
  **CAM** (Machine, Doors, Simulate, Play), **Output** (Render, Drawings, Slice) and **Extensions**. It keeps to one
  row: labels drop to icons when the row would wrap, then groups fold into dropdowns from the right, so a 13" laptop
  with the side panel open no longer loses a quarter of the viewer to a wrapped ribbon. The ribbon can be collapsed
  to its tab strip (▴), and the last tab and collapsed state are remembered.
- The CAM tab's machine picker follows the program's machine; the playback readout has its own line.

## [0.30.2] — 2026-10-10

### Changed
- **Machine view framing:** showing the machine frames the whole machine, hiding it frames the part(s) again, so no manual zooming when switching.

## [0.30.1] — 2026-10-10

### Fixed
- **Haas doors** no longer poke through the enclosure side when fully open: they park inside the wing pockets.

## [0.30.0] — 2026-10-10

### Added
- **Simulation playback like the real machine.** Playback is now time-based: every move takes its real time (distance at
  the programmed feed, rapids at the machine's rapid rate, A moves at the rotary's speed) and the tool moves
  continuously along each move instead of jumping per move. A speed control (¼× to 100× real time), ⏮ / ⏭ to jump
  between operations, the slider in program time, and a readout of setup · operation · tool · move · elapsed / total.
  The operations list shows setup headers (WCS, orientation), highlights the operation being run and has ▶ to play
  from any operation or setup. On the Machine view a flipped (bottom) setup turns the work over on the table.

## [0.29.1] — 2026-10-10

### Fixed
- **Haas VF-4 doors:** they now meet at the centre when closed (they were modelled part-open, as in Haas's photos) and
  park out in the wings when opened, outer edge flush with the enclosure side.

## [0.29.0] — 2026-10-10

### Added
- **Machine view in the CAM tab.** A *Machine view* section at the top of the CAM tab (no program needed): pick any
  library machine, **Show machine** / **Hide machine**, and **Open doors** / **Close doors**.
- **Doors and lids in machine models.** A machine script can declare door nodes, `node(name, parent, door="hinge",
  pivot=..., direction=..., open=degrees)` for a lid that swings about a hinge line or `door="slide"` with
  `open=mm` for sliding doors; the parts on that node animate open and closed in the viewer instead of the model
  being rebuilt. The built-in Makera Z1 and Carvera Air canopies lift about their rear hinges and the Haas doors
  slide apart. The agent's brief asks for door nodes on any machine with a cover.

### Fixed
- **Makera Z1 model:** the invented front accessory rail, probe dock and standing probe are gone; the tool-length
  sensor now sits in a cutout at the rear-right corner of the bed (position estimated, per reviews — measure yours).

## [0.28.0] — 2026-10-10

### Added
- **Startup splash with progress.** The server now accepts connections at once and builds the last design in the
  background, so the window shows a splash screen immediately: the steps as they run (reading the workspace, building
  the design, rebuilding the CAM program), a progress bar, elapsed time on the step in progress, and a note when a
  design with real threads is taking minutes. The desktop app shows its own splash even earlier, while the CAD kernel
  loads and the server starts, then hands over to the page. `GET /api/startup` reports the status; the page falls back
  to polling it while the build holds the server busy. `AGENTICCAD_STARTUP_SYNC=1` restores the old blocking start
  (tests).

## [0.27.1] — 2026-10-10

### Fixed
- **Carvera Air model:** the canopy's front pane now leans back from the base trim to the chamfered top edge as on the
  real machine (it was vertical); the LED bar moved inside the leaned pane.

## [0.27.0] — 2026-10-10

### Added
- **Machines are scripts: model your own machine.** Every library machine's 3D model is now a Python script in the same
  spirit as `design.py` and `cam.py`: `machines/<slug>.machine.py` in the workspace, written against a small kinematic
  API (`node(...)` for the moving axes, `part(...)` for build123d shapes with a material and a collision flag,
  `machine(...)` for home, travel, clearance, the nose profile, table and 4th axis) in the design-script namespace, so
  vendor CAD can be attached with `import_step`. The built-in Makera Z1, Carvera Air, Haas VMC and gantry-router models
  are the same kind of script ([machine_scripts/](machine_scripts/)) and are what a machine falls back to. **Code tab ▸
  machine.py** shows the script of any library machine (the built-in one as a starting point), Run validates it,
  saves it as that machine's own model and shows it; **Settings ▸ Machines ▸ Model** gains *Show in viewer* (the
  Machine view now works without a CAM program, with the current design sitting on the table) and *Edit as script…*.
  The agent gets `get_machine_code` and `build_machine` and a brief on the API, so "add my Shapeoko 5 Pro and model
  it" is a chat request; two evals (`machine_shapeoko_5_pro`, `machine_tormach_1100m`) grade the result against the
  brief. Deleting a machine deletes its script. `GET /api/cam/machine_code`, WebSocket `run_machine` /
  `get_machine_code`.
- **Built-in models rebuilt from the makers' photos.** The Makera Z1 and Carvera Air (plain and 4th axis) and the Haas VF-4 built-ins
  were modelled by the agent against the manufacturers' product photos (charcoal plinth and wrap-around tinted canopy on
  the Z1; white body and chamfered canopy with the pendant arm on the Air; enclosure, wings, doors, side-mount changer
  with its cover, pendant, auger and chute on the VF-4), with every
  number sourced as verified / reference / estimated. The other Haas models share a detailed parametric VMC script
  (umbrella or side-mount changer by model). Drop photos of your own machine into the chat with "model my machine" and
  the agent works the same way. New material `paint_red`.

## [0.26.0] — 2026-10-10

### Added
- **Machine models and collision checks.** Every built-in machine now has a kinematic 3D model
  ([machine_models.py](machine_models.py)) built in build123d from published and measured numbers: Makera Z1 and
  Carvera Air (bed with the real hole grid from the official/community bed files, moving bed under the rear bridge,
  head in X/Z, 4th-axis chuck and tailstock, enclosure), Haas VF-2 / VF-2SS / VF-4 / Mini Mill / TM-1 (table with the
  real T-slots, saddle, column, CAT40 + ER32 holder, verified nose-to-table range) and a parametric gantry router for
  other machines. **Machine** in the ribbon (Output, `A`) shows the program's machine around the stock and plays the
  simulation on it: head, table and chuck move as they would, the tool hangs from the spindle, and parts the simulator
  saw collide flash red at that move. The simulator checks the whole stack above the flutes (shank, collet nut, nose,
  collar, head) against the remaining stock, the tool tip against the bed under the spoilboard, and the holder against
  the 4th-axis chuck and tailstock; contacts are reported in the summary and in `simulate_cam`. **Settings ▸ Machines ▸
  Model** lists every number with its source and accuracy tier (verified / reference / estimated) and takes measured
  overrides (nose profile, stickout, spoilboard, clearance) per machine (`"model"` in the machine JSON). `GET
  /api/cam/machine_model` serves the tessellated model. CAM remains experimental.

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
