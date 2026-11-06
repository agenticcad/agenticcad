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
# Sizes from Makera's simplified Z1 4-axis model (MakeraInc/CarveraProfiles Machine_Design_Files, the file the head was taken
# from; STEP → this frame = −(97.76, 9.74, −45) from its MDF solid) unless marked (photo): those are scaled from the owner's
# photos of their module (housing top 35 wide, tailstock block 35 wide as the scale) and taken on the larger side. The STEP
# draws the module with its chuck axis 9.74 mm in front of the bed centre, 45 above the MDF top, its housing face 92.76 left
# of the bed centre. The axis position is per machine: the firmware's coordinate.rotation_offset_y puts the centreline at
# anchor1_y + offset (85.5 default → bed Y −2.65; this user's config says 69.0 → bed Y −19.15) — machine.model["rotary"]
# (Machine page probing / Settings) overrides the reference numbers.
rotary = None
rotary_source = None
if fourth:
    # the chuck (a K02-50, from its marking) ships with its jaws REVERSED: teeth outward, the bar held on a step wall with its
    # end on the step (photos; the STEP draws them teeth-in). jaws="standard" in machine.model["rotary"] turns them back.
    rg = rotary_cfg(clearance=nose_z, axis_y=-9.74, axis_z=45.0, chuck_gap=None, jaw_reach=13.9, tail=True, tail_gap=0.0, jaws="reversed")
    ay, az = rg["axis_y"], rg["axis_z"]
    cf = -59.76                                                                      # the chuck BODY face (the jaws stand proud of it)
    hf = cf - 33.0                                                                   # the housing's chuck-side face (STEP: body face 33 along)
    jaw_reach, chuck_gap, jaws = rg["jaw_reach"], rg["chuck_gap"], rg["jaws"] or "reversed"
    if jaws == "standard" and chuck_gap is None:
        chuck_gap = 5.0                                                              # reference: 5 mm of bar in front of the body face
    node("chuck", "bed", axis="a", mode="table", stock=True, pivot=(0.0, ay, az))

    def upright(name, x0, length, top_hole=False, cut=None):
        """A 35-wide housing / tailstock block: 57 tall from 3 mm down in the rail's slot, 2.5 chamfers on its top edges, on a
        52-wide, 5-thick foot flange that sits on the rail (STEP solids 11 and 18)."""
        up = Pos(x0 + length / 2, ay, 5.5) * Box(length, 35, 57, align=(Align.CENTER, Align.CENTER, Align.MIN))
        up = chamfer(up.edges().filter_by(Axis.X).group_by(Axis.Z)[-1], 2.5)
        if top_hole:
            up = up - cyl_z(2.75, 10, at=(x0 + length / 2, ay), zmin=53)             # tapped hole in the top (photo)
        if cut is not None:
            up = up - cut
        part(name, "bed", up, "paint_light", collision=True)
        part(f"{name} flange", "bed", box(length, 52, 5, at=(x0 + length / 2, ay, 0), zmin=8.5), "paint_light", collision=True)

    # fixed: the 262 × 52 × 8.5 rail, the housing on its chuck end, the NEMA17 motor in front of and below the axis (its axis
    # 49 forward, 20 down), the belt plate and cover behind the housing, outlined round the two pulleys (all STEP, in the bed frame)
    rail = box(262, 52, 8.5, at=(hf + 96.31, ay, 0), zmin=0) - box(264, 30.5, 5.0, at=(hf + 96.31, ay, 0), zmin=3.5)   # the blocks' tongues sit in its slot
    for side in (-1, 1):                                                             # a slot along each side for the tailstock's clamp bolt (photo)
        rail = rail - box(264, 3.0, 3.0, at=(hf + 96.31, ay + side * 26.0, 0), zmin=2.5)
    part("4th axis rail", "bed", rail, "alu", collision=True)
    # the housing, with the 4th-axis probe pad: a 6 × 12 recess and a tapped hole at its top's chuck-side edge (photo)
    upright("4th axis housing", hf - 35.0, 35.0, cut=box(6, 12, 1.2, at=(hf - 3.5, ay, 0), zmin=61.4) + cyl_z(2.0, 10, at=(hf - 3.5, ay), zmin=53))

    def slab(name, xmin, length, yz, material):                                     # an outline about the axis (y, z) extruded along X
        part(name, "bed", Plane(origin=(xmin, ay, az), x_dir=(0, 1, 0), z_dir=(1, 0, 0)) * extrude(Polygon(*yz, align=None), length), material, collision=True)

    slab("4th axis belt plate", hf - 40.0, 5.0, [(21, -17), (21, 17), (17, 21), (-16, 21), (-67, 1), (-71, -3), (-71, -37), (-67, -41), (-33, -41), (-15.3, -36), (17, -21)], "paint_light")
    slab("4th axis belt cover", hf - 53.0, 13.0, [(21, -17), (21, 17), (17, 21), (-16, 21), (-67, 1), (-71, -3), (-71, -21.6), (-66, -26.6), (-14.7, -39.5), (14.7, -39.5), (17.5, -34.6)], "paint_light")
    mt = box(40, 43, 42, at=(hf - 15.0, ay - 49.5, 0), zmin=4.0)
    part("4th axis motor", "bed", chamfer(mt.edges().filter_by(Axis.X), 4), "paint_dark", collision=True)
    part("4th axis hand knob", "bed", cyl_x(11.0, 3.0, ay - 49.5, 25.0, hf - 56.0) + cyl_x(8.0, 5.0, ay - 49.5, 25.0, hf - 61.0), "anodised")   # on the motor's rear shaft, outside the cover (photo)
    # the chuck: Ø30 hub out of the housing, the Ø52 black knurled ring (its widest part), the Ø48.7 body with 4 jaw slots and
    # 4 tommy-bar holes (STEP; the body runs full size from the ring to the face: photos)
    part("chuck hub", "chuck", cyl_x(15.0, 6.5, ay, az, hf), "steel", collision=True)
    part("chuck ring", "chuck", cyl_x(26.0, 9.0, ay, az, cf - 26.5), "anodised", collision=True)
    body = cyl_x(24.35, 17.5, ay, az, cf - 17.5)
    for k in range(4):
        body = body - Pos(cf - 6.0, ay, az) * Rot(90 * k, 0, 0) * Pos(0, 15.0, 0) * Box(12.2, 20.0, 10.6)
        body = body - Pos(cf - 10.0, ay, az) * Rot(45 + 90 * k, 0, 0) * Pos(0, 20.0, 0) * Rot(90, 0, 0) * Cylinder(2.5, 14.0)
    part("chuck body", "chuck", body, "steel", collision=True)
    # 4 self-centring jaws (STEP, teeth-in): 10.43 wide, serrated face flat, stepped on the back — +25.53 for the 11.5 inside
    # the body, chamfering to +17.6 by 4 mm proud, +9.6 for the last 4.9 — tips 13.9 proud of the body face. Reversed (as fitted:
    # photos), the steps face the axis: the bar sits on a step, the serrated faces stand outside at r 26.5 when closed (photo).
    # Each jaw slides radially with the bar it grips: the viewer and the simulator place them from the program's stock
    # (machine_script.jaw_envelope; stock_diameter in machine.model["rotary"] sets where they are drawn here)
    jaw_r_in, jaw_w, jaw_closed = 6.2, 10.43, 26.5
    jaw_profile = [[-11.5, -3.0, 25.53], [-3.0, 4.0, 25.53, 17.6], [4.0, 9.0, 17.6], [9.0, jaw_reach, 9.6]]    # x from the face, r above the serrated face
    jaw_spec = {"jaws": jaws, "jaw_profile": jaw_profile, "jaw_r_in": jaw_r_in, "jaw_r_out_closed": jaw_closed, "jaw_travel": 10.0}
    jaw_env = jaw_envelope(jaw_spec, (rg.get("stock_diameter") or 0.0) / 2.0)
    jaw = extrude(Polygon((-11.5, 0), (-11.5, 25.53), (-3.0, 25.53), (4.0, 17.6), (9.0, 17.6), (9.0, 9.6), (jaw_reach, 9.6), (jaw_reach, 0),
                          align=None), amount=jaw_w / 2, both=True)
    teeth = [extrude(Polygon((x - 0.7, -0.01), (x + 0.7, -0.01), (x, 0.8), align=None), amount=jaw_w / 2 + 0.5, both=True)
             for x in [1.0 + 2.0 * k for k in range(7)]]                             # 7 V-serrations, 2 mm pitch, on the proud part (photos)
    jaw = jaw - Compound(teeth)
    if jaws == "reversed":
        jaw = mirror(jaw, about=Plane.XZ)                                            # teeth outward: the steps face the axis
    for k, nm in enumerate(("+Y", "+Z", "-Y", "-Z")):
        part(f"chuck jaw {nm}", "chuck", Pos(cf, ay, az) * Rot(90 * k, 0, 0) * Pos(0, jaw_env["ref"], 0) * jaw, "ground_steel", collision=True)
    # the tailstock: a 30-thick upright (photo: 30 against its 35 width; the STEP draws 19) like the housing's, its live centre
    # pointing at the chuck — a Ø18 threaded body 10.5 out of the block, then a 90° point 5 long (photo, larger side), a
    # Ø12 knurled clamp knob on a Ø6 neck behind the block, a bearing retainer with 4 screws round the centre (photos)
    tail_cone_len, tail_barrel, tail_r, tail_half = 5.0, 10.5, 9.0, 45.0
    tail_blk, knob_len = 30.0, 8.0
    if rg["tail"]:
        # tip at the nominal bar's far end (+ tail_gap); the viewer slides the tailstock to the program's stock, the simulator checks it there
        bar = rg["stock_length"] if rg["stock_length"] is not None else 120.0
        gap0 = chuck_gap if chuck_gap is not None else jaw_env["seat"]
        tail_tip = round(cf + gap0 + bar + rg["tail_gap"], 3)
        xb = tail_tip + tail_cone_len + tail_barrel
        upright("tailstock", xb, tail_blk, top_hole=True)
        screws = None
        for sy, sz in ((-12, -12), (12, -12), (-12, 12), (12, 12)):
            sc = cyl_x(2.2, 0.6, ay + sy, az + sz, xb - 0.6)
            screws = sc if screws is None else screws + sc
        part("tailstock retainer screws", "bed", screws, "stainless")
        part("tailstock centre", "bed", cyl_x(tail_r, tail_barrel, ay, az, tail_tip + tail_cone_len)
             + Plane(origin=(tail_tip + tail_cone_len, ay, az), z_dir=(-1, 0, 0))
             * Cone(tail_cone_len * math.tan(math.radians(tail_half)), 0, tail_cone_len, align=(Align.CENTER, Align.CENTER, Align.MIN)),
             "stainless", collision=True)
        part("tailstock clamp screw", "bed", cyl_x(3.0, 2.0, ay, az, xb + tail_blk) + cyl_x(6.0, knob_len - 2.0, ay, az, xb + tail_blk + 2.0), "anodised", collision=True)
    else:
        tail_tip = None
    # the simulator's solids, about the axis: x from the chuck body face (fixed_boxes) or the live centre's tip (tail_boxes),
    # y from the axis, z in the bed frame
    rotary = {"axis_y": ay, "axis_z": az, "chuck_face_x": cf, "chuck_r": 26.0, "chuck_len": 33.0, "chuck_gap": chuck_gap,
              "jaw_reach": jaw_reach, "jaw_w": jaw_w, "jaw_count": 4, "stock_diameter": rg.get("stock_diameter"), **jaw_spec,
              "jaw_r": jaw_env["outer_r"],
              "fixed_boxes": [{"name": "4th axis housing", "x": [-68.0, -33.0], "y": [-17.5, 17.5], "z": [5.5, 62.5]},
                              {"name": "4th axis housing flange", "x": [-68.0, -33.0], "y": [-26.0, 26.0], "z": [8.5, 13.5]},
                              {"name": "4th axis belt plate", "x": [-73.0, -68.0], "y": [-35.0, 21.0], "z": [4.0, 66.0]},
                              {"name": "4th axis motor", "x": [-68.0, -28.0], "y": [-71.0, -27.0], "z": [4.0, 46.0]},
                              {"name": "4th axis belt cover", "x": [-86.0, -73.0], "y": [-71.0, 21.0], "z": [5.5, 66.0]},
                              {"name": "4th axis rail", "x": [-67.69, 194.31], "y": [-26.0, 26.0], "z": [0.0, 8.5]}],
              "tail": rg["tail"], "tail_x": tail_tip, "tail_r": tail_r, "tail_gap": rg["tail_gap"],
              "tail_cone": {"half_angle": tail_half, "length": tail_cone_len}, "tail_barrel": tail_barrel,
              "tail_block": {"half_w": 17.5, "z0": 5.5, "z1": 62.5, "length": tail_blk},
              "tail_boxes": [{"name": "tailstock flange", "x": [tail_cone_len + tail_barrel, tail_cone_len + tail_barrel + tail_blk], "y": [-26.0, 26.0], "z": [8.5, 13.5]},
                             {"name": "tailstock clamp screw", "x": [tail_cone_len + tail_barrel + tail_blk, tail_cone_len + tail_barrel + tail_blk + knob_len],
                              "y": [-6.0, 6.0], "z": [az - 6.0, az + 6.0]}],
              "max_diameter": 80, "max_length": 150, "source": rg["source"]}
    rotary_source = rg["source_text"]

