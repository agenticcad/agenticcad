"""CAM tab ▸ Add operation: a checked spec becomes one program.add(...) line at the end of cam.py, the program rebuilds
with it, and a spec that wouldn't build (or isn't valid) leaves cam.py untouched."""
from __future__ import annotations

import pytest

import cam_kernel as cam
from agent import CadAgent

PART = '''with BuildPart() as p:
    Box(40, 30, 8, align=(Align.CENTER, Align.CENTER, Align.MIN))
    with Locations((-6, 0, 8)):
        Box(12, 10, 3, align=(Align.CENTER, Align.CENTER, Align.MIN))
    with Locations((-15, 8, 8)):
        Hole(1.25)
    with Locations((12, 0, 8)):
        Box(8, 8, 3, align=(Align.CENTER, Align.CENTER, Align.MAX), mode=Mode.SUBTRACT)
result = {"Part": p.part}
'''
CAM = '''m = machines["Makera Z1"]
bb0, bb1 = model.bbox_min, model.bbox_max
stock = Stock.block(bb1[0] - bb0[0] + 10, bb1[1] - bb0[1] + 10, bb1[2] - bb0[2] + 1, top=bb1[2] + 1)
setup = Setup(m, stock, origin="stock-top-left", name="Top")
program = Program(setup, name="addop")
program.add(face(setup, tools[1], name="Face"))
'''


class Rec:
    def __init__(self):
        self.events = []

    async def emit(self, e):
        self.events.append(e)


@pytest.fixture
def ag(tmp_path):
    ws = tmp_path / "ws"
    for sub in ("exports", "history", "designs", "imports", "images", "drawings"):
        (ws / sub).mkdir(parents=True)

    async def shot(spec):
        raise RuntimeError("no browser")
    a = CadAgent(ws, Rec().emit, shot)
    a.quality = "draft"
    return a


def test_each_kind_adds_one_line_and_builds(ag):
    import asyncio

    async def go():
        await ag.build(PART, source="user")
        await ag.set_cam_code(CAM, source="user")
        floor = next(f.id for f in ag.model.faces if f.kind == "PLANE" and f.normal[2] > 0.99 and abs(f.center[2] - 5.0) < 0.01)
        specs = [dict(kind="face", tool=1, depth=0.5, name="Face"),               # a second "Face": named "Face 2"
                 dict(kind="adaptive", tool=2, leave=0.3, stepover=0.2),
                 dict(kind="pocket", tool=2, faces=[floor], name="Square pocket"),
                 dict(kind="contour", tool=1, tabs=2, name="Outline"),
                 dict(kind="drill", tool=5, dmin=2, dmax=3, name="Holes"),
                 dict(kind="parallel3d", tool=4, stepover=0.2, name="Finish")]
        for sp in specs:
            await ag.add_cam_op(sp)
        names = [o.name for o in ag.program.ops]
        assert names == ["Face", "Face 2", "Adaptive", "Square pocket", "Outline", "Holes", "Finish"], names
        assert all(o.moves for o in ag.program.ops)
        code = ag.cam_code
        assert code.count("operations added in the CAM tab") == 1 and code.count("program.add(") == 7
        assert "pocket(program.setups[0], tools[2], [face_polygon(model.get_face(f)) for f in [%d]]" % floor in code
        assert (ag.workspace / "cam.py").read_text() == code
        drill = ag.program.ops[5]
        assert len([m for m in drill.moves if m[0] == cam.PLUNGE]) == 1          # the one Ø2.5 hole
        # refused before anything is written: unknown tool, a non-flat face, an out-of-range value, a bad kind
        for bad, why in ((dict(kind="face", tool=77), "isn't in the tool library"),
                         (dict(kind="pocket", tool=2, faces=[next(f.id for f in ag.model.faces if f.kind == "CYLINDER")]), "flat face"),
                         (dict(kind="adaptive", tool=2, stepover=3), "stepover"),
                         (dict(kind="exec", tool=1), "unknown operation kind")):
            with pytest.raises(cam.CamError, match=why):
                await ag.add_cam_op(bad)
        assert ag.cam_code == code
        # a spec that is valid but doesn't build (no holes in that range): the build fails, cam.py stays as it was
        with pytest.raises(cam.CamError):
            await ag.add_cam_op(dict(kind="drill", tool=5, dmin=50, dmax=60))
        assert ag.cam_code == code and (ag.workspace / "cam.py").read_text() == code
    asyncio.run(go())
