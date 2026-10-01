"""Design Kit: built-in components and guides, search, the generated TOC, the workspace layer and script use."""
import json
import re
from pathlib import Path

import pytest

import cad_kernel as ck
from designkit import CATEGORIES, Kit, KitError

ROOT = Path(__file__).resolve().parents[1]


def test_builtin_entries_are_complete_and_links_resolve():
    k = Kit(None)
    es = k.entries()
    ids = {e.id for e in es}
    assert len([e for e in es if e.kind == "component"]) >= 25 and len([e for e in es if e.kind == "guide"]) >= 8
    for e in es:
        assert e.category in CATEGORIES, e.id
        assert e.summary and len(e.summary) < 160, e.id
        for r in e.related:
            assert r in ids, f"{e.id} links to missing {r}"


def test_checked_in_toc_matches_the_metadata():
    """designkit/TOC.md is generated; regenerate with Kit(None).toc(layer='builtin') when entries change."""
    assert (ROOT / "designkit" / "TOC.md").read_text() == Kit(None).toc(layer="builtin")


def test_search_ranks_the_obvious_entry_first():
    k = Kit(None)
    top = lambda q: k.search(q)[0].id  # noqa: E731
    assert top("688 bearing") == "ball_bearing"
    assert top("planetary gearbox") == "planetary_stage"
    assert top("circlip for a bore") in ("circlip_internal", "circlip_groove_internal")
    assert top("nema17 stepper") == "nema_stepper"
    assert top("socket head cap screw") == "socket_screw"
    assert k.search("688")[0].id == "ball_bearing" and all(e.id != "parallel_key" for e in k.search("688"))
    assert [e.kind for e in k.search("clearance", kind="guide")] and all(e.kind == "guide" for e in k.search("clearance", kind="guide"))


def test_read_gives_signature_example_and_guide_text():
    k = Kit(None)
    comp = k.read("ball_bearing")
    assert "kit.ball_bearing(designation" in comp and "Example:" in comp and "688ZZ" in comp
    guide = k.read("transmission/planetary-gear-sets")
    assert "z_ring = z_sun + 2 * z_planet" in guide and not guide.startswith("---")
    assert k.read("planetary-gear-sets").startswith("# Planetary gear sets")          # short id works when unique
    with pytest.raises(KitError):
        k.read("no-such-thing")


def test_scripts_call_the_kit_and_stay_builder_safe():
    m = ck.run_script('''
with BuildPart() as bp:
    Box(40, 40, 6)
    b = kit.ball_bearing("608")
result = {"Plate": bp.part, "Bearing": kit.place(b, Pos(0, 0, 10)), "Screw": Pos(15, 15, 3) * kit.socket_screw("M3", 10)}
''')
    assert abs(m.body_by_name("Plate").volume - 40 * 40 * 6) < 1e-6
    names = [b.path for b in m.bodies]
    assert "Bearing/InnerRing" in names and "Bearing/Cage" in names and sum("Ball" in n for n in names) >= 6
    assert abs(m.body_by_name("Bearing/InnerRing").bbox_min[2] - 10) < 1e-6


def test_unknown_component_names_suggest_alternatives():
    with pytest.raises(ck.CadError, match="similar: .*ball_bearing"):
        ck.run_script('result = kit.bearing("608")')


def test_workspace_layer_notes_guides_parts_usage_and_toc(tmp_path):
    k = Kit(tmp_path)
    assert k.note("ball_bearing", "use the ZZ width for 688") == "ball_bearing"
    assert "use the ZZ width for 688" in k.read("ball_bearing")
    gid = k.save_guide("Snap fits", "Snap fits for PETG", "enclosures", "Cantilever snap-fit sizing for PETG",
                       "Strain under 2 %.", tags=["snap fit", "petg"])
    assert gid == "enclosures/snap-fits" and k.search("petg snap")[0].id == gid
    names = k.save_part("brackets", '''
@component("structural", "L bracket with two holes", tags=["bracket"])
def l_bracket(leg: float = 30, t: float = 4):
    """An L bracket."""
    return Box(leg, 20, t) + Pos(-leg / 2 + t / 2, 0, leg / 2) * Box(t, 20, leg)
''')
    assert names == ["l_bracket"]
    m = ck.run_script("result = {'B': kit.l_bracket(40)}", workspace=tmp_path)
    assert m.bodies and abs(m.bodies[0].bbox_max[0] - m.bodies[0].bbox_min[0] - 40) < 1e-6
    assert k.record_usage("a = kit.l_bracket(40)\nb = kit.ball_bearing('608')\nc = kit.nope(1)") == ["ball_bearing", "l_bracket"]
    usage = json.loads((tmp_path / "kit" / "usage.json").read_text())
    assert usage["l_bracket"]["count"] == 1 and usage["ball_bearing"]["reads"] >= 1
    toc = (tmp_path / "kit" / "TOC.md").read_text()
    assert "## Most used here" in toc and "`l_bracket`" in toc and "`enclosures/snap-fits`" in toc
    with pytest.raises(KitError, match="built-in"):
        k.save_part("clash", '@component("fasteners", "x")\ndef socket_screw():\n    return Box(1, 1, 1)\n')
    with pytest.raises(KitError, match="no components"):
        k.save_part("empty", "x = 1\n")


