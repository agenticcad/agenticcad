"""
Shop drawings: third-angle orthographic views (front / top / right) + isometric, with hidden lines,
automatic overall dimensions, hole callouts and a hole table, title block. SVG output (and DXF of
the view geometry). Built from the exact B-rep via OCCT hidden-line removal.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import build123d as b3d
from build123d import Shape, Vector
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.GCPnts import GCPnts_TangentialDeflection

import cad_kernel as ck

SHEETS = {"A4": (297.0, 210.0), "A3": (420.0, 297.0)}
SCALES = [10, 5, 4, 2, 1, 0.5, 0.25, 0.2, 0.1, 0.05]
FONT = "Helvetica, Arial, sans-serif"

# view name -> (direction from which we look, up vector, which world axis is the view direction)
VIEWS = {
    "front": ((0, -1, 0), (0, 0, 1)),
    "top": ((0, 0, 1), (0, 1, 0)),
    "right": ((1, 0, 0), (0, 0, 1)),
    "iso": ((1, -1, 1), (0, 0, 1)),
}


def _edge_pts(edge, tol=0.02):
    c = BRepAdaptor_Curve(edge.wrapped)
    d = GCPnts_TangentialDeflection(c, 0.1, tol, 2)
    return [(d.Value(i).X(), d.Value(i).Y()) for i in range(1, d.NbPoints() + 1)]


@dataclass
class View:
    name: str
    visible: list[list[tuple[float, float]]]
    hidden: list[list[tuple[float, float]]]
    xmin: float; xmax: float; ymin: float; ymax: float
    holes: list[dict[str, Any]]          # circles seen face-on: {x, y, d, through}

    @property
    def w(self): return self.xmax - self.xmin
    @property
    def h(self): return self.ymax - self.ymin


def project(shape: Shape, name: str) -> View:
    direction, up = VIEWS[name]
    bb = shape.bounding_box()
    c = bb.center()
    diag = max(bb.size.X, bb.size.Y, bb.size.Z, 1.0) * 5
    origin = (c.X + direction[0] * diag, c.Y + direction[1] * diag, c.Z + direction[2] * diag)
    vis, hid = shape.project_to_viewport(origin, viewport_up=up, look_at=(c.X, c.Y, c.Z))
    vpl = [_edge_pts(e) for e in vis]
    hpl = [_edge_pts(e) for e in hid]
    xs = [p[0] for pl in vpl + hpl for p in pl]; ys = [p[1] for pl in vpl + hpl for p in pl]
    if not xs:
        xs, ys = [0.0], [0.0]
    v = View(name, vpl, hpl, min(xs), max(xs), min(ys), max(ys), [])
    # holes seen face-on: cylindrical concave faces whose axis is parallel to the view direction
    if name != "iso":
        v.holes = _holes_in_view(shape, name, v)
    return v


def _holes_in_view(shape: Shape, name: str, v: View) -> list[dict[str, Any]]:
    direction = Vector(*VIEWS[name][0]).normalized()
    up = Vector(*VIEWS[name][1]).normalized()
    right = direction.cross(up) * -1.0            # screen-right in world coords (matches OCCT HLR projector)
    bb = shape.bounding_box(); c = bb.center()
    found: dict[tuple, dict] = {}
    for face in shape.faces().filter_by(b3d.GeomType.CYLINDER):
        try:
            cyl = BRepAdaptor_Surface(face.wrapped).Cylinder()
        except Exception:
            continue
        ax = Vector(cyl.Axis().Direction().X(), cyl.Axis().Direction().Y(), cyl.Axis().Direction().Z())
        if abs(abs(ax.dot(direction)) - 1) > 1e-6:
            continue
        loc = cyl.Axis().Location()
        fc = face.center(); n = face.normal_at(fc)
        radial = Vector(fc.X - loc.X(), fc.Y - loc.Y(), fc.Z - loc.Z()) - ax * (Vector(fc.X - loc.X(), fc.Y - loc.Y(), fc.Z - loc.Z()).dot(ax))
        if radial.length < 1e-9 or radial.dot(n) > 0:
            continue                                  # convex -> boss, not a hole
        p = Vector(loc.X(), loc.Y(), loc.Z()) - c
        sx, sy = p.dot(right), p.dot(up)
        key = (round(sx, 3), round(sy, 3), round(cyl.Radius(), 3))
        fb = face.bounding_box()
        lo = min(fb.min.dot(ax), fb.max.dot(ax)); hi = max(fb.min.dot(ax), fb.max.dot(ax))
        if key not in found:
            found[key] = {"x": sx, "y": sy, "d": 2 * cyl.Radius(), "lo": lo, "hi": hi, "axis": ax, "loc": Vector(loc.X(), loc.Y(), loc.Z()), "area": float(face.area)}
        else:
            found[key]["lo"] = min(found[key]["lo"], lo); found[key]["hi"] = max(found[key]["hi"], hi); found[key]["area"] += float(face.area)
    out = []
    for h in found.values():
        # only full-round holes: the cylindrical faces must cover (nearly) the whole circumference (fillets are partial)
        full = 2 * math.pi * (h["d"] / 2) * max(h["hi"] - h["lo"], 1e-9)
        if h["area"] < 0.85 * full:
            continue
        del h["area"]
        ax = h["axis"]; base = h["loc"] - ax * h["loc"].dot(ax)      # point on the axis at parameter 0
        above = base + ax * (h["hi"] + 0.05); below = base + ax * (h["lo"] - 0.05)
        try:
            through = not _inside(shape, above) and not _inside(shape, below)
        except Exception:
            through = False
        h["depth"] = round(h["hi"] - h["lo"], 3)
        h["through"] = through
        del h["axis"], h["loc"], h["lo"], h["hi"]
        out.append(h)
    return sorted(out, key=lambda h: (h["d"], h["x"], h["y"]))


def _inside(shape: Shape, p: Vector) -> bool:
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.gp import gp_Pnt
    from OCP.TopAbs import TopAbs_IN
    cls = BRepClass3d_SolidClassifier(shape.wrapped, gp_Pnt(p.X, p.Y, p.Z), 1e-6)
    return cls.State() == TopAbs_IN


def _pick_scale(w: float, h: float, sheet_w: float, sheet_h: float) -> float:
    for sc in SCALES:
        if w * sc <= sheet_w and h * sc <= sheet_h:
            return sc
    return SCALES[-1]


THREADS: list = []     # threads.ThreadSpec of the model being drawn (set by drawings_for_model)


def _thread_label_for(shape: Shape, name: str, h: dict[str, Any]) -> str | None:
    """If a registered tapped hole lies on this projected hole's axis, return its callout."""
    direction = Vector(*VIEWS[name][0]).normalized(); up = Vector(*VIEWS[name][1]).normalized()
    right = direction.cross(up) * -1.0
    c = shape.bounding_box().center()
    for t in THREADS:
        if abs(abs(Vector(*t.axis).dot(direction)) - 1) > 1e-3:
            continue
        p = Vector(*t.at) - c
        if abs(p.dot(right) - h["x"]) < 0.05 and abs(p.dot(up) - h["y"]) < 0.05:
            return t.label()
    return None


