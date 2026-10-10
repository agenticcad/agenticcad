"""Eval cases: prompt + deterministic graders. Add a case = add a Case here."""
from __future__ import annotations

import base64
import io
import math
import re

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

# ---------------------------------------------------------------- Makera / multi-setup / 4th axis (2026-10-04)
TWO_SIDED = """# plate machined from both sides: recess on top, counterbore from below
plate = Box(60, 40, 12, align=(Align.CENTER, Align.CENTER, Align.MIN))
plate = plate - Pos(0, 0, 12) * Box(30, 20, 8)          # top recess, 4 deep
plate = plate - Pos(10, 0, 0) * Cylinder(6, 6)          # Ø12 counterbore 3 deep in the bottom face
result = {"Plate": plate}
"""
SHAFT = """# stepped shaft along X with a flat (for 4th-axis evals)
shaft = Rot(0, 90, 0) * Cylinder(10, 60, align=(Align.CENTER, Align.CENTER, Align.MIN))
shaft = shaft + Rot(0, 90, 0) * Pos(0, 0, 20) * Cylinder(14, 20, align=(Align.CENTER, Align.CENTER, Align.MIN))
shaft = shaft - Pos(50, 0, 13) * Box(14, 30, 10)
result = {"Shaft": shaft}
"""


def simulated_clean(max_left: float | None = None, ops: list[int] | None = None):
    """Run the material-removal simulation on the final program: no gouges, no rapids through material."""
    def g(run: Run):
        import cam_sim
        p = run.program
        if p is None:
            return [check("simulation", False, "no program")]
        try:
            r = cam_sim.simulate(p, ops=ops, part=run.model)
        except Exception as e:  # noqa: BLE001
            return [check("simulation ran", False, str(e))]
        st = r.stats
        out = [check("no gouges", st["gouge"]["cells"] == 0, r.summary()),
               check("no rapids through material", not st["rapid_hits"], str(st["rapid_hits"][:3]))]
        if max_left is not None:
            out.append(check(f"material left ≤ {max_left} mm", st["left"]["max"] <= max_left, r.summary()))
        return out
    return g


def makera_gcode(run: Run):
    p = run.program
    if p is None:
        return [check("makera gcode", False, "no program")]
    g = p.gcode(); lines = g.splitlines()
    return [check("Makera post", p.setup.machine.post == "makera", p.setup.machine.name),
            check("lines ≤ 63 chars", all(len(l) <= 63 for l in lines), max(lines, key=len)),
            check("no line numbers", not any(re.match(r"N\d", l) for l in lines), ""),
            check("M6 Tn tool changes", bool(re.search(r"^M6 T\d+$", g, re.M)), "")]


def two_setups(run: Run):
    p = run.program
    if p is None:
        return [check("two setups", False, "no program")]
    sts = p.setups
    return [check("≥2 setups incl. a bottom one", len(sts) >= 2 and any(s.orient == "bottom" for s in sts), str([(s.name, s.orient) for s in sts])),
            check("operator pause between setups", "M600" in p.gcode(), "")]


def rotary_program(run: Run):
    p = run.program
    if p is None:
        return [check("rotary program", False, "no program")]
    return [check("4th-axis setup", any(s.rotary for s in p.setups), str([s.describe() for s in p.setups])),
            check("continuous A moves", any(o.uses_a for o in p.ops), str([o.kind for o in p.ops])),
            check("rough + finish", {"rotary_rough", "rotary_finish"} <= {o.kind for o in p.ops}, str([o.kind for o in p.ops])),
            check("ball finish", any(o.kind == "rotary_finish" and o.tool.type == "ball" for o in p.ops), "")]


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


# ---- showcase assemblies (big multi-body models for the speed video)
def _c3(b):
    return tuple((lo + hi) / 2 for lo, hi in zip(b.bbox_min, b.bbox_max))


def _size(b):
    return tuple(hi - lo for lo, hi in zip(b.bbox_min, b.bbox_max))


def no_overlaps(tol: float = 1.0):
    """Real interference check: intersect every pair of bodies whose boxes overlap (a compound's volume can't show it)."""
    def g(run: Run):
        m = run.model
        if m is None:
            return [check("no interference", False, "no model")]
        bad = []
        bs = m.bodies
        for i, a in enumerate(bs):
            for b in bs[i + 1:]:
                if any(a.bbox_max[k] <= b.bbox_min[k] + 1e-6 or b.bbox_max[k] <= a.bbox_min[k] + 1e-6 for k in range(3)):
                    continue
                x = a.shape & b.shape
                v = x.volume if x is not None else 0.0
                if v > tol:
                    bad.append(f"{a.name}×{b.name} {v:.1f}mm³")
        return [check("no interference between bodies", not bad, ", ".join(bad[:6]))]
    return g


def _dist_xz(a, b):
    ca, cb = _c3(a), _c3(b)
    return math.hypot(ca[0] - cb[0], ca[2] - cb[2])


def _named(run: Run, *names):
    m = run.model
    got = {n: m.body_by_name(n) for n in names} if m else {}
    missing = [n for n, b in got.items() if b is None] if m else list(names)
    return got, missing


def gearbox_checks(run: Run):
    b, missing = _named(run, "Base", "FrontPlate", "Motor", "InputShaft", "IntermediateShaft", "OutputShaft",
                        "Pinion1", "Gear1", "Pinion2", "Gear2")
    if missing:
        return [check("gearbox bodies present", False, f"missing {missing}")]
    out = [check("stage 1 centre distance 42", abs(_dist_xz(b["Pinion1"], b["Gear1"]) - 42) < 0.3, f"{_dist_xz(b['Pinion1'], b['Gear1']):.2f}"),
           check("stage 2 centre distance 42", abs(_dist_xz(b["Pinion2"], b["Gear2"]) - 42) < 0.3, f"{_dist_xz(b['Pinion2'], b['Gear2']):.2f}")]
    for n, d in (("Pinion1", 24), ("Pinion2", 24), ("Gear1", 66), ("Gear2", 66)):
        s = _size(b[n])
        out.append(check(f"{n} Ø{d} × 10 along Y", abs(s[0] - d) < 0.8 and abs(s[2] - d) < 0.8 and abs(s[1] - 10) < 0.3, f"size {tuple(round(v, 1) for v in s)}"))
    for g, sh in (("Pinion1", "InputShaft"), ("Gear1", "IntermediateShaft"), ("Pinion2", "IntermediateShaft"), ("Gear2", "OutputShaft")):
        out.append(check(f"{g} on {sh}", _dist_xz(b[g], b[sh]) < 0.3, f"{_dist_xz(b[g], b[sh]):.2f}"))
    ms = _size(b["Motor"])
    out.append(check("Motor 42.3 square × 40", abs(ms[0] - 42.3) < 0.6 and abs(ms[2] - 42.3) < 0.6 and abs(ms[1] - 40) < 0.6, str(tuple(round(v, 1) for v in ms))))
    out.append(check("Motor on the input axis", _dist_xz(b["Motor"], b["InputShaft"]) < 0.5, f"{_dist_xz(b['Motor'], b['InputShaft']):.2f}"))
    radii = [round(f.radius, 2) for f in run.model.faces if f.kind == "CYLINDER"]
    fp = b["FrontPlate"]
    fp_r = [f.radius for f in run.model.faces if f.kind == "CYLINDER" and f.body_name == fp.name]
    out.append(check("front plate: 4 motor screw holes Ø3.4", sum(1 for r in fp_r if abs(r - 1.7) < 0.05) >= 4, str(sorted(set(round(r, 2) for r in fp_r)))))
    out.append(check("front plate: Ø22.5 motor pilot", any(abs(r - 11.25) < 0.05 for r in fp_r), str(sorted(set(radii)))[:120]))
    return out


