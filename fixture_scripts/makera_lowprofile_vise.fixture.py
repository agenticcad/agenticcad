# Makera Z1 Low-Profile Vise — a 226 × 103 mm aluminium T-slot extrusion that bolts straight to the Z1's aluminium bed
# (the MDF wasteboard comes off), a removable fixed jaw block at the left (−X) end and a sliding moving jaw + jaw
# carrier on the right, both locked into the T-slots. Jaws close along X. "Slotted soft jaws": each jaw has a rebate
# along its top inner edge — thin stock sits on the rebate floor (20 mm above the rails) and is gripped by the rebate
# walls; taller stock sits on the rails and is gripped by the full jaw faces.
#
# Fixture frame: X along the vise (fixed jaw at −X), Y across, Z up; origin = centre of the extrusion footprint on the
# aluminium bed top (6 mm below the Z1's spoilboard top — replaces_spoilboard=True).
#
# VERIFIED (Makera's dimensioned product photos and the Z1 Low-Profile Vise quick-start on wiki.makera.com):
#   overall 226 × 103 mm, max opening 130 mm between the jaws, jaw height 20 mm above the rails (to the rebate floor),
#   locating pins 17.5 mm from the left end and 191 apart, mounting holes 105 / 208.5 mm from it (17.5 + 87.5, then 103.5),
#   67 mm apart across the vise,
#   six M5×20 screws into the bed, two 4×11 mm locating pins, MDF wasteboard removed, side screws 1 / 2 / 3.
# MEASURED from those photos (scaled by the labelled 226 / 103 / 67 mm; ±0.5 mm): every height and block length below.
# Heights are taken at the upper end of the measurement so the collision check errs on the safe side. Calipers on
# your own vise beat photos: override any of them with params (base_h, jaw_h, ledge, end_h, ...).
P = params
L = float(P.get("length", 226.0))           # verified
W = float(P.get("width", 103.0))            # verified
base_h = float(P.get("base_h", 12.6))       # extrusion height, bed to rail tops (measured)
jaw_h = float(P.get("jaw_h", 22.6))         # jaw top above the rails (measured)
ledge = float(P.get("ledge", 20.0))         # rebate floor above the rails (verified: Makera's "20 mm")
reb_d = float(P.get("rebate", 3.0))         # rebate depth along X (measured)
end_len, end_h = float(P.get("end_len", 25.4)), float(P.get("end_h", 13.5))           # fixed-jaw end block (measured)
fj_t, mj_t = float(P.get("fixed_jaw_t", 13.0)), float(P.get("moving_jaw_t", 11.5))     # jaw thicknesses (measured)
body_len, body_h = float(P.get("body_len", 25.3)), float(P.get("body_h", 13.9))       # moving-jaw body (measured)
car_len, car_h = float(P.get("carrier_len", 20.0)), float(P.get("carrier_h", 13.6))    # jaw carrier (measured)
gap = float(P.get("carrier_gap", 0.8))      # moving body ↔ carrier, set by screw 2 (measured at full opening)
slot_y = (-24.1, 0.0, 24.1)                 # T-slot centres (measured), 5.8 mm openings
X0, X1 = -L / 2, L / 2
F = X0 + end_len + fj_t                     # fixed jaw's lower clamping face
max_lower = X1 - car_len - gap - body_len - mj_t - F       # = 130 mm with the defaults (carrier flush with the end)

# ---- how the stock is held: on the ledges (thin stock, gripped by the rebate walls) or on the rails (full jaw faces)
t = float(stock["z"]) if stock else 10.0
seat = str(P.get("seat") or "auto")
if seat == "auto":
    seat = "ledge" if t < jaw_h else "rails"
if seat not in ("ledge", "rails"):
    raise ValueError("params['seat'] is 'auto', 'ledge' or 'rails'")
grip = float(P.get("opening") or (stock["x"] if stock else 60.0))          # distance between the surfaces that grip the stock
lower = grip - 2 * reb_d if seat == "ledge" else grip                     # distance between the lower jaw faces
if lower < 0:
    raise ValueError(f"stock {grip:g} mm long is shorter than the two {reb_d:g} mm rebates: seat it on the rails (params['seat'] = 'rails')")
if lower > max_lower + 1e-6:
    lim = max_lower + (2 * reb_d if seat == "ledge" else 0)
    raise ValueError(f"the stock is {grip:g} mm along the clamping axis (X) but the Z1 vise opens to {lim:.0f} mm"
                     f" ({'on the ledges' if seat == 'ledge' else 'on the rails'}); turn the stock (fixture_rot=90) or use another fixture")
M = F + lower                               # moving jaw's lower clamping face
zr = base_h                                 # rail tops

# ---- extrusion: three T-slots along X, a shallow groove along each side face, four counterbored M5 mounting holes
ext = box(L, W, base_h, zmin=0)
for sy in slot_y:
    ext = ext - box(L + 2, 5.8, 2.6, at=(0, sy, 0), zmin=base_h - 2.6) - box(L + 2, 10.4, 4.2, at=(0, sy, 0), zmin=base_h - 6.8)
for sy in (-1, 1):
    ext = ext - box(L + 2, 1.2, 1.2, at=(0, sy * (W / 2), 0), zmin=base_h - 8.4)
mount_xy = [(X0 + mx, sy * 33.5) for mx in (105.0, 208.5) for sy in (-1, 1)]      # 67 mm apart (verified: bed M5 holes)
for (mx, my) in mount_xy:
    ext = ext - cyl_z(4.75, 5.6, at=(mx, my), zmin=base_h - 5.4) - cyl_z(2.75, base_h + 2, at=(mx, my), zmin=-1)
