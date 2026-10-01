---
title: Bearing selection, fits and retention
category: bearings
tags: [bearing, ball bearing, fit, retention, housing, shaft, preload]
summary: Picking a deep-groove bearing, which ring gets the tight fit, housing and shaft seats, and how to hold bearings axially
related: [ball_bearing, bearing_seat, circlip_internal, circlip_external, shafts/retention, tolerances/fits-and-clearances]
---
## Picking one

- Start from the shaft diameter. Common: 608 (8×22×7), 625 (5×16×5), 688 (8×16×5 ZZ), 6000 series for 10 mm and up.
- Thin section (68xx/69xx, MR) where space is tight. 62xx for heavier loads.
- ZZ (metal shields) or 2RS (rubber seals) keep grease in and dirt out; they are slightly wider in the 68x/MR ranges.
- Two bearings on a shaft, spaced at least about 1.5 × shaft diameter apart, to take moments.

## Fits (machined parts)

- The ring that rotates relative to the load gets the interference fit. Usually a rotating shaft means an
  interference fit on the inner ring (shaft k5/k6) and a sliding fit on the outer ring (housing H7).
- 3D printed housings: model the seat at nominal + 0.1 to 0.2 mm on diameter and let the print shrink onto it.

## Axial location

Locate one bearing on both sides (the fixed bearing) and let the other float axially, so thermal growth
doesn't load the bearings.

- Shoulders: a shaft shoulder or housing shoulder no taller than the ring (don't touch the shields or seals).
- Circlips: DIN 471 on shafts, DIN 472 in bores, sitting against the ring face.
- Spacers between bearings, and on the shaft between a bearing and a gear or carrier.
- E-clips or collars for light loads.

## In CAD

- `kit.ball_bearing("688ZZ")` gives rings, balls, cage and shields. Place it with `kit.place(b, Pos(...))`.
- `kit.bearing_seat("688", clearance=0.15)` is the cutter for a printed housing seat.
- Leave a clear bore in the housing past the seat, or a shoulder of Ø D − 2 mm, so the outer ring is held
  but the shield is free.
