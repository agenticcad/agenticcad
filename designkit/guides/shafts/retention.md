---
title: Shaft retention and torque transfer
category: shafts
tags: [shaft, key, keyway, circlip, e-clip, set screw, d-flat, collar]
summary: Ways to fix parts to shafts (keys, D-flats, set screws, clamps) and to stop them sliding (circlips, E-clips, collars, shoulders)
related: [parallel_key, keyway_cutter, circlip_external, e_clip, shaft_collar, set_screw, bearings/selection-and-retention]
---
## Transmitting torque

| Method | When |
|---|---|
| D-flat + set screw on the flat | Small motors (NEMA shafts), light loads. The set screw point bears on the flat. |
| Parallel key (DIN 6885) | Real torque, removable. Key size from the shaft diameter (`kit.parallel_key`). |
| Clamp hub (split + screw) | No shaft damage, zero backlash, good for couplings and pulleys. |
| Press fit / splines | Production, high torque. |

Keyway depth in the shaft is t1 from the table. The key sits 1 to 2 mm proud; the hub keyway takes the rest.

## Stopping axial movement

- Shoulders (a step in the shaft) are the most robust.
- Circlips (DIN 471 on a shaft, DIN 472 in a bore) take good thrust and need a groove (`kit.circlip_groove_external`).
- E-clips (DIN 6799) are quick to fit sideways, for light loads.
- Shaft collars with a set screw are adjustable and need no machining.
- Spacers and sleeves transfer location between parts on the same shaft.

## Set screws

- Cup point for general holding, flat point when the shaft is adjusted often, dog point into a hole or groove.
- Length: at least the shaft diameter's half for small sizes, and the screw shouldn't stick out of the hub.
- Two set screws at 90° hold much better than one.
