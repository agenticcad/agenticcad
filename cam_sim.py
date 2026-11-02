"""Material-removal simulation for AgenticCAD CAM programs.

Two representations, picked by the setups of the simulated ops:
  zmap   (3-axis, top and flipped setups): per XY cell a column of material [zlo, zhi] in MODEL coordinates. A tool
         coming from above lowers zhi; a tool from below (a flipped setup) raises zlo.
  radial (4th axis, indexed or continuous): per (x, angle) cell the distance r from the rotary axis to the surface,
         angle measured in the part's A = 0 frame (as cam_rotary.radial_map). The tool (vertical, any Y offset) cuts
         a ray where it covers it out to the current surface.
Tools cut with their real profile (tool_shape: flat, ball, bull nose, V, chamfer, drill, tapered ball) up to their flute
length; the parts above it (the body over the flutes, a neck, a wider shank) do not cut: material they meet is a
collision ("deeper than the flutes", "T9 shank"), as is the holder stack above them. On the 4th axis the whole stack is
also checked against the chuck body, the jaw ring and the tailstock (cone, barrel, block) placed from the machine model's
rotary dict and the setup's stock, for every move including rapids.
The result keeps compressed snapshots for the viewer and compares the final stock with the part: gouges (cut into
the part), material left on the part, and rapids that hit material.
"""
from __future__ import annotations

import base64
import math
import zlib
from dataclasses import dataclass, field

import numpy as np

import cad_kernel as ck
import cam_kernel as cam

EMPTY = 65535


def _profile(tool: cam.Tool, d: np.ndarray) -> np.ndarray:
    """Height of the tool's cutting surface above its tip at radial distance d (inf outside the cutting part)."""
    return tool.shape.cut_height(d)


def _samples(moves, step: float, with_a: bool):
    """Points along each move (from the previous one), with the index of the move they belong to and its kind."""
    pts, idx, kinds = [], [], []
    prev = None
    for i, m in enumerate(moves):
        cur = np.array(m[1:4] + ([m[5] if len(m) > 5 else (prev[3] if prev is not None and len(prev) > 3 else 0.0)] if with_a else []),
                       dtype=float)
        if prev is None:
            pts.append(cur); idx.append(i); kinds.append(m[0])
        else:
            d = float(np.linalg.norm(cur[:3] - prev[:3]))
            if with_a:
                r = max(math.hypot(cur[1], cur[2]), math.hypot(prev[1], prev[2]), 1.0)
                d = max(d, math.radians(abs(cur[3] - prev[3])) * r)
            n = max(1, min(4000, int(math.ceil(d / step))))
            t = (np.arange(1, n + 1) / n)[:, None]
            seg = prev[None, :] + (cur - prev)[None, :] * t
            pts.extend(seg); idx.extend([i] * n); kinds.extend([m[0]] * n)
        prev = cur
    if not pts:
        return np.zeros((0, 4 if with_a else 3)), np.zeros(0, int), np.zeros(0, int)
    return np.array(pts), np.array(idx), np.array(kinds)


@dataclass
class SimResult:
    mode: str
    grid: dict
    frames: list = field(default_factory=list)          # [(upto_moves, {name: compressed bytes})]
    stats: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    final: dict = field(default_factory=dict)            # name -> np.ndarray (final state)
    masks: dict = field(default_factory=dict)            # gouge / left masks (bool arrays)
    per_move: dict = field(default_factory=dict)         # op index -> {"mrr": np.ndarray, "ap": np.ndarray} per move (zmap)
    # zmap with a part: material left per body and feature [{body, area_mm2, max_mm, volume_mm3, features: [{kind, z_lo,
    # z_hi, area_mm2, max_mm, at}]}] (kind: flat | sloped | edge, "underside ..." for a flipped setup's side)
    leftover: list = field(default_factory=list)
    # real metal in every tab of every selected tabbed contour, measured on the stock right after the op:
    # [{op, tab, metal_mm (core thickness), width_mm (length at >= 90 % of it), min_mm (thinnest section, ramps
    # included), z_top, at: (x, y), body, ok}]
    tabs: list = field(default_factory=list)
    # how each body is held, at every change: [{body, op, state, prev, history}] (state: held | tab-held | free); the
    # first entry per body is its state before the first op ("op": None)
    release: list = field(default_factory=list)
    # ops that cut a body which was already held only by tabs, or free: [{op, body, state, since, removed_mm3, at}]
    violations: list = field(default_factory=list)
    history: list = field(default_factory=list)          # names of the earlier ops simulated silently first (stock history)
    tab_min: float = 2.0                                 # thinnest acceptable tab (mm of metal)

    def verdict(self, bite_factor: float = 2.0) -> dict:
        """A compact verdict for gating a run: {"ok": no errors, "errors": [...], "warnings": [...]}; each item is
        {"code", "message", ...details}. Errors: gouges into the part (cut by the simulated ops), machine collisions,
        rapids through material, tabs thinner than tab_min, ops that cut a part already held only by tabs (or free),
        a pass buried deeper than bite_factor x the tool's stepdown. Warnings: material left (per body and feature), a
        shallower bite, removal-rate spikes, feed time in air, parts cut free without tabs."""
        s = self.stats
        errors, warnings = [], []
        g = s.get("gouge")
        if g and g.get("cells"):
            new = g.get("new_cells", g["cells"])
            msg = (f"the tool cut up to {g['max_depth']:.2f} mm into the part over {g['area_mm2']:.1f} mm² (worst at {g['worst']})"
                   + (f"; caused by: {', '.join(g['ops'])}" if g.get("ops") else ""))
            if new:
                errors.append({"code": "GOUGE", "message": msg, "ops": list(g.get("ops") or []), "max_mm": g["max_depth"],
                               "area_mm2": g["area_mm2"], "at": g["worst"]})
            else:
                warnings.append({"code": "GOUGE_BEFORE", "message": msg + " (cut by earlier ops, not the simulated ones)"})
        col = s.get("collisions")
        for h in (col or {}).get("hits", [])[:8]:
            errors.append({"code": "COLLISION", "op": h["op"], "message": f"{h['part']} hits {h['what']} in {h['op']} at "
                           f"({h['pos'][0]:.1f}, {h['pos'][1]:.1f}, {h['pos'][2]:.1f}), {h['depth']:.2f} mm", "depth_mm": h["depth"]})
        for h in (s.get("rapid_hits") or [])[:8]:
            errors.append({"code": "RAPID", "op": h["op"], "message": f"{h['op']}: rapid through material at move {h['move']} "
                           f"({h['depth']:.2f} mm deep)", "depth_mm": h["depth"]})
        for t in self.tabs:
            if not t["ok"]:
                errors.append({"code": "THIN_TAB", "op": t["op"], "message": f"{t['op']} tab {t['tab']} ({t['body']}) at "
                               f"({t['at'][0]:.1f}, {t['at'][1]:.1f}): {t['metal_mm']:.2f} mm of metal over {t['width_mm']:.1f} mm, "
                               f"needs {self.tab_min:g}", "metal_mm": t["metal_mm"], "body": t["body"]})
        for v in self.violations:
            errors.append({"code": "RELEASED", "op": v["op"], "body": v["body"], "message": _violation_text(v)})
        for b in s.get("bites") or []:
            sd = b.get("stepdown") or 0
            by_design = b.get("kind") in ("vcarve", "chamfer")
            if sd and not by_design and b["max"] > sd * 1.1 + 0.02:
                item = {"code": "BITE", "op": b["op"], "message": f"{b['op']}: the {b['tool']} is buried {b['max']:.2f} mm deep at "
                        f"move {b['move']} {tuple(b['at']) if b['at'] else ''} - its stepdown is {sd:g} mm", "max_mm": b["max"],
                        "stepdown": sd, "move": b["move"]}
                (errors if b["max"] > bite_factor * sd else warnings).append(item)
            rated, mean = b.get("mrr_rated") or 0, b.get("mrr_mean") or 0
            if b.get("kind") in FINISHING_KINDS and mean > 0 and b.get("mrr_max", 0) > 3 * mean and b["mrr_max"] > 20:
                warnings.append({"code": "MRR", "op": b["op"], "message": f"{b['op']}: a finishing pass removes {b['mrr_max']:.0f} mm³/min "
                                 f"at move {b['mrr_move']}, {b['mrr_max'] / mean:.0f}x its average"})
            elif rated and not by_design and b.get("mrr_max", 0) > rated * 3.0 and b["mrr_max"] > 5:
                warnings.append({"code": "MRR", "op": b["op"], "message": f"{b['op']}: removes up to {b['mrr_max']:.0f} mm³/min at move "
                                 f"{b['mrr_move']}, {b['mrr_max'] / rated:.1f}x the {b['tool']}'s rating"})
            if b["air_s"] >= 20 and b["air_s"] > 0.15 * max(b["cut_s"], 1.0):
                warnings.append({"code": "AIR", "op": b["op"], "message": f"{b['op']}: {b['air_s']:.0f} s of feed moves cut nothing "
                                 f"(cutting {b['cut_s']:.0f} s)", "air_s": b["air_s"]})
        for lb in self.leftover:
            warnings.append({"code": "LEFT", "body": lb["body"], "message": _leftover_text(lb), "area_mm2": lb["area_mm2"],
                             "max_mm": lb["max_mm"], "features": lb["features"]})
        if not self.leftover and (s.get("left") or {}).get("area_mm2"):
            lf = s["left"]
            warnings.append({"code": "LEFT", "message": f"material left on the part: up to {lf['max']:.2f} mm over {lf['area_mm2']:.0f} mm²",
                             "area_mm2": lf["area_mm2"], "max_mm": lf["max"]})
        for r in self.release:
            if r["state"] == "free" and r["prev"] == "held" and not r.get("history"):
                warnings.append({"code": "FREED", "op": r["op"], "body": r["body"], "message": f"{r['op']} cuts {r['body']} free with no "
                                 "tabs: nothing but the workholding (tape/glue) keeps it from moving into the cutter"})
        return {"ok": not errors, "errors": errors, "warnings": warnings}

    def summary(self) -> str:
        s = self.stats
        lines = [f"Simulation ({self.mode}, {self.grid['res']:.2f} mm cells): {s.get('moves', 0)} moves in {s.get('ops', 0)} ops, "
                 f"removed {s.get('removed_cm3', 0):.2f} cm³"]
        g = s.get("gouge")
        if g and g["cells"]:
            lines.append(f"GOUGES: the tool cut up to {g['max_depth']:.2f} mm into the part over {g['area_mm2']:.1f} mm² "
                         f"(worst at {g['worst']})" + (f"; caused by: {', '.join(g['ops'])}" if g['ops'] else ""))
        else:
            lines.append("No gouges: the tool never cut into the part.")
        lf = s.get("left")
        if lf:
            lines.append(f"Material left on the part: up to {lf['max']:.2f} mm, {lf['area_mm2']:.0f} mm² above {lf['tol']} mm "
                         f"({lf['volume_cm3']:.3f} cm³)")
            lines += [f"  {_leftover_text(lb)}" for lb in self.leftover]
        for t in self.tabs:
            lines.append(f"{'TAB' if t['ok'] else 'THIN TAB'}: {t['op']} tab {t['tab']} ({t['body']}, at {t['at'][0]:.1f}, {t['at'][1]:.1f}): "
                         f"{t['metal_mm']:.2f} mm of metal over {t['width_mm']:.1f} mm of its length"
                         + ("" if t["ok"] else f" - under {self.tab_min:g} mm: it can break and let the part move into the cutter"))
        moves = [r for r in self.release if r["op"] is not None and not r.get("history")]
        if moves:
            lines.append("Release: " + "; ".join(f"{r['body']} {r['prev']} -> {r['state']} after {r['op']}" for r in moves))
        lines += [_violation_text(v) for v in self.violations]
        if s.get("outside_cm3"):
            lines.append(f"Stock still standing outside the part (frame, tabs, uncut margins): {s['outside_cm3']:.2f} cm³")
        col = s.get("collisions")
        if col and col["count"]:
            lines.append(f"COLLISIONS: {col['count']} contact(s) between the tool/machine and the stock, table or fixture (worst {col['max_depth']:.2f} mm): "
                         + "; ".join(f"{h['part']} hits {h['what']} in {h['op']} at ({h['pos'][0]:.1f}, {h['pos'][1]:.1f}, {h['pos'][2]:.1f}), {h['depth']:.2f} mm"
                                     for h in col["hits"][:4]) + (" …" if col["count"] > 4 else ""))
        elif "collisions" in s:
            lines.append("No machine collisions: the holder, nose and head clear the stock; nothing cuts into the table.")
        rc = s.get("rapid_hits") or []
        if rc:
            lines.append(f"RAPIDS THROUGH MATERIAL: {len(rc)} — " + "; ".join(f"{h['op']} move {h['move']} ({h['depth']:.2f} mm deep)" for h in rc[:5]))
        for b in s.get("bites") or []:
            sd = b.get("stepdown") or 0
            by_design = b.get("kind") in ("vcarve", "chamfer")                     # those cut to depth by design
            if sd and b["max"] > sd * 1.1 + 0.02 and not by_design:
                lines.append(f"BITE: {b['op']}: the {b['tool']} is buried {b['max']:.2f} mm deep at move {b['move']} "
                             f"{tuple(b['at']) if b['at'] else ''} - its stepdown is {sd:g} mm. Rough or step the stock down first "
                             "so a finishing pass only meets what it was planned for (small ball and V tools break on steps like this)")
            rated = b.get("mrr_rated") or 0
            mean = b.get("mrr_mean") or 0
            where = f"at move {b['mrr_move']} {tuple(b['mrr_at']) if b['mrr_at'] else ''}"
            if b.get("kind") in FINISHING_KINDS and mean > 0 and b.get("mrr_max", 0) > 3 * mean and b["mrr_max"] > 20:
                lines.append(f"MRR: {b['op']}: a finishing pass removes {b['mrr_max']:.0f} mm³/min {where}, {b['mrr_max'] / mean:.0f}x the "
                             f"operation's own average of {mean:.0f} - it has found stock the roughing left (a cap, a step, a collar) "
                             "and is cutting it as a slot. Rough that away first; a slower feed there only hides it (Program.limit_feeds)")
            elif rated and not by_design and b.get("mrr_max", 0) > rated * 3.0 and b["mrr_max"] > 5:   # a full slot is ~2.5x
                lines.append(f"MRR: {b['op']}: removes up to {b['mrr_max']:.0f} mm³/min {where} - {b['mrr_max'] / rated:.1f}x the {b['tool']}'s "
                             f"{rated:.0f} mm³/min at its stepdown x stepover x feed (mean while cutting {mean:.0f}). The tool cannot sustain "
                             "that: change the path there (rough first, approach from above the stock) or limit the feed (Program.limit_feeds)")
            if b["air_s"] >= 20 and b["air_s"] > 0.15 * max(b["cut_s"], 1.0):
                lines.append(f"{b['op']}: {b['air_s']:.0f} s of feed moves cut nothing ({b['air_moves']} moves; cutting {b['cut_s']:.0f} s): "
                             "links, approaches or returns that could be rapids or shorter")
        lines += [f"note: {n}" for n in self.notes]
        if self.history:
            lines.append(f"note: started from the stock as {len(self.history)} earlier op(s) leave it ({_short_list(self.history)}), "
                         "simulated first and not reported")
        return "\n".join(lines)

    def frame_payload(self, k: int) -> dict:
        k = max(0, min(k, len(self.frames) - 1))
        upto, data = self.frames[k]
        out = {"index": k, "upto": upto, "mode": self.mode, "grid": self.grid,
               "data": {n: base64.b64encode(v).decode() for n, v in data.items()}}
        if k == len(self.frames) - 1:
            for n, m in self.masks.items():
                out["data"][n] = base64.b64encode(zlib.compress(np.packbits(m.astype(np.uint8)).tobytes(), 6)).decode()
        return out

    def to_payload(self) -> dict:
        return {"mode": self.mode, "grid": self.grid, "frames": [f[0] for f in self.frames], "stats": self.stats,
                "notes": self.notes, "summary": self.summary(), "leftover": self.leftover, "tabs": self.tabs,
                "release": self.release, "violations": self.violations, "history": self.history, "verdict": self.verdict()}


