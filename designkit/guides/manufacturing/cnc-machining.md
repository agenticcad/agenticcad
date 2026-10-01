---
title: Designing for CNC machining
category: manufacturing
tags: [cnc, milling, machining, turning, tolerance, corner radius, pocket, drilling]
summary: Corner radii, pocket depths, wall thickness, holes and threads, tolerances and setups for milled and turned parts
related: [tolerances/fits-and-clearances, tolerances/materials, reference/metric-threads]
---
## Milling

- Internal vertical corners take the tool's radius: design them at least 1.1 to 1.3 × the tool radius (e.g. R3.5
  for a Ø6 cutter) so the tool isn't buried in the corner.
- Pocket depth up to 3 to 4 × the tool diameter is routine; 6 × needs long tools and costs more.
- Walls: ≥ 0.8 mm in metal, ≥ 1.5 mm in plastics; thin tall walls chatter.
- Floors of pockets are flat with a corner radius (tool nose) or sharp with a flat endmill.
- Features that face six directions mean six setups: put features on as few faces as possible.

## Holes and threads

- Standard drill sizes are cheapest; depth up to ~4 × diameter is easy, 10 × is gun-drilling territory.
- Tapped holes: thread depth 1.5 × d is plenty (2 × d in aluminium); drill 1 to 2 threads deeper than the thread.
- Blind holes have a 118° drill point at the bottom unless you ask for flat.

## Turning

- Keep diameters coaxial and on one axis; undercuts for thread relief (width ≥ 2 × pitch).
- Length-to-diameter over ~4 needs a tailstock.

## Tolerances

- ±0.1 mm is standard (ISO 2768-m); ±0.025 mm on specific features costs more; call out fits (H7) rather than
  tolerancing every dimension.
- Specify surface finish only where it matters (Ra 3.2 standard milled, Ra 1.6 fine, Ra 0.8 ground).
