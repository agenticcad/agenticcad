---
title: Designing for FDM 3D printing
category: manufacturing
tags: [3d printing, fdm, fff, pla, petg, overhang, clearance, orientation]
summary: Wall thickness, overhangs, holes, clearances, orientation and fastening rules for parts printed on FDM printers
related: [tolerances/fits-and-clearances, fasteners/choosing-fasteners, bearing_seat]
---
## Geometry

- Walls: multiples of the line width. With a 0.4 mm nozzle use 0.8, 1.2 or 1.6 mm; 1.6 to 2.4 mm for strong parts.
- Overhangs up to about 45° print without support; chamfer (don't fillet) the downward-facing edges.
- Horizontal holes print oval at the top: use a teardrop or flat-topped hole, or drill them out.
- Bridges up to about 20 mm are fine; longer ones sag.
- Minimum feature about 2 line widths; text at least 0.6 mm deep and 4 mm tall.

## Clearances (typical, well-tuned printer)

| Fit | Gap per side |
|---|---|
| Press fit | 0 to 0.05 mm |
| Snug / sliding | 0.1 to 0.15 mm |
| Free running | 0.2 to 0.3 mm |
| Print-in-place moving parts | 0.3 to 0.5 mm |

Holes print undersize by about 0.1 to 0.2 mm: model a 3.4 mm clearance hole for M3 as 3.5 mm, or ream it.

## Orientation

- Layers are the weak direction: orient so loads run along the layers, not peeling them apart.
- Put the largest flat face on the bed; put cosmetic faces up or on the sides.
- Screw bosses: vertical axis prints the roundest holes.

## Fastening

- Heat-set brass inserts for repeated assembly (hole ~0.1 to 0.3 mm under the insert's knurl diameter).
- Captive nut traps: hex pocket + 0.2 mm across flats.
- Self-tapping into plastic: hole about 85% of the screw's major diameter, for a few assembly cycles.

## Slicing in AgenticCAD

Use the Slice tool (or slice_for_printing) with your installed OrcaSlicer or Bambu Studio to check layers,
supports and print time before exporting.
