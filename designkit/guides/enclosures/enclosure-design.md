---
title: Enclosure design for 3D printing
category: enclosures
tags: [enclosure, box, housing, wall, boss, rib, snap fit, lid, seal]
summary: Wall thickness, bosses and ribs, lids and fasteners, snap fits, openings and sealing for printed enclosures
related: [enclosure, screw_boss, snap_fit_hook, vent_slots_cutter, heat_set_insert, electronics/pcb-mounting-and-venting]
---
## Walls

- 1.6 to 2.4 mm walls for printed boxes (multiples of the line width); 2.5 to 3 mm where screws bite.
- Fillet outside vertical corners (r 2 to 5 mm); chamfer the bottom edge instead of filleting (it prints cleanly).
- Ribs 0.6 to 0.8 × the wall thickness stiffen large flat walls.

## Bosses

- Heat-set inserts: boss outer Ø ≈ 2 to 2.5 × the insert hole; tie the boss to the wall or add gussets.
- Self-tapping screws: pilot ≈ 0.85 × screw diameter, boss Ø ≈ 2.5 × screw diameter.

## Lids

- A locating lip (1 to 2 mm tall, 0.2 mm clearance per side) keeps the lid aligned and hides the joint.
- Four screws into inserts for anything opened repeatedly; snap fits for quick access.

## Snap fits

- Cantilever hooks: keep strain under about 2 % for PLA/PETG (5 % for nylon). Overhang ≈ 0.67 × strain × L² / t.
- Print hooks with the beam along the layers (lying flat or standing so layers don't split at the root);
  radius the root.

## Openings and sealing

- Openings in vertical walls: flat-top or teardrop shapes print without support.
- Gaskets: a groove for a 1.5 to 2 mm cord, or a printed TPU gasket; add screws every 50 to 80 mm for an even seal.
