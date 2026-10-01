---
title: Laser and waterjet cutting
category: manufacturing
tags: [laser cutting, waterjet, kerf, sheet, acrylic, plywood, tab and slot]
summary: Kerf compensation, minimum features, tab-and-slot joints and material notes for laser-cut and waterjet parts
related: [manufacturing/sheet-metal, reference/stock-sizes, tolerances/fits-and-clearances]
---
- Kerf: 0.1 to 0.2 mm on CO₂ lasers in acrylic and plywood, 0.1 to 0.3 mm fibre lasers in metal, ~0.8 mm waterjet.
  Most shops compensate; say whether your drawing is nominal (usual) or already offset.
- Minimum hole diameter ≈ material thickness (metal); minimum web between cuts ≈ thickness.
- Tab-and-slot: slot = sheet thickness + 0.1 mm for a snug fit, tabs 2 to 3 × thickness long; check the real sheet
  thickness (plywood varies ±0.3 mm).
- Acrylic: cast cuts cleaner than extruded; avoid sharp internal corners (stress cracks), use R ≥ 0.5 mm.
- Engraving is a raster pass: slow for big areas; vector scoring is fast for thin lines.
- Export 2D profiles as DXF at 1:1 in mm, one part per closed outline, no overlapping duplicate lines.
