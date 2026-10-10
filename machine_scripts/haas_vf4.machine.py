# Haas VF-4 — machine model matched to Haas product photos (front 3/4, front, left, right, table, SMTC, spindle).
# Frame: X right, Y back (away from the operator), Z up; origin = centre of the table top at machine home.
# Table moves X on the saddle, saddle moves Y on the base, spindle head moves Z on the column.
# Photo scale: front photo, enclosure width (565 px) = 3050 mm -> 5.4 mm/px; the same scale puts the table top
# 890 mm above the floor, which agrees with the usual VF table height, and the cable chain top at ~3050 mm,
# agreeing with Haas' 121.8 in (3093 mm) operational height.
# Iteration 2 (after comparing renders with the photos): shallow sloped strip along the top front (right-side photo),
# door header under it, pendant light-grey side frame, status light on the right wing.

# ---------------- verified numbers ----------------
L, W = 1320.8, 457.2                       # table (Haas spec)
n_slots, sw, pitch = 5, 16.0, 80.0         # T-slots (Haas spec)
nose_min, nose_max = 106.7, 741.7          # spindle nose to table (Haas spec)
tx, ty, tz = 1270.0, 508.0, 635.0          # travels (haascnc.com)
pockets = 40                               # 40+1 side-mount tool changer
fl_r, fl_h, nut_r, nut_h, gauge = 31.75, 15.9, 25.0, 25.0, 101.6   # CAT40 flange, ER32 nut, gauge length
nose_z = nose_max

RED = "paint_red"                          # accent material (Haas red)

# ---------------- estimated layout (from the photos) ----------------
tt = 90.0                                  # table thickness
z_floor = -890.0                           # table top ~890 above the floor
sad_h = 170.0
z_bt = -tt - sad_h                         # saddle underside
ex = 3050.0; xw = ex / 2                   # enclosure width
y_front, y_face, y_back = -790.0, -650.0, 1000.0    # wing fronts, recessed door plane, enclosure back
z_base = z_floor + 460.0                   # dark base band top
z_roof = z_floor + 2000.0                  # enclosure roof
slope_d, slope_h = 220.0, 90.0             # sloped strip along the top front (right-side photo)
open_hw, z_sill, z_head = 795.0, -60.0, 990.0       # door opening half-width, sill, head
col_front, col_back, col_w, col_top = 640.0, 1250.0, 1120.0, 1450.0
head_z0, head_top, tower_top = nose_z + 60.0, nose_z + 340.0, z_floor + 2800.0

def zs(y):                                 # roof height on the front slope
    return z_roof - slope_h + slope_h * min(1.0, (y - y_front) / slope_d)

