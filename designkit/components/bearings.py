"""Deep-groove ball bearings by designation, modelled as real parts (rings with raceway grooves, balls, cage, shields)."""
from __future__ import annotations

import math
import re

from build123d import Align, Cylinder, Part, Pos, Sphere, Torus

from designkit.core import component

# designation: (bore d, outside D, width B open, width B shielded ZZ/2RS)
BEARINGS: dict[str, tuple[float, float, float, float]] = {
    # miniature 6xx
    "604": (4, 12, 4, 4), "605": (5, 14, 5, 5), "606": (6, 17, 6, 6), "607": (7, 19, 6, 6), "608": (8, 22, 7, 7), "609": (9, 24, 7, 7),
    "623": (3, 10, 4, 4), "624": (4, 13, 5, 5), "625": (5, 16, 5, 5), "626": (6, 19, 6, 6), "627": (7, 22, 7, 7), "628": (8, 24, 8, 8),
    "629": (9, 26, 8, 8),
    "683": (3, 7, 2, 3), "684": (4, 9, 2.5, 4), "685": (5, 11, 3, 5), "686": (6, 13, 3.5, 5), "687": (7, 14, 3.5, 5), "688": (8, 16, 4, 5),
    "689": (9, 17, 4, 5),
    "693": (3, 8, 3, 4), "694": (4, 11, 4, 4), "695": (5, 13, 4, 4), "696": (6, 15, 5, 5), "697": (7, 17, 5, 5), "698": (8, 19, 6, 6),
    # MR miniature
    "MR63": (3, 6, 2, 2.5), "MR74": (4, 7, 2, 2.5), "MR83": (3, 8, 2.5, 3), "MR85": (5, 8, 2, 2.5), "MR95": (5, 9, 2.5, 3),
    "MR105": (5, 10, 3, 4), "MR106": (6, 10, 2.5, 3), "MR115": (5, 11, 4, 4), "MR117": (7, 11, 2.5, 3), "MR126": (6, 12, 3, 4),
    "MR128": (8, 12, 2.5, 3.5), "MR148": (8, 14, 3.5, 4),
    # 60xx / 62xx
    "6000": (10, 26, 8, 8), "6001": (12, 28, 8, 8), "6002": (15, 32, 9, 9), "6003": (17, 35, 10, 10), "6004": (20, 42, 12, 12),
    "6005": (25, 47, 12, 12), "6006": (30, 55, 13, 13), "6007": (35, 62, 14, 14), "6008": (40, 68, 15, 15), "6009": (45, 75, 16, 16),
    "6010": (50, 80, 16, 16),
    "6200": (10, 30, 9, 9), "6201": (12, 32, 10, 10), "6202": (15, 35, 11, 11), "6203": (17, 40, 12, 12), "6204": (20, 47, 14, 14),
    "6205": (25, 52, 15, 15), "6206": (30, 62, 16, 16), "6207": (35, 72, 17, 17), "6208": (40, 80, 18, 18),
    # thin section 68xx / 69xx
    "6800": (10, 19, 5, 5), "6801": (12, 21, 5, 5), "6802": (15, 24, 5, 5), "6803": (17, 26, 5, 5), "6804": (20, 32, 7, 7),
    "6805": (25, 37, 7, 7), "6806": (30, 42, 7, 7),
    "6900": (10, 22, 6, 6), "6901": (12, 24, 6, 6), "6902": (15, 28, 7, 7), "6903": (17, 30, 7, 7), "6904": (20, 37, 9, 9),
    "6905": (25, 42, 9, 9),
}


def bearing_size(designation: str) -> tuple[float, float, float, bool]:
    """(d, D, B, shielded) for a designation like '688', '688ZZ', '6204-2RS', 'MR105ZZ'."""
    m = re.fullmatch(r"\s*(MR\d+|\d{3,4})\s*[-_ ]?\s*(ZZ|2Z|2RS|RS|Z|2RSH|DDU)?\s*", designation.upper())
    if not m or m.group(1) not in BEARINGS:
        raise ValueError(f"unknown bearing '{designation}'. Known: {', '.join(BEARINGS)}")
    d, D, b_open, b_sh = BEARINGS[m.group(1)]
    shielded = bool(m.group(2))
    return d, D, (b_sh if shielded else b_open), shielded


@component("bearings", "Deep-groove ball bearing by designation (608, 688ZZ, 6204-2RS, MR105...) as real parts",
           tags=["bearing", "ball bearing", "deep groove", "608", "625", "688", "6000", "6200", "mr"],
           standard="ISO 15 boundary dimensions",
           example='b = kit.place(kit.ball_bearing("688ZZ"), Pos(0, 0, 29))   # dict of parts; bottom face at z = 29',
           related=["bearings/selection-and-retention", "circlip_internal", "circlip_external"])
