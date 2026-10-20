"""4th-axis simulation realism: configurable rotary geometry (machine.model.rotary), the flute length as a limit on what
cuts, the tool stack against the chuck, its jaws and the tailstock in 3D, one mounting per simulation, and per-op gouge
attribution."""
import pytest

import cad_kernel as ck
import cam_kernel as cam
import cam_sim
import machine_models as mm
import machine_script
from cam_data import Library
from test_cam_setups import SHAFT, flat, ball  # noqa: F401


@pytest.fixture(scope="module")
def machines(tmp_path_factory):
    return Library(tmp_path_factory.mktemp("lib")).machines()


@pytest.fixture(scope="module")
def shaft():
    return ck.run_script(SHAFT)


def z1_record(machines, rotary_override=None):
    m = machines["Makera Z1 + 4th axis"]
    return cam.Machine(**{**m.__dict__, "model": {"rotary": rotary_override} if rotary_override else None})


def manual_op(st, tool, name, moves):
    """A hand-built op: moves = [(kind, x, y, z[, a])] in the setup frame."""
    op = cam.Op(name, "contour", tool)
    pb = cam.PathBuilder(st, tool, op)
    for mv in moves:
        kind, x, y, z = mv[:4]
        pb._add(kind, x, y, z, 0 if kind == cam.RAPID else 300, mv[4] if len(mv) > 4 else None)
    return op


# ------------------------------------------------------------------ rotary geometry overrides (machine.model.rotary)

def test_rotary_overrides_move_the_axis_and_record_the_source(machines):
    ref = mm.model_for(z1_record(machines), workspace=False)
    assert ref.rotary["axis_y"] == -10.0 and ref.rotary["axis_z"] == 45.0 and ref.rotary["source"] == "reference"
    assert ref.rotary["chuck_gap"] == 5.0 and ref.rotary["jaw_reach"] == 16.0 and ref.rotary["tail"] and ref.rotary["tail_x"] == 80.0
    assert ref.rotary["tail_cone"] == {"half_angle": 30.0, "length": 12.0} and ref.rotary["jaw_r"] > ref.rotary["tail_r"]
    assert "reference" in ref.sources["4th axis position"]
    # machine coordinates from the controller's config: rotation_offset_y 69 → machine Y −125.45, converted with the Z1
    # profile's anchor (−194.3) and bed anchor (−88) → bed Y −19.15; the live anchor (−194.45) gives −19.0
    cfg = mm.model_for(z1_record(machines, {"axis_my": -125.45, "axis_mz": -71.3}), workspace=False)
    assert cfg.rotary["axis_y"] == pytest.approx(-19.15) and cfg.rotary["axis_z"] == pytest.approx(45.0) and cfg.rotary["source"] == "config"
    assert "machine Y -125.45" in cfg.sources["4th axis position"] and cfg.sources["4th axis position"].startswith("user:")
    live = mm.model_for(z1_record(machines, {"axis_my": -125.45, "anchor1_y": -194.45, "probed": "2026-10-20"}), workspace=False)
    assert live.rotary["axis_y"] == pytest.approx(-19.0) and live.rotary["source"] == "probed" and "2026-10-20" in live.sources["4th axis position"]
    # the parts follow: the chuck node's pivot and the chuck body sit on the new axis
    piv = next(n for n in live.nodes if n.axis == "a").pivot
    assert piv[1] == pytest.approx(-19.0) and piv[2] == pytest.approx(45.0)
    bb = next(p for p in live.parts if p.name == "chuck").shape.bounding_box()
    assert bb.center().Y == pytest.approx(-19.0, abs=0.01) and bb.center().Z == pytest.approx(45.0, abs=0.01)
    # bed-frame numbers by hand, chuck gap and jaw reach
    man = mm.model_for(z1_record(machines, {"axis_y": -2.5, "axis_z": 44.0, "chuck_gap": 8, "jaw_reach": 12}), workspace=False)
    assert man.rotary["axis_y"] == -2.5 and man.rotary["axis_z"] == 44.0 and man.rotary["source"] == "manual"
    assert man.rotary["chuck_gap"] == 8 and man.rotary["jaw_reach"] == 12 and "user" in man.sources["4th axis chuck gap"]
    jaws = next(p for p in man.parts if p.name == "chuck jaws").shape.bounding_box()
    assert jaws.max.X == pytest.approx(man.rotary["chuck_face_x"] + 12)
    # the mapping is documented where the override keys are defined
    assert "axis_my" in mm.ROTARY_OVERRIDE_KEYS and "bed_y = axis_my - anchor1_y + bed_anchor_y" in open(mm.__file__).read()


