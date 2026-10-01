"""Fluid and pneumatic: O-rings with groove cutters, tube, push-fit fittings and hose barbs."""
from __future__ import annotations

import math

from build123d import Align, Cone, Cylinder, Part, Pos, RegularPolygon, Torus, extrude

from designkit.core import component

# port threads: (major diameter, thread length, hex across flats for a small fitting)
PORTS = {"M5": (5.0, 4.5, 8.0), "1/8 BSP": (9.728, 7.0, 13.0), "1/4 BSP": (13.157, 9.0, 17.0), "3/8 BSP": (16.662, 10.0, 21.0),
         "1/8 NPT": (10.29, 7.0, 13.0), "1/4 NPT": (13.72, 9.0, 17.0)}


def _c(r: float, h: float) -> Part:
    return Cylinder(r, h, align=(Align.CENTER, Align.CENTER, Align.MIN))


@component("fluid", "O-ring (ISO 3601 / AS568 style) by inner diameter and cross-section", tags=["o-ring", "seal", "gasket", "oring"],
           standard="ISO 3601", example='o = Pos(0, 0, 2) * kit.o_ring(20, 2.0)   # mid-plane at the given z',
           related=["o_ring_groove_cutter", "fluid/o-ring-grooves"])
def o_ring(inner_d: float = 20.0, cross_section: float = 2.0) -> Part:
    """O-ring (free state) on the Z axis, centred on the XY plane: a torus of inner diameter `inner_d` and section
    diameter `cross_section`."""
    return Torus((inner_d + cross_section) / 2, cross_section / 2)


@component("fluid", "Groove cutter for an O-ring: face (axial) seal, piston (groove on the shaft) or rod (groove in the bore)",
           tags=["o-ring", "groove", "gland", "seal", "cutter"], standard="squeeze and fill per common gland design",
           example='lid = lid - Pos(0, 0, 0) * kit.o_ring_groove_cutter(2.0, 40, "face")', related=["o_ring", "fluid/o-ring-grooves"])
def o_ring_groove_cutter(cross_section: float = 2.0, seal_d: float = 40.0, kind: str = "face", dynamic: bool = False) -> Part:
    """Annular cutter on the Z axis.
    face:   groove in a flat face at z = 0 going down; `seal_d` = groove inner diameter (≈ O-ring ID for internal
            pressure); depth 0.75 × CS (25 % squeeze), width 1.4 × CS.
    piston: groove in a shaft/piston that seals on a bore of diameter `seal_d`; radial depth 0.8 × CS static or 0.85 ×
            CS dynamic (20 / 15 % squeeze), width 1.4 × CS, from z = 0 up. Pick an O-ring ID ≈ the groove bottom (≤ 5 % stretch).
    rod:    groove in a housing bore that seals on a rod of diameter `seal_d`, same depth rule, from z = 0 up."""
    cs = cross_section
    depth = (0.85 if dynamic else 0.8) * cs
    width = 1.4 * cs
    if kind == "face":
        return Pos(0, 0, -0.75 * cs) * (_c(seal_d / 2 + width, 0.75 * cs + 0.01) - _c(seal_d / 2, 3 * cs))
    if kind == "piston":
        bottom = seal_d / 2 - depth
        return _c(seal_d / 2 + 1.0, width) - Pos(0, 0, -1) * _c(bottom, width + 2)
    if kind == "rod":
        outer = seal_d / 2 + depth
        return _c(outer, width) - Pos(0, 0, -1) * _c(seal_d / 2 - 1.0, width + 2)
    raise ValueError("kind must be face | piston | rod")


@component("fluid", "Tube (pneumatic, hydraulic or structural) by outside and inside diameter", tags=["tube", "pipe", "hose", "pneumatic"],
           example='t = kit.tube(6, 4, 100)   # along +Z from z = 0', related=["push_fit_fitting", "hose_barb"])
