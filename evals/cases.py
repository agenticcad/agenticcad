"""Eval cases: prompt + deterministic graders. Add a case = add a Case here."""
from __future__ import annotations

import base64
import io
import math

import cad_kernel as ck
import cam_kernel as cam
from harness import (Case, Run, check, expect_answer, expect_bbox, expect_bodies, expect_face_kinds, expect_gcode,
                     expect_holes, expect_op, expect_params, expect_program, expect_script_contains, expect_tools_used,
                     expect_volume, no_agent_error)

import script_edit

SKETCHED = script_edit.set_sketch('''plate_l, plate_w, plate_t = 60, 40, 6
with BuildPart() as bp:
    Box(plate_l, plate_w, plate_t)
result = {"Plate": bp.part}
''', "sk1", {"origin": [0, 0, 3], "x_dir": [1, 0, 0], "z_dir": [0, 0, 1], "label": "plate top"},
    [{"type": "rect", "cx": 10, "cy": 0, "w": 20, "h": 12, "angle": 0, "mode": "add"},
     {"type": "circle", "cx": 10, "cy": 0, "r": 3, "mode": "subtract"}])


def _bc(b):
    return ((b.bbox_min[0] + b.bbox_max[0]) / 2, (b.bbox_min[1] + b.bbox_max[1]) / 2)


def thread_checks(run: Run):
    m = run.model
    if m is None:
        return [check("threads", False, "no model")]
    labels = sorted(t.label() for t in m.threads)
    hs = cam.holes(m.shape)
    tap = [h for h in hs if abs(h.diameter - 3.3) < 0.1]
    clr = [h for h in hs if abs(h.diameter - 4.5) < 0.15 or abs(h.diameter - 4.3) < 0.15]
    return [check("2 registered M4 threads", labels == ["M4×0.7 THRU", "M4×0.7 THRU"] or len([l for l in labels if l.startswith("M4")]) == 2, str(labels)),
            check("tap-drill holes Ø3.3 ×2", len(tap) == 2, f"holes {hs}"),
            check("2 clearance holes for M4", len(clr) == 2, f"holes {hs}")]


def sketch_used(run: Run):
    m = run.model
    if m is None:
        return [check("sketch", False, "no model")]
    defs = script_edit.sketches(m.code)
    return [check("sketch block preserved", [d["name"] for d in defs] == ["sk1"] and len(defs[0]["items"]) == 2, str(defs)),
            check("script uses extrude(sk1", "extrude(sk1" in m.code, ""),
            check("boss volume added (≈ 1270 mm³)", abs(m.volume - (60 * 40 * 6 + (20 * 12 - math.pi * 9) * 6)) < 60, f"{m.volume:.0f}")]


PLATE = '''# 60x40x6 plate
plate_l, plate_w, plate_t = 60, 40, 6
with BuildPart() as bp:
    Box(plate_l, plate_w, plate_t)
result = {"Plate": bp.part}
'''