def quad_checks(run: Run):
    names = ["BottomPlate", "TopPlate", "FlightController", "Battery"] + [f"Motor{i}" for i in range(1, 5)] + [f"Prop{i}" for i in range(1, 5)]
    b, missing = _named(run, *names)
    if missing:
        return [check("quad bodies present", False, f"missing {missing}")]
    motors = [b[f"Motor{i}"] for i in range(1, 5)]
    cs = [_c3(mo) for mo in motors]
    diag = max(math.dist(p[:2], q[:2]) for p in cs for q in cs)
    out = [check("220 mm motor-to-motor diagonal", abs(diag - 220) < 1.0, f"{diag:.1f}"),
           check("motors sit on the bottom plate top (z=5)", all(abs(mo.bbox_min[2] - 5) < 0.3 for mo in motors), str([round(mo.bbox_min[2], 2) for mo in motors])),
           check("motors Ø28 × 18", all(abs(_size(mo)[0] - 28) < 0.5 and abs(_size(mo)[2] - 18) < 0.5 for mo in motors), str([tuple(round(v, 1) for v in _size(mo)) for mo in motors]))]
    props = [b[f"Prop{i}"] for i in range(1, 5)]
    over = [min(motors, key=lambda mo: math.dist(_c3(mo)[:2], _c3(p)[:2])) for p in props]
    tips = [2 * max(math.hypot(v.X - _c3(mo)[0], v.Y - _c3(mo)[1]) for v in p.shape.tessellate(0.05)[0]) for p, mo in zip(props, over)]   # mesh: rounded tips have no vertex at the tip
    out.append(check("props 127 mm tip to tip", all(125.5 <= t <= 130 for t in tips), str([round(t, 1) for t in tips])))
    out.append(check("props centred over their motors, above them",
                     len({id(mo) for mo in over}) == 4 and all(math.dist(_c3(p)[:2], _c3(mo)[:2]) < 1.0 and p.bbox_min[2] >= mo.bbox_max[2] - 0.3 for p, mo in zip(props, over)),
                     str([(round(math.dist(_c3(p)[:2], _c3(mo)[:2]), 2), round(p.bbox_min[2], 2)) for p, mo in zip(props, over)])))
    fc = _size(b["FlightController"])
    out.append(check("flight controller 36 × 36 × 1.6", abs(fc[0] - 36) < 0.5 and abs(fc[1] - 36) < 0.5 and abs(fc[2] - 1.6) < 0.2, str(tuple(round(v, 2) for v in fc))))
    out.append(check("battery sits on the top plate", abs(b["Battery"].bbox_min[2] - b["TopPlate"].bbox_max[2]) < 0.3, f"{b['Battery'].bbox_min[2]:.2f} vs {b['TopPlate'].bbox_max[2]:.2f}"))
    bp = b["BottomPlate"].shape
    m3 = cam.holes(bp, 3.05, 3.35); m5 = cam.holes(bp, 4.85, 5.15)
    want3 = [(c[0] + dx, c[1] + dy) for c in cs for dx in (-8, 8) for dy in (-8, 8)]            # 16 × 16 motor patterns...
    want3 += [(dx, dy) for dx in (-15.25, 15.25) for dy in (-15.25, 15.25)]                    # ...and the 30.5 stack
    rot3 = [(c[0] + r * math.cos(a), c[1] + r * math.sin(a)) for c in cs for r in (8 * math.sqrt(2),)   # pattern may be turned 45°
            for a in (math.atan2(c[1], c[0]) + math.pi / 4 + k * math.pi / 2 for k in range(4))]
    has = lambda pts: sum(any(math.dist(q, (h.x, h.y)) < 0.4 for h in m3) for q in pts)  # noqa: E731
    n_motor = max(has(want3[:16]), has(rot3))
    out.append(check("bottom plate: Ø3.2 motor patterns (16) and stack holes (4)", n_motor == 16 and has(want3[16:]) == 4,
                     f"motor {n_motor}/16, stack {has(want3[16:])}/4, {len(m3)} Ø3.2 holes in all"))
    out.append(check("bottom plate: Ø5 shaft hole under each motor", all(any(math.dist(c[:2], (h.x, h.y)) < 0.4 for h in m5) for c in cs), f"{len(m5)} Ø5 holes"))
    return out


# ---- showcase: NEMA 17 stepper + planetary gearhead, every part modelled (reference: evals/reference/planetary_gearhead.py)
def _teeth(shape, r, z, c=(0.0, 0.0), n=720):
    from build123d import Vector
    ins = [shape.is_inside(Vector(c[0] + r * math.cos(2 * math.pi * k / n), c[1] + r * math.sin(2 * math.pi * k / n), z)) for k in range(n)]
    return sum(1 for k in range(n) if ins[k] and not ins[k - 1])


def _first_tooth_angle(shape, r, z, n=1440):
    from build123d import Vector
    ins = [shape.is_inside(Vector(r * math.cos(2 * math.pi * k / n), r * math.sin(2 * math.pi * k / n), z)) for k in range(n)]
    return next(360 * k / n for k in range(n) if ins[k] and not ins[k - 1])