def cbox(x0, x1, y0, y1, z0, z1):
    return Pos((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2) * Box(x1 - x0, y1 - y0, z1 - z0)

def prism_x(pts_yz, x0, x1):
    return extrude(make_face(Polyline(*[(x0, y, z) for y, z in pts_yz], close=True)), amount=x1 - x0, dir=(1, 0, 0))

def prism_y(pts_xz, y0, y1):
    return extrude(make_face(Polyline(*[(x, y0, z) for x, z in pts_xz], close=True)), amount=y1 - y0, dir=(0, 1, 0))

def column_solid(m):
    return prism_y([(-col_w / 2 - m, z_bt - 40 - m), (col_w / 2 + m, z_bt - 40 - m), (col_w * 0.32 + m, col_top + m), (-col_w * 0.32 - m, col_top + m)],
                   col_front - m, col_back + m)

node("base")
node("saddle", "base", axis="y", mode="table")
node("table", "saddle", axis="x", mode="table", stock=True)
node("column", "base")
node("spindle", "column", axis="z")

# ---------------- table with T-slots and end drain troughs ----------------
table = cbox(-L / 2, L / 2, -W / 2, W / 2, -tt, 0)
for k in range(n_slots):
    y = (k - (n_slots - 1) / 2) * pitch
    table = table - cbox(-L / 2 - 1, L / 2 + 1, y - sw / 2, y + sw / 2, -20, 1) - cbox(-L / 2 - 1, L / 2 + 1, y - sw / 2 - 6, y + sw / 2 + 6, -32, -20)
for sx in (-1, 1):
    table = table - cbox(sx * (L / 2 - 30) - 15, sx * (L / 2 - 30) + 15, -W / 2 + 30, W / 2 - 30, -12, 1)
part("table", "table", table, "ground_steel", collision=True)
part("table skirts", "table", cbox(-L / 2 + 20, L / 2 - 20, -W / 2 + 10, W / 2 - 10, -tt - 40, -tt), "paint_dark")

# ---------------- saddle and telescopic Y way covers ----------------
saddle = cbox(-449, 449, -348, 348, z_bt, -tt - 40) + cbox(-502, 502, -278, 278, -tt - 40, -tt)
part("saddle", "saddle", saddle, "cast")
ycov = Compound([cbox(-450, 450, -470, -349, z_bt - 30, -115), cbox(-450, 450, 349, 470, z_bt - 30, -115)])
part("Y way covers", "saddle", ycov, "stainless", collision=True)
ycov_f = Compound([cbox(-470, 470, -630, -440, z_bt - 40, -135), cbox(-470, 470, 440, 615, z_bt - 40, -135)])
part("Y way covers (fixed)", "base", ycov_f, "stainless", collision=True)

# ---------------- base casting, dark sheet-metal base skirt, levelling feet ----------------
base = prism_x([(-600, z_floor + 120), (1260, z_floor + 120), (1260, z_bt - 40), (-520, z_bt - 40), (-600, z_bt - 120)], -760, 760)
for sx in (-300, 300):
    base = base + cbox(sx - 20, sx + 20, -560, 1200, z_bt - 40, z_bt)
part("base casting", "base", base, "cast")
skirt = cbox(-xw, xw, y_front, y_back, z_floor + 60, z_base) - cbox(-xw + 20, xw - 20, y_front + 20, y_back - 20, z_floor, z_base + 1)
part("base skirt", "base", skirt, "paint_dark")
feet = Compound([cyl_z(60, 60, at=(fx, fy), zmin=z_floor) for fx in (-xw + 120, 0, xw - 120) for fy in (y_front + 120, y_back - 120)])
part("levelling feet", "base", feet, "rubber")

# ---------------- chip slopes and chip auger ----------------
slopes = Compound([prism_y([(sx * 760, -430), (sx * (xw - 20), -150), (sx * (xw - 20), -130), (sx * 760, -410)], y_face + 20, col_front - 15)
                   for sx in (-1, 1)])
part("chip troughs", "base", slopes, "stainless", collision=True)
trough = cbox(-xw + 20, xw - 20, -630, -480, -500, -430) - cbox(-xw + 21, xw - 21, -615, -495, -485, -420)
part("chip auger trough", "base", trough, "stainless", collision=True)
auger = cyl_x(16, ex - 60, -555, -440, -xw + 30)
for i in range(int((ex - 80) / 90)):
    auger = auger + cyl_x(42, 5, -555, -440, -xw + 40 + 90 * i)
part("chip auger", "base", auger, "steel")
a0, a1 = Vector(-xw - 20, -555, -440), Vector(-1960, -555, 210)
dv = a1 - a0
chute = Pos(*((a0 + a1) * 0.5)) * Rot(0, math.degrees(math.atan2(dv.X, dv.Z)), 0) * Cylinder(75, dv.length)
chute = chute + cbox(-2250, -1880, -650, -460, 150, 300) + cbox(-1600, -1510, -640, -470, -560, -360)
part("auger discharge chute", "base", chute, "paint_dark")

# ---------------- column (A-frame casting), Z way cover, top cover ----------------
col = column_solid(0)
for sx in (-1, 1):
    col = col - prism_y([(sx * col_w * 0.54, 150), (sx * col_w * 0.54, nose_z + 500), (sx * col_w * 0.29, nose_z + 500), (sx * col_w * 0.375, 150)],
                        col_front + 120, col_back - 120)
part("column", "column", col, "cast", collision=True)
zc = cbox(-300, 300, 615, col_front, -90, 1060)
for i in range(12):
    zc = zc + cbox(-300, 300, 611, 615, -60 + 92 * i, -50 + 92 * i)
part("Z way cover", "column", zc, "stainless", collision=True)
part("column top cover", "column", cbox(-330, 330, col_front, 1150, z_roof + 1, col_top + 40) + cbox(-115, 115, 700, 930, col_top + 40, col_top + 230), "paint_dark")

# ---------------- spindle head: black housing, tall head cover with Haas logo, coolant ring ----------------
hx = prism_x([(-230, head_z0 + 60), (-170, head_z0), (610, head_z0), (610, head_top), (-230, head_top)], -230, 230)
hy = prism_y([(-230, head_z0 + 70), (-160, head_z0), (160, head_z0), (230, head_z0 + 70), (230, head_top + 1), (-230, head_top + 1)], -240, 620)
part("spindle head", "spindle", hx & hy, "paint_dark", collision=True)
part("head cover tower", "spindle", cbox(-195, 195, -205, 455, head_top, tower_top), "paint_dark")
part("Haas logo panel", "spindle", cbox(-150, 150, -211, -205, tower_top - 230, tower_top - 30), RED)
logo_h = (cbox(-95, -55, -214, -211, tower_top - 200, tower_top - 60) + cbox(55, 95, -214, -211, tower_top - 200, tower_top - 60)
          + cbox(-60, 60, -214, -211, tower_top - 140, tower_top - 115))
part("Haas logo", "spindle", logo_h, "paint_white")
part("spindle cartridge", "spindle", cyl_z(95, 40, zmin=nose_z + 20), "steel", collision=True)
part("spindle nose", "spindle", cyl_z(45, 20, zmin=nose_z), "steel", collision=True)
ring = Pos(0, 0, nose_z + 52) * Torus(140, 8)
part("coolant ring", "spindle", ring, "stainless", collision=True)
nozzles = Compound([Pos(125 * math.cos(math.radians(p)), 125 * math.sin(math.radians(p)), nose_z + 25) * Rot(0, 0, p) * Rot(0, 30, 0) * Cylinder(6, 60)
                    for p in (-150, -30, 30, 150)])
part("coolant nozzles", "spindle", nozzles, RED, collision=True)

# ---------------- CAT40 + ER32 holder (gauge line at the nose face) ----------------
part("CAT40 taper (in spindle)", "spindle", Pos(0, 0, nose_z) * Cone(22.225, 12.3, 68.0, align=(Align.CENTER, Align.CENTER, Align.MIN)), "stainless")
flange = cyl_z(fl_r, fl_h, zmin=nose_z - fl_h)
flange = flange - (cyl_z(fl_r + 1, 6, zmin=nose_z - fl_h + 5) - cyl_z(28.5, 6, zmin=nose_z - fl_h + 5))
body_h = gauge - fl_h - nut_h
hbody = cyl_z(22.5, 10, zmin=nose_z - fl_h - 10) + Pos(0, 0, nose_z - gauge + nut_h) * Cone(21.0, 22.5, body_h - 10, align=(Align.CENTER, Align.CENTER, Align.MIN))
nut = cyl_z(nut_r, nut_h, zmin=nose_z - gauge)
for px_, py_ in ((nut_r, 0), (-nut_r, 0), (0, nut_r), (0, -nut_r)):
    nut = nut - cbox(px_ - 4, px_ + 4, py_ - 4, py_ + 4, nose_z - gauge - 1, nose_z - gauge + nut_h + 1)
nut = nut - cyl_z(12, 4, zmin=nose_z - gauge - 1)
part("CAT40 ER32 holder", "spindle", flange + hbody, "stainless", collision=True)
part("ER32 nut", "spindle", nut, "steel", collision=True)

# ---------------- 40+1 side-mount tool changer (drum axis along X, left of the column) ----------------
def holder_x(y, z, x0):
    return cyl_x(fl_r, fl_h, y, z, x0) + cyl_x(22, 61, y, z, x0 + fl_h) + cyl_x(nut_r, nut_h, y, z, x0 + fl_h + 61)

pr, cy, cz, disc_x = 446.0, 300.0, 940.0, -880.0      # pitch radius from ~70 mm pocket spacing
car = cyl_x(pr + 40, 30, cy, cz, disc_x)
th_change = -math.acos(max(-1.0, min(1.0, -cy / pr)))
tools_ = []
for k in range(pockets):
    th = th_change + math.radians(360.0 / pockets * k)
    y, z = cy + pr * math.cos(th), cz + pr * math.sin(th)
    car = car + cyl_x(34, 40, y, z, disc_x - 40)
    if k:
        tools_.append(holder_x(y, z, disc_x + 30))
car = car + cyl_x(120, 120, cy, cz, disc_x - 160)
part("tool carousel", "column", car, "anodised", collision=True)
part("tool number plate", "column", cyl_x(300, 3, cy, cz, disc_x - 43) - cyl_x(140, 5, cy, cz, disc_x - 44), "paint_white")
part("carousel logo", "column", cyl_x(130, 3, cy, cz, disc_x - 43) - cyl_x(100, 5, cy, cz, disc_x - 44), RED)
part("carousel tools", "column", Compound(tools_), "stainless", collision=True)
px = disc_x + 220
tp = (cyl_z(36, 70, at=(px, 0), zmin=nose_z) + cyl_z(fl_r, fl_h, at=(px, 0), zmin=nose_z - fl_h)
      + cyl_z(22, 61, at=(px, 0), zmin=nose_z - fl_h - 61) + cyl_z(nut_r, nut_h, at=(px, 0), zmin=nose_z - gauge))
part("change pocket + tool", "column", tp, "stainless", collision=True)
ax = px + 330
arm = (cyl_z(60, 140, at=(ax, 0), zmin=nose_z - 60) + cbox(ax - 100, ax + 100, -100, 100, nose_z + 80, nose_z + 300)
       + cbox(ax - 35, ax + 35, -350, 350, nose_z - 60, nose_z - 20) + cyl_z(42, 45, at=(ax, 350), zmin=nose_z - 65) + cyl_z(42, 45, at=(ax, -350), zmin=nose_z - 65))
part("tool change arm", "column", arm, "steel", collision=True)
hood = prism_x([(-400, z_roof), (840, z_roof), (840, z_roof + 340), (-180, z_roof + 340), (-400, z_roof + 150)], -1150, -260) - column_solid(15)
part("tool changer cover", "base", hood, "paint_dark")

# ---------------- enclosure: side panels, roof with sloped front strip, back, door opening, side windows ----------------
enc = cbox(-xw, xw, y_face, y_back, z_base, z_roof) - cbox(-xw + 20, xw - 20, y_face + 20, y_back - 20, z_base - 1, z_roof - 20)
y_s1 = y_front + slope_d
enc = enc - prism_x([(y_face - 1, zs(y_face) - 0.5), (y_s1, z_roof), (y_s1, z_roof + 5), (y_face - 1, z_roof + 5)], -xw - 1, xw + 1)
enc = enc + prism_x([(y_face, zs(y_face) - 20), (y_s1, z_roof - 20), (y_s1, z_roof), (y_face, zs(y_face))], -xw, xw)   # sloped roof sheet
enc = enc - cbox(-open_hw, open_hw, y_face - 1, y_face + 21, z_sill, z_head)                 # door opening
for sx in (-1, 1):
    enc = enc - cbox(sx * xw - 30, sx * xw + 30, -410, 450, -107, 838)                        # side windows
enc = enc - cbox(-215, 215, -220, 470, z_roof - 30, z_roof + 10)                              # roof slot for the head cover
enc = enc - cbox(-1060, -700, cy - 500, cy + 500, z_roof - 30, z_roof + 10)                   # roof opening under the changer cover
enc = enc - column_solid(15)
part("enclosure", "base", enc, "paint_light", collision=True)
part("side windows", "base", Compound([cbox(sx * (xw - 10) - 3, sx * (xw - 10) + 3, -410, 450, -107, 838) for sx in (-1, 1)]), "acrylic")

wings = []
for sx in (-1, 1):
    x0, x1 = sorted((sx * open_hw, sx * xw))
    w = prism_x([(y_front, z_base), (y_face, z_base), (y_face, zs(y_face)), (y_front, zs(y_front))], x0, x1)
    w = w - cbox(x0 + (25 if sx < 0 else -1), x1 - (25 if sx > 0 else -1), y_front + 25, y_face + 1, z_base + 20, z_head + 5)   # door pocket
    wings.append(w)
hdr = prism_x([(y_front + 50, z_head), (y_face, z_head), (y_face, zs(y_face)), (y_front + 50, zs(y_front + 50))], -open_hw + 1, open_hw - 1)
part("front wings", "base", Compound(wings), "paint_light")
part("door header", "base", hdr, "paint_light")
part("lower front panel", "base", cbox(-open_hw, open_hw, y_front + 50, y_face, z_base, z_sill), "paint_dark")
part("status light", "base", cyl_z(28, 110, at=(xw - 330, y_face + 60), zmin=zs(y_face + 60) - 5), "paint_white")

# ---------------- sliding doors (shown part-open as in the photos) ----------------
frames, panes, handles = [], [], []
for sx, y0 in ((-1, -715), (1, -692)):
    x0, x1 = sorted((sx * 555, sx * 1385))
    frames.append(cbox(x0, x1, y0, y0 + 20, z_sill + 5, z_head - 5) - cbox(x0 + 80, x1 - 80, y0 - 1, y0 + 21, 80, 900))
    panes.append(cbox(x0 + 80, x1 - 80, y0 + 7, y0 + 13, 80, 900))
    hx_ = sx * 600
    handles.append(cyl_z(14, 420, at=(hx_, -745), zmin=60) + cyl_y(10, 30, hx_, 90, -745) + cyl_y(10, 30, hx_, 450, -745))
node("door L", "base", door="slide", direction=(-1, 0, 0), open=240)     # the doors slide apart into the wings until the opening is clear
node("door R", "base", door="slide", direction=(1, 0, 0), open=240)
for i, nd in enumerate(("door L", "door R")):
    part(f"front door {'L' if i == 0 else 'R'}", nd, frames[i], "paint_dark", collision=True)
    part(f"door window {'L' if i == 0 else 'R'}", nd, panes[i], "acrylic")
    part(f"door handle {'L' if i == 0 else 'R'}", nd, handles[i], "stainless")

# ---------------- left wing: VF-4 badge, toolholder shelf, tray ----------------
part("VF-4 badge", "base", cbox(-1490, -1070, -796, -790, 780, 960), RED)
vf = Pos(-1280, -796, 870) * Rot(90, 0, 0) * extrude(Text("VF4", font_size=150), 6)
part("VF-4 lettering", "base", vf, "paint_white")
shelf = cbox(-1445, -810, -940, -790, 640, 690)
for i in range(7):
    shelf = shelf - cyl_z(28, 60, at=(-1395 + 90 * i, -865), zmin=660)
part("toolholder shelf", "base", shelf, "paint_light")
tray = cbox(-1390, -1010, -950, -790, -225, 120) - cbox(-1375, -1025, -935, -789, -205, 121)
part("left tray", "base", tray, "stainless")

# ---------------- control pendant on its swing arm (right wing) ----------------
pcx = 1207.0
pend = cbox(pcx - 262, pcx + 262, -930, -820, 190, 920)
pend = pend + cyl_z(25, 190, at=(pcx - 150, -875), zmin=920) + cbox(pcx - 175, pcx - 125, -875, -790, 1080, 1130)
part("control pendant", "base", pend, "paint_dark")
part("pendant side frame", "base", cbox(pcx + 262, pcx + 300, -930, -820, 150, 960), "paint_light")
part("pendant screen", "base", cbox(pcx - 235, pcx + 235, -936, -930, 618, 875), "acrylic")
keys = [cbox(pcx - 80 + 36 * i, pcx - 52 + 36 * i, -938, -930, 560 - 30 * j, 582 - 30 * j) for i in range(9) for j in range(6)]
part("pendant keypad", "base", Compound(keys), "rubber")
part("jog handle", "base", cyl_y(40, 25, pcx - 180, 400, -955), "rubber")
part("e-stop", "base", cyl_y(30, 35, pcx - 180, 520, -965), RED)
ptray = cbox(pcx - 230, pcx + 230, -960, -800, -50, 130) - cbox(pcx - 215, pcx + 215, -945, -799, -35, 131) + cyl_z(45, 150, at=(pcx + 150, -900), zmin=-40)
part("pendant tray", "base", ptray, "stainless")
part("pendant hinge", "base", cyl_z(40, 120, at=(pcx - 150, -830), zmin=1040), "paint_light")

# ---------------- cable chain, roof box, rear electrical cabinet, coolant tank ----------------
links = []
ccx, ccz, cr, cyy = 560.0, 1800.0, 360.0, 300.0
for i in range(31):
    a = math.radians(180 - 6 * i)
    links.append(Pos(ccx + cr * math.cos(a), cyy, ccz + cr * math.sin(a)) * Rot(0, -(math.degrees(a) + 90), 0) * Box(36, 120, 70))
for j in range(12):
    links.append(cbox(ccx + cr - 35, ccx + cr + 35, cyy - 60, cyy + 60, ccz - 40 * (j + 1), ccz - 40 * j - 4))
part("cable chain", "base", Compound(links), "paint_dark")
part("roof box", "base", cbox(700, 1200, 150, 650, z_roof, z_roof + 190), "paint_light")
part("control cabinet", "base", cbox(600, 1450, y_back, y_back + 300, z_floor + 80, z_floor + 2050), "paint_light")
part("cabinet vents", "base", Compound([cbox(1440, 1452, y_back + 60 + 40 * i, y_back + 80 + 40 * i, -100, 500) for i in range(5)]), "paint_blue")
part("coolant tank", "base", cbox(-1300, -200, y_back + 20, 1700, z_floor, z_floor + 500), "paint_dark")

machine(key="haas", home=(0.0, 0.0, nose_z), travel=(tx, ty, tz), clearance=nose_max,
        nose=[{"name": "ER32 nut", "r": nut_r, "h": nut_h}, {"name": "holder body", "r": 22.5, "h": gauge - fl_h - nut_h},
              {"name": "CAT40 flange", "r": fl_r, "h": fl_h}, {"name": "spindle nose", "r": 45.0, "h": 20.0},
              {"name": "spindle cartridge", "r": 95.0, "h": 40.0}, {"name": "spindle head", "r": 230.0, "h": head_top - head_z0}],
        table={"x": L, "y": W, "t": tt, "slots": {"count": n_slots, "width": sw, "pitch": pitch}},
        sources={"table 1320.8 x 457.2, five 16 mm T-slots at 80 mm pitch": "verified: Haas spec sheet (via dealer listings)",
                 "nose-to-table 106.7-741.7": "verified: Haas spec sheet",
                 "travel 1270 x 508 x 635": "verified: haascnc.com",
                 "CAT40 flange 63.5 x 15.9, ER32 nut 50 x 25, gauge 101.6": "verified: ASME B5.50 / Techniks catalogue",
                 "40+1 side-mount tool changer": "verified: user / Haas VF-4 SMTC option",
                 "operational height 3093 (cable chain top ~3050 here)": "reference: Haas VF-4 machine layout drawing (haascnc.com MLD 01/2022)",
                 "enclosure width 3050, height 2000, wing depth 140, door opening 1590 x 1050, sill 830 above floor": "reference: Haas product photo (front), scaled 5.4 mm/px",
                 "enclosure depth 1790, side window 860 x 945, sloped front roof strip 220 x 90": "reference: Haas product photo (right side)",
                 "dark base band 460 high, lower front panel": "reference: Haas product photo (front)",
                 "pendant 524 x 730 at x +1207, screen 470 x 257, side frame, arm, tray": "reference: Haas product photo (front, front 3/4)",
                 "VF-4 badge, toolholder shelf, left tray, status light positions": "reference: Haas product photo (front, front 3/4)",
                 "head cover tower 390 x 660, top 2800 above floor, Haas logo": "reference: Haas product photo (front, right side)",
                 "spindle housing with chamfered corners, coolant ring with red nozzles": "reference: Haas product photo (spindle shot)",
                 "SMTC drum (axis along X), Haas logo hub, number ring, cover on the roof": "reference: Haas product photo (tool changer, front 3/4); drum pitch radius 446 estimated from ~70 mm pocket pitch",
                 "auger trough along the front, discharge chute out the left": "reference: Haas product photo (front, left angle); sizes estimated",
                 "Z way cover (ribbed stainless), Y covers": "reference: Haas product photo (table)",
                 "cable chain, roof box, rear cabinet": "reference: Haas product photo (front 3/4, right side); sizes estimated",
                 "table thickness 90, table top 890 above floor, castings, saddle, column 640-1250": "estimated",
                 "coolant tank": "estimated"},
        notes="Haas VF-4 with 40+1 side-mount tool changer. Table moves X on the saddle, saddle moves Y, spindle head moves Z on the column. "
              "Home = spindle at Z top over the table centre. Doors are shown part-open as in Haas' photos. "
              "Red accents (logos, nozzles, e-stop) use the 'anodised' material because the machine material set has no red.")
