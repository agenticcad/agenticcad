import pytest

import cad_kernel as ck
import script_edit as se
from conftest import close

PLANE = {"origin": [0, 0, 4], "x_dir": [1, 0, 0], "z_dir": [0, 0, 1], "label": "top face"}
ITEMS = [{"type": "rect", "cx": 5, "cy": 0, "w": 20, "h": 10, "angle": 0, "mode": "add"},
         {"type": "circle", "cx": 5, "cy": 0, "r": 3, "mode": "subtract"},
         {"type": "polygon", "pts": [[-20, -10], [-10, -10], [-15, -2]], "mode": "add"},
         {"type": "slot", "x1": -20, "y1": 8, "x2": -8, "y2": 8, "w": 3, "mode": "add"}]


def test_sketch_block_roundtrip_and_build():
    code = se.set_sketch(ck.DEFAULT_CODE, "sk1", PLANE, ITEMS)
    assert "# sketch:sk1 " in code and code.index("# /sketch:sk1") < code.index("result =")
    defs = se.sketches(code)
    assert defs == [{"name": "sk1", "plane": PLANE, "items": ITEMS, "constraints": []}]
    m = ck.run_script(code)
    assert [s["name"] for s in m.sketches] == ["sk1"]
    sk = m.sketches[0]
    assert sk["faces"] == 3 and close(sk["origin"][2], 4) and close(sk["area"], 200 - 3.14159 * 9 + 40 + (12 * 3 + 3.14159 * 1.5 ** 2), 0.5)
    assert "Sketches" in m.summary(0)
    # replace keeps a single block; removing drops it and the model no longer has sketches
    code2 = se.set_sketch(code, "sk1", PLANE, ITEMS[:1])
    assert code2.count("# sketch:sk1") == 1 and ck.run_script(code2).sketches[0]["faces"] == 1
    code3 = se.remove_sketch(code2, "sk1")
    assert "sk1" not in code3 and ck.run_script(code3).sketches == []
    with pytest.raises(se.Unsupported):
        se.remove_sketch(code3, "sk1")
    with pytest.raises(se.Refused):
        se.set_sketch(code3, "bad name", PLANE, [])


def test_sketch_usable_by_extrude_in_script():
    code = se.set_sketch("base = Box(60, 40, 8)\nresult = base\n", "sk1", PLANE, ITEMS[:2])
    code = code.replace("result = base", "boss = extrude(sk1, amount=6)\ncut = extrude(sk1, amount=-8)\nresult = base + boss\n")
    m = ck.run_script(code)
    assert close(m.bbox_max[2], 10) and len(m.bodies) == 1
    assert close(m.volume, 60 * 40 * 8 + (200 - 3.14159 * 9) * 6, 1.0)


def test_sketch_on_tilted_plane():
    plane = {"origin": [0, 0, 0], "x_dir": [1, 0, 0], "z_dir": [0, -0.7071, 0.7071], "label": "tilted"}
    code = se.set_sketch("result = Box(10, 10, 10)\n", "s", plane, [{"type": "circle", "cx": 0, "cy": 0, "r": 4, "mode": "add"}])
    m = ck.run_script(code)
    n = m.sketches[0]["normal"]
    assert close(abs(n[1]), 0.7071, 1e-3) and close(abs(n[2]), 0.7071, 1e-3)


