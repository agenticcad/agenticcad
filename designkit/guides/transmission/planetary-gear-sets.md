---
title: Planetary gear sets
category: transmission
tags: [gear, planetary, epicyclic, ring gear, gearbox, reduction]
summary: Tooth-count rules, ratio, planet phasing and ring gears for a simple planetary stage, with a checked recipe
related: [planetary_stage, planetary_layout, ring_gear, spur_gear, transmission/spur-gears]
---
A simple planetary stage has a sun at the centre, n equal planets on a carrier, and an internal ring gear.
All three share one module and pressure angle.

## Tooth counts

- Ring: `z_ring = z_sun + 2 * z_planet` (the planets fill the gap exactly).
- Equal spacing: `(z_sun + z_ring) / n` must be a whole number, or the planets cannot all mesh at 360/n degrees.
- Planets must not touch each other: `2 * a * sin(180°/n) > planet outside diameter`, where
  `a = module * (z_sun + z_planet) / 2` is the sun-to-planet centre distance.
- Keep the sun at 12 teeth or more and the planets at 12 or more to avoid heavy undercut.

`kit.planetary_layout(m, z_sun, z_planet, n)` checks all of this and raises if the set is impossible.

## Ratio

With the ring fixed, sun in and carrier out: `ratio = 1 + z_ring / z_sun`. Examples:

| Sun | Planet | Ring | n | Ratio |
|---|---|---|---|---|
| 18 | 18 | 54 | 3 | 4:1 |
| 12 | 18 | 48 | 3 | 5:1 |
| 16 | 16 | 48 | 4 | 4:1 |
| 13 | 17 | 47 | 3 | 4.615:1 |

## Phasing (the part people get wrong)

With the sun's tooth 0 on +X, planet i at carrier angle θ must be turned about its own axis by
`θ (1 + z_sun / z_planet) + 180 − 180 / z_planet` degrees. The ring's cutter profile is turned by
`180 / z_ring` when the planet tooth count is even, and by 0 when it is odd.
`planetary_layout` returns both (`rotation` per planet, `ring_rotation`). Verified interference-free for
even and odd counts.

## Ring gear

The ring's tooth spaces are an external involute profile of `z_ring` teeth with addendum 1.25 modules and no
clearance: sketch the housing outline, subtract that profile, extrude. `kit.ring_gear(...)` does exactly this.
Open it by a little backlash (0.03 to 0.05 mm at module 0.5 to 1) so exact CAD meshes don't touch.

## Recipe

```python
st = kit.planetary_stage(0.6, 18, 18, 3, face_width=8, sun_bore=5, planet_bore=5,
                         ring_thickness=18, ring_outside=42.3, ring_square=True, ring_chamfer=3)
result = {"Stage": kit.place(st, Pos(0, 0, 13))}
```

## Around the gears

- Carrier: two plates joined by posts placed between the planets (at θ + 180/n), pins through both plates.
- Planets run on pins, with bushings or small bearings (MR63, MR85) and thrust washers each side.
- Carrier plates must clear the ring's tooth tips: plate diameter < ring tip diameter − 0.5 mm.
- Check every mesh with an interference check before calling it done.
