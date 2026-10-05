# Reference: NEMA 17 stepper + 4:1 planetary gearhead, every part modelled. Z = motor axis, motor front face z = 0.
SQ = 42.3
HOLE = 15.5                                   # NEMA 17 31 mm square screw pattern
CORNERS = [(sx * HOLE, sy * HOLE) for sx in (-1, 1) for sy in (-1, 1)]


def face_poly(pts):
    return Face(Wire.make_polygon([Vector(x, y, 0) for x, y in pts], close=True))


def sector(r0, r1, a0, a1, n=16):
    arc = lambda r, a, b: [(r * math.cos(math.radians(a + (b - a) * k / n)), r * math.sin(math.radians(a + (b - a) * k / n))) for k in range(n + 1)]
    return face_poly(arc(r1, a0, a1) + arc(r0, a1, a0))


def square_block(z0, t, ch):
    with BuildSketch() as s:
        Rectangle(SQ, SQ)
        chamfer(s.vertices(), ch)
    return Pos(0, 0, z0) * extrude(s.sketch, t)


def bearing(d, D, B, n_balls, z0, name):
    pr = (d + D) / 4; db = 0.3 * (D - d) / 2 * 2.1; br = db / 2
    groove = Pos(0, 0, z0 + B / 2) * Torus(pr, 0.52 * db)
    inner = Pos(0, 0, z0) * (Cylinder(pr - 0.55 * br, B, align=(Align.CENTER, Align.CENTER, Align.MIN)) - Cylinder(d / 2, 3 * B)) - groove
    outer = Pos(0, 0, z0) * (Cylinder(D / 2, B, align=(Align.CENTER, Align.CENTER, Align.MIN)) - Cylinder(pr + 0.55 * br, 3 * B)) - groove
    inner = inner.chamfer(0.15, None, inner.edges().filter_by(GeomType.CIRCLE).filter_by(lambda e: abs(e.radius - d / 2) < 1e-6))
    outer = outer.chamfer(0.2, None, outer.edges().filter_by(GeomType.CIRCLE).filter_by(lambda e: abs(e.radius - D / 2) < 1e-6))
    parts = {"InnerRing": inner, "OuterRing": outer}
    cage = Pos(0, 0, z0 + B / 2) * (Cylinder(pr + 0.4 * br, 0.62 * B) - Cylinder(pr - 0.4 * br, B))
    for i in range(n_balls):
        a = 2 * math.pi * i / n_balls
        c = (pr * math.cos(a), pr * math.sin(a), z0 + B / 2)
        parts[f"Ball{i + 1}"] = Pos(*c) * Sphere(br)
        cage = cage - Pos(*c) * Sphere(br * 1.06)
    parts["Cage"] = cage
    return parts


def socket_screw(size, length):
    s = bolt(size, length, head="socket", real=True, thread_length=min(length, 2 * iso(size)["major"] + 12))
    return s


def csk_screw(size, length):
    d = iso(size); major = d["major"]
    head_d = 2 * major; hh = (head_d - major) / 2
    body = bolt(size, length, head="none", real=True)
    head = Pos(0, 0, -hh) * Cone(major / 2, head_d / 2, hh, align=(Align.CENTER, Align.CENTER, Align.MIN))
    sock = Pos(0, 0, 0.01) * extrude(RegularPolygon(0.65 * major / 2 / math.cos(math.pi / 6), 6), -0.45 * major - 0.01)
    return (body + head) - sock


def set_screw(size, length):
    d = iso(size)
    body = bolt(size, length, head="none", real=True)
    sock = Pos(0, 0, 0.01) * extrude(RegularPolygon(0.5 * d["major"] / 2 / math.cos(math.pi / 6), 6), -0.5 * length - 0.01)
    return body - sock