def test_wrap_body_expr_join_cut_transform():
    rect = [{"type": "rect", "cx": 20, "cy": 0, "w": 12, "h": 10, "angle": 0, "mode": "add"}]   # clear of boss and holes
    code = se.set_sketch(ck.DEFAULT_CODE, "sk1", PLANE, rect)                                    # on the plate top (z=4)
    base = ck.run_script(code).body_by_name("Bracket").volume
    assert se.body_expr(code, "Bracket") == "bracket.part"
    joined = ck.run_script(se.wrap_body_expr(code, "Bracket", "({expr}) + extrude(sk1, amount=6)")).body_by_name("Bracket")
    assert close(joined.bbox_max[2], 24) and close(joined.volume - base, 12 * 10 * 6, 0.5)
    cut = ck.run_script(se.wrap_body_expr(code, "Bracket", "({expr}) - extrude(sk1, amount=-8)")).body_by_name("Bracket")
    assert close(base - cut.volume, 12 * 10 * 8, 0.5)
    moved = ck.run_script(se.wrap_body_expr(code, "Pin", "Pos(10, 0, 0) * Rot(0, 0, 45) * ({expr})")).body_by_name("Pin")
    assert close((moved.bbox_min[0] + moved.bbox_max[0]) / 2, 10, 0.05)
    with pytest.raises(se.Unsupported):
        se.wrap_body_expr("result = Box(1,1,1)", "Body1", "({expr})")


def test_wrap_body_hoists_when_template_uses_body():
    code = ck.DEFAULT_CODE
    out = se.wrap_body_expr(code, "Bracket", "{body}.fillet(1, [{body}.edges().sort_by_distance((30, 20, 4))[0]])")
    assert "_bracket = bracket.part\n" in out and '"Bracket": _bracket.fillet(1, [_bracket.edges()' in out
    m = ck.run_script(out)
    assert len(m.body_by_name("Bracket").shape.faces()) > 17        # fillet faces added
    # a second wrap on the same body reuses the existing name (no re-hoist)
    out2 = se.wrap_body_expr(out, "Bracket", "{body} - Pos(0, 0, 0) * Box(1, 1, 1)")
    assert out2.count("_bracket = ") == 1


def test_toolbar_ops_generate_valid_code():
    """The same code the toolbar writes: press-pull, hole, tapped hole, chamfer of a face's edges, shell."""
    code = ck.DEFAULT_CODE
    base = ck.run_script(code).body_by_name("Bracket")
    top = "(9, 0, 24)"     # a point ON the boss top annulus (r 6..12), as the UI would send
    pull = se.wrap_body_expr(code, "Bracket", f"{{body}} + extrude({{body}}.faces().sort_by_distance({top})[0], amount=5, dir=(0, 0, 1))")
    assert close(ck.run_script(pull).body_by_name("Bracket").bbox_max[2], 29)
    push = se.wrap_body_expr(code, "Bracket", f"{{body}} - extrude({{body}}.faces().sort_by_distance({top})[0], amount=3, dir=(0, 0, -1))")
    assert close(ck.run_script(push).body_by_name("Bracket").bbox_max[2], 21)
    h = se.wrap_body_expr(code, "Bracket", "hole({body}, 5, at=(24, 0, 4), through=True, axis=(0, 0, -1), counterbore=(9, 3))")
    hm = ck.run_script(h).body_by_name("Bracket")
    assert close(base.volume - hm.volume, 3.14159 * 2.5 ** 2 * 8 + 3.14159 * (4.5 ** 2 - 2.5 ** 2) * 3, 2.0)
    t = se.wrap_body_expr(code, "Bracket", 'tap({body}, "M4", at=(-24, 0, 4), depth=6, axis=(0, 0, -1))')
    assert ck.run_script(t).threads[0].label() == "M4×0.7 ↧6"
    ch = se.wrap_body_expr(code, "Bracket", "{body}.chamfer(1, None, [*{body}.faces().sort_by_distance((9, 0, 24))[0].edges()])")
    assert len(ck.run_script(ch).body_by_name("Bracket").shape.faces()) > 17
    sh = se.wrap_body_expr("result = {\"Cup\": Box(30, 30, 20)}", "Cup", "offset({body}, amount=-2, openings=[{body}.faces().sort_by_distance((0, 0, 10))[0]])")
    assert close(ck.run_script(sh).volume, 30 * 30 * 20 - 26 * 26 * 18, 1.0)


