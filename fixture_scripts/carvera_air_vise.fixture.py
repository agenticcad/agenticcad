# Carvera Air vise — low two-jaw vise for the Air's 306 × 222 bed. Proportions ESTIMATED (no published drawing):
# base 150 × 60 × 10, jaws 150 wide × 12 deep × 24 tall, jaw floor 5 above the base; jaws close along Y, fixed jaw at the back.
opening = float(params.get("opening") or (stock["y"] if stock else 50.0))
par = params.get("parallel")
if par is None:
    par = 0.0
opening = min(opening, 90.0)
jaw_h, jaw_d, w, base_t, floor_t = 24.0, 12.0, 150.0, 10.0, 5.0
if params.get("parallel") is None and stock and stock["z"] < jaw_h:
    par = round(jaw_h - stock["z"] + 2.0, 1)        # auto parallel: the stock top sits 2 mm proud of the jaws
part("base", box(w, 60 + opening, base_t, zmin=0), "anodised")
part("jaw floor", box(w, opening + 2 * jaw_d, floor_t, zmin=base_t), "alu")
part("fixed jaw", box(w, jaw_d, jaw_h, at=(0, opening / 2 + jaw_d / 2, 0), zmin=base_t + floor_t), "steel")
part("moving jaw", box(w, jaw_d, jaw_h, at=(0, -opening / 2 - jaw_d / 2, 0), zmin=base_t + floor_t), "steel")
if par > 0:
    for sy in (-1, 1):
        part(f"parallel {'F' if sy < 0 else 'B'}", box(w, 3, par, at=(0, sy * (opening / 2 - 1.5), 0), zmin=base_t + floor_t), "ground_steel")
part("lead screw", cyl_y(5, opening + 2 * jaw_d + 34, 0, base_t + floor_t + 7, -opening / 2 - jaw_d - 34), "ground_steel", collision=False)
part("screw knob", cyl_y(11, 12, 0, base_t + floor_t + 7, -opening / 2 - jaw_d - 34), "anodised")
fixture("Carvera Air vise", work_origin=(0.0, 0.0, base_t + floor_t + par), clamp_axis="y", max_opening=90.0,
        mount={"type": "holes", "pitch": 25.0, "note": "M6 bolts into the Air bed grid"},
        sources={"proportions": "estimated: Makera product photos — measure yours"},
        notes="Jaws close along Y; fixed jaw at the back (+Y). Stock sits on the jaw floor (thin stock on auto parallels, params['parallel']) 15 mm above the bed.")
