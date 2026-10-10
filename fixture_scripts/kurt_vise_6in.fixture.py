# 6" Kurt-style machine vise (DX6 proportions): base 470 × 190, bed 43 above the base, 6" (152.4) wide × 44 tall jaws,
# stationary jaw at the back, movable jaw on the screw with the handle out the front. Opening up to 225 (8.9").
# Numbers from the Kurt DX6 data sheet (jaw width 6", jaw depth 1.75", opening 8.9", height to bed 1.69"); castings estimated.
opening = float(params.get("opening") or (stock["y"] if stock else 100.0))
opening = min(opening, 225.0)
par = params.get("parallel")
jaw_h_ = 44.5
if par is None:
    par = round(jaw_h_ - stock["z"] + 2.0, 1) if stock and stock["z"] < jaw_h_ else 0.0   # auto parallel: stock top 2 mm proud of the jaws
par = float(par)
jaw_w, jaw_d, jaw_h, bed_z = 152.4, 25.0, 44.5, 43.0
L = 470.0
part("base", box(L, 190, bed_z, zmin=0), "cast")
part("stationary jaw", box(jaw_w + 50, 48, jaw_h + 10, at=(0, opening / 2 + 24, 0), zmin=bed_z), "cast")
part("stationary jaw plate", box(jaw_w, jaw_d, jaw_h, at=(0, opening / 2 + jaw_d / 2, 0), zmin=bed_z), "ground_steel")
part("movable jaw", box(jaw_w + 50, 60, jaw_h + 6, at=(0, -opening / 2 - 30, 0), zmin=bed_z), "cast")
part("movable jaw plate", box(jaw_w, jaw_d, jaw_h, at=(0, -opening / 2 - jaw_d / 2, 0), zmin=bed_z), "ground_steel")
part("screw", cyl_y(12, L / 2 + 40, 0, bed_z / 2, -L / 2 - 40), "ground_steel", collision=False)
part("handle hub", cyl_y(18, 30, 0, bed_z / 2, -L / 2 - 70), "steel")
part("handle", cyl_x(6, 180, -L / 2 - 60, bed_z / 2, -90) + cyl_y(6, 110, 90, bed_z / 2, -L / 2 - 60), "steel")
if par > 0:
    for sy in (-1, 1):
        part(f"parallel {'F' if sy < 0 else 'B'}", box(jaw_w, 4, par, at=(0, sy * (opening / 2 - 2), 0), zmin=bed_z), "ground_steel")
fixture('6" Kurt-style vise', work_origin=(0.0, 0.0, bed_z + par), clamp_axis="y", max_opening=225.0,
        mount={"type": "tslot", "note": "keyed to the table T-slots; bolts through the base ears"},
        sources={"jaw width 6\", jaw depth 1.75\", opening 8.9\", bed height 1.69\"": "verified: Kurt DX6 data sheet",
                 "castings, handle": "estimated: representative"},
        notes="Jaws close along Y; stationary jaw at the back (+Y). params['parallel'] raises the stock on parallels.")
