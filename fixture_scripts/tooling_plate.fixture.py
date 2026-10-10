# Tooling (fixture) plate: an aluminium plate with a grid of threaded holes; the stock sits on it and is held with screws
# or clamps. params: size=(x, y) (default 200 × 200), thickness (15), pitch (25), hole_d (6.6).
sx, sy = params.get("size") or ((max(stock["x"] + 60, 100), max(stock["y"] + 60, 100)) if stock else (200.0, 200.0))
t = float(params.get("thickness") or 15.0); pitch = float(params.get("pitch") or 25.0); hd = float(params.get("hole_d") or 6.6)
plate = box(sx, sy, t, zmin=0)
nx, ny = int(sx // pitch), int(sy // pitch)
for i in range(nx):
    for j in range(ny):
        x = (i - (nx - 1) / 2) * pitch; y = (j - (ny - 1) / 2) * pitch
        plate = plate - cyl_z(hd / 2, t + 1, at=(x, y), zmin=-0.5)
part("plate", plate, "alu")
fixture("Tooling plate", work_origin=(0.0, 0.0, t), clamp_axis=None,
        mount={"type": "holes", "pitch": pitch, "note": "bolts to the bed; the stock is screwed or clamped to the grid"},
        sources={"plate": "estimated: parametric (size, thickness, pitch, hole_d)"},
        notes="The stock sits on the plate top. Cuts into the plate are reported as collisions; use a spoilboard on top if you cut through.")
