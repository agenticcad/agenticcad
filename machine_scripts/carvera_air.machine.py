# Makera Carvera Air (+ 4th axis) — machine model script (styled after Makera's product photos).
# Kinematic numbers kept from the built-in model: makera.com (work area 300×200×130, gantry clearance 120,
# footprint 500×450×450, 4th axis Ø92×200) and the Carvera Community simplified Air model (bed 306×222×15
# with 66 Ø6 holes, nose Ø16×21 under a Ø33.6×8 collar, head block 120×98×200, bridge rods Ø20).
# Styling (canopy, panels, frame, pendant, hose, chains, setter, probe, feet) estimated from the photos.

node("base")
node("bed", "base", axis="y", mode="table", stock=not fourth)
node("bridge", "base")
node("head", "bridge", axis="x")
node("spindle", "head", axis="z")

# ---------------- bed (verified / reference) ----------------
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

# ---------------- body envelope (footprint verified, shape estimated) ----------------
W, D = 500.0, 450.0
yf, yr = -165.0, 285.0                      # front / rear faces (same placement as the built-in model)
z_feet, z_body, z_trim, z_top = -100.0, -82.0, -45.0, 356.0
cham_x, cham_y = 35.0, 70.0                 # chamfered front corners (plan)
top_cy, top_cz = 50.0, 55.0                 # chamfer on the canopy's front-top edge
t_can = 4.0
y_hinge = 265.0                             # canopy top ends at the rear hinge bar
y_side = 235.0                              # canopy sides end at the light-grey rear side panels

def plan_face(rear):
    return make_face(Polyline((-W / 2, rear), (-W / 2, yf + cham_y), (-W / 2 + cham_x, yf), (W / 2 - cham_x, yf),
                              (W / 2, yf + cham_y), (W / 2, rear), close=True))

def side_face(rear, bottom):
    return make_face(Polyline((yf, bottom), (rear, bottom), (rear, z_top), (yf + top_cy, z_top), (yf, z_top - top_cz), close=True))

def plan_solid(face2d, z0, z1):
    return Pos(0, 0, z0) * extrude(face2d, amount=z1 - z0)

def side_solid(face2d):
    return Plane.YZ * extrude(face2d, amount=W, both=True)

P_big = plan_face(yr + 120)
P_body = plan_face(yr)
P_in = offset(P_big, amount=-t_can, kind=Kind.INTERSECTION)
S_big = side_face(yr + 120, -200)
S_in = offset(S_big, amount=-t_can, kind=Kind.INTERSECTION)

# light body: lower base, rear panel, rear side panels, hinge bar, inner frame (side walls + top plate)
lower = plan_solid(P_body, z_body, z_trim)
rear_panel = box(W - 2 * t_can, t_can, z_top - t_can - z_trim, at=(0, yr - t_can / 2, 0), zmin=z_trim)
side_rear = None
for s in (-1, 1):
    sp = box(t_can, yr - y_side, z_top - t_can - z_trim, at=(s * (W / 2 - t_can / 2), (y_side + yr) / 2, 0), zmin=z_trim)
    side_rear = sp if side_rear is None else side_rear + sp
hinge_bar = box(W, yr - y_hinge, 16, at=(0, (y_hinge + yr) / 2, 0), zmin=z_top - 14)
walls = None
for s in (-1, 1):
    w = box(12, 401, 380, at=(s * 228, 80.5, 0), zmin=z_trim) - box(14, 250, 310, at=(s * 228, 25, 0), zmin=-10)
    walls = w if walls is None else walls + w
top_plate = box(468, 400, 6, at=(0, 80, 0), zmin=335) - box(436, 164, 8, at=(0, 132, 0), zmin=334)
part("enclosure", "base", lower + rear_panel + side_rear + hinge_bar + walls + top_plate, "paint_light", collision=True)

for (fx, fy) in ((-185, -135), (185, -135), (-185, 250), (185, 250)):
    part(f"foot {fx:+.0f} {fy:+.0f}", "base", cyl_z(15, z_body - z_feet, at=(fx, fy), zmin=z_feet), "rubber")

try:
    bp = Plane(origin=(0, yf, (z_body + z_trim) / 2), x_dir=(1, 0, 0), z_dir=(0, -1, 0))
    badge = bp * (Pos(-20, 0) * extrude(Text("CARVERA", 13), amount=0.8)) + bp * (Pos(40, -1.5) * extrude(Text("AIR", 7), amount=0.8))
