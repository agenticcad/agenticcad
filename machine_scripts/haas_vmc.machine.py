# Haas vertical machining centre — machine model script (VF-2, VF-2SS, VF-4, Mini Mill, TM-1 by the record's name).
# Verified: table size and T-slots, nose-to-table range and travels (Haas spec sheets), CAT40 flange Ø63.5, ER32 nut
# Ø50, gauge length 101.6 (ASME B5.50 / Techniks). Castings, column and enclosure are representative.
spec = next((v for k, v in HAAS.items() if k.lower() in machine_record.name.lower()), HAAS["Haas VF-2"])
L, W = spec["table"]; n_slots, sw, pitch = spec["slots"]; nose_min, nose_max = spec["nose"]; tx, ty, tz = spec["travel"]
tt = 60.0                                                                       # table thickness

node("base")
node("saddle", "base", axis="y", mode="table")                                  # saddle moves Y, carries the table
node("table", "saddle", axis="x", mode="table", stock=True)                     # table moves X, carries the work
node("column", "base")
node("spindle", "column", axis="z")

table = box(L, W, tt, zmin=-tt)
for k in range(n_slots):
    y = (k - (n_slots - 1) / 2) * pitch
    table = table - box(L + 2, sw, 14, at=(0, y, 0), zmin=-14) - box(L + 2, sw + 10, 10, at=(0, y, 0), zmin=-24)
part("table", "table", table, "ground_steel", collision=True)
part("saddle", "saddle", box(min(L * 0.55, 520), W + 120, 140, zmin=-tt - 140), "paint_light")
base_w, base_d = L + 500, W + 900
part("base casting", "base", box(base_w, base_d, 700, at=(0, 150, 0), zmin=-tt - 140 - 700), "paint_light")
col_y = W / 2 + 260
part("column", "column", box(min(L * 0.5, 620), 520, nose_max + 900, at=(0, col_y, 0), zmin=-tt - 140), "paint_light")

# head at Z top: spindle housing, CAT40 nose Ø90 and an ER32 holder (gauge line to nut face 101.6)
nose_z = nose_max
part("spindle head", "spindle", box(360, 480, 640, at=(0, col_y - 380, 0), zmin=nose_z + 20), "paint_light", collision=True)
part("spindle nose", "spindle", cyl_z(45, 20, zmin=nose_z), "steel", collision=True)
holder = (cyl_z(31.75, 20, zmin=nose_z - 20) + Pos(0, 0, nose_z - 20) * Cone(24, 22, 56.6, align=(Align.CENTER, Align.CENTER, Align.MAX))
          + cyl_z(25, 25, zmin=nose_z - 101.6))
part("CAT40 ER32 holder", "spindle", holder, "stainless", collision=True)

# enclosure: full splash guard with sliding doors from below the table to above the head (representative)
enc_w, enc_d, enc_h = base_w + 300, base_d + 200, nose_max + 1400
enc_zmin = -tt - 140 - 700 - 60
enc = shell((enc_w, enc_d, enc_h), (enc_w - 40, enc_d - 40, enc_h - 40), at=(0, 150, 0), zmin=enc_zmin, inner_zmin=enc_zmin + 20)
door_w, door_h, door_z = enc_w * 0.72, nose_max + 500, -tt - 300
enc = enc - box(door_w, 80, door_h, at=(0, 150 - enc_d / 2, 0), zmin=door_z)
part("enclosure", "base", enc, "panel", collision=True)
part("front doors", "base", box(door_w, 4, door_h, at=(0, 150 - enc_d / 2 + 2, 0), zmin=door_z), "acrylic")
part("control pendant", "base", box(80, 300, 600, at=(enc_w / 2 + 60, -100, 0), zmin=200), "paint_dark")

machine(key="haas", home=(0.0, 0.0, nose_z), travel=(tx, ty, tz), clearance=nose_max,
        nose=[{"name": "ER32 nut", "r": 25.0, "h": 25.0}, {"name": "holder body", "r": 24.0, "h": 56.6},
              {"name": "CAT40 flange", "r": 31.75, "h": 20.0}, {"name": "spindle nose", "r": 45.0, "h": 20.0},
              {"name": "spindle head", "r": 180.0, "h": 640.0}],
        table={"x": L, "y": W, "t": tt, "slots": {"count": n_slots, "width": sw, "pitch": pitch}},
        sources={"table size and T-slots": "verified: Haas spec sheet (via dealer listings)",
                 "nose-to-table range": "verified: Haas spec sheet", "travel": "verified: haascnc.com",
                 "CAT40 flange Ø63.5, ER32 nut Ø50, gauge length 101.6": "verified: ASME B5.50 / Techniks catalogue",
                 "castings, column, enclosure": "estimated: representative proportions"},
        notes="Table moves X on the saddle, saddle moves Y, spindle moves Z. Machine zero: spindle at Z top over the table centre "
              "(G54 is wherever you set it; the stock placement puts the work on the table).")
