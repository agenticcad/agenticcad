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
    # Makera's simplified 4-axis model: axis 9.74 in front of the bed centre, 45 up; chuck body face at −59.76, jaws 13.9 proud.
    # The jaws are fitted reversed (teeth out): a bar sits on a step, so the gap is None (as far in as the step) — on the
    # drawn (closed) jaws the first step is 4 proud of the face: the tailstock for a 120 bar sits at −59.76 + 4 + 120
    assert ref.rotary["axis_y"] == -9.74 and ref.rotary["axis_z"] == 45.0 and ref.rotary["source"] == "reference"
    assert ref.rotary["jaws"] == "reversed" and ref.rotary["chuck_gap"] is None and ref.rotary["jaw_r_out_closed"] == 26.5 and ref.rotary["jaw_travel"] == 10.0
    assert ref.rotary["jaw_reach"] == 13.9 and ref.rotary["tail"] and ref.rotary["tail_x"] == pytest.approx(64.24)
    assert ref.rotary["chuck_face_x"] == -59.76 and ref.rotary["chuck_r"] == 26.0 and ref.rotary["chuck_len"] == 33.0
    assert ref.rotary["tail_cone"] == {"half_angle": 45.0, "length": 5.0} and ref.rotary["tail_r"] == 9.0 and ref.rotary["tail_barrel"] == 10.5
    assert ref.rotary["jaw_r_in"] == 6.2 and [s[:2] for s in ref.rotary["jaw_profile"]] == [[-11.5, -3.0], [-3.0, 4.0], [4.0, 9.0], [9.0, 13.9]]
    std = mm.model_for(z1_record(machines, {"jaws": "standard"}), workspace=False)
    assert std.rotary["jaws"] == "standard" and std.rotary["chuck_gap"] == 5.0 and "user" in std.sources["4th axis jaws"]
    # jaw_envelope: where the jaws sit for a bar. Reversed: Ø25.4 is held on the first step (ref = 12.7 + 17.6), its end 4 in
    # front of the face, the outer block a ring 8 off the bar; Ø50 needs the second step (ref 34.6, seat 9); closed = 26.5
    e = machine_script.jaw_envelope(ref.rotary, 12.7)
    assert (e["ref"], e["seat"], e["riser"]) == (30.3, 4.0, 17.6) and e["rings"][-1] == (9.0, 13.9, pytest.approx(20.7), 30.3, 30.3)
    e2 = machine_script.jaw_envelope(ref.rotary, 25.0)
    assert (e2["ref"], e2["seat"], e2["riser"]) == (34.6, 9.0, 9.6) and all(r[2] == 0.0 for r in e2["rings"])
    assert machine_script.jaw_envelope(ref.rotary, 0.0)["ref"] == 26.5
    s = machine_script.jaw_envelope(std.rotary, 12.7)
    assert (s["ref"], s["seat"], s["outer_r"]) == (12.7, 0.0, pytest.approx(38.23)) and s["rings"][1] == (-3.0, 4.0, 0.0, pytest.approx(38.23), pytest.approx(30.3))
    assert {b["name"] for b in ref.rotary["fixed_boxes"]} >= {"4th axis housing", "4th axis motor", "4th axis belt cover", "4th axis rail"}
    assert "reference" in ref.sources["4th axis position"] and "Makera" in ref.sources["4th axis position"]
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
    bb = next(p for p in live.parts if p.name == "chuck body").shape.bounding_box()
    assert bb.center().Y == pytest.approx(-19.0, abs=0.01) and bb.center().Z == pytest.approx(45.0, abs=0.01)
    # bed-frame numbers by hand, chuck gap and jaw reach
    man = mm.model_for(z1_record(machines, {"axis_y": -2.5, "axis_z": 44.0, "chuck_gap": 8, "jaw_reach": 12}), workspace=False)
    assert man.rotary["axis_y"] == -2.5 and man.rotary["axis_z"] == 44.0 and man.rotary["source"] == "manual"
    assert man.rotary["chuck_gap"] == 8 and man.rotary["jaw_reach"] == 12 and "user" in man.sources["4th axis chuck gap"]
    jaws = [p for p in man.parts if p.name.startswith("chuck jaw ")]
    assert len(jaws) == 4 and all(p.collision and p.node == "chuck" for p in jaws) and {p.name[-2:] for p in jaws} == {"+Y", "+Z", "-Y", "-Z"}
    assert jaws[0].shape.bounding_box().max.X == pytest.approx(man.rotary["chuck_face_x"] + 12)
    # grip (bar inside the jaws, from their tips) is the other way to say chuck_gap; the bar can go no further than the body face
    grip = mm.model_for(z1_record(machines, {"grip": 10.0}), workspace=False)
    assert grip.rotary["chuck_gap"] == pytest.approx(3.9) and "user" in grip.sources["4th axis chuck gap"]
    assert mm.model_for(z1_record(machines, {"grip": 15.0}), workspace=False).rotary["chuck_gap"] == 0.0
    # the jaws are drawn slid out to a nominal bar; the viewer and the simulator place them from the program's stock. Reversed,
    # the serrated face is the outside: a Ø50 bar on the second step puts it at r 34.6, the jaw reaching 25.53 inward
    big = mm.model_for(z1_record(machines, {"stock_diameter": 50.0}), workspace=False)
    top = next(p for p in big.parts if p.name == "chuck jaw +Z").shape.bounding_box()
    assert top.max.Z == pytest.approx(45.0 + 34.6, abs=0.01) and top.min.Z == pytest.approx(45.0 + 34.6 - 25.53, abs=0.01) and big.rotary["stock_diameter"] == 50.0
    closed = next(p for p in ref.parts if p.name == "chuck jaw +Z").shape.bounding_box()
    assert closed.max.Z == pytest.approx(45.0 + 26.5, abs=0.01)                                 # drawn closed: serrated faces at r 26.5
    stdj = next(p for p in std.parts if p.name == "chuck jaw +Z").shape.bounding_box()
    assert stdj.min.Z == pytest.approx(45.0 + 6.2, abs=0.01) and stdj.max.Z == pytest.approx(45.0 + 6.2 + 25.53, abs=0.01)   # teeth in: r 6.2 → 31.73
    # the mapping is documented where the override keys are defined
    assert "axis_my" in mm.ROTARY_OVERRIDE_KEYS and "bed_y = axis_my - anchor1_y + bed_anchor_y" in open(mm.__file__).read()


