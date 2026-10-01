---
title: build123d pitfalls and assembly habits
category: modelling
tags: [build123d, builder, sketch, locations, pos, boolean, assembly, interference, performance]
summary: Mistakes that silently produce wrong geometry in build123d, and habits for big multi-part assemblies
related: [fasteners/choosing-fasteners, transmission/planetary-gear-sets]
---
## Silent mistakes

1. **`Pos(x, y) * Circle(r, mode=Mode.SUBTRACT)` inside a BuildSketch cuts at the origin.** In builder mode the
   object is applied to the sketch when it is created, before `Pos` moves it. Use
   `with Locations((x, y)): Circle(r, mode=Mode.SUBTRACT)` (or build in algebra mode outside the builder).
   The same goes for `Pos(...) * Box(...)` inside BuildPart.
2. **Helper functions that create builder objects inside your builder add to it.** Kit components and the
   gear/thread helpers are safe; your own helper functions that call `Box()`, `Cylinder()` or `extrude()` while a
   `with BuildPart()` block is open will add into that part. Call them outside the block, or build with
   algebra (`a = Box(...) - Cylinder(...)`).
3. **Exactly touching gears or threads intersect in checks.** Give gears backlash; accept a few mm³ where real
   threads mate.
4. **`a & b` can return an empty/null shape** when nothing overlaps; guard interference checks with try/except.

## Assemblies

- One body per real part, named like a parts list (`HousingScrew1`, `Planet2`), grouped in components
  (`{"Motor": {...}, "Gearhead": {...}}`). Bearings and other kit assemblies drop in as sub-components.
- Put every key dimension in a named variable at the top so the Parameters card can drive it.
- Build in stages and rebuild after each: frame, main parts, then fasteners, then details.
- Check interference between every pair of bodies whose boxes overlap before reporting done.

## Performance

- Real threads are the slowest thing in most models (seconds per screw). Model them only where asked; reuse the
  same call for identical screws (built once); give long screws a partial thread (ISO: 2d + 12 mm).
- While the agent is working, `real=True` threads build plain ("draft threads") and the app models the real
  helices once at the end of the turn. A 100-part gearhead rebuilds in about 15 s instead of 70 s this way.
- Tapped holes with `real=True` cost about a second each.
- Prefer one sketch + extrude over many small booleans for toothed or patterned outlines.
