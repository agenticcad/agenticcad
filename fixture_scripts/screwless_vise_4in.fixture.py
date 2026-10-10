# 4" screwless (toolmaker's) vise — hardened precision vise: body 160 × 105 × 32 with a flat bed, 4" (101.6) wide jaws
# 20 tall; the moving jaw is pulled down by a half-nut on a screw along the body (no handle sticking out).
# Standard dimensions of the common imported 4" screwless vise (sources below); the stock sits on the body bed between
# the jaws, 32 mm above the table.
opening = float(params.get("opening") or (stock["y"] if stock else 60.0))
par = params.get("parallel")
if par is None:
    par = 0.0
opening = min(opening, 100.0)
L, W, H, jaw_w, jaw_d, jaw_h = 160.0, 105.0, 32.0, 101.6, 16.0, 20.0
if params.get("parallel") is None and stock and stock["z"] < jaw_h:
    par = round(jaw_h - stock["z"] + 2.0, 1)        # auto parallel: the stock top sits 2 mm proud of the jaws
part("body", box(jaw_w + 6, L, H, zmin=0), "ground_steel")
part("fixed jaw", box(jaw_w, jaw_d, jaw_h, at=(0, opening / 2 + jaw_d / 2, 0), zmin=H), "steel")
part("moving jaw", box(jaw_w, jaw_d, jaw_h, at=(0, -opening / 2 - jaw_d / 2, 0), zmin=H), "steel")
if par > 0:
    for sy in (-1, 1):
        part(f"parallel {'F' if sy < 0 else 'B'}", box(jaw_w, 3, par, at=(0, sy * (opening / 2 - 1.5), 0), zmin=H), "ground_steel")
part("clamp screw", cyl_y(6, L - 20, 0, H / 2, -L / 2 + 10), "ground_steel", collision=False)
fixture('4" screwless vise', work_origin=(0.0, 0.0, H + par), clamp_axis="y", max_opening=100.0,
        mount={"type": "clamps", "note": "held with clamps or a stop; often sat on parallels"},
        sources={"4\" jaw width, 160 × 105 × 32 body": "reference: common imported 4\" screwless vise listings (Vertex/Soba type)"},
        notes="Jaws close along Y. Stock sits on the body bed 32 mm above the table; thin stock is raised on parallels automatically (params['parallel'] sets the height).")
