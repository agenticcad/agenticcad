# Haas vertical machining centre — machine model script (VF-2, VF-2SS, VF-4, Mini Mill, TM-1 by the record's name).
# Frame: X right, Y back (away from the operator), Z up; origin = centre of the table top at machine home.
# Table moves X on the saddle, saddle moves Y on the base, spindle head moves Z on the column.
# Verified: table size and T-slots, nose-to-table range and travels (Haas spec sheets), CAT40 flange Ø63.5 × 15.9,
# ER32 nut Ø50 × 25, gauge length 101.6 (ASME B5.50 / Techniks). Castings, column, head, changer, enclosure and
# pendant are representative proportions scaled from the verified numbers (estimated).
spec = next((v for k, v in HAAS.items() if k.lower() in machine_record.name.lower()), HAAS["Haas VF-2"])
L, W = spec["table"]; n_slots, sw, pitch = spec["slots"]; nose_min, nose_max = spec["nose"]; tx, ty, tz = spec["travel"]
pockets, atc_kind = spec.get("atc", (20, "carousel"))
tt = 90.0                                   # table thickness (estimated)
nose_z = nose_max                           # nose bottom at Z top (home)
fl_r, fl_h, nut_r, nut_h, gauge = 31.75, 15.9, 25.0, 25.0, 101.6   # CAT40 flange, ER32 nut, gauge length (verified)

# ---- representative proportions, all relative to the verified table / travel / nose numbers (estimated)
z_floor = -890.0                            # table top ~890 above the floor
sad_h = 170.0
z_bt = -tt - sad_h                          # saddle underside / Y way top
base_w = L + 200
base_y0, base_y1 = -(W / 2 + 670), W / 2 + 410 + 610 + 50
col_front, col_back = W / 2 + 410, W / 2 + 410 + 610
col_w, col_top = min(L * 0.85, 1120.0), nose_z + 900.0
head_z0, head_h = nose_z + 60.0, 640.0

def prism_x(pts_yz, x0, x1):                # YZ profile extruded along +X
    return extrude(make_face(Polyline(*[(x0, y, z) for y, z in pts_yz], close=True)), amount=x1 - x0, dir=(1, 0, 0))

def prism_y(pts_xz, y0, y1):                # XZ profile extruded along +Y
    return extrude(make_face(Polyline(*[(x, y0, z) for x, z in pts_xz], close=True)), amount=y1 - y0, dir=(0, 1, 0))

node("base")
node("saddle", "base", axis="y", mode="table")
node("table", "saddle", axis="x", mode="table", stock=True)
node("column", "base")
node("spindle", "column", axis="z")

# ---------------- table with T-slots and end drain troughs ----------------
table = box(L, W, tt, zmin=-tt)
for k in range(n_slots):
    y = (k - (n_slots - 1) / 2) * pitch
    table = table - box(L + 2, sw, 20, at=(0, y, 0), zmin=-20) - box(L + 2, sw + 12, 12, at=(0, y, 0), zmin=-32)
for sx in (-1, 1):
    table = table - box(30, W - 60, 12, at=(sx * (L / 2 - 30), 0, 0), zmin=-12)
part("table", "table", table, "ground_steel", collision=True)

# ---------------- saddle (Y) with telescopic front way covers ----------------
saddle = box(min(L * 0.68, 900.0), W + 240, sad_h, zmin=z_bt) + box(L * 0.76, W + 100, 40, zmin=-tt - 40)
part("saddle", "saddle", saddle, "paint_light")
covers = (box(L * 0.65, 130, 150, at=(0, -(W / 2 + 120) - 65, 0), zmin=z_bt - 35)
          + box(L * 0.62, 130, 125, at=(0, -(W / 2 + 250) - 65, 0), zmin=z_bt - 35))
part("Y way covers", "saddle", covers, "stainless")

# ---------------- base casting with Y guides and levelling feet ----------------
base = prism_x([(base_y0, z_floor + 80), (base_y1, z_floor + 80), (base_y1, z_bt - 40), (base_y0 + 80, z_bt - 40), (base_y0, z_bt - 140)],
               -base_w / 2, base_w / 2)
base = base - box(base_w * 0.8, 300, 260, at=(0, base_y0, 0), zmin=z_floor + 180)             # front coolant / chip recess
for sx in (-300, 300):
    base = base + box(40, base_y1 - base_y0 - 100, 40, at=(sx, (base_y0 + base_y1) / 2, 0), zmin=z_bt - 40)
