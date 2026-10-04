"""Multiple setups, the 4th axis, the Makera post and the material-removal simulation."""
import math
import re

import numpy as np
import pytest

import cad_kernel as ck
import cam_kernel as cam
import cam_sim
from cam_data import Library

PLATE = '''
plate = Box(60, 40, 12, align=(Align.CENTER, Align.CENTER, Align.MIN))
plate = plate - Pos(0, 0, 12) * Box(30, 20, 8)          # top pocket 4 deep
plate = plate - Pos(10, 0, 0) * Cylinder(6, 6)          # bottom counterbore 3 deep
result = {"Plate": plate}
'''
SHAFT = '''
shaft = Rot(0, 90, 0) * Cylinder(10, 60, align=(Align.CENTER, Align.CENTER, Align.MIN))
shaft = shaft + Rot(0, 90, 0) * Pos(0, 0, 20) * Cylinder(14, 20, align=(Align.CENTER, Align.CENTER, Align.MIN))
shaft = shaft - Pos(50, 0, 13) * Box(14, 30, 10)
result = {"Shaft": shaft}
'''


@pytest.fixture(scope="module")
def machines(tmp_path_factory):
    return Library(tmp_path_factory.mktemp("lib")).machines()


@pytest.fixture(scope="module")
def plate():
    return ck.run_script(PLATE)


@pytest.fixture(scope="module")
def shaft():
    return ck.run_script(SHAFT)


def flat(d=3.175, n=1, **kw):
    return cam.Tool(n, f"{d} flat", "flat", d, 2, 12, rpm=12000, feed=600, plunge=150, stepdown=0.8, stepover=0.4, **kw)


def ball(d=3.175, n=2):
    return cam.Tool(n, f"{d} ball", "ball", d, 2, 12, rpm=12000, feed=500, plunge=150, stepdown=0.8, stepover=0.15)


def two_sided(machine, part, cut_too_wide=False):
    stock = cam.Stock.from_model(part, margin=3, top=1)
    top = cam.Setup(machine, stock, name="Top")
    bot = cam.Setup(machine, stock, orient="bottom", name="Bottom")
    t = flat()
    p = cam.Program(top, name="two sided")
    p.add(cam.face(top, t, z_top=top.stock.top, z_bottom=12))
    w = 20 if cut_too_wide else 15
    p.add(cam.pocket(top, t, cam.rect(-w, -10, w, 10), z_top=12, z_bottom=8))
    p.add(cam.contour(top, t, cam.section(part.shape, 6), z_top=12, z_bottom=6, side="outside"))
    p.add(cam.pocket(bot, t, cam.circle(10, 0, 12), z_top=0, z_bottom=-3, name="Counterbore"))
    p.add(cam.contour(bot, t, cam.section(bot.view(part.shape), -6), z_top=0, z_bottom=-6.5, side="outside", name="Cut out"))
    return p


# ------------------------------------------------------------------ machines

def test_makera_machines_seeded_once(tmp_path):
    lib = Library(tmp_path)
    ms = lib.machines()
    for name in ("Makera Z1", "Makera Z1 + 4th axis", "Carvera Air", "Carvera Air + 4th axis"):
        assert name in ms and ms[name].post == "makera" and ms[name].collet == pytest.approx(3.175)
    z1r = ms["Makera Z1 + 4th axis"]
    assert z1r.rotary["max_diameter"] == 80 and z1r.rotary["max_length"] == 150
    assert ms["Makera Z1"].rotary is None and ms["Makera Z1"].spindle["max"] == 13000
    lib.delete_machine("Makera Z1")
    assert "Makera Z1" not in Library(tmp_path).machines()          # a deleted built-in stays deleted


# ------------------------------------------------------------------ setups

@pytest.mark.parametrize("orient", list(cam.ORIENTS))
def test_setup_view_matches_to_model(machines, plate, orient):
    """Geometry seen through setup.view(part) and points mapped back with to_model() agree for every orientation."""
    st = cam.Setup(machines["Makera Z1"], cam.Stock.from_model(plate, margin=3, top=1), orient=orient)
    v = st.view(plate.shape).bounding_box()
    corners = np.array([[x, y, z] for x in (v.min.X, v.max.X) for y in (v.min.Y, v.max.Y) for z in (v.min.Z, v.max.Z)])
    back = st.to_model(corners)
    bb = plate.shape.bounding_box()
    assert np.allclose(back.min(axis=0), bb.min.to_tuple(), atol=1e-6) and np.allclose(back.max(axis=0), bb.max.to_tuple(), atol=1e-6)
    # the setup stock wraps the viewed part and the tool axis points away from the machined face
    assert st.stock.zmax >= v.max.Z - 1e-6 and st.stock.zmin <= v.min.Z + 1e-6
    assert np.allclose(st.tool_axis_model, st.to_model([[0, 0, 1]]) - st.to_model([[0, 0, 0]]), atol=1e-9)


