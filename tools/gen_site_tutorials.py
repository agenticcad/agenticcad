"""Bake simulated-tutorial data for the GitHub Pages site from the REAL kernel: every model state is
built with build123d, every toolpath with cam_kernel. Output: <out>/<tutorial>.json with steps + meshes.
Usage: .venv/bin/python tools/gen_site_tutorials.py --out ../agenticcad-site/tutorials/data"""
from __future__ import annotations
import argparse, json, math, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import cad_kernel as ck, cam_kernel as cam, script_edit
from cam_data import Library

def mesh_of(model: ck.Model) -> dict:
    """Compact viewer payload (draft-ish quality, rounded)."""
    m = model.mesh
    return {"bodies": [{"id": b["id"], "name": b["name"], "path": b["path"],
                        "positions": [round(v, 3) for v in b["positions"]], "normals": [round(v, 3) for v in b["normals"]],
                        "indices": b["indices"], "faceRanges": b["faceRanges"],
                        "edges": [[round(v, 3) for v in pl] for pl in b["edges"]], "bboxMin": b["bboxMin"], "bboxMax": b["bboxMax"]} for b in m["bodies"]],
            "faces": m["faces"],
            "sketches": [{"name": s["name"], "edges": [[round(v, 3) for v in pl] for pl in s["edges"]]} for s in m.get("sketches", [])],
            "bboxMin": m["bboxMin"], "bboxMax": m["bboxMax"], "summary": model.summary(0).splitlines()[0]}

def build(code: str, quality="draft") -> ck.Model:
    return ck.run_script(code, quality)

PLATE = '''# Mounting plate with a bossed bore
plate_l, plate_w, plate_t = 60, 40, 8
boss_d, boss_h, bore_d = 20, 15, 8
hole_d, inset = 5, 8
with BuildPart() as plate:
    Box(plate_l, plate_w, plate_t)
    with Locations((0, 0, plate_t / 2)):
        Cylinder(boss_d / 2, boss_h, align=(Align.CENTER, Align.CENTER, Align.MIN))
    Hole(bore_d / 2)
    with Locations((-plate_l/2 + inset, -plate_w/2 + inset), (plate_l/2 - inset, -plate_w/2 + inset),
                   (-plate_l/2 + inset, plate_w/2 - inset), (plate_l/2 - inset, plate_w/2 - inset)):
        Hole(hole_d / 2)
result = {"Plate": plate.part}
'''
PLATE_FILLET = PLATE.replace('        Hole(hole_d / 2)\nresult', '        Hole(hole_d / 2)\n    fillet(plate.edges().filter_by(Axis.Z).group_by(Axis.X)[0] + plate.edges().filter_by(Axis.Z).group_by(Axis.X)[-1], 4)\nresult')

def face_id(model, pred):
    return next(f.id for f in model.faces if pred(f))

def top_face(model, z):
    return face_id(model, lambda f: f.kind == "PLANE" and f.normal and f.normal[2] > 0.99 and abs(f.center[2] - z) < 1e-3)

