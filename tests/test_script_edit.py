import pytest

import cad_kernel as ck
import script_edit as se


def test_params_lists_numeric_assignments_including_tuples():
    ps = se.params(ck.DEFAULT_CODE)
    names = [p["name"] for p in ps]
    assert names[:6] == ["plate_l", "plate_w", "plate_t", "boss_d", "boss_h", "bore_d"]
    assert {p["name"]: p["value"] for p in ps}["boss_h"] == 20


def test_params_ignores_non_numeric_and_reads_comments():
    code = "a = 5      # width\nb = 'x'\nc = a + 1\nd = -2.5\nresult = Box(a, a, a)\n"
    ps = se.params(code)
    assert [(p["name"], p["value"], p["comment"]) for p in ps] == [("a", 5, "width"), ("d", -2.5, "")]


def test_set_params_rewrites_in_place_keeping_int_float_style():
    code = "a = 5\nb, c = 2.5, 10\nresult = Box(a, b, c)\n"
    out = se.set_params(code, {"a": 7, "b": 3, "c": 12.5})
    assert out.splitlines()[:2] == ["a = 7", "b, c = 3.0, 12.5"]
    assert ck.run_script(out).bbox_max[0] == 3.5


def test_set_params_unknown_names_are_refused():
    code = "a = 1\nresult = Box(a, a, a)\n"
    with pytest.raises(se.Refused, match="zzz"):
        se.set_params(code, {"zzz": 2})            # silently ignoring a typo would mislead the agent
    assert "a = 3" in se.set_params(code, {"a": 3})

def test_rename_and_delete_dict_bodies():
    code = ck.DEFAULT_CODE
    renamed = se.rename(code, "Pin", "Dowel")
    assert '"Dowel": pin' in renamed
    m = ck.run_script(renamed)
    assert [b.path for b in m.bodies] == ["Bracket", "Dowel"]
    deleted = se.delete(renamed, "Dowel")
    assert [b.path for b in ck.run_script(deleted).bodies] == ["Bracket"]


def test_rename_nested_component_and_multiline_delete():
    code = 'a = Box(1,1,1)\nb = Box(2,2,2)\nc = Box(3,3,3)\nresult = {\n    "Asm": {\n        "A": a,\n        "B": b,\n    },\n    "Loose": c,\n}\n'
    out = se.delete(code, "Asm/A")
    assert '"A": a' not in out and ck.run_script(out).bodies[0].path == "Asm/B"
    out = se.rename(code, "Asm", "Assembly")
    assert [b.path for b in ck.run_script(out).bodies][0] == "Assembly/A"


def test_refusals():
    with pytest.raises(se.Refused):
        se.delete('result = {"A": Box(1,1,1)}', "A")
    with pytest.raises(se.Refused):
        se.rename(ck.DEFAULT_CODE, "Pin", "Bracket")
    with pytest.raises(se.Unsupported):
        se.rename("result = Box(1,1,1)", "Body1", "X")
    with pytest.raises(se.Unsupported):
        se.rename(ck.DEFAULT_CODE, "Nope", "X")


def test_add_body_into_dict_and_plain_result():
    out = se.add_body(ck.DEFAULT_CODE, "washer", "Pos(0, 0, 24) * Cylinder(10, 2)", "Washer")
    assert [b.path for b in ck.run_script(out).bodies] == ["Bracket", "Pin", "Washer"]
    out = se.add_body("result = Box(10,10,10)", "pin", "Cylinder(2, 30)", "Pin")
    assert [b.path for b in ck.run_script(out).bodies] == ["Body1", "Pin"]
    out = se.add_body("x = 1\n", "p", "Box(1,1,1)", "P")   # no result at all
    assert ck.run_script(out).bodies[0].path == "P"