def tube(outer_d: float = 6.0, inner_d: float = 4.0, length: float = 100.0) -> Part:
    return _c(outer_d / 2, length) - Pos(0, 0, -1) * _c(inner_d / 2, length + 2)


@component("fluid", "Straight push-in (push-to-connect) pneumatic fitting with a male port thread and release collet",
           tags=["push fit", "push-to-connect", "pneumatic", "fitting", "pc fitting", "bsp", "m5"],
           example='f = kit.push_fit_fitting(6, "1/8 BSP")   # port thread down from z = 0, collet up',
           related=["tube", "hose_barb", "fluid/o-ring-grooves"])
def push_fit_fitting(tube_od: float = 6.0, port: str = "1/8 BSP") -> dict[str, Part]:
    """Fitting on the Z axis: the male port thread (cosmetic) below z = 0, a hex above it, the body and the release
    collet on top, with the tube bore through. Returns {"Body", "Collet"}. Overall proportions are typical of
    common straight male connectors; check the maker's drawing for exact lengths."""
    if port not in PORTS:
        raise ValueError(f"port must be one of {', '.join(PORTS)}")
    maj, tl, af = PORTS[port]
    af = max(af, tube_od + 6)
    thread = Pos(0, 0, -tl) * _c(maj / 2, tl)
    hexa = extrude(RegularPolygon(af / 2 / math.cos(math.pi / 6), 6), 0.35 * af)
    body_d = tube_od + 6
    body = _c(body_d / 2, 0.35 * af + tube_od + 8)
    collar = Pos(0, 0, 0.35 * af + tube_od + 8) * _c(tube_od / 2 + 1.2, 1.0)
    b = thread + hexa + body + collar
    b -= Pos(0, 0, -tl - 1) * _c(min(maj / 2 - 1.0, tube_od / 2 - 0.5), tl + 0.35 * af + tube_od + 12)
    b -= Pos(0, 0, 0.35 * af + 2) * _c(tube_od / 2 + 0.02, tube_od + 10)
    z_top = 0.35 * af + tube_od + 8 + 1.0
    collet = Pos(0, 0, z_top) * (_c(tube_od / 2 + 2.5, 2.5) - Pos(0, 0, -1) * _c(tube_od / 2 + 0.05, 5))
    collet += Pos(0, 0, z_top - 4) * (_c(tube_od / 2 + 1.1, 4) - Pos(0, 0, -1) * _c(tube_od / 2 + 0.05, 6))
    b -= Pos(0, 0, z_top - 4) * _c(tube_od / 2 + 1.15, 5)
    return {"Body": b, "Collet": collet}


@component("fluid", "Hose barb fitting with three barbs, hex and a male port thread", tags=["hose barb", "barb", "fitting", "hose", "tubing"],
           example='h = kit.hose_barb(6, "1/8 BSP")', related=["tube", "push_fit_fitting"])
def hose_barb(hose_id: float = 6.0, port: str = "1/8 BSP") -> Part:
    """Barb on the Z axis: port thread below z = 0, hex, then three barbs (Ø ≈ 1.15 × hose ID) up to the tip, with a
    through bore of about 0.6 × hose ID."""
    maj, tl, af = PORTS[port]
    b = Pos(0, 0, -tl) * _c(maj / 2, tl) + extrude(RegularPolygon(af / 2 / math.cos(math.pi / 6), 6), 0.4 * af)
    z = 0.4 * af
    shank = 0.85 * hose_id / 2
    b += Pos(0, 0, z) * _c(shank, 3 * 1.4 * hose_id)
    for k in range(3):
        b += Pos(0, 0, z + 1.0 + k * 1.4 * hose_id) * Cone(1.15 * hose_id / 2, shank, 1.4 * hose_id * 0.9, align=(Align.CENTER, Align.CENTER, Align.MIN))
    return b - Pos(0, 0, -tl - 1) * _c(0.3 * hose_id, tl + z + 5 * hose_id + 2)