def _short_list(names, n: int = 3) -> str:
    return ", ".join(names[:n]) + (f" … {names[-1]}" if len(names) > n else "")


def _violation_text(v) -> str:
    if v.get("freed"):
        return (f"RELEASED: {v['op']} cuts through the tabs that held {v['body']} (held only by tabs since {v['since'] or 'an earlier op'}): "
                "it comes loose under the cutter")
    how = "held only by tabs" if v["state"] == "tab-held" else "free (nothing holds it)"
    return (f"RELEASED: {v['op']} cuts {v['body']} ({v['removed_mm3']:.0f} mm³ near {v['at'][0]:.1f}, {v['at'][1]:.1f}) after "
            f"{v['since'] or 'an earlier op'} left it {how}: machine it before the release, or hold it another way")


def _feature_text(f) -> str:
    z = f"z {f['z_lo']:.2f}" if f["z_hi"] - f["z_lo"] < 0.1 else f"z {f['z_lo']:.1f}-{f['z_hi']:.1f}"
    under = f["kind"].startswith("underside")
    k = f["kind"].replace("underside ", "")
    pre = "underside " if under else ""
    if k == "flat":
        return f"{f['area_mm2']:.0f} mm² {pre}flat at {z}, +{f['max_mm']:.2f} mm (a level face left above its height)"
    if k == "sloped":
        return f"{f['area_mm2']:.0f} mm² {pre}sloped faces {z}, up to {f['max_mm']:.2f} mm (chamfer/bevel/countersink not machined)"
    return f"{f['area_mm2']:.0f} mm² {pre}along an edge/wall at {z}, up to {f['max_mm']:.2f} mm"


def _leftover_text(lb) -> str:
    big = [f for f in lb["features"] if f["area_mm2"] >= 1.0]
    rest = lb["area_mm2"] - sum(f["area_mm2"] for f in big)
    return (f"{lb['body']}: {lb['area_mm2']:.0f} mm² left, up to {lb['max_mm']:.2f} mm - " + "; ".join(_feature_text(f) for f in big)
            + (f"{'; ' if big else ''}{rest:.0f} mm² in small spots" if rest >= 0.5 else ""))


def _part_mesh(model_or_shape):
    if isinstance(model_or_shape, ck.Model):
        return model_or_shape.mesh
    return ck.build_model(ck.coerce_bodies(model_or_shape), "", "fine").mesh


TAB_MIN_MM = {"aluminium": 2.0, "aluminum": 2.0}      # thinnest acceptable tab by stock material (default TAB_MIN_DEFAULT)
TAB_MIN_DEFAULT = 2.0


def simulate(program: cam.Program, ops: list[int] | None = None, part=None, resolution: float | None = None,
             frames: int = 48, tol: float = 0.05, machine_model=None, placement=None, res_a: float = 1.0,
             history: bool = True, tab_min: float | None = None, material: str | None = None,
             attach_min: float = 0.1) -> SimResult:
    """Simulate the given ops (indices into program.ops, default all) in order. `part` (model or shape) enables the
    gouge / left-material comparison (per body and feature: result.leftover). `resolution` = cell size along X/Y (mm;
    default from the stock size, 0.2 mm on the 4th axis), `res_a` = the angular cell on the 4th axis (degrees, default 1°).
    `placement` (4th axis: chuck_gap, tail_gap in mm) overrides the machine model's mounting numbers.
    history (default True): when the selection starts later in the program (a later setup) or skips ops, every earlier
    op of the program is simulated first, silently, so the selected ops meet the real in-process stock (register holes
    cut on side A, the back already thinned); history=False starts from fresh stock.
    Tabs: every selected tabbed contour's tabs are measured on the stock (result.tabs); a tab with less than `tab_min`
    mm of metal is an error (default by `material` / the program's stock material: TAB_MIN_MM, else 2 mm). Release
    tracking (result.release, result.violations): whether each body is still held by the surrounding stock, only by
    tabs, or free (material columns overlapping by more than `attach_min` mm connect). Returns a SimResult
    (summary(), verdict(), frame_payload(k))."""
    sel = list(range(len(program.ops))) if ops is None else [i for i in ops if 0 <= i < len(program.ops)]
    if not sel:
        raise cam.CamError("simulate: no operations selected")
    setups = [program.setup_of(program.ops[i]) for i in sel]
    rot = [s.rotary for s in setups]
    if any(rot) and not all(rot):
        raise cam.CamError("simulate 4th-axis and flat setups separately (they use different stock models)")
    if machine_model is None:
        try:
            import machine_models as _mm
            machine_model = _mm.model_for(program.setup.machine)
        except Exception:  # noqa: BLE001 - the machine model never stops a simulation
            machine_model = None
    # the run order: (op index, silent). Silent ops are the earlier ops the selection skips (stock history only)
    chosen = set(sel)
    if history and sel == sorted(chosen):
        order = [(i, i not in chosen) for i in range(sel[-1] + 1)]
    elif history:
        order = [(i, True) for i in range(min(sel)) if i not in chosen] + [(i, False) for i in sel]
    else:
        order = [(i, False) for i in sel]
    if tab_min is None:
        mat = material or getattr(program, "material", None) or getattr(program.setup.stock, "material", None)
        tab_min = TAB_MIN_MM.get(str(mat).lower().strip(), TAB_MIN_DEFAULT) if mat else TAB_MIN_DEFAULT
    if all(rot):
        st0 = setups[0]
        same = [(i, s) for i, s in order if not s or (program.setup_of(program.ops[i]).rotary
                                                       and cam.Program.same_mounting(st0, program.setup_of(program.ops[i])))]
        out = _sim_radial(program, sel, part, resolution, frames, tol, machine_model, placement or {}, res_a,
                          pre=[i for i, s in same if s and i < min(sel)])
        return out
    out = _sim_zmap(program, order, part, resolution, frames, tol, machine_model, placement or {}, float(tab_min), attach_min)
    return out


FINISHING_KINDS = ("parallel3d", "contour", "rotary_finish", "rotary_profile")   # ops planned as skims: a buried pass is a surprise


def _bite_stats(op, mv, pts, idx, kinds, eng, rapid_rate) -> tuple[dict, dict]:
    """Per operation: the deepest axial engagement of one pass (and where) against the tool's stepdown, the highest
    material removal rate (mm3/min: volume a sample removes / its step x the move's feed) against the tool's rated
    MRR (stepdown x stepover width x feed), how the time splits into rapids, cutting feeds and feed moves that removed
    nothing - and per move the max ap and MRR (for Program.limit_feeds)."""
    n = len(mv)
    ap_move = np.full(n, -np.inf); mrr_move = np.zeros(n); vol_move = np.zeros(n)
    if len(idx):
        np.maximum.at(ap_move, idx, eng[:, 0])
        feeding = kinds != cam.RAPID
        vol = np.where(feeding, eng[:, 1], 0.0)
        np.add.at(vol_move, idx, vol)
        # removal rate over a window of one tool radius (at least 0.5 mm) of feed travel: a single sample's volume
        # comes in cell-sized jumps and a 0.02 mm move would read ten times its real rate
        step = np.zeros(len(pts))
        if len(pts) > 1:
            step[1:] = np.linalg.norm(pts[1:, :3] - pts[:-1, :3], axis=1)
        feed = np.array([mv[i][4] or 0.0 for i in idx], dtype=float)
        minutes = np.where(feeding & (feed > 0), step / np.maximum(feed, 1e-9), 0.0)
        dist = np.where(feeding, step, 0.0)
        cv, ct, cd = np.concatenate([[0.0], np.cumsum(vol)]), np.concatenate([[0.0], np.cumsum(minutes)]), np.concatenate([[0.0], np.cumsum(dist)])
        W = max(op.tool.diameter / 2.0, 0.5)
        j = np.searchsorted(cd, cd[1:] - W, side="left")       # window start for each sample (index into the cumulative arrays)
        for a, b in _runs(kinds):                                # a window never reaches back across a rapid
            j[a:b] = np.maximum(j[a:b], a)
        dv = cv[1:] - cv[j]; dt = ct[1:] - ct[j]
        rate = np.where((dt > 1e-9) & feeding, dv / np.maximum(dt, 1e-9), 0.0)
        np.maximum.at(mrr_move, idx, rate)
    rapid_s = cut_s = air_s = 0.0
    air_moves = []
    for k in range(1, n):
        a, b = mv[k - 1], mv[k]
        d = math.dist(a[1:4], b[1:4])
        if b[0] == cam.RAPID:
            rapid_s += d / rapid_rate * 60.0
            continue
        t = d / (b[4] or 1.0) * 60.0
        if vol_move[k] > 1e-4 or ap_move[k] > 0.005:
            cut_s += t
        else:
            air_s += t
            if d > 0.05:
                air_moves.append(k)
    worst = int(np.argmax(ap_move)) if n else 0
    mx = float(ap_move[worst]) if n and np.isfinite(ap_move[worst]) else 0.0
    wm = int(np.argmax(mrr_move)) if n else 0
    tool = op.tool
    rated = tool.rated_mrr()
    b = {"op": op.name, "kind": op.kind, "tool": tool.label(), "max": round(max(mx, 0.0), 3), "move": worst + 1,
         "at": [round(float(v), 2) for v in mv[worst][1:4]] if n else None,
         "stepdown": float((op.params or {}).get("ap") or tool.stepdown),   # a planned full-depth finish pass is buried by design
         "mrr_max": round(float(mrr_move[wm]), 1), "mrr_move": wm + 1, "mrr_at": [round(float(v), 2) for v in mv[wm][1:4]] if n else None,
         "mrr_rated": round(rated, 1), "mrr_mean": round(float(vol_move.sum()) / (cut_s / 60.0), 1) if cut_s > 1e-6 else 0.0,
         "removed_mm3": round(float(vol_move.sum()), 2),
         "rapid_s": round(rapid_s, 1), "cut_s": round(cut_s, 1), "air_s": round(air_s, 1), "air_moves": len(air_moves)}
    return b, {"ap": np.where(np.isfinite(ap_move), ap_move, 0.0), "mrr": mrr_move}


def _frame_marks(total: int, frames: int) -> list[int]:
    if total <= 0:
        return [0]
    n = max(1, min(frames, total))
    return sorted({int(round(total * (i + 1) / n)) for i in range(n)})


