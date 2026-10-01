---
title: Material properties and densities
category: tolerances
tags: [material, density, strength, modulus, aluminium, steel, pla, petg, abs, nylon, mass]
summary: Density, stiffness, strength and temperature limits of common metals and plastics, including 3D printing filaments
related: [manufacturing/cnc-machining, manufacturing/fdm-3d-printing, manufacturing/injection-moulding]
---
Use `mass_properties` with a density to get weight. Typical values (datasheets vary by grade and process):

## Metals

| Material | Density g/cm³ | E GPa | Yield MPa | Notes |
|---|---|---|---|---|
| Aluminium 6061-T6 | 2.70 | 69 | 276 | General machining; cracks when bent tight |
| Aluminium 7075-T6 | 2.81 | 72 | 503 | High strength, poor weldability |
| Aluminium 5052-H32 | 2.68 | 70 | 193 | Sheet that bends well |
| Mild steel (S235/1018) | 7.85 | 200 | 235 to 370 | Cheap, welds, rusts |
| Stainless 304 | 8.00 | 193 | 215 | Corrosion resistant, work-hardens |
| Brass C360 | 8.50 | 97 | 125 to 310 | Machines beautifully |
| Titanium Ti-6Al-4V | 4.43 | 114 | 880 | Light and strong, hard to machine |

## Plastics

| Material | Density | E GPa | Service temp | Print temp | Notes |
|---|---|---|---|---|---|
| PLA | 1.24 | 3.5 | ~55 °C | 190 to 220 °C | Stiff, brittle, creeps in warm places |
| PETG | 1.27 | 2.1 | ~70 °C | 230 to 250 °C | Tough, good layer adhesion |
| ABS | 1.04 | 2.2 | ~90 °C | 230 to 260 °C | Needs an enclosure to print |
| ASA | 1.07 | 2.2 | ~90 °C | 240 to 260 °C | ABS with UV resistance |
| Nylon PA12 / PA6 | 1.01 / 1.14 | 1.5 to 2.5 | ~100 °C | 240 to 280 °C | Tough, wear resistant, absorbs water |
| Polycarbonate | 1.20 | 2.3 | ~120 °C | 260 to 300 °C | Very tough, heat resistant |
| TPU 95A | 1.21 | flexible | ~80 °C | 210 to 230 °C | Gaskets, bumpers |
| POM (Delrin) | 1.41 | 2.8 | ~100 °C | machined | Low friction, bushings and gears |
| Acrylic (PMMA) | 1.18 | 3.0 | ~80 °C | laser cut | Clear, brittle |