def test_bottom_setup_flips_part(machines, plate):
    bot = cam.Setup(machines["Makera Z1"], cam.Stock.from_model(plate, margin=3, top=1), orient="bottom")
    assert bot.tool_axis_model == pytest.approx((0, 0, -1))
    # the model's bottom face (z = 0) is the setup's top
    assert bot.to_model([[0, 0, 0]])[0][2] == pytest.approx(0.0)


def test_rotary_setup_needs_a_4th_axis(machines, shaft):
    with pytest.raises(cam.CamError, match="4th axis"):
        cam.Setup(machines["Makera Z1"], cam.Stock.cylinder(32, 62, x0=-1), rotary=True)
    st = cam.Setup(machines["Makera Z1 + 4th axis"], cam.Stock.cylinder(32, 62, x0=-1), rotary=True)
    assert st.max_radius == pytest.approx(16.0) and st.stock.ymin == pytest.approx(-16)
    # A turns the part about +X by the right-hand rule: at A=90 the tool (setup +Z) sees the model's +Y side
    assert np.allclose(st.to_model([[0, 0, 10]], a=90.0)[0], [0, 10, 0], atol=1e-9)


# ------------------------------------------------------------------ multi-setup program + posts

def test_two_setups_get_their_own_wcs_and_a_pause(machines, plate):
    p = two_sided(machines["Makera Z1"], plate)
    assert p.check() == [] or all("collet" not in w for w in p.check())
    g = p.gcode()
    lines = g.splitlines()
    assert "G54" in g and "G55" in g and lines.count("M600") == 1
    i = lines.index("M600")
    # spindle stopped before the operator pause and restarted afterwards even though the tool is unchanged
    assert any(l.startswith("M5") for l in lines[:i]) and any(l.startswith("M3") for l in lines[i:])
    pay = p.to_payload()
    assert [s["wcs"] for s in pay["setups"]] == ["G54", "G55"] and [o["setup"] for o in pay["ops"]] == [0, 0, 0, 1, 1]
    # bottom-side moves come back in model coordinates: the counterbore cuts up from z = 0 to its 3 mm floor
    cb = [m[3] for m in pay["ops"][3]["moves"] if m[0] != 0]
    assert max(cb) == pytest.approx(3.0, abs=1e-6) and min(cb) > 0


def test_makera_post_dialect(machines, plate):
    g = two_sided(machines["Makera Z1"], plate).gcode()
    for l in g.splitlines():
        assert len(l) <= cam.MAKERA_MAX_LINE, l
        assert not re.match(r"^N\d", l), l                     # line numbers stop a line executing
        assert not l[:1].islower(), l                          # lowercase first letter = console command
        assert "M4" not in l.split(";")[0].split() and "M8" not in l.split()
        if l.startswith(("(", ";")):
            assert "G90" not in l and "G91" not in l           # the firmware hoists G90/G91 even out of comments
    assert re.search(r"^M6 T1$", g, re.M)                      # tool change carries the tool on the same line
    assert g.rstrip().endswith("M30") and "G28" in g


def test_grbl_post_unchanged_for_generic_machines(machines, plate):
    g = two_sided(machines["Generic 3018"], plate).gcode()
    assert "M600" not in g and "M0" in g and "G94" in g


def test_makera_a_feed_rule():
    """A-only moves: F is deg/min boosted by 360/perimeter below 360 mm; mixed: the A surface speed is held to F."""
    t = 0.5                                                    # minutes
    r = 10.0
    per = 2 * math.pi * r + 30
    f = cam._makera_feed(0.0, 90.0, 0.0, r, t, 1800)
    assert f == pytest.approx(min(90 / t / (360 / per), 1800))
    f = cam._makera_feed(10.0, 90.0, 0.0, r, t, 1800)
    assert f == pytest.approx(max(10.0, 90 * per / 360) / t)
    assert cam._makera_feed(10.0, 0.0, 0.0, r, t, 1800) == pytest.approx(20.0)


