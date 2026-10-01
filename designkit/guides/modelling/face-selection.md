---
title: Selecting faces and edges robustly
category: modelling
tags: [build123d, face, edge, selection, sort_by_distance, filter_by, group_by, plane, sketch on face]
summary: Selecting faces and edges by position and direction (not ids), sketching on a face, and editing selected geometry
related: [modelling/builder-pitfalls]
---
Face and edge ids change on every rebuild, so select geometry by where it is and which way it faces.

- Nearest face to a point: `part.faces().sort_by_distance((x, y, z))[0]` (the [Selected geometry] block gives
  the clicked face's centre).
- Faces pointing a way: `part.faces().filter_by(Axis.Z)` (normal along ±Z), then `.sort_by(Axis.Z)[-1]` for the top.
- Edges by direction and position: `part.edges().filter_by(Axis.Z).group_by(Axis.X)[-1]` (vertical edges at max X).
- Circular edges of a radius: `[e for e in part.edges() if e.geom_type.name == "CIRCLE" and abs(e.radius - r) < 1e-6]`.
- Sketch on a face: `Plane(face)` gives its plane (origin at the face centre, normal outwards); build the sketch on
  it and extrude with a negative amount to cut into the part.
- After an edit, check the change with inspect_model or a screenshot: a selector that picked the wrong face fails
  silently.
