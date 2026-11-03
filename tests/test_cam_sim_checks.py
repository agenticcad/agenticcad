"""The simulation as a gate (boat latch job, 2026-11-02): subsets chain the earlier ops' stock, material left per body
and feature, the real metal in each tab, release tracking (held / tab-held / free) and a machine-readable verdict."""
import json

import numpy as np

import pytest

import cad_kernel as ck
import cam_kernel as cam
import cam_sim
from cam_data import Library

TWO = '''
cap = Box(20, 20, 6, align=(Align.CENTER, Align.CENTER, Align.MIN))
cap = chamfer(cap.edges().group_by(Axis.Z)[-1], 1.0)
cap = Pos(-15, 0, 0) * cap
step = Pos(15, 0, 0) * Box(16, 16, 6, align=(Align.CENTER, Align.CENTER, Align.MIN))
step = step - Pos(15, 0, 4) * Box(8, 8, 4, align=(Align.CENTER, Align.CENTER, Align.MIN))
result = {"Cap": cap, "Step": step}
'''
SQUARE = '''
sq = Box(30, 30, 6, align=(Align.CENTER, Align.CENTER, Align.MIN))
result = {"Square": chamfer(sq.edges().group_by(Axis.Z)[-1], 0.5)}
'''
RES = 0.4


def flat(stepdown=0.8):
    return cam.Tool(2, "1/8 flat", "flat", 3.175, 1, 12, rpm=12000, feed=600, plunge=150, stepdown=stepdown, stepover=0.4)


V90 = cam.Tool(4, "90 chamfer", "chamfer", 6.0, 2, 3, rpm=12000, feed=600, plunge=150, stepdown=0.3, stepover=0.2,
               angle=90, sharp=True)


@pytest.fixture(scope="module")
def z1(tmp_path_factory):
    return Library(tmp_path_factory.mktemp("lib")).machines()["Makera Z1"]


@pytest.fixture(scope="module")
def two():
    return ck.run_script(TWO)


@pytest.fixture(scope="module")
def square():
    return ck.run_script(SQUARE)


SQ = cam.rect(-15, -15, 15, 15)


def square_job(z1, back=0.0, tab_height=1.0, chamfer_first=False, tabs=4):
    """A 30 x 30 x 6 square cut from a 50 x 50 x 6 plate: optionally the back thinned around it first (a flipped setup),
    a tabbed (or untabbed) profile through, a chamfer on its top edge before or after the release. (Tabs kept low: the
    tool ramps on and off them, and tall tabs on a short outline would leave no full-depth cut between them.)"""
    stock = cam.Stock.block(50, 50, 6, bottom=0)
    top = cam.Setup(z1, stock, name="Top")
    p = cam.Program(top, name="square")
    if back:
        bot = cam.Setup(z1, stock, orient="bottom", name="Back")
        p.add(cam.pocket(bot, flat(), cam.rect(-21, -21, 21, 21).difference(SQ), z_top=0, z_bottom=-back, name="A thin the back"))
    ch = cam.chamfer_edges(top, V90, SQ, z_top=6, width=0.5, side="outside", name="Chamfer")
    if chamfer_first:
        p.add(cam.contour(top, flat(), SQ, z_top=6, z_bottom=-0.2 + tab_height, side="outside", name="Wall"))   # to the tab tops
        p.add(ch)
    p.add(cam.contour(top, flat(), SQ, z_top=6, z_bottom=-0.2, side="outside", tabs=tabs, tab_height=tab_height,
                      tab_width=6, name="Release"))
    if not chamfer_first:
        p.add(ch)
    return p


