"""Joints, couplings, explode and appearance declared in scripts; kinematics; collisions through motion; the Joint op."""
import numpy as np
import pytest

import analysis
import cad_kernel as ck
import motion as mo
from conftest import close
from test_agent_ops import ag, build  # noqa: F401

ARM = '''
base = Box(80, 80, 5)
post = Pos(0, 25, 2.5) * Cylinder(3, 15, align=(Align.CENTER, Align.CENTER, Align.MIN))
arm = Pos(15, 0, 6) * Box(30, 6, 4, align=(Align.CENTER, Align.CENTER, Align.MIN))
tip = Pos(28, 0, 10) * Cylinder(2, 6, align=(Align.CENTER, Align.CENTER, Align.MIN))
result = {"Base": base, "Post": post, "Arm": arm, "Tip": tip}
'''


def test_joints_resolve_couple_and_chain():
    m = ck.run_script(ARM + '''
revolute("arm", ["Arm"], axis="Z", limits=(-90, 90))
revolute("tip", ["Ti*"], axis=((28, 0, 0), (0, 0, 1)), parent="arm")
couple("tip", "arm", ratio=-2, offset=10)
explode({"Post": (0, 0, 20)}, axis="Z")
appearance({"Base": "black anodised", "Tip": {"color": "#ff0000", "metalness": 0, "roughness": 0.5}})
''')
    assert [(j["name"], j["bodies"]) for j in m.motion["joints"]] == [("arm", ["Arm"]), ("tip", ["Tip"])]
    assert m.motion["drive"] == {"joint": "arm", "start": -90, "stop": 90, "seconds": 4.0}   # default: the first free joint
    v = mo.joint_values(m.motion, {"arm": 90})
    assert v == {"arm": 90, "tip": -170.0}
    M = mo.joint_matrices(m.motion, v)
    assert np.allclose((M["tip"] @ [28, 0, 10, 1])[:3], [0, 28, 10], atol=1e-9)        # the child axis rode with the arm
    assert m.explode["offsets"] == {"Post": [0, 0, 20]} and m.explode["auto"]["axis"] == "Z"
    assert m.appearance["Base"]["name"] == "black anodised" and m.appearance["Tip"]["color"] == "#ff0000"
    assert m.mesh["motion"]["joints"][0]["name"] == "arm" and "appearance" in m.mesh


@pytest.mark.parametrize("extra, msg", [
    ('revolute("a", ["Nope"])', "no body matches"),
    ('revolute("a", ["Arm"])\nrevolute("b", ["Arm"])', "is in joints"),
    ('revolute("a", ["Arm"], parent="zz")', "parent 'zz'"),
    ('revolute("a", ["Arm"])\nrevolute("b", ["Tip"])\ncouple("a", "b")\ncouple("b", "a")', "loop"),
    ('revolute("a", ["Arm"])\nrevolute("b", ["Tip"])\ncouple("b", "a")\ndrive("b")', "coupled"),
    ('appearance({"Arm": "unobtainium"})', "unknown material"),
])
def test_motion_declarations_are_validated(extra, msg):
    with pytest.raises(ck.CadError, match=msg):
        ck.run_script(ARM + extra)


def test_auto_appearance_by_name_and_nothing_declared():
    m = ck.run_script('result = {"Plate": Box(10, 10, 2), "Screw1": Cylinder(1, 8), "Coil": Box(2, 2, 2), "Blob": Sphere(2)}')
    assert m.motion is None
    assert m.appearance["Screw1"]["name"] == "black oxide" and m.appearance["Coil"]["name"] == "copper"
    assert m.appearance["Plate"]["name"] == "aluminium" and "Blob" not in m.appearance    # falls back to its tint


def test_collisions_through_motion_ignore_rest_overlap():
    m = ck.run_script(ARM + 'revolute("arm", ["Arm", "Tip"], axis="Z", limits=(0, 180))')
    r = analysis.motion_interference(m, steps=18)
    assert [(h["a"], h["b"]) for h in r["collisions"]] in ([("Arm", "Post")], [("Arm", "Post"), ("Tip", "Post")])
    hit = r["collisions"][0]
    assert 60 <= hit["first"] <= 90 and close(hit["at"], 90, 10)
    assert "COLLISIONS" in analysis.motion_summary(r)
    # the arm already sits on the base at rest (touching): that is not a collision
    assert not any("Base" in (h["a"], h["b"]) for h in r["collisions"])
    with pytest.raises(ck.CadError, match="no motion"):
        analysis.motion_interference(ck.run_script(ARM), steps=4)


async def test_joint_op_appends_code(ag):  # noqa: F811
    await build(ag, ARM)
    await ag.op_joint("arm", "revolute", ["Arm"], [0, 0, 0], [0, 0, 1], limits=[-45, 45])
    await ag.op_joint("tipslide", "slider", ["Tip"], [0, 0, 0], [0, 0, 1], parent="arm", couple_to="arm", ratio=0.1)
    assert "revolute('arm', ['Arm'], axis=((0, 0, 0), (0, 0, 1)), limits=(-45, 45))" in ag.model.code
    assert "couple('tipslide', 'arm', ratio=0.1)" in ag.model.code
    assert [j["name"] for j in ag.model.motion["joints"]] == ["arm", "tipslide"]
    with pytest.raises(ck.CadError, match="already exists"):
        await ag.op_joint("arm", "revolute", ["Post"], [0, 0, 0], [0, 0, 1])
