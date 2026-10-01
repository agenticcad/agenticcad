---
title: Designing sheet metal parts
category: manufacturing
tags: [sheet metal, bend, k-factor, bend allowance, flange, relief, press brake]
summary: Bend radius, K-factor and bend allowance, minimum flanges, hole-to-bend distances, reliefs and hems
related: [manufacturing/laser-cutting, reference/stock-sizes, tolerances/materials]
---
## Bends

- Inside bend radius ≈ 1 × thickness for mild and stainless steel; 1.5 to 2 × for 6061-T6 (which cracks: prefer 5052
  aluminium for bent parts).
- K-factor 0.33 to 0.5 (0.44 is a common default for air bending). Bend allowance
  `BA = π/180 × angle × (R + K × t)`; flat length = sum of the flange lengths to the tangent lines + BA per bend.
- Keep all bends in a part at the same radius so one tool does them.

## Features near bends

- Flange length ≥ 4 × thickness (the part has to sit across the die).
- Holes and slots ≥ 2 × thickness + bend radius from the bend line, or they stretch.
- Bend relief where a bend meets an edge: width ≥ thickness, depth ≥ bend radius + thickness.
- Holes ≥ thickness in diameter and ≥ 1 to 2 × thickness from edges.

## Other

- Hems (folded edges) need a flange ≥ 4 × t; they make safe edges and stiffen.
- PEM self-clinching nuts and studs give strong threads in thin sheet.
