# Makera Z1 (+ 4th axis) — detailed machine model.
# Frame: X right, Y back, Z up, origin = centre of the bed top (spoilboard top) with the bed at machine zero (forward).
# Verified: makera.com (work area 200×200×100, gantry clearance 115, 4th axis Ø80×150), Makera Z1-MDF-v2.1 bed file
# (206×206×6, 36 counterbored M5 holes), Makera's simplified Z1 model (MakeraInc/CarveraProfiles: spindle axis, Ø16×20 nose,
# 65×70 head 52 mm in front of the axis, motor cap, X carriage; enclosure 355×435×449, chuck Ø52). Case styling (plinth / canopy / diagonal split) from Makera's
# product photos; its proportions are ESTIMATED. Rods, carriages, chains, tool-length sensor size, LED, feet: estimated.

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
mdf = mdf - cyl_z(11, mdf_t + 2, at=(88.0, 88.0), zmin=-mdf_t - 1)        # cutout for the tool-length sensor (rear-right)
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

# tool-length sensor set into a cutout at the REAR-RIGHT corner of the bed (reviews: "the tool-length sensor occupies the
# rear-right corner"); the Z1 has no fixed probe dock — the wired 3D probe is a tool that goes in the collet. Estimated size.
tls_x, tls_y = 88.0, 88.0
part("tool length sensor housing", "bed", cyl_z(10, 15, at=(tls_x, tls_y), zmin=-6), "anodised", collision=True)
part("tool length sensor", "bed", cyl_z(6, 3, at=(tls_x, tls_y), zmin=9), "steel", collision=True)

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

# ------------------------------------------------------------------ head (VERIFIED: Makera's own simplified Z1 model,
# MakeraInc/CarveraProfiles ▸ Machine_Design_Files ▸ Makera-Z1_Simplified_4Axis.step, mapped to this frame): spindle
# axis x −107.8, y +101.0; Ø16 nose from 116.3 (nose bottom at Z home) to 136.3; head cover 65 wide (centred on the
# axis) × 70 deep — 52 in front of the axis, 18 behind — from 136.3 to 286.3, front-bottom chamfered 16 back × 19 up,
# R5 front edges; the motor cap on top is a cone Ø64 → Ø48.7 from 286.3 to 326.3 centred 16 mm in front of the axis;
# the X carriage block behind it is 83 × 48 from 136.3 to 332.3.
hx, hy, nose_z = -107.8, 101.0, 116.3          # home: spindle axis and nose bottom above the spoilboard (verified)
y_front, y_back = hy - 52.0, hy + 18.0
z_hb, z_ht = nose_z + 20.0, nose_z + 170.0     # head cover bottom / top

xc = box(83, 48, 196, at=(hx, y_back + 24.0, 0), zmin=z_hb)            # X carriage (moves in X only)
for z in (170, 300):
    for x0 in (hx - 38, hx + 8):
        xc = xc + (cyl_x(11, 30, rod_y, z, x0) - cyl_x(6.2, 32, rod_y, z, x0 - 1))       # bearing blocks on the X rods
part("X carriage", "head", xc, "alu", collision=True)

# head cover (moves in Z): the box with its front-bottom chamfer, R5 on the front edges
hc = box(65, 70, z_ht - z_hb, at=(hx, (y_front + y_back) / 2, 0), zmin=z_hb)
wedge = extrude(Plane.YZ.offset(hx - 40) * Polygon((y_front - 1, z_hb - 1), (y_front + 16.0, z_hb - 1), (y_front + 16.0, z_hb),
                                                    (y_front, z_hb + 19.0), (y_front - 1, z_hb + 19.0), align=None), amount=80)
hc = hc - wedge
try:
    front = hc.edges().filter_by(lambda e: max(v.Y for v in e.vertices()) < y_front + 16.5 and min(v.Z for v in e.vertices()) < z_ht - 1)
    hc = fillet(front, 5.0)
except Exception:
    pass                                                                 # sharp edges if OCCT can't blend the corner
