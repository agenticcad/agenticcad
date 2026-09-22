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

def tutorial_first_part():
    m1 = build(PLATE); m2 = build(PLATE_FILLET)
    return {"id": "first-part", "title": "Your first part", "minutes": 3,
            "intro": "Describe a part in plain words. The agent writes build123d code, the exact kernel builds it, and the viewer updates. You never see a sketch unless you want one.",
            "meshes": {"m1": mesh_of(m1), "m2": mesh_of(m2)},
            "steps": [
                {"kind": "user", "text": "Make a 60 × 40 × 8 mm mounting plate with four Ø5 holes 8 mm in from each corner, and a Ø20 boss 15 mm tall in the middle with a Ø8 through bore. Call it Plate."},
                {"kind": "agent", "text": "I'll build that as one body with named dimensions so you can tweak them later."},
                {"kind": "tool", "name": "build_model", "detail": f"{len(PLATE.splitlines())} lines", "code": PLATE, "result": m1.summary(0).splitlines()[0]},
                {"kind": "model", "mesh": "m1", "caption": "The viewer updates the moment the script builds. Every face is exact B-rep; the triangles are display only."},
                {"kind": "agent", "text": f"Done — **Plate**: 60 × 40 × 8 plate, Ø20 × 15 boss with a Ø8 through bore, four Ø5 holes on a 44 × 24 pattern. Volume {m1.volume:,.0f} mm³. Dimensions are parameters at the top of the script (plate_l, boss_d, hole_d…), so say the word to change any of them."},
                {"kind": "note", "text": "Open the **Design** tab and the same dimensions appear as editable fields. Change one and the part rebuilds — no agent turn needed."},
                {"kind": "user", "text": "Fillet the four vertical corner edges 4 mm, then export a STEP."},
                {"kind": "tool", "name": "build_model", "detail": f"{len(PLATE_FILLET.splitlines())} lines", "code": PLATE_FILLET, "result": m2.summary(0).splitlines()[0]},
                {"kind": "model", "mesh": "m2", "caption": "Fillets are true cylindrical faces. STEP export writes them as CYLINDRICAL_SURFACE, not triangles."},
                {"kind": "tool", "name": "export_model", "detail": 'formats=["step"]', "result": "exported: workspace/exports/plate.step"},
                {"kind": "agent", "text": "Filleted the four vertical plate edges at R4 and exported **plate.step** (exact geometry, one solid). The hole pattern and boss are unchanged."},
            ]}

def tutorial_select_face():
    m1 = build(PLATE_FILLET)
    top = face_id(m1, lambda f: f.kind == "PLANE" and f.normal and f.normal[2] > 0.99 and abs(f.center[2] - 19) < 1e-3)
    CH = PLATE_FILLET.replace("result = {", "boss_top = plate.part.faces().sort_by(Axis.Z)[-1]\nplate_part = plate.part.chamfer(1, None, [boss_top.edges().filter_by(GeomType.CIRCLE).sort_by(SortBy.RADIUS)[-1]])\nresult = {").replace('"Plate": plate.part', '"Plate": plate_part')
    m2 = build(CH)
    fi = next(f for f in m1.faces if f.id == top)
    return {"id": "select-face", "title": "Point at a face and ask", "minutes": 2,
            "intro": "Click geometry in the viewer to talk about it. The selection travels with your message as exact type, centre and normal, so the agent picks the same face in code.",
            "meshes": {"m1": mesh_of(m1), "m2": mesh_of(m2)},
            "steps": [
                {"kind": "model", "mesh": "m1", "caption": "Start from the filleted plate."},
                {"kind": "select", "face": top, "caption": f"You click the boss top. It highlights and a chip appears in the composer: {fi.short_label()}."},
                {"kind": "user", "text": "Put a 1 mm chamfer on the outer edge of this face.", "chips": [fi.short_label()]},
                {"kind": "agent", "text": "Selected face: plane +Z at z = 19, Ø20. I'll chamfer its outer circular edge."},
                {"kind": "tool", "name": "build_model", "detail": f"{len(CH.splitlines())} lines", "code": CH, "result": m2.summary(0).splitlines()[0]},
                {"kind": "model", "mesh": "m2", "caption": "A new conical face appears on the boss. The inner bore edge is untouched."},
                {"kind": "agent", "text": "Chamfered the outer top edge of the boss 1 × 45°; the bore edge is left sharp. The face count went from %d to %d." % (len(m1.faces), len(m2.faces))},
                {"kind": "user", "text": "How thick is the plate, and how far apart are the holes along X?"},
                {"kind": "tool", "name": "measure", "detail": "faces=[top, bottom] · edges=[hole A, hole B]", "result": "plane gap 8.000 mm · centre distance 44.000 mm"},
                {"kind": "agent", "text": "Plate thickness **8.00 mm** (top and bottom faces are parallel, gap 8.000). Hole centres are **44.00 mm** apart along X and 24.00 mm along Y — measured, not read from the code."},
            ]}