def make_drawing(shape: Shape, title: str, out_path: Path, body_name: str = "", material: str = "",
                 density: float | None = None, sheet: str = "A4", notes: str = "",
                 dxf_path: Path | None = None) -> dict[str, Any]:
    sw, sh = SHEETS.get(sheet, SHEETS["A4"])
    margin, gap, tb_h = 12.0, 14.0, 24.0
    views = {n: project(shape, n) for n in ("front", "top", "right", "iso")}
    f, t, r, iso = views["front"], views["top"], views["right"], views["iso"]
    # third-angle layout in model units: front at origin, top above, right to the right, iso top-right
    dim_space = 16.0
    layout_w = f.w + gap + r.w + gap + iso.w * 0.75 + dim_space
    layout_h = f.h + gap + t.h + dim_space
    avail_w, avail_h = sw - 2 * margin - 34, sh - 2 * margin - tb_h - 30
    # choose the scale in model->paper terms (views scaled, dimensions/text not)
    scale = _pick_scale(layout_w, layout_h, avail_w, avail_h)
    S = scale
    # paper positions (mm), y down
    ox = margin + 24 + dim_space
    base_y = sh - margin - tb_h - 6 - dim_space          # bottom of the front view on paper
    pos = {}
    pos["front"] = (ox, base_y)
    pos["top"] = (ox, base_y - f.h * S - gap - 0)          # bottom of top view
    pos["right"] = (ox + f.w * S + gap, base_y)
    iso_x = ox + f.w * S + gap + r.w * S + gap
    pos["iso"] = (iso_x, base_y - f.h * S - gap)
    svg: list[str] = []
    W = lambda v: f"{v:.3f}"

    def emit_view(v: View, x0: float, ybot: float, s: float):
        def tx(p): return x0 + (p[0] - v.xmin) * s
        def ty(p): return ybot - (p[1] - v.ymin) * s
        for pl in v.hidden:
            svg.append('<polyline fill="none" stroke="#666" stroke-width="0.18" stroke-dasharray="1.6,0.8" points="'
                       + " ".join(f"{W(tx(p))},{W(ty(p))}" for p in pl) + '"/>')
        for pl in v.visible:
            svg.append('<polyline fill="none" stroke="#000" stroke-width="0.35" stroke-linejoin="round" points="'
                       + " ".join(f"{W(tx(p))},{W(ty(p))}" for p in pl) + '"/>')
        return tx, ty

    def dim_h(x1, x2, y, text, above=False):
        """Horizontal dimension between paper x1..x2 at paper y."""
        svg.append(f'<line x1="{W(x1)}" y1="{W(y)}" x2="{W(x2)}" y2="{W(y)}" stroke="#000" stroke-width="0.18"/>')
        for x, d in ((x1, 1), (x2, -1)):
            svg.append(f'<path d="M{W(x)},{W(y)} l{W(2.2*d)},-0.8 l0,1.6 z" fill="#000"/>')
        svg.append(f'<text x="{W((x1+x2)/2)}" y="{W(y - 1.2 if not above else y - 1.2)}" font-size="3.2" font-family="{FONT}" text-anchor="middle">{text}</text>')

    def dim_v(y1, y2, x, text):
        svg.append(f'<line x1="{W(x)}" y1="{W(y1)}" x2="{W(x)}" y2="{W(y2)}" stroke="#000" stroke-width="0.18"/>')
        for y, d in ((y1, 1), (y2, -1)):
            svg.append(f'<path d="M{W(x)},{W(y)} l-0.8,{W(2.2*d)} l1.6,0 z" fill="#000"/>')
        svg.append(f'<text x="{W(x - 1.2)}" y="{W((y1+y2)/2)}" font-size="3.2" font-family="{FONT}" text-anchor="middle" transform="rotate(-90 {W(x-1.2)} {W((y1+y2)/2)})">{text}</text>')

    def ext_line(x1, y1, x2, y2):
        svg.append(f'<line x1="{W(x1)}" y1="{W(y1)}" x2="{W(x2)}" y2="{W(y2)}" stroke="#000" stroke-width="0.13"/>')

    hole_rows: list[dict[str, Any]] = []
    label_i = 0
    for name in ("front", "top", "right", "iso"):
        v = views[name]
        x0, ybot = pos[name]
        s = S * (0.75 if name == "iso" else 1.0)
        tx, ty = emit_view(v, x0, ybot, s)
        # view label
        svg.append(f'<text x="{W(x0)}" y="{W(ybot + 4.5)}" font-size="3" font-family="{FONT}" fill="#333">{name.upper()}' + (f" (1:{1/S:g})" if name == "iso" else "") + '</text>')
        if name == "iso":
            continue
        # overall dimensions (width below, height left)
        xl, xr = tx((v.xmin, 0)), tx((v.xmax, 0)); yt, yb = ty((0, v.ymax)), ty((0, v.ymin))
        dy = yb + 9
        ext_line(xl, yb + 1, xl, dy + 1); ext_line(xr, yb + 1, xr, dy + 1)
        dim_h(xl, xr, dy, f"{v.w:.2f}".rstrip("0").rstrip("."))
        dx = xl - 9
        ext_line(xl - 1, yt, dx - 1, yt); ext_line(xl - 1, yb, dx - 1, yb)
        dim_v(yt, yb, dx, f"{v.h:.2f}".rstrip("0").rstrip("."))
        # holes: label + callout per diameter group
        groups: dict[float, list[dict]] = {}
        for h in v.holes:
            groups.setdefault(round(h["d"], 3), []).append(h)
        k = 0
        for d, hs in sorted(groups.items()):
            for h in hs:
                cx, cy = tx((h["x"] - (v.xmin + v.xmax) / 2 + (v.xmin + v.xmax) / 2 - _view_center_x(v), 0)), 0
            # position holes: projected coordinates are relative to the bbox centre of the shape;
            # the view polylines are in the same frame, so convert with the view centre
            for h in hs:
                px = tx((h["x"], 0)); py = ty((0, h["y"]))
                label = chr(ord("A") + (label_i % 26))
                label_i += 1
                k += 1
                svg.append(f'<circle cx="{W(px)}" cy="{W(py)}" r="0.5" fill="#c00"/>')
                svg.append(f'<text x="{W(px + h["d"] * s / 2 + 0.8)}" y="{W(py - 0.8)}" font-size="2.6" font-family="{FONT}" fill="#c00">{label}</text>')
                hole_rows.append({"label": label, "view": name, "x": h["x"] - v.xmin, "y": h["y"] - v.ymin, "d": h["d"],
                                  "through": h["through"], "depth": h["depth"], "thread": _thread_label_for(shape, name, h)})
            # one callout per group: leader from the first hole to the upper-right of the view
            h0 = hs[0]
            px = tx((h0["x"], 0)); py = ty((0, h0["y"]))
            lx, ly = tx((v.xmax, 0)) + 4, ty((0, v.ymax)) - 3 - 4.5 * (list(sorted(groups)).index(d))
            svg.append(f'<line x1="{W(px + h0["d"] * s / 2 * 0.7)}" y1="{W(py - h0["d"] * s / 2 * 0.7)}" x2="{W(lx - 1)}" y2="{W(ly)}" stroke="#000" stroke-width="0.18"/>')
            tl = _thread_label_for(shape, name, h0)
            callout = (f"{len(hs)}× {tl}" if tl else f"{len(hs)}× Ø{d:g}" + (" THRU" if all(x["through"] for x in hs) else f" ↧{hs[0]['depth']:g}"))
            svg.append(f'<text x="{W(lx)}" y="{W(ly + 1)}" font-size="3" font-family="{FONT}">{_esc(callout)}</text>')
    # hole table (top-left), only for the top view rows if present else all
    rows = [r_ for r_ in hole_rows if r_["view"] == "top"] or hole_rows
    if rows:
        tx0, ty0 = margin, margin + 4
        svg.append(f'<text x="{W(tx0)}" y="{W(ty0)}" font-size="3.2" font-family="{FONT}" font-weight="bold">HOLE TABLE ({rows[0]["view"].upper()} view, from lower-left corner)</text>')
        hdr = ["#", "X", "Y", "Ø", "depth"]
        colx = [tx0, tx0 + 8, tx0 + 26, tx0 + 44, tx0 + 58]
        yy = ty0 + 5
        for cx_, h_ in zip(colx, hdr):
            svg.append(f'<text x="{W(cx_)}" y="{W(yy)}" font-size="2.8" font-family="{FONT}" font-weight="bold">{h_}</text>')
        for r_ in rows[:24]:
            yy += 4
            vals = [r_["label"], f"{r_['x']:.2f}", f"{r_['y']:.2f}", (r_["thread"].split(" ")[0] if r_.get("thread") else f"Ø{r_['d']:g}"), "THRU" if r_["through"] else f"{r_['depth']:g}"]
            for cx_, val in zip(colx, vals):
                svg.append(f'<text x="{W(cx_)}" y="{W(yy)}" font-size="2.8" font-family="{FONT}">{val}</text>')
    # title block
    bb = shape.bounding_box()
    vol = float(shape.volume)
    mass = f"{vol / 1000 * density:.1f} g" if density else "—"
    tb_y = sh - margin - tb_h
    svg.append(f'<rect x="{W(margin)}" y="{W(tb_y)}" width="{W(sw - 2*margin)}" height="{W(tb_h)}" fill="none" stroke="#000" stroke-width="0.35"/>')
    cells = [("TITLE", title), ("PART", body_name or "—"), ("MATERIAL", material or "—"), ("MASS", mass),
             ("SIZE (X×Y×Z)", f"{bb.size.X:.2f} × {bb.size.Y:.2f} × {bb.size.Z:.2f} mm"),
             ("SCALE", f"1:{1/S:g}" if S <= 1 else f"{S:g}:1"), ("UNITS", "mm · third angle"), ("DATE", time.strftime("%Y-%m-%d")),
             ("NOTES", notes or "break sharp edges · unspecified tolerances ±0.1")]
    cw = (sw - 2 * margin) / 5
    for i, (k_, v_) in enumerate(cells[:10]):
        cx_ = margin + (i % 5) * cw + 2; cy_ = tb_y + (5 if i < 5 else 15)
        svg.append(f'<text x="{W(cx_)}" y="{W(cy_)}" font-size="2.2" font-family="{FONT}" fill="#555">{k_}</text>')
        svg.append(f'<text x="{W(cx_)}" y="{W(cy_ + 4.5)}" font-size="3.4" font-family="{FONT}" font-weight="bold">{_esc(str(v_))}</text>')
    svg.append(f'<rect x="{W(margin)}" y="{W(margin)}" width="{W(sw - 2*margin)}" height="{W(sh - 2*margin)}" fill="none" stroke="#000" stroke-width="0.5"/>')
    doc = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{sw}mm" height="{sh}mm" viewBox="0 0 {sw} {sh}">'
           f'<rect width="{sw}" height="{sh}" fill="#fff"/>' + "".join(svg) + "</svg>")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(doc)
    info = {"svg": str(out_path), "scale": S, "views": {n: {"w": round(v.w, 3), "h": round(v.h, 3), "holes": len(v.holes)} for n, v in views.items()},
            "holes": hole_rows}
    if dxf_path is not None:
        try:
            _export_dxf(views, pos, S, dxf_path)
            info["dxf"] = str(dxf_path)
        except Exception as e:  # noqa: BLE001
            info["dxf_error"] = str(e)
    return info