def sketch_image() -> dict:
    """A dimensioned L-bracket sketch (front view) rendered with matplotlib — deterministic test input."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(6, 4.5), dpi=110)
    ax.plot([0, 60, 60, 5, 5, 0, 0], [0, 0, 5, 5, 40, 40, 0], "k-", lw=2)
    ax.annotate("", (0, -6), (60, -6), arrowprops=dict(arrowstyle="<->")); ax.text(30, -9, "60", ha="center")
    ax.annotate("", (-6, 0), (-6, 40), arrowprops=dict(arrowstyle="<->")); ax.text(-9, 20, "40", va="center", rotation=90)
    ax.annotate("", (63, 0), (63, 5), arrowprops=dict(arrowstyle="<->")); ax.text(65, 2.5, "5 thk", va="center")
    ax.plot([20, 20], [0, 5], "k--", lw=1); ax.plot([45, 45], [0, 5], "k--", lw=1)
    ax.text(20, 8, "Ø5", ha="center"); ax.text(45, 8, "Ø5", ha="center")
    ax.text(30, 20, "L-bracket, 30 wide (into page)\nmaterial 5 mm", ha="center")
    ax.set_xlim(-14, 78); ax.set_ylim(-14, 46); ax.set_aspect("equal"); ax.axis("off")
    buf = io.BytesIO(); fig.savefig(buf, format="png"); plt.close(fig)
    return {"name": "bracket_sketch.png", "data": base64.b64encode(buf.getvalue()).decode(), "mime": "image/png"}


async def seed_library(run: Run) -> None:
    run.agent.parts.save_script("hex standoff", "af = 6\nh = 15\nresult = extrude(RegularPolygon(af / 2 / math.cos(math.pi / 6), 6), h)\n",
                                description="M3 hex standoff, af=across flats, h=height")


def top_face_selection(run: Run):
    m = run.model
    top = max((f for f in m.faces if f.kind == "PLANE" and f.normal and f.normal[2] > 0.99), key=lambda f: f.center[2])
    return {"faces": [top.id], "bodies": []}


async def select_boss_top(run: Run) -> None:
    run.case.selection = top_face_selection(run)


def standoff_on_plate(run: Run):
    m = run.model
    others = [b for b in m.bodies if b.name != "Plate"]
    if not others:
        return [check("standoff body", False, f"bodies {[b.path for b in m.bodies]}")]
    b = others[0]
    z0, z1 = b.bbox_min[2], b.bbox_max[2]
    return [check("standoff body", True, b.path),
            check("standoff sits on plate top (z 3..23)", abs(z0 - 3) < 0.5 and abs(z1 - 23) < 0.5, f"z {z0:.2f}..{z1:.2f}")]


def feeds_from_calculator(run: Run):
    """Either the feeds_speeds tool was called or apply_feeds()/feeds() ran inside the CAM script."""
    p = run.program
    via_script = any("[aluminium" in (o.tool.notes or "") for o in (p.ops if p else []))
    return [check("aluminium feeds from the calculator", "feeds_speeds" in run.tools_used or via_script, f"tools {run.tools_used}")]


def adaptive_engagement_reported(run: Run):
    p = run.program
    ops = [o for o in (p.ops if p else []) if o.kind == "adaptive"]
    if not ops:
        return [check("adaptive op", False, "missing")]
    o = ops[0]
    tgt = o.params.get("engagement_angle", 0)
    return [check("adaptive engagement 0.15×D", abs(o.params.get("engagement", 0) - 0.15 * o.tool.diameter) < 1e-3, f"{o.params.get('engagement')} for Ø{o.tool.diameter}"),
            check("adaptive ran to completion", not any("budget" in w or "not reachable" in w for w in o.warnings), "; ".join(o.warnings)),
            check("max engagement < 4× target", o.params.get("max_engagement_angle", 999) < 4 * tgt, f"{o.params.get('max_engagement_angle')} vs {tgt}")]


# ---------------------------------------------------------------- additional scenarios (2026-09-24)
POCKETED = """# plate with a rectangular recess (for rest-machining evals)
plate_l, plate_w, plate_t = 60, 40, 10
with BuildPart() as bp:
    Box(plate_l, plate_w, plate_t)
    with Locations((0, 0, plate_t / 2)):
        Box(30, 20, 4, align=(Align.CENTER, Align.CENTER, Align.MAX), mode=Mode.SUBTRACT)
result = {"Plate": bp.part}
"""
DOMED = """# block with a spherical dome on top (for 3D finishing evals)
with BuildPart() as bp:
    Box(40, 40, 10)
    with Locations((0, 0, 5)):
        Sphere(12)