def test_a_broken_workspace_part_does_not_break_designs(tmp_path):
    (tmp_path / "kit" / "parts").mkdir(parents=True)
    (tmp_path / "kit" / "parts" / "bad.py").write_text("raise RuntimeError('boom')\n")
    m = ck.run_script("result = {'S': kit.socket_screw('M3', 8)}", workspace=tmp_path)
    assert m.bodies
    assert any("FAILED TO LOAD" in e.summary for e in Kit(tmp_path).entries())


@pytest.mark.parametrize("call", [
    'kit.socket_screw("M3", 16)', 'kit.button_head_screw("M4", 10)', 'kit.countersunk_screw("M3", 12)',
    'kit.set_screw("M3", 4)', 'kit.set_screw("M4", 6, point="cone")', 'kit.hex_bolt("M6", 40)', 'kit.hex_nut("M6")',
    'kit.washer("M6")', 'kit.ball_bearing("6204-2RS")', 'kit.ball_bearing("MR63ZZ")', 'kit.ball_bearing("6001", detail="simple")',
    'kit.circlip_external(8)', 'kit.circlip_internal(16)', 'kit.e_clip(8)', 'kit.parallel_key(20, 25)', 'kit.shaft_collar(8)',
    'kit.dowel_pin(4, 12)', 'kit.ring_gear(1, 48, 6)', 'kit.nema_stepper(17, 40)', 'kit.nema_stepper(23, 56)',
    'kit.servo("SG90")', 'kit.servo("MG996R")', 'kit.stepper_28byj48()', 'kit.n20_gearmotor()',
    'kit.arduino_uno()', 'kit.raspberry_pi_4()', 'kit.pi_pico()', 'kit.fan(40)', 'kit.fan(120)', 'kit.cell_18650()',
    'kit.extrusion("2020", 100)', 'kit.extrusion("2040", 60)', 'kit.extrusion("4080", 30)', 'kit.corner_bracket(20)',
    'kit.t_nut(20)', 'kit.gt2_pulley(20)', 'kit.gt2_pulley(36, bore=8)', 'kit.lead_screw_t8(100)', 'kit.rigid_coupler(5, 8)',
    'kit.jaw_coupling(5, 8)', 'kit.heat_set_insert("M3")', 'kit.standoff("M2.5", 6, kind="MF")', 'kit.enclosure(90, 60, 30)',
    'kit.screw_boss("M3", 12)', 'kit.snap_fit_hook(10, 1.6, 6, 0.8)',
    'kit.compression_spring(1.0, 10, 30, coils=8)', 'kit.extension_spring(1.0, 8, 20)', 'kit.knob(25, 16, shaft_d=6)',
    'kit.pull_handle(96)', 'kit.disc_cam(20, 8)', 'kit.link_bar(60)', 'kit.hinge(40, 20, angle=90)', 'kit.o_ring(20, 2)',
    'kit.tube(6, 4, 50)', 'kit.push_fit_fitting(6, "1/8 BSP")', 'kit.hose_barb(6, "1/8 BSP")',
])
def test_components_build_valid_single_solid_parts(call):
    m = ck.run_script(f"result = {{'X': {call}}}")
    assert m.bodies and all(b.shape.is_valid and len(b.shape.solids()) == 1 for b in m.bodies)


