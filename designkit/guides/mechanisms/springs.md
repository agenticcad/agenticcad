---
title: Springs
category: mechanisms
tags: [spring, compression spring, extension spring, rate, stiffness, solid height]
summary: Spring rate, index, solid height and working range for helical compression and extension springs, with worked numbers
related: [compression_spring, extension_spring]
---
## Rate

`k = G · d⁴ / (8 · D³ · n)` with wire diameter d, mean coil diameter D = OD − d, active coils n.

| Wire | G |
|---|---|
| Music wire / spring steel | 79.3 GPa (79 300 N/mm²) |
| Stainless 302/304 | 69 GPa |
| Phosphor bronze | 41 GPa |

Example: music wire d = 1.0, OD = 10 (D = 9), n = 6: k = 79 300 × 1 / (8 × 729 × 6) = 2.27 N/mm.

## Proportions

- Spring index C = D / d between 4 and 12 (below 4 is hard to coil, above 12 tangles and buckles).
- Solid height ≈ total coils × d (ground ends slightly less). Work in 15 to 85 % of the available deflection.
- Free length over mean diameter above ~4 buckles unless guided by a rod or bore; leave 10 % diametral clearance.

## Ends and loops

- Compression: closed and ground ends sit square and load evenly; `kit.compression_spring(..., ground=True)`.
- Extension: machine loops or hooks; stress is highest at the loop bend, so keep the loop radius ≈ the coil radius.
- Model the spring at its installed length when checking clearances.
