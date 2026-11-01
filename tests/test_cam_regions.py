"""Pocket regions that are free of the part through their whole band, and gouge detection on walls inside the part
(2026-11-02, the boat latch: one section's holes pocketed over a band cut its thrust boss and bored its countersinks
straight down; the simulation flagged single cells along the cup wall the relief pocket correctly ran against)."""
import numpy as np
import pytest

import cad_kernel as ck
import cam_kernel as cam
import cam_sim

# a 40 x 40 x 10 block with a Ø24 recess 5 deep, a Ø8 boss rising 2 mm from its floor, and a countersunk through hole
PART = """from build123d import *
part = Box(40, 40, 10, align=(Align.CENTER, Align.CENTER, Align.MIN))
part -= Pos(0, 0, 5) * Cylinder(12, 6, align=(Align.CENTER, Align.CENTER, Align.MIN))
part += Pos(0, 0, 5) * Cylinder(4, 2, align=(Align.CENTER, Align.CENTER, Align.MIN))
part -= Pos(8, 0, 0) * Cylinder(1.5, 20)
part -= Pos(8, 0, 2) * Cone(1.5, 3.5, 3.01, align=(Align.CENTER, Align.CENTER, Align.MIN))
result = part
"""


@pytest.fixture(scope="module")
def part():
    return ck.run_script(PART).shape


def test_free_region_keeps_islands_and_what_changes_inside_the_band(part):
    recess = cam.circle(0, 0, 24)
    one_level = cam.section(part, 6.5)
    holes_at_one_level = cam.unary_union([cam.Polygon(r) for p in one_level.geoms for r in p.interiors])
    # the recess band 9 -> 5: the boss (top at 7) stands inside it
    free = cam.free_region(part, 10, 5, within=recess)
    assert free.area == pytest.approx(np.pi * (12 ** 2 - 4 ** 2), rel=0.01)
    assert not free.intersects(cam.circle(0, 0, 7.8))
    # the countersunk hole below the floor: Ø7 at the floor, Ø3 from z 2 down. A section at 4 gives the cone's width there;
    # the band 5 -> 0 is free only where the hole is free all the way down (Ø3)
    hole = cam.free_region(part, 5, 0, within=cam.circle(8, 0, 10))
    assert hole.area == pytest.approx(np.pi * 1.5 ** 2, rel=0.03)
    wide = cam.section(part, 4.0)
    cone = cam.unary_union([cam.Polygon(r) for p in wide.geoms for r in p.interiors]).intersection(cam.circle(8, 0, 10))
    assert cone.area > 2 * hole.area                                   # what pocketing one section would have bored
    assert holes_at_one_level.covers(cam.circle(0, 0, 7.8))            # and one section's holes contain the boss
    assert "free_region" in cam.PUBLIC_API


def test_a_cut_against_a_wall_inside_the_part_is_not_a_gouge():
    n = 12
    top = np.full((n, n), 10.0)
    bot = np.zeros((n, n)); bot[:, 6:] = 6.0                          # the part's underside steps from 0 to 6 at column 6
    pmaps = (top, bot)
    zhi = np.full((n, n), 10.0)
    zlo = np.zeros((n, n)); zlo[:, 6:] = 6.0
    zlo[:, 5] = 6.0                                                    # the cut reaches the wall's cell from the 6 side
    gouge, depth, _, _ = cam_sim._gouge_z(pmaps, zhi, zlo, 0.05, True)
    assert not gouge.any()
    zlo[3:8, 1:5] = 6.0                                                # a real cut, 4 cells into the thick side
    gouge, depth, _, _ = cam_sim._gouge_z(pmaps, zhi, zlo, 0.05, True)
    assert gouge[4:7, 2:4].all() and depth.max() == pytest.approx(6.0)