def test_z1_tailstock_follows_the_stock_length_and_can_be_removed(machines):
    ref = mm.model_for(z1_record(machines), workspace=False)
    long = mm.model_for(z1_record(machines, {"stock_length": 100}), workspace=False)
    tip = lambda model: next(p for p in model.parts if p.name == "tailstock centre").shape.bounding_box().min.X  # noqa: E731
    assert tip(ref) == pytest.approx(64.24) and tip(long) == pytest.approx(-59.76 + 4 + 100)            # chuck face + the bar's seat + bar
    assert long.rotary["tail_x"] == pytest.approx(44.24)
    blk = next(p for p in long.parts if p.name == "tailstock").shape.bounding_box()                     # the block 15.5 behind the tip, 30 thick
    assert blk.min.X == pytest.approx(44.24 + 15.5) and blk.max.X == pytest.approx(44.24 + 45.5) and (blk.min.Z, blk.max.Z) == (5.5, 62.5)
    gap = mm.model_for(z1_record(machines, {"stock_length": 100, "tail_gap": -2}), workspace=False)
    assert tip(gap) == pytest.approx(42.24)                                                              # centre 2 mm into a centre hole
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
    st = cam.Setup(m, cam.Stock.cylinder(32, 62, x0=0), rotary=True)          # reversed jaws: the bar's end on the step 4 in front of the face → face −4, tips +9.9

    def groove(x):
        p = cam.Program(st)
        p.add(manual_op(st, flat(), f"Groove at {x}", [(cam.RAPID, x, 0, 20, 0), (cam.FEED, x, 0, 14, 0), (cam.FEED, x, 0, 14, 360), (cam.RAPID, x, 0, 20, 360)]))
        return cam_sim.simulate(p)

    near = groove(2.0)
    jaw = [h for h in near.stats["collisions"]["hits"] if h["what"] == "chuck jaws"]
    assert jaw and any(h["part"] == "tool" and h["depth"] > 0 for h in jaw) and near.stats["mounting"]["jaw_tip_x"] == 9.9
    assert near.stats["mounting"]["chuck_face_x"] == -4.0 and near.stats["mounting"]["jaw_ref_r"] == pytest.approx(33.6)   # Ø32 on the first step
    assert not [h for h in near.stats["collisions"]["hits"] if h["what"] == "chuck"]     # the body face is further back
    far = groove(27.0)
    assert far.stats["collisions"]["count"] == 0
    # a holder level wide enough reaches the jaws from further along: a Ø40 nut 18 above the tip, 15 mm from the chuck face,
    # lowered to 2 above the axis (the nut's bottom at 20 is inside the jaws, r 33.6 on this bar); the move's end is what hits
    deep = cam.Program(st)
    deep.add(manual_op(st, flat(), "To the axis", [(cam.RAPID, 15, 0, 30, 0), (cam.FEED, 15, 0, 2, 0)]))
    model = mm.model_for(m, workspace=False); model.nose = [{"name": "nut", "r": 20.0, "h": 10.0}]
    rr = cam_sim.simulate(deep, machine_model=model)
    nut = next(h for h in rr.stats["collisions"]["hits"] if h["part"] == "nut")
    assert nut["what"] == "chuck jaws" and nut["move"] == 1 and 4.0 < nut["depth"] < 14.0
    assert rr.stats["collisions"]["count"] == len(set((h["part"], h["what"]) for h in rr.stats["collisions"]["hits"]))   # one per level and solid per piece