part("extrusion", ext, "alu")
for i, (mx, my) in enumerate(mount_xy):
    part(f"mount screw {i + 1}", cyl_z(4.25, 4.6, at=(mx, my), zmin=base_h - 5.0) - cyl_z(2.0, 3.0, at=(mx, my), zmin=base_h - 2.5), "anodised", collision=False)

# ---- fixed jaw: the low end block (two counterbored M5 jaw screws, two small pins, a tapped hole in the front face)
#      and the tall jaw with its rebate on the clamping (+X) side
eb = box(end_len, W, end_h, at=(X0 + end_len / 2, 0, 0), zmin=zr)
for sy in (-1, 1):
    eb = eb - cyl_z(4.75, 5.6, at=(X0 + 12.7, sy * 25.6), zmin=zr + end_h - 5.4)
eb = eb - cyl_y(2.5, 8.0, X0 + 13.4, zr + 9.0, -W / 2 - 0.5)
part("fixed jaw block", eb, "alu")
for sy in (-1, 1):
    part(f"jaw screw {'F' if sy < 0 else 'B'}", cyl_z(4.25, 4.6, at=(X0 + 12.7, sy * 25.6), zmin=zr + end_h - 5.0), "anodised", collision=False)
    part(f"jaw pin {'F' if sy < 0 else 'B'}", cyl_z(1.6, 0.6, at=(X0 + 12.7, sy * 33.5), zmin=zr + end_h - 0.5), "steel", collision=False)
fj = box(fj_t, W, jaw_h, at=(F - fj_t / 2, 0, 0), zmin=zr) - box(reb_d + 0.01, W + 2, jaw_h - ledge + 1, at=(F - reb_d / 2 + 0.005, 0, 0), zmin=zr + ledge)
part("fixed jaw", fj, "alu")

# ---- moving jaw (rebate on its −X side), its body (screws 3 into T-nuts) and the jaw carrier (screws 1; screw 2
#      pushes the jaw along X and sticks out of the carrier's end)
mj = box(mj_t, W, jaw_h, at=(M + mj_t / 2, 0, 0), zmin=zr) - box(reb_d + 0.01, W + 2, jaw_h - ledge + 1, at=(M + reb_d / 2 - 0.005, 0, 0), zmin=zr + ledge)
part("moving jaw", mj, "alu")
bx0 = M + mj_t
body = box(body_len, W, body_h, at=(bx0 + body_len / 2, 0, 0), zmin=zr)
cx0 = bx0 + body_len + gap
car = box(car_len, W, car_h, at=(cx0 + car_len / 2, 0, 0), zmin=zr)
for sy in (-1, 1):
    body = body - cyl_z(4.75, 5.6, at=(bx0 + body_len / 2, sy * 23.8), zmin=zr + body_h - 5.4)
    car = car - cyl_z(4.75, 5.6, at=(cx0 + car_len / 2, sy * 23.8), zmin=zr + car_h - 5.4)
part("moving jaw body", body, "alu")
part("jaw carrier", car, "alu")
for sy in (-1, 1):
    part(f"screw 3 {'F' if sy < 0 else 'B'}", cyl_z(4.25, 4.6, at=(bx0 + body_len / 2, sy * 23.8), zmin=zr + body_h - 5.0), "anodised", collision=False)
    part(f"screw 1 {'F' if sy < 0 else 'B'}", cyl_z(4.25, 4.6, at=(cx0 + car_len / 2, sy * 23.8), zmin=zr + car_h - 5.0), "anodised", collision=False)
part("screw 2", cyl_x(3.75, 4.5, 0.0, zr + 8.7, cx0 + car_len), "anodised")

fixture("Makera low-profile vise", work_origin=((F + M) / 2, 0.0, zr + (ledge if seat == "ledge" else 0.0)), clamp_axis="x",
        max_opening=round(max_lower + (2 * reb_d if seat == "ledge" else 0.0), 1), replaces_spoilboard=True, home=(0.0, -51.5),
        mount={"type": "bolted", "machines": ["Makera Z1"], "screws": "6 × M5×20 into the aluminium bed (2 under the fixed jaw)",
               "pins": "2 × Ø4×11 locating pins", "note": "remove the MDF wasteboard first"},
        sources={"226 × 103 mm, 130 mm max opening, 20 mm jaw height, 67 mm pins, holes at 105 / 208.5 mm":
                 "verified: Makera product page dimension photos (makera.com/products/makera-z1-low-profile-vise)",
                 "MDF removed, 6 × M5×20, 2 × 4×11 pins, slotted soft jaws, screws 1/2/3":
                 "verified: Makera Z1 Low-Profile Vise quick start (wiki.makera.com/en/Z1/Accessories/Low-Profile-Vise)",
                 "heights (rails 12.6, jaws 22.6, end block 13.5, body 13.9, carrier 13.6), block lengths, rebate 3 mm, T-slots":
                 "measured: Makera's photos scaled by the labelled dimensions (±0.5 mm; heights at the upper end)",
                 "position on the bed: centred in X, front half (centre y −51.5)":
                 "verified: Makera Z1 aluminium-base hole pattern (cinetronixlabs/makera-z1-bed-cad-files, checked against Makera's bed file): "
                 "Ø4 pin holes at x ±95.5, y −7 and M5 holes at x −8 / 95.5, y −18 / −85 match the vise's pins and mounting holes"},
        notes=f"Bolts to the Z1 bed with the MDF wasteboard removed. Jaws close along X, fixed jaw at −X. Stock seated "
              f"{'on the jaw ledges (thin stock, gripped by the rebate walls) ' + str(ledge) + ' mm above the rails' if seat == 'ledge' else 'on the rails (gripped by the full jaw faces)'}; "
              f"params: seat='ledge'|'rails', opening, carrier_gap, and caliper overrides base_h / jaw_h / ledge / ...")