# ================================================================= MOTOR (40 mm NEMA 17 hybrid stepper)
front_cap = square_block(-8, 8, 3) + Pos(0, 0, 0) * Cylinder(11, 2, align=(Align.CENTER, Align.CENTER, Align.MIN))
front_cap -= Pos(0, 0, -8) * (Cylinder(18, 3, align=(Align.CENTER, Align.CENTER, Align.MIN)) - Cylinder(10, 6))   # end-winding pocket
front_cap -= Pos(0, 0, -8) * Cylinder(8, 5, align=(Align.CENTER, Align.CENTER, Align.MIN))                      # 625 bearing seat
front_cap -= Cylinder(3, 40)                                                                                      # shaft clearance
for x, y in CORNERS:
    front_cap = tap(front_cap, "M3", at=(x, y, 0), depth=4.5, real=True)
    front_cap = tap(front_cap, "M3", at=(x, y, -8), depth=3, real=True, axis=(0, 0, 1))

with BuildSketch() as st:
    Rectangle(SQ, SQ)
    chamfer(st.vertices(), 5)
    Circle(17, mode=Mode.SUBTRACT)
st_face = st.sketch
for k in range(8):
    a = 45 * k
    pole = Rot(0, 0, a) * (Pos(14.6, 0) * Rectangle(5.4, 4.5)) + sector(11.1, 12.3, a - 19.8, a + 19.8)
    for j in range(5):
        c = a + 7.2 * (j - 2)
        pole = pole - sector(11.0, 11.5, c - 1.8, c + 1.8, 4)
    st_face = st_face + pole
stator = Pos(0, 0, -30) * extrude(st_face, 22)
for x, y in CORNERS:
    stator -= Pos(x, y, 0) * Cylinder(1.7, 100)

coils = {}
for k in range(8):
    with BuildSketch(Plane.YZ) as cs:
        RectangleRounded(8.9, 22 + 4.4, 2.2)
        Rectangle(4.5, 22, mode=Mode.SUBTRACT)
    coil = Pos(12.6, 0, -19) * extrude(cs.sketch, 3.6)
    coils[f"Coil{k + 1}"] = Rot(0, 0, 45 * k) * coil

def rotor_cup(z0, h, phase):
    pts = []
    for i in range(50):
        a0 = 7.2 * i + phase
        for a, r in ((a0 - 1.8, 11.0), (a0 + 1.8, 11.0), (a0 + 1.8, 10.6), (a0 + 5.4, 10.6)):
            pts.append((r * math.cos(math.radians(a)), r * math.sin(math.radians(a))))
    return Pos(0, 0, z0) * (extrude(face_poly(pts), h) - Cylinder(2.5, 3 * h))
rotor_a = rotor_cup(-29, 9, 0)
magnet = Pos(0, 0, -20) * (Cylinder(10, 2, align=(Align.CENTER, Align.CENTER, Align.MIN)) - Cylinder(2.5, 5))
rotor_b = rotor_cup(-18, 9, 3.6)

shaft = Pos(0, 0, -35) * Cylinder(2.5, 59, align=(Align.CENTER, Align.CENTER, Align.MIN))
shaft -= Pos(2.0 + 5, 0, 9) * Box(10, 10, 15, align=(Align.CENTER, Align.CENTER, Align.MIN))

rear_cap = square_block(-40, 10, 3)
rear_cap -= Pos(0, 0, -33) * (Cylinder(18, 3, align=(Align.CENTER, Align.CENTER, Align.MIN)) - Cylinder(10, 8))
rear_cap -= Pos(0, 0, -35) * Cylinder(8, 5, align=(Align.CENTER, Align.CENTER, Align.MIN))
rear_cap -= Pos(21.15 - 2.9, 0, -37) * Box(5.81, 14.1, 4.81)                                                         # connector pocket
for x, y in CORNERS:
    rear_cap -= Pos(x, y, 0) * Cylinder(1.7, 100)

housing = Pos(21.15 - 2.9, 0, -37) * Box(5.8, 14.0, 4.8) - Pos(21.15 - 2.25 + 0.01, 0, -37) * Box(4.5, 12.4, 3.2)
pins = {f"Pin{i + 1}": Pos(21.15 - 4.5 + 0.01 + 1.75, -5 + 2 * i, -37) * Box(3.5, 0.64, 0.64) for i in range(6)}   # stand on the cavity floor

motor_screws = {f"Screw{i + 1}": Pos(x, y, -40) * Rot(180, 0, 0) * socket_screw("M3", 35) for i, (x, y) in enumerate(CORNERS)}

