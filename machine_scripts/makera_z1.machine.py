# Makera Z1 (+ 4th axis) — detailed machine model.
# Frame: X right, Y back, Z up, origin = centre of the bed top (spoilboard top) with the bed at machine zero (forward).
# Verified: makera.com (work area 200×200×100, gantry clearance 115, 4th axis Ø80×150), Makera Z1-MDF-v2.1 bed file
# (206×206×6, 36 counterbored M5 holes). Reference: Carvera Community simplified Z1 model (spindle nose Ø16×20,
# head 65×70×190, enclosure 355×435×449, chuck Ø52). Everything else (rods, carriages, chains, probe dock, tool setter,
# window, LED, feet) is ESTIMATED from photos — representative, not measured.

node("base")
node("bed", "base", axis="y", mode="table", stock=not fourth)      # the bed carries the work and moves in Y
node("bridge", "base")
node("head", "bridge", axis="x")
node("spindle", "head", axis="z")

# ------------------------------------------------------------------ helpers
def drag_chain(origin, run, sep, rot=0.0, h=12.0, w=14.0, pitch=11.0):
    """Flat U-shaped cable chain: two straight runs `sep` apart joined by a 180° bend (local u along the run)."""
    links = []
    n = int(run // pitch)
    for i in range(n + 1):
        for v in (0.0, sep):
            links.append(Pos(i * pitch, v, h / 2) * Box(pitch - 1.5, w, h))
    r = sep / 2
    nb = max(3, int(math.pi * r / pitch) + 1)
    for k in range(1, nb):
        th = -90 + 180 * k / nb
        links.append(Pos(n * pitch + r * math.cos(math.radians(th)), r + r * math.sin(math.radians(th)), h / 2)
                     * Rot(0, 0, th + 90) * Box(pitch - 1.5, w, h))
    return Pos(*origin) * Rot(0, 0, rot) * Compound(links)

# ------------------------------------------------------------------ bed: aluminium plate + MDF spoilboard (verified)
mdf_t, alu_t = 6.0, 12.0
part("bed plate", "bed", box(206, 206, alu_t, zmin=-mdf_t - alu_t), "alu", collision=True)
mdf = box(206, 206, mdf_t, zmin=-mdf_t)
holes = bed_holes("makera_z1_mdf")
for (u, v) in holes:
    x, y = u - 103, v - 103
    mdf = mdf - cyl_z(2.75, mdf_t + 1, at=(x, y), zmin=-mdf_t - 0.5) - cyl_z(5.0, 3.0, at=(x, y), zmin=-3.0)
part("spoilboard", "bed", mdf, "mdf", collision=True)
part("bed carriage", "bed", box(180, 180, 26, zmin=-mdf_t - alu_t - 26), "paint_dark")

# Y linear bearings under the carriage
rod_z = -52.0
yb = None
for x in (-70, 70):
    for y0 in (-80, 40):
        b = cyl_y(8, 40, x, rod_z, y0) - cyl_y(6.2, 42, x, rod_z, y0 - 1)
        yb = b if yb is None else yb + b
part("Y bearings", "bed", yb, "alu")

# front accessory rail on the bed: tool-length setter (front-left) and wireless probe dock (front-right) — estimated
rail_y0, rail_y1 = -116.0, -103.0
ry = (rail_y0 + rail_y1) / 2
part("accessory rail", "bed", box(206, rail_y1 - rail_y0, 12, at=(0, ry, 0), zmin=-18), "anodised")
tls = cyl_z(6, 11, at=(-85, ry), zmin=-3) + cyl_z(4.5, 3, at=(-85, ry), zmin=8)
part("tool length setter", "bed", tls, "steel", collision=True)
part("tool setter base", "bed", box(20, 12, 3, at=(-85, ry, 0), zmin=-6), "paint_dark")
dock = box(28, 12, 16, at=(80, ry, 0), zmin=-6) - cyl_z(4.0, 12, at=(80, ry), zmin=-2)
part("probe dock", "bed", dock, "paint_dark", collision=True)
probe = (cyl_z(1.0, 10, at=(80, ry), zmin=0) + Pos(80, ry, 0) * Sphere(1.5)
         + cyl_z(7.5, 26, at=(80, ry), zmin=10) + cyl_z(3.0, 14, at=(80, ry), zmin=36))
probe = probe - cyl_z(7.6, 1.0, at=(80, ry), zmin=30) + cyl_z(7.0, 1.0, at=(80, ry), zmin=30)
part("wireless probe", "bed", probe, "stainless", collision=True)

# ------------------------------------------------------------------ base: frame, Y rods, Y screw, Y cable chain
part("base frame", "base", box(300, 400, 20, at=(0, 95, 0), zmin=-97), "paint_dark")
for x in (-70, 70):
    part(f"Y rod {'L' if x < 0 else 'R'}", "base", cyl_y(6, 422, x, rod_z, -115), "ground_steel")
    for y0 in (-117, 297):
        part(f"Y rod support {'L' if x < 0 else 'R'}{'F' if y0 < 0 else 'B'}", "base",
             box(26, 10, 31, at=(x, y0 + 5, 0), zmin=-77), "alu")
part("Y ball screw", "base", cyl_y(4, 400, 0, -64, -105), "ground_steel")
part("Y cable chain", "base", drag_chain((128, -60, -77), 190, 28, rot=90), "rubber")

# ------------------------------------------------------------------ rear bridge: uprights, X rods, screw, beam, motor, X chain
rod_y = 176.0
for sx in (-1, 1):
    part(f"bridge upright {'L' if sx < 0 else 'R'}", "bridge", box(14, 70, 415, at=(sx * 165, 201, 0), zmin=-97), "paint_white", collision=True)
for z in (170, 300):
    part(f"X rod {'low' if z < 200 else 'high'}", "bridge", cyl_x(6, 316, rod_y, z, -158), "ground_steel")
part("X ball screw", "bridge", cyl_x(4, 316, rod_y, 235, -158), "ground_steel")
part("bridge beam", "bridge", box(344, 56, 12, at=(0, 208, 0), zmin=318), "paint_white")
part("X motor", "bridge", box(42, 40, 42, at=(137, 256, 0), zmin=214), "paint_dark")
part("X cable chain", "bridge", drag_chain((-160, 197, 330), 150, 28), "rubber")

# ------------------------------------------------------------------ head: X carriage (moves in X)
hx, hy, nose_z = -108.0, 101.0, 116.0          # home: spindle axis x=-108, y=+101, nose bottom 116 above the bed (verified)
xc = box(90, 20, 176, at=(hx, 156, 0), zmin=140)
xc = fillet(xc.edges().filter_by(Axis.Z), 3)
for z in (170, 300):
    for x0 in (hx - 38, hx + 8):
        xc = xc + (cyl_x(11, 30, rod_y, z, x0) - cyl_x(6.2, 32, rod_y, z, x0 - 1))
for sx in (-1, 1):
    xc = xc + box(8, 4, 170, at=(hx + sx * 22, 146, 0), zmin=145)       # Z linear rails on the carriage front (y 144..148)
part("X carriage", "head", xc, "paint_white", collision=True)
part("Z motor", "head", box(28, 28, 28, at=(hx, 160, 0), zmin=316), "paint_dark")

# ------------------------------------------------------------------ Z carriage + brushless spindle (moves in Z)
# 65×70×190 head envelope (reference): exposed spindle motor 136..180 + white head cover 180..326
hc = box(65, 70, 146, at=(hx, 109, 0), zmin=180)
hc = fillet(hc.edges().filter_by(Axis.Z), 10)
part("Z carriage", "spindle", hc, "paint_white", collision=True)
part("head badge", "spindle", box(40, 1, 60, at=(hx, 73.5, 0), zmin=250), "paint_dark")
sm = cyl_z(22, 44, at=(hx, hy), zmin=nose_z + 20)
for z in range(140, 178, 5):
    sm = sm - (cyl_z(23, 2, at=(hx, hy), zmin=z) - cyl_z(20.5, 2.2, at=(hx, hy), zmin=z - 0.1))   # cooling fins
part("spindle motor", "spindle", sm, "anodised", collision=True)
# spindle nose Ø16×20 envelope (reference): nose 124..136 + collet nut 116..124
part("spindle nose", "spindle", cyl_z(8, 12, at=(hx, hy), zmin=nose_z + 8), "stainless", collision=True)
cn = (cyl_z(8, 8, at=(hx, hy), zmin=nose_z) & box(14, 30, 10, at=(hx, hy, 0), zmin=nose_z - 1))
cn = cn - cyl_z(1.6, 3, at=(hx, hy), zmin=nose_z - 0.5) - (cyl_z(8.5, 0.8, at=(hx, hy), zmin=nose_z + 4) - cyl_z(7.4, 1, at=(hx, hy), zmin=nose_z + 3.9))
part("collet nut", "spindle", cn, "steel", collision=True)
# magnetic dust shoe (removable; not in the nose collision stack)
ds = cyl_z(30, 10, at=(hx, hy), zmin=nose_z + 20) - cyl_z(23, 12, at=(hx, hy), zmin=nose_z + 19)
ds = ds + (cyl_x(7, 18, hy, nose_z + 27, hx + 27) - cyl_x(5, 20, hy, nose_z + 27, hx + 26))
part("dust shoe", "spindle", ds, "acrylic")
part("dust shoe brush", "spindle", cyl_z(30, 16, at=(hx, hy), zmin=nose_z + 4) - cyl_z(26, 18, at=(hx, hy), zmin=nose_z + 3), "rubber")

# ------------------------------------------------------------------ enclosure: white sheet metal, front window opening
ex, ey, eh, ecy, ez0, wall = 355.0, 435.0, 449.0, 95.0, -99.0, 2.0
yf = ecy - ey / 2                                  # front face y = -122.5
outer = box(ex, ey, eh, at=(0, ecy, 0), zmin=ez0)
outer = fillet(outer.edges().filter_by(Axis.Z), 12)
inner = box(ex - 2 * wall, ey - 2 * wall, eh - 2 * wall, at=(0, ecy, 0), zmin=ez0 + wall)
inner = fillet(inner.edges().filter_by(Axis.Z), 10)
win_w, win_z0, win_z1 = 304.0, -20.0, 335.0
enc = outer - inner - box(win_w, 10, win_z1 - win_z0, at=(0, yf + 1, 0), zmin=win_z0)
for k in range(6):                                 # rear vent slots
    enc = enc - box(140, 10, 5, at=(0, yf + ey - 1, 0), zmin=180 + k * 12)
part("enclosure", "base", enc, "paint_white", collision=True)

# large tinted flip-up front window, hinged at the top (shown closed)
fw, fh = win_w + 8, win_z1 - win_z0 + 8
frame = box(fw, 4, fh, at=(0, yf - 2, 0), zmin=win_z0 - 4) - box(fw - 20, 6, fh - 20, at=(0, yf - 2, 0), zmin=win_z0 + 6)
part("front window frame", "base", frame, "paint_dark")
part("front window", "base", box(fw - 20, 3, fh - 20, at=(0, yf - 2, 0), zmin=win_z0 + 6), "acrylic")
part("window hinge", "base", cyl_x(3.5, 300, yf - 4.5, win_z1 + 8, -150), "steel")
part("window handle", "base", box(120, 10, 8, at=(0, yf - 9, 0), zmin=win_z0 + 2), "paint_dark")
# front panel: status light + power button + logo
part("status light", "base", cyl_y(7, 3, 130, -60, yf - 3), "acrylic")
part("status light bezel", "base", cyl_y(10, 1.5, 130, -60, yf - 1.5) - cyl_y(7.2, 2, 130, -60, yf - 1.8), "paint_dark")
part("power button", "base", cyl_y(7, 4, 155, -60, yf - 4), "paint_dark")
try:
    logo = Plane(origin=(-150, yf, -66), x_dir=(1, 0, 0), z_dir=(0, -1, 0)) * extrude(Text("MAKERA", 14, align=(Align.MIN, Align.MIN)), 0.8)
    part("logo", "base", logo, "paint_dark")
except Exception:
    pass
# LED light strip under the roof, behind the window
part("LED strip", "base", box(290, 10, 3, at=(0, yf + 14, 0), zmin=ez0 + eh - wall - 3), "acrylic")
# rubber feet
for fx in (-150, 150):
    for fy in (yf + 30, yf + ey - 30):
        part(f"foot {'L' if fx < 0 else 'R'}{'F' if fy < 0 else 'B'}", "base", cyl_z(12, 8, at=(fx, fy), zmin=ez0 - 8), "rubber")

# ------------------------------------------------------------------ 4th-axis module (when fitted)
rotary = None
if fourth:
    ay, az = -10.0, 45.0                                                             # chuck axis: 10 mm forward of centre, 45 above the bed
    node("chuck", "bed", axis="a", mode="table", stock=True, pivot=(0.0, ay, az))
    part("4th axis rail", "bed", box(262, 52, 8.5, at=(3, ay, 0), zmin=0), "anodised", collision=True)
    hs = box(35, 52, 57, at=(-110, ay, 0), zmin=8.5)
    hs = fillet(hs.edges().filter_by(Axis.X), 4)
    part("4th axis housing", "bed", hs, "anodised", collision=True)
    part("4th axis motor cap", "bed", box(4, 44, 44, at=(-129.5, ay, 0), zmin=15), "paint_dark")
    part("chuck", "chuck", cyl_x(26, 6, ay, az, -84) + cyl_x(24.35, 11, ay, az, -78) + cyl_x(15, 20, ay, az, -67), "steel", collision=True)
    jaws = None
    for k in range(4):
        a = math.radians(90 * k)
        jaw = Pos(-60 + 15.5, ay + 13.2 * math.cos(a), az + 13.2 * math.sin(a)) * Box(31, 10.4 if k % 2 == 0 else 25.5, 25.5 if k % 2 == 0 else 10.4)
        jaws = jaw if jaws is None else jaws + jaw
    part("chuck jaws", "chuck", jaws, "anodised", collision=True)
    tb = box(34, 52, 57, at=(111, ay, 0), zmin=8.5)
    tb = fillet(tb.edges().filter_by(Axis.X), 4)
    tail = (tb + cyl_x(7.5, 4, ay, az, 93) + cyl_x(7, 5, ay, az, 88)
            + Plane(origin=(88, ay, az), z_dir=(-1, 0, 0)) * Cone(7, 0, 8, align=(Align.CENTER, Align.CENTER, Align.MIN)))
    part("tailstock", "bed", tail, "anodised", collision=True)
    part("tailstock handwheel", "bed", cyl_x(4, 6, ay, az, 128) + cyl_x(14, 6, ay, az, 134) + cyl_x(3, 10, ay, az + 9, 140), "alu")
    rotary = {"axis_y": ay, "axis_z": az, "chuck_face_x": -45.0, "chuck_r": 26.0, "tail_x": 80.0, "tail_r": 7.5, "max_diameter": 80, "max_length": 150}

machine(key="z1_4axis" if fourth else "z1", home=(hx, hy, nose_z), travel=(200, 200, 100), clearance=nose_z,
        nose=[{"name": "collet / nose", "r": 8.0, "h": 20.0}, {"name": "head", "r": 36.0, "h": 190.0}],
        table={"x": 206, "y": 206, "t": mdf_t + alu_t, "holes": holes, "hole_d": 5.5}, rotary=rotary, spoilboard=mdf_t,
        sources={"work area": "verified: makera.com 200×200×100", "gantry clearance": "verified: makera.com 115 mm",
                 "bed 206×206 and hole grid": "verified: Makera Z1-MDF-v2.1 (official bed file)",
                 "spindle nose Ø16×20": "reference: community simplified model — measure yours",
                 "head 65×70×190": "reference: community simplified model", "enclosure 355×435×449": "reference: community simplified model",
                 "4th axis chuck Ø52, axis 45 above bed, tailstock Ø14": "reference: community simplified model; Ø80×150 verified: makera.com",
                 "rods, carriages, bridge uprights, motors, cable chains": "estimated: from photos, representative only",
                 "probe dock and tool-length setter positions": "estimated: front rail of the bed (x −85 / +80, y −110) — measure yours",
                 "front window, hinge, LED strip, status light, feet, dust shoe": "estimated: from photos (dust shoe Ø60 not in the nose collision stack)"},
        notes="Moving bed in Y under a fixed rear bridge; the head moves X along the bridge and Z. Machine zero: head back-left, bed forward. "
              "Front window shown closed. Detail parts beyond the verified/reference numbers are estimated.")
