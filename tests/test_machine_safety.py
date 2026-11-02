"""The 2026-11-02 latch job, replayed on the emulator: every way the machine side let the assistant hurt the Z1 that day
is refused now (latch-job-review-2026-11-02.md, Phase 1).

  14:23 a corner probe started 69 mm up; the firmware set Z0 at its touch while the app's guard sent a feed hold and said
        "probe never triggered"                         -> start-height rule, halt instead of hold, the touch reported,
                                                           the partial origin recorded and not trusted
  14:29 set_origin Z84.926 worked out from TLOs, then lower_to 3 drove T2 into the plate
                                                        -> typed Z refused (touch-off only), lower_to needs a probed Z0
  16:50 side-B probe ops ran in G55, never set          -> check_job checks the program's own WCS
  17:00 pocket_center from 5 mm above the hole          -> must start inside, probe_descend gets it there
  17:02 the refused lower_to resent as a raw `G1 Z-3`   -> the assistant's console has no motion words
"""
import asyncio

import pytest

import machine_link as ml
from test_machine_link import PROG1, Emu, _at_top_over, _measured, _probed, _setup, _until, run  # noqa: F401


async def _ready(mc, emu, tool=None):
    await mc.arm(True); await mc.home(); await _until(lambda: mc.homed)
    if tool is not None:
        emu.tool = tool
        await asyncio.sleep(0.3)
    _measured(mc, emu)


# ---------------------------------------------------------------- Z origin and lower_to
def test_typed_z_is_refused_touch_off_is_recorded_with_its_facts_and_the_tlo_reference():
    async def go():
        emu, mc, _ = await _setup()
        try:
            await _ready(mc, emu)                                    # T6 fitted and measured
            n = len(emu.lines)
            with pytest.raises(ml.Refused, match="typed Z .*touch-off.*auto_z_probe"):
                await mc.set_origin(None, None, 84.926)              # 14:29: Z0 worked out by hand
            with pytest.raises(ml.Refused, match="typed Z"):
                mc.check_set_origin(z=-1.5)
            assert len(emu.lines) == n                               # nothing reached the machine
            # a touch-off: the card says what it replaces and how far Z0 moves
            info = mc.check_set_origin(z=0.0)
            assert "TOUCH-OFF" in info["facts"] and "replaces G54 Z0 set before this session" in info["facts"]
            assert "moves it +60.360 mm" in info["facts"] and "T6 becomes the tool-length reference" in info["facts"]
            await mc.set_origin(None, None, 0.0)
            assert emu.lines[-1] == "G10 L20 P0 Z0.000" and abs(emu.wco[2] + 1.0) < 1e-6
            z = mc.payload()["origin"]["z"]
            assert z["source"] == "touch-off" and z["verified"] and z["tool"] == 6 and abs(z["delta"] - 60.36) < 1e-3
            assert mc.tlo_ref["tool"] == 6 and mc.origin_log[-1]["axes"] == "z" and abs(mc.origin_log[-1]["delta"][0] - 60.36) < 1e-3
            facts = mc.check_set_origin(z=0.5)["facts"]
            assert "replaces G54 Z0 set by touch-off at the head (Z0) (touch-off) at " in facts and "moves it -0.500 mm (DOWN" in facts
            d = mc.describe()
            assert "tool-length reference T6" in d and "G54 (active): " in d and "Z top by touch-off" in d
        finally:
            await mc.disconnect(); await emu.stop()
    run(go())


