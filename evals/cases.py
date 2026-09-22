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
]
