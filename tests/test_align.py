"""align(): measuring a flipped setup and adjusting its work offset - the default outcome, not an alarm (2026-11-02: side
B went down 0.17° turned, BP3 halted the machine, recovery took many cards and the turn was worked out by hand).

PlateEmu is the test emulator with a plate on the bed at a true turn and offset: G38.3 finds its top or drops into its
holes, and Makera's pocket-centre routine (M480.9) touches the hole walls where a 2 mm ball really would (exact
ball-in-circle contact), reporting [PRB:..] touches in machine coordinates and writing X0 Y0 of the active WCS at the
centre, as the firmware does."""
import asyncio
import math

import pytest

import machine_link as ml
from test_machine_link import PROG1, Emu, _measured, _until, run  # noqa: F401

HOLES = [(-40.0, -25.0, 6.0), (40.0, 25.0, 6.0), (40.0, -25.0, 6.0)]     # nominal work XY and Ø of the register holes
G54 = (-100.0, -100.0, -54.0)                                             # side A's offsets (machine): the rough zero


class PlateEmu(Emu):
    """A plate whose nominal work frame sits at machine `origin`, turned `rot_deg` (CCW from above): machine =
    origin + R(rot) · work. Top at machine Z `top` (the probe tip on it), `thickness` thick, through holes HOLES (minus
    `missing`), the bed (-75) around it."""

    def __init__(self, *a, origin=(0.0, 0.0), rot_deg=0.0, holes=HOLES, missing=(), top=-55.0, half=(60.0, 45.0),
                 thickness=6.0, **kw):
        super().__init__(*a, **kw)
        self.o, self.th = origin, math.radians(rot_deg)
        self.holes = [h for i, h in enumerate(holes) if i not in missing]
        self.top, self.half, self.thickness = top, half, thickness
        self.surface_z = lambda x, y: self.top if self._on_plate(x, y) else -75.0

    def to_machine(self, x, y):
        c, s = math.cos(self.th), math.sin(self.th)
        return self.o[0] + c * x - s * y, self.o[1] + s * x + c * y

    def _to_work(self, x, y):
        c, s = math.cos(-self.th), math.sin(-self.th)
        dx, dy = x - self.o[0], y - self.o[1]
        return c * dx - s * dy, s * dx + c * dy

    def _on_plate(self, x, y):
        wx, wy = self._to_work(x, y)
        return abs(wx) <= self.half[0] and abs(wy) <= self.half[1]

    def _hole_at(self, x, y):
        """The hole whose free circle (radius - ball radius) holds the ball centre at machine x, y."""
        for hx, hy, d in self.holes:
            cx, cy = self.to_machine(hx, hy)
            if math.hypot(x - cx, y - cy) <= d / 2 - self.ball_r + 1e-6:
                return (cx, cy), d / 2
        return None

    def _surface(self, x, y):
        if self._hole_at(x, y):
            return self.top - self.thickness - 10.0                      # through: the probe can go well down
        return self.top if self._on_plate(x, y) else -75.0

    async def _g38(self, axis, dist, rate):
        if axis == "z" or self.probe3d_fail:
            return await super()._g38(axis, dist, rate)
        q = list(self.mpos)
        h = self._hole_at(q[0], q[1])
        if h is None:
            return await super()._g38(axis, dist, rate)
        (cx, cy), r_hole = h
        re = r_hole - self.ball_r                                        # the ball centre's free circle
        i, sgn = "xyz".index(axis), (1.0 if dist > 0 else -1.0)
        u = [0.0, 0.0]
        u[i] = sgn
        d = (q[0] - cx, q[1] - cy)
        du = d[0] * u[0] + d[1] * u[1]
        t = -du + math.sqrt(max(du * du - (d[0] ** 2 + d[1] ** 2 - re * re), 0.0))
        hit = t <= abs(dist)
        end = list(q)
        end[i] += sgn * (t if hit else abs(dist))
        await self._move_to(end, rate)
        self.state = "Run"
        self.replies.append(f"[PRB:{self.mpos[0]:.3f},{self.mpos[1]:.3f},{self.mpos[2]:.3f}:{int(hit)}]")
        if not hit:
            self.replies.append("ALARM: Probe fail")
            self.halted = True
            self.state = "Alarm"
            raise asyncio.CancelledError
        return list(self.mpos)


SPEC = {"wcs": "G55", "surface": {"x": 0.0, "y": 30.0}, "z_top": 0.0,
        "holes": [{"x": x, "y": y, "dia": d, "depth": 3.0} for x, y, d in HOLES], "rough": {"from": "G54"}}


