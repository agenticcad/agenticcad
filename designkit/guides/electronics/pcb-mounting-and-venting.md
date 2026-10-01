---
title: Mounting boards, venting and cable entry
category: electronics
tags: [pcb, board, mounting, standoff, enclosure, vent, cooling, connector, cable]
summary: How to mount PCBs in enclosures, clearances for connectors and hands, venting and fan sizing, and cable entry
related: [arduino_uno, raspberry_pi_4, pi_pico, standoff, fan, fan_grille_cutter, vent_slots_cutter, enclosures/enclosure-design]
---
## Mounting

- Use the board's mounting holes on standoffs (M2.5 for Raspberry Pi, M3 for Arduino). 3 to 6 mm of standoff
  keeps solder joints clear of the floor.
- Printed posts with heat-set inserts or self-tapping holes work too; model the post under each hole at the
  datasheet coordinate (kit boards put their lower-left corner at the origin, so holes are at datasheet coordinates).
- Leave 1 mm around the PCB outline and 0.5 mm over the tallest part to the lid.

## Connectors

- Cut-outs: the connector's envelope + 0.5 mm each side (1 mm for printed walls); round corners.
- Plugs are bigger than sockets: USB-A plugs need about 12 × 5 mm clear, USB-C 9 × 3.5 mm, HDMI cables 15 × 7 mm
  around the opening.
- Put boards against the wall the connectors face so the connector reaches the opening.

## Heat and venting

- A Raspberry Pi 4 or 5 under load needs airflow: a 30 or 40 mm fan, or a heatsink and vents.
- Vents low on one side and high on the other drive convection; slot width 2 to 3 mm keeps fingers out.
- Fan airflow scales with size: prefer a slower, larger fan for noise. Keep 5 mm or more between the fan and an
  obstruction.

## Cable entry

- Cable glands (PG7 for 3 to 6.5 mm cables, hole Ø12.5) for sealed boxes; grommets or strain-relief slots otherwise.
- Route cables away from the fan intake and moving parts.