def test_detailed_assemblies_have_no_interference():
    kit = Kit(None).namespace()

    def flat(o, pre=""):
        if isinstance(o, dict):
            for k2, v in o.items():
                yield from flat(v, f"{pre}{k2}/")
        else:
            yield pre.rstrip("/"), o
    for parts in (kit.ball_bearing("688ZZ"), kit.planetary_stage(1.0, 13, 17, 3, face_width=4),
                  kit.nema_stepper(17, 40, detail="full"), kit.jaw_coupling(5, 8), kit.enclosure(60, 40, 20),
                  kit.servo("MG996R"), kit.stepper_28byj48(), kit.n20_gearmotor(), kit.fan(60), kit.raspberry_pi_4(),
                  kit.hinge(40, 20, angle=120), kit.knob(25, 16), kit.push_fit_fitting(6, "1/8 BSP")):
        items = list(flat(parts))
        for i, (na, a) in enumerate(items):
            for nb, b in items[i + 1:]:
                if any(w in na + nb for w in ("Screw", "Insert")):   # cosmetic threads sit in tap-drill holes
                    continue
                try:
                    x = a & b
                    v = x.volume if x is not None else 0.0
                except ValueError:
                    v = 0.0
                assert v < 0.01, (na, nb, v)


def test_boards_put_mounting_holes_at_datasheet_coordinates():
    kit = Kit(None).namespace()
    for board, holes, d in ((kit.arduino_uno(), [(13.97, 2.54), (15.24, 50.8), (66.04, 7.62), (66.04, 35.56)], 3.2),
                            (kit.raspberry_pi_4(), [(3.5, 3.5), (61.5, 3.5), (3.5, 52.5), (61.5, 52.5)], 2.7)):
        import cam_kernel as cam
        found = cam.holes(board["PCB"], d - 0.05, d + 0.05)
        assert sorted((round(h.x, 2), round(h.y, 2)) for h in found) == sorted(holes)


def test_cutters_subtract_cleanly():
    m = ck.run_script('''
panel = Box(60, 60, 3)
panel = panel - Pos(0, 0, -1.5) * kit.fan_grille_cutter(40, depth=5) - Pos(0, 25, 0) * kit.vent_slots_cutter(2, 20, 2.5, 4)
boss_plate = Box(20, 20, 4) - Pos(0, 0, 2) * kit.insert_hole_cutter("M3")
result = {"Panel": panel, "Boss": Pos(50, 0, 0) * boss_plate}
''')
    assert all(b.shape.is_valid for b in m.bodies) and m.body_by_name("Panel").volume < 60 * 60 * 3 * 0.8


def test_spring_geometry_matches_its_parameters():
    kit = Kit(None).namespace()
    s = kit.compression_spring(1.0, 10, 30, coils=8)
    zs = [p.Z for p in s.tessellate(0.01)[0]]                      # B-spline bounding boxes are loose; use the mesh
    assert abs(min(zs)) < 1e-3 and abs(max(zs) - 30) < 1e-3        # ground flat at both ends
    assert sorted(round(f.center().Z, 1) for f in s.faces() if f.geom_type.name == "PLANE")[0] == 0.0
    wire_len = 3.14159 * 9 * 8                                   # ~ π · D · coils (pitch adds a little)
    assert 0.95 * wire_len * 3.14159 * 0.25 < s.volume < 1.1 * wire_len * 3.14159 * 0.25
    with pytest.raises(ValueError):
        kit.compression_spring(1.0, 10, 8, coils=8)              # below solid height


def test_o_ring_groove_squeeze():
    kit = Kit(None).namespace()
    face = kit.o_ring_groove_cutter(2.0, 40, "face")
    assert abs(face.bounding_box().size.Z - 1.5) < 0.02            # 25 % squeeze on a 2 mm section
    piston = kit.o_ring_groove_cutter(2.0, 20, "piston")
    import math
    r_bottom = min(math.hypot(v.X, v.Y) for v in piston.vertices())
    assert abs(r_bottom - (10 - 1.6)) < 0.02                         # 0.8 × CS radial depth


def test_catalogue_does_not_depend_on_import_order():
    import subprocess, sys
    code = ("import sys; sys.path.insert(0, '.');"
            "import designkit.components.mechanisms, designkit.components.enclosures;"
            "from designkit import Kit; ids = {e.id for e in Kit(None).entries()};"
            "assert {'compression_spring', 'enclosure', 'socket_screw', 'spur_gear'} <= ids, sorted(ids)")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-500:]