def test_profile_of_lines_and_arcs():
    """A profile item: lines and three-point arcs closed back to the start (here a 20 x 10 slot-like D shape with a
    semicircular end bulging +X, minus a rectangular window drawn as a line profile)."""
    D = {"type": "profile", "start": [-10, -5], "segs": [{"to": [10, -5]}, {"to": [10, 5], "via": [15, 0]}, {"to": [-10, 5]}], "mode": "add"}
    win = {"type": "profile", "start": [-5, -2], "segs": [{"to": [5, -2]}, {"to": [5, 2]}, {"to": [-5, 2]}, {"to": [-5, -2]}], "mode": "subtract"}
    code = se.set_sketch("result = {}\n", "sk", PLANE, [D, win])
    assert "ThreePointArc((10, -5), (15, 0), (10, 5))" in code and "make_face(mode=Mode.SUBTRACT)" in code
    assert code.count("Line((-10, 5), (-10, -5))") == 1                          # closed back to the start automatically
    assert se.sketches(code)[0]["items"] == [D, win]
    m = ck.run_script(code)
    assert close(m.sketches[0]["area"], 20 * 10 + 3.14159265 * 25 / 2 - 10 * 4, 1e-3)
    # an arc with a collinear through-point is a line; zero-length segments are dropped
    flat = {"type": "profile", "start": [0, 0], "segs": [{"to": [10, 0], "via": [5, 0]}, {"to": [10, 0]}, {"to": [10, 10]}, {"to": [0, 10]}], "mode": "add"}
    c2 = se.set_sketch("result = {}\n", "s2", PLANE, [flat])
    assert "ThreePointArc" not in c2 and close(ck.run_script(c2).sketches[0]["area"], 100, 1e-6)
    # a profile that can't enclose an area is refused before it reaches the kernel
    with pytest.raises(se.Refused):
        se.set_sketch("result = {}\n", "s3", PLANE, [{"type": "profile", "start": [0, 0], "segs": [{"to": [10, 0]}], "mode": "add"}])
    with pytest.raises(se.Unsupported, match="profile"):
        se.set_sketch("result = {}\n", "s4", PLANE, [{"type": "profile", "segs": []}])


# ------------------------------------------------------------------ constraints
import sketch_solver as SS  # noqa: E402

BOX = lambda: [{"type": "profile", "start": [0.3, -0.2], "segs": [{"to": [39, 1]}, {"to": [41, 19]}, {"to": [1, 21.5]}, {"to": [0.3, -0.2]}], "mode": "add"}]
P = lambda k, i=0: {"i": i, "p": k}
E = lambda k, i=0: {"i": i, "e": k}


def test_solver_squares_up_a_profile_and_counts_freedom():
    items = BOX()
    hv = [{"type": "fix", "refs": [P("s")], "value": [0, 0]}, {"type": "horizontal", "refs": [E(0)]}, {"type": "vertical", "refs": [E(1)]},
          {"type": "horizontal", "refs": [E(2)]}, {"type": "vertical", "refs": [E(3)]}]
    r = SS.solve(items, hv)
    assert r["ok"] and r["dof"] == 2                                      # width and height still free
    r = SS.solve(items, hv + [{"type": "length", "refs": [E(0)], "value": 40}, {"type": "length", "refs": [E(1)], "value": 20}])
    assert r["ok"] and r["dof"] == 0 and not any(r["free"])
    assert items[0]["segs"][1]["to"] == [40, 20] and items[0]["segs"][3]["to"] == items[0]["start"] == [0, 0]