def interference_free(strict: float = 0.05, fastener: float = 6.0):
    """Pairwise solid intersection. Mating real threads (a body named *Screw*) may overlap by up to `fastener` mm³."""
    def g(run: Run):
        m = run.model
        if m is None:
            return [check("no interference", False, "no model")]
        bad = []
        bs = m.bodies
        for i, a in enumerate(bs):
            for b in bs[i + 1:]:
                if any(a.bbox_max[k] <= b.bbox_min[k] + 1e-6 or b.bbox_max[k] <= a.bbox_min[k] + 1e-6 for k in range(3)):
                    continue
                try:
                    x = a.shape & b.shape
                    v = x.volume if x is not None else 0.0
                except ValueError:                     # empty intersection comes back as a null shape
                    v = 0.0
                tol = fastener if ("screw" in a.name.lower() or "screw" in b.name.lower()) else strict
                if v > tol:
                    bad.append(f"{a.path}×{b.path} {v:.2f}mm³")
        return [check("no interference between any of the parts", not bad, "; ".join(bad[:8]) + (f" (+{len(bad) - 8})" if len(bad) > 8 else ""))]
    return g


GEARHEAD_NAMES = ["FrontCap", "Stator", "RearCap", "Shaft", "RotorCupA", "RotorCupB", "RotorMagnet"] + [f"Coil{i}" for i in range(1, 9)] + \
    ["AdapterPlate", "RingGear", "Sun", "SetScrew", "Planet1", "Planet2", "Planet3", "CarrierRearPlate", "Carrier", "FrontCover",
     "OutputKey"] + [f"HousingScrew{i}" for i in range(1, 5)]


def gearhead_checks(run: Run):
    m = run.model
    b = {n: m.body_by_name(n) for n in GEARHEAD_NAMES}
    missing = [n for n, x in b.items() if x is None]
    out = [check("all named parts present", not missing, f"missing {missing}")]
    if missing:
        return out
    zc = lambda x: (x.bbox_min[2] + x.bbox_max[2]) / 2  # noqa: E731
    # gears: tooth counts on the pitch circles, coaxial sun, planets on a 10.8 radius 120° apart
    out.append(check("sun: 18 teeth, on the motor axis", _teeth(b["Sun"].shape, 5.4, zc(b["Sun"]) + 2) == 18 and math.hypot(*_c3(b["Sun"])[:2]) < 0.05,
                     f"{_teeth(b['Sun'].shape, 5.4, zc(b['Sun']) + 2)} teeth"))
    pc = [_c3(b[f"Planet{i}"]) for i in (1, 2, 3)]
    out.append(check("planets: 18 teeth each", all(_teeth(b[f"Planet{i}"].shape, 5.4, pc[i - 1][2], pc[i - 1][:2]) == 18 for i in (1, 2, 3)),
                     str([_teeth(b[f"Planet{i}"].shape, 5.4, pc[i - 1][2], pc[i - 1][:2]) for i in (1, 2, 3)])))
    rad = [math.hypot(p[0], p[1]) for p in pc]
    angs = sorted(math.degrees(math.atan2(p[1], p[0])) % 360 for p in pc)
    gaps = [(angs[(i + 1) % 3] - angs[i]) % 360 for i in range(3)]
    out.append(check("planets on a 10.8 mm radius, 120° apart", all(abs(r - 10.8) < 0.1 for r in rad) and all(abs(gp - 120) < 0.5 for gp in gaps),
                     f"radii {[round(r, 2) for r in rad]}, gaps {[round(gp, 1) for gp in gaps]}"))
    rt = _teeth(b["RingGear"].shape, 16.2, zc(b["RingGear"]))
    out.append(check("ring gear: 54 internal teeth", rt == 54, f"{rt}"))
    # motor internals: 50-tooth rotor cups offset half a tooth, 8-pole stator with small teeth on the poles
    za, zb = zc(b["RotorCupA"]), zc(b["RotorCupB"])
    ta, tb = _teeth(b["RotorCupA"].shape, 10.85, za), _teeth(b["RotorCupB"].shape, 10.85, zb)
    out.append(check("rotor cups: 50 teeth each", ta == 50 and tb == 50, f"{ta}, {tb}"))
    off = (_first_tooth_angle(b["RotorCupB"].shape, 10.85, zb) - _first_tooth_angle(b["RotorCupA"].shape, 10.85, za)) % 7.2
    out.append(check("rotor cups offset by half a tooth (3.6°)", abs(off - 3.6) < 0.6, f"{off:.2f}°"))
    zs = zc(b["Stator"])
    poles = _teeth(b["Stator"].shape, 14.0, zs)
    out.append(check("stator: 8 poles", poles == 8, f"{poles}"))
    ptee = max(_teeth(b["Stator"].shape, r, zs, n=2160) for r in (11.2, 11.3, 11.4, 11.5, 11.6))
    out.append(check("stator poles carry small teeth (≥ 32 around the bore)", ptee >= 32, f"{ptee}"))
    ss = _size(b["Stator"])
    out.append(check("NEMA 17 frame 42.3 mm square", abs(ss[0] - 42.3) < 0.2 and abs(ss[1] - 42.3) < 0.2, str(tuple(round(v, 2) for v in ss))))
    # bearings: real balls and raceway grooves
    balls = [x for x in m.bodies if x.shape.faces() and all(f.geom_type.name == "SPHERE" for f in x.shape.faces())]
    out.append(check("bearings have real balls (≥ 28)", len(balls) >= 28, f"{len(balls)} balls"))
    torus = [x for x in m.bodies if "bearing" in x.path.lower() and any(f.geom_type.name == "TORUS" for f in x.shape.faces())]
    out.append(check("bearing rings have raceway grooves (≥ 8 rings)", len(torus) >= 8, f"{len(torus)}"))
    # fasteners: every screw carries a modelled helix; tapped holes are real threads too
    screws = [x for x in m.bodies if "screw" in x.name.lower()]
    helix = [x for x in screws if any(f.geom_type.name == "BSPLINE" for f in x.shape.faces())]
    out.append(check("≥ 12 screws, all with real threads", len(screws) >= 12 and len(helix) == len(screws), f"{len(helix)}/{len(screws)} threaded"))
    tapped = [t for t in m.threads if t.real and not t.external]
    # tapped holes may come from tap(real=True) (registered) or from the agent's own helix geometry: accept either
    helix_in = {n: sum(1 for f in b[n].shape.faces() if f.geom_type.name == "BSPLINE") for n in ("FrontCap", "Carrier", "FrontCover", "Sun")}
    out.append(check("real tapped holes (≥ 12 registered, or helices in the front cap, carrier, cover and sun)",
                     len(tapped) >= 12 or all(v > 0 for v in helix_in.values()), f"{len(tapped)} registered; helix faces {helix_in}"))
    ks = sorted(_size(b["OutputKey"]))
    out.append(check("3 × 3 × 12 output key", abs(ks[0] - 3) < 0.2 and abs(ks[1] - 3) < 0.2 and abs(ks[2] - 12) < 0.5, str([round(v, 2) for v in ks])))
    gap = b["OutputKey"].shape.distance_to(b["Carrier"].shape)
    kc = _c3(b["OutputKey"]); kr = math.hypot(kc[0], kc[1])
    out.append(check("key seated in the output shaft keyway", gap < 0.05 and 2.5 < kr < 4.5, f"gap {gap:.2f} mm, key centre {kr:.2f} mm off axis"))
    pins = [x for x in m.bodies if "connector" in x.path.lower() and "pin" in x.name.lower()]
    out.append(check("connector with 6 pins", len(pins) == 6, f"{len(pins)}"))
    out.append(check("says the ratio is 4:1", bool(re.search(r"\b4(\.0)?\s*:\s*1\b", run.text)), run.text[-200:]))
    return out


