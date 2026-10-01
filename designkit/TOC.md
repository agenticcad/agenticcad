# Design Kit contents

63 components, 27 guides. Generated from each entry's metadata; do not edit by hand.

## Fasteners & threads

- `fasteners/choosing-fasteners` (guide): Which screw head to use, clearance and tap-drill sizes, counterbores, thread engagement lengths and screw length choice [screw, bolt, thread, engagement, clearance hole, tap drill, counterbore]
- `fasteners/fastening-into-plastic` (guide): Heat-set inserts vs self-tapping screws vs nut traps vs printed threads, with hole sizes and boss rules [insert, heat-set, self-tapping, plastic, 3d printing, nut trap, threads]
- `button_head_screw` (component · ISO 7380): ISO 7380 button head socket screw [screw, button, socket, metric]
- `countersunk_screw` (component · ISO 10642): ISO 10642 countersunk (flat head) socket screw, 90° head [screw, countersunk, flat head, csk, metric]
- `heat_set_insert` (component): Brass heat-set threaded insert for 3D-printed parts, knurled [insert, heat-set, threaded insert, 3d printing, brass]
- `hex_bolt` (component · ISO 4017 / ISO 4014): ISO 4017 / 4014 hex head bolt with chamfered head [bolt, hex, metric]
- `hex_nut` (component · ISO 4032): ISO 4032 hex nut, chamfered both sides [nut, hex, metric]
- `insert_hole_cutter` (component): Hole cutter for a heat-set insert (recommended hole plus lead-in chamfer and relief below) [insert, heat-set, hole, cutter, 3d printing]
- `set_screw` (component · ISO 4026/4027/4028/4029): ISO 4026-4029 hex socket set screw (grub screw): flat, cup, cone or dog point [set screw, grub screw, socket, metric]
- `socket_screw` (component · ISO 4762): ISO 4762 / DIN 912 socket head cap screw [screw, socket, cap screw, shcs, metric]
- `standoff` (component): Hex standoff (spacer) M-F, F-F or M-M, for PCBs and panels [standoff, spacer, pcb, hex, brass, nylon]
- `washer` (component · ISO 7089): ISO 7089 plain washer [washer, metric]

## Bearings & bushings

- `bearings/selection-and-retention` (guide): Picking a deep-groove bearing, which ring gets the tight fit, housing and shaft seats, and how to hold bearings axially [bearing, ball bearing, fit, retention, housing, shaft, preload]
- `ball_bearing` (component · ISO 15 boundary dimensions): Deep-groove ball bearing by designation (608, 688ZZ, 6204-2RS, MR105...) as real parts [bearing, ball bearing, deep groove, 608, 625, 688, 6000, 6200, mr]
- `bearing_seat` (component): Bore/seat cutter for a bearing: a cylinder of the outside diameter and width, for housings [bearing, seat, housing, bore, cutter]

## Shafts & retention

- `shafts/retention` (guide): Ways to fix parts to shafts (keys, D-flats, set screws, clamps) and to stop them sliding (circlips, E-clips, collars, shoulders) [shaft, key, keyway, circlip, e-clip, set screw, d-flat, collar]
- `circlip_external` (component · DIN 471): DIN 471 external circlip (retaining ring for shafts), seated in its groove [circlip, retaining ring, shaft, snap ring]
- `circlip_groove_external` (component · DIN 471): Groove cutter for a DIN 471 external circlip (subtract from the shaft) [circlip, groove, cutter, shaft]
- `circlip_groove_internal` (component · DIN 472): Groove cutter for a DIN 472 internal circlip (subtract from the housing) [circlip, groove, cutter, bore]
- `circlip_internal` (component · DIN 472): DIN 472 internal circlip (retaining ring for bores), seated in its groove [circlip, retaining ring, bore, housing]
- `dowel_pin` (component · ISO 8734): ISO 8734 dowel pin with chamfered and rounded ends [dowel, pin, location]
- `e_clip` (component · DIN 6799): DIN 6799 E-clip (retaining washer) sized for a shaft, seated in its groove [e-clip, retaining, shaft]
- `e_clip_groove` (component · DIN 6799): Groove cutter for a DIN 6799 E-clip [e-clip, groove, cutter, shaft]
- `jaw_coupling` (component): Jaw (spider) flexible coupling: two three-jaw hubs and an elastomer spider, with clamp set screws [coupling, jaw coupling, spider, l-type, flexible coupling, shaft]
- `keyway_cutter` (component · DIN 6885): Keyway cutter for a DIN 6885 key (subtract from the shaft) [keyway, cutter, shaft]
- `parallel_key` (component · DIN 6885): DIN 6885 form A parallel key (round ends), sized from the shaft diameter [key, parallel key, keyway, shaft]
- `rigid_coupler` (component): Rigid shaft coupler joining two shafts (e.g. 5 mm motor to 8 mm lead screw) with set screws [coupler, coupling, rigid coupler, shaft, lead screw, 5x8]
- `shaft_collar` (component): Set-screw shaft collar with a tapped radial hole and a set screw [collar, shaft collar, set screw, stop]

