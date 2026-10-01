---
title: ISO metric thread table
category: reference
tags: [thread, metric, pitch, tap drill, clearance hole, iso 261, iso 273]
summary: Coarse and fine pitches, tap drills and ISO 273 clearance holes for M1.6 to M24
related: [fasteners/choosing-fasteners, socket_screw, hex_bolt]
---
| Size | Coarse pitch | Tap drill | Fine pitch (tap drill) | Clearance fine / medium / coarse |
|---|---|---|---|---|
| M1.6 | 0.35 | 1.25 | | 1.7 / 1.8 / 2.0 |
| M2 | 0.4 | 1.6 | | 2.2 / 2.4 / 2.6 |
| M2.5 | 0.45 | 2.05 | | 2.7 / 2.9 / 3.1 |
| M3 | 0.5 | 2.5 | | 3.2 / 3.4 / 3.6 |
| M4 | 0.7 | 3.3 | | 4.3 / 4.5 / 4.8 |
| M5 | 0.8 | 4.2 | | 5.3 / 5.5 / 5.8 |
| M6 | 1.0 | 5.0 | 0.75 (5.2) | 6.4 / 6.6 / 7.0 |
| M8 | 1.25 | 6.8 | 1.0 (7.0) | 8.4 / 9.0 / 10.0 |
| M10 | 1.5 | 8.5 | 1.25 (8.8) | 10.5 / 11.0 / 12.0 |
| M12 | 1.75 | 10.2 | 1.5 (10.5) | 13.0 / 13.5 / 14.5 |
| M16 | 2.0 | 14.0 | 1.5 (14.5) | 17.0 / 17.5 / 18.5 |
| M20 | 2.5 | 17.5 | 1.5 (18.5) | 21.0 / 22.0 / 24.0 |
| M24 | 3.0 | 21.0 | 2.0 (22.0) | 25.0 / 26.0 / 28.0 |

In scripts: `iso("M4")` gives major, pitch, tap drill and clearance; `tap(part, "M4", at=..., depth=...)` cuts and
registers a tapped hole; UNC/UNF sizes (#4-40 to 1-8) work the same way.