# ----------------------------------------------------------------------------- zmap
def _sim_zmap(program, order, part, resolution, frames, tol, machine_model=None, placement=None, tab_min=TAB_MIN_DEFAULT,
              attach_min=0.1) -> SimResult:
    """`order`: [(op index, silent)] in run order; silent ops (the earlier ops a selection skips) only cut the stock."""
    placement = placement or {}
    sel = [i for i, silent in order if not silent]
    setups = [program.setup_of(program.ops[i]) for i in sel]
    all_setups = [program.setup_of(program.ops[i]) for i, _ in order]
    keep = [not s_.rotary for s_ in all_setups]                       # a 4th-axis op can't be part of a flat stock's history
    skipped_rot = [program.ops[i].name for (i, silent), k in zip(order, keep) if not k and silent]
    order = [o for o, k in zip(order, keep) if k]
    all_setups = [s_ for s_, k in zip(all_setups, keep) if k]
    st_boxes = [s.model_stock for s in setups]
    all_boxes = [s.model_stock for s in all_setups]
    x0 = min(b.xmin for b in all_boxes); x1 = max(b.xmax for b in all_boxes)
    y0 = min(b.ymin for b in all_boxes); y1 = max(b.ymax for b in all_boxes)
    zmin = min(b.zmin for b in all_boxes); zmax = max(b.zmax for b in all_boxes)
    area = (x1 - x0) * (y1 - y0)
    res = resolution or max(0.15, math.sqrt(area / 160_000.0))
    # the fixtures (vises / clamps / plates) of the top setup, in model coordinates: uncuttable. They get their own height
    # map (fix_grid), so a big vise or a second fixture across the table never coarsens the stock's grid.
    fix_shapes = _fixture_shapes(setups)
    nx = int(math.ceil((x1 - x0) / res)) + 1
    ny = int(math.ceil((y1 - y0) / res)) + 1
    zhi = np.full((ny, nx), -np.inf); zlo = np.full((ny, nx), np.inf)
    st0 = all_boxes[0]
    ix0 = int(round((st0.xmin - x0) / res)); ix1 = int(round((st0.xmax - x0) / res))
    iy0 = int(round((st0.ymin - y0) / res)); iy1 = int(round((st0.ymax - y0) / res))
    zhi[iy0:iy1 + 1, ix0:ix1 + 1] = st0.zmax
    zlo[iy0:iy1 + 1, ix0:ix1 + 1] = st0.zmin
    grid = {"res": res, "x0": x0, "y0": y0, "nx": nx, "ny": ny, "zmin": zmin, "zmax": zmax}
    out = SimResult("zmap", grid)
    out.tab_min = tab_min
    if skipped_rot:
        out.notes.append(f"the stock history leaves out {len(skipped_rot)} 4th-axis op(s) ({_short_list(skipped_rot)}): a different stock model")
    has_bottom = False
    plans = []                                            # (op_index, op, side, model moves, src, silent, setup-frame z)
    for i, silent in order:
        op = program.ops[i]; st = program.setup_of(op)
        ax = st.tool_axis_model
        if ax[2] > 0.999:
            side = 1
        elif ax[2] < -0.999:
            side = -1; has_bottom = True
        else:
            if not silent:
                out.notes.append(f"{op.name}: side setup ({st.orient}) is not simulated in the height-map model")
            plans.append((i, op, 0, None, [], silent, None)); continue
        if op.kind == "probe":                            # probe ops run once; the touches stopped short of their faces
            tm, src = op.sim_moves or op.moves, list(range(len(op.moves)))
        else:
            tm, src = cam.tile_moves(op, program.op_tiles(op), st.clearance_z)  # every tile, as the post runs them
        P = st.to_model(np.array([m[1:4] for m in tm], dtype=float)) if tm else np.zeros((0, 3))
        mv = [[m[0], *p, m[4]] for m, p in zip(tm, P)]
        plans.append((i, op, side, mv, src, silent, np.array([m[3] for m in tm], dtype=float)))
    total = sum((len(mv) if mv is not None else len(op.moves)) for _, op, _, mv, _, silent, _ in plans if not silent)
    marks = _frame_marks(total, frames)
    done = 0
    mark_i = 0
    rapid_hits = []
    collisions: list[dict] = []

    def snap(upto):
        q = {"zhi": _quant(zhi, zlo, zmin, zmax)}
        if has_bottom:
            q["zlo"] = _quant(zlo, zhi, zmin, zmax, low=True)
        out.frames.append((upto, q))

    bodies = _body_maps_z(part, res, x0, y0, nx, ny) if part is not None else None
    pmaps = (bodies["top"], bodies["bot"]) if bodies is not None else None
    fix = _fixture_map(fix_shapes, res) if fix_shapes else None
    fix_hi = fix["top"] if fix else None
    if fix_shapes:
        placement = {**placement, "fixture": placement.get("fixture", setups[0].fixture_height()),
                     "replaces_spoilboard": placement.get("replaces_spoilboard", setups[0].replaces_spoilboard())}
    gouged = np.zeros((ny, nx), dtype=bool)
    gouge_ops: list[str] = []
    bites: list[dict] = []                                # per op: the deepest single-pass cut, and feed time spent in air
    rapid_rate = float(getattr(program.setup.machine, "rapid", 2000.0) or 2000.0)
    rel = _Release(bodies, res, attach_min, x0, y0) if bodies is not None and bodies["names"] else None
    if rel is not None:
        rel.start(zhi, zlo, out.release)
    started = False
    initial = 0.0
    silent_removed = 0.0                                  # removed by silent ops after the first selected one (not counted)

    def vol():
        return float(np.where(zhi > zlo, zhi - zlo, 0.0).sum()) * res * res

    for i, op, side, mv, src, silent, zs in plans:
        if silent:
            if side == 0 or not mv or op.kind == "probe":
                continue
            v0 = vol() if started else 0.0
            pts, idx, kinds = _samples([m[:4] for m in mv], res / 2, False)
            _stamp_z(zhi, zlo, pts, kinds, idx, op.tool, side, res, x0, y0, [], op.name)
            out.history.append(op.name)
            if rel is not None:
                if op.kind == "contour" and (op.params or {}).get("tabs"):
                    _find_tabs(op, mv, zs, side, pts, idx, kinds, zhi, zlo, res, x0, y0, bodies, rel, attach_min, tab_min)
                rel.after(op.name, zhi, zlo, out.release, history=True)
            if started:
                silent_removed += v0 - vol()
                if pmaps is not None:
                    g = _gouge_z(pmaps, zhi, zlo, tol, has_bottom)[0]
                    gouged_start |= g & ~gouged
                    gouged |= g
            continue
        if not started:                                   # the stock as the history left it: where the reported run starts
            started = True
            initial = vol()
            snap(0)
            if pmaps is not None and out.history:
                gouged = _gouge_z(pmaps, zhi, zlo, tol, has_bottom)[0]
            gouged_start = gouged.copy()
        if side == 0 or not mv:
            done += len(mv) if mv else len(op.moves)
            while mark_i < len(marks) and marks[mark_i] <= done:
                snap(marks[mark_i]); mark_i += 1
            continue
        tool = op.tool
        pts, idx, kinds = _samples([m[:4] for m in mv], res / 2, False)
        if op.kind == "probe":
            _probe_air(collisions, zhi, zlo, pts, kinds, idx, tool, side, res, x0, y0, op.name, mv)
            done += len(mv)
            while mark_i < len(marks) and marks[mark_i] <= done:
                snap(marks[mark_i]); mark_i += 1
            continue
        before = (zhi.copy(), zlo.copy()) if rel is not None else None
        eng = np.full((len(pts), 3), -np.inf); eng[:, 1] = 0.0
        # process in pieces that end at frame marks so snapshots are exact
        start = 0
        while start < len(pts):
            next_mark = marks[mark_i] if mark_i < len(marks) else None
            if next_mark is not None and next_mark - done <= len(mv):
                end_move = next_mark - done - 1            # last move index (in this op) of this piece
                end = int(np.searchsorted(idx, end_move, side="right"))
            else:
                end = len(pts)
            end = max(end, start + 1) if end <= start else end
            P = pts[start:end]; K = kinds[start:end]; I = idx[start:end]
            if side == 1:
                _buried_z(collisions, zhi, P, K, I, tool, res, x0, y0, op.name)
            _stamp_z(zhi, zlo, P, K, I, tool, side, res, x0, y0, rapid_hits, op.name, eng[start:end])
            if machine_model is not None and side == 1:
                _collide_z(collisions, zhi, P, I, done, tool, machine_model, res, x0, y0, op.name, st_boxes[0], placement)
            if fix is not None and side == 1:
                _collide_fixture(collisions, fix, P, K, I, tool, machine_model, res, op.name)
            start = end
            if next_mark is not None and end_move_done(idx, end, done, next_mark):
                snap(next_mark); mark_i += 1
        done += len(mv)
        b, per_move = _bite_stats(op, mv, pts, idx, kinds, eng, rapid_rate)
        if len(mv) != len(op.moves):                       # tiles: each op move gets its worst over the tiles
            sa = np.asarray(src); keep_ = sa >= 0
            fold = {}
            for k, arr in per_move.items():
                f = np.zeros(len(op.moves)); np.maximum.at(f, sa[keep_], np.asarray(arr)[keep_]); fold[k] = f
            per_move = fold
            b["tiles"] = int(len(program.op_tiles(op)))
            for key in ("move", "mrr_move"):
                j = b[key] - 1
                b[key] = int(src[j]) + 1 if 0 <= j < len(src) and src[j] >= 0 else b[key]
        bites.append(b); out.per_move[i] = per_move
        if rel is not None:
            rel.check_cut(op.name, before, zhi, zlo, out.violations)
            if op.kind == "contour" and (op.params or {}).get("tabs"):
                out.tabs += _find_tabs(op, mv, zs, side, pts, idx, kinds, zhi, zlo, res, x0, y0, bodies, rel, attach_min, tab_min)
            rel.after(op.name, zhi, zlo, out.release, violations=out.violations)
        if pmaps is not None:
            g = _gouge_z(pmaps, zhi, zlo, tol, has_bottom)[0]
            if (g & ~gouged).sum() * res * res > 0.5:
                gouge_ops.append(op.name)
            gouged |= g
        while mark_i < len(marks) and marks[mark_i] <= done:
            snap(marks[mark_i]); mark_i += 1
    if not out.frames or out.frames[-1][0] != total:
        snap(total)
    if pmaps is not None and sel and max(sel) < len(program.ops) - 1:
        out.notes.append(f"the program goes on after {program.ops[max(sel)].name}: material left on the part includes what "
                         "its later ops are meant to cut")
    remaining = vol()
    out.stats = {"moves": total, "ops": len(sel), "removed_cm3": round((initial - remaining - silent_removed) / 1000.0, 3),
                 "rapid_hits": rapid_hits[:50], "bites": bites}
    if machine_model is not None or fix_hi is not None or collisions:
        out.stats["collisions"] = _collision_stats(collisions)
        if machine_model is not None:
            out.stats["machine"] = {"key": machine_model.key, "name": machine_model.name, "placement": placement}
        if fix_shapes:
            out.stats["fixture"] = setups[0].fixture_payload()
        if has_bottom:
            out.notes.append("machine collisions are checked on top setups only; flipped (bottom) setups are not checked against the holder")
    if pmaps is not None:
        _compare_zmap(out, pmaps, zhi, zlo, res, x0, y0, nx, tol, has_bottom, bodies)
        out.stats["gouge"]["ops"] = gouge_ops
        out.stats["gouge"]["new_cells"] = int((out.masks["gouge"] & ~gouged_start).sum())
        if rel is not None:
            out.stats["held"] = {n: s_ for n, (s_, _) in rel.state.items()}
    out.final = {"zhi": zhi, "zlo": zlo}
    return out


def end_move_done(idx, end, done, next_mark) -> bool:
    return end > 0 and idx[end - 1] + done + 1 >= next_mark


def _quant(a, other, lo, hi, low=False) -> bytes:
    span = max(hi - lo, 1e-9)
    empty = ~(a > other) if not low else ~(other > a)
    q = np.clip(np.round((np.nan_to_num(a, posinf=hi, neginf=lo) - lo) / span * 65534), 0, 65534).astype(np.uint16)
    q[empty] = EMPTY
    return zlib.compress(q.tobytes(), 6)


def _runs(K):
    """(start, end) of consecutive samples that are all rapids or all feeds, in order."""
    if len(K) == 0:
        return []
    r = (K == cam.RAPID).astype(np.int8)
    cuts = np.flatnonzero(np.diff(r)) + 1
    edges = np.concatenate([[0], cuts, [len(K)]])
    return list(zip(edges[:-1], edges[1:]))


def _stamp_z(zhi, zlo, P, K, I, tool, side, res, x0, y0, rapid_hits, op_name, eng=None):
    for a, b in _runs(K):                              # in order: a rapid sees everything cut before it
        _stamp_z_run(zhi, zlo, P[a:b], K[a:b], I[a:b], tool, side, res, x0, y0, rapid_hits, op_name,
                     None if eng is None else eng[a:b])