except Exception:
    badge = box(110, 0.8, 9, at=(0, yf - 0.4, 0), zmin=(z_body + z_trim) / 2 - 4.5)
part("badge", "base", badge, "paint_dark")

# smoked canopy: one shell over the front, sides and top; open at the bottom and back, hinged at the rear bar
outer = (plan_solid(P_big, z_trim + 15, z_top) & side_solid(S_big)
         & box(W + 2, y_hinge - yf + 1, 700, at=(0, (yf + y_hinge) / 2 - 0.5, 0), zmin=-300))
inner = plan_solid(P_in, -300, 600) & side_solid(S_in)
canopy = outer - inner
for s in (-1, 1):
    canopy = canopy - box(20, 80, z_top - t_can - 0.5 - z_trim + 20, at=(s * (W / 2 - 5), y_side + 40, 0), zmin=z_trim - 20)
part("canopy", "base", canopy, "acrylic")
part("canopy trim", "base", plan_solid(P_body, z_trim, z_trim + 15) - plan_solid(P_in, z_trim - 1, z_trim + 16), "paint_dark")
hinges = None
for hxk in (-150, 150):
    h = cyl_x(5, 40, y_hinge, z_top + 2, hxk - 20)
    hinges = h if hinges is None else hinges + h
part("canopy hinges", "base", hinges, "paint_dark")
part("LED light bar", "base", box(400, 10, 6, at=(0, -112, 0), zmin=329), "paint_white")

# touch-screen pendant on an arm at the right side
part("pendant arm", "base", box(20, 30, 50, at=(W / 2 + 10, 262, 0), zmin=135) + box(40, 12, 12, at=(W / 2 + 40, 256, 0), zmin=154), "paint_light")
part("pendant", "base", box(240, 14, 160, at=(W / 2 + 160, 243, 0), zmin=80), "paint_dark")
part("pendant screen", "base", box(222, 1, 140, at=(W / 2 + 160, 235.5, 0), zmin=90), "paint_blue")

# 3D probe parked in a dock at the front left
part("probe dock", "base", box(34, 34, 10, at=(-190, -135, 0), zmin=z_trim), "paint_dark")
part("probe", "base", cyl_z(10, 38, at=(-190, -135), zmin=z_trim + 10) + cyl_z(3, 25, at=(-190, -135), zmin=z_trim + 48), "stainless")

def u_loop(x_bend, x_up, x_low, r, t):
    ring = Pos(x_bend, 0) * (Circle(r + t / 2) - Circle(r - t / 2))
    half = ring & Pos(x_bend + r + t, 0) * Rectangle(2 * (r + t), 2 * (r + t))
    up = Pos((x_up + x_bend) / 2, r) * Rectangle(x_bend - x_up, t)
    low = Pos((x_low + x_bend) / 2, -r) * Rectangle(x_bend - x_low, t)
    return half + up + low

# Y cable chain beside the bed (right), shown at home
ych = Plane(origin=(190, 0, -27), x_dir=(0, 1, 0), z_dir=(1, 0, 0)) * extrude(u_loop(60, -60, -150, 12, 6), amount=7.5, both=True)
part("Y cable chain", "base", ych, "paint_dark")
part("Y chain bracket", "bed", box(45, 15, 7, at=(175.5, -62.5, 0), zmin=-12), "alu")

# tool-length setter on the bed, front right (outside the 300 mm X travel of the nose)
part("tool setter", "bed", box(28, 28, 20, at=(167, -96, 0), zmin=-30) + cyl_z(6, 22, at=(168, -96), zmin=-10) + cyl_z(4, 3, at=(168, -96), zmin=12), "stainless", collision=True)

# ---------------- bridge ----------------
bridge_y = 196.0
for z in (125, 214):
    part(f"X rod {'low' if z < 200 else 'high'}", "bridge", cyl_x(10, 456, bridge_y, z, -228), "ground_steel")
part("bridge beam", "bridge", box(444, 12, 125, at=(0, 213, 0), zmin=110), "paint_light")
xch = Plane(origin=(0, 228, 275), x_dir=(1, 0, 0), z_dir=(0, -1, 0)) * extrude(u_loop(55, 0, -86, 25, 10), amount=7.5, both=True)
part("X cable chain", "bridge", xch, "paint_dark")
part("X chain mount", "bridge", box(16, 16, 30, at=(0, 228, 0), zmin=305), "paint_dark")