def test_a_later_setup_starts_from_the_stock_the_earlier_one_left(z1):
    """Side B alone used to start from fresh stock: the probe 'hit' the register holes side A had cut (symmetric: the
    flip mirrors Y)."""
    stock = cam.Stock.block(60, 50, 10, center_xy=(0, 0), bottom=0)
    a = cam.Setup(z1, stock, origin="stock-top-center", orient="bottom", name="A Back")
    b = cam.Setup(z1, stock, origin="stock-top-center", name="B Top")
    p = cam.Program(a)
    p.add(cam.pocket(a, flat(0.5), cam.unary_union([cam.circle(x, y, 8) for x in (-20, 20) for y in (-15, 15)]), z_top=0,
                     z_bottom=-10.2, name="A0 holes"))
    p.add(cam.probe_hole(b, -20, -15, 8, name="P1 hole"))
    p.add(cam.pocket(b, flat(), cam.circle(0, 0, 10), z_top=10, z_bottom=9, name="B1 pocket"))
    side_b = cam_sim.simulate(p, ops=[1, 2], resolution=RES)
    assert "3D probe" not in side_b.summary() and side_b.history == ["A0 holes"]
    assert side_b.stats["ops"] == 2 and side_b.stats["removed_cm3"] < 0.1     # only B1's own cut is counted
    assert "simulated first and not reported" in side_b.summary()
    fresh = cam_sim.simulate(p, ops=[1, 2], resolution=RES, history=False)
    assert "3D probe" in fresh.summary() and fresh.history == []
    assert any(e["code"] == "COLLISION" for e in fresh.verdict()["errors"])


def test_material_left_per_body_and_feature(z1, two):
    stock = cam.Stock.block(60, 40, 6, bottom=0)
    st = cam.Setup(z1, stock, name="Top")
    p = cam.Program(st)
    for x, w, name in ((-15, 10, "Cap wall"), (15, 8, "Step wall")):
        p.add(cam.contour(st, flat(), cam.rect(x - w, -w, x + w, w), z_top=6, z_bottom=3, side="outside", name=name))
    r = cam_sim.simulate(p, part=two, resolution=RES)
    by = {b["body"]: b for b in r.leftover}
    assert set(by) == {"Cap", "Step"}
    cap, step = by["Cap"], by["Step"]
    assert [f["kind"] for f in cap["features"]][0] == "sloped"                 # the 1 mm chamfer was never cut
    f = cap["features"][0]
    assert 4.9 <= f["z_lo"] < 5.4 and f["z_hi"] > 5.6 and f["max_mm"] == pytest.approx(1.0, abs=0.15)
    g = step["features"][0]                                                    # the 8 x 8 recess was never pocketed
    assert g["kind"] == "flat" and g["z_lo"] == pytest.approx(4.0, abs=0.01) and g["max_mm"] == pytest.approx(2.0, abs=0.01)
    assert g["area_mm2"] == pytest.approx(64, rel=0.25)
    s = r.summary()
    assert "Cap:" in s and "sloped faces" in s and "Step:" in s and "flat at z 4.00, +2.00 mm" in s
    assert sum(b["area_mm2"] for b in r.leftover) == pytest.approx(r.stats["left"]["area_mm2"], abs=1.0)
    w = [x for x in r.verdict()["warnings"] if x["code"] == "LEFT"]
    assert {x["body"] for x in w} == {"Cap", "Step"}


def test_tab_metal_is_measured_on_the_stock(z1, square):
    r = cam_sim.simulate(square_job(z1), part=square, resolution=RES)
    assert len(r.tabs) == 4 and all(t["op"] == "Release" and t["body"] == "Square" for t in r.tabs)
    for t in r.tabs:                                  # tab top -0.2 + 1.0 = 0.8 above the plate's underside
        assert t["metal_mm"] == pytest.approx(0.8, abs=0.06) and not t["ok"] and t["width_mm"] > 1.0
    assert "THIN TAB: Release tab 1" in r.summary()
    assert sum(e["code"] == "THIN_TAB" for e in r.verdict()["errors"]) == 4
    ok = cam_sim.simulate(square_job(z1), part=square, resolution=RES, tab_min=0.5)
    assert all(t["ok"] for t in ok.tabs) and not any(e["code"] == "THIN_TAB" for e in ok.verdict()["errors"])
    # tabs 1.6 high by Z - but a flipped setup already took 1 mm off the back there: 0.6 mm of metal is what holds it
    thick = cam_sim.simulate(square_job(z1, tab_height=1.8), ops=[0, 1], part=square, resolution=RES, tab_min=1.5)
    assert len(thick.tabs) == 4 and all(t["metal_mm"] == pytest.approx(1.6, abs=0.06) and t["ok"] for t in thick.tabs)
    thinned = cam_sim.simulate(square_job(z1, back=1.0, tab_height=1.8), ops=[1, 2], part=square, resolution=RES, tab_min=1.5)
    assert thinned.history == ["A thin the back"]
    assert len(thinned.tabs) == 4 and all(t["metal_mm"] == pytest.approx(0.6, abs=0.06) and not t["ok"] for t in thinned.tabs)


