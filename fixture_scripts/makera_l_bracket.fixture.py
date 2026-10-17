# Makera Z1 L-bracket: the stop the stock is pushed into at the bed's front-left corner, located by dowel pins.
# Frame origin = the bracket's inside corner (Makera's Anchor 1), on the spoilboard; arms run +X (front arm) and +Y (left
# arm), 100 mm long and 15 mm wide, so each arm covers 85 mm of a stock edge from the corner.
# Verified: Makera_Z1_Bed_v1_1.step (cinetronix bed CAD, approved by Makera): arms 100 × 15, inside corner 88 mm left of and
# 88 mm in front of the bed centre; thin 5 mm, thick 15 mm. Measured on a real Z1 (2026-10-17, wired probe): the thin
# bracket's top 4.85 mm above the spoilboard.
kind = str(params.get("type") or "thin")
if kind not in ("thin", "thick"):
    raise ValueError("L-bracket type: 'thin' (5 mm) or 'thick' (15 mm)")
h = float(params.get("height") or (5.0 if kind == "thin" else 15.0))
arm, w = 100.0, 15.0
part("front arm", box(arm, w, h, at=(arm / 2 - w, -w / 2, 0), zmin=0), "anodised")
part("left arm", box(w, arm - w, h, at=(-w / 2, (arm - w) / 2, 0), zmin=0), "anodised")
fixture("Makera L-bracket", work_origin=(0.0, 0.0, 0.0), clamp_axis=None, home=(-88.0, -88.0),
        mount={"type": "dowels", "note": "two dowel pins in the Z1 bed; fixed position"},
        sources={"size and position": "verified: Makera Z1 bed CAD (Makera_Z1_Bed_v1_1.step)",
                 "thin height on the spoilboard": "measured on a real Z1: 4.85 mm"},
        notes="A stop, not a clamp: push the stock into its inside corner (Anchor 1) and clamp it with strap clamps. "
              "Tools reaching past the stock's left or front face below the bracket top will hit it.")
