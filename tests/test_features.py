"""Pattern / mirror / pipe helpers, the ribbon's feature ops (mirror, pattern, revolve, loft, sweep) and the
interference check."""
import math

import pytest

import analysis
import cad_kernel as ck
import features as F
from conftest import close
from test_agent_ops import ag, build  # noqa: F401  (fixture + helper)

PLATE_PEG = ('plate = Box(60, 40, 6)\npeg = Pos(20, 0, -5) * Cylinder(3, 10, align=(Align.CENTER, Align.CENTER, Align.MIN))\n'
             'result = {"Plate": plate, "Peg": peg}\n')


def test_pattern_and_mirror_helpers():
    import build123d as b
    cyl = b.Pos(20, 0, 0) * b.Cylinder(3, 10)
    assert close(F.pattern_circular(cyl, 6).volume, 6 * math.pi * 9 * 10, 1e-6)
    part = F.pattern_circular(cyl, 3, angle=90)                     # partial arc: last copy at the angle
    bb = part.bounding_box()
    assert close(bb.max.Y, 23, 1e-6) and close(bb.min.X, -3, 1e-6)
    grid = F.pattern_linear(b.Box(5, 5, 5), (1, 0, 0), 4, 10, direction2=(0, 1, 0), count2=3, spacing2=10)
    assert close(grid.volume, 12 * 125, 1e-6)
    assert len(F.pattern_linear(b.Box(5, 5, 5), (1, 0, 0), 4, 10, separate=True, include_original=False)) == 3
    assert close(F.mirror_about(b.Pos(10, 0, 0) * b.Box(4, 4, 4), normal=(1, 0, 0)).center().X, -10, 1e-9)
    with pytest.raises(ValueError):
        F.pattern_linear(cyl, (1, 0, 0), 1, 5, include_original=False)


def test_pipe_mitres_sharp_corners():
    import build123d as b
    box = b.Box(40, 40, 10)
    path = F.path_wire([box.edges().sort_by_distance((0, 20, 5))[0], box.edges().sort_by_distance((20, 0, 5))[0]])
    assert close(F.pipe(path, 2).volume, math.pi * 80, 1e-3)                 # the full L, not half of it
    assert close(F.pipe(path, 4, wall=1).volume, math.pi * (4 - 1) * 80, 1e-2)
    with pytest.raises(ValueError, match="separate chains"):
        F.path_wire([box.edges().sort_by_distance((0, 20, 5))[0], box.edges().sort_by_distance((0, -20, -5))[0]])


def test_interference_finds_overlaps_and_labels_threads():
    m = ck.run_script('''
a = Box(20, 20, 10)
b = Pos(15, 0, 0) * Box(20, 20, 10)
c = Pos(40, 0, 0) * Box(20, 20, 10)
d = Pos(0, 0, 10) * Box(20, 20, 10)
plate = Pos(0, 40, 0) * Box(30, 30, 6)
plate = tap(plate, "M5", at=(0, 40, 3), depth=6, axis=(0, 0, -1))
bolt = Pos(0, 40, 0) * Cylinder(2.5, 12)
result = {"A": a, "B": b, "C": c, "D": d, "Plate": plate, "Bolt": bolt}
''')
    r = analysis.interference(m)
    real = [p for p in r["pairs"] if not p["thread"]]
    assert [(p["a"], p["b"]) for p in real] == [("A", "B")] and close(real[0]["volume"], 1000, 1e-3)   # D only touches A
    assert real[0]["mesh"]["indices"] and close(real[0]["center"][0], 7.5, 1e-3)
    thr = [p for p in r["pairs"] if p["thread"]]
    assert [(p["a"], p["b"]) for p in thr] == [("Plate", "Bolt")] and "M5" in thr[0]["thread"]
    s = analysis.summary(r)
    assert "INTERFERENCE: 1 overlapping pair" in s and "A × B" in s and "thread" in s
    assert analysis.interference(m, ["C", "D"])["pairs"] == []
    with pytest.raises(ck.CadError):
        analysis.interference(m, ["Nope"])


