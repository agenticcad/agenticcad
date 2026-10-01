---
title: Hinges and print-in-place joints
category: mechanisms
tags: [hinge, knuckle, pin, print in place, living hinge, clearance]
summary: Knuckle hinges, pin sizing, print-in-place clearances and orientation, and living hinges in PP and PETG
related: [hinge, link_bar, mechanisms/linkages-and-cams, manufacturing/fdm-3d-printing]
---
## Knuckle hinges

- Use an odd number of knuckles (3 or 5) so both leaves carry load symmetrically.
- Pin diameter about a third of the knuckle's outer diameter; knuckle wall at least 1.2 mm printed, 0.8 mm metal.
- Machined: pin H7/g6 in the knuckles. Printed: 0.2 to 0.3 mm diametral clearance on the pin, 0.2 mm axial gaps.
- A stop face on each leaf limits the opening angle; countersink the screw holes so heads sit flush.

## Print-in-place

- Gaps of 0.3 to 0.5 mm between moving parts on FDM (0.2 on a very well tuned printer).
- Print with the hinge axis horizontal and parallel to the bed; chamfer the knuckle undersides at 45° so they
  print without support.
- Break the joint free by working it gently after printing.

## Living hinges

- Polypropylene is the only common material that survives thousands of flexes: 0.3 to 0.5 mm thick, 1 to 2 mm
  long, flexed while still warm after moulding/printing.
- PETG and nylon survive tens to hundreds of cycles at 0.4 to 0.6 mm; PLA cracks quickly.
