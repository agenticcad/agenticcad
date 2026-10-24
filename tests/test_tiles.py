"""Tiling: pattern() in the design (one part, copies sharing its geometry) and Setup(tiles=...) in CAM (ops computed
once, run at every tile, all tiles with one tool before the next; G54 with offsets on a Makera, G54.. per tile
elsewhere on request)."""
import re

import pytest

import cad_kernel as ck
import cam_kernel as cam
import cam_sim
from cam_data import Library

PART = """from build123d import *
part = Box(25, 40, 3, align=(Align.MIN, Align.MIN, Align.MAX)) - Pos(12.5, 20, -1) * Cylinder(6, 3)
result = {"badge": pattern(part, cols=4, rows=2, pitch=(35, 50))}
"""


@pytest.fixture(scope="module")
def machines(tmp_path_factory):
    return Library(tmp_path_factory.mktemp("lib")).machines()


@pytest.fixture(scope="module")
def model():
    return ck.run_script(PART, "normal")


def flat():
    return cam.Tool(2, "3.175 flat", "flat", 3.175, 2, 12, rpm=12000, feed=600, plunge=150, stepdown=0.8, stepover=0.4)


def test_pattern_makes_copies_that_share_the_part(model):
    assert [b.path for b in model.bodies] == ["badge"] + [f"badge@{k}" for k in range(2, 9)]
    assert model.patterns == [{"base": "badge", "instances": [b.path for b in model.bodies],
                               "offsets": [[c * 35.0, r * 50.0, 0.0] for r in range(2) for c in range(4)]}]
    b0, b6 = model.mesh["bodies"][0], model.mesh["bodies"][6]
    assert b6.get("patternOf") == "badge" and b6["indices"] == b0["indices"]           # the base's triangles, moved
    assert [round(b6["positions"][i] - b0["positions"][i], 6) for i in range(3)] == [70.0, 50.0, 0.0]
    assert b6["faceRanges"][0][0] == model.bodies[6].face_ids[0]                         # its own face ids
    assert model.volume == pytest.approx(8 * model.bodies[0].volume)
    assert model.bodies[6].bbox_min[0] == pytest.approx(70.0, abs=1e-6)
    # offsets form, and bad input
    m2 = ck.run_script("from build123d import *\nresult = pattern(Box(5, 5, 5), offsets=[(0, 0), (10, 0, 2)])\n")
    assert len(m2.bodies) == 2 and m2.bodies[1].bbox_min[2] == pytest.approx(-0.5)
    with pytest.raises(ck.CadError, match="same place"):
        ck.run_script("from build123d import *\nresult = pattern(Box(5, 5, 5), offsets=[(0, 0), (0, 0)])\n")
    assert cam.tiles_of(model, "badge")[5] == (35.0, 50.0, 0.0) and cam.tiles_of(model) == cam.tiles_of(model, "badge")
    with pytest.raises(cam.CamError, match="no pattern"):
        cam.tiles_of(ck.run_script("from build123d import *\nresult = Box(5, 5, 5)\n"))


def _prog(machines, model, mname="Makera Z1", **kw):
    m = machines[mname]
    stock = cam.Stock.block(160, 110, 4, center_xy=(65, 45), bottom=-3)
    st = cam.Setup(m, stock, origin="stock-top-left", tiles=cam.tiles_of(model, "badge"), **kw)
    p = cam.Program(st, name="sheet")
    t = flat()
    face_all = cam.face(st, t, z_top=1, z_bottom=0, region=cam.rect(-5, -5, 135, 95))
    p.add(face_all, tiles=False)                                       # over the whole plate: once
    p.add(cam.pocket(st, t, cam.circle(12.5, 20, 6), z_top=0, z_bottom=-1))
    p.add(cam.contour(st, t, cam.rect(0, 0, 25, 40), z_top=0, z_bottom=-2.9, side="outside"))
    return p