def test_jaws_sit_where_the_bar_puts_them(machines):
    """The jaws slide out with the bar and the bar sits where the jaws let it. Reversed (as fitted): a Ø25.4 bar is held on the
    first step wall (serrated faces at r 12.7 + 17.6 = 30.3), its end on that step 4 in front of the body face; the outer block
    (9 → 13.9 proud) is a ring 8 off the bar. A Ø50 bar needs the second step: faces at r 34.6, end 9 in front. Standard (teeth
    in): the faces close on the bar and the steps stand outward — the base to r 38.2 on Ø25.4."""
    m = z1_record(machines)

    def run(d, x, z, a=0, ov=None, pl=None):
        rec = z1_record(machines, ov) if ov else m
        st = cam.Setup(rec, cam.Stock.cylinder(d, 80, x0=0), rotary=True, a=a)      # the bar's end at x 0
        p = cam.Program(st)
        p.add(manual_op(st, flat(), "Dip", [(cam.RAPID, x, 0, 60, a), (cam.FEED, x, 0, z, a), (cam.RAPID, x, 0, 60, a)]))
        r = cam_sim.simulate(p, placement=pl)
        return r.stats["mounting"], [(h["what"], h["part"]) for h in r.stats["collisions"]["hits"]]

    small, hits_small = run(25.4, 2.5, 32.0)                    # Ø25.4 reversed: face −4, bar end 0, tips 9.9; faces at 30.3 → 32 clears
    assert (small["jaws"], small["chuck_face_x"], small["bar_seat_x"], small["jaw_tip_x"], small["grip"]) == ("reversed", -4.0, 0.0, 9.9, 9.9)
    assert small["jaw_rings"] == [(-15.5, -7.0, 0.0, 30.3, 30.3), (-7.0, 0.0, 0.0, 30.3, 30.3), (0.0, 5.0, 0.0, 30.3, 30.3), (5.0, 9.9, pytest.approx(20.7), 30.3, 30.3)]
    assert not hits_small and ("chuck jaws", "tool") in run(25.4, 2.5, 28.0)[1]
    big, hits_big = run(50.0, 2.5, 32.0)                        # Ø50 reversed: second step, faces at 34.6, end 9 in front → the same move is in
    assert (big["chuck_face_x"], big["jaw_ref_r"], big["jaw_tip_x"]) == (-9.0, 34.6, 4.9) and all(r[2] == 0.0 for r in big["jaw_rings"])
    assert ("chuck jaws", "tool") in hits_big
    for a in (0, 90, 225):                                      # the jaws turn with A: rings, so the angle makes no difference
        assert ("chuck jaws", "tool") in run(50.0, 2.5, 32.0, a)[1]
    # the outer block beyond the bar's seat is a ring: a level inside its hole (r < 20.7) is clear, one across it is not
    assert ("chuck jaws", "tool") in run(25.4, 7.5, 25.0)[1] and not run(25.4, 7.5, 32.0)[1]
    assert ("chuck jaws", "tool") in run(25.4, 7.5, 15.0)[1]   # the flutes reach 27: across the ring's inner edge
    # standard jaws (teeth in) on the same bar: face −5 (gap 5), the base ring r 38.2 just behind the face — 31 up at x −12
    # is in it, while the reversed jaws (r 30.3 there) clear it; at the tips (r 22.3) the standard jaws are the smaller
    stdm, std_hits = run(25.4, -12.0, 31.0, ov={"jaws": "standard"})
    assert stdm["jaws"] == "standard" and stdm["chuck_face_x"] == -5.0 and stdm["jaw_tip_x"] == 8.9 and stdm["jaw_ring_r"] == pytest.approx(38.23)
    assert ("chuck jaws", "tool") in std_hits and ("chuck", "tool") not in std_hits and not run(25.4, -12.0, 31.0)[1]
    assert not run(25.4, 6.5, 25.0, ov={"jaws": "standard"})[1] and ("chuck jaws", "tool") in run(25.4, 6.5, 20.0, ov={"jaws": "standard"})[1]
    # the body itself: r 26 for 33 mm behind the face, then the housing (a box, not a cylinder)
    assert [w for w, _ in run(25.4, -30.0, 24.0)[1]] == ["chuck"] and not run(25.4, -30.0, 27.5)[1]
    assert ("chuck", "tool") not in run(25.4, -45.0, 24.0)[1] and ("4th axis housing", "tool") in run(25.4, -45.0, 10.0)[1]
    # grip: the bar held n mm from the jaw tips; reversed jaws cannot take it past the step (9.9 on the first step)
    assert run(25.4, 40.0, 60.0, pl={"grip": 5.0})[0]["jaw_tip_x"] == pytest.approx(5.0) and run(25.4, 40.0, 60.0, pl={"grip": 5.0})[0]["bar_seat_x"] == pytest.approx(-4.9)
    assert run(25.4, 40.0, 60.0, pl={"grip": 15.0})[0]["grip"] == pytest.approx(9.9) and run(25.4, 40.0, 60.0, pl={"chuck_gap": 1.0})[0]["chuck_gap"] == 4.0
    assert run(25.4, 40.0, 60.0, ov={"jaws": "standard"}, pl={"grip": 20.0})[0]["chuck_gap"] == 0.0      # teeth in: it bottoms on the body face


