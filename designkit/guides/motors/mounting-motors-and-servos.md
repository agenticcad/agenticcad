---
title: Mounting motors and servos
category: motors
tags: [motor, stepper, nema, servo, mount, bracket, pilot, alignment]
summary: NEMA flange patterns, pilot bosses, shaft alignment, servo horns and brackets, and cooling for motor mounts
related: [nema_stepper, servo, stepper_28byj48, n20_gearmotor, rigid_coupler, gt2_pulley]
---
## NEMA steppers

| Frame | Square | Hole pattern | Holes | Pilot |
|---|---|---|---|---|
| 8 | 20.3 | 16.0 | M2 | Ø15 × 1.5 |
| 11 | 28.2 | 23.0 | M2.5 | Ø22 × 2 |
| 14 | 35.2 | 26.0 | M3 | Ø22 × 2 |
| 17 | 42.3 | 31.0 | M3 | Ø22 × 2 |
| 23 | 56.4 | 47.14 | Ø5.1 through | Ø38.1 × 1.6 |

- Locate the motor on its pilot boss: bore the plate Ø22.1 to Ø22.2 (printed: +0.2 to 0.3) so the shaft is
  concentric, and use the screws only to clamp.
- Screw length into NEMA 17 front threads: plate thickness + 3.5 to 4 mm (the tapped depth is about 4.5 mm).
- Slotted holes let you tension a belt: slot along the belt direction, 4 to 6 mm of travel.
- Steppers run hot (50 to 80 °C on the case). PLA brackets creep: use PETG, ABS/ASA or metal, and keep a gap or
  standoffs between the motor face and printed parts where possible.

## Shaft connections

- D-flat shafts: put the set screw on the flat. Couplers between two shafts need the axes within ~0.1 mm;
  otherwise use a jaw or beam coupling.
- Pulleys and gears on a motor shaft: keep them close to the face to limit the overhung load.

## Hobby servos

- Mount through the tabs (SG90: Ø2 holes 27.8 mm apart; MG996R: two Ø4.5 per tab, 49.5 × 10 mm) or clamp the case.
- The output spline sits off-centre: design linkages from the spline axis, not the case centre.
- Leave room for the cable exit and the horn's sweep.

## Small geared motors

- 28BYJ-48: two Ø4.2 ears 35 mm apart; the shaft is 8 mm off the can centre.
- N20: clamp the gearbox or use its two M1.6 face holes, 9 mm apart; support the can, it is not a mount.