def test_tangent_concentric_equal_angle_and_distance():
    items = BOX() + [{"type": "circle", "cx": 7, "cy": 6, "r": 4, "mode": "subtract"}, {"type": "circle", "cx": 30, "cy": 12, "r": 2, "mode": "subtract"}]
    cons = [{"type": "fix", "refs": [P("s")], "value": [0, 0]}, {"type": "horizontal", "refs": [E(0)]}, {"type": "vertical", "refs": [E(3)]},
            {"type": "angle", "refs": [E(0), E(1)], "value": 90}, {"type": "parallel", "refs": [E(0), E(2)]},
            {"type": "length", "refs": [E(0)], "value": 40}, {"type": "length", "refs": [E(3)], "value": 20},
            {"type": "tangent", "refs": [E(0), {"i": 1}]}, {"type": "tangent", "refs": [E(3), {"i": 1}]}, {"type": "radius", "refs": [{"i": 1}], "value": 5},
            {"type": "equal", "refs": [{"i": 1}, {"i": 2}]}, {"type": "hdistance", "refs": [P("c", 1), P("c", 2)], "value": 25},
            {"type": "vdistance", "refs": [P("c", 1), P("c", 2)], "value": 8}]
    r = SS.solve(items, cons)
    assert r["ok"] and r["dof"] == 0
    assert close(items[1]["cx"], 5) and close(items[1]["cy"], 5) and close(items[2]["r"], 5) and close(items[2]["cx"], 30) and close(items[2]["cy"], 13)
    assert close(items[0]["segs"][1]["to"][0], 40) and close(items[0]["segs"][1]["to"][1], 20)


def test_arcs_points_on_edges_and_midpoints():
    items = [{"type": "profile", "start": [0, 0], "segs": [{"to": [20, 0]}, {"to": [21, 10], "via": [26, 5]}, {"to": [0, 10]}], "mode": "add"},
             {"type": "circle", "cx": 9, "cy": 2, "r": 1, "mode": "subtract"}]
    cons = [{"type": "fix", "refs": [P("s")], "value": [0, 0]}, {"type": "horizontal", "refs": [E(0)]}, {"type": "horizontal", "refs": [E(2)]},
            {"type": "vertical", "refs": [P("e0"), P("e1")]}, {"type": "tangent", "refs": [E(0), E(1)]}, {"type": "radius", "refs": [E(1)], "value": 5},
            {"type": "length", "refs": [E(0)], "value": 20}, {"type": "midpoint", "refs": [P("c", 1), E(0)]}]
    r = SS.solve(items, cons)
    assert r["ok"]
    c, rad = SS.circle(items, E(1))
    assert close(rad, 5, 1e-5) and close(c[0], 20, 1e-5) and close(c[1], 5, 1e-5)          # a semicircle end on the bar
    assert close(items[1]["cx"], 10) and close(items[1]["cy"], 0)                             # the hole sits on the bottom edge's midpoint


def test_conflicts_are_refused_and_saved_constraints_build():
    items = BOX()
    cons = [{"type": "horizontal", "refs": [E(0)]}, {"type": "length", "refs": [E(0)], "value": 40}, {"type": "length", "refs": [E(0)], "value": 30}]
    with pytest.raises(SS.SketchConstraintError, match="conflict"):
        SS.apply(items, cons)
    with pytest.raises(se.Refused, match="conflict"):
        se.set_sketch("result = {}\n", "s", PLANE, BOX(), cons)
    ok = [{"type": "fix", "refs": [P("s")], "value": [0, 0]}, {"type": "horizontal", "refs": [E(0)]}, {"type": "vertical", "refs": [E(1)]},
          {"type": "horizontal", "refs": [E(2)]}, {"type": "vertical", "refs": [E(3)]},
          {"type": "length", "refs": [E(0)], "value": 40}, {"type": "length", "refs": [E(1)], "value": 20}]
    code = se.set_sketch("result = {}\n", "s", PLANE, BOX(), ok)
    d = se.sketches(code)[0]
    assert len(d["constraints"]) == 7 and d["items"][0]["segs"][1]["to"] == [40, 20]
    assert close(ck.run_script(code).sketches[0]["area"], 800, 1e-6)
    with pytest.raises(se.Refused, match="item 5"):
        se.set_sketch("result = {}\n", "s", PLANE, BOX(), [{"type": "horizontal", "refs": [E(0, 5)]}])