part("head cover", "spindle", hc, "paint_light", collision=True)
# status LED strip (centre of the front face) and the vertical MAKERA lettering on its left (Makera's model / photos)
part("status LED", "spindle", box(5.5, 0.8, 72, at=(hx, y_front - 0.1, 0), zmin=z_hb + 21), "led_blue", collision=False)
try:                                                    # MAKERA, read top-to-bottom, on the front face's left (Makera's model, photo)
    txt = Text("MAKERA", font_size=8.6, align=(Align.MIN, Align.CENTER))           # ~6 mm tall, stretched to Makera's 45.5 mm
    bb = txt.bounding_box(); txt = scale(txt, by=(45.5 / max(bb.max.X - bb.min.X, 1e-6), 1, 1))
    lettering = extrude(Plane(origin=(hx - 19.5, y_front + 0.01, z_hb + 139.1), x_dir=(0, 0, -1), z_dir=(0, -1, 0)) * txt, amount=0.35)
    part("head lettering", "spindle", lettering, "lettering", collision=False)
except Exception:
    pass
# finned motor cap on top: a cone Ø64 → Ø48.7 with vertical fins (cap centred 16 mm in front of the axis)
cy_cap = hy - 15.94
cap = Pos(hx, cy_cap, z_ht) * Cone(32.0, 24.35, 40.0, align=(Align.CENTER, Align.CENTER, Align.MIN))
for k in range(28):
    a = 360.0 * k / 28
    cap = cap - Pos(hx, cy_cap, z_ht + 4) * Rot(0, 0, a) * Pos(30.0, 0, 0) * Box(7.0, 1.6, 40.0, align=(Align.CENTER, Align.CENTER, Align.MIN))
part("motor cap", "spindle", cap, "anodised", collision=True)
# tool-change lever (front, Makera's photos; not in Makera's model, estimated, well above any cut)
lev = Pos(hx - 12, y_front - 4, z_hb + 95) * Rot(0, 40, 0) * Cylinder(3.0, 44, align=(Align.CENTER, Align.CENTER, Align.MIN))
lev = lev + Pos(hx - 12 + 44 * 0.643, y_front - 4, z_hb + 95 + 44 * 0.766) * Sphere(5.0)
part("tool-change lever", "spindle", lev, "ground_steel", collision=False)
# wired probe stored in its holder on the right side of the head (photos: down to ~12 mm above the head bottom; estimated)
part("wired probe holder", "spindle", box(19, 22, 108, at=(hx + 42.0, y_front + 11, 0), zmin=z_hb + 12), "acrylic", collision=True)
part("wired probe", "spindle", cyl_z(4.5, 60, at=(hx + 42.0, y_front + 11), zmin=z_hb + 14) + cyl_z(1.0, 4, at=(hx + 42.0, y_front + 11), zmin=z_hb + 10),
     "paint_dark", collision=True)
# spindle nose Ø16 × 20 (Makera's model) with its collet ring
part("spindle nose", "spindle", cyl_z(8, 14, at=(hx, hy), zmin=nose_z + 6) - cyl_z(2.2, 16, at=(hx, hy), zmin=nose_z), "stainless", collision=True)
part("collet ring", "spindle", cyl_z(6.5, 6, at=(hx, hy), zmin=nose_z) - cyl_z(1.8, 8, at=(hx, hy), zmin=nose_z - 1), "steel", collision=True)

# ------------------------------------------------------------------ case (from Makera's photos)
# One rounded shell 355×435×449 (reference), split three ways:
#   * dark charcoal plinth: the front lower band, in front of the side diagonal
#   * smoked-acrylic canopy: the whole front above the plinth + the roof + the side triangles in front of the diagonal
#     (one piece, hinged at the rear end of its roof so it lifts up; shown closed)
#   * light grey sheet panels ('enclosure'): the side trapezoids behind the diagonal, the rear roof strip, rear and floor
ex, ey, eh, ecy, ez0, wall = 355.0, 435.0, 449.0, 95.0, -99.0, 3.0
yf = ecy - ey / 2                                  # front face y = -122.5
yr = ecy + ey / 2                                  # rear face  y = +312.5
ztop = ez0 + eh                                    # roof z = 350
r_top, r_bot = 32.0, 12.0                          # estimated: rounded front-top / roof / vertical edges, softer base edge
plinth_top = -10.0                                 # estimated: plinth ≈ 89 mm tall (photos: ~1/5 of the height)
rim_h = 12.0                                       # estimated: black rim along the canopy's lower edge
diag_y0, diag_y1 = yf + 40.0, yf + 355.0           # estimated: side diagonal from (40 back, floor) to (355 back, roof)