def test_module_housing_motor_rail_and_tailstock_are_checked(machines):
    """The parts that do not turn: the housing (35 wide, to 17.5 above the axis), the motor in front of it (49 forward, 20 below),
    the belt cover behind, the rail under everything, and the tailstock's block, flange and clamp screw behind the live centre."""
    m = z1_record(machines)
    st = cam.Setup(m, cam.Stock.cylinder(25.4, 93, x0=0), rotary=True)       # face −4: housing −72..−37; tip at 93: barrel 98..108.5, block 108.5..138.5

    def dip(x, y, z, a=0, setup=st):
        p = cam.Program(setup)
        p.add(manual_op(setup, flat(), f"Dip {x} {y} {z}", [(cam.RAPID, x, y, 60, a), (cam.FEED, x, y, z, a)]))
        return sorted({h["what"] for h in cam_sim.simulate(p).stats["collisions"]["hits"]})

    assert dip(-50, 0, 10) == ["4th axis housing"] and dip(-50, 0, 25) == []                       # over the housing: top at +17.5
    assert dip(-50, -49, -10) == ["4th axis motor"] and dip(-50, -49, 8) == []                     # the motor: top at +1
    assert dip(-85, 0, 15) == ["4th axis belt cover"] and dip(-85, 0, 28) == []                    # cover to +21
    assert dip(185, 22, -40) == ["4th axis rail"] and dip(185, 22, -30) == []                      # rail top 36.5 below the axis (past the tailstock)
    for a in (90, 180, 270):                                                                       # fixed parts do not turn with A
        sa = cam.Setup(m, cam.Stock.cylinder(25.4, 93, x0=0), rotary=True, a=a)
        assert dip(-50, -49, -10, a, sa) == ["4th axis motor"] and dip(-50, 0, 10, a, sa) == ["4th axis housing"]
    assert dip(104, 0, 5) == ["tailstock"] and dip(104, 0, 12) == []                               # the Ø18 body, 5 → 15.5 behind the tip
    assert dip(120, 0, 10) == ["tailstock"] and dip(120, 0, 25) == []                              # the 30-thick block
    assert "tailstock flange" in dip(120, 22, -34) and dip(142, 0, 0) == ["tailstock clamp screw"]    # (the nose beside the block hits it too)
    assert dip(96, 0, 2) == ["tailstock"]                                                          # the 90° point: r 4.6 at 4.6 behind the tip
    # the mounting payload names what was checked
    p = cam.Program(st); p.add(manual_op(st, flat(), "Lift", [(cam.RAPID, 40, 0, 60)]))
    mt = cam_sim.simulate(p).stats["mounting"]
    assert mt["jaw_tip_x"] == 9.9 and mt["tail_tip_x"] == 93.0 and mt["chuck_face_x"] == -4.0 and mt["jaws"] == "reversed"


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
    assert any(h["what"].startswith("tailstock") and h.get("rapid") for h in hits)
    assert {h["what"] for h in hits} == {"tailstock"}                    # x 100 is in the 30 mm block (77.5 → 107.5)
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