# what moves with the tool, from the nose bottom up, in the machine's X / Y about the spindle axis: the Ø16 nose, then the
# head cover — its front steps out along the chamfer (taken at each slab's widest) — with the stored wired probe on its
# right from 12 mm up, then the motor cap
_hd = [-32.5, 32.5]
machine(key="z1_4axis" if fourth else "z1", home=(hx, hy, nose_z), travel=(200, 159 if fourth else 200, 100), clearance=nose_z,   # the 4th-axis module takes 41 mm of Y (cam_data)
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
                 "4th axis Ø80×150, belt drive, 4-jaw self-centring chuck, live-centre tailstock": "verified: makera.com",
                 "4th axis rail 262×52×8.5, housing 35×35×57 on a 52 flange, NEMA17 motor 49 in front / 20 below the axis, belt plate and cover, "
                 "chuck Ø52 ring / Ø48.7 body 33 off the housing, 4 stepped jaws 10.43 wide, steps +9.6 / +17.6 / +25.53, tips 13.9 proud":
                     "verified: Makera's simplified Z1 4-axis model (MakeraInc/CarveraProfiles Machine_Design_Files)",
                 "jaws fitted reversed (teeth out, bar on a step), serrated faces at r 26.5 closed, tommy-bar holes, probe pad, hand knob":
                     "estimated: the owner's photos of their module (K02-50 chuck) — measure the jaws' outer radius with your bar in",
                 "jaw travel 10 mm (which step holds which bar: Ø16–38 on the first, Ø32–54 on the second)":
                     "estimated: Makera's 20–35 / over-35 mm jaw ranges — measure yours",
                 "tailstock block 30 thick, Ø18 centre body 10.5 out, 90° point 5 long, Ø12 clamp knob":
                     "estimated: the owner's photos, scaled by the block's 35 mm width, larger side (the STEP draws a 19 block and a Ø15 barrel) — measure yours",
                 **({"4th axis position": rotary_source} if rotary_source else {}),
                 "rods, carriages, bridge uprights, motors, cable chains": "estimated: from photos, representative only",
                 "tool-length sensor at the rear-right corner of the bed": "reference: reviews (fauxhammer); position (88, 88) and size estimated — measure yours",
                 "canopy hinges, LED bar, power button, vents, feet": "estimated: from photos"},
        notes="Moving bed in Y under a fixed rear bridge; the head moves X along the bridge and Z. Machine zero: head back-left, bed forward. "
              "Case: dark plinth, one smoked-acrylic canopy (front + roof + side triangles) hinged at the rear of its roof, shown closed; "
              "light grey side panels behind the diagonal. Detail parts beyond the verified/reference numbers are estimated.")