def test_lower_to_needs_a_probed_z0_and_comes_down_slowly_for_the_last_10_mm():
    async def go():
        emu, mc, _ = await _setup()
        try:
            await _ready(mc, emu)
            emu.wco[2] = -55.0                                       # Z0 from an earlier session (or typed): not trusted
            await asyncio.sleep(0.4)
            n = len(emu.lines)
            with pytest.raises(ml.Refused, match="not set this session.*Probe it first"):
                await mc.lower_to(5)
            mc._record_origin(z=("top", "console G10 L20 P0 Z0"), source="typed")
            with pytest.raises(ml.Refused, match=r"\(typed\).*not probed"):
                await mc.lower_to(5)
            assert len(emu.lines) == n
            _probed(mc)
            info = mc.check_lower_to(5)
            assert abs(info["mz"] + 50.0) < 1e-6 and "machine Z -50.000" in info["summary"] and "100 mm/min" in info["summary"]
            res = await mc.lower_to(5, wait=True)                    # returns once it has arrived
            assert emu.lines[n:] == ["G53 G1 Z-40.000 F300", "G53 G1 Z-50.000 F100"]
            assert abs(emu.mpos[2] + 50.0) < 1e-3 and "final" in res and "state Idle" in res["final"]
            # something else moves Z0 afterwards (same tool fitted): no longer trusted
            emu.wco[2] = -57.0
            await asyncio.sleep(0.4)
            with pytest.raises(ml.Refused, match="changed after"):
                mc.check_lower_to(3)
            assert "CHANGED since" in mc.describe()
        finally:
            await mc.disconnect(); await emu.stop()
    run(go())


# ---------------------------------------------------------------- 3D probe
def test_corner_probe_start_height_ball_vs_hole_and_the_descend_helper():
    async def go():
        emu, mc, _ = await _setup()
        try:
            await _ready(mc, emu, tool=9999)
            await _at_top_over(mc, emu, -96.28, -99.45)              # over the stock's back-right corner, at the top
            with pytest.raises(ml.Refused, match="needs a probed reference.*probe_descend"):
                mc.check_probe_3d("outside_back_right")              # nothing probed this session
            emu.wco[2] = -55.0
            await asyncio.sleep(0.4)
            _probed(mc)
            with pytest.raises(ml.Refused, match=r"54\.00 mm above G54 Z0.*must start 1 .. 10.5"):
                mc.check_probe_3d("outside_back_right")              # 14:23: 69 mm up
            with pytest.raises(ml.Refused, match="BALL diameter.*hole_dia"):
                mc.check_probe_3d("pocket_center", dia=8.0)          # the hole's Ø passed as the ball
            with pytest.raises(ml.Refused, match="hole_dia is for pocket_center"):
                mc.check_probe_3d("outside_back_right", hole_dia=8.0)
            await mc.lower_to(5, wait=True)
            info = mc.check_probe_3d("outside_back_right")
            assert "5.0 mm above G54 Z0" in info["summary"] and "HALTS" in info["summary"]
            assert abs(info["guard"]["z_floor"] - (-58.0)) < 1e-6    # 3 mm under the probed Z0, not 10 under the start
            # a fresh session's way to a reference: probe_descend finds the surface (G38.3) and backs off 5 mm
            mc.origins = {}
            await mc.raise_z_top(wait=True)
            emu.mpos[2] = -40.0                                      # 15 mm above the stock top
            await asyncio.sleep(0.4)
            n = len(emu.lines)
            await mc.probe_descend(max_dist=25)
            assert emu.lines[n] == "G38.3 Z-25.000 F100" and mc.routine["touched"] and "5 mm above it" in mc.routine["msg"]
            await _until(lambda: not mc.jog_busy, timeout=10)
            await asyncio.sleep(0.3)
            assert abs(emu.mpos[2] + 50.0) < 1e-3 and abs(mc.surface_touch["mz"] + 55.0) < 1e-3
            info = mc.check_probe_3d("outside_back_right")           # 5 mm above the touched surface
            assert "probe_descend touched" in info["summary"]
            assert not emu.crashes
        finally:
            await mc.disconnect(); await emu.stop()
    run(go())


