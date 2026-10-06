"""2D sketch constraint solver (the same equations as the sketch editor's JavaScript solver).

Geometry comes from the sketch items (rect, circle, slot, polygon, profile); their numbers are the variables.
References into the geometry:
    point  {"i": item, "p": key}   rect "c" "p0".."p3" · circle "c" · slot "a" "b" · polygon "p0".. · profile "s" (start),
                                   "e0".. (end of segment k), "v0".. (an arc's through point)
    edge   {"i": item, "e": k}     rect side k (0 bottom, 1 right, 2 top, 3 left) · polygon side k · profile segment k
                                   (a line, or an arc when it has a through point) · slot 0 (centre line)
    circle {"i": item}             a circle (or {"i", "e": k} for a profile arc)
Constraints: {"type": t, "refs": [...], "value": v?}
    coincident [pt, pt] · on [pt, edge|circle] · horizontal / vertical [edge] or [pt, pt] · parallel / perpendicular
    [edge, edge] · tangent [edge|circle, edge|circle] · equal [edge, edge] or [circle, circle] · concentric
    [circle, circle] · midpoint [pt, edge] · fix [pt] (value [x, y])
Dimensions (driving): length [edge] · distance [pt, pt] or [pt, edge] · hdistance / vdistance [pt, pt] · radius /
    diameter [circle] · angle [edge, edge] (degrees)
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

TOL = 1e-7


class SketchConstraintError(ValueError):
    pass


# ---------------------------------------------------------------- variables <-> items
def _slots(items: list[dict]) -> list[tuple[int, Any]]:
    """[(item index, accessor)] for every variable, in a stable order. An accessor is ('k', key) or ('pt', path, axis)."""
    out = []
    for i, it in enumerate(items):
        t = it.get("type")
        if t == "rect":
            out += [(i, ("k", k)) for k in ("cx", "cy", "w", "h")]
        elif t == "circle":
            out += [(i, ("k", k)) for k in ("cx", "cy", "r")]
        elif t == "slot":
            out += [(i, ("k", k)) for k in ("x1", "y1", "x2", "y2", "w")]
        elif t == "polygon":
            for j in range(len(it["pts"])):
                out += [(i, ("pt", ("pts", j), 0)), (i, ("pt", ("pts", j), 1))]
        elif t == "profile":
            out += [(i, ("pt", ("start",), 0)), (i, ("pt", ("start",), 1))]
            n = len(it["segs"])
            for j, sg in enumerate(it["segs"]):
                if not (j == n - 1 and _closes(it)):
                    out += [(i, ("pt", ("to", j), 0)), (i, ("pt", ("to", j), 1))]
                if sg.get("via"):
                    out += [(i, ("pt", ("via", j), 0)), (i, ("pt", ("via", j), 1))]
    return out


def _closes(it) -> bool:
    s, last = it["start"], it["segs"][-1]["to"]
    return abs(s[0] - last[0]) < 1e-9 and abs(s[1] - last[1]) < 1e-9


def _get(items, slot):
    i, acc = slot
    it = items[i]
    if acc[0] == "k":
        return float(it[acc[1]])
    path, ax = acc[1], acc[2]
    if path[0] == "pts":
        return float(it["pts"][path[1]][ax])
    if path[0] == "start":
        return float(it["start"][ax])
    return float(it["segs"][path[1]][path[0]][ax])


def _set(items, slot, v):
    i, acc = slot
    it = items[i]
    if acc[0] == "k":
        it[acc[1]] = v
        return
    path, ax = acc[1], acc[2]
    if path[0] == "pts":
        it["pts"][path[1]][ax] = v
    elif path[0] == "start":
        it["start"][ax] = v
        if it["segs"] and _closes_after_start_change(it, ax):
            it["segs"][-1]["to"][ax] = v
    else:
        it["segs"][path[1]][path[0]][ax] = v


def _closes_after_start_change(it, ax) -> bool:
    return it.get("_closed", False)


# ---------------------------------------------------------------- geometry from items
def point(items, ref) -> np.ndarray:
    it = items[ref["i"]]
    t, k = it["type"], ref["p"]
    if t == "rect":
        if k == "c":
            return np.array([it["cx"], it["cy"]], float)
        sx, sy = {"p0": (-1, -1), "p1": (1, -1), "p2": (1, 1), "p3": (-1, 1)}[k]
        return np.array([it["cx"] + sx * it["w"] / 2, it["cy"] + sy * it["h"] / 2], float)
    if t == "circle":
        return np.array([it["cx"], it["cy"]], float)
    if t == "slot":
        return np.array([it["x1"], it["y1"]] if k == "a" else [it["x2"], it["y2"]], float)
    if t == "polygon":
        return np.array(it["pts"][int(k[1:])], float)
    if t == "profile":
        if k == "s":
            return np.array(it["start"], float)
        j = int(k[1:])
        return np.array(it["segs"][j]["via" if k[0] == "v" else "to"], float)
    raise SketchConstraintError(f"no point {k!r} on a {t}")


def edge(items, ref) -> tuple[np.ndarray, np.ndarray, np.ndarray | None]:
    """(a, b, via): a line from a to b, or a three-point arc when via is set."""
    it = items[ref["i"]]
    t, k = it["type"], int(ref["e"])
    if t == "rect":
        names = ["p0", "p1", "p2", "p3"]
        return point(items, {"i": ref["i"], "p": names[k]}), point(items, {"i": ref["i"], "p": names[(k + 1) % 4]}), None
    if t == "polygon":
        n = len(it["pts"])
        return np.array(it["pts"][k], float), np.array(it["pts"][(k + 1) % n], float), None
    if t == "slot":
        return np.array([it["x1"], it["y1"]], float), np.array([it["x2"], it["y2"]], float), None
    if t == "profile":
        a = np.array(it["start"] if k == 0 else it["segs"][k - 1]["to"], float)
        sg = it["segs"][k]
        return a, np.array(sg["to"], float), (np.array(sg["via"], float) if sg.get("via") else None)
    raise SketchConstraintError(f"a {t} has no edges")


def circumcircle(a, m, b):
    d = 2 * (a[0] * (m[1] - b[1]) + m[0] * (b[1] - a[1]) + b[0] * (a[1] - m[1]))
    if abs(d) < 1e-12:
        return None, None
    s1, s2, s3 = a @ a, m @ m, b @ b
    c = np.array([(s1 * (m[1] - b[1]) + s2 * (b[1] - a[1]) + s3 * (a[1] - m[1])) / d,
                  (s1 * (b[0] - m[0]) + s2 * (a[0] - b[0]) + s3 * (m[0] - a[0])) / d])
    return c, float(np.linalg.norm(a - c))


def circle(items, ref) -> tuple[np.ndarray, float]:
    it = items[ref["i"]]
    if "e" in ref:
        a, b, via = edge(items, ref)
        if via is None:
            raise SketchConstraintError("that profile segment is a line, not an arc")
        c, r = circumcircle(a, via, b)
        if c is None:
            return (a + b) / 2, 1e9
        return c, r
    if it["type"] == "circle":
        return np.array([it["cx"], it["cy"]], float), float(it["r"])
    if it["type"] == "slot":                                  # the slot's end radius, centred on its first end
        return np.array([it["x1"], it["y1"]], float), float(it["w"]) / 2
    raise SketchConstraintError(f"a {it['type']} is not a circle")


def _kind(items, ref) -> str:
    if "p" in ref:
        return "pt"
    it = items[ref["i"]]
    if "e" in ref:
        if it["type"] == "profile" and it["segs"][int(ref["e"])].get("via"):
            return "arc"
        return "line"
    return "circle"


def _shared_point(items, R):
    """The joint between two adjacent segments of the same profile, if the refs are such a pair."""
    if len(R) != 2 or any("e" not in r for r in R) or R[0]["i"] != R[1]["i"]:
        return None
    it = items[R[0]["i"]]
    if it.get("type") != "profile":
        return None
    n, a, b = len(it["segs"]), int(R[0]["e"]), int(R[1]["e"])
    lo, hi = min(a, b), max(a, b)
    if hi - lo == 1:
        return np.array(it["segs"][lo]["to"], float), (lo, hi)
    if lo == 0 and hi == n - 1:
        return np.array(it["start"], float), (hi, lo)
    return None


def _unit(v):
    n = np.linalg.norm(v)
    return v / n if n > 1e-12 else v


def residuals(items, c) -> list[float]:
    t, R = c["type"], c["refs"]
    K = [_kind(items, r) for r in R]
    if t == "coincident":
        return list(point(items, R[0]) - point(items, R[1]))
    if t == "fix":
        return list(point(items, R[0]) - np.array(c["value"], float))
    if t in ("horizontal", "vertical"):
        if K[0] == "pt":
            a, b = point(items, R[0]), point(items, R[1])
        else:
            a, b, _ = edge(items, R[0])
        return [b[1] - a[1]] if t == "horizontal" else [b[0] - a[0]]
    if t in ("parallel", "perpendicular"):
        a1, b1, _ = edge(items, R[0]); a2, b2, _ = edge(items, R[1])
        d1, d2 = _unit(b1 - a1), _unit(b2 - a2)
        return [d1[0] * d2[1] - d1[1] * d2[0]] if t == "parallel" else [d1 @ d2]
    if t == "on":
        p = point(items, R[0])
        if K[1] == "line":
            a, b, _ = edge(items, R[1]); d = _unit(b - a)
            return [d[0] * (p - a)[1] - d[1] * (p - a)[0]]
        cc, r = circle(items, R[1])
        return [float(np.linalg.norm(p - cc)) - r]
    if t == "midpoint":
        a, b, _ = edge(items, R[1])
        return list(point(items, R[0]) - (a + b) / 2)
    if t == "equal":
        if K[0] == "line" and K[1] == "line":
            a1, b1, _ = edge(items, R[0]); a2, b2, _ = edge(items, R[1])
            return [float(np.linalg.norm(b1 - a1) - np.linalg.norm(b2 - a2))]
        return [circle(items, R[0])[1] - circle(items, R[1])[1]]
    if t == "concentric":
        return list(circle(items, R[0])[0] - circle(items, R[1])[0])
    if t == "tangent":
        if K[0] == "line" and K[1] == "line":
            raise SketchConstraintError("tangent needs a circle or arc")
        shared = _shared_point(items, R)
        if shared is not None:                    # connected segments: tangent AT the joint (well-conditioned)
            p, (e1, e2) = shared
            def direction(ref, kind):
                if kind == "line":
                    a, b, _ = edge(items, ref)
                    return _unit(b - a)
                cc, _r = circle(items, ref)
                rad = _unit(p - cc)
                return np.array([-rad[1], rad[0]])
            d1, d2 = direction(R[0], K[0]), direction(R[1], K[1])
            return [d1[0] * d2[1] - d1[1] * d2[0]]
        if K[0] == "line" or K[1] == "line":
            li, ci = (0, 1) if K[0] == "line" else (1, 0)
            a, b, _ = edge(items, R[li]); cc, r = circle(items, R[ci]); d = _unit(b - a)
            return [abs(d[0] * (cc - a)[1] - d[1] * (cc - a)[0]) - r]
        (c1, r1), (c2, r2) = circle(items, R[0]), circle(items, R[1])
        dist = float(np.linalg.norm(c1 - c2))
        internal = c.get("internal")
        if internal is None:
            internal = abs(dist - abs(r1 - r2)) < abs(dist - (r1 + r2))
            c["internal"] = bool(internal)
        return [dist - (abs(r1 - r2) if internal else r1 + r2)]
    v = float(c.get("value", 0.0))
    if t == "length":
        a, b, _ = edge(items, R[0])
        return [float(np.linalg.norm(b - a)) - v]
    if t == "distance":
        p = point(items, R[0])
        if K[1] == "pt":
            return [float(np.linalg.norm(point(items, R[1]) - p)) - v]
        a, b, _ = edge(items, R[1]); d = _unit(b - a)
        return [abs(d[0] * (p - a)[1] - d[1] * (p - a)[0]) - v]
    if t in ("hdistance", "vdistance"):
        a, b = point(items, R[0]), point(items, R[1])
        ax = 0 if t == "hdistance" else 1
        sign = c.setdefault("sign", 1.0 if b[ax] - a[ax] >= 0 else -1.0)
        return [(b[ax] - a[ax]) * sign - v]
    if t == "radius":
        return [circle(items, R[0])[1] - v]
    if t == "diameter":
        return [2 * circle(items, R[0])[1] - v]
    if t == "angle":
        a1, b1, _ = edge(items, R[0]); a2, b2, _ = edge(items, R[1])
        d1, d2 = _unit(b1 - a1), _unit(b2 - a2)
        ang = math.degrees(math.atan2(d1[0] * d2[1] - d1[1] * d2[0], d1 @ d2))
        sign = c.setdefault("sign", 1.0 if ang >= 0 else -1.0)
        return [math.radians(ang * sign - v)]
    raise SketchConstraintError(f"unknown constraint type {t!r}")


DIMENSIONS = {"length", "distance", "hdistance", "vdistance", "radius", "diameter", "angle"}


def _validate(items, cons):
    for k, c in enumerate(cons):
        for r in c.get("refs", []):
            if not (0 <= int(r.get("i", -1)) < len(items)):
                raise SketchConstraintError(f"constraint {k + 1} ({c.get('type')}) refers to item {r.get('i')}, which doesn't exist")
        try:
            residuals(items, c)
        except (KeyError, IndexError, ValueError, TypeError) as e:
            raise SketchConstraintError(f"constraint {k + 1} ({c.get('type')}): {e}") from None


def _all(items, cons, slots, x):
    for s, v in zip(slots, x):
        _set(items, s, float(v))
    return np.array([r for c in cons for r in residuals(items, c)], float)


def solve(items: list[dict], constraints: list[dict], drag: dict | None = None, iters: int = 80) -> dict[str, Any]:
    """Solve in place. Returns {ok, residual, dof, free: [variable is free], iterations}. ok=False leaves the items at the
    best least-squares compromise (the caller decides whether to keep it)."""
    for it in items:
        if it.get("type") == "profile":
            it["_closed"] = _closes(it)
    _validate(items, constraints)
    cons = list(constraints) + ([{"type": "coincident", "refs": [drag["ref"], drag["ref"]], "_target": drag["to"]}] if drag else [])
    slots = _slots(items)
    x0 = np.array([_get(items, s) for s in slots], float)
    if not cons or not slots:
        _cleanup(items)
        return {"ok": True, "residual": 0.0, "dof": len(slots), "free": [True] * len(slots), "iterations": 0}

    def F(x):
        r = _all(items, constraints, slots, x)
        if drag:
            r = np.concatenate([r, point(items, drag["ref"]) - np.array(drag["to"], float)])
        return r

    x = x0.copy(); lam = 1e-3; r = F(x); it_n = 0
    for it_n in range(iters):
        if r @ r < TOL ** 2:
            break
        J = _jac(F, x, r)
        A = J.T @ J; g = J.T @ r
        improved = False
        for _ in range(12):
            try:
                step = np.linalg.solve(A + lam * np.diag(np.maximum(np.diag(A), 1e-9)) + 1e-12 * np.eye(len(x)), -g)
            except np.linalg.LinAlgError:
                lam *= 10; continue
            xn = x + step; rn = F(xn)
            if rn @ rn < r @ r:
                x, r, lam, improved = xn, rn, max(lam / 3, 1e-9), True
                break
            lam *= 4
        if not improved:
            break
    res = float(np.sqrt(r @ r))
    _all(items, constraints, slots, x)
    Jc = _jac(lambda z: _all(items, constraints, slots, z), x, _all(items, constraints, slots, x)) if constraints else np.zeros((0, len(x)))
    _all(items, constraints, slots, x)
    dof, free = _dof(Jc)
    _cleanup(items)
    return {"ok": res < 1e-6, "residual": res, "dof": dof, "free": free, "iterations": it_n}


def _jac(F, x, r0):
    J = np.zeros((len(r0), len(x)))
    for j in range(len(x)):
        h = 1e-7 * (1 + abs(x[j]))
        xp = x.copy(); xp[j] += h
        J[:, j] = (F(xp) - r0) / h
    F(x)
    return J


def _dof(J) -> tuple[int, list[bool]]:
    n = J.shape[1]
    if J.shape[0] == 0:
        return n, [True] * n
    _, s, vt = np.linalg.svd(J, full_matrices=True)
    smax = s[0] if len(s) else 0.0
    rank = int((s > max(1e-6, 1e-8 * smax)).sum())
    null = vt[rank:]
    free = [bool(np.any(np.abs(null[:, j]) > 1e-6)) for j in range(n)] if len(null) else [False] * n
    return n - rank, free


def _cleanup(items):
    for it in items:
        if it.get("type") == "profile":
            if it.pop("_closed", False):
                it["segs"][-1]["to"] = list(it["start"])
            it["start"] = [round(v, 6) for v in it["start"]]
            for sg in it["segs"]:
                sg["to"] = [round(v, 6) for v in sg["to"]]
                if sg.get("via"):
                    sg["via"] = [round(v, 6) for v in sg["via"]]
        elif it.get("type") == "polygon":
            it["pts"] = [[round(a, 6), round(b, 6)] for a, b in it["pts"]]
        else:
            for k in ("cx", "cy", "w", "h", "r", "x1", "y1", "x2", "y2"):
                if k in it:
                    it[k] = round(float(it[k]), 6)


def apply(items: list[dict], constraints: list[dict]) -> list[dict]:
    """Solve a sketch for saving: raise a readable error when the constraints can't all be met."""
    if not constraints:
        return items
    res = solve(items, constraints)
    if not res["ok"]:
        bad = [f"{k + 1}. {c['type']}" for k, c in enumerate(constraints)
               if max((abs(v) for v in residuals(items, c)), default=0) > 1e-4]
        raise SketchConstraintError("the sketch constraints conflict (no shape meets them all); check " + ", ".join(bad[:6]))
    return items