def test_z1_tailstock_follows_the_stock_length_and_can_be_removed(machines):
    ref = mm.model_for(z1_record(machines), workspace=False)
    long = mm.model_for(z1_record(machines, {"stock_length": 100}), workspace=False)
    tip = lambda model: next(p for p in model.parts if p.name == "tailstock").shape.bounding_box().min.X  # noqa: E731
    assert tip(ref) == pytest.approx(80.0) and tip(long) == pytest.approx(-45 + 5 + 100)                 # chuck face + gap + bar
    assert long.rotary["tail_x"] == pytest.approx(60.0)
    gap = mm.model_for(z1_record(machines, {"stock_length": 100, "tail_gap": -2}), workspace=False)
    assert tip(gap) == pytest.approx(58.0)                                                              # centre 2 mm into a centre hole
    none = mm.model_for(z1_record(machines, {"tail": False}), workspace=False)
    assert none.rotary["tail"] is False and none.rotary["tail_x"] is None and not [p for p in none.parts if "tailstock" in p.name]
    assert "no tailstock" in machine_script.summary(none)
    air = mm.carvera_air(True)
    assert air.rotary["tail"] and air.rotary["jaw_reach"] == 16.0 and air.rotary["tail_cone"]["half_angle"] == 30.0


def test_normalize_rotary_defaults_and_errors():
    old = machine_script.normalize_rotary({"axis_y": 0, "axis_z": 40, "chuck_face_x": -20, "chuck_r": 25, "tail_x": 90, "tail_r": 6})
    assert old["chuck_gap"] == 5.0 and old["jaw_reach"] == 0.0 and old["jaw_r"] == 25 and old["tail"] is True
    assert old["tail_cone"]["half_angle"] == 30.0 and old["tail_cone"]["length"] == pytest.approx(6 / 0.57735, abs=0.01)
    assert old["tail_block"] is None and old["source"] == "reference"
    no_tail = machine_script.normalize_rotary({"axis_y": 0, "axis_z": 40, "chuck_face_x": -20, "chuck_r": 25})
    assert no_tail["tail"] is False and no_tail["tail_x"] is None
    with pytest.raises(machine_script.MachineScriptError, match="missing"):
        machine_script.normalize_rotary({"axis_y": 0, "axis_z": 40})
    with pytest.raises(machine_script.MachineScriptError, match="tail_x"):
        machine_script.normalize_rotary({"axis_y": 0, "axis_z": 40, "chuck_face_x": -20, "chuck_r": 25, "tail": True})
    with pytest.raises(machine_script.MachineScriptError, match="half_angle"):
        machine_script.normalize_rotary({"axis_y": 0, "axis_z": 40, "chuck_face_x": -20, "chuck_r": 25, "tail_cone": {"half_angle": 95}})
    assert mm.makera_z1(True).travel == (200.0, 159.0, 100.0)


# ------------------------------------------------------------------ the radial simulation