def tutorial_first_part():
    PLATE20 = PLATE.replace("boss_d, boss_h, bore_d = 20, 15, 8", "boss_d, boss_h, bore_d = 20, 20, 8")
    m1 = build(PLATE); m1b = build(PLATE20); m2 = build(PLATE_FILLET)
    return {"id": "first-part", "title": "Your first part", "minutes": 4,
            "intro": "Describe a part in plain words. The agent writes build123d code, the exact kernel builds it, and the viewer updates. Then change a number by hand and watch it rebuild without the agent.",
            "meshes": {"m1": mesh_of(m1), "m1b": mesh_of(m1b), "m2": mesh_of(m2)},
            "steps": [
                {"kind": "user", "text": "Make a 60 × 40 × 8 mm mounting plate with four Ø5 holes 8 mm in from each corner, and a Ø20 boss 15 mm tall in the middle with a Ø8 through bore. Call it Plate.",
                 "narr": "The design is empty. You describe the part in plain words in the chat. Give the dimensions you care about; the agent will turn them into named parameters."},
                {"kind": "agent", "text": "I'll build that as one body with named dimensions so you can tweak them later.",
                 "narr": "The agent replies like a colleague: a one-line plan, then it gets to work."},
                {"kind": "tool", "name": "build_model", "detail": f"{len(PLATE.splitlines())} lines", "code": PLATE, "result": m1.summary(0).splitlines()[0],
                 "narr": "It calls the build_model tool with a build123d script (shown at the bottom left, and under 'code ▸' on the card). The result line is the kernel's verdict: bodies, faces and volume, or a traceback the agent then fixes."},
                {"kind": "model", "mesh": "m1",
                 "narr": "The viewer shows the exact model as soon as the script builds. Drag to orbit. Every face is true B-rep geometry; the triangles you see are display only."},
                {"kind": "agent", "text": f"Done — **Plate**: 60 × 40 × 8 plate, Ø20 × 15 boss with a Ø8 through bore, four Ø5 holes on a 44 × 24 pattern. Volume {m1.volume:,.0f} mm³. The dimensions are parameters at the top of the script (plate_l, boss_d, hole_d…), so say the word to change any of them.",
                 "narr": "The agent confirms what it built with numbers that came back from the kernel, not from its own arithmetic."},
                {"kind": "params", "items": [["plate_l", 60], ["plate_w", 40], ["plate_t", 8], ["boss_d", 20], ["boss_h", 15], ["bore_d", 8], ["hole_d", 5], ["inset", 8]], "change": ["boss_h", 20],
                 "narr": "Now without the agent: the Design tab lists every top-level number in the script as a field. Change boss_h from 15 to 20 and press Enter."},
                {"kind": "model", "mesh": "m1b",
                 "narr": "The part rebuilt with a 20 mm boss. Only that literal in the script changed, and the agent is sent a note about it so it stays in sync."},
                {"kind": "user", "text": "Set the boss back to 15 tall, fillet the four vertical corner edges 4 mm, then export a STEP.",
                 "narr": "Back to chat. Parameter edits and requests mix freely because both end up in the same script."},
                {"kind": "tool", "name": "build_model", "detail": f"{len(PLATE_FILLET.splitlines())} lines", "code": PLATE_FILLET, "result": m2.summary(0).splitlines()[0],
                 "narr": "The agent rewrites the script: boss_h back to 15 and a fillet on the four vertical corner edges, selected by axis rather than by guessing edge numbers."},
                {"kind": "model", "mesh": "m2",
                 "narr": "Fillets are true cylindrical faces. STEP export writes them as cylindrical surfaces, never as triangles."},
                {"kind": "tool", "name": "export_model", "detail": 'formats=["step"]', "result": "exported: workspace/exports/plate.step",
                 "narr": "export_model writes the STEP file into workspace/exports. STL is different: it is tessellated only at export, at a tolerance you choose."},
                {"kind": "agent", "text": "Boss back to 15 mm, the four vertical plate edges filleted at R4, and **plate.step** exported (exact geometry, one solid). The hole pattern is unchanged.",
                 "narr": "That is the whole loop: describe, build, check, adjust. Next: pointing at a face so the agent works on exactly the geometry you mean."},
            ]}

