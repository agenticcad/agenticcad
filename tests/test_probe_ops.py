"""Probe ops: the 3D probe sets or checks the work origin from inside a program (2026-11-02, the boat latch: "a probe op
at the start of setup 2" after the flip had no op to be). Posted for the Makera firmware as G38.2 / G38.3 touches,
G10 L20 and Makera's M480.9 pocket-centre routine between PROBE markers; the Run panel lets exactly those through."""
import re

import pytest

import cam_kernel as cam
import cam_sim
import machine_link as ml
from cam_data import Library

T = cam.Tool(2, "1/8 flat", "flat", 3.175, 1, 12, rpm=12000, feed=600, plunge=150, stepdown=0.5, stepover=0.4)
REG = [(-58, -53), (58, -53), (58, 53), (-58, 53)]


@pytest.fixture(scope="module")
def lib(tmp_path_factory):
    return Library(tmp_path_factory.mktemp("lib"))


@pytest.fixture(scope="module")
def setups(lib):
    m = lib.machines()["Makera Z1"]
    stock = cam.Stock.block(130, 120, 10, center_xy=(0, 0), bottom=0)
    return (cam.Setup(m, stock, origin="stock-top-center", orient="bottom", name="A Back"),
            cam.Setup(m, stock, origin="stock-top-center", name="B Top"))


def flip_program(setups, extra=()):
    a, b = setups
    pr = cam.Program(a, name="latch")
    pr.add(cam.pocket(a, T, cam.unary_union([cam.circle(x, y, 8) for x, y in REG]), z_top=0, z_bottom=-10.2, name="A0 holes"))
    pr.add(cam.probe_surface(b, 0, 0, name="P1 Z on top"))
    pr.add(cam.probe_hole(b, -58, -53, 8, name="P2 XY from hole"))
    pr.add(cam.probe_hole(b, 58, 53, 8, check=0.1, name="P3 check hole"))
    pr.add(cam.probe_edge(b, "+y", -60, 0, 7, check=0.3, name="P4 check front edge"))
    for o in extra:
        pr.add(o)
    pr.add(cam.pocket(b, T, cam.circle(0, 0, 10), z_top=10, z_bottom=9, name="B1 pocket"))
    return pr


def block(g, name):
    s = g.index(f"; PROBE BEGIN {name}")
    return g[s:g.index("; PROBE END", s)].splitlines()[1:]


def test_the_post_sets_the_origin_with_the_probe_and_never_the_spindle(setups):
    g = flip_program(setups).gcode()
    after = g[g.index("; B Top:"):]
    # the setup change says the hand zero only has to be close, then the probe is fitted (no spindle) and probes
    assert "the probe ops then set Z/X/Y" in after
    probe_part = after[after.index("M6 T9999"):after.index("M6 T2")]
    assert not re.search(r"\bM0?[34]\b", probe_part)
    assert after.index("M6 T2") < after.index("M3 S", after.index("M6 T2"))
    # Z from the top: down to 3 above it, touch up to 3 below, Z0 there (work Z0 is the stock top)
    assert block(g, "P1 Z on top") == ["G0 Z5", "G1 Z3 F300", "G38.2 Z-6 F100", "G10 L20 P0 Z0", "G0 Z5"]
    # the hole: 3 mm below its top, Makera's pocket-centre routine (gap 3 = Ø8/2 - ball 1, + search 2), then that
    # centre gets its coordinates
    p2 = block(g, "P2 XY from hole")
    assert p2[2:5] == ["G1 Z-3 F100", "M480.9 X5 Y5 Z3 D2", "G10 L20 P0 X-58 Y-53"]


