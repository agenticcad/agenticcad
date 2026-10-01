---
title: Spur gears
category: transmission
tags: [gear, spur, involute, module, backlash]
summary: Module, pressure angle, centre distance, backlash, face width and printable sizes for involute spur gears
related: [spur_gear, gear_centre_distance, gear_dims, transmission/planetary-gear-sets]
---
## Basics

- Module m = pitch diameter / teeth. Meshing gears share m and pressure angle (20° unless told otherwise).
- Pitch diameter `m·z`, outside `m·(z + 2)`, root `m·(z − 2.5)`.
- Centre distance of two external gears: `m·(z1 + z2) / 2` (`kit.gear_centre_distance`).
- Ratio = driven teeth / driving teeth.
- Fewer than about 17 teeth at 20° undercuts; 12 is a practical minimum for small gears.

## Meshing in CAD

`spur_gear` puts tooth 0 on +X. Two gears whose centres lie on the X axis interleave when one is turned by
half a tooth pitch, `180 / z` degrees. For a general angle, use the phasing formula in the planetary guide.
Always add a little backlash (0.03 to 0.1 mm), because exact touching solids fail interference checks.

## Face width and strength

- Face width: 8 to 12 modules for metal gears, 3 to 5 mm minimum for printed gears.
- Hub: at least 1 module of rim under the roots, and more for set screws and keys.

## Choosing a module

| Use | Module |
|---|---|
| Small instruments, watch-like | 0.3 to 0.5 |
| Hobby gearboxes, printed small | 0.5 to 1.0 |
| Printed drivetrains | 1.0 to 2.0 (FDM prints m ≥ 1 cleanly) |
| Machinery | 2 and up |
