# Design Kit

The Design Kit is what the agent reaches for before it models something standard or designs in an unfamiliar area.
It has two halves that share one catalogue:

- **Components**: Python functions that build real, standard parts in one call (`kit.ball_bearing("688")`,
  `kit.socket_screw("M3", 40)`, `kit.nema_stepper(17, 40)`). A design stays one plain build123d script; it just calls
  these. Each function carries metadata: category, tags, a one-line summary, the standard it follows, and an example.
- **Guides**: Markdown notes (with images where useful) on how to design things well: gear sets, bearing retention,
  thread engagement, 3D-printing clearances, build123d pitfalls. Each has frontmatter: title, category, tags,
  summary, related entries.

## Using it in a script

```python
motor = kit.nema_stepper(17, 40, detail="full")               # dict of parts, front face at z = 0
stage = kit.planetary_stage(0.6, 18, 18, 3, face_width=8)     # sun, planets, ring, phased and meshing
result = {"Motor": motor,
          "Stage": kit.place(stage, Pos(0, 0, 13)),
          "Bearing": kit.place(kit.ball_bearing("688ZZ"), Pos(0, 0, 29)),
          "Screw": Pos(15.5, 15.5, 37) * kit.socket_screw("M3", 40, real=True)}
```

Components are builder-safe: calling them inside a `with BuildPart()` block never adds to that part.

## Two layers

| Layer | Where | Who writes it |
|---|---|---|
| Built-in | `kit/` in the app, versioned with each release | Us. Read-only to the agent. |
| Workspace | `<workspace>/kit/` (`parts/*.py`, `guides/**/*.md`) | The agent and the user, as they work. |

Workspace entries use the same format as built-in ones, so a good one can be promoted into the built-in kit by a PR.

## Table of contents, kept by the agent

`TOC.md` (built-in) and `<workspace>/kit/TOC.md` are generated from the entries' metadata, never hand-edited, so they
can't drift. The agent keeps the workspace layer growing and useful:

- **Usage**: every successful build that calls `kit.<name>(...)` and every guide the agent reads is counted, so the TOC
  can show what this workspace actually uses.
- **Notes**: `kit note <entry> "..."` appends a dated lesson to an entry, for example "688 bearings: use the 5 mm ZZ
  width, the open width is 4". Notes show when the entry is read.
- **New entries**: `kit save_guide` and `kit save_part` write into the workspace layer and regenerate its TOC.

## Agent tool

`kit` with actions `toc`, `search`, `read`, `note`, `save_guide`, `save_part`. The system prompt only lists the
categories and says to search the kit before hand-modelling standard parts, so the prompt stays small as the kit
grows. `read` returns a guide's full text, or a component's signature, parameters, standard, example and notes.

## Catalogue

Status: **available** = in this release, **planned** = next. Categories are fixed; tags are free.

| # | Category | Components | Guides |
|---|---|---|---|
| 1 | Fasteners & threads | socket head (ISO 4762), button head (ISO 7380), countersunk (ISO 10642), set screws (ISO 4026-4029), hex bolts and nuts (ISO 4017/4032), washers (ISO 7089), all with optional real threads, heat-set inserts with hole cutters, hex standoffs (FF/MF/MM) **available**; nyloc, flange and square nuts, thumb screws, rivets **planned** | Choosing fasteners and thread engagement, fastening into plastic **available**; bolted joints **planned** |
| 2 | Bearings & bushings | Deep-groove ball bearings by designation: 6xx, 60xx, 62xx, 68xx, 69xx, MR, open or ZZ/2RS (rings with raceways, balls, cage, shields), bearing seat cutters **available**; flanged F6xx, thrust, linear LMxUU, plain bushings, pillow blocks **planned** | Bearing selection, fits and retention **available**; preload **planned** |
| 3 | Shafts & retention | External and internal circlips (DIN 471/472) with groove cutters, E-clips (DIN 6799), parallel keys (DIN 6885) with keyway cutters, shaft collars, dowel pins (ISO 8734), rigid and jaw couplings **available**; beam couplings, spring pins, D-shafts **planned** | Shaft retention **available**; key sizing, coupling selection **planned** |
| 4 | Power transmission | Spur gears, internal ring gears, planetary layout and complete planetary stages, GT2 pulleys, T8 lead screw with flange nut **available**; racks, HTD pulleys, belts, ball screws (SFU), sprockets, bevel and worm gears **planned** | Spur gears, planetary gear sets, belt drives and lead screws **available** |
| 5 | Motors & actuators | NEMA 8/11/14/17/23 steppers, outside only, or full internals for 14/17/23; 28BYJ-48; hobby servos (SG90, MG90S, MG996R, DS3218); N20 gear motors **available**; brushless outrunners, DC motors, linear actuators **planned** | Mounting motors and servos **available** |
| 6 | Structural & framing | T-slot extrusions (2020 to 4080) and their profiles, corner brackets, T-nuts **available**; V-slot, gussets, tube, angle, channel, DIN rail **planned** | Frames from extrusion **available** |
| 7 | Electronics packaging | Arduino Uno, Raspberry Pi 4, Pi Pico (exact mounting holes, connector envelopes), fans 25 to 120 mm with grille cutters, 18650 cells **available**; Arduino Nano, ESP32 DevKit, Pi 5/Zero, connectors, switches, displays, cell holders **planned** | Mounting boards, venting and cable entry **available** |
| 8 | Enclosures & housings | Screw-lid box with insert bosses and lip, screw bosses, vent slot cutters, snap-fit hooks **available**; living hinges, gasket grooves, feet **planned** | Enclosure design for 3D printing **available** |
| 9 | Mechanisms | Knuckle hinges, compression springs (closed/ground ends) and extension springs swept on the real helix, fluted knobs, pull handles, disc cams, link bars **available**; torsion springs, latches, detents **planned** | Hinges and print-in-place, springs, linkages and cams **available** |
| 10 | Fluid & pneumatic | O-rings with face/piston/rod groove cutters, tube, push-fit fittings and hose barbs (M5, BSP, NPT ports) **available**; elbows and tees **planned** | O-ring grooves **available** |
| 11 | Manufacturing | (guides only) | FDM and resin printing, CNC machining, sheet metal, laser and waterjet, injection moulding **available**; SLS/MJF **planned** |
| 12 | Tolerances, fits & materials | (guides only) | Fits and clearances by process, material properties and densities **available** |
| 13 | Standards & reference | (tables inside the components) | ISO metric threads, standard stock sizes **available** |
| 14 | Modelling with build123d | (guides only) | Builder pitfalls and assemblies (including real-thread performance), face selection **available** |