def _stamp_z_run(zhi, zlo, P, K, I, tool, side, res, x0, y0, rapid_hits, op_name, eng=None):
    """Stamp the tool along the samples. `eng` (len(P) x 3, in/out) measures every feed sample's cut against the
    stock as the previous sample left it: [0] ap - the axial depth of the material being cut (stock height above
    the tool tip over the cells this sample removes something from: a buried ball in a slot reads the slot depth,
    not the sample's increment), [1] the volume it removes (mm3), [2] the deepest the stock stands above the
    tool's cutting surface (the old 'bite'). Material removal rate follows from [1] / step x feed."""
    ny, nx = zhi.shape
    if len(P) == 0:
        return
    R = tool.shape.cut_radius
    edge = max(0.01, (res / 4) ** 2 / max(R, 1e-3))     # how far a sample's distance to a cell can be off (step / 2 along the path)
    k = int(math.ceil(R / res)) + 1
    oy, ox = np.mgrid[-k:k + 1, -k:k + 1]
    oy = oy.ravel(); ox = ox.ravel()
    bx = np.floor((P[:, 0] - x0) / res).astype(np.int64)
    by = np.floor((P[:, 1] - y0) / res).astype(np.int64)
    chunk = max(1, int(2_000_000 // len(ox)))
    for s in range(0, len(P), chunk):
        e = s + chunk
        CX = bx[s:e, None] + ox[None, :]; CY = by[s:e, None] + oy[None, :]
        # exact distance from the real tool position to each cell centre (no snapping)
        d = np.hypot(x0 + CX * res - P[s:e, 0:1], y0 + CY * res - P[s:e, 1:2])
        ok = (CX >= 0) & (CX < nx) & (CY >= 0) & (CY < ny) & (d < R - 1e-4)   # touching a cell centre is not cutting it
        prof_all = _profile(tool, np.where(ok, d, 0.0))
        ok &= np.isfinite(prof_all)
        flat = (CY * nx + CX)[ok]
        # cells grazed by the cutter's rim: whether a pass takes them depends on where its samples fall along the path
        # (a cell centre on the wall line, right at R, survives one lap and goes on the next, with the full height of the
        # wall above it). They count as removed material but not as axial engagement (the BITE depth).
        inner = (d < R - edge)[ok]
        prof = prof_all[ok]
        zt = np.broadcast_to(P[s:e, 2:3], d.shape)[ok]
        vals = zt + prof if side > 0 else zt - prof
        rap = (K[s:e] == cam.RAPID)
        if rap.any():                                  # rapids that would cut material
            rows = np.broadcast_to(rap[:, None], d.shape)[ok]
            if rows.any():
                f = flat[rows]; v = vals[rows]
                hi = zhi.ravel()[f]; lo = zlo.ravel()[f]
                mat = hi > lo
                depth = (hi - v) if side > 0 else (v - lo)
                hit = mat & (depth > 0.05)
                if hit.any() and len(rapid_hits) < 50:
                    owner = np.broadcast_to(I[s:e][:, None], d.shape)[ok][rows]
                    j = int(np.argmax(np.where(hit, depth, -1)))
                    rapid_hits.append({"op": op_name, "move": int(owner[j]) + 1, "depth": round(float(depth[j]), 3)})
        if eng is not None and not rap.all():
            # exact per-sample engagement: for every cell, the stock as ALL earlier samples of this chunk left it (a
            # grouped running min/max over the samples touching the cell, in sample order), not only the one before
            m_ = d.shape[0]
            hi = zhi.ravel()[flat]; lo = zlo.ravel()[flat]
            mat = hi > lo
            rows = np.broadcast_to(np.arange(m_)[:, None], d.shape)[ok]
            tipz = np.broadcast_to(P[s:s + m_, 2:3], d.shape)[ok]
            order = np.lexsort((rows, flat))
            f_s, v_s = flat[order], vals[order]
            gstart = np.r_[True, f_s[1:] != f_s[:-1]] if len(f_s) else np.zeros(0, bool)
            gid = (np.cumsum(gstart) - 1).astype(float)
            BIG = 4096.0                                   # > any z range here: keeps each cell's run separate
            if side > 0:
                cm = np.minimum.accumulate(v_s - gid * BIG) + gid * BIG if len(v_s) else v_s
                prev = np.empty_like(cm); prev[1:] = cm[:-1]
                if len(prev): prev[gstart] = np.inf
                ceil_s = np.minimum(hi[order], prev)
                inc_s = ceil_s - v_s
                ax_s = ceil_s - tipz[order]
            else:
                cm = np.maximum.accumulate(v_s + gid * BIG) - gid * BIG if len(v_s) else v_s
                prev = np.empty_like(cm); prev[1:] = cm[:-1]
                if len(prev): prev[gstart] = -np.inf
                ceil_s = np.maximum(lo[order], prev)
                inc_s = v_s - ceil_s
                ax_s = tipz[order] - ceil_s
            ms = mat[order]
            inc = np.empty_like(inc_s); inc[order] = np.where(ms, inc_s, -np.inf)
            axial = np.empty_like(ax_s); axial[order] = ax_s
            cut = inc > 1e-6
            per = np.full(m_, -np.inf); np.maximum.at(per, rows, inc); per[rap] = -np.inf
            ap = np.full(m_, -np.inf); np.maximum.at(ap, rows, np.where(cut & inner, axial, -np.inf)); ap[rap] = -np.inf
            vol = np.zeros(m_); np.add.at(vol, rows[cut], inc[cut] * res * res); vol[rap] = 0.0
            eng[s:s + m_, 0] = np.maximum(eng[s:s + m_, 0], ap)
            eng[s:s + m_, 1] += vol
            eng[s:s + m_, 2] = np.maximum(eng[s:s + m_, 2], per)
        if side > 0:
            np.minimum.at(zhi.ravel(), flat, vals)
        else:
            np.maximum.at(zlo.ravel(), flat, vals)


def _buried_z(hits, zhi, P, K, I, tool, res, x0, y0, op_name):
    """Before a piece is cut: is there material in the tool's footprint higher than its flutes reach (tip + flute
    length)? Then the body above the flutes is in the material — a cut deeper than the tool can make. Feed moves only
    (rapids through material are reported on their own)."""
    if len(P) == 0:
        return
    sh = tool.shape
    r, fl = sh.cut_radius, sh.flute_length
    ny, nx = zhi.shape
    rc = int(math.ceil(r / res))
    step = max(1, int((r / 2) / (res / 2)))
    worst = None
    for k in range(0, len(P), step):
        if K[k] == cam.RAPID:
            continue
        x, y, z = P[k]
        ix, iy = int(round((x - x0) / res)), int(round((y - y0) / res))
        xa, xb = max(0, ix - rc), min(nx - 1, ix + rc); ya, yb = max(0, iy - rc), min(ny - 1, iy + rc)
        if xa > xb or ya > yb:
            continue
        sub = zhi[ya:yb + 1, xa:xb + 1]
        top = float(np.max(sub)) if sub.size else -np.inf
        if not np.isfinite(top) or top <= z + fl + 0.05:
            continue
        yy, xx = np.mgrid[ya:yb + 1, xa:xb + 1]
        dd = np.hypot((xx - (x - x0) / res) * res, (yy - (y - y0) / res) * res)
        m = (dd < r) & np.isfinite(sub) & (sub > z + fl + 0.05)
        if m.any():
            depth = float((sub[m] - (z + fl)).max())
            if worst is None or depth > worst["depth"]:
                worst = {"op": op_name, "move": int(I[k]), "pos": [round(float(x), 2), round(float(y), 2), round(float(z), 2)],
                         "part": f"tool above its {fl:g} mm flutes", "what": "stock", "depth": round(depth, 2)}
    if worst is not None:
        hits.append(worst)


def _probe_air(hits, zhi, zlo, P, K, I, tool, side, res, x0, y0, op_name, mv):
    """A probe op's moves, its touches stopped short of the faces they should find, must pass through air: material
    anywhere on them is the probe meeting the part where the program expects none (the firmware halts the job on a
    probe triggered by a move, or the touch finds the wrong face). Nothing is removed."""
    if len(P) == 0:
        return
    zc, lc = zhi.copy(), zlo.copy()
    _stamp_z(zc, lc, P, K, I, tool, side, res, x0, y0, [], op_name)
    with np.errstate(invalid="ignore"):                  # material is zlo..zhi where zhi > zlo: what the probe took
        before = np.where(np.isfinite(zhi) & np.isfinite(zlo), np.maximum(zhi - zlo, 0.0), 0.0)
        after = np.where(np.isfinite(zc) & np.isfinite(lc), np.maximum(zc - lc, 0.0), 0.0)
        d = before - after
    if d.max() <= 0.05:
        return
    iy, ix = np.unravel_index(int(np.argmax(d)), d.shape)
    cx, cy = x0 + ix * res, y0 + iy * res
    k = int(np.argmin(np.hypot(P[:, 0] - cx, P[:, 1] - cy)))
    hits.append({"op": op_name, "move": int(I[k]) + 1, "pos": [round(float(v), 2) for v in P[k]],
                 "part": "3D probe (the job halts: probe triggered)", "what": "stock", "depth": round(float(d.max()), 2)})


def _part_maps_z(part, res, x0, y0, nx, ny):
    """The part's top and bottom surface heights per cell (-inf / +inf where there is no part)."""
    mesh = _part_mesh(part)
    top = np.full((ny, nx), -np.inf); nbot = np.full((ny, nx), -np.inf)
    for bd in mesh["bodies"]:
        P = np.array(bd["positions"], dtype=float).reshape(-1, 3)
        I = np.array(bd["indices"], dtype=np.int64).reshape(-1, 3)
        cam._raster_triangles(P, I, top, x0, y0, res)
        Q = P.copy(); Q[:, 2] = -Q[:, 2]
        cam._raster_triangles(Q, I, nbot, x0, y0, res)
    return top, -nbot


def _body_maps_z(part, res, x0, y0, nx, ny) -> dict:
    """Per body of the part: its top / bottom surface heights per cell (-inf / +inf where it is absent), names, and the
    union: "top" (max), "bot" (min), "owner" (index of the body whose top is highest in each cell, -1 = no part)."""
    model = part if isinstance(part, ck.Model) else ck.build_model(ck.coerce_bodies(part), "", "fine")
    mbodies = model.mesh["bodies"]
    names = [b.name for b in model.bodies] if len(model.bodies) == len(mbodies) else [f"body {k + 1}" for k in range(len(mbodies))]
    seen: dict[str, int] = {}
    for k, n in enumerate(names):                          # unique names (two bodies called "Body")
        if n in seen:
            names[k] = f"{n} #{seen[n] + 1}"
        seen[n] = seen.get(n, 0) + 1
    tops, bots = [], []
    for bd in mbodies:
        P = np.array(bd["positions"], dtype=float).reshape(-1, 3)
        I = np.array(bd["indices"], dtype=np.int64).reshape(-1, 3)
        top = np.full((ny, nx), -np.inf); nbot = np.full((ny, nx), -np.inf)
        cam._raster_triangles(P, I, top, x0, y0, res)
        Q = P.copy(); Q[:, 2] = -Q[:, 2]
        cam._raster_triangles(Q, I, nbot, x0, y0, res)
        tops.append(top); bots.append(-nbot)
    if tops:
        T = np.stack(tops)
        top_u = T.max(axis=0); bot_u = np.stack(bots).min(axis=0)
        owner = np.where(np.isfinite(top_u), np.argmax(T, axis=0), -1)
    else:
        top_u = np.full((ny, nx), -np.inf); bot_u = np.full((ny, nx), np.inf); owner = np.full((ny, nx), -1)
    return {"names": names, "tops": tops, "bots": bots, "has": [np.isfinite(t) for t in tops], "top": top_u, "bot": bot_u,
            "owner": owner}


def _slope(H, res):
    """Surface slope (rise / run) per cell. Along each axis the gentler of the two one-sided differences (only one where
    the other neighbour is off the surface): a face that really slopes rises on both sides, a cell beside a step or on
    the flat next to a chamfer has one level side and reads flat."""
    Hn = np.where(np.isfinite(H), H, np.nan)
    P = np.pad(Hn, 1, constant_values=np.nan)
    g2 = np.zeros(H.shape)
    for fwd, bwd in ((P[2:, 1:-1], P[:-2, 1:-1]), (P[1:-1, 2:], P[1:-1, :-2])):
        with np.errstate(invalid="ignore"):
            f = np.abs(fwd - Hn) / res
            b = np.abs(Hn - bwd) / res
        d = np.where(np.isfinite(f) & np.isfinite(b), np.minimum(f, b), np.where(np.isfinite(f), f, np.where(np.isfinite(b), b, 0.0)))
        g2 += d * d
    return np.where(np.isfinite(Hn), np.sqrt(g2), 0.0)


SLOPE_MIN = 0.25                     # a part surface steeper than this (14°) is a sloped face (chamfer, bevel, draft)


def _features(m, surf, amt, slope, res, x0, y0, prefix=""):
    """Leftover cells `m` of one body grouped by what the part surface does there: sloped faces (one feature: their z
    range), level faces left above their height (one feature per level), thin strips along an edge or wall."""
    from scipy import ndimage
    cell = res * res
    feats = []

    def item(kind, mm, zlo, zhi):
        a = np.where(mm, amt, -1.0); j = int(np.argmax(a)); iy, ix = divmod(j, mm.shape[1])
        return {"kind": prefix + kind, "z_lo": round(float(zlo), 2), "z_hi": round(float(zhi), 2),
                "area_mm2": round(float(mm.sum() * cell), 1), "max_mm": round(float(amt[mm].max()), 2),
                "at": (round(x0 + ix * res, 2), round(y0 + iy * res, 2))}
    sl = m & (slope > SLOPE_MIN)
    if sl.any():                                          # sloped patches grouped by overlapping height ranges
        lab, n = ndimage.label(sl, structure=np.ones((3, 3), bool))
        spans = []
        for k, sl_ in enumerate(ndimage.find_objects(lab), 1):
            comp = lab[sl_] == k
            z = surf[sl_][comp]
            spans.append([float(z.min()), float(z.max()), [k]])
        spans.sort(key=lambda t: t[0])
        groups = []
        for sp in spans:
            if groups and sp[0] <= groups[-1][1] + 0.2:
                groups[-1][1] = max(groups[-1][1], sp[1]); groups[-1][2] += sp[2]
            else:
                groups.append(sp)
        for zlo_, zhi_, ks in groups:
            feats.append(item("sloped", np.isin(lab, ks), zlo_, zhi_))
    fl = m & ~sl
    if fl.any():
        lab, n = ndimage.label(fl, structure=np.ones((3, 3), bool))
        levels: dict[int, np.ndarray] = {}
        edge = np.zeros_like(fl)
        for k, sl_ in enumerate(ndimage.find_objects(lab), 1):
            comp = lab[sl_] == k
            core = ndimage.binary_erosion(comp)
            if core.sum() < 0.25 * comp.sum():            # a strip a cell or two wide: along an edge / a wall
                edge[sl_] |= comp
                continue
            z = float(np.median(surf[sl_][comp]))
            key = int(round(z / 0.1))
            full = levels.setdefault(key, np.zeros_like(fl))
            full[sl_] |= comp
        for key, mm in levels.items():
            feats.append(item("flat", mm, surf[mm].min(), surf[mm].max()))
        if edge.any():
            feats.append(item("edge", edge, surf[edge].min(), surf[edge].max()))
    return sorted(feats, key=lambda f: -f["area_mm2"])


def _leftover(bodies, left_t, amt_t, left_b, amt_b, res, x0, y0) -> list[dict]:
    """Material left on the part per body (the body owning the column) and feature."""
    out = []
    owner = bodies["owner"]
    st = _slope(bodies["top"], res)
    sb = _slope(bodies["bot"], res) if left_b is not None and left_b.any() else None
    cell = res * res
    for k, name in enumerate(bodies["names"] + ["(no body)"]):
        kk = k if k < len(bodies["names"]) else -1
        mt = left_t & (owner == kk)
        mb = (left_b & (owner == kk)) if sb is not None else None
        if not mt.any() and (mb is None or not mb.any()):
            continue
        feats = _features(mt, bodies["top"], amt_t, st, res, x0, y0) if mt.any() else []
        if mb is not None and mb.any():
            feats += _features(mb, bodies["bot"], amt_b, sb, res, x0, y0, "underside ")
        both = mt | mb if mb is not None else mt
        amt = np.maximum(np.where(mt, amt_t, 0.0), np.where(mb, amt_b, 0.0) if mb is not None else 0.0)
        out.append({"body": name, "area_mm2": round(float(both.sum() * cell), 1), "max_mm": round(float(amt.max()), 2),
                    "volume_mm3": round(float(amt.sum() * cell), 1), "features": sorted(feats, key=lambda f: -f["area_mm2"])})
    return sorted(out, key=lambda b: -b["area_mm2"])


def _disc(r_cells: float):
    k = int(math.ceil(r_cells))
    oy, ox = np.mgrid[-k:k + 1, -k:k + 1]
    m = (ox * ox + oy * oy) <= r_cells * r_cells + 1e-9
    return oy[m], ox[m]


def _cells_near(pts_xy, r, res, x0, y0, shape):
    """Bool mask of the cells whose centre is within r of any of the points."""
    ny, nx = shape
    mask = np.zeros(shape, dtype=bool)
    if len(pts_xy) == 0:
        return mask
    c = np.unique(np.stack([np.rint((pts_xy[:, 1] - y0) / res), np.rint((pts_xy[:, 0] - x0) / res)], axis=1).astype(np.int64), axis=0)
    oy, ox = _disc(r / res)
    Y = (c[:, 0:1] + oy[None, :]).ravel(); X = (c[:, 1:2] + ox[None, :]).ravel()
    ok = (Y >= 0) & (Y < ny) & (X >= 0) & (X < nx)
    mask[Y[ok], X[ok]] = True
    return mask


def _find_tabs(op, mv, zs, side, pts, idx, kinds, zhi, zlo, res, x0, y0, bodies, rel, attach_min, tab_min) -> list[dict]:
    """The tabs a tabbed contour left, measured on the stock right after it: the path's stretches at the tab top (below
    the op's first pass under it) seed them; a tab is the connected stock standing in the kerf (the cells the tool
    swept at or below the tab top) above the kerf floor around a seed. Its metal is what actually joins the part to
    the waste there: the stock's top over the tab minus the higher of the stock's bottom and the part's underside next
    to it (a back already thinned by an earlier setup counts). Registers each tab's cells with the release tracker."""
    from scipy import ndimage
    p = op.params or {}
    if not int(p.get("tabs") or 0) or zs is None or len(mv) < 2:
        return []
    th = p.get("tab_height")
    if th is None:
        th = (op.cut_overrides or {}).get("tab_height", (op.cut_defaults or {}).get("tab_height", 3.0))
    tt = float(p.get("tab_top", float(p["z_bottom"]) + float(th)))           # setup frame
    feed = np.array([m[0] != cam.RAPID for m in mv])
    below = zs < tt - 0.01
    if not below.any():
        return []
    first = int(np.argmax(below))
    at_tt = np.abs(zs - tt) < 0.01
    seed_mv = np.zeros(len(mv), dtype=bool)
    seed_mv[1:] = feed[1:] & at_tt[1:] & at_tt[:-1]
    seed_mv[:first + 1] = False
    kerf_mv = feed & (zs <= tt + 0.01)
    kerf_mv[:first] = False
    if not seed_mv.any():
        return []
    seed_s, kerf_s = seed_mv[idx], kerf_mv[idx]
    R = op.tool.shape.cut_radius
    shape = zhi.shape
    kerf = _cells_near(pts[kerf_s, :2], 0.8 * R, res, x0, y0, shape)
    floor = pts[kerf_s, 2].min() if side > 0 else pts[kerf_s, 2].max()
    with np.errstate(invalid="ignore"):
        mat = zhi > zlo + 1e-6
        standing = kerf & mat & ((zhi - floor > attach_min) if side > 0 else (floor - zlo > attach_min))
    lab, n = ndimage.label(standing, structure=np.ones((3, 3), bool))
    seeds = np.flatnonzero(seed_s)
    S = pts[seeds]
    D = pts[np.minimum(seeds + 1, len(pts) - 1), :2] - pts[np.maximum(seeds - 1, 0), :2]   # path direction at each seed
    nrm = np.hypot(D[:, 0], D[:, 1])
    ok = nrm > 1e-9
    S, D, nrm = S[ok], D[ok], nrm[ok]
    Nx, Ny = -D[:, 1] / nrm, D[:, 0] / nrm
    sy = np.clip(np.rint((S[:, 1] - y0) / res).astype(np.int64), 0, shape[0] - 1)
    sx = np.clip(np.rint((S[:, 0] - x0) / res).astype(np.int64), 0, shape[1] - 1)
    sl = lab[sy, sx]
    # one tab per cluster of seed cells (every pass under the tab top rides over the same stretch); a tab whose stretch
    # has no metal under it (the kerf was already cut there) is still a tab: of 0 mm
    seedmask = np.zeros(shape, dtype=bool); seedmask[sy, sx] = True
    tl, _ = ndimage.label(ndimage.binary_dilation(seedmask, iterations=2), structure=np.ones((3, 3), bool))
    st_ = tl[sy, sx]
    # the tab's metal at each seed: the thinnest stock column on the line across the kerf through it (the tool's width
    # either side of the path) - the bridge is only as thick as its thinnest section between part and waste
    with np.errstate(invalid="ignore"):
        T = np.where(zhi > zlo, zhi - zlo, 0.0)
    ts = np.arange(-R, R + 1e-9, res / 2)
    cy = np.clip(np.rint((S[:, 1:2] + Ny[:, None] * ts[None, :] - y0) / res).astype(np.int64), 0, shape[0] - 1)
    cx = np.clip(np.rint((S[:, 0:1] + Nx[:, None] * ts[None, :] - x0) / res).astype(np.int64), 0, shape[1] - 1)
    cross = T[cy, cx].min(axis=1)
    owner, names = bodies["owner"], bodies["names"]
    oy, ox = _disc((R + 2 * res) / res)
    tabs = []
    for L in [int(v) for v in dict.fromkeys(st_.tolist()) if v > 0]:             # in path order
        sel_ = st_ == L
        c = cross[sel_]
        metal = float(c.max())
        core = sel_.copy(); core[sel_] = c >= 0.9 * metal - 1e-6
        dx_, dy_ = D[sel_].mean(axis=0) if sel_.sum() else (1.0, 0.0)
        dn = math.hypot(dx_, dy_) or 1.0
        proj = (S[core, 0] * dx_ + S[core, 1] * dy_) / dn
        width = float(proj.max() - proj.min()) if len(proj) and metal > 0.05 else 0.0
        Y = np.clip(sy[sel_][:, None] + oy[None, :], 0, shape[0] - 1); X = np.clip(sx[sel_][:, None] + ox[None, :], 0, shape[1] - 1)
        ow = owner[Y, X].ravel()
        ow = ow[ow >= 0]
        body = names[int(np.bincount(ow).argmax())] if len(ow) else "(no part)"
        tabs.append({"op": op.name, "tab": len(tabs) + 1, "metal_mm": round(metal, 2), "min_mm": round(float(c.min()), 2),
                     "width_mm": round(width, 2),
                     "z_top": round(float(np.median((zhi if side > 0 else zlo)[sy[core], sx[core]])), 2),
                     "at": (round(float(S[core, 0].mean()), 1), round(float(S[core, 1].mean()), 1)),
                     "body": body, "ok": bool(metal >= tab_min - 1e-6)})
        comps = [int(v) for v in np.unique(sl[sel_]) if v > 0]                   # the standing stock it left
        if rel is not None and comps:
            rel.add_tab(ndimage.binary_dilation(np.isin(lab, comps)))
    return tabs


def _components(zhi, zlo, keep, eps):
    """Connected material (labels, -1 = none): 4-neighbour columns whose material intervals overlap by more than eps."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    ny, nx = zhi.shape
    with np.errstate(invalid="ignore"):
        mat = keep & (zhi - zlo > eps)
        er = mat[:, :-1] & mat[:, 1:] & (np.minimum(zhi[:, :-1], zhi[:, 1:]) - np.maximum(zlo[:, :-1], zlo[:, 1:]) > eps)
        ed = mat[:-1, :] & mat[1:, :] & (np.minimum(zhi[:-1, :], zhi[1:, :]) - np.maximum(zlo[:-1, :], zlo[1:, :]) > eps)
    ids = np.arange(ny * nx).reshape(ny, nx)
    r = np.concatenate([ids[:, :-1][er], ids[:-1, :][ed]]); c = np.concatenate([ids[:, 1:][er], ids[1:, :][ed]])
    g = coo_matrix((np.ones(len(r), dtype=np.int8), (r, c)), shape=(ny * nx, ny * nx)).tocsr()
    _, lab = connected_components(g, directed=False)
    lab = lab.reshape(ny, nx)
    lab[~mat] = -1
    return lab


class _Release:
    """Is each body still part of the plate? A body is held while its material connects (columns overlapping by more
    than attach_min) to the frame - the largest piece of stock outside all the bodies; tab-held when it connects only
    through the tabs of tabbed contours (_find_tabs registers their cells); free when nothing connects it. Bodies that
    are not held at the start (outside the stock, or no frame around them) are not tracked."""

    def __init__(self, bodies, res, attach_min, x0=0.0, y0=0.0):
        from scipy import ndimage
        self.x0, self.y0 = x0, y0
        self.names = bodies["names"]
        self.fp = bodies["has"]
        anyb = np.logical_or.reduce(self.fp) if self.fp else None
        self.outside = ~ndimage.binary_dilation(anyb, iterations=2) if anyb is not None else None
        self.tab_mask = np.zeros_like(anyb) if anyb is not None else None
        self.res, self.eps = res, attach_min
        self.min_frame = max(4, int(4.0 / (res * res)))        # >= 4 mm² of stock outside the bodies
        self.state: dict[str, tuple[str, str | None]] = {}     # name -> (state, the op that made it so)
        self.tracked: list[int] = []

    def add_tab(self, mask):
        self.tab_mask |= mask

    def _attached(self, zhi, zlo, keep) -> list[bool]:
        lab = _components(zhi, zlo, keep, self.eps)
        out_lab = lab[self.outside & (lab >= 0)]
        if len(out_lab) == 0:
            return [False] * len(self.names)
        cnt = np.bincount(out_lab)
        frame = int(cnt.argmax())
        if cnt[frame] < self.min_frame:
            return [False] * len(self.names)
        return [bool((lab[fp] == frame).any()) for fp in self.fp]

    def classify(self, zhi, zlo) -> dict[int, str]:
        full = self._attached(zhi, zlo, np.ones(zhi.shape, dtype=bool))
        notab = self._attached(zhi, zlo, ~self.tab_mask) if self.tab_mask.any() else full
        return {k: ("held" if notab[k] else "tab-held" if full[k] else "free") for k in range(len(self.names))}

    def start(self, zhi, zlo, log):
        with np.errstate(invalid="ignore"):
            mat = zhi > zlo
        st = self.classify(zhi, zlo)
        for k, name in enumerate(self.names):
            if st[k] == "held" and (mat & self.fp[k]).any():
                self.tracked.append(k)
                self.state[name] = ("held", None)
                log.append({"body": name, "op": None, "state": "held", "prev": None, "history": True})

    def after(self, op_name, zhi, zlo, log, history=False, violations=None):
        """The states after an op; an op (not history) that cuts the tabs a tab-held body hung on, leaving it free, is
        a violation too: the part comes loose under the cutter."""
        if not self.tracked:
            return
        st = self.classify(zhi, zlo)
        for k in self.tracked:
            name = self.names[k]
            prev, since = self.state[name]
            if st[k] != prev:
                self.state[name] = (st[k], op_name)
                log.append({"body": name, "op": op_name, "state": st[k], "prev": prev, "history": history})
                if violations is not None and not history and prev == "tab-held" and st[k] == "free":
                    violations.append({"op": op_name, "body": name, "state": prev, "since": since, "removed_mm3": 0.0,
                                       "at": None, "freed": True})

    def check_cut(self, op_name, before, zhi, zlo, violations, min_mm3: float = 0.5):
        """An op that removes material from a body that was tab-held or free when it started."""
        bhi, blo = before
        with np.errstate(invalid="ignore"):
            m0 = np.where(bhi > blo, bhi - blo, 0.0)
            m1 = np.where(zhi > zlo, zhi - zlo, 0.0)
        gone = np.clip(m0 - m1, 0.0, None)
        cell = self.res * self.res
        for k in self.tracked:
            name = self.names[k]
            state, since = self.state[name]
            if state == "held":
                continue
            g = np.where(self.fp[k], gone, 0.0)
            v = float(g.sum()) * cell
            if v > min_mm3:
                iy, ix = divmod(int(np.argmax(g)), g.shape[1])        # where it cuts the most
                at = (round(self.x0 + ix * self.res, 1), round(self.y0 + iy * self.res, 1))
                violations.append({"op": op_name, "body": name, "state": state, "since": since, "removed_mm3": round(v, 1), "at": at})


def _nb_extreme(H, op, pad):
    """Per cell, the min (op=np.minimum) or max of its 4 neighbours' values; `pad` beyond the grid."""
    P = np.pad(H, 1, constant_values=pad)
    return op(op(P[:-2, 1:-1], P[2:, 1:-1]), op(P[1:-1, :-2], P[1:-1, 2:]))


def _gouge_z(pmaps, zhi, zlo, tol, has_bottom):
    top, bot = pmaps
    has = np.isfinite(top)                               # the part occupies this column
    mat = zhi > zlo
    # ignore the outermost cell ring of the part: an outline cut that runs exactly along the edge is not a gouge
    core = has.copy()
    core[1:, :] &= has[:-1, :]; core[:-1, :] &= has[1:, :]; core[:, 1:] &= has[:, :-1]; core[:, :-1] &= has[:, 1:]
    # a cell on a wall inside the part (a step in its top or bottom surface) holds both sides of the wall: the cut that
    # stops at the wall's far side is right, so a cell whose neighbour reaches the cut's depth is not a gouge (a pocket
    # whose last pass runs along a round wall otherwise "gouges" single cells all the way round it)
    nb_top_min, nb_bot_max = _nb_extreme(top, np.minimum, np.inf), _nb_extreme(bot, np.maximum, -np.inf)
    with np.errstate(invalid="ignore"):
        g_top = core & ((~mat) | (zhi < top - tol)) & ~(mat & (nb_top_min <= zhi + tol))   # below the part's top (or through it)
        g_bot = (core & mat & (zlo > bot + tol) & ~(nb_bot_max >= zlo - tol)) if has_bottom else np.zeros_like(has)
    gouge = g_top | g_bot
    with np.errstate(invalid="ignore"):                   # inf - inf outside the part, never selected
        d_top = np.where(mat, top - zhi, top - bot)
        d_bot = np.where(mat, zlo - bot, 0.0)
        depth = np.where(gouge, np.maximum(np.where(g_top, d_top, 0.0), np.where(g_bot, d_bot, 0.0)), 0.0)
    return gouge, depth, has, mat


def _no_step(H, res):
    """Cells whose 4 neighbours are within max(0.5, 2·res) of their own height (finite heights only)."""
    jump = max(0.5, 2 * res)
    ok = np.isfinite(H)
    with np.errstate(invalid="ignore"):
        for axis, shift in ((0, 1), (0, -1), (1, 1), (1, -1)):
            nb = np.roll(H, shift, axis=axis)
            d = np.abs(nb - H)
            ok &= ~(np.isfinite(nb) & (d > jump))
    return ok


def _compare_zmap(out, pmaps, zhi, zlo, res, x0, y0, nx, tol, has_bottom, bodies=None):
    top, bot = pmaps
    gouge, depth, has, mat = _gouge_z(pmaps, zhi, zlo, tol, has_bottom)
    flat_t, flat_b = _no_step(top, res), _no_step(bot, res)   # a column straddling a wall is not "left" material
    left = has & mat & flat_t & (zhi > top + tol)
    left_t, left_b, amt_b = left, None, None
    with np.errstate(invalid="ignore"):
        amt_t = np.where(left, zhi - top, 0.0)
        left_amt = amt_t
        if has_bottom:
            left_b = has & mat & flat_b & (zlo < bot - tol)
            amt_b = np.where(left_b, bot - zlo, 0.0)
            left_amt = np.maximum(left_amt, amt_b)
            left = left | left_b
    if bodies is not None and left.any():
        out.leftover = _leftover(bodies, left_t, amt_t, left_b, amt_b, res, x0, y0)
    outside = (~has) & mat
    cell = res * res
    g = {"cells": int(gouge.sum()), "area_mm2": round(float(gouge.sum() * cell), 2),
         "max_depth": round(float(depth.max()), 3) if gouge.any() else 0.0, "worst": None, "ops": []}
    if gouge.any():
        j = int(np.argmax(depth)); iy, ix = divmod(j, nx)
        g["worst"] = (round(x0 + ix * res, 2), round(y0 + iy * res, 2))
    out.stats["gouge"] = g
    out.stats["left"] = {"max": round(float(left_amt.max()), 3) if left.any() else 0.0, "area_mm2": round(float(left.sum() * cell), 1),
                         "volume_cm3": round(float(left_amt.sum() * cell / 1000.0), 3), "tol": tol}
    out.stats["outside_cm3"] = round(float(np.where(outside, zhi - zlo, 0.0).sum() * cell / 1000.0), 3)
    out.masks = {"gouge": gouge, "left": left}


# ----------------------------------------------------------------------------- radial
def _sim_radial(program, sel, part, resolution, frames, tol, machine_model=None, placement=None, res_a=1.0, pre=()) -> SimResult:
    """4th-axis material removal on a (angle, x) radius map, plus what the height map can't see: the holder stack and the
    body above the flutes against the remaining bar (contact, not cutting), and the whole tool stack against the chuck,
    its jaws and the tailstock (from the machine model's rotary dict and the setup's stock) for every move."""
    placement = placement or {}
    setups = [program.setup_of(program.ops[i]) for i in sel]
    st = setups[0]
    for i, s_ in zip(sel[1:], setups[1:]):
        if s_ is not st and not cam.Program.same_mounting(st, s_):
            raise cam.CamError(f"simulate: '{program.ops[sel[0]].name}' ({st.name}) and '{program.ops[i].name}' ({s_.name}) are on "
                               "different 4th-axis mountings (the stock, the rotary axis, the origin or the machine differ): "
                               "simulate the setups separately")
    x0, x1 = st.stock.xmin, st.stock.xmax
    res_x = resolution or max(0.2, (x1 - x0) / 400.0)
    nx = int(math.ceil((x1 - x0) / res_x)) + 1
    na = max(36, int(round(360.0 / max(float(res_a or 1.0), 0.05))))
    res_a = 360.0 / na
    psi = np.radians(np.arange(na) * res_a)
    # initial radius per angle: cylinder, or the stock box's cross-section seen from the axis
    ms = st.model_stock
    if ms.is_cylinder:
        r0 = np.full(na, ms.radius)
    else:
        y0a, z0a = st.axis
        hy0, hy1 = ms.ymin - y0a, ms.ymax - y0a
        hz0, hz1 = ms.zmin - z0a, ms.zmax - z0a
        s_, c_ = np.sin(psi), np.cos(psi)
        with np.errstate(divide="ignore", invalid="ignore"):
            ty = np.where(s_ > 1e-12, hy1 / s_, np.where(s_ < -1e-12, hy0 / s_, np.inf))
            tz = np.where(c_ > 1e-12, hz1 / c_, np.where(c_ < -1e-12, hz0 / c_, np.inf))
        r0 = np.minimum(ty, tz)
    r = np.tile(r0[:, None], (1, nx))
    spacing = min(res_x / 2, 0.25)
    for i in pre:                                        # the earlier ops on this mounting: stock history, not reported
        op = program.ops[i]; s_ = program.setup_of(op)
        if op.moves and op.kind != "probe":
            mv = [list(m[:5]) + [m[5] if len(m) > 5 else s_.a] for m in op.moves]
            pts, idx, kinds = _samples(mv, spacing, True)
            _stamp_r(r, pts, kinds, idx, op.tool, res_x, res_a, x0, [], op.name)
    initial = 0.5 * (r ** 2).sum() * math.radians(res_a) * res_x
    rmax = float(r0.max())
    grid = {"res": res_x, "x0": x0, "nx": nx, "na": na, "res_a": res_a, "rmax": rmax,
            "axis": list(st.axis), "x1": x1}
    out = SimResult("radial", grid)
    out.history = [program.ops[i].name for i in pre]
    total = sum(len(program.ops[i].moves) for i in sel)
    marks = _frame_marks(total, frames)
    mark_i = 0
    done = 0
    rapid_hits = []
    hits: list[dict] = []
    mount = _rotary_mounting(st, machine_model, placement)

    def snap(upto):
        q = np.clip(np.round(r / max(rmax, 1e-9) * 65534), 0, 65534).astype(np.uint16)
        out.frames.append((upto, {"r": zlib.compress(q.tobytes(), 6)}))

    # the part's radius map (for the gouge test after every op: which op cut into the part, and where)
    R = core = None
    if part is not None:
        shape = part.shape if isinstance(part, ck.Model) else part
        xs, angles, R = cam.radial_map(st, shape, res_x, res_a, (x0, x0 + (nx - 1) * res_x))
        R = R[:, :nx] if R.shape[1] >= nx else np.pad(R, ((0, 0), (0, nx - R.shape[1])))
        has = R > 0
        # cells beside a step in the part (shoulders, groove walls, the edges of flats) belong to both sides of the
        # step on this grid: leave them out of the gouge test
        jump = max(0.5, 2 * res_x)
        core = has.copy()
        for axis, shift in ((1, 1), (1, -1), (0, 1), (0, -1)):
            nb = np.roll(R, shift, axis=axis)
            if axis == 1:                                # no wrap along X
                if shift > 0:
                    nb[:, 0] = R[:, 0]
                else:
                    nb[:, -1] = R[:, -1]
            core &= np.abs(nb - R) <= jump
    cell_a = np.radians(res_a) * res_x                   # cell area = cell_a · R
    gouged = np.zeros((na, nx), dtype=bool)
    if R is not None and pre:                            # cut into the part by the history: not the selected ops' doing
        gouged = core & (r < R - tol)
    gouged0 = int(gouged.sum())
    gouge_ops: list[str] = []
    gouge_by_op: list[dict] = []

    snap(0)
    for i in sel:
        op = program.ops[i]; s_ = program.setup_of(op)
        if not op.moves:
            continue
        tool = op.tool
        mv = [list(m[:5]) + [m[5] if len(m) > 5 else s_.a] for m in op.moves]
        pts, idx, kinds = _samples(mv, spacing, True)
        start = 0
        while start < len(pts):
            next_mark = marks[mark_i] if mark_i < len(marks) else None
            if next_mark is not None and next_mark - done <= len(op.moves):
                end = int(np.searchsorted(idx, next_mark - done - 1, side="right"))
            else:
                end = len(pts)
            if end <= start:
                end = start + 1
            P, K, I = pts[start:end], kinds[start:end], idx[start:end]
            _stamp_r(r, P, K, I, tool, res_x, res_a, x0, rapid_hits, op.name)
            _contact_r(hits, r, P, I, tool, machine_model, res_x, res_a, x0, spacing, op.name)
            if mount is not None:
                _collide_mount_r(hits, mount, P, K, I, tool, machine_model, spacing, op.name)
            start = end
            if next_mark is not None and end_move_done(idx, end, done, next_mark):
                snap(next_mark); mark_i += 1
        done += len(op.moves)
        if R is not None:                                # per-op gouge attribution: cells this op first cut below the part
            g = core & (r < R - tol)
            new = g & ~gouged
            if new.any() and float((cell_a * R)[new].sum()) > 0.5:
                d = np.where(new, R - r, -1.0); j = int(np.argmax(d)); ia, ix = divmod(j, nx)
                gouge_ops.append(op.name)
                gouge_by_op.append({"op": op.name, "cells": int(new.sum()), "area_mm2": round(float((cell_a * R)[new].sum()), 2),
                                    "max_depth": round(float(d.flat[j]), 3), "worst": {"x": round(x0 + ix * res_x, 2), "angle": round(ia * res_a, 1)}})
            gouged |= g
        while mark_i < len(marks) and marks[mark_i] <= done:
            snap(marks[mark_i]); mark_i += 1
    if not out.frames or out.frames[-1][0] != total:
        snap(total)
    remaining = 0.5 * (r ** 2).sum() * math.radians(res_a) * res_x
    out.stats = {"moves": total, "ops": len(sel), "removed_cm3": round((initial - remaining) / 1000.0, 3), "rapid_hits": rapid_hits[:50]}
    if machine_model is not None or hits:
        out.stats["collisions"] = _collision_stats(hits)
    if machine_model is not None:
        out.stats["machine"] = {"key": machine_model.key, "name": machine_model.name, "placement": placement}
    if mount is not None:
        out.stats["mounting"] = {k: mount[k] for k in ("chuck_face_x", "jaw_tip_x", "jaw_ring_r", "tail_tip_x", "tail", "chuck_gap", "tail_gap")}
    if R is not None:
        gouge = core & (r < R - tol)
        left = core & (r > R + tol)                     # material in a corner shows up beside the step, not on it
        cell = np.outer(np.full(na, 1.0), np.full(nx, res_x)) * np.radians(res_a)
        g = {"cells": int(gouge.sum()), "area_mm2": round(float((cell * R)[gouge].sum()), 2),
             "max_depth": round(float((R - r)[gouge].max()), 3) if gouge.any() else 0.0, "worst": None,
             "ops": gouge_ops, "by_op": gouge_by_op, "new_cells": int(gouge.sum()) - gouged0}
        if gouge.any():
            j = int(np.argmax(np.where(gouge, R - r, -1))); ia, ix = divmod(j, nx)
            g["worst"] = {"x": round(x0 + ix * res_x, 2), "angle": round(ia * res_a, 1)}
        out.stats["gouge"] = g
        amt = np.where(left, r - R, 0.0)
        out.stats["left"] = {"max": round(float(amt.max()), 3) if left.any() else 0.0, "area_mm2": round(float((cell * R)[left].sum()), 1),
                             "volume_cm3": round(float((amt * cell * R)[left].sum() / 1000.0), 3), "tol": tol}
        out.masks = {"gouge": gouge, "left": left}
    out.final = {"r": r}
    return out


def _rotary_mounting(st, mm, placement) -> dict | None:
    """Where the chuck, its jaws and the tailstock are in the SETUP frame (x along the rotary axis, y = z = 0 on it), from
    the machine model's rotary dict and the setup's stock: the chuck body face sits chuck_gap behind the stock's near end,
    the jaws reach jaw_reach past the face (a full ring of radius jaw_ring_r: the jaws' tips, or the stock plus a 6 mm jaw
    wall when the stock is bigger), the live centre's tip sits tail_gap past the stock's far end."""
    rot = getattr(mm, "rotary", None) if mm is not None else None
    if not rot:
        return None
    stock = st.stock                                     # setup frame: xmin..xmax along the axis, radius = swing
    r_st = float(getattr(stock, "radius", None) or st.max_radius)
    gap = float(placement.get("chuck_gap", rot.get("chuck_gap", 5.0)))
    tgap = float(placement.get("tail_gap", rot.get("tail_gap", 0.0)))
    face = float(stock.xmin) - gap
    reach = float(rot.get("jaw_reach", 0.0))
    jaw_r = max(float(rot.get("jaw_r", rot["chuck_r"])), r_st + 6.0)
    tail = bool(rot.get("tail", rot.get("tail_x") is not None))
    cone = rot.get("tail_cone") or {"half_angle": 30.0, "length": float(rot.get("tail_r", 7.0)) / math.tan(math.radians(30.0))}
    blk = rot.get("tail_block")
    az = float(rot.get("axis_z", 0.0))
    return {"chuck_face_x": round(face, 3), "chuck_r": float(rot["chuck_r"]), "chuck_gap": gap,
            "jaw_tip_x": round(face + reach, 3), "jaw_reach": reach, "jaw_ring_r": round(jaw_r, 3),
            "tail": tail, "tail_gap": tgap, "tail_tip_x": round(float(stock.xmax) + tgap, 3) if tail else None,
            "tail_r": float(rot.get("tail_r", 7.0)), "tail_half": float(cone.get("half_angle", 30.0)), "tail_len": float(cone.get("length", 0.0)),
            "tail_barrel": float(rot.get("tail_barrel", 0.0)),
            "tail_block": ({"half_w": float(blk["half_w"]), "z0": float(blk["z0"]) - az, "z1": float(blk["z1"]) - az,
                            "length": float(blk.get("length", 40.0))} if blk else None)}


def _levels_above_flutes(mm, tool):
    """Everything above the cutting part as (name, radius, bottom above the tip, height, box): the tool's own bands over the
    flutes (named 'tool above its N mm flutes' where they are no wider than the flutes), then the machine's nose stack."""
    sh = tool.shape
    fl, cut_r = float(sh.flute_length), float(sh.cut_radius)
    if mm is not None:
        levels = holder_levels(mm, tool)
    else:
        stick = tool_stickout(tool)
        levels = [("tool shank" if rr >= sh.max_radius - 1e-6 else "tool neck", rr, b, t - b, None) for b, t, rr in sh.bands(fl) if b < stick] \
            or [("tool shank", tool.radius, fl, max(stick - fl, 0.0), None)]
    out = []
    for name, rr, zb, h, box in levels:
        if h <= 0:
            continue
        if name in ("tool shank", "tool neck") and rr <= cut_r + 1e-6:
            name = f"tool above its {fl:g} mm flutes"
        out.append((name, rr, zb, h, box))
    return out


def _thin(n: int, step: int, I) -> np.ndarray:
    """Sample indices every `step`, always including the last sample of every move (a straight move is deepest at an end)."""
    ends = np.flatnonzero(np.diff(I)) if len(I) > 1 else np.zeros(0, int)
    return np.unique(np.concatenate([np.arange(0, n, max(1, step)), ends, [n - 1]]).astype(np.int64))


def _ray_interval(sp, cp, ylo, yhi, zlo, zhi):
    """For rays s·(sin φ, cos φ) (arrays broadcast together): the s-interval inside the rectangle y ∈ [ylo, yhi],
    z ∈ [zlo, zhi] (s_lo ≥ 0; empty where s_lo ≥ s_hi)."""
    # a ray parallel to a slab is inside it for every s (−inf, +inf) or never (+inf, +inf: an empty interval after min/max)
    with np.errstate(divide="ignore", invalid="ignore"):
        in_y = (ylo <= 0) & (yhi >= 0); in_z = (zlo <= 0) & (zhi >= 0)
        ty1 = np.where(np.abs(sp) > 1e-9, ylo / sp, np.where(in_y, -np.inf, np.inf))
        ty2 = np.where(np.abs(sp) > 1e-9, yhi / sp, np.inf)
        tz1 = np.where(np.abs(cp) > 1e-9, zlo / cp, np.where(in_z, -np.inf, np.inf))
        tz2 = np.where(np.abs(cp) > 1e-9, zhi / cp, np.inf)
    s_lo = np.maximum(np.maximum(np.minimum(ty1, ty2), np.minimum(tz1, tz2)), 0.0)
    s_hi = np.minimum(np.maximum(ty1, ty2), np.maximum(tz1, tz2))
    return s_lo, s_hi


def _contact_r(hits, r, P, I, tool, mm, res_x, res_a, x0, spacing, op_name):
    """After a piece is stamped: does anything above the flutes (the tool body over them, a neck, the shank, the nose
    stack) stand inside the bar that is left? Each level is a vertical cylinder (or box) over the tool tip; a ray of the
    radius map (part angle ψ → machine angle φ = ψ − A) carries material out to r[ψ, x]: contact where the ray enters the
    level before it leaves the material. Sampled every max(level radius / 2, cell) along the path."""
    if len(P) == 0:
        return
    na, nx = r.shape
    for name, rl, zb, h, box in _levels_above_flutes(mm, tool):
        pick = _thin(len(P), int(max(rl / 2, res_x) / spacing), I)
        Q = P[pick]; IQ = I[pick]
        xt, yt, zt, at = Q[:, 0], Q[:, 1], Q[:, 2], Q[:, 3]
        if box is not None:
            lx0, lx1 = min(b[0] for b in box), max(b[1] for b in box)
            ly0, ly1 = min(b[2] for b in box), max(b[3] for b in box)
        else:
            lx0, lx1, ly0, ly1 = -rl, rl, -rl, rl
        ixa = np.clip(np.floor((xt + lx0 - x0) / res_x).astype(np.int64), 0, nx - 1)
        ixb = np.clip(np.ceil((xt + lx1 - x0) / res_x).astype(np.int64), 0, nx - 1)
        z_c = zt + zb
        worst = None
        for k in range(len(Q)):
            if ixb[k] < ixa[k]:
                continue
            sub = r[:, ixa[k]:ixb[k] + 1]                         # (na, w)
            if sub.max() <= z_c[k] + 0.02:                       # material is never higher than its radius: nothing reaches the level
                continue
            phi = np.radians(np.arange(na) * res_a - at[k])[:, None]
            sp, cp = np.sin(phi), np.cos(phi)
            if box is None:                                      # round level: half-width shrinks with the X offset of the cell
                dx = x0 + np.arange(ixa[k], ixb[k] + 1) * res_x - xt[k]
                rho = np.sqrt(np.maximum(rl * rl - dx * dx, 0.0))[None, :]
                ok_x = (np.abs(dx) < rl)[None, :]
                ylo, yhi = yt[k] - rho, yt[k] + rho
            else:
                ok_x = np.ones((1, sub.shape[1]), dtype=bool)
                ylo, yhi = yt[k] + ly0, yt[k] + ly1
            s_lo, s_hi = _ray_interval(sp, cp, ylo, yhi, z_c[k], z_c[k] + h)
            m = ok_x & (s_lo < s_hi) & (s_lo < sub - 0.02)
            if m.any():
                depth = float((sub - s_lo)[m].max())
                if worst is None or depth > worst["depth"]:
                    worst = {"op": op_name, "move": int(IQ[k]), "pos": [round(float(v), 2) for v in Q[k, :3]], "a": round(float(at[k]), 2),
                             "part": name, "what": "stock", "depth": round(depth, 2)}
        if worst is not None:
            hits.append(worst)


def _collide_mount_r(hits, mount, P, K, I, tool, mm, spacing, op_name):
    """Every level of the tool stack — the flutes, the body above them, the nose stack — against the chuck body (a cylinder
    behind the chuck face), the jaw ring and the tailstock (cone, barrel, block), all solids about the rotary axis in the
    setup frame, for every sample of every move (rapids too). One hit per level and solid per piece: the deepest."""
    if len(P) == 0:
        return
    sh = tool.shape
    fl = float(sh.flute_length)
    levels = [("tool", rr, b, t - b, None) for b, t, rr in sh.bands(0.0) if t <= fl + 1e-6] or [("tool", float(sh.cut_radius), 0.0, fl, None)]
    levels += _levels_above_flutes(mm, tool)
    face, chuck_r = mount["chuck_face_x"], mount["chuck_r"]
    jaw_tip, jaw_r = mount["jaw_tip_x"], mount["jaw_ring_r"]
    for name, rl, zb, h, box in levels:
        if h <= 0:
            continue
        pick = _thin(len(P), int(max(rl / 2, 0.25) / spacing), I)
        Q = P[pick]; IQ = I[pick]; KQ = K[pick]
        xt, yt, zt = Q[:, 0], Q[:, 1], Q[:, 2]
        if box is not None:
            lx0, lx1 = min(b[0] for b in box), max(b[1] for b in box)
            ly0, ly1 = min(b[2] for b in box), max(b[3] for b in box)
        else:
            lx0, lx1, ly0, ly1 = -rl, rl, -rl, rl
        xlo, xhi = xt + lx0, xt + lx1
        ylo, yhi = yt + ly0, yt + ly1
        zlo, zhi = zt + zb, zt + zb + h
        # distance from the axis (y = z = 0) to the level's cross-section rectangle
        dy = np.maximum(0.0, np.maximum(ylo, -yhi)); dz = np.maximum(0.0, np.maximum(zlo, -zhi))
        dist = np.hypot(dy, dz)
        found = []
        # chuck body: an X cylinder of radius chuck_r for x < face
        m = (xlo < face - 0.02) & (dist < chuck_r - 0.02)
        if m.any():
            found.append(("chuck", np.where(m, np.minimum(chuck_r - dist, face - xlo), -1.0)))
        # jaws: a ring (full disc) of radius jaw_r from the face to the jaw tips
        if jaw_tip > face:
            m = (xlo < jaw_tip - 0.02) & (xhi > face + 0.02) & (dist < jaw_r - 0.02)
            if m.any():
                found.append(("chuck jaws", np.where(m, np.minimum(jaw_r - dist, np.minimum(jaw_tip - xlo, xhi - face)), -1.0)))
        if mount["tail"]:
            tip, half, L = mount["tail_tip_x"], math.radians(mount["tail_half"]), mount["tail_len"]
            # the live centre's cone: radius grows from 0 at the tip to L·tan(half) at its base
            ov0, ov1 = np.maximum(xlo, tip), np.minimum(xhi, tip + L)
            rc = (ov1 - tip) * math.tan(half)                  # the widest the cone is within the overlap
            m = (ov1 > ov0 + 1e-6) & (dist < rc - 0.02)
            if m.any():
                found.append(("tailstock", np.where(m, np.minimum(rc - dist, ov1 - ov0), -1.0)))
            # the barrel behind the cone
            b0, b1 = tip + L, tip + L + mount["tail_barrel"]
            if b1 > b0:
                m = (xlo < b1 - 0.02) & (xhi > b0 + 0.02) & (dist < mount["tail_r"] - 0.02)
                if m.any():
                    found.append(("tailstock", np.where(m, np.minimum(mount["tail_r"] - dist, xhi - b0), -1.0)))
            blk = mount["tail_block"]
            if blk:
                k0, k1 = b1, b1 + blk["length"]
                m = ((xlo < k1 - 0.02) & (xhi > k0 + 0.02) & (ylo < blk["half_w"] - 0.02) & (yhi > -blk["half_w"] + 0.02)
                     & (zlo < blk["z1"] - 0.02) & (zhi > blk["z0"] + 0.02))
                if m.any():
                    pen = np.minimum(np.minimum(xhi - k0, k1 - xlo), np.minimum(blk["half_w"] - ylo, yhi + blk["half_w"]))
                    pen = np.minimum(pen, np.minimum(blk["z1"] - zlo, zhi - blk["z0"]))
                    found.append(("tailstock", np.where(m, pen, -1.0)))
        for what, depth in found:
            j = int(np.argmax(depth))
            if depth[j] <= 0:
                continue
            hits.append({"op": op_name, "move": int(IQ[j]), "pos": [round(float(v), 2) for v in Q[j, :3]], "a": round(float(Q[j, 3]), 2),
                         "part": name, "what": what, "depth": round(float(depth[j]), 2), "rapid": bool(KQ[j] == cam.RAPID)})

def _stamp_r(r, P, K, I, tool, res_x, res_a, x0, rapid_hits, op_name):
    for a, b in _runs(K):
        _stamp_r_run(r, P[a:b], K[a:b], I[a:b], tool, res_x, res_a, x0, rapid_hits, op_name)


SIDE_FORM_TYPES = {"gear_cutter"}      # tools whose radius varies with height both ways and that cut from beside the axis


def _stamp_r_run(r, P, K, I, tool, res_x, res_a, x0, rapid_hits, op_name):
    """P: (n, 4) samples (x, y, z, a) in the setup frame at A = a. Cut each (x, angle) ray the tool covers."""
    na, nx = r.shape
    if len(P) == 0:
        return
    if tool.type in SIDE_FORM_TYPES:
        return _stamp_r_profile(r, P, K, I, tool, res_x, res_a, x0, rapid_hits, op_name)
    R = tool.radius
    kx = int(math.ceil(R / res_x)) + 1
    dxs = np.arange(-kx, kx + 2) * res_x
    # angular window: how far around the axis the tool reaches from its own direction
    zt_min = max(float(np.min(np.hypot(P[:, 1], P[:, 2]))), R, 0.5)
    ka = int(math.ceil(math.degrees(math.asin(min(1.0, (R + 0.5) / zt_min))) / res_a)) + 2
    ka = min(ka, na // 4)
    das = np.arange(-ka, ka + 1)
    chunk = max(1, int(1_500_000 // (len(dxs) * len(das))))
    for s in range(0, len(P), chunk):
        Q = P[s:s + chunk]
        xt, yt, zt, at = Q[:, 0], Q[:, 1], Q[:, 2], Q[:, 3]
        # part angle psi whose ray points at the tool tip: machine angle phi = atan2(y, z), psi = phi + a
        psi_c = np.degrees(np.arctan2(yt, zt)) + at
        ia = (np.rint(psi_c / res_a).astype(np.int64)[:, None] + das[None, :]) % na              # (n, A)
        phi = np.radians(ia * res_a - at[:, None])                                                # machine angle of each ray
        sphi, cphi = np.sin(phi), np.cos(phi)
        ix = np.floor((xt - x0) / res_x).astype(np.int64)[:, None] + np.arange(-kx, kx + 2)[None, :]   # (n, X)
        dx = (x0 + ix * res_x) - xt[:, None]                       # exact X offset of each cell from the tool
        okx = (ix >= 0) & (ix < nx) & (np.abs(dx) < R - 1e-4)
        rho_x = np.sqrt(np.maximum(R * R - dx * dx, 0.0))
        ball = tool.type == "ball"
        prof_x = 0.0 if ball else np.nan_to_num(_profile(tool, np.minimum(np.abs(dx), R)), posinf=1e6)
        # broadcast to (n, A, X). Tool = cylinder (|y - yt| <= rho at this X) above z_c, plus for a ball the sphere
        # centred R above the tip. The ray is s * (sin phi, cos phi) in machine (y, z).
        z_c = zt[:, None, None] + (R if ball else 0.0) + (prof_x[:, None, :] if not ball else 0.0)
        rh = rho_x[:, None, :]
        sp = sphi[:, :, None]; cp = cphi[:, :, None]
        with np.errstate(divide="ignore", invalid="ignore"):
            s_z = np.where(cp > 1e-9, z_c / cp, np.inf)
            s_top = np.where(cp > 1e-9, (zt[:, None, None] + tool.shape.flute_length) / cp, np.inf)   # the flutes end here: nothing above them cuts
            ylo = (yt[:, None, None] - rh); yhi = (yt[:, None, None] + rh)
            t1 = np.where(np.abs(sp) > 1e-9, ylo / sp, np.where((ylo <= 0) & (yhi >= 0), -np.inf, np.inf))
            t2 = np.where(np.abs(sp) > 1e-9, yhi / sp, np.where((ylo <= 0) & (yhi >= 0), np.inf, -np.inf))
        s_lo = np.maximum(np.minimum(t1, t2), 0.0)
        s_hi = np.minimum(np.maximum(t1, t2), s_top)
        s1 = np.maximum(s_z, s_lo)
        valid_c = (cp > 1e-9) & (s1 < s_hi)
        if ball:
            # sphere: |(dx, s sin - yt, s cos - zc)|^2 <= R^2  ->  s^2 - 2 b s + c <= 0
            yt3 = yt[:, None, None]; zc3 = z_c
            bq = yt3 * sp + zc3 * cp
            cq = (dx * dx)[:, None, :] + yt3 * yt3 + zc3 * zc3 - R * R
            disc = bq * bq - cq
            ok_s = disc > 0
            sq = np.sqrt(np.maximum(disc, 0.0))
            sa, sb = bq - sq, bq + sq
            # union of the sphere's and the shank's intervals (they join at the sphere's equator)
            s1 = np.where(ok_s & valid_c, np.minimum(sa, s1), np.where(ok_s, sa, s1))
            s_hi = np.where(ok_s & valid_c, np.maximum(sb, s_hi), np.where(ok_s, sb, s_hi))
            valid = (valid_c | (ok_s & (sb > 0))) & okx[:, None, :]
            s1 = np.maximum(s1, 0.0)
        else:
            valid = valid_c & okx[:, None, :]
        if not valid.any():
            continue
        IA = np.broadcast_to(ia[:, :, None], valid.shape)[valid]
        IX = np.broadcast_to(np.clip(ix, 0, nx - 1)[:, None, :], valid.shape)[valid]
        S1 = s1[valid]; SH = s_hi[valid]
        rap = np.broadcast_to((K[s:s + chunk] == cam.RAPID)[:, None, None], valid.shape)[valid]
        owner = np.broadcast_to(I[s:s + chunk][:, None, None], valid.shape)[valid]
        # the flutes end at s_top, so a sample only cuts a ray once its flutes reach the surface: a chunk is stamped in one
        # go, so pass again for the samples an earlier sample in the chunk has opened the way for (a plunge deeper than
        # the flutes cuts progressively; what it leaves above the flutes is the body's contact, reported by _contact_r)
        for _ in range(8):
            cur = r[IA, IX]
            reach = SH >= cur - 1e-3                                                               # tool covers out to the surface
            cut = reach & (S1 < cur)
            if not cut.any():
                break
            hit = cut & rap & (cur - S1 > 0.05)
            if hit.any() and len(rapid_hits) < 50:
                depth = (cur - S1)[hit]
                j = int(np.argmax(depth))
                rapid_hits.append({"op": op_name, "move": int(owner[hit][j]) + 1, "depth": round(float(depth[j]), 3)})
            np.minimum.at(r, (IA[cut], IX[cut]), S1[cut])
            keep = ~cut
            IA, IX, S1, SH, rap, owner = IA[keep], IX[keep], S1[keep], SH[keep], rap[keep], owner[keep]


def _ray_table(H0, H1, RHO, yt, zt, si, co, dxg, budget=3_000_000):
    """Entry and exit distance along each ray (machine angle given by sin, cos; the ray is s (sin, cos) in the setup's
    (y, z) at the X offset dx from the tool axis) through a stack of cylinders (heights h0..h1 above the tool tip at
    (yt, zt), radius rho). inf / -inf where the ray misses the tool. The union of the slabs' intervals is taken as one
    (entry = first entry, exit = last exit): anything between would be floating material."""
    A, X = len(si), len(dxg)
    S1 = np.full((A, X), np.inf)
    SH = np.full((A, X), -np.inf)
    eps = 1e-9
    zA_all, zB_all = zt + H0, zt + H1
    horiz = np.abs(co) < eps
    vert = (np.abs(si) < eps)[None, :, None]
    kchunk = max(1, budget // (A * X))
    for k0 in range(0, len(RHO), kchunk):
        zA = zA_all[k0:k0 + kchunk, None]; zB = zB_all[k0:k0 + kchunk, None]; rho = RHO[k0:k0 + kchunk]
        with np.errstate(divide="ignore", invalid="ignore"):
            t1 = zA / co[None, :]; t2 = zB / co[None, :]
        in_z = (zA <= 0) & (zB >= 0)                                  # a horizontal ray is in the slab or not at all
        zlo = np.where(horiz[None, :], np.where(in_z, -np.inf, np.inf), np.minimum(t1, t2))
        zhi = np.where(horiz[None, :], np.where(in_z, np.inf, -np.inf), np.maximum(t1, t2))
        w2 = rho[:, None] ** 2 - dxg[None, :] ** 2                   # (K, X): half chord of the cylinder at this dx
        okw = w2 > 0
        w = np.sqrt(np.maximum(w2, 0.0))
        ylo_y = (yt - w)[:, None, :]; yhi_y = (yt + w)[:, None, :]
        with np.errstate(divide="ignore", invalid="ignore"):
            u1 = ylo_y / si[None, :, None]; u2 = yhi_y / si[None, :, None]
        through = (ylo_y <= 0) & (yhi_y >= 0)
        ylo = np.where(vert, np.where(through, -np.inf, np.inf), np.minimum(u1, u2))
        yhi = np.where(vert, np.where(through, np.inf, -np.inf), np.maximum(u1, u2))
        lo = np.maximum(np.maximum(zlo[:, :, None], ylo), 0.0)
        hi = np.minimum(zhi[:, :, None], yhi)
        ok = (lo < hi) & okw[:, None, :]
        np.minimum(S1, np.where(ok, lo, np.inf).min(axis=0), out=S1)
        np.maximum(SH, np.where(ok, hi, -np.inf).max(axis=0), out=SH)
    return S1, SH


def _stamp_r_profile(r, P, K, I, tool, res_x, res_a, x0, rapid_hits, op_name):
    """Stamp a tool of any profile (tool_shape.cut_slabs: the cutting part as a stack of cylinders, each within 0.02 mm
    of the real radius) at any position: beside the axis (a gear cutter at Y = r + R, Z = 0) as well as above it, with
    rays near horizontal. Samples that share (y, z, A) (a pass along X) share one table of ray entries / exits over
    (angle, X offset); A is grouped to res_a / 10 (a helical pass makes more, smaller groups); X offsets are snapped
    to the sampling step. Only the cutting part cuts."""
    na, nx = r.shape
    sh = tool.shape
    H0, H1, RHO = sh.cut_slabs(0.02)
    if len(RHO) == 0:
        return
    rho_max = float(RHO.max())
    step = min(res_x / 2, 0.25)                                       # _samples' step: the dx grid of the tables
    aq = res_a / 10.0
    key = np.stack([np.round(P[:, 1], 4), np.round(P[:, 2], 4), np.round(P[:, 3] / aq)], axis=1)
    _, inv = np.unique(key, axis=0, return_inverse=True)
    inv = np.asarray(inv).ravel()
    x_last = x0 + (nx - 1) * res_x
    for g in range(int(inv.max()) + 1):
        idx = np.where(inv == g)[0]
        yt, zt = float(P[idx[0], 1]), float(P[idx[0], 2])
        a_rep = float(np.round(P[idx[0], 3] / aq) * aq)
        rmax_now = float(r.max())
        rho_t = math.hypot(yt, zt)
        gap = rho_t - rmax_now
        if gap >= rho_max:
            continue                                                  # the tool is clear of all the material
        x_reach = math.sqrt(max(rho_max ** 2 - max(gap, 0.0) ** 2, 0.0)) + step
        xs = P[idx, 0]
        keep = (xs + x_reach >= x0) & (xs - x_reach <= x_last)
        if not keep.any():
            continue
        idx = idx[keep]; xs = xs[keep]
        if rho_t <= rho_max + 1e-9:
            half = 180.0
        else:
            half = math.degrees(math.asin(min(1.0, rho_max / rho_t)))
            if abs(rho_t - rmax_now) < rho_max < rho_t + rmax_now:      # rays must meet the material where they meet the tool
                c = (rho_t ** 2 + rmax_now ** 2 - rho_max ** 2) / (2 * rho_t * rmax_now)
                half = min(half, math.degrees(math.acos(max(-1.0, min(1.0, c)))))
        ka = min(int(math.ceil(half / res_a)) + 1, na // 2)
        das = np.arange(-ka, ka + 1)
        psi_c = math.degrees(math.atan2(yt, zt)) + a_rep              # part angle of the ray through the tool axis
        rows_abs = int(np.rint(psi_c / res_a)) + das
        rows = rows_abs % na
        phi = np.radians(rows_abs * res_a - a_rep)                     # machine angle of each ray
        kx = int(math.ceil(x_reach / step)) + 1
        dxg = np.arange(-kx, kx + 1) * step
        S1, SH = _ray_table(H0, H1, RHO, yt, zt, np.sin(phi), np.cos(phi), dxg)
        if not np.isfinite(S1).any():
            continue
        kw = int(math.ceil(x_reach / res_x)) + 1
        ixc = np.floor((xs - x0) / res_x).astype(np.int64)
        cols = ixc[:, None] + np.arange(-kw, kw + 2)[None, :]         # (n, W)
        dx = (x0 + cols * res_x) - xs[:, None]
        dj = np.rint(dx / step).astype(np.int64) + kx
        okx = (cols >= 0) & (cols < nx) & (dj >= 0) & (dj < len(dxg))
        djc = np.clip(dj, 0, len(dxg) - 1)
        s1 = np.transpose(S1[:, djc], (1, 0, 2))                       # (n, A, W)
        shi = np.transpose(SH[:, djc], (1, 0, 2))
        valid = np.isfinite(s1) & okx[:, None, :]
        if not valid.any():
            continue
        IA = np.broadcast_to(rows[None, :, None], valid.shape)[valid]
        IX = np.broadcast_to(np.clip(cols, 0, nx - 1)[:, None, :], valid.shape)[valid]
        S1v = s1[valid]; SHv = shi[valid]
        cur = r[IA, IX]
        cut = (SHv >= cur - 1e-3) & (S1v < cur)
        rap = np.broadcast_to((K[idx] == cam.RAPID)[:, None, None], valid.shape)[valid]
        hit = cut & rap & (cur - S1v > 0.05)
        if hit.any() and len(rapid_hits) < 50:
            owner = np.broadcast_to(I[idx][:, None, None], valid.shape)[valid][hit]
            depth = (cur - S1v)[hit]
            j = int(np.argmax(depth))
            rapid_hits.append({"op": op_name, "move": int(owner[j]) + 1, "depth": round(float(depth[j]), 3)})
        if cut.any():
            np.minimum.at(r, (IA[cut], IX[cut]), S1v[cut])


# ----------------------------------------------------------------------------- machine collisions (holder / nose / head / table)
def tool_stickout(tool: cam.Tool) -> float:
    """How far the tool projects below the nose/nut face: the tool's own stickout, else its reach + 6 mm (estimated)."""
    return float(getattr(tool, "stickout", 0) or 0) or float(tool.shape.stickout)


def _collision_stats(hits: list[dict]) -> dict:
    if not hits:
        return {"count": 0, "max_depth": 0.0, "hits": []}
    # one entry per (op, part, what) keeps the worst depth; the list is sorted deepest first
    best: dict[tuple, dict] = {}
    for h in hits:
        k = (h["op"], h["part"], h["what"])
        if k not in best or h["depth"] > best[k]["depth"]:
            best[k] = h
    return {"count": len(hits), "max_depth": max(h["depth"] for h in hits), "hits": sorted(best.values(), key=lambda h: -h["depth"])[:50],
            "all": sorted(hits, key=lambda h: -h["depth"])[:400]}


def holder_levels(machine_model, tool: cam.Tool) -> list[tuple]:
    """(name, radius, bottom above the tool tip, height, box) for everything above the flutes: the tool shank, then the
    machine's nose profile (collet nut, nose, collar, head). box = (x0, x1, y0, y1) about the spindle axis for a level
    that is not round (an off-centre head), else None; radius is then its farthest corner."""
    stick = tool_stickout(tool)
    ov = getattr(machine_model, "stickout", None)
    if ov:
        stick = max(float(ov), float(tool.flute_length))
    sh = tool.shape
    fl = float(sh.flute_length)
    # the tool above its flutes, in bands of its real radius (the body over the flutes, a neck, a wider shank)
    levels = [("tool shank" if r >= sh.max_radius - 1e-6 else "tool neck", r, b, t - b, None)
              for b, t, r in sh.bands(fl) if b < stick]
    if not levels:
        levels = [("tool shank", tool.radius, fl, max(stick - fl, 0.0), None)]
    z = stick
    for lvl in machine_model.nose:
        bxs = lvl.get("boxes") or ([lvl["box"]] if lvl.get("box") else None)
        box = tuple(tuple(float(v) for v in b) for b in bxs) if bxs else None
        levels.append((lvl["name"], float(lvl["r"]), z, float(lvl["h"]), box))
        z += float(lvl["h"])
    return levels


def _level_mask(xx, yy, x, y, x0, y0, res, r, tool_r, box):
    """Cells (index grids xx, yy) under a holder level centred on the tool at (x, y): within r of the axis (outside the
    flutes' own radius), or inside its box about the axis."""
    dx = (xx - (x - x0) / res) * res; dy = (yy - (y - y0) / res) * res
    if box is not None:                                   # a tuple of boxes (x0, x1, y0, y1): their union
        m = np.zeros(dx.shape, dtype=bool)
        for b in box:
            m |= (dx >= b[0]) & (dx <= b[1]) & (dy >= b[2]) & (dy <= b[3])
        return m
    d2 = dx * dx + dy * dy
    return (d2 <= r * r) & (d2 > tool_r * tool_r)


def _pool_max(zhi: np.ndarray, c: int) -> np.ndarray:
    """Block-max of zhi over c×c cells (padded with -inf)."""
    ny, nx = zhi.shape
    py, px = (-ny) % c, (-nx) % c
    Z = np.pad(zhi, ((0, py), (0, px)), constant_values=-np.inf)
    return Z.reshape(Z.shape[0] // c, c, Z.shape[1] // c, c).max(axis=(1, 3))


def _collide_z(hits, zhi, P, I, done, tool, mm, res, x0, y0, op_name, stock, placement):
    """After a piece of moves is stamped: does any level of the holder stack dip below the remaining stock around it, or
    does the tool tip go below the table (through the spoilboard)? Sampled every r/2 along the path."""
    if len(P) == 0:
        return
    levels = holder_levels(mm, tool)
    ny, nx = zhi.shape
    base = stock.zmin - float(placement.get("fixture", 0.0))         # the plane the stock (or its fixture) stands on
    spoil = 0.0 if placement.get("replaces_spoilboard") else float(mm.spoilboard)
    table_z = base
    cut_r = tool.shape.cut_radius
    for name, r, zb_off, h, box in levels:
        if r <= cut_r + 1e-6 and name in ("tool shank", "tool neck"):
            continue                                                   # inside the flutes' own cut (the buried check covers it)
        c = max(1, int(r / (2 * res)))                                 # coarse cells ~ r/2
        coarse = _pool_max(zhi, c)
        step = max(1, int((r / 2) / (res / 2)))                        # sample spacing ≤ r/2 (samples are res/2 apart)
        rc = int(math.ceil(r / res))
        worst = None                                                   # the deepest hit of this level in this piece
        for k in range(0, len(P), step):
            x, y, z = P[k]
            zb = z + zb_off
            ix, iy = int(round((x - x0) / res)), int(round((y - y0) / res))
            cx0, cx1 = max(0, (ix - rc) // c), min(coarse.shape[1] - 1, (ix + rc) // c)
            cy0, cy1 = max(0, (iy - rc) // c), min(coarse.shape[0] - 1, (iy + rc) // c)
            if cx0 > cx1 or cy0 > cy1:
                continue
            if coarse[cy0:cy1 + 1, cx0:cx1 + 1].max() <= zb + 0.02:
                continue
            # exact: cells within r of the axis, above the level's bottom
            xa, xb = max(0, ix - rc), min(nx - 1, ix + rc); ya, yb = max(0, iy - rc), min(ny - 1, iy + rc)
            sub = zhi[ya:yb + 1, xa:xb + 1]
            yy, xx = np.mgrid[ya:yb + 1, xa:xb + 1]
            mask = _level_mask(xx, yy, x, y, x0, y0, res, r, cut_r, box) & (sub > zb + 0.02) & np.isfinite(sub)
            if mask.any():
                depth = float((sub[mask] - zb).max())
                if worst is None or depth > worst["depth"]:            # one hit per level per piece: the deepest (ramps enter gradually)
                    worst = {"op": op_name, "move": int(I[k]), "pos": [round(float(x), 2), round(float(y), 2), round(float(z), 2)],
                             "part": name, "what": "stock", "depth": round(depth, 2)}
        if worst is not None:
            hits.append(worst)
    # the tool tip through the spoilboard into the table
    zmin = float(P[:, 2].min())
    if zmin < table_z - spoil - 0.02:
        k = int(np.argmin(P[:, 2]))
        hits.append({"op": op_name, "move": int(I[k]), "pos": [round(float(P[k, 0]), 2), round(float(P[k, 1]), 2), round(zmin, 2)],
                     "part": "tool", "what": "table" if spoil <= 0 else "bed under the spoilboard", "depth": round(table_z - spoil - zmin, 2)})


# ----------------------------------------------------------------------------- fixtures (vise jaws, clamps, plates: uncuttable)
def _fixture_shapes(setups):
    """[(shape, fixture name)] of every fixture of the top setup (the holding one and any others on the table) in model
    coordinates; flipped and rotary setups have none."""
    st = setups[0]
    if not hasattr(st, "fixture_collision_shapes") or st.rotary or st.orient != "top" or not (st.fixture or st.fixtures):
        return []
    return st.fixture_collision_shapes()


def _fixture_map(named_shapes, stock_res):
    """Top-surface height of the fixtures on their own grid, with which fixture owns each cell:
    {"top": (ny, nx) heights (-inf = none), "owner": (ny, nx) index into "names", "names", "res", "x0", "y0"}."""
    from build123d import Compound
    names = []
    for _, n in named_shapes:
        if n not in names:
            names.append(n)
    bbs = [s.bounding_box() for s, _ in named_shapes]
    fx0, fx1 = min(b.min.X for b in bbs) - 1, max(b.max.X for b in bbs) + 1
    fy0, fy1 = min(b.min.Y for b in bbs) - 1, max(b.max.Y for b in bbs) + 1
    res = max(stock_res, 0.25, math.sqrt((fx1 - fx0) * (fy1 - fy0) / 600_000.0))     # ≤ ~600k cells
    nx = int(math.ceil((fx1 - fx0) / res)) + 1; ny = int(math.ceil((fy1 - fy0) / res)) + 1
    top = np.full((ny, nx), -np.inf); owner = np.zeros((ny, nx), dtype=np.int16)
    for k, name in enumerate(names):
        layer = np.full((ny, nx), -np.inf)
        mesh = _part_mesh(Compound([s for s, n in named_shapes if n == name]))
        for bd in mesh["bodies"]:
            P = np.array(bd["positions"], dtype=float).reshape(-1, 3)
            I = np.array(bd["indices"], dtype=np.int64).reshape(-1, 3)
            cam._raster_triangles(P, I, layer, fx0, fy0, res)
        win = layer > top
        top[win] = layer[win]; owner[win] = k
    return {"top": top, "owner": owner, "names": names, "res": res, "x0": fx0, "y0": fy0}


def _collide_fixture(hits, fix, P, K, I, tool, mm, sample_res, op_name):
    """The tool (flutes included) and the holder stack must stay clear of every fixture: any contact is a collision.
    `fix` is _fixture_map's grid; P are tool-tip samples sample_res / 2 apart."""
    if len(P) == 0:
        return
    fix_hi, res, x0, y0 = fix["top"], fix["res"], fix["x0"], fix["y0"]
    sh = tool.shape
    levels = [("tool", r, b, t - b, None) for b, t, r in sh.bands(0.0) if t <= sh.flute_length + 1e-6] or [("tool", float(sh.cut_radius), 0.0, float(sh.flute_length), None)]
    levels += holder_levels(mm, tool) if mm is not None else [("tool shank", r, b, t - b, None) for b, t, r in sh.bands(sh.flute_length)]
    ny, nx = fix_hi.shape
    for name, r, zb_off, h, box in levels:
        c = max(1, int(r / (2 * res)))
        coarse = _pool_max(fix_hi, c)
        step = max(1, int(r / sample_res))
        rc = int(math.ceil(r / res))
        worst = None                                                   # the deepest hit of this level in this piece
        for k in range(0, len(P), step):
            x, y, z = P[k]; zb = z + zb_off
            ix, iy = int(round((x - x0) / res)), int(round((y - y0) / res))
            cx0, cx1 = max(0, (ix - rc) // c), min(coarse.shape[1] - 1, (ix + rc) // c)
            cy0, cy1 = max(0, (iy - rc) // c), min(coarse.shape[0] - 1, (iy + rc) // c)
            if cx0 > cx1 or cy0 > cy1 or coarse[cy0:cy1 + 1, cx0:cx1 + 1].max() <= zb + 0.02:
                continue
            xa, xb = max(0, ix - rc), min(nx - 1, ix + rc); ya, yb = max(0, iy - rc), min(ny - 1, iy + rc)
            sub = fix_hi[ya:yb + 1, xa:xb + 1]
            yy, xx = np.mgrid[ya:yb + 1, xa:xb + 1]
            mask = _level_mask(xx, yy, x, y, x0, y0, res, r, 0.0, box) & (sub > zb + 0.02) & np.isfinite(sub)
            if mask.any():
                dd = np.where(mask, sub - zb, -np.inf); j = int(np.argmax(dd))
                depth = round(float(dd.flat[j]), 2)
                who = fix["names"][int(fix["owner"][ya:yb + 1, xa:xb + 1].flat[j])]
                if worst is None or depth > worst["depth"]:
                    worst = {"op": op_name, "move": int(I[k]), "pos": [round(float(x), 2), round(float(y), 2), round(float(z), 2)],
                             "part": name, "what": f"fixture ({who})", "depth": depth,
                             "rapid": bool(K[k] == 0) if hasattr(K, "__len__") else False}
        if worst is not None:
            hits.append(worst)