def test_ops_are_computed_once_and_run_at_every_tile(machines, model):
    p = _prog(machines, model)
    one = sum(len(o.moves) for o in p.ops)
    g = p.gcode()
    assert [len(p.op_tiles(o)) for o in p.ops] == [1, 8, 8]
    assert g.count("M6 T2") == 1                                        # one tool: one change for all tiles
    assert len(re.findall(r"^; tile \d+/8", g, re.M)) == 16              # pocket and contour at 8 tiles each
    assert "x8 tiles" in g and "8 tiles, all in G54" in g and "G55" not in g
    # tile 2 is tile 1 shifted 35 mm in X: same cutting lines, X + 35
    blocks = re.split(r"^; tile \d+/8.*$", g.split("; Contour")[0].split("; Pocket")[1], flags=re.M)
    cut = lambda b: [ln for ln in b.splitlines() if ln.startswith(("G1", "G2", "G3"))]
    t1, t2 = cut(blocks[1]), cut(blocks[2])
    assert len(t1) == len(t2) > 10
    xs = lambda ls: [float(m) for ln in ls for m in re.findall(r"X(-?[\d.]+)", ln)]
    assert all(abs(b - a - 35.0) < 1e-3 for a, b in zip(xs(t1), xs(t2)))
    assert "G53 G0 Z-3" in blocks[2].splitlines()[1]                      # between tiles: up to the clearance first
    # time / bounds include the tiles; the program keeps one tile's moves
    assert sum(len(o.moves) for o in p.ops) == one
    t_one = sum(o.time_minutes(p.machine) for o in p.ops[1:])
    assert p.time_minutes() > p.ops[0].time_minutes(p.machine) + 8 * t_one * 0.99
    (x0, y0, _), (x1, y1, _) = p.bounds()
    assert x1 > 105 + 25 and y1 > 50 + 40
    s = p.summary()
    assert "tiles: 8" in s and "(8 tiles)" in s
    # the viewer gets each op's moves once plus the tile offsets
    pl = p.to_payload()
    assert pl["ops"][1]["tiles"] == 8 and len(pl["ops"][1]["tile_offsets"]) == 8 and pl["ops"][0]["tile_offsets"] is None
    assert len(pl["ops"][1]["moves"]) == len(p.ops[1].moves) and pl["setups"][0]["tiles"][3] == [105.0, 0.0, 0.0]


def test_simulation_cuts_every_tile_and_folds_stats(machines, model):
    p = _prog(machines, model)
    r = cam_sim.simulate(p, part=model, resolution=0.4, frames=2)
    assert r.stats["gouge"]["cells"] == 0
    assert [b.get("tiles") for b in r.stats["bites"]] == [None, 8, 8]
    assert [len(v["mrr"]) for v in r.per_move.values()] == [len(o.moves) for o in p.ops]
    one = cam.Program(p.setup); one.add(p.ops[0], tiles=False); one.add(p.ops[1])      # the same, one tile
    p.setup.tiles = [(0.0, 0.0, 0.0)]
    try:
        r1 = cam_sim.simulate(one, resolution=0.4, frames=2)
    finally:
        p.setup.tiles = cam.tiles_of(model, "badge")
    assert r.stats["bites"][1]["removed_mm3"] == pytest.approx(8 * r1.stats["bites"][1]["removed_mm3"], rel=0.05)


def test_tile_wcs_refused_on_makera_and_listed_elsewhere(machines, model):
    p = _prog(machines, model, tile_wcs=True)
    with pytest.raises(cam.CamError, match="not on a Makera"):
        p.gcode()
    assert any("not on a Makera" in w for w in p.check())
    grbl = next(n for n, m in machines.items() if m.post == "grbl")
    g = _prog(machines, model, mname=grbl, tile_wcs=True).gcode()
    assert ";   G59.1 = G54 + X70 Y50 Z0" in g and ";   G54: zero it as usual" in g
    assert re.findall(r"^(G5[4-9](?:\.\d)?)$", g, re.M) == ["G54", "G55", "G56", "G57", "G58", "G59", "G59.1", "G59.2"] * 2
    i = g.index("; Contour"); assert g[:i].rstrip("\n").split("\n")[-2:] == ["G0 Z15", "G54"]   # back to G54: up, then switch
    with pytest.raises(cam.CamError, match="9 work offsets"):
        cam.Setup(machines[grbl], cam.Stock.block(10, 10, 4), tiles=[(i, 0) for i in range(10)], tile_wcs=True)
    with pytest.raises(cam.CamError, match="4th-axis"):
        cam.Setup(machines["Makera Z1 + 4th axis"], cam.Stock.cylinder(20, 50), rotary=True, tiles=[(0, 0), (0, 30)])