def machine_model_checks(name_sub: str, table: tuple[float, float] | None = None, clearance: float | None = None,
                         travel: tuple[float, float, float] | None = None, min_parts: int = 6, tol: float = 0.08,
                         moving: dict[str, str] | None = None):
    """The agent saved a machine model script for a library machine matching `name_sub`; it runs, its numbers match the
    brief within `tol` (relative), it has enough parts, collision parts, a nose profile, and the axes move the right
    bodies (`moving` = {axis: 'head'|'table'})."""
    def g(run: Run):
        import machine_script as ms
        ws = run.workspace
        ms_dir = ws / "machines"
        recs = {m.name: m for m in run.agent.library.machines().values()}
        name = next((n for n in recs if name_sub.lower() in n.lower()), None)
        out = [check(f"machine '{name_sub}' in the library", name is not None, str(list(recs)))]
        if name is None:
            return out
        p = ms.user_path(name, ws)
        out.append(check("model script saved", p is not None and p.exists(), str(sorted(x.name for x in ms_dir.glob('*.machine.py')))))
        if not (p and p.exists()):
            return out
        try:
            model = ms.run(p.read_text(), recs[name], ws)
        except Exception as e:  # noqa: BLE001
            return out + [check("script runs", False, str(e))]
        out.append(check("script runs", True, ms.summary(model)))
        out.append(check(f"≥{min_parts} parts", len(model.parts) >= min_parts, f"{len(model.parts)} parts"))
        out.append(check("collision parts marked", any(p.collision for p in model.parts), str([p.name for p in model.parts if p.collision])))
        out.append(check("no invalid solids", not model.warnings, str(model.warnings)))
        rel = lambda a, b: abs(a - b) <= tol * max(abs(b), 1.0)
        if table:
            out.append(check(f"table ≈ {table[0]} × {table[1]}", rel(model.table["x"], table[0]) and rel(model.table["y"], table[1]), f"{model.table.get('x')} × {model.table.get('y')}"))
        if clearance is not None:
            out.append(check(f"clearance ≈ {clearance}", rel(model.clearance, clearance), str(model.clearance)))
        if travel:
            out.append(check(f"travel ≈ {travel}", all(rel(a, b) for a, b in zip(model.travel, travel)), str(model.travel)))
        for ax, mode in (moving or {}).items():
            got = [n.mode for n in model.nodes if n.axis == ax]
            out.append(check(f"{ax.upper()} axis moves the {mode}", mode in got, f"{ax}: {got}"))
        # the nose profile starts small (a collet nut / nose) and sits at the clearance height at home
        out.append(check("nose profile from a collet nut/nose", model.nose and model.nose[0]["r"] <= 40, str(model.nose[:2])))
        out.append(check("home nose height = clearance", abs(model.home[2] - model.clearance) < 1e-6, f"home {model.home}, clearance {model.clearance}"))
        return out
    return g


def reference_photos(folder: str) -> list[dict] | None:
    """Reference photos for a case from evals/reference/<folder>/*.jpg|png (not in the repo: vendor images stay local)."""
    import pathlib
    d = pathlib.Path(__file__).parent / "reference" / folder
    files = sorted([*d.glob("*.jpg"), *d.glob("*.png")]) if d.exists() else []
    return [{"name": f.name, "data": base64.b64encode(f.read_bytes()).decode(), "mime": "image/png" if f.suffix == ".png" else "image/jpeg"} for f in files] or None


def fixture_used(name_sub: str | None = None):
    """The program's first setup holds the stock in a fixture (optionally one whose name contains name_sub)."""
    def g(run: Run):
        p = run.program
        if p is None:
            return [check("program built", False, "no program")]
        fx = p.setup.fixture_payload() if hasattr(p.setup, "fixture_payload") else None
        out = [check("setup uses a fixture", fx is not None, str(fx))]
        if name_sub and fx:
            out.append(check(f"fixture is a '{name_sub}'", name_sub.lower() in fx["name"].lower(), fx["name"]))
        return out
    return g


