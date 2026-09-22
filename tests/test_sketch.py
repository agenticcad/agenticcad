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
    assert defs == [{"name": "sk1", "plane": PLANE, "items": ITEMS}]
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