for fx in (-(base_w / 2 - 60), base_w / 2 - 60):
    for fy in (base_y0 + 50, base_y1 - 50):
        base = base + cyl_z(55, 80, at=(fx, fy), zmin=z_floor)
part("base casting", "base", base, "paint_light")

# ---------------- enclosure extents (needed by the chip troughs) ----------------
ex, ey0, ey1, ez0, ez1, et = tx + 1930.0, -(W / 2 + 770), col_back + 200, z_floor + 80, nose_z + 1300.0, 20.0
tr = Compound([prism_y([(sx * base_w / 2, -520), (sx * (ex / 2 - 20), -170), (sx * (ex / 2 - 20), -150), (sx * base_w / 2, -500)], ey0 + 20, col_back)
               for sx in (-1, 1)])
part("chip troughs", "base", tr, "stainless")

# ---------------- column (A-frame casting with cored side pockets and Z guides) ----------------
col = prism_y([(-col_w / 2, z_bt - 40), (col_w / 2, z_bt - 40), (col_w * 0.32, col_top), (-col_w * 0.32, col_top)], col_front, col_back)
for sx in (-1, 1):
    col = col - prism_y([(sx * col_w * 0.54, 150), (sx * col_w * 0.54, nose_z + 500), (sx * col_w * 0.29, nose_z + 500), (sx * col_w * 0.375, 150)],
                        col_front + 120, col_back - 120)
for sx in (-220, 220):
    col = col + box(45, 30, col_top - z_bt - 200, at=(sx, col_front - 15, 0), zmin=z_bt + 100)
col = col + box(230, 230, 250, at=(0, col_front + 180, 0), zmin=col_top)                    # Z servo motor
part("column", "column", col, "paint_light", collision=True)

# ---------------- spindle head (Z): housing, belt guard, motor, cartridge, nose ----------------
head = prism_x([(-180, head_z0 + 40), (-130, head_z0), (col_front - 30, head_z0), (col_front - 30, head_z0 + head_h),
                (-80, head_z0 + head_h), (-180, head_z0 + head_h - 100)], -230, 230)
part("spindle head", "spindle", head, "paint_light", collision=True)
top = (box(320, 520, 100, at=(0, 170, 0), zmin=head_z0 + head_h) + cyl_z(140, 280, at=(0, 330), zmin=head_z0 + head_h + 100)
       + cyl_z(70, 120, at=(0, 0), zmin=head_z0 + head_h + 100))
part("spindle motor", "spindle", top, "paint_dark")
part("spindle cartridge", "spindle", cyl_z(95, 40, zmin=nose_z + 20), "steel", collision=True)
part("spindle nose", "spindle", cyl_z(45, 20, zmin=nose_z), "steel", collision=True)

# ---------------- CAT40 + ER32 holder (gauge line at the nose face) ----------------
part("CAT40 taper (in spindle)", "spindle", Pos(0, 0, nose_z) * Cone(22.225, 12.3, 68.0, align=(Align.CENTER, Align.CENTER, Align.MIN)), "stainless")
flange = cyl_z(fl_r, fl_h, zmin=nose_z - fl_h)
flange = flange - (cyl_z(fl_r + 1, 6, zmin=nose_z - fl_h + 5) - cyl_z(28.5, 6, zmin=nose_z - fl_h + 5))          # V-groove
body_h = gauge - fl_h - nut_h
body = cyl_z(22.5, 10, zmin=nose_z - fl_h - 10) + Pos(0, 0, nose_z - gauge + nut_h) * Cone(21.0, 22.5, body_h - 10, align=(Align.CENTER, Align.CENTER, Align.MIN))
nut = cyl_z(nut_r, nut_h, zmin=nose_z - gauge)
for px_, py_ in ((nut_r, 0), (-nut_r, 0), (0, nut_r), (0, -nut_r)):
    nut = nut - box(8, 8, nut_h + 2, at=(px_, py_, 0), zmin=nose_z - gauge - 1)                                  # spanner slots
nut = nut - cyl_z(12, 4, zmin=nose_z - gauge - 1)
part("CAT40 ER32 holder", "spindle", flange + body, "stainless", collision=True)
part("ER32 nut", "spindle", nut, "steel", collision=True)

# ---------------- tool changer ----------------
def holder_x(y, z, x0):                     # a CAT40 holder lying along +X from x0 (flange, body, nut)
    return cyl_x(fl_r, fl_h, y, z, x0) + cyl_x(22, 61, y, z, x0 + fl_h) + cyl_x(nut_r, nut_h, y, z, x0 + fl_h + 61)