async def test_feature_ops_write_code(ag):  # noqa: F811
    await build(ag, PLATE_PEG)
    v0 = ag.model.body_by_name("Plate").shape.volume
    await ag.op_pattern("Peg", "circular", {"count": 2, "angle": 360}, mode="cut", target="Plate")
    assert [b.path for b in ag.model.bodies] == ["Plate"]                    # the peg was only a tool
    assert close(v0 - ag.model.body_by_name("Plate").shape.volume, 2 * math.pi * 9 * 6, 1e-3)
    assert "pattern_circular(" in ag.model.code
    await ag.op_mirror("Plate", [30, 0, 0], [1, 0, 0], mode="join")
    assert close(ag.model.bbox_max[0], 90, 1e-6) and len(ag.model.bodies) == 1
    plane = {"origin": [0, -40, 0], "x_dir": [1, 0, 0], "z_dir": [0, -1, 0], "label": "XZ"}
    await ag.set_sketch("ring", plane, [{"type": "rect", "cx": 20, "cy": 5, "w": 4, "h": 10, "angle": 0, "mode": "add"}])
    await ag.op_revolve("ring", [0, -40, 0], [0, 0, 1], 360, "new")
    assert close(ag.model.bodies[-1].shape.volume, math.pi * (22 ** 2 - 18 ** 2) * 10, 1e-3)
    await ag.set_sketch("s1", {"origin": [0, 80, 0], "x_dir": [1, 0, 0], "z_dir": [0, 0, 1]}, [{"type": "rect", "cx": 0, "cy": 0, "w": 20, "h": 20, "angle": 0, "mode": "add"}])
    await ag.set_sketch("s2", {"origin": [0, 80, 30], "x_dir": [1, 0, 0], "z_dir": [0, 0, 1]}, [{"type": "circle", "cx": 0, "cy": 0, "r": 5, "mode": "add"}])
    await ag.op_loft(["s1", "s2"])
    assert ag.model.bodies[-1].path == "Loft" and ag.model.bodies[-1].shape.volume > 0
    with pytest.raises(ck.CadError):
        await ag.op_loft(["s1"])


async def test_sweep_pipe_new_and_cut_without_shadowing(ag):  # noqa: F811
    await build(ag, 'plate = Box(60, 40, 6)\nresult = {"Plate": plate}\n')
    await ag.op_sweep("Plate", [[0, 20, 3], [-30, 0, 3]], diameter=4, wall=1, mode="new")
    assert close(ag.model.bodies[-1].shape.volume, math.pi * 3 * 100, 1e-3)
    await ag.op_sweep("Plate", [[0, -20, 3]], diameter=3, mode="cut", target="Plate")
    assert close(ag.model.body_by_name("Plate").shape.volume, 60 * 40 * 6 - math.pi * 2.25 * 60 / 4, 1e-3)
    await ag.op_sweep("Plate", [[0, 20, -3]], diameter=3, mode="new")          # a second pipe: `pipe` must still be the helper
    assert [b.path for b in ag.model.bodies] == ["Plate", "Pipe", "Pipe 2"]
    assert "pipe_body = pipe(" in ag.model.code
    with pytest.raises(ck.CadError):
        await ag.op_sweep("Plate", [], diameter=3)


async def test_sweep_around_a_face_outline(ag):  # noqa: F811
    await build(ag, 'plate = Box(60, 40, 6)\nresult = {"Plate": plate}\n')
    await ag.op_sweep("Plate", [], diameter=4, mode="new", face_point=[0, 0, 3])
    assert ".outer_wire()" in ag.model.code and ag.model.bodies[-1].path == "Pipe"
    assert close(ag.model.bodies[-1].shape.volume, math.pi * 4 * 200, 0.02 * math.pi * 4 * 200)   # 200 mm perimeter


async def test_interference_rebuilds_real_threads_as_draft(ag):  # noqa: F811
    await build(ag, 'plate = Box(30, 30, 6)\nplate = tap(plate, "M5", at=(0, 0, 3), depth=4, axis=(0, 0, -1), real=True)\n'
                    'bolt = Pos(0, 0, -0.5) * Cylinder(2.5, 10, align=(Align.CENTER, Align.CENTER, Align.MIN))\n'
                    'result = {"Plate": plate, "Bolt": bolt}\n')                # ends inside the 4 mm thread
    res = await ag.interference()
    assert any("plain cylinders" in n for n in res["notes"]) and [p["thread"] is not None for p in res["pairs"]] == [True]