def test_rotary_ops_post_with_a_words(machines, shaft):
    m = machines["Makera Z1 + 4th axis"]
    st = cam.Setup(m, cam.Stock.cylinder(32, 62, x0=-1), rotary=True)
    p = cam.Program(st)
    p.add(cam.rotary_rough(st, flat(), shaft.shape, stepdown=2.0, mode="rings"))
    p.add(cam.rotary_finish(st, ball(), shaft.shape, mode="spiral", stepover=0.1))
    assert all(o.uses_a for o in p.ops)
    g = p.gcode()
    assert re.search(r"^G1 .*A-?\d", g, re.M) and not re.search(r"^G[23] .*A", g, re.M)   # no A during arcs
    assert all(len(l) <= cam.MAKERA_MAX_LINE for l in g.splitlines())
    rough = p.ops[0]
    assert min(mv[3] for mv in rough.moves if mv[0] != cam.RAPID) >= 8.0 + 0.3 - 1e-6    # leaves 0.3 on the flat at r = 8
    # a rotary op on a 3-axis setup is refused by the checks
    flat_setup = cam.Setup(machines["Makera Z1"], cam.Stock.from_model(shaft, margin=2))
    bad = cam.Program(flat_setup); bad.add(p.ops[0])
    assert any("4th axis" in w or "rotary" in w.lower() for w in bad.check())


def test_indexed_3plus1_shares_wcs_without_pause(machines, shaft):
    m = machines["Makera Z1 + 4th axis"]
    stock = cam.Stock.cylinder(32, 62, x0=-1)
    a0 = cam.Setup(m, stock, rotary=True, a=0, name="A0")
    a90 = cam.Setup(m, stock, rotary=True, a=90, name="A90")
    t = flat()
    p = cam.Program(a0)
    p.add(cam.pocket(a0, t, cam.rect(2, -8, 20, 8), z_top=16, z_bottom=14))
    p.add(cam.pocket(a90, t, cam.rect(2, -8, 20, 8), z_top=16, z_bottom=14))
    g = p.gcode()
    assert "M600" not in g and "G55" not in g and re.search(r"^G0 A90", g, re.M)


# ------------------------------------------------------------------ simulation

def test_sim_two_sided_clean_and_attributes_gouges(machines, plate):
    good = cam_sim.simulate(two_sided(machines["Makera Z1"], plate), part=plate)
    assert good.mode == "zmap" and good.stats["gouge"]["cells"] == 0 and good.stats["removed_cm3"] > 10
    assert "No gouges" in good.summary()
    bad = cam_sim.simulate(two_sided(machines["Makera Z1"], plate, cut_too_wide=True), part=plate)
    gg = bad.stats["gouge"]
    assert gg["max_depth"] == pytest.approx(4.0, abs=0.05) and gg["area_mm2"] > 100 and "Pocket" in gg["ops"]
    assert "GOUGES" in bad.summary()
    # frames: one per mark, decodable, the last one carries the masks
    pay = bad.to_payload()
    assert pay["frames"][0] == 0 and pay["frames"][-1] == sum(len(o.moves) for o in bad_ops(machines, plate))
    last = bad.frame_payload(len(pay["frames"]) - 1)
    assert {"zhi", "zlo", "gouge", "left"} <= set(last["data"])


def bad_ops(machines, plate):
    return two_sided(machines["Makera Z1"], plate, cut_too_wide=True).ops


def test_sim_single_op_and_rapid_through_material(machines, plate):
    p = two_sided(machines["Makera Z1"], plate)
    one = cam_sim.simulate(p, ops=[1], part=plate)
    assert one.stats["ops"] == 1 and one.stats["gouge"]["cells"] == 0
    st = p.setup
    op = cam.Op("crash", "contour", flat())
    pb = cam.PathBuilder(st, op.tool, op)
    pb._add(cam.RAPID, -40, 0, 5, 0)
    pb._add(cam.RAPID, 40, 0, 5, 0)                            # a rapid straight through the uncut stock top
    crash = cam.Program(st); crash.add(op)
    r = cam_sim.simulate(crash)
    assert r.stats["rapid_hits"] and "RAPID" in r.summary().upper()


def test_sim_rotary_finds_too_deep_finish(machines, shaft):
    m = machines["Makera Z1 + 4th axis"]
    st = cam.Setup(m, cam.Stock.cylinder(32, 62, x0=-1), rotary=True)
    p = cam.Program(st)
    p.add(cam.rotary_rough(st, flat(), shaft.shape, stepdown=2.0, mode="rings"))
    p.add(cam.rotary_finish(st, ball(), shaft.shape, mode="spiral", stepover=0.1))
    good = cam_sim.simulate(p, part=shaft)
    assert good.mode == "radial" and good.stats["gouge"]["cells"] == 0
    deep = cam.Program(st)
    deep.add(cam.rotary_finish(st, ball(), shaft.shape, mode="rings", stepover=0.3, leave=-0.5))
    r = cam_sim.simulate(deep, part=shaft)
    assert r.stats["gouge"]["max_depth"] == pytest.approx(0.5, abs=0.1)