def test_pocket_center_must_start_inside_the_hole_and_hole_dia_sizes_the_search():
    async def go():
        emu, mc, _ = await _setup()
        try:
            await _ready(mc, emu, tool=9999)
            emu.wco[2] = -55.0
            emu.pocket = (-150.0, -130.0, -150.0, -130.0, -60.0)     # a 20 mm (square) hole, 5 deep
            await asyncio.sleep(0.4)
            _probed(mc)
            await _at_top_over(mc, emu, -140.0, -140.0)
            await mc.lower_to(5, wait=True)
            with pytest.raises(ml.Refused, match="INSIDE the hole.*probe_descend"):
                mc.check_probe_3d("pocket_center", hole_dia=20)      # 17:00: 5 mm above the hole
            # off-centre over solid stock: the descent touches the top and says so (no alarm), backing off 1 mm
            await mc.goto_xy_raised(-120.0, -140.0, 600, wait=True)
            await mc.lower_to(5, wait=True)
            with pytest.raises(ml.Refused, match="NOT over the hole"):
                await mc.probe_descend(to_z=-3)
            assert emu.state != "Alarm" and not emu.halted
            await _until(lambda: not mc.jog_busy, timeout=10)
            await mc.goto_xy_raised(-140.0, -140.0, 600, wait=True)
            await mc.lower_to(5, wait=True)
            await mc.probe_descend(to_z=-3)
            assert mc.routine["stage"] == "done" and "inside the hole" in mc.routine["msg"]
            await asyncio.sleep(0.3)
            info = mc.check_probe_3d("pocket_center", hole_dia=20)
            assert info["command"] == "M480.9 X12 Y12 Z2 D2"          # search hole/2 + 2 each way; D stays the 2 mm ball
            await mc.probe_3d("pocket_center", hole_dia=20)
            await _until(lambda: mc.routine["stage"] in ("done", "failed"), timeout=30)
            assert mc.routine["stage"] == "done", mc.routine
            assert abs(mc.routine["size"]["x"] - 20.0) < 0.03 and abs(emu.wco[0] + 140.0) < 0.02
            assert mc.payload()["origin"]["xy"]["source"] == "probe"
            assert not emu.crashes
        finally:
            await mc.disconnect(); await emu.stop()
    run(go())


def test_a_routine_stopped_after_its_touch_says_so_and_its_partial_origin_is_not_trusted():
    """14:23 replayed: the firmware touched and set Z0, then the app stopped the routine. The message must not say the
    probe didn't trigger, and the Z0 the firmware left is recorded as partial - lower_to and probe starts refuse it."""
    async def go():
        emu, mc, _ = await _setup()
        try:
            await _ready(mc, emu, tool=9999)
            emu.wco[2] = -53.0                                       # a Z0 probed 2 mm too high
            await asyncio.sleep(0.4)
            _probed(mc)
            await _at_top_over(mc, emu, -96.28, -99.45)
            await mc.lower_to(5, wait=True)                          # really 7 above the stock top
            await mc.probe_3d("outside_back_right")
            # once the Z touches are in (the firmware has set Z0), the head moves out past X: shrink the area so that trips
            await _until(lambda: len(mc._prbs(0)) >= 2, timeout=20)
            g = mc.routine["guard"]
            g["box"] = (g["box"][0] + 25.0, g["box"][1], g["box"][2], g["box"][3])
            await _until(lambda: mc.routine["stage"] in ("done", "failed"), timeout=20)
            msg = mc.routine["msg"]
            assert mc.routine["stage"] == "failed" and "left the routine's area" in msg and "HALT sent" in msg
            assert "did report 2 touches, the first at machine Z -55.000" in msg and "is the probe triggering" not in msg
            assert "moved the work origin (Z by -2.000 mm)" in msg and "NOT trusted" in msg
            await _until(lambda: b"\x18" in emu.realtime, timeout=3)
            assert mc.payload()["origin"]["z"]["source"] == "routine-partial"
            assert not mc.z0_state()["verified"] and mc.origin_log[-1]["source"] == "routine-partial"
            await mc.recover()
            with pytest.raises(ml.Refused, match="not probed"):
                mc.check_lower_to(5)
        finally:
            await mc.disconnect(); await emu.stop()
    run(go())