def tutorial_sketch():
    plane = {"origin": [0, 0, 4], "x_dir": [1, 0, 0], "z_dir": [0, 0, 1], "label": "Plate top"}
    items = [{"type": "rect", "cx": -18, "cy": 0, "w": 14, "h": 20, "angle": 0, "mode": "add"}, {"type": "circle", "cx": -18, "cy": 0, "r": 3, "mode": "subtract"}]
    base = PLATE
    with_sketch = script_edit.set_sketch(base, "sketch1", plane, items)
    m1 = build(base); m2 = build(with_sketch)
    ext = script_edit.wrap_body_expr(with_sketch, "Plate", "({expr}) + extrude(sketch1, amount=6)")
    m3 = build(ext)
    return {"id": "sketch-to-boss", "title": "Sketch by hand, build with the agent", "minutes": 3,
            "intro": "Fusion-style sketches on any flat face: draw rectangles, circles, polygons and slots, and they become a real Sketch variable in the script that either of you can extrude, cut or revolve.",
            "meshes": {"m1": mesh_of(m1), "m2": mesh_of(m2), "m3": mesh_of(m3)},
            "steps": [
                {"kind": "model", "mesh": "m1", "caption": "The plate from the first tutorial, before the fillets."},
                {"kind": "sketchmode", "plane": plane, "items": items, "caption": "Select the plate's top face and press K (Sketch). The camera squares up to the face. Two clicks make a rectangle, two more a subtract circle — snapped to the 1 mm grid."},
                {"kind": "model", "mesh": "m2", "caption": "Finish writes the sketch into the script as an editable block; it renders as a cyan outline and shows under Sketches in the Browser."},
                {"kind": "code", "text": with_sketch[with_sketch.index("# sketch:"):with_sketch.index("# /sketch:sketch1") + len("# /sketch:sketch1")], "caption": "What landed in the script. The JSON header lets the editor reopen it; the agent is told never to rewrite it by hand."},
                {"kind": "user", "text": "Extrude sketch1 6 mm up and join it to the plate.", "chips": ["sketch: sketch1"]},
                {"kind": "tool", "name": "build_model", "detail": "extrude(sketch1, amount=6)", "code": ext[ext.index("result ="):], "result": m3.summary(0).splitlines()[0]},
                {"kind": "model", "mesh": "m3", "caption": "The boss with its bore is fused into the plate. The sketch block is unchanged, so you can still edit it."},
                {"kind": "agent", "text": "Extruded **sketch1** 6 mm along its plane normal and fused it into Plate (one body). The sketch stays in the script; edit it in the sketch editor and the boss follows."},
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
    top = face_id(m1, lambda f: f.kind == "PLANE" and f.normal and f.normal[2] > 0.99 and abs(f.center[2] - 19) < 1e-3)
    return {"id": "ribbon", "title": "Tweak it by hand — still code", "minutes": 3,
            "intro": "The ribbon works like a desktop CAD tool: pick a face or edges, type a number, OK. Under the hood every operation is written into the script, so hand edits and agent edits live in the same file.",
            "meshes": {"m1": mesh_of(m1), "m2": mesh_of(m2), "m3": mesh_of(m3), "m4": mesh_of(m4)},
            "steps": [
                {"kind": "model", "mesh": "m1", "caption": "Starting part."},
                {"kind": "ribbon", "tool": "extrude", "face": top, "fields": [["Distance", "5 mm"], ["Direction", "Outward (add)"]], "caption": "Press/Pull (Q): click the boss top, drag the handle or type 5, OK."},
                {"kind": "model", "mesh": "m2", "caption": "The boss grew 5 mm. In the script the Plate expression became `_plate + extrude(_plate.faces().sort_by_distance((7, 0, 19))[0], amount=5, dir=(0, 0, 1))` — the face is referenced by the point you clicked."},
                {"kind": "ribbon", "tool": "hole", "point": [18, 0, 4], "fields": [["Type", "Tapped M4"], ["Depth", "6 mm"]], "caption": "Hole (H): click where it goes on the plate, choose a tapped M4, 6 deep."},
                {"kind": "model", "mesh": "m3", "caption": "A tap-drill hole at Ø3.3, registered as M4×0.7 so drawings call it out correctly."},
                {"kind": "ribbon", "tool": "fillet", "face": face_id(m3, lambda f: f.kind == "PLANE" and f.normal and f.normal[2] > 0.99 and abs(f.center[2] - 24) < 1e-3), "fields": [["Radius", "2 mm"]], "caption": "Fillet (F): click the boss top face to take all of its edges, radius 2, OK."},
                {"kind": "model", "mesh": "m4", "caption": "Two fillets on the boss top: outer edge and bore edge."},
                {"kind": "code", "text": fil[fil.index("_plate ="):], "caption": "Everything you did, as code. Undo steps back through it; the agent sees a note for each change."},
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
    # subsample dense ops for the site (keep every move for short ops)
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
    return {"id": "cam", "title": "CAM in five minutes", "minutes": 4,
            "intro": "Ask for a program and get real toolpaths: facing, constant-engagement adaptive roughing, drilling, a tabbed outline, and GRBL G-code with arcs and tool changes — checked against your machine's limits.",
            "meshes": {"m1": mesh_of(m)},
            "program": payload,
            "gcode_head": "\n".join(lines[:28]),
            "steps": [
                {"kind": "model", "mesh": "m1", "caption": "The filleted plate."},
                {"kind": "user", "text": "Make a CAM program on the Generic 3018 in aluminium: face the stock, adaptive-rough around the boss with the 6 mm endmill, open the bore with the 3 mm, pilot-drill the mounting holes, then cut the outline through with 4 tabs. Stock = bbox + 3 mm, 1 mm extra on top."},
                {"kind": "tool", "name": "cam_context", "detail": "", "result": "Machines: Generic 3018 (300×180×45, 1000–10000 rpm)… Tools: T1 6 mm flat, T2 3 mm flat, T5 3 mm drill… Vertical round holes (5): Ø5 ×4 through, Ø8 through"},
                {"kind": "agent", "text": f"Aluminium on a 3018 is spindle-limited ({setup.machine.spindle["max"]:,.0f} rpm), so I'll take feeds from the calculator (6 mm: {t6.feed:.0f} mm/min at S{t6.rpm:.0f}, 3 mm: {t3.feed:.0f} mm/min) and keep the adaptive stepover at 15 % so the 6 mm cutter can run full depth."},
                {"kind": "tool", "name": "build_cam", "detail": f"{len(CAM_CODE.splitlines())} lines", "code": CAM_CODE, "result": prog.summary().splitlines()[0]},
                {"kind": "toolpaths", "caption": "Toolpaths draw over the model: rapids red, each operation its own colour. The orange spiral is the adaptive op — the tool never exceeds its target engagement, so it can run full flute depth."},
                {"kind": "agent", "text": prog.summary().replace("\n", "  \n")},
                {"kind": "tool", "name": "export_gcode", "detail": 'name="plate"', "result": f"wrote workspace/exports/plate.nc — {len(lines)} lines, G0 {sum(l.startswith('G0 ') for l in lines)}, G1 {sum(l.startswith('G1 ') for l in lines)}, arcs {sum(l.startswith(('G2 ', 'G3 ')) for l in lines)}, tool changes {sum(l.startswith('M6') for l in lines)}"},
                {"kind": "gcode", "caption": "The head of the exported file: units, WCS, a tool change with a pause for the 3018, arcs where the path is circular."},
                {"kind": "agent", "text": "Exported **plate.nc**. Two things to check before you run it: " + ("; ".join(prog.check()) or "no warnings") + ". The Ø5 holes are only piloted at Ø3 — say the word and I'll add a 3 mm helical bore to open them to size."},
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
