---
title: Fits and clearances by process
category: tolerances
tags: [fit, tolerance, clearance, iso 286, press fit, h7, machining]
summary: ISO 286 hole/shaft fits in plain terms, and practical clearances for machined, printed and laser-cut parts
related: [bearings/selection-and-retention, manufacturing/fdm-3d-printing]
---
## ISO 286 in plain terms (hole-basis, H7 hole)

| Fit | Shaft | Feel | Use |
|---|---|---|---|
| H7/g6 | g6 | Close running, slides freely | Pivots, sliding shafts, locating pins that come out |
| H7/h6 | h6 | Sliding, no play | Locating spigots, pilots |
| H7/k6 | k6 | Transition, light press | Bearing inner rings, gears on shafts |
| H7/p6 | p6 | Press fit | Permanent bushings, dowels in holes |

Typical magnitudes at 10 to 18 mm: H7 is +0/+18 µm; g6 is −6/−17 µm; k6 is +1/+12 µm; p6 is +18/+29 µm.
Model nominal sizes in CAD and state the fit on the drawing; don't model µm offsets.

## Practical gaps by process

| Process | Sliding gap per side | Press |
|---|---|---|
| CNC machined | stated by fit (above) | stated by fit |
| FDM printed | 0.1 to 0.15 mm | 0 to 0.05 mm |
| Resin printed | 0.05 to 0.1 mm | 0 |
| Laser-cut sheet (kerf ~0.1 to 0.2 mm) | slot = sheet thickness + 0.1 mm | slot = thickness − 0.05 mm |

## General tolerances

For dimensions without a tolerance, ISO 2768-m (medium) is the usual default on drawings: ±0.1 mm up to 6 mm,
±0.2 mm up to 30 mm, ±0.3 mm up to 120 mm.