# ---------------------------------------------------------------- console
def test_the_assistants_console_cannot_move_the_machine_and_nobody_sends_m480():
    async def go():
        emu, mc, _ = await _setup()
        try:
            await _ready(mc, emu)
            n = len(emu.lines)
            for bad in ("G1 Z-3", "g0 x10", "G53 G1 Z-50 F300", "G38.2 Z-5 F100", "G38.3 Z-5", "G10 L20 P0 Z0", "G92 Z0", "G28",
                        "G30", "G91", "G20", "X10", "$J Z-1 F0.5", "M3 S10000", "M480.9 X10 Y10 Z2 D2", "play /sd/gcodes/x.nc", "abort"):
                with pytest.raises(ml.Refused):
                    await mc.console(bad, assistant=True)
            assert len(emu.lines) == n                               # nothing reached the machine
            with pytest.raises(ml.Refused, match="A refused move is final"):
                mc.check_console("G1 Z-3", assistant=True)            # refused before any approval card
            assert mc.check_console("$#", assistant=True)["read_only"]
            assert "selects work offset G55" in mc.check_console("G55", assistant=True)["summary"]
            with pytest.raises(ml.Refused, match="M480"):
                await mc.console("M480.9 X10 Y10 Z2 D2")             # not from the user's console either
            await mc.console("$#", assistant=True)                   # read-only queries are fine
            await mc.console("G55", assistant=True)                  # selecting a work offset is tracked
            await _until(lambda: emu.active == "G55")
            assert mc.active_wcs == "G55"
            await mc.console("g54", assistant=True)
            await mc.console("M5", assistant=True)
            await _until(lambda: emu.lines[-1] == "M5")
            assert mc.active_wcs == "G54" and emu.active == "G54"
            n = len(emu.lines)
            await mc.console("G0 Y-5")                               # the user's own console is unchanged
            await _until(lambda: len(emu.lines) > n)
            assert emu.lines[n] == "G0 Y-5"
            await _until(lambda: mc.link.status["state"] == "Idle" and emu.state == "Idle", timeout=10)
            await mc.console("G10 L20 P0 Z0")                        # a typed Z0 from the user: recorded as typed, not probed
            z = mc.payload()["origin"]["z"]
            assert z["source"] == "typed" and not z["verified"] and mc.payload()["origin"].get("xy") is None
        finally:
            await mc.disconnect(); await emu.stop()
    run(go())


# ---------------------------------------------------------------- per-setup WCS
G55_PROG = PROG1.replace("G54", "G55")


def test_offsets_are_read_and_check_job_uses_the_programs_own_wcs():
    async def go():
        emu, mc, _ = await _setup()
        try:
            await _ready(mc, emu)
            emu.wco = [-150.0, -150.0, -60.0]                        # G54: side A
            await asyncio.sleep(0.4)
            _probed(mc)
            t = await mc.read_offsets()
            assert t["G54"][:3] == [-150.0, -150.0, -60.0] and t["G55"][:3] == [0.0, 0.0, 0.0] and t["tlo"] == 0.0
            assert mc.active_wcs == "G54"
            assert mc._prbs(0) == []                                  # the $# reply's [PRB:] is history, not a touch
            prep = ml.prepare_program(G55_PROG, ["Face"])
            assert prep["wcs"] == "G55" and prep["wcs_list"] == ["G55"]
            mc.check_job(ml.prepare_program(PROG1, ["Face"]))         # G54: zeroed, fits
            with pytest.raises(ml.Refused, match="G55, which has never been set"):
                mc.check_job(prep)                                    # 16:50: side B with G55 unset
            # X0 Y0 of G55 from machine coordinates (G10 L2 P2): not the active WCS, nothing else changes
            info = mc.check_origin_at(-140.0, -150.0, p="G55")
            assert info["command"] == "G10 L2 P2 X-140.000 Y-150.000" and "replaces G55 X0 Y0 set before this session" in info["facts"]
            await mc.origin_at(-140.0, -150.0, "front-left", "side B corner", p=2)
            await _until(lambda: emu._wcs["G55"][0] == -140.0)
            assert emu._wcs["G55"][:2] == [-140.0, -150.0] and emu.active == "G54" and mc.active_wcs == "G54"
            await _until(lambda: (mc.wcs_table or {}).get("G55", [0])[0] == -140.0, timeout=3)
            with pytest.raises(ml.Refused, match="runs in G55, but Z of its origin was not set"):
                mc.check_job(prep)                                    # Z0 of G55 still unset
            # Z0 in G55: select it and touch off (set_origin with p selects first)
            emu.mpos[2] = -60.0
            await asyncio.sleep(0.4)
            await mc.set_origin(None, None, 0.0, p="G55")
            assert emu.active == "G55" and mc.active_wcs == "G55" and emu.lines[-2:] == ["G55", "G10 L20 P0 Z0.000"]
            chk = mc.check_job(prep)
            assert chk["wcs"] == "G55" and any("runs in G55" in x for x in chk["notes"])
            # the extents are checked in G55's frame: X 40 fits from -140 (G55) though it is the same file
            far = ml.prepare_program(G55_PROG.replace("X40", "X145"), ["Face"])
            with pytest.raises(ml.Refused, match="from the G55 work origin"):
                mc.check_job(far)
            # and G54 is checked against its own offsets even with G55 active (from the $# read)
            assert abs(mc.check_job(ml.prepare_program(PROG1, ["Face"]))["work_origin"][0] + 150.0) < 1e-6
            d = mc.describe()
            assert "work position (G55)" in d and "G55 (active)" in d and "G54: XY test" in d
        finally:
            await mc.disconnect(); await emu.stop()
    run(go())