def rounded_block(w, d, h, zmin, r, rb):
    b = box(w, d, h, at=(0, ecy, 0), zmin=zmin)
    b = fillet(b.edges().filter_by(Axis.Z) + b.faces().sort_by(Axis.Z)[-1].edges(), r)
    b = fillet(b.faces().sort_by(Axis.Z)[0].edges(), rb)
    return b

outer = rounded_block(ex, ey, eh, ez0, r_top, r_bot)
inner = rounded_block(ex - 2 * wall, ey - 2 * wall, eh - 2 * wall, ez0 + wall, r_top - wall, r_bot - wall / 2)
shell = outer - inner

def diag_y(z, dy=0.0):
    return diag_y0 + (z - ez0) * (diag_y1 - diag_y0) / eh + dy

def front_of_diagonal(dy=0.0):
    """Prism (along X) of everything in front of the side diagonal, shifted dy in Y."""
    za, zb = ez0 - 60, ztop + 60
    pts = [(yf - 60, za), (diag_y(za, dy), za), (diag_y(zb, dy), zb), (yf - 60, zb)]
    return extrude(Plane.YZ * make_face(Polyline(*pts, close=True)), amount=ex, both=True)

front = front_of_diagonal()
diag_band = front - front_of_diagonal(-9.0)        # 9 mm black edge of the canopy along the diagonal and the roof joint
below_plinth = box(ex + 20, ey + 120, plinth_top - ez0 + 60, at=(0, ecy, 0), zmin=ez0 - 60)
rim_slab = box(ex + 20, ey + 120, rim_h, at=(0, ecy, 0), zmin=plinth_top)

# grey side panels + rear + roof strip behind the canopy
part("enclosure", "base", shell - front, "paint_light", collision=True)

# dark plinth with the shallow front handle recess under the canopy lip
plinth = shell & front & below_plinth
plinth = plinth - box(140, 4, 10, at=(0, yf, 0), zmin=plinth_top - 10)
part("plinth", "base", plinth, "paint_dark", collision=True)

# smoked acrylic canopy (front + roof + side triangles), with its black rim and diagonal edge
canopy_zone = shell & front - below_plinth
node("canopy", "base", door="hinge", pivot=(0.0, diag_y(ztop) + 8, ztop + 3.5), direction=(1, 0, 0), open=-80)   # lifts up about the rear hinge line
part("canopy", "canopy", canopy_zone - rim_slab - diag_band, "acrylic", collision=True)
part("canopy frame", "canopy", (canopy_zone & rim_slab) + (canopy_zone & diag_band), "paint_dark", collision=True)
for hxp in (-60, 60):                              # rear hinges where the canopy roof meets the grey roof strip
    hinge = box(24, 16, 6, at=(hxp, diag_y(ztop) + 2, 0), zmin=ztop - 1) + cyl_x(3.5, 24, diag_y(ztop) + 8, ztop + 3.5, hxp - 12)
    part(f"canopy hinge {'L' if hxp < 0 else 'R'}", "base", hinge, "paint_dark")

# plinth front: pill power button + MAKERA badge
pill_z = plinth_top - 32
pill = Plane(origin=(0, yf, pill_z), x_dir=(1, 0, 0), z_dir=(0, -1, 0)) * extrude(SlotOverall(32, 12), 1.5)
part("power button", "base", pill, "paint_dark")
part("power button ring", "base", Plane(origin=(0, yf + 0.2, pill_z), x_dir=(1, 0, 0), z_dir=(0, -1, 0))
     * extrude(SlotOverall(36, 16) - SlotOverall(32.5, 12.5), 0.8), "stainless")
try:
    logo = Plane(origin=(0, yf, pill_z - 26), x_dir=(1, 0, 0), z_dir=(0, -1, 0)) * extrude(
        Text("MAKERA", 13, font_style=FontStyle.BOLD, align=(Align.CENTER, Align.CENTER)), 0.6)
    part("logo", "base", logo, "paint_white")
except Exception:
    pass