async def _plate(rot_deg=0.17, shift=(0.35, -0.40), missing=(), tool=9999):
    emu = PlateEmu(speed=40.0, origin=(G54[0] + shift[0], G54[1] + shift[1]), rot_deg=rot_deg, missing=missing)
    port, _ = await emu.start()
    mc = ml.MachineControl()
    await mc.connect("127.0.0.1", port)
    mc._diag_t = float("inf")
    emu._wcs["G54"] = list(G54)                                           # side A was zeroed in G54
    await mc.arm(True); await mc.home(); await _until(lambda: mc.homed)
    emu.tool = tool
    await asyncio.sleep(0.3)
    _measured(mc, emu)
    assert await mc.read_offsets() is not None
    return emu, mc


def _no_alarm(emu, mc):
    assert b"\x18" not in emu.realtime and b"!" not in emu.realtime and emu.state != "Alarm" and not emu.halted
    assert mc.armed and not emu.crashes


def test_fit_rigid_turn_shift_residuals_and_spacing():
    th = math.radians(0.17)
    o = (-99.65, -100.4)
    nom = [(-40.0, -25.0), (40.0, 25.0), (40.0, -25.0)]
    meas = [(o[0] + math.cos(th) * x - math.sin(th) * y, o[1] + math.sin(th) * x + math.cos(th) * y) for x, y in nom]
    f = ml.fit_rigid(nom, meas)
    assert abs(f["rot_deg"] - 0.17) < 1e-9 and abs(f["origin"][0] - o[0]) < 1e-9 and abs(f["origin"][1] - o[1]) < 1e-9
    assert f["residual_max"] < 1e-9 and f["spacing_max"] < 1e-9
    f2 = ml.fit_rigid(nom[:2], meas[:2])                                  # two holes: exact
    assert abs(f2["rot_deg"] - 0.17) < 1e-9 and f2["residual_max"] < 1e-9
    meas[2] = (meas[2][0] + 0.2, meas[2][1])                              # hole 3 drilled 0.2 mm off
    f3 = ml.fit_rigid(nom, meas)
    assert 0.05 < f3["residual_max"] < 0.2 and abs(f3["spacing_err"][1]) > 0.1


def test_align_measures_a_017_degree_turn_and_adjusts_the_wcs():
    async def go():
        emu, mc = await _plate()
        try:
            info = mc.check_align(SPEC)
            assert "one approval" in info["summary"] and "roughly from G54" in info["summary"] and "M480.9" in info["summary"]
            assert info["start"]["rough"] and info["start"]["xy"] == [G54[0], G54[1]]
            n = len(emu.lines)
            res = await mc.align(SPEC)
            # measured to 0.01° / 0.01 mm, and adjusted
            assert res["adjusted"], res
            assert abs(res["rot_deg"] - 0.17) < 0.01 and abs(res["dx"] - 0.35) < 0.01 and abs(res["dy"] + 0.40) < 0.01, res
            assert res["residual_max"] < 0.01 and res["spacing_max"] < 0.01 and res["misses"] == 0
            assert "turned 0.17° counter-clockwise" in res["summary"] and "rotated by +0.17" in res["summary"]
            # G55 X0 Y0 is the true nominal origin, Z0 the probed top; G54 untouched
            g55 = emu._wcs["G55"]
            assert abs(g55[0] - emu.o[0]) < 0.01 and abs(g55[1] - emu.o[1]) < 0.01 and abs(g55[2] - emu.top) < 0.01, g55
            assert emu._wcs["G54"] == list(G54) and emu.active == "G55" and mc.active_wcs == "G55"
            # the first moves: select G55, rough X0 Y0 from G54, then over the surface point from that rough zero
            sent = emu.lines[n:]
            assert sent[0] == "G55" and sent[1] == "G10 L2 P2 X-100.000 Y-100.000", sent[:4]
            first_xy = next(s for s in sent if s.startswith("G53 G1 X"))
            assert first_xy == "G53 G1 X-100.000 Y-70.000 F600", first_xy
            assert sum(1 for s in sent if s.startswith("M480.9")) == 3 and not any(s.startswith(("G38.2", "M480.1")) for s in sent)
            _no_alarm(emu, mc)
            # recorded: rough, then the top by probe, then the aligned origin
            log = [e for e in mc.origin_log if e["wcs"] == "G55"]
            assert log[0]["source"] == "rough" and any(e["source"] == "probe" and e["axes"] == "z" for e in log)
            assert log[-1]["source"] == "align" and log[-1]["axes"] == "xy"
            assert mc.z0_state("G55")["verified"]
            al = mc.alignment("G55")
            assert al["valid"] and al["cam_align"] == {"rot_deg": res["rot_deg"], "dx": 0.0, "dy": 0.0}
            p = mc.payload()
            assert p["alignments"]["G55"]["valid"] and p["align"]["stage"] == "done"
            assert "G55 aligned" in mc.describe() and "turned by +0.17" in mc.describe()
            # a G55 program must be posted turned to match
            prep = ml.prepare_program(PROG1.replace("G54", "G55"), ["Face"])
            with pytest.raises(ml.Refused, match=r"turned \+0\.17.*without that turn"):
                mc.check_job(prep)
            prep["aligned"] = {"G55": al["rot_deg"]}
            notes = mc.check_job(prep)["notes"]
            assert any("G55 aligned: the program is turned +0.17" in t for t in notes)
            # anything that sets G55 X0 Y0 afterwards voids the alignment
            await mc.origin_at(-100.0, -100.0, "x", "typed", p="G55")
            assert not mc.alignment("G55")["valid"]
        finally:
            await mc.disconnect(); await emu.stop()
    run(go())