PROBE_FIRST = """; AgenticCAD makera post: probe first
G21 G90 G17
G55
; Probe top [probe] T9999
M6 T9999
; PROBE BEGIN Probe top
G0 Z10
G1 Z5 F300
G38.2 Z-10 F100
G10 L20 P0 Z0
G0 Z10
; PROBE END
; Face [face] T6 flat D3.175
M6 T6
M3 S10000
G0 X10 Y10
G1 Z-0.5 F300
G1 X40 F1200
G0 Z5
M5
"""


def test_a_program_whose_own_probe_ops_set_the_origin_still_needs_a_rough_zero_this_session():
    prep = ml.prepare_program(PROBE_FIRST, ["Probe top", "Face"])
    assert prep["probes"][0]["before_cut"] and prep["probes"][0]["wcs"] == "G55" and prep["probes"][0]["sets"] == ["z"]
    late = ml.prepare_program(PROBE_FIRST.replace("G21 G90 G17\n", "G21 G90 G17\nM6 T6\nM3 S9000\nM5\n"),
                              ["Probe top", "Face"])
    assert not late["probes"][0]["before_cut"]                       # a cut (spindle on) came first

    async def go():
        emu, mc, _ = await _setup()
        try:
            await _ready(mc, emu)
            emu._wcs["G55"] = [-150.0, -150.0, -50.0]
            await mc.read_offsets()
            await mc.origin_at(-150.0, -150.0, "front-left", "side B corner", p="G55")
            # the probe op sets Z exactly, but its first moves use G55's Z as it stands: a rough Z0 this session first
            with pytest.raises(ml.Refused, match="a rough zero is enough"):
                mc.check_job(prep)
            with pytest.raises(ml.Refused, match="Z of its origin was not set"):
                mc.check_job(late)
            assert any("G55: Z not set this session - acknowledged" in x for x in mc.check_job(late, ack=True)["notes"])
            _probed(mc, wcs="G55")                                    # e.g. auto_z_probe on the flipped plate
            assert any("runs in G55" in x for x in mc.check_job(prep)["notes"])
        finally:
            await mc.disconnect(); await emu.stop()
    run(go())


# ---------------------------------------------------------------- recovery, waits, status
def test_recover_from_a_hold_and_an_alarm_and_refuse_the_e_stop():
    async def go():
        emu, mc, _ = await _setup()
        try:
            await _ready(mc, emu)
            emu.speed = 1.0
            await mc.jog("y", -10)
            await mc.hold()
            await _until(lambda: mc.link.status["state"] == "Hold", timeout=3)
            assert not mc.payload()["position_reliable"] and "POSITION NOT RELIABLE" in mc.describe()
            emu.speed = 40.0
            plan = mc.check_recover()["summary"]
            assert plan.startswith("Recover from Hold: HALT") and "unlock ($X), then Control on, then home" in plan
            res = await mc.recover()
            assert res["recover"]["steps"][:3] == ["halt (clears the queued moves)", "unlock ($X)", "Control on"]
            assert res["recover"]["steps"][-1] == "homed" and mc.homed and mc.armed and emu.mpos == [-1.0, -1.0, -1.0]
            assert mc.tool_ok() and "state Idle" in res["final"]      # the same tool: still measured
            # an alarm (manual stop) is cleared the same way
            await mc.halt()
            await _until(lambda: mc.link.status["state"] == "Alarm", timeout=3)
            res = await mc.recover()
            assert res["recover"]["steps"][0] == "unlock ($X)" and mc.homed
            # the e-stop is not: the user releases it
            emu.halt_code = 13
            await mc.halt()
            await _until(lambda: (mc.link.status.get("halt") or {}).get("code") == 13, timeout=3)
            n = len(emu.lines)
            with pytest.raises(ml.Refused, match="emergency stop.*recover doesn't clear"):
                await mc.recover()
            assert len(emu.lines) == n
        finally:
            await mc.disconnect(); await emu.stop()
    run(go())