## Power transmission

- `transmission/belt-drives` (guide): GT2 pulley and belt sizing, tensioning and idlers, and when to use a lead screw instead, with typical numbers [belt, gt2, pulley, timing belt, tension, lead screw, t8, linear motion]
- `transmission/planetary-gear-sets` (guide): Tooth-count rules, ratio, planet phasing and ring gears for a simple planetary stage, with a checked recipe [gear, planetary, epicyclic, ring gear, gearbox, reduction]
- `transmission/spur-gears` (guide): Module, pressure angle, centre distance, backlash, face width and printable sizes for involute spur gears [gear, spur, involute, module, backlash]
- `gear_centre_distance` (component): Centre distance of two meshing external spur gears [gear, layout]
- `gear_dims` (component): Pitch, outside, root and base diameters of a gear [gear, dimensions]
- `gt2_pulley` (component): GT2 timing pulley (2 mm pitch): toothed section, flanges, hub with two set-screw holes [pulley, gt2, timing belt, belt drive, 3d printer]
- `involute_gear_profile` (component): Closed involute gear outline as a Face (for sketches, e.g. ring gear cutters) [gear, profile, sketch, involute]
- `lead_screw_t8` (component): T8 lead screw (8 mm, 2 mm pitch, 8 mm lead) with a brass flange nut [lead screw, t8, acme, trapezoidal, linear motion, 3d printer, z axis]
- `planetary_layout` (component): Planetary layout: ring teeth, ratio, planet positions and mesh phasing (verified) [gear, planetary, epicyclic, layout]
- `planetary_stage` (component): Complete simple planetary stage: sun, n phased planets and ring, meshing without interference [gear, planetary, epicyclic, gearbox, reduction]
- `ring_gear` (component): Internal (ring) gear: housing outline with involute internal teeth, sketched then extruded [gear, ring gear, internal gear, annulus, planetary]
- `spur_gear` (component · ISO 53 basic rack): Involute spur gear solid (Z up, tooth 0 on +X), optional bore, hub and keyway [gear, spur, involute]

## Motors & actuators

- `motors/mounting-motors-and-servos` (guide): NEMA flange patterns, pilot bosses, shaft alignment, servo horns and brackets, and cooling for motor mounts [motor, stepper, nema, servo, mount, bracket, pilot, alignment]
- `n20_gearmotor` (component): N20 micro metal gear motor: gearbox, flatted motor can, end cap with terminals, D-shaft [n20, gear motor, dc motor, micro motor, robot]
- `nema_stepper` (component · NEMA ICS 16 frame sizes): NEMA 8/11/14/17/23 hybrid stepper motor: outside only, or full internals (stator poles, coils, 50-tooth rotor, bearings) [stepper, nema, nema17, motor, hybrid stepper, actuator]
- `servo` (component): Hobby RC servo (SG90, MG90S, MG996R, DS3218): case, mounting tabs with holes, gear cap and splined output [servo, rc servo, sg90, mg996r, actuator, robot]
- `stepper_28byj48` (component): 28BYJ-48 geared 5 V stepper: Ø28 can, mounting ears, offset flatted output shaft, wiring cover [stepper, 28byj-48, geared stepper, motor, uln2003]

## Structural & framing

