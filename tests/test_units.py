"""Display units (mm / in, auto from the timezone), inch-aware parameters, drawings in inches, Unified threads."""
import re
import time

import pytest

import cad_kernel as ck
import drawing
import script_edit as se
import threads as thr
import units as un


def test_units_from_timezone():
    assert un.units_for_timezone("America/New_York") == "in" and un.units_for_timezone("America/Indiana/Indianapolis") == "in"
    assert un.units_for_timezone("Pacific/Honolulu") == "in" and un.units_for_timezone("US/Pacific") == "in"
    assert un.units_for_timezone("Australia/Sydney") == "mm" and un.units_for_timezone("Europe/Berlin") == "mm"
    assert un.units_for_timezone("America/Toronto") == "mm" and un.units_for_timezone("America/Mexico_City") == "mm"
    assert un.units_for_timezone("", ("Eastern Standard Time", "Eastern Daylight Time")) == "in"     # Windows names
    assert un.units_for_timezone("", ("AUS Eastern Standard Time", "AUS Eastern Daylight Time")) == "mm"
    assert un.units_for_timezone("", ("PST", "PDT")) == "in" and un.units_for_timezone("", ("CST", "CST")) == "mm"   # China's CST has no DST
    assert un.units_for_timezone("", ("AEST", "AEDT")) == "mm"
    u, reason = un.detect_default_units()
    assert u in ("mm", "in") and reason.startswith("timezone")
    assert un.fmt_len(25.4, "in") == "1 in" and un.fmt_len(12.7, "in") == "0.5 in" and un.fmt_len(12.7, "mm") == "12.7 mm"


def test_inch_parameters_round_trip():
    code = "plate_l = 2.5 * inch   # length\nplate_t = 6\nhole_d = inch * 0.25\nresult = Box(plate_l, 40, plate_t)\n"
    ps = se.params(code)
    assert [(p["name"], p["value"], p["unit"]) for p in ps] == [("plate_l", 2.5, "in"), ("plate_t", 6, "mm"), ("hole_d", 0.25, "in")]
    out = se.set_params(code, {"plate_l": 3, "plate_t": 8})
    assert "plate_l = 3.0 * inch   # length" in out and "plate_t = 8" in out and "hole_d = inch * 0.25" in out
    m = ck.run_script(out, "draft")
    assert m.bodies[0].bbox_max[0] - m.bodies[0].bbox_min[0] == pytest.approx(76.2, abs=0.01)     # 3 in
    assert ck.run_script("result = Box(1 * ft, 1 * inch, 10 * thou + 5 * mm)", "draft").bodies[0].bbox_max[0] == pytest.approx(152.4, abs=0.01)


def test_unified_thread_tables_and_hardware():
    d = thr.thread("1/4-20")
    assert d["system"] == "un" and d["label"] == "1/4-20 UNC" and d["tpi"] == 20 and d["major"] == pytest.approx(6.35)
    assert d["pitch"] == pytest.approx(1.27) and d["tap_drill"] == pytest.approx(0.201 * 25.4, abs=0.01)
    assert d["clearance"]["medium"] == pytest.approx(0.266 * 25.4, abs=0.01)
    assert thr.thread("#10-32")["label"] == "#10-32 UNF" and thr.thread("10-32")["size"] == "#10-32"
    assert thr.thread("3/8")["size"] == "3/8-16" and thr.thread(".375-16")["size"] == "3/8-16" and thr.thread('1/4"-20 UNC')["size"] == "1/4-20"
    assert thr.thread("M4")["system"] == "iso" and thr.iso is thr.thread
    for bad in ("1/4-24", "9/16-12", "M7"):
        with pytest.raises(thr.ThreadError):
            thr.thread(bad)
    m = ck.run_script('''
plate = Box(60, 40, 10)
plate = tap(plate, "1/4-20", at=(15, 0, 5), depth=8)
plate = tap(plate, "M6", at=(-15, 0, 5), through=True)
b = Pos(0, 15, 5) * bolt("3/8-16", 20, head="hex")
n = Pos(0, -15, 5) * nut("#10-32")
w = Pos(25, -15, 5) * washer("1/2-13")
s = Pos(-25, -15, 5) * bolt("#8-32", 12, head="socket")
result = {"Plate": plate, "Bolt": b, "Nut": n, "Washer": w, "Screw": s}
''', "draft")
    assert len(m.bodies) == 5
    labels = [t.label() for t in m.threads]
    assert "1/4-20 UNC ↧8" in labels and "M6×1 THRU" in labels
    assert [t.label("in") for t in m.threads if t.size == "1/4-20"] == ["1/4-20 UNC ↧0.315"]
    bolt = m.body_by_name("Bolt"); af = 0.5625 * 25.4
    assert bolt.bbox_max[0] - bolt.bbox_min[0] == pytest.approx(af / (3 ** 0.5 / 2), rel=0.02)     # hex across corners
    wash = m.body_by_name("Washer")
    assert wash.bbox_max[0] - wash.bbox_min[0] == pytest.approx(1.062 * 25.4, abs=0.05)
    # tap-drill hole of the 1/4-20 tap is present in the plate
    assert any(abs(f.radius - 0.201 * 25.4 / 2) < 0.05 for f in m.faces if f.kind == "CYLINDER" and f.body_name == "Plate")


def test_drawing_in_inches(tmp_path):
    m = ck.run_script('plate = Box(2 * inch, 1 * inch, 0.25 * inch)\nplate = tap(plate, "1/4-20", at=(0, 0, 0.125 * inch), through=True)\nresult = {"Plate": plate}\n', "draft")
    res = drawing.drawings_for_model(m, tmp_path, "imp", units="in")
    svg = open(res[0]["svg"]).read()
    assert "in · third angle" in svg and ">2<" in svg and ">1<" in svg and "1/4-20 UNC" in svg and "2 × 1 × 0.25 in" in svg
    res_mm = drawing.drawings_for_model(m, tmp_path, "met", units="mm")
    svg_mm = open(res_mm[0]["svg"]).read()
    assert "mm · third angle" in svg_mm and ">50.8<" in svg_mm
