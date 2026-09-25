"""Involute gear helpers available to scripts (spur_gear, involute_gear_profile, gear_dims, gear_centre_distance)."""
import math

import pytest

import cad_kernel as ck
import gears


def test_dims_and_centre_distance():
    d = gears.gear_dims(2, 20)
    assert (d["pitch_d"], d["outside_d"], d["root_d"]) == (40, 44, 35) and abs(d["base_d"] - 40 * math.cos(math.radians(20))) < 1e-9
    assert gears.gear_centre_distance(2, 20, 40) == 60
    with pytest.raises(gears.GearError):
        gears.gear_dims(2, 3)
    with pytest.raises(gears.GearError):
        gears.gear_dims(0, 20)


def test_profile_is_a_closed_valid_face_with_the_right_tooth_count():
    f = gears.involute_gear_profile(2, 20)
    assert f.is_valid and math.pi * 17.5 ** 2 < f.area < math.pi * 22 ** 2         # between root and outside circles
    # radial sampling: the outline crosses the pitch circle 2*teeth times (one entry and one exit per tooth)
    verts = sorted((math.atan2(v.Y, v.X) % (2 * math.pi), math.hypot(v.X, v.Y)) for v in f.vertices())
    above = [r > 20 for _, r in verts]
    crossings = sum(1 for a, b in zip(above, above[1:] + above[:1]) if a != b)
    assert crossings == 40
    small = gears.involute_gear_profile(1.5, 8)          # root below the base circle: radial flank segment path
    assert small.is_valid and small.area > 0


def test_spur_gear_solid_bore_hub_keyway_and_meshing_pair():
    m = ck.run_script("result = {'G': spur_gear(2, 20, 10, bore=8)}", "draft")
    b = m.bodies[0]
    assert b.bbox_min == pytest.approx((-22, -22, 0), abs=0.01) and b.bbox_max == pytest.approx((22, 22, 10), abs=0.01)
    assert any(abs(f.radius - 4) < 1e-6 for f in m.faces if f.kind == "CYLINDER")       # the bore
    m2 = ck.run_script("a = spur_gear(2, 20, 10, bore=8, hub_d=20, hub_h=6, keyway=(3, 1.5))\n"
                       "b = Pos(gear_centre_distance(2, 20, 12), 0, 0) * Rot(0, 0, 180 / 12) * spur_gear(2, 12, 10, bore=5)\n"
                       "result = {'A': a, 'B': b}", "draft")
    a, bb = m2.bodies
    assert a.bbox_max[2] == pytest.approx(16, abs=0.01)                               # hub on top
    assert bb.bbox_min[0] == pytest.approx(32 - 14, abs=0.5)                          # centre 32, outside radius 14
    assert m2.shape.volume == pytest.approx(a.shape.volume + bb.shape.volume, rel=1e-6)   # teeth interleave, no overlap
    with pytest.raises(gears.GearError):
        gears.spur_gear(2, 20, 10, bore=40)
    with pytest.raises(gears.GearError):
        gears.spur_gear(2, 20, 10, hub_d=40, hub_h=5)


def test_disconnected_edges_hint_and_no_builtin_shell(tmp_path):
    """The classic hand-rolled-profile failure gets a hint pointing at the helpers; the agent options expose no
    built-in Bash/Edit tools (only the web pair when enabled)."""
    import asyncio
    from agent import CadAgent
    from tests.test_agent_ops import Recorder
    ws = tmp_path / "ws"
    for sub in ("exports", "history", "designs", "imports", "images", "drawings"):
        (ws / sub).mkdir(parents=True)
    a = CadAgent(ws, Recorder().emit, None); a.quality = "draft"; a._make_server()
    r = asyncio.run(a.tool_handlers["build_model"]({"code": "e = [Line((0, 0), (10, 0)), Line((10, 0.5), (0, 10))]\nresult = Face(Wire(e))"}))
    assert r["is_error"] and "disconnected" in r["content"][0]["text"].lower() and "spur_gear" in r["content"][0]["text"]
    import inspect, agent as agent_mod
    src = inspect.getsource(agent_mod.CadAgent.start)
    assert "tools=builtin" in src and 'builtin: list[str] = []' in src