pr = max(pockets * 72.0 / (2 * math.pi), 120.0)                                  # pitch radius from the pocket count
if atc_kind == "smtc":
    # side-mount drum left of the column, axis along X; the pocket at the change position tilts down to hang the tool
    cy, cz, disc_x = W / 2 + 70, nose_z + 358.0, -(tx / 2 + 245)
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
    part("carousel tools", "column", Compound(tools_), "stainless", collision=True)
    px = disc_x + 220                                                           # tilted pocket: tool hanging vertically at nose height
    tp = (cyl_z(36, 70, at=(px, 0), zmin=nose_z) + cyl_z(fl_r, fl_h, at=(px, 0), zmin=nose_z - fl_h)
          + cyl_z(22, 61, at=(px, 0), zmin=nose_z - fl_h - 61) + cyl_z(nut_r, nut_h, at=(px, 0), zmin=nose_z - gauge))
    part("change pocket + tool", "column", tp, "stainless", collision=True)
    part("carousel cover", "column", cyl_x(pr + 140, 300, cy, cz, disc_x - 40) - cyl_x(pr + 120, 300, cy, cz, disc_x - 20), "paint_white", collision=True)
    part("carousel bracket", "column", box(240, 150, 220, at=(disc_x + 380, col_front + 300, cz + 20)), "paint_light")
    ax = px + 330                                                               # double-arm exchanger, parked along Y
    arm = (cyl_z(60, 140, at=(ax, 0), zmin=nose_z - 60) + box(200, 200, 260, at=(ax, 0, 0), zmin=nose_z + 80)
           + box(70, 700, 40, at=(ax, 0, 0), zmin=nose_z - 60) + cyl_z(42, 45, at=(ax, 350), zmin=nose_z - 65) + cyl_z(42, 45, at=(ax, -350), zmin=nose_z - 65))
    part("tool change arm", "column", arm, "steel", collision=True)
else:
    # umbrella carousel: a horizontal disc beside the head on the left of the column, tools hanging from its pockets
    pr = max(pr, 160.0)
    cx_, cy_, cz_ = -(tx / 2 + pr + 120), col_front - 120.0, nose_z + 120.0
    car = cyl_z(pr + 40, 25, at=(cx_, cy_), zmin=cz_) + cyl_z(60, 180, at=(cx_, cy_), zmin=cz_ + 25)
    tools_ = []
    for k in range(pockets):
        th = math.radians(360.0 / pockets * k)
        x, y = cx_ + pr * math.cos(th), cy_ + pr * math.sin(th)
        car = car - cyl_z(fl_r + 2, 26, at=(x, y), zmin=cz_ - 0.5)
        tools_.append(cyl_z(fl_r, fl_h, at=(x, y), zmin=cz_ - fl_h) + cyl_z(22, 61, at=(x, y), zmin=cz_ - fl_h - 61) + cyl_z(nut_r, nut_h, at=(x, y), zmin=cz_ - gauge))
    part("tool carousel", "column", car, "anodised", collision=True)
    part("carousel tools", "column", Compound(tools_), "stainless", collision=True)
    part("carousel bracket", "column", box(200, 160, 120, at=(cx_ + pr + 60, col_front + 80, cz_ + 120)), "paint_light")
    part("carousel cover", "column", cyl_z(pr + 150, 60, at=(cx_, cy_), zmin=cz_ + 25) - cyl_z(pr + 60, 60, at=(cx_, cy_), zmin=cz_ + 25), "paint_white", collision=True)

# ---------------- enclosure, sliding doors with windows, pendant, cabinet, beacon ----------------
eyc, ed = (ey0 + ey1) / 2, ey1 - ey0
enc = box(ex, ed, ez1 - ez0, at=(0, eyc, 0), zmin=ez0) - box(ex - 2 * et, ed - 2 * et, ez1 - ez0 - et + 1, at=(0, eyc, 0), zmin=ez0 - 1)
open_w, dh, dz0 = min(1900.0, ex - 700), min(1560.0, ez1 - 280), -180.0
enc = enc - box(open_w, 3 * et, dh - 60, at=(0, ey0 + et / 2, 0), zmin=dz0 + 30)                               # door opening
for sx in (-1, 1):
    enc = enc - box(3 * et, 500, 400, at=(sx * ex / 2, 300, 0), zmin=500)                                        # side windows