- `structural/extrusion-frames` (guide): Choosing profiles, joining methods, T-nuts and screws, squareness and rigidity for extrusion frames [extrusion, t-slot, frame, 2020, 2040, bracket, t-nut, rigidity]
- `corner_bracket` (component): Cast corner bracket for T-slot extrusion with a gusset and a slotted hole in each leg [bracket, corner bracket, angle, extrusion, gusset]
- `extrusion` (component): T-slot aluminium extrusion (2020, 2040, 3030, 4040 ...) cut to length, with the centre bores [extrusion, aluminium profile, t-slot, 2020, 2040, 3030, 4040, frame, misumi]
- `extrusion_profile` (component): Cross-section Face of a T-slot extrusion, for sketches or a custom extrude [extrusion, profile, t-slot, sketch]
- `t_nut` (component): Drop-in T-nut for a T-slot extrusion, with a tapped hole [t-nut, slot nut, hammer nut, extrusion]

## Electronics packaging

- `electronics/pcb-mounting-and-venting` (guide): How to mount PCBs in enclosures, clearances for connectors and hands, venting and fan sizing, and cable entry [pcb, board, mounting, standoff, enclosure, vent, cooling, connector, cable]
- `arduino_uno` (component): Arduino Uno R3: 68.6 × 53.3 PCB with the four mounting holes, headers, USB-B and barrel jack [arduino, uno, board, microcontroller, pcb]
- `cell_18650` (component): 18650 Li-ion cell (Ø18.4 × 65.2) with positive button and insulator ring [battery, 18650, li-ion, cell]
- `fan` (component): Axial cooling fan 25-120 mm: square frame with mounting holes, hub on struts, 7 pitched blades [fan, cooling, axial fan, 40mm, 80mm, 120mm, ventilation]
- `fan_grille_cutter` (component): Grille cutter for a fan: the swept circle minus concentric rings and spokes, to cut into a panel [fan, grille, vent, cutter, enclosure]
- `pi_pico` (component): Raspberry Pi Pico: 51 × 21 PCB, four Ø2.1 holes, castellated pads, micro-USB, RP2040 [raspberry pi, pico, rp2040, board, microcontroller, pcb]
- `raspberry_pi_4` (component): Raspberry Pi 4 Model B: 85 × 56 PCB, M2.5 holes on 58 × 49, GPIO header, USB/Ethernet stack, USB-C, micro-HDMI [raspberry pi, pi 4, board, sbc, pcb]

## Enclosures & housings

- `enclosures/enclosure-design` (guide): Wall thickness, bosses and ribs, lids and fasteners, snap fits, openings and sealing for printed enclosures [enclosure, box, housing, wall, boss, rib, snap fit, lid, seal]
- `enclosure` (component): 3D-printable box with a screw-on lid: rounded walls, corner bosses with heat-set inserts, locating lip, screws [enclosure, box, case, housing, lid, 3d printing, project box]
- `screw_boss` (component): Screw boss for plastic parts: a post with an insert or pilot hole and optional gussets [boss, screw boss, insert, post, 3d printing, moulding]
- `snap_fit_hook` (component): Cantilever snap-fit hook for printed or moulded parts [snap fit, clip, latch, cantilever, 3d printing]
- `vent_slots_cutter` (component): Cutter for a row of rounded vent slots (subtract from a wall or lid) [vent, slots, ventilation, grille, cutter]

## Mechanisms

- `mechanisms/hinges-and-print-in-place` (guide): Knuckle hinges, pin sizing, print-in-place clearances and orientation, and living hinges in PP and PETG [hinge, knuckle, pin, print in place, living hinge, clearance]
- `mechanisms/linkages-and-cams` (guide): Four-bar linkage rules (Grashof, transmission angle) and cam design (motion laws, pressure angle, base circle) [linkage, four bar, grashof, crank, rocker, cam, follower, pressure angle]
- `mechanisms/springs` (guide): Spring rate, index, solid height and working range for helical compression and extension springs, with worked numbers [spring, compression spring, extension spring, rate, stiffness, solid height]
- `compression_spring` (component): Helical compression spring swept along its real helix, with closed (and optionally ground) ends [spring, compression spring, coil spring, helix]
- `disc_cam` (component): Disc cam with harmonic rise-dwell-return lift, hub bore and keyway-ready hub [cam, disc cam, follower, lift, mechanism]
- `extension_spring` (component): Helical extension spring with close-wound body and a full loop at each end [spring, extension spring, tension spring, helix, hook]
- `hinge` (component): Butt hinge: two leaves with alternating knuckles, a pin and countersunk screw holes; opens to any angle [hinge, butt hinge, knuckle, pin, lid, door, print in place]
- `knob` (component): Fluted control knob with a D-shaft bore, a set screw and a pointer line [knob, control knob, potentiometer, encoder, handle, dial]
- `link_bar` (component): Link bar for linkages: rounded ends with pivot holes at a given centre distance [link, linkage, four bar, lever, arm, pivot]
- `pull_handle` (component): Bar pull handle (cabinet/drawer or equipment handle) with tapped feet [handle, pull handle, bar handle, drawer, grip]