def fixture_script_checks(name_sub: str, min_parts: int = 4, clamp_axis: str | None = "y"):
    """The agent saved a fixture script whose name contains name_sub; it runs with a sample stock and seats it."""
    def g(run: Run):
        import fixture_lib as fl
        fx = [f for f in fl.list_fixtures(run.workspace) if f["user"] and name_sub.lower() in f["name"].lower()]
        out = [check(f"fixture script '{name_sub}' saved", bool(fx), str([f["name"] for f in fl.list_fixtures(run.workspace) if f["user"]]))]
        if not fx:
            return out
        try:
            m = fl.model_for(fx[0]["name"], {}, {"x": 60, "y": 40, "z": 12}, run.workspace)
        except Exception as e:  # noqa: BLE001
            return out + [check("script runs", False, str(e))]
        out += [check("script runs", True, fl.summary(m)), check(f"≥{min_parts} parts", len(m.parts) >= min_parts, str(len(m.parts))),
                check("collision parts", any(p.collision for p in m.parts), ""), check("work origin above the table", m.height > 0, str(m.height))]
        if clamp_axis:
            out.append(check(f"clamps along {clamp_axis}", m.clamp_axis == clamp_axis, str(m.clamp_axis)))
        return out
    return g


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
                               check("answer in inches", "inch" in run.text.lower() or '"' in run.text or re.search(r"\d\s*in\b", run.text) is not None, run.text[:200])]]),
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
    Case("cam_makera_two_sided", tags=["cam", "makera", "setups"], initial_code=TWO_SIDED,
         prompt=("CAM on my Makera Z1 in aluminium with the 1/8\" flat endmill (feeds from the calculator). Stock: part bbox + 3 mm "
                 "margin, 1 mm extra on top. Top setup: face the top, clear the 30 × 20 recess and cut the outline down to 6 mm. "
                 "Then flip it: bottom setup to cut the Ø12 counterbore and finish the outline from below. "
                 "Simulate it and tell me if anything is wrong."),
         graders=[no_agent_error(), expect_program(min_ops=4, no_warnings_matching="travel|rpm|collet"), two_setups, makera_gcode,
                  expect_tools_used("simulate_cam"), simulated_clean()]),
    Case("cam_makera_4axis_shaft", tags=["cam", "makera", "4axis"], initial_code=SHAFT,
         prompt=("Machine this shaft on my Makera Z1 with the 4th axis from Ø32 aluminium bar, 62 mm long starting at x = -1. "
                 "Rough it with the 1/8\" flat endmill, then finish with a continuous spiral using a 1/8\" ball endmill "
                 "(add it to the tool library as T7 if it isn't there). Simulate the program and report gouges or leftover material."),
         graders=[no_agent_error(), expect_program(min_ops=2, no_warnings_matching="travel|rpm|collet|4th axis"), rotary_program,
                  makera_gcode, expect_tools_used("simulate_cam"), simulated_clean()]),
    Case("cad_showcase_gearbox", tags=["cad", "showcase", "gears"],
         prompt=("Model a two-stage spur reduction gearbox, 9:1, as a multi-body assembly. All gears module 1.5, 20° pressure angle, 10 mm face width, 8 mm bores. "
                 "Base: 160 × 90 × 8 mm plate centred on the origin, z 0 to 8, with four Ø6.6 mounting holes 10 mm in from each corner. "
                 "FrontPlate and BackPlate: 140 mm long (X) × 70 mm tall × 6 mm thick side plates standing on the base, parallel to XZ, inner faces at y = ±30. "
                 "Three Ø8 shafts along Y at z = 50: InputShaft at x = -42, IntermediateShaft at x = 0, OutputShaft at x = 42. Input and intermediate shafts run between the plates' outer faces (y -36 to 36); "
                 "the output shaft runs from y -36 out the back to y 60. The plates get Ø8 bores where the shafts pass. "
                 "Stage 1 at y = -12: a 14-tooth Pinion1 on the input shaft meshing with a 42-tooth Gear1 on the intermediate shaft. "
                 "Stage 2 at y = +12: a 14-tooth Pinion2 on the intermediate shaft meshing with a 42-tooth Gear2 on the output shaft. "
                 "Motor: a NEMA 17 body, 42.3 mm square × 40 mm, on the outside of the front plate (y -36 to -76), coaxial with the input shaft; "
                 "the front plate gets its 4 × Ø3.4 screw holes on a 31 mm square and a Ø22.5 pilot hole instead of the input bore. "
                 "Eleven bodies, none overlapping."),
         graders=[no_agent_error(), expect_bodies(["Base", "FrontPlate", "BackPlate", "InputShaft", "IntermediateShaft", "OutputShaft",
                                                   "Pinion1", "Gear1", "Pinion2", "Gear2", "Motor"], count=11),
                  expect_holes(4, diameter=6.6, body="Base"), no_overlaps(), gearbox_checks]),
    Case("cad_showcase_quad", tags=["cad", "showcase"],
         prompt=("Model a 5-inch FPV quadcopter as separate bodies, X layout, 220 mm motor-to-motor diagonal, centred on the origin. "
                 "BottomPlate: one 5 mm plate (z 0 to 5): an 80 × 40 mm centre with four 22 mm wide arms running diagonally out to the motors, with rounded ends. "
                 "At each motor: four Ø3.2 holes on a 16 × 16 mm square and a Ø5 centre hole. In the centre: four Ø3.2 stack holes on a 30.5 mm square. "
                 "Four Ø5 × 6 mm spacers (Spacer1-4) on the stack holes carry a 36 × 36 × 1.6 mm FlightController board. "
                 "Four Ø5 × 25 mm standoffs (Standoff1-4) at (±34, ±15) carry a 2 mm, 80 × 40 mm TopPlate (z 30 to 32). "
                 "A 75 × 35 × 30 mm Battery sits centred on the top plate. "
                 "Motor1-4: Ø28 × 18 mm on the arm ends, sitting on the plate. Prop1-4: two-blade 5-inch props on top of each motor, "
                 "127 mm tip to tip, blades 12 mm wide and 3 mm thick, with a Ø12 × 6 mm hub. Twenty bodies, none overlapping."),
         graders=[no_agent_error(), expect_bodies(count=20), no_overlaps(), quad_checks]),
    Case("cad_showcase_planetary", tags=["cad", "showcase", "gears", "threads"],
         prompt=(
             "Model a NEMA 17 stepper motor with a 4:1 planetary gearhead, every physical part as its own body, at production-drawing detail. "
             "No simplified stand-ins: real involute gears, real bearings with balls, real helical threads on every screw and tapped hole. "
             "Motor axis is Z, motor front face at z = 0. Components: Motor and Gearhead, bearings as sub-components.\n"
             "MOTOR (40 mm hybrid stepper): FrontCap (aluminium, z -8 to 0, 42.3 mm square, chamfered corners, Ø22 × 2 mm pilot boss, "
             "four M3 tapped holes 4.5 deep on the 31 mm square from the front and 3 deep from the back, a 625 bearing seat and end-winding pocket inside). "
             "Stator (laminated, z -30 to -8, 42.3 square with chamfered corners, 8 poles with 6 small teeth on each pole face, 22.2 bore). "
             "Coil1-Coil8 wound round the pole necks, end turns sitting in the cap pockets. RotorCupA and RotorCupB (50 teeth, Ø22, 9 mm long, "
             "offset half a tooth) with an axially magnetised RotorMagnet between them. Shaft Ø5 from z -35 to +24 with a 0.5 mm D-flat on the last 15 mm. "
             "FrontBearing and RearBearing: 625 (5 × 16 × 5). RearCap (z -40 to -30) with a JST-PH 6-pin Connector (housing and 6 pins) in a pocket in its side. "
             "Four M3 × 35 socket screws from the back through the rear cap and stator into the front cap.\n"
             "GEARHEAD (module 0.6, 20°): AdapterPlate (z 0 to 9, 42.3 square, pilot recess underneath). "
             "RingGear (z 9 to 27, 42.3 square outside, 54 internal teeth; sketch the tooth profile and extrude it). "
             "Sun (18 teeth, z 13 to 21.8, Ø10 hub below it, D-bore on the motor shaft, radial M3 SetScrew with hex socket on the flat). "
             "Planet1-3 (18 teeth, 8 wide, z 13.5 to 21.5) on bronze bushings and Ø3 planet pins, thrust washers each side. "
             "Two-plate planet carrier: CarrierRearPlate (Ø30 × 2) held by three M2 countersunk screws into posts on the Carrier, "
             "whose front plate carries a Ø8 output shaft to z 60 with a keyway and a 3 × 3 × 12 OutputKey. "
             "Output shaft in two 688 bearings (8 × 16 × 5) in the FrontCover (z 27 to 40, Ø22 pilot boss, four M3 mounting holes on a 28 mm circle), "
             "with a spacer below them, an internal circlip and an E-clip on the shaft. "
             "Four M3 × 40 socket HousingScrew1-4 through cover, ring and adapter into the motor.\n"
             "Nothing may overlap anything else. Tell me the ratio when you are done."),
         graders=[no_agent_error(), interference_free(), gearhead_checks,
                  lambda run: [check("≥ 95 bodies", run.model is not None and len(run.model.bodies) >= 95, f"{len(run.model.bodies) if run.model else 0}")]]),
]