result = {"Dome": bp.part}
"""


async def seed_step_import(run: Run) -> None:
    """Put a STEP file of the Pin into workspace/imports so the agent can import_step() it."""
    import cad_kernel as _ck
    _ck.export(run.model, run.workspace / "imports", "pin", ["step"], body="Pin")
    (run.workspace / "imports" / "pin.step").exists() or (run.workspace / "imports" / "pin_Pin.step").rename(run.workspace / "imports" / "pin.step")


async def manual_hole_first(run: Run) -> None:
    """Simulate the user drilling a Ø4 hole with the ribbon before asking the agent about it (notes mechanism)."""
    await run.agent.op_hole("Bracket", [20, 12, 4], [0, 0, 1], 4, None, True)


def files_in(sub: str, pattern: str, at_least: int = 1):
    def g(run: Run):
        hits = sorted(str(p.relative_to(run.workspace)) for p in (run.workspace / sub).rglob(pattern))
        return [check(f"{sub}/{pattern} written", len(hits) >= at_least, str(hits))]
    return g


def hole_diameter_present(d: float, tol: float = 0.15, count: int | None = None):
    def g(run: Run):
        hs = [h for h in cam.holes(run.model.shape) if abs(h.diameter - d) < tol] if run.model else []
        ok = len(hs) >= 1 if count is None else len(hs) == count
        return [check(f"Ø{d} hole present" + (f" ×{count}" if count else ""), ok, str(hs))]
    return g


def mass_of_bracket_steel_answered(run: Run):
    import cad_kernel as _ck
    m = run.model
    if m is None or m.body_by_name("Bracket") is None:
        return [check("mass", False, "no Bracket")]
    mp = _ck.mass_properties(m, "Bracket", "steel")
    g = mp["mass_g"]
    import re as _re
    nums = [float(x) for x in _re.findall(r"\b(\d+(?:\.\d+)?)\s*(?:g|grams?)\b", run.text)]
    return [check(f"answer states mass ≈ {g:.0f} g (±3%)", any(abs(n - g) / g < 0.03 for n in nums), f"found {nums} in answer")]


def library_has(name_sub: str):
    def g(run: Run):
        names = [p["name"] for p in run.agent.parts.list()]
        return [check(f"library part containing '{name_sub}'", any(name_sub in n.lower() for n in names), str(names))]
    return g


def sketch_named(name: str):
    def g(run: Run):
        defs = script_edit.sketches(run.model.code) if run.model else []
        return [check(f"editable sketch '{name}' in script", any(d["name"] == name for d in defs), str([d["name"] for d in defs]))]
    return g


def volume_dropped_by(expected: float, rel: float = 0.15, from_code: str = ""):
    def g(run: Run):
        base = ck.run_script(from_code).volume if from_code else 0
        drop = base - run.model.volume if run.model else 0
        return [check(f"volume reduced by ≈{expected:.0f} mm³", abs(drop - expected) < rel * expected, f"drop {drop:.0f}")]
    return g



def _long_script(n_posts: int = 14) -> str:
    """A realistic long design (~150 lines): plate, posts, ribs, many named bodies with comments. Used to measure
    edit_model against whole-script rebuilds."""
    lines = ["# Fixture plate with posts and ribs — long script", "plate_l, plate_w, plate_t = 160, 100, 6", "post_d, post_h = 8, 30",
             "rib_w, rib_h = 4, 10", "", "with BuildPart() as plate_bp:", "    Box(plate_l, plate_w, plate_t)",
             "    with Locations((plate_l / 2 - 8, plate_w / 2 - 8), (-plate_l / 2 + 8, plate_w / 2 - 8), (plate_l / 2 - 8, -plate_w / 2 + 8), (-plate_l / 2 + 8, -plate_w / 2 + 8)):",
             "        Hole(4.5)", "plate = plate_bp.part", ""]
    names = []
    for i in range(n_posts):
        x = -60 + (i % 7) * 20; y = -25 if i < 7 else 25
        lines += [f"# Post {i + 1}: standing on the plate at ({x}, {y})", f"post{i + 1}_x, post{i + 1}_y = {x}, {y}",
                  f"with BuildPart() as p{i + 1}:", f"    with Locations((post{i + 1}_x, post{i + 1}_y, plate_t / 2)):",
                  f"        Cylinder(post_d / 2, post_h, align=(Align.CENTER, Align.CENTER, Align.MIN))",
                  f"    with Locations((post{i + 1}_x, post{i + 1}_y, plate_t / 2 + post_h)):",
                  f"        Cylinder(post_d / 2 - 1.5, 4, align=(Align.CENTER, Align.CENTER, Align.MIN))",
                  f"post{i + 1} = p{i + 1}.part", ""]
        names.append(f'"Post{i + 1}": post{i + 1}')
    for j in range(4):
        y = -40 + j * 26
        lines += [f"# Rib {j + 1}: stiffener under the plate", f"rib{j + 1} = Pos(0, {y}, -plate_t / 2 - rib_h / 2) * Box(plate_l - 20, rib_w, rib_h)", ""]
        names.append(f'"Rib{j + 1}": rib{j + 1}')
    lines += ["result = {\"Plate\": plate, " + ", ".join(names) + "}", ""]
    return "\n".join(lines)


LONG_SCRIPT = _long_script()


CASES: list[Case] = [
    Case("cad_box_hole", tags=["cad"],
         prompt="Make a 20 × 30 × 10 mm block centred on the origin with a Ø5 through hole down the centre (Z axis). Single body called Block.",
         graders=[no_agent_error(), expect_bodies(["Block"], count=1), expect_bbox((20, 30, 10)),
                  expect_holes(1, 5.0, through=True), expect_volume(20 * 30 * 10 - math.pi * 2.5 ** 2 * 10)]),
    Case("cad_named_bodies", tags=["cad"],
         prompt="Two separate bodies: a base plate 40 × 40 × 5 mm sitting on the XY plane (z from 0 to 5) named Base, and a Ø10 × 30 mm post named Post standing on top of it at the centre. Do not fuse them.",
         graders=[no_agent_error(), expect_bodies(["Base", "Post"], count=2), expect_bbox((40, 40, 5), body="Base"),
                  expect_bbox((10, 10, 30), body="Post"),
                  lambda run: [check("post starts at z=5", abs(run.model.body_by_name("Post").bbox_min[2] - 5) < 0.2, str(run.model.body_by_name("Post").bbox_min))]]),
    Case("cad_spur_gear", tags=["cad", "gears"],
         prompt="Make a spur gear: module 2, 20 teeth, 20° pressure angle, 10 mm thick, with an 8 mm bore. One body named Gear.",
         graders=[no_agent_error(), expect_bodies(["Gear"], count=1), expect_bbox((44, 44, 10), body="Gear", tol=0.3),
                  expect_script_contains("spur_gear"),
                  lambda run: [check("bore Ø8 present", any(abs(f.radius - 4) < 0.05 for f in run.model.faces if f.kind == "CYLINDER"), "no Ø8 cylinder face")]]),
    Case("cad_gear_train_incremental", tags=["cad", "gears", "incremental"],
         prompt="Build a two-gear train on a 4 mm base plate: a 20-tooth and a 12-tooth spur gear, module 2, both 8 mm thick, 6 mm bores, meshing correctly on the plate. Plate 80 × 50 mm. Bodies: Plate, Gear20, Gear12.",
         graders=[no_agent_error(), expect_bodies(["Plate", "Gear20", "Gear12"], count=3),
                  lambda run: [check("gears mesh without overlap", abs(run.model.shape.volume - sum(b.shape.volume for b in run.model.bodies)) < 1.0, "bodies overlap"),
                               check("centre distance 32 mm", abs(math.dist(_bc(run.model.body_by_name("Gear20")), _bc(run.model.body_by_name("Gear12"))) - 32) < 0.5, "wrong spacing")]]),
    Case("cad_complex_incremental", tags=["cad", "incremental"],
         prompt="Model a small open gearbox as separate bodies: a 120 × 70 × 6 mm base plate (Plate); two Ø6 × 40 mm vertical shafts (ShaftA, ShaftB) standing on the plate, 32 mm apart along X and centred on the plate; a 20-tooth and a 12-tooth module-2 spur gear, 8 mm thick, 6 mm bores, on the shafts 4 mm above the plate, meshing (GearA on ShaftA, GearB on ShaftB); two Ø14 × 4 mm spacer collars under the gears (CollarA, CollarB); and four Ø4.5 mm mounting holes in the plate corners, 8 mm in from the edges. Seven bodies.",
         graders=[no_agent_error(), expect_bodies(["Plate", "ShaftA", "ShaftB", "GearA", "GearB", "CollarA", "CollarB"], count=7),
                  expect_holes(4, diameter=4.5),
                  lambda run: [check("gears mesh without overlap", abs(run.model.shape.volume - sum(b.shape.volume for b in run.model.bodies)) < 1.0, "bodies overlap"),
                               check("built incrementally (build_model then ≥1 edit_model)", sum(1 for t in run.tools_used if t.endswith("edit_model")) >= 1 and sum(1 for t in run.tools_used if t.endswith("build_model")) >= 1, f"tools {run.tools_used}")]]),
    Case("cad_long_script_edit", tags=["cad", "longscript"],
         prompt="Make the base plate 8 mm thick instead of 6, and add a Ø5 mm hole through the plate at the centre (0, 0). Keep everything else exactly as it is.",
         initial_code=LONG_SCRIPT,
         graders=[no_agent_error(), expect_bodies(["Plate"], count=19),
                  expect_bbox((160, 100, 8), body="Plate", tol=0.3),
                  lambda run: [check("Ø5 hole present", any(abs(f.radius - 2.5) < 0.05 for f in run.model.faces if f.kind == "CYLINDER"), "no Ø5 cylinder face"),
                               check("posts untouched", run.model.body_by_name("Post14") is not None and abs(run.model.body_by_name("Post14").bbox_max[2] - (4 + 30 + 4)) < 0.5, "post moved or missing")]]),
    Case("cad_imperial_bracket", tags=["cad", "units", "threads"],
         prompt="Make a 3 x 2 inch mounting plate, 1/4 inch thick, with two 1/4-20 tapped holes 2 inches apart on the centreline, through. One body named Plate. Report the sizes in inches.",
         graders=[no_agent_error(), expect_bodies(["Plate"], count=1), expect_bbox((76.2, 50.8, 6.35), body="Plate", tol=0.3),
                  expect_script_contains("inch"),
                  lambda run: [check("two 1/4-20 threads registered", sorted(t.size for t in run.model.threads) == ["1/4-20", "1/4-20"], str([t.label() for t in run.model.threads])),
                               check("answer in inches", "inch" in run.text.lower() or '"' in run.text, run.text[:200])]]),
    Case("cad_parametric", tags=["cad", "params"],
         prompt="Model an L-shaped angle bracket: 50 long, 30 tall, 20 wide, 4 mm thick, with a 3 mm inside fillet. Expose the length, height, width and thickness as parameters at the top of the script.",
         graders=[no_agent_error(), expect_bbox((50, 20, 30), tol=1.0), expect_params(), expect_face_kinds("CYLINDER"),
                  lambda run: [check("≥4 numeric parameters", len(__import__("script_edit").params(run.model.code)) >= 4, "")]]),
    Case("cad_selected_face_chamfer", tags=["cad", "selection"], initial_code=ck.DEFAULT_CODE, setup=select_boss_top,
         prompt="Add a 1 mm chamfer to the outer edge of this face.",
         graders=[no_agent_error(), expect_face_kinds("CONE"), expect_bbox((60, 40, 45)),
                  expect_bodies(["Bracket", "Pin"], count=2)]),
    Case("cad_param_tool", tags=["cad", "params"], initial_code=ck.DEFAULT_CODE,
         prompt="Change the plate length to 80 mm. Keep everything else as it is.",
         graders=[no_agent_error(), expect_bbox((80, 40, 45)), expect_params(values={"plate_l": 80}),
                  lambda run: [check("used set_parameters (cheap path)", "set_parameters" in run.tools_used, f"used {run.tools_used}")]]),
    Case("cad_library_reuse", tags=["cad", "library"], initial_code=PLATE, setup=seed_library,
         prompt="Add a hex standoff from the part library standing on the top of the plate at the origin, 20 mm tall. Don't model one from scratch.",
         graders=[no_agent_error(), expect_bodies(count=2), expect_script_contains("from_library("), standoff_on_plate]),
    Case("cad_from_image", tags=["cad", "image"], images=[sketch_image()],
         prompt="Model this bracket from the sketch. It is 5 mm thick and 30 mm wide (into the page). Single body.",
         graders=[no_agent_error(), expect_bodies(count=1), expect_bbox((60, 30, 40), tol=1.0), expect_holes(2, 5.0, tol=0.3)]),
    Case("cad_threads", tags=["cad", "threads"], initial_code=PLATE,
         prompt="Add two M4 tapped through-holes in the plate at (-20, 0) and (20, 0), and two M4 clearance holes (medium fit) at (0, -12) and (0, 12). Use the thread helpers so the drawing calls out the threads. Cosmetic threads.",
         graders=[no_agent_error(), expect_bodies(count=1), thread_checks, expect_script_contains("M4")]),
    Case("cad_sketch_extrude", tags=["cad", "sketch"], initial_code=SKETCHED,
         prompt="Extrude sketch sk1 by 6 mm upward and join it to the Plate as a boss. Keep the sketch block as it is.",
         graders=[no_agent_error(), expect_bodies(count=1), sketch_used, expect_bbox((60, 40, 12), tol=0.5)]),
    Case("cad_sketch_edit_tool", tags=["cad", "sketch"], initial_code=SKETCHED,
         prompt="In sketch sk1, add a second Ø6 subtract circle at (10, 4) and make the rectangle 24 wide instead of 20. Keep it editable for me. Don't extrude anything.",
         graders=[no_agent_error(), expect_tools_used("sketch"),
                  lambda run: [check("sketch items updated", (lambda d: d and d["name"] == "sk1" and len(d["items"]) == 3 and any(abs(i.get("w", 0) - 24) < 1e-6 for i in d["items"] if i["type"] == "rect") and sum(1 for i in d["items"] if i["type"] == "circle" and i.get("mode") == "subtract") == 2)(next(iter(script_edit.sketches(run.model.code)), None)), str(script_edit.sketches(run.model.code)))],
                  expect_bodies(count=1), expect_volume(60 * 40 * 6, rel=0.001)]),
    Case("measure_qa", tags=["measure"], initial_code=ck.DEFAULT_CODE,
         prompt="Using the measure tools (do not guess from the code): how far apart are the two front mounting holes along X, and how thick is the plate? Answer with the two numbers.",
         graders=[no_agent_error(), expect_answer(r"\b44(\.0+)?\s*mm"), expect_answer(r"\b8(\.0+)?\s*mm"),
                  lambda run: [check("used measure/inspect tool", any(t in run.tools_used for t in ("measure", "inspect_model", "mass_properties")), f"used {run.tools_used}")]]),
    Case("cam_basic", tags=["cam"], initial_code=ck.DEFAULT_CODE,
         prompt="Create a CAM program on the Generic 3018 for the Bracket body only: face the stock top, drill the four Ø3.2 mounting holes with the 3 mm drill, and cut the plate outline through with the 6 mm endmill using 4 tabs. Stock = bracket bbox + 3 mm margin, 1 mm extra on top.",
         graders=[no_agent_error(), expect_program(["face", "drill", "contour"], min_ops=3, no_warnings_matching="travel|rpm"),
                  expect_op("contour", tabs=4), expect_op("drill", holes=4), expect_gcode(z_floor=-6.0)]),
    Case("cam_adaptive_finish", tags=["cam", "adaptive"], initial_code=ck.DEFAULT_CODE,
         prompt="CAM on the Generic 3018, Bracket only, aluminium feeds from the calculator: adaptive-rough the central Ø6 bore with the 3 mm endmill at 0.15 stepover leaving 0.2 mm, then finish the bore wall with an inside contour. Nothing else.",
         graders=[no_agent_error(), expect_program(["adaptive", "contour"], min_ops=2), expect_op("contour", side="inside"),
                  adaptive_engagement_reported, feeds_from_calculator, expect_gcode()]),
    # ---- added 2026-09-24: first run, multi-body edits, exports, drawings, mass, imports, library save, notes, CAM rest/3D/limits
    Case("cad_first_run_flange", tags=["cad", "first-run"],
         prompt="Make a flange: Ø60 disc, 8 mm thick, with a Ø20 centre bore and four Ø6 bolt holes on a 44 mm PCD. Call the body Flange.",
         graders=[no_agent_error(), expect_bodies(["Flange"], count=1), expect_bbox((60, 60, 8), tol=0.5),
                  expect_holes(5, through=True), hole_diameter_present(20.0, count=1), hole_diameter_present(6.0, count=4)]),
    Case("cad_rename_and_add_body", tags=["cad", "bodies"], initial_code=ck.DEFAULT_CODE,
         prompt="Rename the Pin body to Dowel, and add a separate Washer body (Ø20 outside, Ø8 hole, 2 mm thick) lying flat on top of the plate at the origin. Keep the bracket unchanged.",
         graders=[no_agent_error(), expect_bodies(["Bracket", "Dowel", "Washer"], count=3), expect_bbox((20, 20, 2), body="Washer"),
                  lambda run: [check("washer sits on plate top (z≈4)", abs(run.model.body_by_name("Washer").bbox_min[2] - 4) < 0.3, str(run.model.body_by_name("Washer").bbox_min))]]),
    Case("cad_export_files", tags=["cad", "export"], initial_code=ck.DEFAULT_CODE,
         prompt="Export a fine STL (0.01 mm tolerance) of only the Pin named pin_fine, and a STEP of the whole assembly named bracket_assy.",
         graders=[no_agent_error(), expect_tools_used("export_model"), files_in("exports", "pin_fine*.stl"), files_in("exports", "bracket_assy*.step"),
                  expect_bodies(["Bracket", "Pin"], count=2)]),
    Case("cad_drawings", tags=["cad", "drawings"], initial_code=ck.DEFAULT_CODE,
         prompt="Produce shop drawings for this design in aluminium 6061 on A4 sheets.",
         graders=[no_agent_error(), expect_tools_used("make_drawings"), files_in("drawings", "*.svg", at_least=2), files_in("drawings", "*.dxf")]),
    Case("measure_mass_steel", tags=["measure"], initial_code=ck.DEFAULT_CODE,
         prompt="What would the Bracket body weigh in steel? Use the tools, not an estimate, and give the answer in grams.",
         graders=[no_agent_error(), expect_tools_used("mass_properties"), mass_of_bracket_steel_answered]),
    Case("cad_step_import", tags=["cad", "import"], initial_code=ck.DEFAULT_CODE, setup=seed_step_import,
         prompt="There is a STEP file pin.step in the imports folder. Add it to the design as a new body named ImportedPin, moved 30 mm along +X so it sits beside the bracket. Keep the existing bodies.",
         graders=[no_agent_error(), expect_script_contains("import_step("), expect_bodies(count=3),
                  lambda run: [check("ImportedPin exists and is offset in X", (lambda b: b is not None and b.bbox_min[0] > 20)(run.model.body_by_name("ImportedPin")), str([(b.name, b.bbox_min) for b in run.model.bodies]))]]),
    Case("cad_library_save_body", tags=["cad", "library"], initial_code=ck.DEFAULT_CODE,
         prompt="Save the Bracket body to the part library as 'demo bracket body' with the tags bracket and demo, description 'L bracket with boss'.",
         graders=[no_agent_error(), expect_tools_used("library"), library_has("demo bracket body"), expect_bodies(["Bracket", "Pin"], count=2)]),
    Case("cad_manual_op_note", tags=["cad", "notes"], initial_code=ck.DEFAULT_CODE, setup=manual_hole_first,
         prompt="Briefly, what did I just change by hand? Then make that new hole Ø6 instead of Ø4, nothing else.",
         graders=[no_agent_error(), expect_answer(r"hole|drill"),
                  lambda run: [check("the hand-drilled hole at (20, 12) is now Ø6", any(abs(h.x - 20) < 0.5 and abs(h.y - 12) < 0.5 and abs(h.diameter - 6) < 0.15 for h in cam.holes(run.model.shape)), str(cam.holes(run.model.shape)))],
                  lambda run: [check("Ø4 hole gone", not any(abs(h.diameter - 4) < 0.1 for h in cam.holes(run.model.shape)), "")],
                  expect_bodies(["Bracket", "Pin"], count=2)]),
    Case("cad_agent_sketch_cut", tags=["cad", "sketch"], initial_code=PLATE,
         prompt="Create an editable sketch called slot1 on the top face of the plate with a 20 × 6 mm slot centred at (15, 0) along X, then cut it 3 mm deep into the plate. Keep the sketch editable for me.",
         graders=[no_agent_error(), expect_tools_used("sketch"), sketch_named("slot1"), expect_bodies(count=1),
                  volume_dropped_by((20 - 6) * 6 * 3 + math.pi * 9 * 3, rel=0.2, from_code=PLATE)]),
    Case("cam_rest_machining", tags=["cam", "rest"], initial_code=POCKETED,
         prompt="CAM on the Generic 3018 in aluminium (feeds from the calculator): clear the 30 × 20 recess with the 6 mm endmill as a pocket, then rest-machine the corners it could not reach with the 3 mm endmill. Nothing else.",
         graders=[no_agent_error(), expect_program(["pocket", "rest"], min_ops=2, no_warnings_matching="travel|rpm"), feeds_from_calculator,
                  lambda run: [check("rest op uses the 3 mm tool", any(o.kind == "rest" and abs(o.tool.diameter - 3) < 0.01 for o in run.program.ops), str([(o.kind, o.tool.diameter) for o in run.program.ops]))],
                  expect_gcode()]),
    Case("cam_3d_finish", tags=["cam", "3d"], initial_code=DOMED,
         prompt="Finish the dome on this part with the 6 mm ball endmill using parallel 3D passes at 0.5 mm stepover over the dome region only (about ±14 mm around the centre), on the Generic 3018. Nothing else.",
         graders=[no_agent_error(), expect_program(["parallel3d"], min_ops=1, no_warnings_matching="travel|rpm"),
                  lambda run: [check("ball tool, fine stepover", any(o.kind == "parallel3d" and o.tool.type == "ball" and o.params.get("stepover", 99) <= 0.6 for o in run.program.ops), str([(o.tool.type, o.params.get("stepover")) for o in run.program.ops]))],
                  expect_gcode()]),
    Case("cam_travel_limits", tags=["cam", "limits"],
         prompt="Model a 400 × 60 × 6 mm rail named Rail, then create a CAM program on the Generic 3018 that cuts its outline. Tell me plainly if anything about the machine setup is a problem.",
         graders=[no_agent_error(), expect_bodies(["Rail"], count=1), expect_answer(r"travel|too (long|large|big)|exceed|does not fit|doesn't fit|won't fit|300 ?mm")]),
    Case("cam_gcode_export", tags=["cam", "export"], initial_code=ck.DEFAULT_CODE,
         prompt="CAM for the Bracket on the Generic 3018: face the stock and cut the outline through with the 6 mm endmill (2 tabs). Then export the G-code as bracket_run and tell me how many lines it has.",
         graders=[no_agent_error(), expect_program(["face", "contour"], min_ops=2), expect_tools_used("export_gcode"),
                  files_in("exports", "bracket_run*.nc"), expect_answer(r"\b\d{2,5}\s*lines")]),
]
