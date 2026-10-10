# Generic gantry router — parametric machine model sized from the record's travel (3018, Shapeoko-class, LinuxCNC
# mills…). Fixed bed, gantry moves Y, head moves X and Z. Everything is `estimated` except the travel: duplicate the
# machine and edit this script with your real numbers.
tr = machine_record.travel or {}
tx, ty, tz = float(tr.get("x", 300)), float(tr.get("y", 180)), float(tr.get("z", 45))
big = machine_record.post == "linuxcnc" or tx > 500
spindle_d = 80.0 if big else (65.0 if tx > 350 else 52.0)                       # round spindle body Ø
nut_d = 25.0 if big else 19.0                                                    # collet nut Ø
bed_x, bed_y, bed_t = tx + 80, ty + 80, 18.0

node("base")
node("bed", "base", stock=not fourth)                                            # fixed bed carries the work
node("gantry", "base", axis="y")
node("head", "gantry", axis="x")
node("spindle", "head", axis="z")

bed = box(bed_x, bed_y, bed_t, zmin=-bed_t)
n = int(bed_x // 60) or 1
for k in range(n):
    bed = bed - box(8, bed_y + 2, 6, at=((k - (n - 1) / 2) * 60, 0, 0), zmin=-6)   # T-track grooves
part("bed", "bed", bed, "mdf" if spindle_d < 60 else "alu", collision=True)
part("frame", "base", box(bed_x + 80, bed_y + 80, 40, zmin=-bed_t - 40), "anodised")
for x in (-bed_x / 2 - 20, bed_x / 2 + 20):
    part(f"Y rail {'L' if x < 0 else 'R'}", "base", box(40, bed_y + 80, 40, at=(x, 0, 0), zmin=0), "anodised")
clear = tz + 20                                                                  # nose above the bed at Z top
gantry = (box(40, 50, clear + 60, at=(-bed_x / 2 - 20, 0, 0), zmin=40) + box(40, 50, clear + 60, at=(bed_x / 2 + 20, 0, 0), zmin=40)
          + box(bed_x + 80, 40, 60, at=(0, 0, 0), zmin=clear + 40))
part("gantry", "gantry", gantry, "anodised", collision=True)
hx, hy, nose_z = -tx / 2, ty / 2, clear
part("X carriage", "head", box(90, 60, 120, at=(hx, 30, 0), zmin=clear + 10), "anodised")
part("spindle body", "spindle", cyl_z(spindle_d / 2, 95, at=(hx, 0), zmin=nose_z + 20), "alu", collision=True)
part("collet nut", "spindle", cyl_z(nut_d / 2, 20, at=(hx, 0), zmin=nose_z), "steel", collision=True)

rotary = None
if fourth:
    r = machine_record.rotary or {}
    az = float(r.get("max_diameter", 80)) / 2 + 8
    node("chuck", "bed", axis="a", mode="table", stock=True, pivot=(0.0, 0.0, az))
    part("chuck", "chuck", cyl_x(26, 18, 0, az, -tx / 2 - 10), "steel", collision=True)
    part("tailstock", "bed", box(40, 60, az + 20, at=(tx / 2 + 20, 0, 0), zmin=0) + cyl_x(6, 15, 0, az, tx / 2 - 15), "anodised", collision=True)
    rotary = {"axis_y": 0.0, "axis_z": az, "chuck_face_x": -tx / 2 + 8, "chuck_r": 26.0, "tail_x": tx / 2 - 15, "tail_r": 6.0,
              "max_diameter": r.get("max_diameter", 80), "max_length": r.get("max_length", tx)}

machine(key="router", home=(hx, hy, nose_z), travel=(tx, ty, tz), clearance=clear,
        nose=[{"name": "collet nut", "r": nut_d / 2, "h": 20.0}, {"name": "spindle", "r": spindle_d / 2, "h": 95.0}],
        table={"x": bed_x, "y": bed_y, "t": bed_t}, rotary=rotary, spoilboard=bed_t if spindle_d < 60 else 0.0,
        sources={"travel": "verified: machine record", "everything else": "estimated: parametric gantry router"},
        notes="Representative gantry router: fixed bed, gantry moves Y, head moves X and Z.")