# --------------------------------------------------------------------------- extensions (the agent extends the app itself)
def _ext(run: Run):
    import extensions as X
    return X.Extensions(run.workspace)


def ext_saved(kinds: dict[str, int]):
    """A workspace extension exists, loads, and registers at least these entry kinds (panels/tools/hooks/helpers)."""
    def g(run: Run):
        mods = [m for m in _ext(run).modules() if m["layer"] == "workspace"]
        out = [check("workspace extension saved", bool(mods), f"files: {[p.name for p in (run.workspace / 'extensions').glob('*.py')] if (run.workspace / 'extensions').exists() else []}")]
        if not mods:
            return out
        out.append(check("extension loads", not any(m["error"] for m in mods), "; ".join(m["error"] for m in mods if m["error"])))
        for k, n in kinds.items():
            have = sum(len(m[k]) for m in mods)
            out.append(check(f"registers ≥{n} {k}", have >= n, f"{k}: {have}"))
        return out
    return g


def ext_panel_renders(need_types: list[str] = (), min_children: int = 1):
    """Every workspace panel renders against the current model, with the control types asked for somewhere in it."""
    def g(run: Run):
        import extensions as X
        E = _ext(run)
        panels = [p for p in E.panels() if p.layer == "workspace"]
        if not panels:
            return [check("panel renders", False, "no workspace panel")]
        out = []
        for p in panels:
            try:
                ui = E.render(p.id, X.Ctx(run.model, run.workspace, state={}))
            except Exception as ex:  # noqa: BLE001
                out.append(check(f"panel {p.name} renders", False, f"{type(ex).__name__}: {ex}")); continue
            types = set()
            def walk(n):
                types.add(n.get("type")); [walk(c) for c in n.get("children", []) or []]
            walk(ui)
            out.append(check(f"panel {p.name} renders", len(ui["children"]) >= min_children, f"{len(ui['children'])} children"))
            for t in need_types:
                out.append(check(f"panel {p.name} has a {t}", t in types, f"types {sorted(x for x in types if x)}"))
        return out
    return g


def ext_hook_warns(code: str, needle: str):
    """Build `code` (which should trip the check) and expect a hook warning containing `needle`."""
    def g(run: Run):
        import extensions as X
        m = ck.run_script(code, workspace=run.workspace)
        warns = _ext(run).run_hooks(X.Ctx(m, run.workspace))
        return [check(f"on_build warns about {needle!r}", any(needle.lower() in w.lower() for w in warns), str(warns))]
    return g


def ext_hook_quiet(code: str):
    def g(run: Run):
        import extensions as X
        m = ck.run_script(code, workspace=run.workspace)
        warns = _ext(run).run_hooks(X.Ctx(m, run.workspace))
        return [check("on_build quiet on a good part", not warns, str(warns))]
    return g


def ext_panel_event(min_rows: int = 1):
    """A panel was shown to the browser (show_panel or an opened panel) with a table of at least min_rows rows."""
    def g(run: Run):
        evs = [e for e in run.events if e.get("type") == "ext_panel" and e.get("ui")]
        if not evs:
            return [check("panel shown", False, "no ext_panel event")]
        rows = 0
        def walk(n):
            nonlocal rows
            if n.get("type") == "table":
                rows = max(rows, len(n.get("rows") or []))
            [walk(c) for c in n.get("children", []) or []]
        for e in evs:
            walk(e["ui"])
        return [check("panel shown", True, f"{len(evs)} panel event(s)"), check(f"table with ≥{min_rows} rows", rows >= min_rows, f"{rows} rows")]
    return g


EXT_PLATE = '''wall = 1.0   # wall thickness
plate = Box(60, 40, wall)
result = {"Plate": plate, "Post": Pos(0, 0, 10) * Cylinder(4, 20)}
'''