def test_release_tracking_flags_cuts_on_a_released_part(z1, square):
    after = cam_sim.simulate(square_job(z1, tab_height=1.8), part=square, resolution=RES, tab_min=1.5)
    rel = [(e["body"], e["op"], e["state"]) for e in after.release]
    assert rel == [("Square", None, "held"), ("Square", "Release", "tab-held")]
    assert len(after.violations) == 1
    v = after.violations[0]
    assert (v["op"], v["body"], v["state"], v["since"]) == ("Chamfer", "Square", "tab-held", "Release") and v["removed_mm3"] > 5
    assert "RELEASED: Chamfer cuts Square" in after.summary() and "after Release left it held only by tabs" in after.summary()
    verdict = after.verdict()
    assert not verdict["ok"] and "RELEASED" in [e["code"] for e in verdict["errors"]]
    assert "THIN_TAB" not in [e["code"] for e in verdict["errors"]]
    # the same chamfer before the release: nothing to flag
    before = cam_sim.simulate(square_job(z1, tab_height=1.8, chamfer_first=True), part=square, resolution=RES, tab_min=1.5)
    assert before.violations == []
    assert not {"RELEASED", "THIN_TAB"} & {e["code"] for e in before.verdict()["errors"]}
    assert [e["state"] for e in before.release] == ["held", "tab-held"]
    # no tabs: the profile cuts it free (a warning), and the chamfer after it cuts a free part (an error)
    free = cam_sim.simulate(square_job(z1, tabs=0), part=square, resolution=RES)
    assert [e["state"] for e in free.release] == ["held", "free"]
    assert any(w["code"] == "FREED" for w in free.verdict()["warnings"])
    assert [(v["op"], v["state"]) for v in free.violations] == [("Chamfer", "free")]
    # a subset after the release still knows the part is only on its tabs
    sub = cam_sim.simulate(square_job(z1, tab_height=1.8), ops=[1], part=square, resolution=RES)
    assert sub.history == ["Release"] and [v["op"] for v in sub.violations] == ["Chamfer"] and sub.tabs == []


def test_verdict_errors_and_payload(z1, square):
    stock = cam.Stock.block(50, 50, 6, bottom=0)
    st = cam.Setup(z1, stock, name="Top")
    p = cam.Program(st)
    p.add(cam.pocket(st, flat(), cam.rect(-4, -4, 4, 4), z_top=6, z_bottom=5, name="Gouge"))          # into the part's top
    p.add(cam.contour(st, flat(), SQ, z_top=6, z_bottom=3.5, side="outside", stepdown=2.5, name="Deep wall"))
    r = cam_sim.simulate(p, part=square, resolution=RES)
    codes = [e["code"] for e in r.verdict()["errors"]]
    assert "GOUGE" in codes and "BITE" in codes
    bite = next(e for e in r.verdict()["errors"] if e["code"] == "BITE")
    assert bite["op"] == "Deep wall" and bite["max_mm"] > 2 * bite["stepdown"]
    assert r.verdict(bite_factor=4.0)["errors"][-1]["code"] != "BITE"           # within 4x: only a warning
    # the gouge was made by an op the subset doesn't run: reported, but not this run's error
    later = cam_sim.simulate(p, ops=[1], part=square, resolution=RES)
    v = later.verdict()
    assert later.stats["gouge"]["cells"] > 0 and later.stats["gouge"]["new_cells"] == 0
    assert "GOUGE" not in [e["code"] for e in v["errors"]] and "GOUGE_BEFORE" in [w["code"] for w in v["warnings"]]
    pay = json.loads(json.dumps(r.to_payload()))
    assert pay["verdict"]["ok"] is False and isinstance(pay["leftover"], list) and "release" in pay


SHAFT = '''
shaft = Rot(0, 90, 0) * Cylinder(10, 40, align=(Align.CENTER, Align.CENTER, Align.MIN))
result = {"Shaft": shaft}
'''