def tutorial_select_face():
    m1 = build(PLATE_FILLET)
    top = top_face(m1, 19)
    CH = PLATE_FILLET.replace("result = {", "boss_top = plate.part.faces().sort_by(Axis.Z)[-1]\nplate_part = plate.part.chamfer(1, None, [boss_top.edges().filter_by(GeomType.CIRCLE).sort_by(SortBy.RADIUS)[-1]])\nresult = {").replace('"Plate": plate.part', '"Plate": plate_part')
    m2 = build(CH)
    fi = next(f for f in m1.faces if f.id == top)
    return {"id": "select-face", "title": "Point at a face and ask", "minutes": 2,
            "intro": "Click geometry in the viewer to talk about it. The selection travels with your message as the face's exact type, centre and normal, so the agent picks the same face in code.",
            "meshes": {"m1": mesh_of(m1), "m2": mesh_of(m2)},
            "steps": [
                {"kind": "model", "mesh": "m1", "narr": "We start from the filleted plate of the first tutorial."},
                {"kind": "select", "face": top, "narr": f"Click the top of the boss in the viewer. It turns orange and a chip appears in the composer: {fi.short_label()}. Shift-click adds more faces."},
                {"kind": "user", "text": "Put a 1 mm chamfer on the outer edge of this face.", "chips": [fi.short_label()],
                 "narr": "Your message carries the chip. The server expands it into the face's exact type, centre, normal and size, so the agent never has to guess which face 'this' is."},
                {"kind": "agent", "text": "Selected face: plane +Z at z = 19, Ø20 boss top. I'll chamfer its outer circular edge and leave the bore edge sharp.",
                 "narr": "The agent restates the selection in geometric terms before touching the script."},
                {"kind": "tool", "name": "build_model", "detail": f"{len(CH.splitlines())} lines", "code": CH, "result": m2.summary(0).splitlines()[0],
                 "narr": "In code it selects the same face by position (the top face along Z) and the larger of its two circular edges."},
                {"kind": "model", "mesh": "m2", "narr": "A new conical face appears on the boss. The inner bore edge is untouched."},
                {"kind": "agent", "text": "Chamfered the outer top edge of the boss 1 × 45°; the bore edge is left sharp. The face count went from %d to %d." % (len(m1.faces), len(m2.faces)),
                 "narr": "Face ids are renumbered on every rebuild, so the agent re-inspects after building instead of trusting old numbers."},
                {"kind": "user", "text": "How thick is the plate, and how far apart are the holes along X?",
                 "narr": "Questions work the same way. Measuring is a tool, so the answer is measured, not estimated."},
                {"kind": "tool", "name": "measure", "detail": "faces=[top, bottom] · edges=[hole A, hole B]", "result": "plane gap 8.000 mm · centre distance 44.000 mm",
                 "narr": "The measure tool takes faces, edges, points or bodies and returns distances, angles and spacings from the exact geometry."},
                {"kind": "agent", "text": "Plate thickness **8.00 mm** (top and bottom faces are parallel, gap 8.000). Hole centres are **44.00 mm** apart along X and 24.00 mm along Y.",
                 "narr": "Next: draw a sketch by hand and let the agent build from it."},
            ]}

def tutorial_sketch():
    plane = {"origin": [0, 0, 4], "x_dir": [1, 0, 0], "z_dir": [0, 0, 1], "label": "Plate top"}
    items = [{"type": "rect", "cx": -18, "cy": 0, "w": 12, "h": 16, "angle": 0, "mode": "add"}, {"type": "circle", "cx": -18, "cy": 0, "r": 3, "mode": "subtract"}]
    base = PLATE
    with_sketch = script_edit.set_sketch(base, "sketch1", plane, items)
    m1 = build(base); m2 = build(with_sketch)
    ext = script_edit.wrap_body_expr(with_sketch, "Plate", "({expr}) + extrude(sketch1, amount=6)")
    m3 = build(ext)
    return {"id": "sketch-to-boss", "title": "Sketch by hand, build with the agent", "minutes": 3,
            "intro": "Fusion-style sketches on any flat face: draw rectangles, circles, polygons and slots, and they become a real Sketch variable in the script that either of you can extrude, cut or revolve.",
            "meshes": {"m1": mesh_of(m1), "m2": mesh_of(m2), "m3": mesh_of(m3)},
            "steps": [
                {"kind": "model", "mesh": "m1", "narr": "A plain mounting plate. We will add a small rectangular boss with a hole, drawn by hand rather than described."},
                {"kind": "sketchmode", "plane": plane, "items": items, "face": top_face(m1, 4),
                 "narr": "Click the top face of the plate, then press K (Sketch). The camera squares up to the face. Two clicks make the rectangle, two more the subtract circle, snapped to the 1 mm grid. Then Finish."},
                {"kind": "model", "mesh": "m2",
                 "narr": "Finish writes the sketch into the script. It renders as a cyan outline on the face and appears under Sketches in the Browser, where you can reopen and edit it."},
                {"kind": "code", "text": with_sketch[with_sketch.index("# sketch:"):with_sketch.index("# /sketch:sketch1") + len("# /sketch:sketch1")],
                 "narr": "This is what landed in the script: an ordinary BuildSketch block. The JSON header lets the editor reopen it; the agent is told never to rewrite it by hand."},
                {"kind": "user", "text": "Extrude sketch1 6 mm up and join it to the plate.", "chips": ["sketch: sketch1"],
                 "narr": "Sketches can be selected as chips too. Ask the agent to use it."},
                {"kind": "tool", "name": "build_model", "detail": "extrude(sketch1, amount=6)", "code": ext[ext.index("result ="):], "result": m3.summary(0).splitlines()[0],
                 "narr": "sketch1 is a normal build123d Sketch variable, so the agent simply extrudes it and adds the result to the plate."},
                {"kind": "model", "mesh": "m3", "narr": "The boss with its hole is fused into the plate: one body. The sketch block is unchanged, so you can still edit it and the boss follows."},
                {"kind": "agent", "text": "Extruded **sketch1** 6 mm along its plane normal and fused it into Plate (one body). The sketch stays in the script; edit it in the sketch editor and the boss follows.",
                 "narr": "Next: the ribbon tools for quick manual tweaks that are still written as code."},
            ]}