CASES += [
    Case("machine_shapeoko_5_pro", tags=["machine", "cam"],
         prompt=("Add my Shapeoko 5 Pro 4×4 to the machine library and model it so the Machine view and the collision check work. "
                 "It's a GRBL router (Carbide Motion), fixed bed with a hybrid T-slot/MDF table 1245 × 1245 mm, the gantry moves in Y along ball-screw rails on both sides, "
                 "the Z-Plus-style carriage moves X across the gantry and Z. Travel 1220 × 1220 × 102 mm, max feed 10000 mm/min, rapid 10000. Spindle: a Carbide Compact Router, "
                 "Ø 65 mm body, 1/4\" collet with a Ø 19 mm collet nut, 12,000–30,000 rpm. The collet nut is 120 mm above the table at Z top. "
                 "The T-slot bed has 7 aluminium T-tracks 12 mm wide running in X at 165 mm pitch, MDF strips between them, 22 mm thick. "
                 "Model it, show it to me, and tell me what you estimated."),
         graders=[no_agent_error(), expect_tools_used("build_machine", "screenshot"),
                  machine_model_checks("shapeoko", table=(1245, 1245), clearance=120, travel=(1220, 1220, 102), min_parts=8,
                                       moving={"y": "head", "x": "head", "z": "head"})]),
    Case("machine_tormach_1100m", tags=["machine", "cam"],
         prompt=("Add a Tormach 1100M to the library (post linuxcnc, PathPilot) and build its machine model. Travel 457 × 279 × 419 mm (X × Y × Z), "
                 "table 876 × 240 mm with three 16 mm T-slots at 95 mm pitch, the table moves in X on a saddle that moves in Y; the spindle head moves Z on the column. "
                 "BT30 spindle, 10,000 rpm max, spindle nose Ø 70 mm; with a BT30 ER32 holder (Ø 50 mm nut, 25 mm long nut, 60 mm gauge length) the nut face is 560 mm above the table at Z top. "
                 "Full enclosure with sliding front doors. Rapids 5000 mm/min, 10-station power drawbar tool changes (atc). "
                 "Model it and check it in the viewer."),
         graders=[no_agent_error(), expect_tools_used("build_machine", "screenshot"),
                  machine_model_checks("tormach", table=(876, 240), clearance=560, travel=(457, 279, 419), min_parts=7,
                                       moving={"x": "table", "y": "table", "z": "head"})]),
    Case("machine_haas_vf4", tags=["machine", "cam"],
         prompt=("Model my Haas VF-4 properly for the Machine view and the collision check. There is a built-in VF-4 in the library "
                 "(get_machine_code shows the built-in script) but it is just blocks: I want it to look like the real machine. Verified numbers: "
                 "travel 1270 × 508 × 635 mm, table 1320.8 × 457.2 mm with five 16 mm T-slots at 80 mm pitch, spindle nose to table 106.7–741.7 mm, "
                 "CAT40 spindle (nose Ø 90), 8,100 rpm, 40+1 side-mount tool changer on the left of the column, Haas enclosure with two sliding front doors and "
                 "windows, the control pendant on the right. Model the table, saddle, base casting, column, spindle head with the CAT40 + ER32 holder "
                 "(flange Ø 63.5, ER32 nut Ø 50 × 25, gauge length 101.6), the side-mount changer carousel, the enclosure with its door windows and pendant. "
                 "Mark anything the tool could hit as collision geometry, record where every number came from, then show it to me and tell me what you estimated."),
         graders=[no_agent_error(), expect_tools_used("build_machine", "screenshot"),
                  machine_model_checks("vf-4", table=(1320.8, 457.2), clearance=741.7, travel=(1270, 508, 635), min_parts=12,
                                       moving={"x": "table", "y": "table", "z": "head"})]),
    Case("machine_makera_z1_rich", tags=["machine", "cam"],
         prompt=("Model my Makera Z1 properly: the built-in script (get_machine_code 'Makera Z1') is just blocks and I want it to look like the real "
                 "machine. Keep what is verified in it and reuse it: work area 200 × 200 × 100, bed 206 × 206 (6 mm MDF spoilboard with the 36 counterbored "
                 "holes from bed_holes('makera_z1_mdf') on a 12 mm aluminium plate), the bed moves in Y under the fixed rear bridge, the head moves X along the bridge "
                 "and Z, home = head back-left with the spindle at (-108, 101) and the nose bottom 116 above the bed at Z top, spindle nose Ø 16 × 20 (reference), "
                 "head 65 × 70 × 190 (reference), enclosure about 355 × 435 × 449 (reference), and when `fourth` is true the 4th-axis module: chuck Ø 52 with four jaws, "
                 "axis 45 above the bed and 10 mm forward of centre, tailstock with a Ø 14 live centre, max Ø 80 × 150, with the same rotary numbers. "
                 "Now make it look like a Z1: the white sheet-metal enclosure with its large tinted flip-up front window hinged at the top, the LED light strip, "
                 "the status light, the bridge with its linear rods and X carriage, the Z carriage with the brushless spindle, collet nut and dust shoe, "
                 "the wireless touch probe dock and tool-length setter near the front of the bed, cable chains, rubber feet, and the 4th-axis module when fitted. "
                 "Keep the part names 'spoilboard', 'spindle nose' and 'enclosure' and the sources keys 'work area', 'gantry clearance' and 'bed 206×206 and hole grid'. "
                 "This script must build for both 'Makera Z1' and 'Makera Z1 + 4th axis' (use the `fourth` flag), so build it for 'Makera Z1 + 4th axis' and then "
                 "also save the same script for 'Makera Z1' with build_machine. Show it to me and tell me what you estimated."),
         graders=[no_agent_error(), expect_tools_used("build_machine", "screenshot"),
                  machine_model_checks("z1 + 4th", table=(206, 206), clearance=116, travel=(200, 200, 100), min_parts=20,
                                       moving={"y": "table", "x": "head", "z": "head", "a": "table"})]),
    Case("machine_haas_vf4_photos", tags=["machine", "cam", "photos"], images=reference_photos("haas_vf4"),
         prompt=("Here are Haas's own product photos of the VF-4: front three-quarter, front angle, left angle, right side, table, tool changer and spindle. "
                 "Model my Haas VF-4 to match them as closely as you can — start from get_machine_code('Haas VF-4') (the built-in model, already detailed) and make it "
                 "more realistic against the photos: the sheet-metal enclosure shape and proportions (the sloped top, the side panels, the rear electrical cabinet), "
                 "the two sliding front doors with their windows and the door frame, the side-mount tool changer and its cover on the left, the control pendant on its arm "
                 "on the right, the spindle head and its covers, the way covers, chip auger trough, and the Haas colours (light grey panels, dark grey base, red accents). "
                 "Keep every verified number: travel 1270 × 508 × 635, table 1320.8 × 457.2 with five 16 mm T-slots at 80 mm pitch, nose-to-table 106.7–741.7, "
                 "CAT40 flange Ø 63.5, ER32 nut Ø 50 × 25, gauge 101.6, 40+1 side-mount changer. Mark what the tool can hit as collision geometry, keep the part names "
                 "'table', 'spindle nose', 'ER32 nut' and 'enclosure', record where every number came from (the photos are a source: say 'reference: Haas product photo'), "
                 "build it, compare your screenshots with the photos from the same angles, iterate at least once, and tell me what you estimated."),
         graders=[no_agent_error(), expect_tools_used("build_machine", "screenshot"),
                  machine_model_checks("vf-4", table=(1320.8, 457.2), clearance=741.7, travel=(1270, 508, 635), min_parts=25,
                                       moving={"x": "table", "y": "table", "z": "head"})]),
    Case("machine_makera_z1_photos", tags=["machine", "cam", "photos"], images=reference_photos("makera_z1"),
         prompt=("Here are Makera's own product photos of the Z1 (front, three-quarter, in use with the canopy open, side views) and of its 4th-axis module. "
                 "The built-in model (get_machine_code 'Makera Z1 + 4th axis') has the insides modelled well but its case is WRONG — it was guessed. Uplift the case to match the photos: "
                 "the Z1 is a dark charcoal lower base/plinth with the MAKERA badge and the power button, and above it one large smoked-acrylic canopy that wraps the whole "
                 "front and the top as a single tinted shell with a rounded front top edge, hinged at the rear so the whole canopy lifts up; the sides are light grey/silver "
                 "sheet panels with the diagonal split between the tinted canopy and the grey panel (the 'MAKERA Z1' lettering runs along that diagonal), an LED light bar "
                 "inside along the top rear, the bridge and spindle visible through the canopy. Keep every verified number and all the internal parts from the built-in "
                 "(bed, holes, rods, bridge, carriages, spindle, 4th axis under `fourth`), keep home (-108, 101, 116), keep the part names 'spoilboard', 'spindle nose' and "
                 "'enclosure' and the sources keys 'work area', 'gantry clearance' and 'bed 206×206 and hole grid'. Use materials: 'paint_dark' for the plinth, 'paint_light' "
                 "for the side panels, 'acrylic' for the canopy (it must stay transparent so the inside is visible), and keep the enclosure footprint about 355 × 435 × 449. "
                 "Build it for 'Makera Z1 + 4th axis', screenshot from the front, iso and right views, compare with the photos, iterate at least once until the silhouette matches, "
                 "then save the same script for 'Makera Z1' too with build_machine. Tell me what you estimated."),
         graders=[no_agent_error(), expect_tools_used("build_machine", "screenshot"),
                  machine_model_checks("z1 + 4th", table=(206, 206), clearance=116, travel=(200, 200, 100), min_parts=25,
                                       moving={"y": "table", "x": "head", "z": "head", "a": "table"})]),
    Case("machine_carvera_air_photos", tags=["machine", "cam", "photos"], images=reference_photos("carvera_air"),
         prompt=("Here are Makera's product photos of the Carvera Air (front, three-quarter, canopy open with the pendant, the MDF bed, and the 4th-axis module on the bed). "
                 "The built-in model (get_machine_code 'Carvera Air + 4th axis') is just blocks: keep its verified numbers and reuse them — work area 300 × 200 × 130, "
                 "bed 306 × 222 (15 mm MDF spoilboard with the 66 Ø6 holes from bed_holes('carvera_air_mdf') on a 15 mm aluminium plate), bed moves in Y under the fixed "
                 "rear bridge, head moves X along the bridge rods (Ø20) and Z, home = head back-left with the spindle at (-150, 100) and the nose bottom 120 above the bed at Z top, "
                 "spindle nose Ø 16 × 21 under a Ø 33.6 × 8 collar (reference), head block 120 × 98 × 200 (reference), footprint about 500 × 450 × 450, and when `fourth` is true "
                 "the 4th-axis module (chuck Ø 52 with four jaws, axis 46 above the bed, tailstock Ø 14 live centre, max Ø 92 × 200, same rotary numbers). "
                 "Now make it look like the Carvera Air in the photos: a white/light-grey lower body with the CARVERA AIR badge at the front and small feet, and above it a large "
                 "smoked-blue tinted canopy that wraps the front and the whole top as one shell (flat top, chamfered front top edge, hinged at the rear so it lifts), light grey "
                 "rear and side panels, the bridge with its linear rods, cable chains and the head visible through the canopy, the LED light bar, the touch-screen pendant on an arm "
                 "at the right side, the dust-collection hose, the tool-length setter and probe near the front of the bed. Keep the part names 'spoilboard', 'spindle nose' and "
                 "'enclosure' and the sources keys 'work area', 'gantry clearance' and 'bed 306×222 and hole grid'. Use 'paint_light' for the body, 'acrylic' for the canopy (it must "
                 "stay transparent). This script must build for both 'Carvera Air' and 'Carvera Air + 4th axis' (use `fourth`): build it for 'Carvera Air + 4th axis', screenshot from "
                 "the front, iso and right, compare with the photos, iterate at least once until the silhouette matches, then save the same script for 'Carvera Air' with build_machine. "
                 "Tell me what you estimated."),
         graders=[no_agent_error(), expect_tools_used("build_machine", "screenshot"),
                  machine_model_checks("air + 4th", table=(306, 222), clearance=120, travel=(300, 200, 130), min_parts=25,
                                       moving={"y": "table", "x": "head", "z": "head", "a": "table"})]),
    Case("cam_makera_vise", tags=["cam", "makera", "fixtures"], initial_code=TWO_SIDED,
         prompt=("CAM on my Makera Z1 in aluminium with the 1/8\" flat endmill (feeds from the calculator). Hold the stock (part bbox + 3 mm, "
                 "1 mm extra on top) in my Makera low-profile vise, jaws on the long sides. One setup from the top: face the top and clear the "
                 "30 × 20 recess. Don't cut the outline. Simulate it and tell me if anything hits the vise."),
         graders=[no_agent_error(), expect_program(min_ops=2, no_warnings_matching="travel|rpm|collet"), fixture_used("low-profile"),
                  expect_tools_used("simulate_cam"), simulated_clean()]),
    Case("fixture_screwless_vise_3in", tags=["fixtures"],
         prompt=("Add a fixture for my 3\" screwless toolmaker's vise: hardened body 125 × 80 × 28 mm with a flat bed, jaws 76 mm wide × 14 mm deep × 18 mm "
                 "tall, fixed jaw at the back, the moving jaw pulled by a screw along the body, max opening 70 mm. The stock sits on the body bed between the "
                 "jaws; thin stock should go on parallels automatically like the built-in vises. Start from the built-in 4\" screwless vise script, "
                 "save it as '3\" screwless vise', show it to me, and tell me what you estimated."),
         graders=[no_agent_error(), expect_tools_used("build_fixture", "screenshot"), fixture_script_checks("screwless", min_parts=4, clamp_axis="y")]),
    Case("ext_mass_cost_panel", tags=["ext"],
         prompt="Write an extension with a panel that estimates mass and material cost per body: a material dropdown per body "
                "(aluminium, steel, brass, PLA with sensible densities and $/kg), a table with mass and cost per body, and totals. "
                "Save it and open the panel.",
         initial_code=EXT_PLATE,
         graders=[no_agent_error(), expect_tools_used("extension"), ext_saved({"panels": 1}),
                  ext_panel_renders(need_types=["select", "table"]), ext_panel_event(min_rows=2)]),
    Case("ext_thin_wall_check", tags=["ext"],
         prompt="Add a build check that warns whenever a body is thinner than 1.5 mm in any direction (use each body's bounding box: "
                "the smallest extent). Make it an extension so it runs after every build, then rebuild so I can see it fire on the plate.",
         initial_code=EXT_PLATE,
         graders=[no_agent_error(), expect_tools_used("extension"), ext_saved({"hooks": 1}),
                  ext_hook_warns(EXT_PLATE, "Plate"),
                  ext_hook_quiet('result = {"Block": Box(20, 20, 5)}\n')]),
    Case("ext_show_panel_report", tags=["ext"],
         prompt="Show me a panel in the viewer listing every body with its volume in cm³ and its bounding box size. Don't save anything, "
                "just show it.",
         initial_code=EXT_PLATE,
         graders=[no_agent_error(), expect_tools_used("show_panel"), ext_panel_event(min_rows=2),
                  lambda run: [check("no workspace extension written", not list((run.workspace / "extensions").glob("*.py")) if (run.workspace / "extensions").exists() else True, "a file was saved")]]),
]
