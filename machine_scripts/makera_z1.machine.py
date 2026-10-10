# Makera Z1 (+ 4th axis) — machine model script.
# Frame: X right, Y back, Z up, origin = centre of the bed top. Numbers: makera.com (work area 200×200×100, gantry
# clearance 115, 4th axis Ø80×150), Makera's official Z1-MDF-v2.1 bed file (206×206×6, 36 counterbored M5 holes) and the
# Carvera Community simplified Z1 model (spindle nose Ø16×20, head 65×70×190, enclosure 355×435×449, chuck Ø52).
# `machine_record` is the library entry this model is built for; `fourth` is True when its 4th axis is installed.

node("base")
node("bed", "base", axis="y", mode="table", stock=not fourth)      # the bed carries the work and moves in Y
node("bridge", "base")
node("head", "bridge", axis="x")
node("spindle", "head", axis="z")

# ---- bed: aluminium plate + MDF spoilboard with the real hole grid
mdf_t, alu_t = 6.0, 12.0
part("bed plate", "bed", box(206, 206, alu_t, zmin=-mdf_t - alu_t), "alu", collision=True)
mdf = box(206, 206, mdf_t, zmin=-mdf_t)
holes = bed_holes("makera_z1_mdf")
for (u, v) in holes:
    x, y = u - 103, v - 103
    mdf = mdf - cyl_z(2.75, mdf_t + 1, at=(x, y), zmin=-mdf_t - 0.5) - cyl_z(5.0, 3.0, at=(x, y), zmin=-3.0)
part("spoilboard", "bed", mdf, "mdf", collision=True)
part("bed carriage", "bed", box(180, 180, 26, zmin=-mdf_t - alu_t - 26), "paint_dark")

# ---- Y rods and the base frame (fixed)
for x in (-70, 70):
    part(f"Y rod {'L' if x < 0 else 'R'}", "base", cyl_y(6, 420, x, -mdf_t - alu_t - 26 - 8, -130), "ground_steel")
part("base frame", "base", box(300, 400, 20, at=(0, 100, 0), zmin=-98), "paint_dark")

# ---- rear bridge: two X rods and the beam the head rides on
bridge_y = 142.0
for z in (175, 300):
    part(f"X rod {'low' if z < 200 else 'high'}", "bridge", cyl_x(10, 330, bridge_y, z, -165), "ground_steel")
part("bridge beam", "bridge", box(330, 40, 60, at=(0, bridge_y, 0), zmin=305), "paint_white")

# ---- head: X carriage column + Z carriage + spindle (home: spindle axis x=-108, y=+101, nose bottom at z=116)
hx, hy, nose_z = -108.0, 101.0, 116.0
part("X carriage", "head", box(83, 48, 196, at=(hx, bridge_y, 0), zmin=136), "paint_white")
head = box(65, 70, 190, at=(hx, hy + 8, 0), zmin=nose_z + 20)                      # 65×70 body above the nose plate
head = head + Pos(hx, hy, nose_z + 20 + 190) * Cone(28.7, 22, 40, align=(Align.CENTER, Align.CENTER, Align.MIN))   # motor taper
part("Z carriage", "spindle", head, "paint_white", collision=True)
part("spindle nose", "spindle", cyl_z(8, 20, at=(hx, hy), zmin=nose_z), "stainless", collision=True)

# ---- enclosure: shell around the bed with a full-height front doorway and an acrylic door
part("enclosure", "base", shell((355, 435, 449), (320, 395, 368), at=(0, 105, 0), zmin=-99, inner_zmin=-69, opening=("front", 310, 330)), "panel", collision=True)
part("front door", "base", box(310, 3, 330, at=(0, 105 - 435 / 2 + 1.5, 0), zmin=-69), "acrylic")

rotary = None
if fourth:
    ay, az = -10.0, 45.0                                                             # chuck axis: 10 mm forward of centre, 45 above the bed
    node("chuck", "bed", axis="a", mode="table", stock=True, pivot=(0.0, ay, az))
    part("4th axis rail", "bed", box(262, 52, 8.5, at=(3, ay, 0), zmin=0), "anodised", collision=True)
    part("4th axis housing", "bed", box(35, 52, 57, at=(-110, ay, 0), zmin=8.5), "anodised", collision=True)
    part("chuck", "chuck", cyl_x(26, 6, ay, az, -84) + cyl_x(24.35, 11, ay, az, -78) + cyl_x(15, 20, ay, az, -67), "steel", collision=True)
    jaws = None
    for k in range(4):
        a = math.radians(90 * k)
        jaw = Pos(-60 + 15.5, ay + 13.2 * math.cos(a), az + 13.2 * math.sin(a)) * Box(31, 10.4 if k % 2 == 0 else 25.5, 25.5 if k % 2 == 0 else 10.4)
        jaws = jaw if jaws is None else jaws + jaw
    part("chuck jaws", "chuck", jaws, "anodised", collision=True)
    tail = (box(34, 52, 57, at=(111, ay, 0), zmin=8.5) + cyl_x(7.5, 4, ay, az, 93) + cyl_x(7, 5, ay, az, 88)
            + Plane(origin=(88, ay, az), z_dir=(-1, 0, 0)) * Cone(7, 0, 8, align=(Align.CENTER, Align.CENTER, Align.MIN)))
    part("tailstock", "bed", tail, "anodised", collision=True)
    rotary = {"axis_y": ay, "axis_z": az, "chuck_face_x": -45.0, "chuck_r": 26.0, "tail_x": 80.0, "tail_r": 7.5, "max_diameter": 80, "max_length": 150}

machine(key="z1_4axis" if fourth else "z1", home=(hx, hy, nose_z), travel=(200, 200, 100), clearance=nose_z,
        nose=[{"name": "collet / nose", "r": 8.0, "h": 20.0}, {"name": "head", "r": 36.0, "h": 190.0}],
        table={"x": 206, "y": 206, "t": mdf_t + alu_t, "holes": holes, "hole_d": 5.5}, rotary=rotary, spoilboard=mdf_t,
        sources={"work area": "verified: makera.com 200×200×100", "gantry clearance": "verified: makera.com 115 mm",
                 "bed 206×206 and hole grid": "verified: Makera Z1-MDF-v2.1 (official bed file)",
                 "spindle nose Ø16×20": "reference: community simplified model — measure yours",
                 "head 65×70×190": "reference: community simplified model", "enclosure 355×435×449": "reference: community simplified model",
                 "4th axis chuck Ø52, axis 45 above bed, tailstock Ø14": "reference: community simplified model; Ø80×150 verified: makera.com"},
        notes="Moving bed in Y under a fixed rear bridge; the head moves X along the bridge and Z. Machine zero: head back-left, bed forward.")