def tutorial_ribbon():
    base = PLATE
    m1 = build(base)
    pull = script_edit.wrap_body_expr(base, "Plate", "{body} + extrude({body}.faces().sort_by_distance((7, 0, 19))[0], amount=5, dir=(0, 0, 1))")
    m2 = build(pull)
    hole = script_edit.wrap_body_expr(pull, "Plate", 'tap({body}, "M4", at=(18, 0, 4), depth=6, axis=(0, 0, -1))')
    m3 = build(hole)
    fil = script_edit.wrap_body_expr(hole, "Plate", "{body}.fillet(2, [*{body}.faces().sort_by_distance((7, 0, 24))[0].edges()])")
    m4 = build(fil)
    return {"id": "ribbon", "title": "Tweak it by hand — still code", "minutes": 3,
            "intro": "The ribbon works like a desktop CAD tool: pick a face or edges, type a number, OK. Under the hood every operation is written into the script, so hand edits and agent edits live in the same file.",
            "meshes": {"m1": mesh_of(m1), "m2": mesh_of(m2), "m3": mesh_of(m3), "m4": mesh_of(m4)},
            "steps": [
                {"kind": "model", "mesh": "m1", "narr": "The plain plate again. Nothing in this tutorial goes through the agent; watch the ribbon at the top of the viewer."},
                {"kind": "ribbon", "tool": "extrude", "face": top_face(m1, 19), "fields": [["Distance", "5 mm"], ["Direction", "Outward (add)"]],
                 "narr": "Press/Pull (Q): click the top of the boss. A command dialog opens under the ViewCube with the face as a chip. Drag the handle or type 5, then OK."},
                {"kind": "model", "mesh": "m2",
                 "narr": "The boss grew 5 mm. In the script the body became `_plate + extrude(_plate.faces().sort_by_distance((7, 0, 19))[0], amount=5, dir=(0, 0, 1))`: the face is referenced by the point you clicked, so Undo and the agent both understand it."},
                {"kind": "ribbon", "tool": "hole", "point": [18, 0, 4], "fields": [["Type", "Tapped M4"], ["Depth", "6 mm"]],
                 "narr": "Hole (H): click where it goes on the plate. Choose Tapped M4, 6 mm deep, OK."},
                {"kind": "model", "mesh": "m3",
                 "narr": "A Ø3.3 tap-drill hole, registered as M4×0.7 ↧6 so shop drawings call it out as a thread rather than a plain hole."},
                {"kind": "ribbon", "tool": "fillet", "face": top_face(m3, 24), "fields": [["Radius", "2 mm"]],
                 "narr": "Fillet (F): click the top face of the boss to take all of its edges at once, radius 2, OK."},
                {"kind": "model", "mesh": "m4", "narr": "Two fillets on the boss top: the outer edge and the bore edge."},
                {"kind": "code", "text": fil[fil.index("_plate ="):],
                 "narr": "Everything you just did, as code on the body's expression. Undo steps back through it, and the agent received a note for each change. Next: CAM."},
            ]}