## Fluid & pneumatic

- `fluid/o-ring-grooves` (guide): Squeeze, groove fill, stretch and groove sizes for static face seals and radial piston/rod seals, with cross-section choices [o-ring, groove, gland, seal, squeeze, face seal, piston, rod]
- `hose_barb` (component): Hose barb fitting with three barbs, hex and a male port thread [hose barb, barb, fitting, hose, tubing]
- `o_ring` (component · ISO 3601): O-ring (ISO 3601 / AS568 style) by inner diameter and cross-section [o-ring, seal, gasket, oring]
- `o_ring_groove_cutter` (component · squeeze and fill per common gland design): Groove cutter for an O-ring: face (axial) seal, piston (groove on the shaft) or rod (groove in the bore) [o-ring, groove, gland, seal, cutter]
- `push_fit_fitting` (component): Straight push-in (push-to-connect) pneumatic fitting with a male port thread and release collet [push fit, push-to-connect, pneumatic, fitting, pc fitting, bsp, m5]
- `tube` (component): Tube (pneumatic, hydraulic or structural) by outside and inside diameter [tube, pipe, hose, pneumatic]

## Manufacturing

- `manufacturing/cnc-machining` (guide): Corner radii, pocket depths, wall thickness, holes and threads, tolerances and setups for milled and turned parts [cnc, milling, machining, turning, tolerance, corner radius, pocket, drilling]
- `manufacturing/fdm-3d-printing` (guide): Wall thickness, overhangs, holes, clearances, orientation and fastening rules for parts printed on FDM printers [3d printing, fdm, fff, pla, petg, overhang, clearance, orientation]
- `manufacturing/injection-moulding` (guide): Uniform walls, draft angles, ribs and bosses, radii and undercuts for injection-moulded plastic parts [injection moulding, draft, wall thickness, rib, boss, undercut, gate]
- `manufacturing/laser-cutting` (guide): Kerf compensation, minimum features, tab-and-slot joints and material notes for laser-cut and waterjet parts [laser cutting, waterjet, kerf, sheet, acrylic, plywood, tab and slot]
- `manufacturing/resin-printing` (guide): Wall thickness, supports and orientation, hollowing with drain holes, clearances and post-curing for resin prints [resin, sla, msla, dlp, 3d printing, supports, drain holes, clearance]
- `manufacturing/sheet-metal` (guide): Bend radius, K-factor and bend allowance, minimum flanges, hole-to-bend distances, reliefs and hems [sheet metal, bend, k-factor, bend allowance, flange, relief, press brake]

## Tolerances, fits & materials

- `tolerances/fits-and-clearances` (guide): ISO 286 hole/shaft fits in plain terms, and practical clearances for machined, printed and laser-cut parts [fit, tolerance, clearance, iso 286, press fit, h7, machining]
- `tolerances/materials` (guide): Density, stiffness, strength and temperature limits of common metals and plastics, including 3D printing filaments [material, density, strength, modulus, aluminium, steel, pla, petg, abs, nylon, mass]

## Standards & reference

- `reference/metric-threads` (guide): Coarse and fine pitches, tap drills and ISO 273 clearance holes for M1.6 to M24 [thread, metric, pitch, tap drill, clearance hole, iso 261, iso 273]
- `reference/stock-sizes` (guide): Common metric sheet and plate thicknesses, rod, bar and tube sizes, and board and filament sizes to design around [stock, sheet, plate, rod, bar, tube, thickness, plywood, acrylic, filament]

## Modelling with build123d

- `modelling/builder-pitfalls` (guide): Mistakes that silently produce wrong geometry in build123d, and habits for big multi-part assemblies [build123d, builder, sketch, locations, pos, boolean, assembly, interference, performance]
- `modelling/face-selection` (guide): Selecting faces and edges by position and direction (not ids), sketching on a face, and editing selected geometry [build123d, face, edge, selection, sort_by_distance, filter_by, group_by, plane, sketch on face]