motor = {"FrontCap": front_cap, "Stator": stator, "RearCap": rear_cap, "Shaft": shaft,
         "RotorCupA": rotor_a, "RotorMagnet": magnet, "RotorCupB": rotor_b, **coils,
         "FrontBearing": bearing(5, 16, 5, 7, -8, "F"), "RearBearing": bearing(5, 16, 5, 7, -35, "R"),
         **motor_screws, "Connector": {"Housing": housing, **pins}}

# ================================================================= GEARHEAD (4:1, module 0.6)
m = 0.6
L = planetary_layout(m, 18, 18, 3)
adapter = square_block(0, 9, 3) - Cylinder(6, 40)
adapter -= Cylinder(11.1, 2.2 * 2)                                                                                 # pilot recess (z 0..2.2)
for x, y in CORNERS:
    adapter -= Pos(x, y, 0) * Cylinder(1.7, 100)

with BuildSketch() as rs:
    Rectangle(SQ, SQ)
    chamfer(rs.vertices(), 3)
    add(Rot(0, 0, L["ring_rotation"]) * involute_gear_profile(m, L["ring_teeth"], addendum=1.25, clearance=0.0, backlash=-0.04), mode=Mode.SUBTRACT)
    with Locations(*CORNERS):
        Circle(1.7, mode=Mode.SUBTRACT)
ring = Pos(0, 0, 9) * extrude(rs.sketch, 18)

with BuildSketch() as db:
    Circle(2.5)
    with Locations((2.0 + 5, 0)):
        Rectangle(10, 10, mode=Mode.SUBTRACT)
sun = Pos(0, 0, 13) * spur_gear(m, 18, 8.8, backlash=0.04) + Pos(0, 0, 9.5) * Cylinder(5, 3.5, align=(Align.CENTER, Align.CENTER, Align.MIN))
sun -= Pos(0, 0, 5) * extrude(db.sketch, 20)
sun = tap(sun, "M3", at=(5, 0, 11.25), depth=3.1, real=True, axis=(-1, 0, 0))
setscrew = Pos(5, 0, 11.25) * Rot(0, 90, 0) * set_screw("M3", 3)

planets, bushes, pins_, washers = {}, {}, {}, {}
for i, p in enumerate(L["planets"], 1):
    planets[f"Planet{i}"] = Pos(p["x"], p["y"], 13.5) * Rot(0, 0, p["rotation"]) * spur_gear(m, 18, 8, bore=5, backlash=0.04)
    bushes[f"Bushing{i}"] = Pos(p["x"], p["y"], 13.5) * (Cylinder(2.5, 8, align=(Align.CENTER, Align.CENTER, Align.MIN)) - Cylinder(1.5, 20))
    pins_[f"PlanetPin{i}"] = Pos(p["x"], p["y"], 11) * Cylinder(1.5, 14, align=(Align.CENTER, Align.CENTER, Align.MIN))
    for j, z in ((1, 13), (2, 21.5)):
        washers[f"Washer{i}{'AB'[j - 1]}"] = Pos(p["x"], p["y"], z) * (Cylinder(3, 0.5, align=(Align.CENTER, Align.CENTER, Align.MIN)) - Cylinder(1.6, 5))

a = L["centre_distance"]
POSTS = [(a * math.cos(math.radians(t)), a * math.sin(math.radians(t))) for t in (60, 180, 300)]
rear_plate = Pos(0, 0, 11) * (Cylinder(15, 2, align=(Align.CENTER, Align.CENTER, Align.MIN)) - Cylinder(5.5, 10))
for p in L["planets"]:
    rear_plate -= Pos(p["x"], p["y"], 0) * Cylinder(1.5, 100)
for x, y in POSTS:
    rear_plate = hole(rear_plate, 2.2, at=(x, y, 11), through=True, axis=(0, 0, 1), countersink=4.0)

carrier = Pos(0, 0, 22) * Cylinder(15, 3, align=(Align.CENTER, Align.CENTER, Align.MIN)) - Pos(0, 0, 22) * Cylinder(3, 5)
carrier += Pos(0, 0, 25) * Cylinder(4, 35, align=(Align.CENTER, Align.CENTER, Align.MIN))
for x, y in POSTS:
    carrier += Pos(x, y, 13) * Cylinder(2.5, 9, align=(Align.CENTER, Align.CENTER, Align.MIN))