def ball_bearing(designation: str = "608", detail: str = "full", shields: bool | None = None) -> dict[str, Part] | Part:
    """A deep-groove ball bearing on the Z axis, bottom face at z = 0, centred on the origin.

    detail="full" returns a dict of parts: InnerRing and OuterRing (with toroidal raceway grooves and edge chamfers),
    Ball1..BallN, a ribbon Cage, and ShieldA/ShieldB when shielded. Put the dict straight into `result` as a
    component, or move it with kit.place(bearing, Pos(...)).
    detail="simple" returns one solid (an annulus of the bearing's envelope), for fast layouts.
    shields: None follows the designation (ZZ/2RS suffix => shields, and the shielded width).
    Boundary dimensions (d, D, B) are the catalogue values; ball size and count are typical, not a brand's exact figure."""
    d, D, B, sh = bearing_size(designation)
    shields = sh if shields is None else shields
    if detail == "simple":
        return Cylinder(D / 2, B, align=(Align.CENTER, Align.CENTER, Align.MIN)) - Cylinder(d / 2, 3 * B)
    if detail != "full":
        raise ValueError("detail must be full | simple")
    pr = (d + D) / 4                                  # pitch radius
    t_sh = max(0.2, 0.05 * B)                         # shield thickness
    margin = 0.1 + t_sh + 0.05                        # cage clear of the shields
    db = min(0.315 * (D - d), 0.8 * B, (B - 2 * margin - 0.15) / 1.06)   # ball diameter: pockets never split the cage
    br = db / 2
    n = max(6, round(0.55 * math.pi * 2 * pr / db))
    zc = B / 2
    groove = Pos(0, 0, zc) * Torus(pr, 0.52 * db)
    ri_o, ro_i = pr - 0.55 * br, pr + 0.55 * br       # land diameters either side of the balls
    ch = min(0.3, 0.06 * (D - d))
    inner = (Cylinder(ri_o, B, align=(Align.CENTER, Align.CENTER, Align.MIN)) - Cylinder(d / 2, 3 * B)) - groove
    outer = (Cylinder(D / 2, B, align=(Align.CENTER, Align.CENTER, Align.MIN)) - Cylinder(ro_i, 3 * B)) - groove
    inner = inner.chamfer(ch, None, [e for e in inner.edges() if e.geom_type.name == "CIRCLE" and abs(e.radius - d / 2) < 1e-6])
    outer = outer.chamfer(ch, None, [e for e in outer.edges() if e.geom_type.name == "CIRCLE" and abs(e.radius - D / 2) < 1e-6])
    parts: dict[str, Part] = {"InnerRing": inner, "OuterRing": outer}
    cage_h = max(min(B - 2 * margin, 1.3 * db), 1.06 * db + 0.15)
    cage = Pos(0, 0, zc) * (Cylinder(pr + 0.4 * br, cage_h) - Cylinder(pr - 0.4 * br, B))
    for i in range(n):
        a = 2 * math.pi * i / n
        c = (pr * math.cos(a), pr * math.sin(a), zc)
        parts[f"Ball{i + 1}"] = Pos(*c) * Sphere(br)
        cage = cage - Pos(*c) * Sphere(br * 1.06)
    parts["Cage"] = cage
    if shields:
        t = t_sh
        gap = 0.15 * (ro_i - ri_o)
        for name, z in (("ShieldA", 0.1), ("ShieldB", B - 0.1 - t)):
            parts[name] = Pos(0, 0, z) * (Cylinder(ro_i + 0.4 * (D / 2 - ro_i), t, align=(Align.CENTER, Align.CENTER, Align.MIN))
                                         - Cylinder(ri_o + gap, 3 * t))
        # seat the shields: relieve the outer ring where they sit
        for z in (0.1, B - 0.1 - t):
            parts["OuterRing"] = parts["OuterRing"] - Pos(0, 0, z) * Cylinder(ro_i + 0.4 * (D / 2 - ro_i), t, align=(Align.CENTER, Align.CENTER, Align.MIN))
    return parts


@component("bearings", "Bore/seat cutter for a bearing: a cylinder of the outside diameter and width, for housings",
           tags=["bearing", "seat", "housing", "bore", "cutter"],
           example='housing = housing - Pos(0, 0, 10) * kit.bearing_seat("608")', related=["ball_bearing"])
def bearing_seat(designation: str = "608", depth: float | None = None, clearance: float = 0.0) -> Part:
    """A cylinder of the bearing's outside diameter (+clearance) and width (or `depth`), bottom at z = 0, to subtract
    from a housing. For 3D-printed housings add ~0.1-0.2 mm clearance; for machined press fits use 0 (fit is by tolerance)."""
    d, D, B, _ = bearing_size(designation)
    return Cylinder(D / 2 + clearance / 2, depth or B, align=(Align.CENTER, Align.CENTER, Align.MIN))