# 'MAKERA Z1' along the diagonal on both grey side panels (letters' tops toward the canopy)
ang = math.atan2(eh, diag_y1 - diag_y0)
c, s = math.cos(ang), math.sin(ang)
zt = ez0 + 0.42 * eh                               # estimated: lettering centred a little below mid-height
yt = diag_y(zt) + 22.0 / s                         # 22 mm off the diagonal into the grey panel
for side in (1, -1):
    xd = (0, c, s) if side > 0 else (0, -c, -s)
    try:
        txt = Plane(origin=(side * ex / 2, yt, zt), x_dir=xd, z_dir=(side, 0, 0)) * extrude(
            Text("MAKERA  Z1", 27, font_style=FontStyle.BOLD, align=(Align.CENTER, Align.CENTER)), 0.6)
        part(f"side lettering {'R' if side > 0 else 'L'}", "base", txt, "paint_white")
    except Exception:
        pass

# LED light bar under the roof along the top rear (behind the canopy, in front of the bridge beam)
part("LED bar holder", "base", box(300, 14, 6, at=(0, diag_y(ztop) - 30, 0), zmin=ztop - wall - 6), "paint_dark")
part("LED bar", "base", box(290, 8, 2, at=(0, diag_y(ztop) - 30, 0), zmin=ztop - wall - 8), "paint_white")

# rear vent grille
for k in range(8):
    part(f"rear vent {k}", "base", box(150, 1, 4, at=(0, yr + 0.5, 0), zmin=170 + k * 10), "paint_dark")

# rubber feet
for fx in (-150, 150):
    for fy in (yf + 30, yr - 30):
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

# what moves with the tool, from the nose bottom up, in the machine's X / Y about the spindle axis: the Ø16 nose, then the
# head cover — its front steps out along the chamfer (taken at each slab's widest) — with the stored wired probe on its
# right from 12 mm up, then the motor cap
_hd = [-32.5, 32.5]
machine(key="z1_4axis" if fourth else "z1", home=(hx, hy, nose_z), travel=(200, 200, 100), clearance=nose_z,
        nose=[{"name": "collet / nose", "r": 8.0, "h": 20.0},
              {"name": "head", "box": _hd + [-41.4, 18.0], "h": 6.3},
              {"name": "head", "box": _hd + [-46.8, 18.0], "h": 5.7},
              {"name": "head + wired probe", "boxes": [_hd + [-52.0, 18.0], [32.5, 51.5, -52.0, -30.0]], "h": 138.0},
              {"name": "motor cap", "box": [-32.0, 32.0, -47.94, 16.06], "h": 40.0}],
        table={"x": 206, "y": 206, "t": mdf_t + alu_t, "holes": holes, "hole_d": 5.5}, rotary=rotary, spoilboard=mdf_t,
        sources={"work area": "verified: makera.com 200×200×100", "gantry clearance": "verified: makera.com 115 mm",
                 "bed 206×206 and hole grid": "verified: Makera Z1-MDF-v2.1 (official bed file)",
                 "spindle axis, Ø16×20 nose, head 65×70 (52 in front of the axis / 18 behind) from 136.3 to 286.3, front chamfer, motor cap, X carriage block":
                     "verified: Makera's simplified Z1 model (MakeraInc/CarveraProfiles Machine_Design_Files)",
                 "quick-change lever, fan cooling (no dust shoe), status LED on the head": "verified: Makera Z1 quick start (wiki.makera.com/en/Z1/QuickStart)",
                 "wired probe holder on the head's right side": "estimated: Makera product photo — measure yours",
                 "tool-change lever, LED strip size, lettering": "estimated: Makera product photos (not in the collision stack)",
                 "enclosure 355×435×449": "reference: Makera's simplified Z1 model",
                 "case styling (plinth, canopy, diagonal split, lettering)": "reference: Makera product photos",
                 "plinth height 89, diagonal 40→355 mm back, edge radii 32/12, rim 12": "estimated: scaled from Makera product photos",
                 "4th axis chuck Ø52, axis 45 above bed, tailstock Ø14": "reference: community simplified model; Ø80×150 verified: makera.com",
                 "rods, carriages, bridge uprights, motors, cable chains": "estimated: from photos, representative only",
                 "tool-length sensor at the rear-right corner of the bed": "reference: reviews (fauxhammer); position (88, 88) and size estimated — measure yours",
                 "canopy hinges, LED bar, power button, vents, feet": "estimated: from photos"},
        notes="Moving bed in Y under a fixed rear bridge; the head moves X along the bridge and Z. Machine zero: head back-left, bed forward. "
              "Case: dark plinth, one smoked-acrylic canopy (front + roof + side triangles) hinged at the rear of its roof, shown closed; "
              "light grey side panels behind the diagonal. Detail parts beyond the verified/reference numbers are estimated.")