part("enclosure", "base", enc, "panel", collision=True)
part("side windows", "base", Compound([box(4, 500, 400, at=(sx * (ex / 2 - 10), 300, 0), zmin=500) for sx in (-1, 1)]), "acrylic")
dw = open_w / 2 + 50
frames, panes, handles = [], [], []
for sx, y0 in ((-1, ey0 - 44), (1, ey0 - 22)):                                                                  # left door on the outer track
    cx = sx * (dw / 2 - 10)
    pane_w, pane_h = dw * 0.75, min(900.0, dh - 500)
    frames.append(box(dw, 18, dh, at=(cx, y0 + 9, 0), zmin=dz0) - box(pane_w, 30, pane_h, at=(cx, y0 + 9, 0), zmin=250))
    panes.append(box(pane_w, 6, pane_h, at=(cx, y0 + 9, 0), zmin=250))
    handles.append(box(40, 40, 500, at=(cx - sx * (dw / 2 - 60), y0 - 20, 0), zmin=350))
door_slide = max(200.0, ex / 2 - dw)                                         # until the door's outer edge reaches the enclosure side
node("door L", "base", door="slide", direction=(-1, 0, 0), open=door_slide)  # the doors slide apart into the wings
node("door R", "base", door="slide", direction=(1, 0, 0), open=door_slide)
for i, nd in enumerate(("door L", "door R")):
    part(f"front door {'L' if i == 0 else 'R'}", nd, frames[i], "panel", collision=True)
    part(f"door window {'L' if i == 0 else 'R'}", nd, panes[i], "acrylic")
    part(f"door handle {'L' if i == 0 else 'R'}", nd, handles[i], "paint_dark")
pcx, pcy, pcz = ex / 2 - 280, ey0 - 200, 650.0                                                                    # pendant on a swing arm, front right
pend = (box(560, 120, 480, at=(pcx, pcy, pcz)) + cyl_z(25, 900, at=(pcx + 210, pcy + 140), zmin=pcz - 50)
        + box(60, 160, 60, at=(pcx + 210, pcy + 60, pcz + 150)) + box(60, 60, 60, at=(pcx + 210, pcy + 170, pcz + 820)))
part("control pendant", "base", pend, "paint_dark")
part("pendant screen", "base", box(400, 6, 250, at=(pcx - 40, pcy - 63, pcz + 85)), "acrylic")
part("pendant keypad", "base", Compound([box(34, 8, 22, at=(pcx - 220 + 40 * i, pcy - 64, pcz - 80 - 32 * j)) for i in range(10) for j in range(4)]), "rubber")
part("jog handle + e-stop", "base", cyl_y(40, 25, pcx + 210, pcz - 120, pcy - 85) + cyl_y(26, 35, pcx + 210, pcz + 160, pcy - 95), "rubber")
part("control cabinet", "base", box(min(900.0, ex * 0.3), 380, 1700, at=(ex / 2 - min(900.0, ex * 0.3) / 2 - 250, ey1 + 190, 0), zmin=z_floor + 50), "paint_light")
part("status beacon", "base", cyl_z(30, 220, at=(ex / 2 - 150, ey0 + 100), zmin=ez1), "paint_dark")

machine(key="haas", home=(0.0, 0.0, nose_z), travel=(tx, ty, tz), clearance=nose_max,
        nose=[{"name": "ER32 nut", "r": nut_r, "h": nut_h}, {"name": "holder body", "r": 22.5, "h": gauge - fl_h - nut_h},
              {"name": "CAT40 flange", "r": fl_r, "h": fl_h}, {"name": "spindle nose", "r": 45.0, "h": 20.0},
              {"name": "spindle cartridge", "r": 95.0, "h": 40.0}, {"name": "spindle head", "r": 230.0, "h": head_h}],
        table={"x": L, "y": W, "t": tt, "slots": {"count": n_slots, "width": sw, "pitch": pitch}},
        sources={"table size and T-slots": "verified: Haas spec sheet (via dealer listings)",
                 "nose-to-table range": "verified: Haas spec sheet", "travel": "verified: haascnc.com",
                 "CAT40 flange Ø63.5 × 15.9, ER32 nut Ø50 × 25, gauge length 101.6": "verified: ASME B5.50 / Techniks catalogue",
                 f"tool changer: {pockets} pockets, {'side-mount with exchanger arm' if atc_kind == 'smtc' else 'umbrella carousel'}": "verified: Haas standard configuration; size and position estimated",
                 "table thickness 90, table height above floor 890": "estimated",
                 "saddle, base, column, head geometry": "estimated: representative proportions scaled from the verified numbers",
                 "enclosure, doors, windows, pendant, cabinet": "estimated: from photos, representative"},
        notes="Table moves X on the saddle, saddle moves Y, spindle head moves Z on the column. Home = spindle at Z top over the "
              "table centre (G54 is wherever you set it; the stock placement puts the work on the table).")