def test_rotary_subset_chains_the_earlier_ops_on_the_same_mounting(tmp_path_factory):
    m = Library(tmp_path_factory.mktemp("lib4")).machines()["Makera Z1 + 4th axis"]
    shaft = ck.run_script(SHAFT)
    st = cam.Setup(m, cam.Stock.cylinder(26, 42, x0=-1), rotary=True)
    p = cam.Program(st)
    p.add(cam.rotary_rough(st, flat(), shaft.shape, stepdown=1.5, mode="rings", name="Rough"))
    p.add(cam.rotary_rough(st, flat(), shaft.shape, stepdown=1.5, mode="rings", name="Again"))
    chained = cam_sim.simulate(p, ops=[1], part=shaft, resolution=0.5, res_a=3.0)
    fresh = cam_sim.simulate(p, ops=[1], part=shaft, resolution=0.5, res_a=3.0, history=False)
    assert chained.mode == "radial" and chained.history == ["Rough"] and fresh.history == []
    assert chained.stats["removed_cm3"] < 0.2 * fresh.stats["removed_cm3"]     # the second pass finds the work done


def _stamp_with(backend, side=1, eng=False):
    """A synthetic feed run stamped by the named back end (env + forced availability), returning (zhi, zlo, eng)."""
    import os
    import cam_stamp
    old = os.environ.get("AGENTICCAD_SIM_BACKEND")
    os.environ["AGENTICCAD_SIM_BACKEND"] = backend
    cam_stamp._state["numba"] = None; cam_stamp._state["metal"] = None
    try:
        res, x0, y0, nx, ny = 0.25, -10.0, -10.0, 81, 81
        zhi = np.full((ny, nx), 5.0); zlo = np.zeros((ny, nx))
        t = np.linspace(0, 4 * np.pi, 400)
        P = np.column_stack([6 * np.cos(t), 6 * np.sin(t), 4.0 - t / 4]); P = np.vstack([P, P + [0.1, 0.0, -0.3]])
        K = np.ones(len(P), dtype=int); I = np.arange(len(P))
        e = np.full((len(P), 3), -np.inf) if eng else None
        if e is not None:
            e[:, 1] = 0.0
        tool = flat()
        cam_sim._stamp_z(zhi, zlo, P, K, I, tool, side, res, x0, y0, [], "t", e)
        return zhi, zlo, e, cam_stamp.backend()
    finally:
        if old is None:
            os.environ.pop("AGENTICCAD_SIM_BACKEND", None)
        else:
            os.environ["AGENTICCAD_SIM_BACKEND"] = old
        cam_stamp._state["numba"] = None; cam_stamp._state["metal"] = None


def test_stamp_back_ends_fall_back_to_numpy(monkeypatch):
    """Without numba / metalcompute (or when either fails) the numpy code runs: the same simulation, just slower."""
    import cam_stamp
    monkeypatch.setitem(cam_stamp._state, "numba", False)
    monkeypatch.setitem(cam_stamp._state, "metal", False)
    monkeypatch.setenv("AGENTICCAD_SIM_BACKEND", "auto")
    assert cam_stamp.backend() == "numpy" and not cam_stamp.available()["numba"]
    monkeypatch.setenv("AGENTICCAD_SIM_BACKEND", "numba")
    assert cam_stamp.backend() == "numpy"
    assert cam_stamp.stamp_run(np.zeros((2, 2)), np.zeros((2, 2)), np.zeros((1, 3)), np.ones(1, dtype=int), np.zeros(1, dtype=int), flat(), 1, 0.5, 0, 0, [], "x") is False
    monkeypatch.setenv("AGENTICCAD_SIM_BACKEND", "numpy")
    assert cam_stamp.backend() == "numpy"


def test_numba_stamp_matches_numpy_bit_for_bit():
    pytest.importorskip("numba")
    for side in (1, -1):
        ref = _stamp_with("numpy", side, eng=True)
        got = _stamp_with("numba", side, eng=True)
        assert got[3] == "numba", "numba did not take the run"
        assert np.array_equal(ref[0], got[0]) and np.array_equal(ref[1], got[1])
        m = np.isfinite(ref[2])
        assert np.array_equal(m, np.isfinite(got[2])) and np.allclose(ref[2][m], got[2][m], rtol=0, atol=1e-7)


def test_metal_stamp_matches_numpy_within_float32():
    import cam_stamp
    if not cam_stamp._metal():
        pytest.skip("no Metal here")
    for side in (1, -1):
        ref = _stamp_with("numpy", side)
        got = _stamp_with("metal", side)
        assert got[3] == "metal" and cam_stamp.stats["metal_runs"] >= 1
        assert np.array_equal(np.isfinite(ref[0]), np.isfinite(got[0]))
        assert np.abs(ref[0] - got[0]).max() < 2e-6 and np.abs(ref[1] - got[1]).max() < 2e-6
