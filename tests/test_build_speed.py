"""CAM build speed and stopping (2026-11-02, the boat-latch build that ran for over half an hour): sections are reused
for the same shape across builds, they never change the shape they cut, and a Stop ends a build at its next op."""
import threading

import pytest

import cad_kernel as ck
import cam_kernel as cam
from cam_data import Library

PARTS = """from build123d import *
plate = Box(60, 40, 8, align=(Align.CENTER, Align.CENTER, Align.MIN)) - Pos(0, 0, 4) * Cylinder(8, 8)
ring = Pos(45, 0, 0) * (Cylinder(10, 6, align=(Align.CENTER, Align.CENTER, Align.MIN)) - Cylinder(6, 6, align=(Align.CENTER, Align.CENTER, Align.MIN)))
ring = fillet(ring.edges().group_by(Axis.Z)[-1], 1.0)
result = {"plate": plate, "ring": ring}
"""


@pytest.fixture(scope="module")
def model():
    return ck.run_script(PARTS)


@pytest.fixture(scope="module")
def setup(tmp_path_factory):
    m = Library(tmp_path_factory.mktemp("lib")).machines()["Makera Z1"]
    return cam.Setup(m, cam.Stock.block(100, 50, 8, center_xy=(20, 0), bottom=0), origin="stock-top-center")


def test_the_part_and_its_views_keep_their_identity(model, setup):
    assert model.shape is model.shape                              # one compound, not a new one per access
    assert setup.view(model.shape) is setup.view(model.shape)
    flipped = cam.Setup(setup.machine, setup.stock, origin="stock-top-center", orient="bottom")
    assert flipped.view(model.shape) is not setup.view(model.shape)


def test_sections_are_reused_and_repeat_exactly(model, setup):
    cam.clear_caches()
    v = setup.view(model.shape)
    a = cam.section(v, 4.0)
    assert cam.section(v, 4.0) is a and len(cam._SECTION_CACHE) == 1
    # the uncached section gives the same result every time (OCCT's booleans run non-destructively: a slice of the
    # boat latch's lift ring came out once and then never again, because the first one changed the ring's tolerances)
    for z in (0.5, 4.0, 5.5, 5.95, 7.9):
        first = cam._section(v, z, 0.02)
        assert all(cam._section(v, z, 0.02).equals_exact(first, 0) for _ in range(3))
    assert cam.section(v, 4.0).area == pytest.approx(60 * 40 - 3.14159 * 64 + 3.14159 * (100 - 36), rel=1e-3)


def test_stop_ends_a_build_at_the_next_op(model, setup):
    t = cam.Tool(2, "3.175 flat", "flat", 3.175, 2, 12, rpm=12000, feed=600, plunge=150, stepdown=0.8, stepover=0.4)
    ev = threading.Event()
    tok = cam.STOP.set(ev)
    try:
        cam.pocket(setup, t, cam.circle(0, 0, 7.9), z_top=0, z_bottom=-1)          # runs
        ev.set()
        with pytest.raises(cam.BuildStopped):
            cam.pocket(setup, t, cam.circle(0, 0, 7.9), z_top=0, z_bottom=-1)
        with pytest.raises(cam.BuildStopped):
            cam.section(setup.view(model.shape), 3.0)
    finally:
        cam.STOP.reset(tok)
    cam.pocket(setup, t, cam.circle(0, 0, 7.9), z_top=0, z_bottom=-1)              # no build running: no stop


def test_clearance_leaves_the_bodies_as_they_were(model):
    r = ck.clearance(model, "plate", "ring")
    assert r["status"] in ("clear", "touching", "INTERFERING")
    before = [b.volume for b in model.bodies]
    ck.clearance(model, "plate", "ring")
    assert [b.volume for b in model.bodies] == before