# ------------------------------------------------------------------ radial map speed, time estimate

def test_compiled_radial_map_equals_the_python_loop(machines, shaft, monkeypatch):
    st = cam.Setup(machines["Makera Z1 + 4th axis"], cam.Stock.cylinder(32, 62, x0=-1), rotary=True)
    a = cam.radial_map(st, shaft.shape, res_x=1.0, res_a=4.0)[2]
    monkeypatch.setenv("AGENTICCAD_SIM_BACKEND", "numpy")
    b = cam.radial_map(st, shaft.shape, res_x=1.0, res_a=4.0)[2]
    assert a.max() > 13 and (a == b).all()


def test_time_estimate_allows_for_the_rotary_speed(machines, shaft):
    m = machines["Makera Z1 + 4th axis"]
    st = cam.Setup(m, cam.Stock.cylinder(32, 62, x0=-1), rotary=True)
    op = manual_op(st, flat(), "Turn", [(cam.RAPID, 10, 0, 2, 0.0), (cam.FEED, 10, 0, 2, 3600.0)])
    assert op.a_rate_max() > m.rotary["max_speed"]
    assert op.time_minutes(m) == pytest.approx(3600.0 / m.rotary["max_speed"])     # ten turns at the axis' speed, not the feed's


def test_payload_tells_the_viewer_which_setups_share_a_mounting_and_where_clearance_is(machines, shaft):
    """Playback walks the post's moves between ops on one mounting: it needs to know they share it, and the height to lift to."""
    m = machines["Makera Z1 + 4th axis"]
    stock = cam.Stock.cylinder(30.0, 60.0, x0=-5.0, material="aluminium")
    a = cam.Setup(m, stock, rotary=True, origin=(0.0, 0.0, 0.0), name="A0")
    b = cam.Setup(m, stock, rotary=True, a=90, origin=(0.0, 0.0, 0.0), name="A90")
    c = cam.Setup(m, cam.Stock.cylinder(40.0, 60.0, x0=-5.0, material="aluminium"), rotary=True, origin=(0.0, 0.0, 0.0), name="Other bar")
    prog = cam.Program(a, name="mounts")
    for st in (a, b, c):
        prog.add(manual_op(st, flat(6), st.name, [(cam.RAPID, 0, 0, st.safe_z)]))
    s = prog.to_payload(moves=False)["setups"]
    assert s[0]["mount"] == s[1]["mount"] == 0 and s[2]["mount"] == 2
    assert s[0]["clear_z"] == pytest.approx(a.clearance_z) and s[2]["clear_z"] == pytest.approx(c.clearance_z)


def test_a_bar_too_thin_for_reversed_jaws_stops_the_run_until_the_setting_is_right(machines):
    """Ø12 can't be held on a reversed jaw's step: the operator must turn the jaws over, and then a check against reversed
    jaws would be against smaller solids than the real ones."""
    m = machines["Makera Z1 + 4th axis"]
    def sim(d, jaws=None):
        rec = cam.Machine(**{**m.__dict__, "model": {"rotary": {"jaws": jaws}} if jaws else None})
        st = cam.Setup(rec, cam.Stock.cylinder(d, 62, x0=-1), rotary=True)
        prog = cam.Program(st, name="bar")
        prog.add(manual_op(st, flat(6), "air", [(cam.RAPID, 30, 0, st.safe_z, 0.0), (cam.RAPID, 40, 0, st.safe_z, 0.0)]))
        return cam_sim.simulate(prog, resolution=1.0, machine_model=mm.model_for(rec, workspace=False))
    r = sim(12)
    assert "JAWS:" in r.summary() and any(e["code"] == "JAWS" for e in r.verdict()["errors"])
    assert sim(12, "standard").stats["mounting"]["jaw_note"] is None
    assert sim(25.4).stats["mounting"]["jaw_note"] is None
