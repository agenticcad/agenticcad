---
title: Choosing fasteners and thread engagement
category: fasteners
tags: [screw, bolt, thread, engagement, clearance hole, tap drill, counterbore]
summary: Which screw head to use, clearance and tap-drill sizes, counterbores, thread engagement lengths and screw length choice
related: [socket_screw, button_head_screw, countersunk_screw, hex_bolt, hex_nut, manufacturing/fdm-3d-printing]
---
## Head types

| Head | Use |
|---|---|
| Socket head cap (ISO 4762) | Default for machines: strong, compact, sits in a counterbore. |
| Button head (ISO 7380) | Low profile on the surface, covers, sheet. Lower torque. |
| Countersunk (ISO 10642) | Flush surfaces. Needs a 90° countersink. |
| Hex bolt (ISO 4017/4014) + nut | Through-bolting, structures, where a spanner fits. |
| Set screw (ISO 4026-4029) | Locking a hub on a shaft. |

## Holes

| Size | Tap drill | Clearance (medium) | Counterbore for socket head |
|---|---|---|---|
| M2 | 1.6 | 2.4 | Ø4.4 × 2.2 |
| M2.5 | 2.05 | 2.9 | Ø5.5 × 2.7 |
| M3 | 2.5 | 3.4 | Ø6.5 × 3.3 |
| M4 | 3.3 | 4.5 | Ø8 × 4.4 |
| M5 | 4.2 | 5.5 | Ø10 × 5.4 |
| M6 | 5.0 | 6.6 | Ø11 × 6.5 |
| M8 | 6.8 | 9.0 | Ø15 × 8.6 |

`hole(part, 3.4, at=..., counterbore=(6.5, 3.3))`, `tap(part, "M3", at=..., depth=6)`.

## Engagement

- Steel into steel: engage at least 1 × d. Into aluminium: 1.5 to 2 × d. Into brass or cast iron: 1.5 × d.
- Into plastic, use heat-set inserts rather than threading the plastic.
- Tapped depth = engagement + 2 pitches; drill a further 1 to 2 mm below the thread.

## Choosing length

Head seat to tip = clamped thickness + engagement. Round up to a stock length:
3, 4, 5, 6, 8, 10, 12, 16, 20, 25, 30, 35, 40, 45, 50. Make sure the tip stops short of the tapped depth.

## In CAD

- `kit.socket_screw("M3", 16)` seats with its head on the plane you move it to. `real=True` models the helix;
  mating real threads overlap by a few mm³ in an interference check, which is expected.
- Build identical real-thread screws by calling the same function with the same arguments: they are built once.
