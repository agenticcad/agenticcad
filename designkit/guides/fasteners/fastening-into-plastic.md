---
title: Fastening into plastic
category: fasteners
tags: [insert, heat-set, self-tapping, plastic, 3d printing, nut trap, threads]
summary: Heat-set inserts vs self-tapping screws vs nut traps vs printed threads, with hole sizes and boss rules
related: [heat_set_insert, insert_hole_cutter, screw_boss, fasteners/choosing-fasteners, manufacturing/fdm-3d-printing]
---
| Method | Cycles | Strength | Notes |
|---|---|---|---|
| Heat-set brass insert | Many | High | Hole per the insert maker (M3 ≈ Ø4.0); install with a soldering iron at ~220 °C for PLA |
| Captive hex nut (nut trap) | Many | High | Hex pocket = nut AF + 0.2 to 0.3 mm; needs access from the side or below |
| Self-tapping (thread-forming) screw | Few (5 to 10) | Medium | Pilot ≈ 0.85 × d; use screws for plastics |
| Printed thread | Few | Low | Only M8 and larger, or for low loads |

- Put inserts in bosses at least 1.5 mm of wall around the insert, with relief below it for displaced plastic
  (`kit.insert_hole_cutter`).
- Keep inserts perpendicular to layers where possible so pull-out loads shear across layers.