def test_flutes_shorter_than_the_cut_report_the_body_above_them(machines):
    m = z1_record(machines)
    st = cam.Setup(m, cam.Stock.cylinder(32, 62, x0=0), rotary=True)
    short = cam.Tool(5, "stubby", "flat", 3.175, 2, 5, rpm=12000, feed=600, plunge=150, stepdown=1, stepover=0.4)   # 5 mm flutes
    # a straight plunge at the bar's middle to 2 mm off the axis: 14 mm deep in a Ø32 bar with 5 mm of flutes
    p = cam.Program(st)
    p.add(manual_op(st, short, "Deep plunge", [(cam.RAPID, 30, 0, 20), (cam.FEED, 30, 0, 2), (cam.FEED, 30, 0, 20)]))
    r = cam_sim.simulate(p)
    col = r.stats["collisions"]
    assert col["count"] > 0
    hit = next(h for h in col["hits"] if h["part"].startswith("tool above its 5"))
    assert hit["what"] == "stock" and hit["op"] == "Deep plunge" and 7.0 <= hit["depth"] <= 9.5          # 16 − (2 + 5) = 9 at the top ray
    assert "COLLISIONS" in r.summary() and "tool above its 5 mm flutes" in r.summary()
    # 12 mm flutes to 5 off the axis (11 deep) are clean; the plunge still cuts progressively to its full depth
    p2 = cam.Program(st)
    p2.add(manual_op(st, flat(), "Plunge", [(cam.RAPID, 30, 0, 20), (cam.FEED, 30, 0, 5), (cam.FEED, 30, 0, 20)]))
    ok = cam_sim.simulate(p2)
    assert ok.stats["collisions"]["count"] == 0
    assert ok.final["r"][0].min() == pytest.approx(5.0, abs=0.15)                                   # the top ray, under the tool
    # the same 12 mm tool to 2 off the axis (13.9 deep) is buried 1.9 at its sides (the bar is at 15.9 beside the tool)
    p3 = cam.Program(st)
    p3.add(manual_op(st, flat(), "Plunge", [(cam.RAPID, 30, 0, 20), (cam.FEED, 30, 0, 2), (cam.FEED, 30, 0, 20)]))
    deep = cam_sim.simulate(p3)
    body = next(h for h in deep.stats["collisions"]["hits"] if h["part"].startswith("tool above its 12"))
    assert body["depth"] == pytest.approx(1.9, abs=0.15) and deep.final["r"][0].min() == pytest.approx(2.0, abs=0.15)


def test_cutting_by_the_chuck_hits_the_jaws_but_not_further_along(machines):
    m = z1_record(machines)
    st = cam.Setup(m, cam.Stock.cylinder(32, 62, x0=0), rotary=True)          # chuck face at x −5, jaw tips at +11

    def groove(x):
        p = cam.Program(st)
        p.add(manual_op(st, flat(), f"Groove at {x}", [(cam.RAPID, x, 0, 20, 0), (cam.FEED, x, 0, 14, 0), (cam.FEED, x, 0, 14, 360), (cam.RAPID, x, 0, 20, 360)]))
        return cam_sim.simulate(p)

    near = groove(2.0)
    jaw = [h for h in near.stats["collisions"]["hits"] if h["what"] == "chuck jaws"]
    assert jaw and jaw[0]["part"] == "tool" and jaw[0]["depth"] > 0 and near.stats["mounting"]["jaw_tip_x"] == 11.0
    assert not [h for h in near.stats["collisions"]["hits"] if h["what"] == "chuck"]     # the body face is further back
    far = groove(27.0)
    assert far.stats["collisions"]["count"] == 0
    # a holder level wide enough reaches the jaws from further along: a Ø40 nut 18 above the tip, 15 mm from the chuck face,
    # lowered to 2 above the axis (the nut's bottom at 20 is inside the jaw ring, r 22.6); the move's end is what hits
    deep = cam.Program(st)
    deep.add(manual_op(st, flat(), "To the axis", [(cam.RAPID, 15, 0, 30, 0), (cam.FEED, 15, 0, 2, 0)]))
    model = mm.model_for(m, workspace=False); model.nose = [{"name": "nut", "r": 20.0, "h": 10.0}]
    rr = cam_sim.simulate(deep, machine_model=model)
    nut = next(h for h in rr.stats["collisions"]["hits"] if h["part"] == "nut")
    assert nut["what"] == "chuck jaws" and nut["move"] == 1 and nut["depth"] == pytest.approx(2.6, abs=0.3)
    assert rr.stats["collisions"]["count"] == len(set((h["part"], h["what"]) for h in rr.stats["collisions"]["hits"]))   # one per level and solid per piece