def tutorial_cam():
    m = build(PLATE_FILLET, "draft")
    part = m.body_by_name("Plate").shape
    lib = Library(ROOT / "workspace"); tools = lib.tool_map(); machines = lib.machines()
    stock = cam.Stock.from_model(part, margin=3, top=1.0)
    setup = cam.Setup(machines["Generic 3018"], stock, origin="stock-top-left")
    t6 = cam.apply_feeds(tools[1], "aluminium", setup.machine); t3 = cam.apply_feeds(tools[2], "aluminium", setup.machine); drl = cam.apply_feeds(tools[5], "aluminium", setup.machine)
    hs = cam.holes(part); mount = [h for h in hs if h.diameter < 6]
    prog = cam.Program(setup, name="plate")
    prog.add(cam.face(setup, t6, z_top=stock.top, z_bottom=19, name="Face"))
    prog.add(cam.adaptive(setup, t6, cam.stock_minus(setup, part, 0.0, expand=6), z_top=19, z_bottom=4, stepover=0.15, stock_to_leave=0.3, name="Rough around boss"))
    prog.add(cam.adaptive(setup, t3, cam.circle(0, 0, 8), z_top=19, z_bottom=-4.5, stepover=0.15, name="Bore"))
    prog.add(cam.drill(setup, drl, mount, peck="auto", name="Mounting holes (pilot)"))
    prog.add(cam.contour(setup, t6, cam.section(part, 0.0), z_top=4, z_bottom=-4.5, tabs=4, name="Outline"))
    payload = prog.to_payload()
    for op in payload["ops"]:
        mv = op["moves"]
        if len(mv) > 2500:
            keep = max(1, len(mv) // 2500)
            op["moves"] = [x for i, x in enumerate(mv) if i % keep == 0 or x[0] == 0]
        op["moves"] = [[x[0], round(x[1], 2), round(x[2], 2), round(x[3], 2), x[4]] for x in op["moves"]]
    g = prog.gcode(); lines = g.splitlines()
    CAM_CODE = '''part = bodies["Plate"]
stock = Stock.from_model(part, margin=3, top=1.0)
setup = Setup(machines["Generic 3018"], stock, origin="stock-top-left")
t6, t3, drl = (apply_feeds(tools[n], "aluminium", setup.machine) for n in (1, 2, 5))
mount = [h for h in holes(part) if h.diameter < 6]
program = Program(setup, name="plate")
program.add(face(setup, t6, z_top=stock.top, z_bottom=19))
program.add(adaptive(setup, t6, stock_minus(setup, part, 0.0, expand=6), z_top=19, z_bottom=4, stepover=0.15, stock_to_leave=0.3, name="Rough around boss"))
program.add(adaptive(setup, t3, circle(0, 0, 8), z_top=19, z_bottom=-4.5, stepover=0.15, name="Bore"))
program.add(drill(setup, drl, mount, peck="auto", name="Mounting holes (pilot)"))
program.add(contour(setup, t6, section(part, 0.0), z_top=4, z_bottom=-4.5, tabs=4, name="Outline"))
'''
    warn = "; ".join(prog.check()) or "no warnings"
    return {"id": "cam", "title": "CAM in five minutes (experimental)", "minutes": 4,
            "intro": "Experimental preview: ask for a program and get toolpaths (facing, adaptive roughing, drilling, a tabbed outline) and GRBL G-code checked against your machine's limits. Correct, but not yet optimised — inspect before you run.",
            "meshes": {"m1": mesh_of(m)},
            "program": payload,
            "gcode_head": "\n".join(lines[:28]),
            "steps": [
                {"kind": "model", "mesh": "m1", "narr": "The filleted plate. We will machine it from aluminium bar on a small 3018 router."},
                {"kind": "user", "text": "Make a CAM program on the Generic 3018 in aluminium: face the stock, adaptive-rough around the boss with the 6 mm endmill, open the bore with the 3 mm, pilot-drill the mounting holes, then cut the outline through with 4 tabs. Stock = bbox + 3 mm, 1 mm extra on top.",
                 "narr": "Describe the job the way you would brief a machinist: machine, material, stock, and the operations in order."},
                {"kind": "tool", "name": "cam_context", "detail": "", "result": "Machines: Generic 3018 (300×180×45, 1000–10000 rpm)… Tools: T1 6 mm flat, T2 3 mm flat, T5 3 mm drill… Vertical round holes (5): Ø5 ×4 through, Ø8 through",
                 "narr": "cam_context gives the agent your machine and tool libraries plus facts about the model, including the holes it found from the B-rep."},
                {"kind": "agent", "text": f"Aluminium on a 3018 is spindle-limited ({setup.machine.spindle['max']:,.0f} rpm), so I'll take feeds from the calculator (6 mm: {t6.feed:.0f} mm/min at S{t6.rpm:.0f}, 3 mm: {t3.feed:.0f} mm/min) and keep the adaptive stepover at 15 % so the 6 mm cutter can run full depth.",
                 "narr": "Feeds and speeds come from a calculator (13 materials, clamped to the machine's spindle and feed limits), not from memory."},
                {"kind": "tool", "name": "build_cam", "detail": f"{len(CAM_CODE.splitlines())} lines", "code": CAM_CODE, "result": prog.summary().splitlines()[0],
                 "narr": "CAM is a second script, cam.py, saved next to the design. Each line is one operation on geometry taken straight from the exact model: section, holes, stock_minus."},
                {"kind": "toolpaths",
                 "narr": "Toolpaths draw over the model: rapids red, each operation its own colour, the stock as a yellow box. The orange spiral is the adaptive op: the tool never exceeds its target engagement, so it can cut full flute depth."},
                {"kind": "agent", "text": prog.summary().replace("\n", "  \n"),
                 "narr": "The program summary: every op with its tool, cut length, time and depths, plus warnings. The CAM tab shows the same with visibility toggles and a simulation slider."},
                {"kind": "tool", "name": "export_gcode", "detail": 'name="plate"', "result": f"wrote workspace/exports/plate.nc — {len(lines)} lines, G0 {sum(l.startswith('G0 ') for l in lines)}, G1 {sum(l.startswith('G1 ') for l in lines)}, arcs {sum(l.startswith(('G2 ', 'G3 ')) for l in lines)}, tool changes {sum(l.startswith('M6') for l in lines)}",
                 "narr": "export_gcode runs the GRBL post: arcs fitted where the path is circular, collinear moves merged, tool changes with a pause because the 3018 has no changer."},
                {"kind": "gcode", "narr": "The head of the exported file: the warnings, units and WCS, then the first tool. G2/G3 appear where the path is circular."},
                {"kind": "agent", "text": "Exported **plate.nc**. Two things to check before you run it: " + warn + ". The Ø5 holes are only piloted at Ø3 — say the word and I'll add a 3 mm helical bore to open them to size.",
                 "narr": "The agent passes the post's warnings on instead of hiding them. That is the end of the series; the docs cover every tool and setting in depth."},
            ]}

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--out", required=True); a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    index = []
    for fn in (tutorial_first_part, tutorial_select_face, tutorial_sketch, tutorial_ribbon, tutorial_cam):
        t0 = time.time(); t = fn()
        (out / f"{t['id']}.json").write_text(json.dumps(t, separators=(",", ":")))
        index.append({"id": t["id"], "title": t["title"], "minutes": t["minutes"], "intro": t["intro"], "steps": len(t["steps"])})
        print(f"{t['id']}: {len(t['steps'])} steps, {(out / (t['id'] + '.json')).stat().st_size // 1024} KB, {time.time() - t0:.1f}s")
    (out / "index.json").write_text(json.dumps(index, indent=1))

if __name__ == "__main__":
    main()