def test_moves_can_wait_until_done_and_return_the_final_state():
    async def go():
        emu, mc, _ = await _setup()
        try:
            await _ready(mc, emu)
            res = await mc.goto_xy(-50.0, -60.0, 600, wait=True)
            assert abs(emu.mpos[0] + 50.0) < 1e-6 and "machine position -50.000, -60.000" in res["final"]
            emu.mpos[2] = -20.0
            await asyncio.sleep(0.4)
            res = await mc.goto_xy_raised(-70.0, -60.0, 600, wait=True)
            assert emu.mpos[2] > -2 and "machine position -70.000, -60.000" in res["final"]
            res = await mc.home(wait=True)
            assert mc.homed and "homed this session: yes" in res["final"]
            # a move that ends in a hold reports it instead of pretending it arrived
            emu.speed = 1.0
            t = asyncio.create_task(mc.goto_xy(-100.0, -60.0, 600, wait=True))
            await asyncio.sleep(0.5)
            await mc.hold()
            with pytest.raises(ml.Refused, match="stopped with the machine Hold"):
                await t
        finally:
            await mc.disconnect(); await emu.stop()
    run(go())


def test_report_time_advances_only_on_a_new_status():
    """The Z1 answers some '?' with the very report it sent before; the recorder keys on report_t, which skips those."""
    async def go():
        emu, mc, _ = await _setup()
        try:
            seen_status, seen_report = set(), set()
            t0 = asyncio.get_running_loop().time()
            while asyncio.get_running_loop().time() - t0 < 1.6:      # an Idle machine: the same report every time
                seen_status.add(mc.link.status_t); seen_report.add(mc.link.report_t)
                await asyncio.sleep(0.02)
            assert len(seen_status) >= 5 and len(seen_report) <= 3   # only the 1 s heartbeat
            emu.mpos[0] = -2.0                                       # a change shows at once
            r0 = mc.link.report_t
            await _until(lambda: mc.link.report_t > r0 and mc.link.status["mpos"][0] == -2.0, timeout=1)
        finally:
            await mc.disconnect(); await emu.stop()
    run(go())


def test_spindle_drive_effort_from_m957_is_opt_in_and_stays_out_of_the_log():
    async def go():
        emu, mc, _ = await _setup()
        try:
            assert mc.spindle_load_poll["on"] is False               # off until watched once on the machine
            emu.rpm, emu.pwm = 13000.0, 0.42
            await asyncio.sleep(0.8)
            oks = sum(1 for _, t in mc.link.log if t == "ok")
            assert "M957" not in emu.queries and mc.link.status["spindle"]["pwm"] is None
            mc.set_spindle_load_poll(True, hz=4)
            await _until(lambda: mc.link.status["spindle"].get("pwm") == 0.42, timeout=3)
            assert "spindle 13000.0 rpm, drive 0.42" in mc.describe()
            assert mc.payload()["status"]["spindle"]["pwm_t"] is not None
            await _until(lambda: emu.queries.count("M957") >= 2, timeout=3)
            assert not any("M957" in t or "PWM value" in t for _, t in mc.link.log)       # neither the lines nor their oks
            assert sum(1 for _, t in mc.link.log if t == "ok") == oks
            # spindle off: no polls; the last reading goes stale (None) after 2 s
            emu.rpm = 0.0
            await asyncio.sleep(0.6)
            k = emu.queries.count("M957")
            await asyncio.sleep(2.0)
            assert emu.queries.count("M957") == k and mc.link.status["spindle"]["pwm"] is None
            assert "drive" not in mc.describe()
            mc.set_spindle_load_poll(False)
            assert mc.link.poll_spindle_load is False
            # a user's own line still shows its answer
            await mc.console("$G")
            await _until(lambda: any(t.startswith("[G0 G54") for _, t in mc.link.log), timeout=2)
        finally:
            await mc.disconnect(); await emu.stop()
    run(go())
