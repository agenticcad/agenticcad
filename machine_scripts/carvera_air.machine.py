# Makera Carvera Air (+ 4th axis) — machine model script.
# Numbers: makera.com (work area 300×200×130, gantry clearance 120, footprint 500×450×450, 4th axis Ø92×200) and the
# Carvera Community simplified Air model (bed 306×222×15 with 66 Ø6 holes, nose Ø16×21 under a Ø33.6×8 collar,
# head block 120×98×200, bridge rods Ø20; chuck parts shared with the Z1).

node("base")
node("bed", "base", axis="y", mode="table", stock=not fourth)
node("bridge", "base")
node("head", "bridge", axis="x")
node("spindle", "head", axis="z")

mdf_t, alu_t = 15.0, 15.0
part("bed plate", "bed", box(306, 222, alu_t, zmin=-mdf_t - alu_t), "alu", collision=True)
mdf = box(306, 222, mdf_t, zmin=-mdf_t)
holes = bed_holes("carvera_air_mdf")
for (u, v) in holes:
    mdf = mdf - cyl_z(3.0, mdf_t + 1, at=(u - 153, v - 111), zmin=-mdf_t - 0.5)
part("spoilboard", "bed", mdf, "mdf", collision=True)
part("bed carriage", "bed", box(260, 200, 30, zmin=-mdf_t - alu_t - 30), "paint_dark")
for x in (-100, 100):
    part(f"Y rod {'L' if x < 0 else 'R'}", "base", cyl_y(8, 430, x, -mdf_t - alu_t - 30 - 10, -200), "ground_steel")
part("base frame", "base", box(440, 400, 20, at=(0, 60, 0), zmin=-100), "paint_dark")
bridge_y = 196.0
for z in (125, 214):
    part(f"X rod {'low' if z < 200 else 'high'}", "bridge", cyl_x(10, 490, bridge_y, z, -245), "ground_steel")
part("bridge beam", "bridge", box(490, 46, 50, at=(0, bridge_y, 0), zmin=236), "paint_light")
hx, hy, nose_z = -150.0, 100.0, 120.0
part("X carriage", "head", box(128, 60, 240, at=(hx, bridge_y - 20, 0), zmin=60), "paint_light")
head = box(120, 98, 200, at=(hx, hy + 9, 0), zmin=nose_z + 21 + 8)
part("Z carriage", "spindle", head, "paint_light", collision=True)
part("spindle collar", "spindle", cyl_z(16.8, 8, at=(hx, hy), zmin=nose_z + 21), "anodised", collision=True)
part("spindle nose", "spindle", cyl_z(8, 21, at=(hx, hy), zmin=nose_z), "stainless", collision=True)
part("enclosure", "base", shell((500, 450, 450), (470, 420, 400), at=(0, 60, 0), zmin=-100, inner_zmin=-70, opening=("front", 450, 330)), "panel", collision=True)
part("front door", "base", box(450, 3, 330, at=(0, 60 - 450 / 2 + 1.5, 0), zmin=-70), "acrylic")

rotary = None
if fourth:
    ay, az = 0.0, 46.0
    node("chuck", "bed", axis="a", mode="table", stock=True, pivot=(0.0, ay, az))
    part("4th axis rail", "bed", box(300, 52, 8.5, at=(0, ay, 0), zmin=0), "anodised", collision=True)
    part("4th axis housing", "bed", box(100, 76, 100, at=(-110, ay, 0), zmin=8.5), "anodised", collision=True)
    part("chuck", "chuck", cyl_x(26, 6, ay, az, -60) + cyl_x(24.35, 11, ay, az, -54) + cyl_x(15, 20, ay, az, -43), "steel", collision=True)
    jaws = None
    for k in range(4):
        a = math.radians(90 * k)
        jaw = Pos(-36 + 15.5, ay + 13.2 * math.cos(a), az + 13.2 * math.sin(a)) * Box(31, 10.4 if k % 2 == 0 else 25.5, 25.5 if k % 2 == 0 else 10.4)
        jaws = jaw if jaws is None else jaws + jaw
    part("chuck jaws", "chuck", jaws, "anodised", collision=True)
    tail = (box(40, 76, 100, at=(125, ay, 0), zmin=8.5) + cyl_x(7.5, 4, ay, az, 101) + cyl_x(7, 5, ay, az, 96)
            + Plane(origin=(96, ay, az), z_dir=(-1, 0, 0)) * Cone(7, 0, 8, align=(Align.CENTER, Align.CENTER, Align.MIN)))
    part("tailstock", "bed", tail, "anodised", collision=True)
    rotary = {"axis_y": ay, "axis_z": az, "chuck_face_x": -21.0, "chuck_r": 26.0, "tail_x": 88.0, "tail_r": 7.5, "max_diameter": 92, "max_length": 200}

machine(key="air_4axis" if fourth else "air", home=(hx, hy, nose_z), travel=(300, 200, 130), clearance=nose_z,
        nose=[{"name": "collet / nose", "r": 8.0, "h": 21.0}, {"name": "collar", "r": 16.8, "h": 8.0}, {"name": "head", "r": 55.0, "h": 200.0}],
        table={"x": 306, "y": 222, "t": mdf_t + alu_t, "holes": holes, "hole_d": 6.0}, rotary=rotary, spoilboard=mdf_t,
        sources={"work area": "verified: makera.com 300×200×130", "gantry clearance": "verified: makera.com 120 mm",
                 "bed 306×222 and hole grid": "verified: Carvera Community Air bed model (66 holes)",
                 "spindle nose Ø16×21, collar Ø33.6×8": "reference: community simplified model — measure yours",
                 "head 120×98×200, rods Ø20": "reference: community simplified model",
                 "enclosure 500×450×450": "verified: makera.com footprint; cavity estimated",
                 "4th axis": "reference: chuck parts from the community model; Ø92×200 verified: makera.com"},
        notes="Moving bed in Y under a fixed rear bridge; the head moves X along the bridge rods and Z.")