def test_align_finds_a_5_degree_turn_and_explains_without_adjusting_or_alarming():
    async def go():
        emu, mc = await _plate(rot_deg=5.0)
        try:
            res = await mc.align(SPEC)
            assert not res["adjusted"] and "turned 5.00° (limit 2°)" in res["why"], res
            assert abs(res["rot_deg"] - 5.0) < 0.01 and res["misses"] >= 1        # found by looking along the holes' arcs
            assert "NOT adjusted" in res["summary"] and "back at the start" in res["summary"]
            # no alarm, G55 X0 Y0 back at the rough start, recorded as rough; nothing to apply
            _no_alarm(emu, mc)
            assert emu._wcs["G55"][:2] == [G54[0], G54[1]]
            assert mc.origins["G55"]["xy"]["source"] == "rough"
            assert not mc.alignment("G55")["valid"] and mc.payload()["align"]["stage"] == "refused"
            prep = ml.prepare_program(PROG1.replace("G54", "G55"), ["Face"])
            with pytest.raises(ml.Refused, match="only rough"):
                mc.check_job(prep)
        finally:
            await mc.disconnect(); await emu.stop()
    run(go())


def test_a_hole_that_isnt_there_stops_cleanly():
    async def go():
        emu, mc = await _plate(missing=(2,))
        try:
            with pytest.raises(ml.Refused, match=r"stopped at 'hole 3 of 3'.*hole 3 .*not where it should be.*back at the start"):
                await mc.align(SPEC)
            _no_alarm(emu, mc)
            assert emu._wcs["G55"][:2] == [G54[0], G54[1]] and mc.origins["G55"]["xy"]["source"] == "rough"
            assert mc.payload()["align"]["stage"] == "failed" and mc.alignment("G55") is None
            assert emu.mpos[2] > -2.0                                     # left at the top of travel
        finally:
            await mc.disconnect(); await emu.stop()
    run(go())


def test_align_is_refused_up_front_when_it_couldnt_finish():
    async def go():
        emu, mc = await _plate(tool=6)
        try:
            n = len(emu.lines)
            with pytest.raises(ml.Refused, match=r"3D probe \(T9999\)"):
                mc.check_align(SPEC)
            emu.tool = 9999
            await asyncio.sleep(0.3)
            _measured(mc, emu)
            with pytest.raises(ml.Refused, match="no X0 Y0 set this session: give `rough`"):
                mc.check_align({k: v for k, v in SPEC.items() if k != "rough"})
            with pytest.raises(ml.Refused, match="2 or more holes"):
                mc.check_align({**SPEC, "holes": SPEC["holes"][:1]})
            with pytest.raises(ml.Refused, match="surface point is on a hole"):
                mc.check_align({**SPEC, "surface": {"x": -40.0, "y": -25.0}})
            with pytest.raises(ml.Refused, match="dia 2"):
                mc.check_align({**SPEC, "holes": [{**h, "dia": 2.0} for h in SPEC["holes"]]})
            with pytest.raises(ml.Refused, match="outside"):
                mc.check_align({**SPEC, "rough": {"x": -195.0, "y": -100.0, "z": -54.0}})
            await mc.arm(False)
            with pytest.raises(ml.Refused, match="Control is off"):
                mc.check_align(SPEC)
            assert len(emu.lines) == n                                    # nothing reached the machine
        finally:
            await mc.disconnect(); await emu.stop()
    run(go())
