"""Bolt Pattern: holes on a pitch circle, cut into the face you pick.

Built-in example extension: a face picker plus numbers, a live summary of the hole positions, and Apply, which
edits the body's expression in the script (so the agent sees an ordinary pattern_circular call).
"""
import math


def _basis(n):
    """Two unit vectors in the plane with normal n."""
    nx, ny, nz = n
    a = (1.0, 0.0, 0.0) if abs(nx) < 0.9 else (0.0, 1.0, 0.0)
    u = (ny * a[2] - nz * a[1], nz * a[0] - nx * a[2], nx * a[1] - ny * a[0])
    L = math.sqrt(sum(c * c for c in u)) or 1.0
    u = tuple(c / L for c in u)
    v = (ny * u[2] - nz * u[1], nz * u[0] - nx * u[2], nx * u[1] - ny * u[0])
    return u, v


def _centres(face, pcd, count, start):
    u, v = _basis(face["normal"])
    c, r = face["center"], pcd / 2
    out = []
    for k in range(count):
        a = math.radians(start + 360 * k / count)
        out.append(tuple(round(c[i] + r * (math.cos(a) * u[i] + math.sin(a) * v[i]), 3) for i in range(3)))
    return out


@panel("Bolt pattern", icon="✣", description="Holes on a pitch circle, cut into a flat face you pick")
def bolt_pattern(ctx):
    s = ctx.state
    face = s.get("face")
    pcd, count, d, start = float(s.get("pcd", 40)), int(s.get("count", 4)), float(s.get("d", 3.4)), float(s.get("start", 0))
    depth, through = float(s.get("depth", 10)), bool(s.get("through", True))
    kids = [ui.picker("face", "Face", what="face", hint="a flat face; the pattern is centred on it"),
            ui.row(ui.number("pcd", "PCD", pcd, min=0.1, unit="mm"), ui.number("count", "Holes", count, min=1, max=64, step=1)),
            ui.row(ui.number("d", "Ø", d, min=0.1, unit="mm"), ui.number("start", "Start angle", start, step=15, unit="°")),
            ui.row(ui.checkbox("through", "through", through), ui.number("depth", "Depth", depth, min=0.1, unit="mm"))]
    if face and face.get("normal"):
        if ctx.face(face["id"]).kind != "PLANE":
            kids.append(ui.badge("pick a flat face", "warn"))
        else:
            cs = _centres(face, pcd, count, start)
            kids.append(ui.text(f"{count} × Ø{d:g} on PCD {pcd:g} in {face['body']}, centred on {face['label']}", muted=True))
            kids.append(ui.kv([[f"hole {i + 1}", f"({', '.join(f'{v:g}' for v in c)})"] for i, c in enumerate(cs[:8])] +
                              ([["…", f"{count - 8} more"]] if count > 8 else [])))
            kids.append(ui.row(ui.button("Apply", call="apply_bolt_pattern", primary=True),
                               ui.button("Tapped instead…", chat=f"Put {count} tapped holes for M{d:g} screws on a {pcd:g} mm PCD "
                                         f"centred on face #{face['id']} of {face['body']}, start angle {start:g}°")))
    else:
        kids.append(ui.text("Pick a face to see the positions.", muted=True))
    return ui.panel(*kids)


@tool("apply_bolt_pattern", "Cut the bolt pattern into the picked face's body", agent=False)
def apply_bolt_pattern(ctx, face=None, pcd: float = 40, count: int = 4, d: float = 3.4, start: float = 0,
                       depth: float = 10, through: bool = True, **_):
    if not face:
        return {"error": "pick a face first"}
    fi = ctx.face(face["id"])
    if fi.kind != "PLANE":
        return {"error": "the bolt pattern needs a flat face"}
    body = ctx.body(face["body"])
    n = fi.normal
    c = fi.center
    u, v = _basis(n)
    r = pcd / 2
    # first hole along u; a long cylinder through the whole body when `through`, else a blind hole from the face
    ext = [body.bbox_max[i] - body.bbox_min[i] for i in range(3)]
    length = (max(ext) * 2 + 2) if through else float(depth)
    p0 = tuple(c[i] + r * u[i] for i in range(3))
    origin = tuple(p0[i] - n[i] * length / 2 for i in range(3)) if through else tuple(p0[i] - n[i] * length for i in range(3))
    f = lambda t: "(" + ", ".join(f"{x:g}" for x in t) + ")"
    hole = f"Plane(origin={f(origin)}, z_dir={f(n)}) * Cylinder({d / 2:g}, {length:g}, align=(Align.CENTER, Align.CENTER, Align.MIN))"
    expr = f"{{body}} - pattern_circular({hole}, count={int(count)}, angle=360, axis=({f(c)}, {f(n)}))"
    if start:
        expr = expr.replace(f"origin={f(origin)}", f"origin={f(_rot(origin, c, n, start))}")
    return {"wrap": {"body": body.path, "template": expr},
            "notify": f"bolt pattern: {int(count)} × Ø{d:g} on PCD {pcd:g} cut into {body.path}"}


def _rot(p, c, n, deg):
    """Rotate point p about the axis (c, n) by deg."""
    a = math.radians(deg)
    rel = [p[i] - c[i] for i in range(3)]
    cross = (n[1] * rel[2] - n[2] * rel[1], n[2] * rel[0] - n[0] * rel[2], n[0] * rel[1] - n[1] * rel[0])
    dot = sum(n[i] * rel[i] for i in range(3))
    return tuple(c[i] + rel[i] * math.cos(a) + cross[i] * math.sin(a) + n[i] * dot * (1 - math.cos(a)) for i in range(3))