# ---------------- head ----------------
hx, hy, nose_z = -150.0, 100.0, 120.0
part("X carriage", "head", box(128, 60, 240, at=(hx, bridge_y - 20, 0), zmin=60), "paint_light")
part("X chain bracket", "head", box(20, 30, 10, at=(hx + 54, 213, 0), zmin=245), "alu")
head = box(120, 98, 200, at=(hx, hy + 9, 0), zmin=nose_z + 21 + 8)
part("Z carriage", "spindle", head, "paint_light", collision=True)
part("spindle fan", "spindle", box(56, 3, 56, at=(hx, hy - 41, 0), zmin=nose_z + 120), "paint_dark")
part("head knob", "spindle", Pos(hx - 66, hy - 25, nose_z + 190) * Sphere(8), "paint_dark")
part("spindle collar", "spindle", cyl_z(16.8, 8, at=(hx, hy), zmin=nose_z + 21), "anodised", collision=True)
part("spindle nose", "spindle", cyl_z(8, 21, at=(hx, hy), zmin=nose_z), "stainless", collision=True)
hose_path = Spline((hx + 30, hy - 52, nose_z + 32), (hx + 32, hy - 53, nose_z + 100), (hx + 34, hy - 53, nose_z + 165),
                   (hx + 62, hy - 45, nose_z + 192), (hx + 105, hy - 10, nose_z + 188), (hx + 135, hy + 50, nose_z + 168),
                   (hx + 150, hy + 100, nose_z + 150))
hose = sweep(Plane(origin=hose_path @ 0, z_dir=hose_path % 0) * Circle(11), path=hose_path)
part("dust hose", "spindle", hose, "paint_white")

# ---------------- 4th axis ----------------
rotary = None
if fourth:
    ay, az = 0.0, 46.0
    node("chuck", "bed", axis="a", mode="table", stock=True, pivot=(0.0, ay, az))
    part("4th axis rail", "bed", box(300, 52, 8.5, at=(0, ay, 0), zmin=0), "anodised", collision=True)
    part("4th axis housing", "bed", box(100, 76, 100, at=(-110, ay, 0), zmin=8.5), "anodised", collision=True)
    part("4th axis motor cap", "bed", box(40, 70, 85, at=(-180, ay, 0), zmin=8.5), "acrylic")
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
    part("tailstock knob", "bed", cyl_x(9, 8, ay, 72, 145), "anodised")
    rotary = {"axis_y": ay, "axis_z": az, "chuck_face_x": -21.0, "chuck_r": 26.0, "tail_x": 88.0, "tail_r": 7.5, "max_diameter": 92, "max_length": 200}

machine(key="air_4axis" if fourth else "air", home=(hx, hy, nose_z), travel=(300, 200, 130), clearance=nose_z,
        nose=[{"name": "collet / nose", "r": 8.0, "h": 21.0}, {"name": "collar", "r": 16.8, "h": 8.0}, {"name": "head", "r": 55.0, "h": 200.0}],
        table={"x": 306, "y": 222, "t": mdf_t + alu_t, "holes": holes, "hole_d": 6.0}, rotary=rotary, spoilboard=mdf_t,
        sources={"work area": "verified: makera.com 300×200×130", "gantry clearance": "verified: makera.com 120 mm",
                 "bed 306×222 and hole grid": "verified: Carvera Community Air bed model (66 holes)",
                 "spindle nose Ø16×21, collar Ø33.6×8": "reference: community simplified model — measure yours",
                 "head 120×98×200, rods Ø20": "reference: community simplified model",
                 "enclosure 500×450×450": "verified: makera.com footprint (W 500 × D 450 × H 450); top at +356 / 456 overall so the head clears the canopy",
                 "canopy, panels, frame, chamfers": "estimated: Makera product photos (chamfers 35×70 plan, 50×55 front-top, 4 mm acrylic)",
                 "pendant, hose, cable chains, LED bar, setter, probe, feet": "estimated: Makera product photos",
                 "4th axis": "reference: chuck parts from the community model; Ø92×200 verified: makera.com"},
        notes="Moving bed in Y under a fixed rear bridge; the head moves X along the bridge rods and Z. Canopy hinged at the rear bar (shown closed). Cable chains and hose are drawn at the home pose.")