def test_sim_refuses_mixed_rotary_and_flat(machines, shaft):
    m = machines["Makera Z1 + 4th axis"]
    rot = cam.Setup(m, cam.Stock.cylinder(32, 62, x0=-1), rotary=True)
    fl = cam.Setup(m, cam.Stock.from_model(shaft, margin=2))
    p = cam.Program(rot)
    p.add(cam.rotary_rough(rot, flat(), shaft.shape, stepdown=3.0, mode="rings"))
    p.add(cam.pocket(fl, flat(), cam.rect(5, -5, 15, 5), z_top=fl.stock.top, z_bottom=fl.stock.top - 1))
    with pytest.raises(cam.CamError, match="separately"):
        cam_sim.simulate(p)
    assert cam_sim.simulate(p, ops=[0]).mode == "radial"


# ------------------------------------------------------------------ per-operation overrides

def test_overrides_apply_by_op_key_and_report_unused(machines, plate):
    st = cam.Setup(machines["Makera Z1"], cam.Stock.from_model(plate, margin=3, top=1))
    t = flat()
    cam.reset_overrides()
    cam.overrides({"Pocket": {"feed": 333, "stepover": 0.2}, "Face#2": {"rpm": 9000}, "Gone": {"feed": 100}})
    p = cam.Program(st)
    p.add(cam.face(st, t, z_top=st.stock.top, z_bottom=st.stock.top - 0.5))
    p.add(cam.face(st, t, z_top=st.stock.top - 0.5, z_bottom=st.stock.top - 1))
    p.add(cam.pocket(st, t, cam.rect(-10, -5, 10, 5), z_top=12, z_bottom=10, stepover=0.5))
    p.unused_overrides = cam.unused_overrides()
    f1, f2, pk = p.ops
    assert [o.key for o in p.ops] == ["Face", "Face#2", "Pocket"]
    assert f1.overrides == {} and f1.tool.rpm == t.rpm
    assert f2.tool.rpm == 9000 and f2.base_tool.rpm == t.rpm
    # the override beats both the tool and the script's explicit stepover=0.5; defaults show what it would have been
    assert pk.tool.feed == 333 and pk.defaults["stepover"] == 0.5 and pk.params["stepover"] == pytest.approx(0.2 * t.diameter, abs=1e-3)
    assert all(m[4] <= 333 for m in pk.moves if m[0] == cam.FEED)
    assert any("'Gone' matches no operation" in w for w in p.check())
    pay = p.to_payload()["ops"][2]
    assert pay["key"] == "Pocket" and pay["overrides"]["feed"] == 333 and pay["base_tool"]["feed"] == t.feed
    with pytest.raises(cam.CamError, match="stepover"):
        cam.overrides({"Pocket": {"stepover": 40}})
    with pytest.raises(cam.CamError, match="unknown field"):
        cam.overrides({"Pocket": {"speed": 4}})
    cam.reset_overrides()


def test_override_fields_per_kind_and_nested_ops(machines, shaft):
    m = machines["Makera Z1 + 4th axis"]
    st = cam.Setup(m, cam.Stock.cylinder(32, 62, x0=-1), rotary=True)
    cam.reset_overrides()
    cam.overrides({"Wrapped": {"feed": 222}, "Drill": {"stepdown": 9}})
    import shapely
    op = cam.rotary_wrap(st, flat(), [shapely.box(10, -3, 20, 3)], depth=0.5)
    assert op.key == "Wrapped" and op.tool.feed == 222 and cam.unused_overrides() == ["Drill"]   # inner pocket not double-counted
    flat_st = cam.Setup(machines["Makera Z1"], cam.Stock.block(20, 20, 5))
    d = cam.drill(flat_st, flat(), [(5, 5)], z_top=5, z_bottom=0)
    assert set(d.defaults) == {"rpm", "plunge"} and d.overrides == {}       # stepdown is not a drill setting
    cam.reset_overrides()


def test_machine_stepdown_cap_in_feeds(machines):
    t = flat()
    z1 = machines["Makera Z1"]
    f = cam.feeds(t, "aluminium", z1)
    assert f["stepdown"] == 0.8 and any("capped" in n for n in f["notes"])
    assert cam.feeds(t, "plywood", z1)["stepdown"] > 0.8                      # only the materials listed are capped
    from dataclasses import replace
    m = replace(z1, max_stepdown={"aluminum": 0.5, "*": 2.0})                # aliases and a catch-all
    assert cam.feeds(t, "alu", m)["stepdown"] == 0.5 and cam.feeds(t, "plywood", m)["stepdown"] == 2.0
    assert cam.feeds(t, "aluminium", replace(z1, max_stepdown={}))["stepdown"] > 0.8