def _view_center_x(v: View) -> float:
    return (v.xmin + v.xmax) / 2


def _esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _export_dxf(views: dict[str, View], pos: dict, S: float, path: Path) -> None:
    """DXF of the view geometry laid out like the sheet (1:1 model units, views offset)."""
    from build123d import ExportDXF, LineType, Polyline
    ex = ExportDXF(unit=b3d.Unit.MM)
    ex.add_layer("VISIBLE")
    ex.add_layer("HIDDEN", line_type=LineType.DASHED)
    for name, v in views.items():
        if name == "iso":
            continue
        x0, ybot = pos[name]
        offx, offy = x0 / S - v.xmin, -ybot / S - v.ymin      # keep model scale; mirror paper y
        for pl in v.visible:
            if len(pl) >= 2:
                ex.add_shape(Polyline(*[(p[0] + offx, p[1] + offy) for p in pl]), layer="VISIBLE")
        for pl in v.hidden:
            if len(pl) >= 2:
                ex.add_shape(Polyline(*[(p[0] + offx, p[1] + offy) for p in pl]), layer="HIDDEN")
    ex.write(str(path))


def drawings_for_model(model: ck.Model, out_dir: Path, design_name: str, material: str = "",
                       density: float | None = None, sheet: str = "A4", notes: str = "") -> list[dict[str, Any]]:
    """One sheet per body (plus an assembly sheet when there are several)."""
    global THREADS
    THREADS = list(getattr(model, "threads", []))
    out = []
    targets = [(b.path, b.shape) for b in model.bodies]
    if len(model.bodies) > 1:
        targets.append(("assembly", model.shape))
    for label, shape in targets:
        fname = f"{design_name}_{label.replace('/', '-').replace(' ', '_')}"
        info = make_drawing(shape, design_name, out_dir / f"{fname}.svg", body_name=label, material=material,
                            density=density, sheet=sheet, notes=notes, dxf_path=out_dir / f"{fname}.dxf")
        info["body"] = label
        out.append(info)
    return out