for p in L["planets"]:
    carrier -= Pos(p["x"], p["y"], 0) * Cylinder(1.5, 100)
carrier -= Pos(0, 0, 39.05) * (Cylinder(10, 0.75, align=(Align.CENTER, Align.CENTER, Align.MIN)) - Cylinder(3, 2))   # E-clip groove
with BuildSketch(Plane.XY.offset(0)) as kw:
    SlotCenterToCenter(9, 3)
carrier -= Pos(4 - 1.8, 0, 52) * Rot(0, 90, 0) * Rot(0, 0, 90) * extrude(kw.sketch, 5)                               # keyway, 12 long
carrier = carrier.chamfer(0.5, None, carrier.edges().filter_by(GeomType.CIRCLE).filter_by(lambda e: abs(e.radius - 4) < 1e-6 and e.center().Z > 59))
for x, y in POSTS:
    carrier = tap(carrier, "M2", at=(x, y, 13), depth=5, real=True, axis=(0, 0, 1))
key = Pos(4 - 1.8, 0, 52) * Rot(0, 90, 0) * Rot(0, 0, 90) * extrude(kw.sketch, 3)          # sits in the keyway, 1.2 mm proud
carrier_screws = {f"CarrierScrew{i + 1}": Pos(x, y, 11) * Rot(180, 0, 0) * csk_screw("M2", 6) for i, (x, y) in enumerate(POSTS)}

spacer = Pos(0, 0, 25) * (Cylinder(5, 4, align=(Align.CENTER, Align.CENTER, Align.MIN)) - Cylinder(4.05, 10))
circlip_ring = Pos(0, 0, 28) * (Cylinder(8.4, 1.0, align=(Align.CENTER, Align.CENTER, Align.MIN)) - Cylinder(7.0, 5))
circlip = circlip_ring - Pos(-8, 0, 28.5) * Box(6, 2.5, 3)
for s in (-1, 1):
    lug = Pos(-6.6, s * 2.6, 28) * (Cylinder(1.3, 1.0, align=(Align.CENTER, Align.CENTER, Align.MIN)) - Cylinder(0.5, 5))
    circlip += lug
eclip = Pos(0, 0, 39.05) * (Cylinder(6.15, 0.7, align=(Align.CENTER, Align.CENTER, Align.MIN)) - Cylinder(3.0, 5))
eclip -= Pos(-6, 0, 39.4) * Box(8, 5.2, 3)

cover = square_block(27, 13, 3) + Pos(0, 0, 40) * Cylinder(11, 2, align=(Align.CENTER, Align.CENTER, Align.MIN))
cover -= Pos(0, 0, 27) * Cylinder(8, 12, align=(Align.CENTER, Align.CENTER, Align.MIN))
cover -= Pos(0, 0, 28) * Cylinder(8.4, 1.0, align=(Align.CENTER, Align.CENTER, Align.MIN))                           # circlip groove
cover -= Cylinder(7, 200)
for x, y in CORNERS:
    cover = hole(cover, 3.4, at=(x, y, 40), through=True, counterbore=(6.5, 3.2))
for t in (0, 90, 180, 270):
    cover = tap(cover, "M3", at=(14 * math.cos(math.radians(t)), 14 * math.sin(math.radians(t)), 40), depth=6, real=True)
housing_screws = {f"HousingScrew{i + 1}": Pos(x, y, 36.8) * socket_screw("M3", 40) for i, (x, y) in enumerate(CORNERS)}

gearhead = {"AdapterPlate": adapter, "RingGear": ring, "Sun": sun, "SetScrew": setscrew, **planets, **bushes, **pins_, **washers,
            "CarrierRearPlate": rear_plate, "Carrier": carrier, **carrier_screws, "OutputSpacer": spacer,
            "OutputBearing1": bearing(8, 16, 5, 8, 29, "O1"), "OutputBearing2": bearing(8, 16, 5, 8, 34, "O2"),
            "BearingCirclip": circlip, "ShaftEClip": eclip, "FrontCover": cover, "OutputKey": key, **housing_screws}

result = {"Motor": motor, "Gearhead": gearhead}
