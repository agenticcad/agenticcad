# Makera Z1 low-profile vise — a flat two-jaw vise that bolts to the Z1 bed hole grid. Proportions ESTIMATED from Makera's
# product photo (no published drawing): base 120 × 52 × 8, jaws 120 wide × 10 deep × 20 tall, jaw floor 4 above the base,
# lead screw along Y under the moving jaw. Jaws close along Y: the fixed jaw is at the back (+Y).
opening = float(params.get("opening") or (stock["y"] if stock else 40.0))
par = params.get("parallel")
if par is None:
    par = 0.0
opening = min(opening, 70.0)
jaw_h, jaw_d, w, base_t, floor_t = 20.0, 10.0, 120.0, 8.0, 4.0
if params.get("parallel") is None and stock and stock["z"] < jaw_h:
    par = round(jaw_h - stock["z"] + 2.0, 1)        # auto parallel: the stock top sits 2 mm proud of the jaws
part("base", box(w, 52 + opening, base_t, zmin=0), "anodised")
part("jaw floor", box(w, opening + 2 * jaw_d, floor_t, zmin=base_t), "alu")
part("fixed jaw", box(w, jaw_d, jaw_h, at=(0, opening / 2 + jaw_d / 2, 0), zmin=base_t + floor_t), "steel")
part("moving jaw", box(w, jaw_d, jaw_h, at=(0, -opening / 2 - jaw_d / 2, 0), zmin=base_t + floor_t), "steel")
if par > 0:
    for sy in (-1, 1):
        part(f"parallel {'F' if sy < 0 else 'B'}", box(w, 3, par, at=(0, sy * (opening / 2 - 1.5), 0), zmin=base_t + floor_t), "ground_steel")
part("lead screw", cyl_y(4, opening + 2 * jaw_d + 30, 0, base_t + floor_t + 6, -opening / 2 - jaw_d - 30), "ground_steel", collision=False)
part("screw knob", cyl_y(9, 10, 0, base_t + floor_t + 6, -opening / 2 - jaw_d - 30), "anodised")
for sx in (-1, 1):
    for sy in (-1, 1):
        part(f"mount bolt {'L' if sx < 0 else 'R'}{'F' if sy < 0 else 'B'}", cyl_z(4, base_t + 1, at=(sx * 50, sy * (opening / 2 + jaw_d + 6)), zmin=-0.5), "steel", collision=False)
fixture("Makera low-profile vise", work_origin=(0.0, 0.0, base_t + floor_t + par), clamp_axis="y", max_opening=70.0,
        mount={"type": "holes", "pitch": 25.0, "note": "M5 bolts into the Z1 bed grid (holes 25 mm apart)"},
        sources={"vise type and proportions": "estimated: Makera product photo of the Z1 low-profile vise — measure yours",
                 "mounting": "reference: Z1 bed hole grid (bed_holes.json)"},
        notes="Jaws close along Y; the fixed jaw is at the back (+Y). Stock sits on the jaw floor (thin stock on auto parallels, params['parallel']) 12 mm above the bed.")