def test_reaching_past_the_bar_hits_the_tailstock_cone(machines):
    m = z1_record(machines)
    st = cam.Setup(m, cam.Stock.cylinder(32, 62, x0=0), rotary=True)          # live centre tip at x 62
    p = cam.Program(st)                                                        # 2 mm past the bar's end, down to the axis
    p.add(manual_op(st, flat(), "Face the end", [(cam.RAPID, 64, 0, 20), (cam.FEED, 64, 0, 0.5), (cam.FEED, 64, 0, 20)]))
    r = cam_sim.simulate(p)
    tail = [h for h in r.stats["collisions"]["hits"] if h["what"] == "tailstock"]
    assert tail and tail[0]["part"] == "tool" and tail[0]["op"] == "Face the end" and r.stats["mounting"]["tail_tip_x"] == 62.0
    assert {h["what"] for h in r.stats["collisions"]["all"]} == {"tailstock"} and "tailstock" in r.summary()   # the bar itself is not touched
    # the same move 15 mm above the axis clears the cone (it is only 2 mm wide there)
    hi = cam.Program(st)
    hi.add(manual_op(st, flat(), "Skim", [(cam.RAPID, 64, 0, 20), (cam.FEED, 64, 0, 15)]))
    assert cam_sim.simulate(hi).stats["collisions"]["count"] == 0
    # a rapid through the tailstock barrel is a collision too, and flagged as a rapid
    rp = cam.Program(st)
    rp.add(manual_op(st, flat(), "Rapid", [(cam.RAPID, 100, 0, 20), (cam.RAPID, 100, 0, 2)]))
    hits = cam_sim.simulate(rp).stats["collisions"]["hits"]
    assert any(h["what"] == "tailstock" and h.get("rapid") for h in hits)
    # without a tailstock nothing is there
    none = mm.model_for(z1_record(machines, {"tail": False}), workspace=False)
    assert cam_sim.simulate(p, machine_model=none).stats["collisions"]["count"] == 0
    # placement overrides move the mounting
    far = cam_sim.simulate(p, placement={"tail_gap": 5.0})
    assert far.stats["mounting"]["tail_tip_x"] == 67.0 and far.stats["collisions"]["count"] == 0


def test_mixed_mountings_raise(machines, shaft):
    m = z1_record(machines)
    a = cam.Setup(m, cam.Stock.cylinder(32, 62, x0=-1), rotary=True, name="Bar A")
    b = cam.Setup(m, cam.Stock.cylinder(40, 62, x0=-1), rotary=True, name="Bar B")
    p = cam.Program(a)
    p.add(cam.rotary_rough(a, flat(), shaft.shape, stepdown=3.0, mode="rings", name="Rough A"))
    p.add(cam.rotary_rough(b, flat(), shaft.shape, stepdown=3.0, mode="rings", name="Rough B"))
    with pytest.raises(cam.CamError, match="different 4th-axis mountings"):
        cam_sim.simulate(p)
    assert cam_sim.simulate(p, ops=[0]).mode == "radial" and cam_sim.simulate(p, ops=[1]).mode == "radial"
    # the same mounting twice (two setups that only differ by name) simulates together
    c = cam.Setup(m, cam.Stock.cylinder(32, 62, x0=-1), rotary=True, name="Bar A again")
    q = cam.Program(a)
    q.add(cam.rotary_rough(a, flat(), shaft.shape, stepdown=3.0, mode="rings"))
    q.add(cam.rotary_rough(c, flat(), shaft.shape, stepdown=3.0, mode="rings", name="Again"))
    assert cam_sim.simulate(q).stats["ops"] == 2


def test_gouges_are_attributed_to_the_op_that_made_them(machines, shaft):
    m = z1_record(machines)
    st = cam.Setup(m, cam.Stock.cylinder(32, 62, x0=-1), rotary=True)
    p = cam.Program(st)
    p.add(cam.rotary_rough(st, flat(), shaft.shape, stepdown=2.0, mode="rings", name="Rough"))
    p.add(cam.rotary_finish(st, ball(), shaft.shape, mode="rings", stepover=0.3, leave=-0.5, name="Too deep"))
    r = cam_sim.simulate(p, part=shaft)
    g = r.stats["gouge"]
    assert g["cells"] > 0 and g["ops"] == ["Too deep"] and g["by_op"][0]["op"] == "Too deep"
    assert g["by_op"][0]["max_depth"] == pytest.approx(0.5, abs=0.1) and set(g["by_op"][0]["worst"]) == {"x", "angle"}
    assert "caused by: Too deep" in r.summary()
    # the frames / grid honour the angular resolution parameter
    coarse = cam_sim.simulate(p, part=shaft, res_a=2.0, ops=[0])
    assert coarse.grid["na"] == 180 and coarse.grid["res_a"] == 2.0
