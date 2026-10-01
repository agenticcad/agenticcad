---
title: Linkages and cams
category: mechanisms
tags: [linkage, four bar, grashof, crank, rocker, cam, follower, pressure angle]
summary: Four-bar linkage rules (Grashof, transmission angle) and cam design (motion laws, pressure angle, base circle)
related: [link_bar, disc_cam, hinge, dowel_pin]
---
## Four-bar linkages

- Grashof: with the shortest link s, longest l and the other two p, q, a link can rotate fully only if s + l ≤ p + q.
  If the shortest link is the crank (next to the frame), you get a crank-rocker.
- Keep the transmission angle (between coupler and output link) between 40° and 140°; near 0° or 180° the
  mechanism jams.
- Pivots: shoulder screws or dowel pins in reamed holes; printed parts want a metal pin in a 0.2 mm clearance hole.

## Cams

- Motion laws: simple harmonic (smooth velocity, acceleration jumps at the ends), cycloidal (smooth acceleration,
  best for speed), constant velocity only with blended ends.
- Pressure angle between the follower's path and the profile normal: keep under 30° for translating followers
  (45° for swinging). A larger base circle lowers it.
- Roller followers: the cam profile is the pitch curve offset inward by the roller radius; the concave radius of the
  profile must exceed the roller radius (`kit.disc_cam(..., follower_radius=...)`).
- Hold the follower on the cam with a spring sized for the maximum deceleration.
