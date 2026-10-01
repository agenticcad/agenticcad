---
title: Belt drives and lead screws
category: transmission
tags: [belt, gt2, pulley, timing belt, tension, lead screw, t8, linear motion]
summary: GT2 pulley and belt sizing, tensioning and idlers, and when to use a lead screw instead, with typical numbers
related: [gt2_pulley, lead_screw_t8, rigid_coupler, nema_stepper, motors/mounting-motors-and-servos]
---
## GT2 belts

- 2 mm pitch; 6 mm wide for light axes, 9 or 10 mm for heavier. Pulley pitch diameter = teeth × 2 / π
  (20T: 12.73 mm). Belt travel per revolution = teeth × 2 mm (20T: 40 mm).
- Travel per step (1.8° motor, 16 microsteps, 20T): 40 / 3200 = 0.0125 mm.
- Keep at least 6 teeth in mesh: wrap angle over 90° on small pulleys.
- Idlers: toothed where the belt's teeth touch, smooth where its back touches; flanged, the same diameter as
  the pulley or larger.

## Tension

- Belt tension by a slotted motor mount or a screw tensioner at one end. Aim for a low "twang"; overtension
  loads bearings and motor shafts.
- Belt ends clamp in a toothed slot: 20 mm of engagement is plenty.

## Lead screws

- T8 lead screws: 8 mm lead (2 mm pitch, 4 starts), so 8 mm per turn; use a flexible or rigid coupler to the motor.
- Constrain the screw at one end only (bearing at the motor or at the far end), not both, unless both are
  perfectly aligned.
- Lead screws are self-locking-ish and good for Z axes; belts are faster for X/Y.