def test_checks_halt_outside_the_window(setups):
    g = flip_program(setups).gcode()
    p3 = block(g, "P3 check hole")
    # each wall: G38.3 to tol short of it (a nearer wall stops it, and the G38.2 then halts: already triggered), then
    # G38.2 across the 2 tol window (a further wall: nothing touched, halt); back to the centre between walls
    assert p3[3:6] == ["G38.3 X-2.9 F100", "G38.2 X-0.2 F50", "G0 X58 Y53"]
    assert sum(1 for x in p3 if x.startswith("G38.2")) == 4 and not any(x.startswith(("G10", "M480")) for x in p3)
    assert block(g, "P4 check front edge")[2:5] == ["G38.3 Y2.7 F100", "G38.2 Y0.6 F50", "G0 Y-64"]


def test_the_run_panel_takes_only_the_posts_probe_blocks(setups):
    pr = flip_program(setups)
    g, names = pr.gcode(), [o.name for o in pr.ops]
    prep = ml.prepare_program(g, names)
    assert [(p["op"], p["sets"]) for p in prep["probes"]] == [("P1 Z on top", ["z"]), ("P2 XY from hole", ["x", "y"]),
                                                              ("P3 check hole", []), ("P4 check front edge", [])]
    assert "M480.9" in prep["text"] and "G38.3" in prep["text"]
    air = ml.prepare_program(g, names, air_cut=5)                  # an air cut touches nothing: the probes are left out
    assert all(p["skipped"] for p in air["probes"]) and not re.search(r"G38|G10|M480", air["text"])
    bad = [g.replace("G38.2 Z-6 F100", "G38.2 Z-6 X1 F100"),     # a touch on two axes
           g.replace("M6 T9999", "M6 T3"),                       # probing with a cutter in the spindle
           g.replace("G10 L20 P0 Z0", "G10 L2 P0 Z0"),           # an absolute offset write
           g.replace("; PROBE BEGIN P1 Z on top\n", "; PROBE BEGIN P1 Z on top\nM3 S1000\n"),
           g.replace("; PROBE END\n", "", 1),                    # an unclosed block swallows the next op
           g.replace("; PROBE BEGIN P1 Z on top\n", "").replace("; PROBE END\n", "", 1)]   # probing outside a block
    for b in bad:
        with pytest.raises(ml.Refused):
            ml.prepare_program(b, names)
    with pytest.raises(ml.Refused, match="spindle"):               # the spindle never runs with the probe in
        ml.prepare_program(g.replace("M6 T9999\n", "M6 T9999\nM3 S8000\n", 1), names)


def test_the_simulation_checks_every_probe_move_for_air(setups):
    _, b = setups
    ok = cam_sim.simulate(flip_program(setups)).summary()
    assert "3D probe" not in ok                                    # the register holes exist; the touches stop short
    bad = cam_sim.simulate(flip_program(setups, [cam.probe_hole(b, 30, 30, 8, check=0.1, name="PX no hole here"),
                                                 cam.probe_edge(b, "+y", -55, 0, 7, name="PY edge in the stock")])).summary()
    hits = re.findall(r"3D probe \(the job halts: probe triggered\) hits stock in (P[XY])", bad)
    assert sorted(hits) == ["PX", "PY"]


def test_what_a_probe_op_refuses_and_warns(lib, setups):
    a, b = setups
    with pytest.raises(cam.CamError, match="too small"):
        cam.probe_hole(b, 0, 0, 2.5)
    with pytest.raises(cam.CamError, match="Makera"):
        haas = cam.Setup(lib.machines()["Haas VF-2"], b.stock, origin="stock-top-center")
        cam.probe_surface(haas, 0, 0)
    with pytest.raises(cam.CamError, match="tolerance"):
        cam.probe_hole(b, 0, 0, 8, check=3.5)
    pr = cam.Program(b)
    pr.add(cam.pocket(b, T, cam.circle(0, 0, 10), z_top=10, z_bottom=9, name="B1"))
    pr.add(cam.probe_surface(b, 0, 40, name="late"))
    assert any("late: sets Z after B1" in w for w in pr.check())
    t = cam.probe_tool(b.machine)
    assert (t.number, t.diameter, t.rpm) == (9999, 2.0, 0)
