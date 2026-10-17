# Strap clamp: a slotted strap bar resting on the stock's edge, pulled down by a cap screw into the bed, the far end on a
# packing block. Frame origin = the strap's tip on the stock edge; the strap runs along +X away from the stock (turn it
# with `rot` so it points away from the stock). Sizes are typical of a desktop-CNC clamp kit (estimated).
if not stock:
    raise ValueError("a strap clamp needs the stock thickness (used from a Setup, or pass stock={'z': ..})")
sz = float(stock["z"])
L = float(params.get("length") or 60.0); over = float(params.get("overhang") or 8.0)
w, t = 16.0, 6.0
part("strap", box(L, w, t, at=(L / 2 - over, 0, 0), zmin=sz), "steel")
bx = L * 0.45 - over
part("cap screw", cyl_z(4.25, 5.0, at=(bx, 0), zmin=sz + t), "ground_steel")
part("washer", cyl_z(6.0, 1.2, at=(bx, 0), zmin=sz + t), "ground_steel")
part("packing block", box(12, w, sz, at=(L - over - 6, 0, 0), zmin=0), "steel")
fixture("Strap clamp", work_origin=(0.0, 0.0, 0.0), clamp_axis=None,
        mount={"type": "bolt", "note": "M5 into the bed's threaded holes"},
        sources={"strap, screw, block": "estimated: typical desktop CNC strap clamp"},
        notes="The strap overhangs the stock edge by `overhang` (8 mm) at the stock top: keep tools clear of it.")
