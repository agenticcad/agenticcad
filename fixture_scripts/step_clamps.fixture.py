# Step clamps: four clamp bars on step blocks pressing the stock's ±X edges onto the table (stock on the bed, no riser).
# The bars overhang the stock by params['overhang'] (12 mm): tools near those edges will hit them — the simulator reports it.
# Sizes: bar 100 × 25 × 12 (M10 clamp set), stud Ø10, step block 50 × 25 × 40. Estimated / representative.
if not stock:
    raise ValueError("step clamps need the stock size (used from a Setup, or pass stock={'x':..,'y':..,'z':..})")
sx_, sy_, sz = stock["x"], stock["y"], stock["z"]
over = float(params.get("overhang") or 12.0); bar_l, bar_w, bar_t = 100.0, 25.0, 12.0
n = 2 if sy_ > 60 else 1
for side in (-1, 1):
    for k in range(n):
        y = (k - (n - 1) / 2) * (sy_ * 0.5)
        x_edge = side * sx_ / 2
        bar_c = x_edge + side * (bar_l / 2 - over)
        part(f"clamp bar {'L' if side < 0 else 'R'}{k + 1}", box(bar_l, bar_w, bar_t, at=(bar_c, y, 0), zmin=sz), "steel")
        part(f"stud {'L' if side < 0 else 'R'}{k + 1}", cyl_z(5, sz + bar_t + 25, at=(bar_c, y), zmin=0), "ground_steel")
        part(f"nut {'L' if side < 0 else 'R'}{k + 1}", cyl_z(8.5, 8, at=(bar_c, y), zmin=sz + bar_t), "steel")
        part(f"step block {'L' if side < 0 else 'R'}{k + 1}", box(30, bar_w, sz, at=(x_edge + side * (bar_l - over + 5), y, 0), zmin=0), "steel")
fixture("Step clamps", work_origin=(0.0, 0.0, 0.0), clamp_axis=None,
        mount={"type": "tslot", "note": "T-nuts in the table slots or bolts in the bed grid"},
        sources={"clamp set": "estimated: typical M10 step-clamp kit (100 mm bars)"},
        notes="Stock sits directly on the table; clamp bars overhang its ±X edges. Keep the toolpath clear of them or the simulator will flag the hit.")
