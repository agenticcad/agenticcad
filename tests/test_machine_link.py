"""Machine control against the Z1 emulator: the protocol, and above all the safety rules (the emulator, like the
real firmware, does not stop a jog past the end of travel; these tests prove machine_link refuses it first)."""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import machine_link as ml  # noqa: E402
from z1_emu import Z1Emu  # noqa: E402


def run(coro):
    return asyncio.run(coro)


async def _setup(**kw):
    emu = Z1Emu(speed=kw.pop("speed", 40.0), **kw)
    port, cam = await emu.start()
    mc = ml.MachineControl()
    await mc.connect("127.0.0.1", port)
    mc._diag_t = float("inf")              # no background switch-state reads: tests compare exact command sequences
    return emu, mc, cam


def _measured(mc, emu):
    """Jog-mechanics tests: treat the emulator's fitted tool as measured (the change-tool test does it for real)."""
    mc.measured = {"tool": emu.tool, "touch_z": emu.setter_z, "offset": -17.297, "t": 0}


async def _until(cond, timeout=5.0):
    t0 = asyncio.get_running_loop().time()
    while not cond():
        if asyncio.get_running_loop().time() - t0 > timeout:
            raise AssertionError("timed out")
        await asyncio.sleep(0.02)


def test_frames_and_status_parse():
    f = ml.frame(ml.T_LINE, b"$H\n")
    buf = bytearray(b"junk" + f + f[:5])
    got = list(ml.parse_frames(buf))
    assert got == [(ml.T_LINE, b"$H\n")] and bytes(buf) == f[:5]          # the partial frame waits for the rest
    bad = bytearray(f[:-4] + b"\x00\x00" + f[-2:])
    assert list(ml.parse_frames(bad)) == []                                # a bad CRC is dropped
    # the real Z1's reply (captured 2026-10-17)
    st = ml.parse_status("<Idle|MPos:-1.0000,-1.0000,-1.0000,0.0000,0.0000|WPos:147.8000,124.4000,60.3600,-180.0000,0.0000"
                         "|F:0.0,2000.0,100.0|S:0.0,10000.0,100.0,0,27.2,27.4,0,0,0,0|T:6,-17.297,-1|L:0, 0, 0, 0.0,100.0"
                         "|C:3,1,0,1|E:0,0,0,1,7613|OTA:0,0>")
    assert st["state"] == "Idle" and st["mpos"] == [-1, -1, -1] and st["wpos"] == [147.8, 124.4, 60.36]
    assert st["tool"] == {"number": 6, "offset": -17.297} and st["spindle"]["rpm"] == 0 and "C" in st["other"]


def test_connect_is_monitor_only_and_jogging_needs_control_and_homing():
    async def go():
        emu, mc, _ = await _setup()
        try:
            assert mc.connected and not mc.armed and not mc.homed
            with pytest.raises(ml.Refused, match="Control is off"):
                await mc.jog("x", -1)
            with pytest.raises(ml.Refused, match="Control is off"):
                await mc.home()
            await mc.arm(True)
            with pytest.raises(ml.Refused, match="home the machine first"):
                await mc.jog("x", -1)
            await mc.home()
            await _until(lambda: mc.homed)
            assert emu.mpos == [-1.0, -1.0, -1.0]
            with pytest.raises(ml.Refused, match="hasn't been measured"):
                await mc.jog("x", -5)                      # the rule: no motion before the fitted tool is measured
            _measured(mc, emu)
            assert [l for l in emu.lines if l.startswith("$H")]
            await mc.jog("x", -5)
            await _until(lambda: not mc.jog_busy)
            assert abs(emu.mpos[0] + 6.0) < 1e-6
            assert emu.lines[-1].startswith("$J X-5.000 F0.5")
            assert not emu.crashes
        finally:
            await mc.disconnect(); await emu.stop()
    run(go())


def test_limits_steps_and_z_floor_are_enforced_before_anything_is_sent():
    async def go():
        emu, mc, _ = await _setup()
        try:
            await mc.arm(True); await mc.home(); await _until(lambda: mc.homed); _measured(mc, emu)
            n = len(emu.lines)
            with pytest.raises(ml.Refused, match="outside"):
                await mc.jog("x", 1)                       # past the home end (machine 0)
            with pytest.raises(ml.Refused, match="step limit"):
                await mc.jog("y", -10.5)
            with pytest.raises(ml.Refused, match="step limit for Z down"):
                await mc.jog("z", -2)                      # Z down is 1 mm per click at most
            with pytest.raises(ml.Refused, match="unknown axis"):
                await mc.jog("a", 1)
            assert len(emu.lines) == n                     # nothing reached the machine
            # walk X to the far end in 10 mm steps: the last step that would leave the box is refused
            for _ in range(30):
                try:
                    await mc.jog("x", -10)
                except ml.Refused as e:
                    assert "outside" in str(e); break
                await _until(lambda: not mc.jog_busy)
            assert emu.mpos[0] >= -mc.profile.travel["x"] + mc.profile.margin - 1e-6 and not emu.crashes
            # Z floor in work coordinates: the work zero is 61.36 below home, so Z can come down to WPos 2 mm only
            mc.set_z_floor(59.0)                           # pretend the stock top is 59 mm above work zero
            emu.mpos[2] = -1.5                              # WPos Z = 59.86
            await asyncio.sleep(0.4)
            await mc.jog("z", -0.8)
            await _until(lambda: not mc.jog_busy)
            with pytest.raises(ml.Refused, match="Z floor"):
                await mc.jog("z", -0.8)                    # 59.06 - 0.8 < 59
        finally:
            await mc.disconnect(); await emu.stop()
    run(go())


def test_one_jog_at_a_time_and_hold_halt_always_work():
    async def go():
        emu, mc, _ = await _setup(speed=1.0)          # real-time moves: a 10 mm jog takes a second
        try:
            await mc.arm(True); await mc.home()
            emu.speed = 50.0
            await _until(lambda: mc.homed)
            emu.speed = 1.0
            _measured(mc, emu)
            await mc.jog("y", -10)
            with pytest.raises(ml.Refused):
                await mc.jog("y", -10)                     # still moving
            await mc.arm(False)
            await mc.hold()                                # hold works with Control off
            await _until(lambda: emu.state == "Hold")
            with pytest.raises(ml.Refused, match="Control is off"):
                await mc.resume()
            await mc.halt()                                # halt always works: Alarm, Control off, homing needed again
            await _until(lambda: (mc.link.status or {}).get("state") == "Alarm")
            assert not mc.armed and not mc.homed
            assert mc.link.status["halt"]["code"] == 1 and mc.link.status["halt"]["reason"] == "manual stop"
            with pytest.raises(ml.Refused, match=r"Alarm \(manual stop\): press Unlock"):
                await mc.arm(True)                         # can't arm in Alarm; the message says what to do
            await mc.unlock()
            await _until(lambda: (mc.link.status or {}).get("state") == "Idle")
            await mc.arm(True)
            with pytest.raises(ml.Refused, match="home the machine first"):
                await mc.jog("y", 1)
        finally:
            await mc.disconnect(); await emu.stop()
    run(go())


def test_switches_and_connection_cap_and_lost_link():
    async def go():
        emu, mc, _ = await _setup(max_clients=1)
        try:
            await mc.switch("dust", True)
            await _until(lambda: emu.switches["extendout"])
            await mc.switch("dust", False)
            await _until(lambda: not emu.switches["extendout"])
            assert "M851" in emu.lines and "M852" in emu.lines
            other = ml.MachineControl()
            with pytest.raises(ConnectionError, match="maximum number of connections"):
                await other.connect("127.0.0.1", emu.port)
            await mc.arm(True)
            await emu.stop()                               # the machine goes away
            for w in list(mc.link.writer and [mc.link.writer] or []):
                w.close()
            await _until(lambda: not mc.armed, timeout=4)
        finally:
            await mc.disconnect()
    run(go())


def test_camera_relay_shares_one_stream():
    async def go():
        emu = Z1Emu()
        _, cam = await emu.start()
        relay = ml.CameraRelay()
        relay.set_host("127.0.0.1", cam)
        try:
            jpg = await relay.snapshot()
            assert jpg[:2] == b"\xff\xd8" and len(jpg) > 1000
            g1, g2 = relay.frames(), relay.frames()
            a = await asyncio.wait_for(g1.__anext__(), 5)
            b = await asyncio.wait_for(g2.__anext__(), 5)
            assert a[:2] == b[:2] == b"\xff\xd8" and relay.viewers == 2
            await g1.aclose(); await g2.aclose()
            assert relay.viewers == 0
            # switching machines must not hand out the old machine's frame, or an empty one
            emu2 = Z1Emu()
            _, cam2 = await emu2.start()
            relay.set_host("127.0.0.1", cam2)
            assert relay.frame is None and relay.frame_t == 0
            jpg2 = await relay.snapshot()
            assert jpg2 and jpg2[:2] == b"\xff\xd8"
            await emu2.stop()
        finally:
            relay.set_host(None)
            await emu.stop()
    run(go())


def test_goto_xy_needs_z_at_top_and_stays_in_the_box_and_laser_switch():
    async def go():
        emu, mc, _ = await _setup()
        try:
            await mc.arm(True); await mc.home(); await _until(lambda: mc.homed); _measured(mc, emu)
            await mc.switch("probe_laser", True)
            await _until(lambda: emu.switches["probe_laser"])
            await mc.goto_xy(-191.28, -194.45, 300)          # the Z1's Anchor 1 (live config)
            await _until(lambda: not mc.jog_busy, timeout=10)
            assert abs(emu.mpos[0] + 191.28) < 1e-6 and abs(emu.mpos[1] + 194.45) < 1e-6 and emu.mpos[2] == -1.0
            assert emu.lines[-1] == "G53 G1 X-191.280 Y-194.450 F300"
            with pytest.raises(ml.Refused, match="outside"):
                await mc.goto_xy(-205, -100, 300)              # past the far end of X
            with pytest.raises(ml.Refused, match="feed"):
                await mc.goto_xy(-100, -100, 5000)
            emu.mpos[2] = -10.0                                  # Z no longer at the top
            await asyncio.sleep(0.4)
            with pytest.raises(ml.Refused, match="Z at the top"):
                await mc.goto_xy(-100, -100, 300)
            assert not emu.crashes
        finally:
            await mc.disconnect(); await emu.stop()
    run(go())


def test_unsafe_z1_commands_are_never_sent():
    async def go():
        emu, mc, _ = await _setup()
        try:
            for bad in ("M490", "M490.0", "m490", "M498.2"):
                with pytest.raises(ml.Refused, match="unsafe"):
                    await mc.link.line(bad)
            for ok in ("M490.1", "M490.2", "M498.1", "M4900"):
                assert not ml.BLOCKED.search(ok), ok
            assert not any(l.upper().startswith(("M490", "M498.2")) for l in emu.lines)
        finally:
            await mc.disconnect(); await emu.stop()
    run(go())


def test_change_tool_uses_makeras_m6_and_only_then_allows_motion():
    async def go():
        emu, mc, _ = await _setup()
        try:
            await mc.arm(True); await mc.home(); await _until(lambda: mc.homed)
            with pytest.raises(ml.Refused, match="hasn't been measured"):
                await mc.jog("y", -1)
            n = len(emu.lines)
            await mc.change_tool(0)
            await _until(lambda: len(emu.lines) > n)
            assert emu.lines[n:] == ["M5", "M6 T0"]                  # spindle off first, then Makera's change
            await _until(lambda: mc.routine["stage"] == "awaiting", timeout=15)       # the machine is in TOOL state
            assert emu.state == "Tool" and emu.mpos[:2] == [-11.6, -14.6]
            await mc.confirm_tool_change()
            await _until(lambda: emu.lines[-1] == "M490.2")
            await _until(lambda: mc.routine["stage"] in ("done", "failed"), timeout=30)
            assert mc.routine["stage"] == "done", mc.routine
            assert emu.tool == 0 and mc.tool_ok() and abs(mc.measured["touch_z"] - emu.setter_z) < 1e-3
            await mc.jog("y", -1); await _until(lambda: not mc.jog_busy)
            # the same number again: M6 would do nothing, so the app re-measures with M491
            n = len(emu.lines)
            await mc.change_tool(0)
            await _until(lambda: len(emu.lines) > n)
            assert emu.lines[n:] == ["M5", "M491"] and not mc.tool_ok()
            await _until(lambda: mc.routine["stage"] in ("done", "failed"), timeout=30)
            assert mc.routine["stage"] == "done" and mc.tool_ok()
        finally:
            await mc.disconnect(); await emu.stop()
    run(go())


def test_origin_anchor_and_makeras_auto_z_probe():
    async def go():
        emu, mc, _ = await _setup()
        try:
            mc.profile.anchor1 = (-191.28, -194.45)
            await mc.arm(True); await mc.home(); await _until(lambda: mc.homed)
            _measured(mc, emu)
            with pytest.raises(ml.Refused, match="probe \\(T0\\)"):
                await mc.auto_z_probe(50, 50)                    # T6 fitted
            await mc.change_tool(0)
            await _until(lambda: mc.routine["stage"] == "awaiting", timeout=15)
            await mc.confirm_tool_change()
            await _until(lambda: mc.routine["stage"] in ("done", "failed"), timeout=30)
            await mc.set_origin_anchor()
            await _until(lambda: emu.lines[-1].startswith("G10"))
            assert emu.lines[-1] == "G10 L2 P0 X-191.280 Y-194.450" and emu.wco[:2] == [-191.28, -194.45]
            n = len(emu.lines)
            await asyncio.sleep(0.4)                             # the new work offset reaches the status
            await mc.auto_z_probe(50, 50)                        # the stock centre, in work coordinates
            await _until(lambda: len(emu.lines) > n)
            assert emu.lines[n:] == ["M495 X50.000 Y50.000 O0 F0"]
            await _until(lambda: mc.routine["stage"] in ("done", "failed"), timeout=30)
            assert mc.routine["stage"] == "done", mc.routine
            assert abs(emu.mpos[0] + 141.28) < 1e-6 and abs(emu.mpos[1] + 144.45) < 1e-6
            assert abs(emu.wco[2] + 55.0) < 1e-6                 # work Z zero = the stock top
            await asyncio.sleep(0.4)
            assert abs(mc.link.status["wpos"][2] - 1.0) < 1e-3  # lifted 1 mm above it
            assert "surface touched" in mc.routine["msg"] and "1.000 above it" in mc.routine["msg"]
        finally:
            await mc.disconnect(); await emu.stop()
    run(go())



def test_origin_record_set_here_and_the_console():
    """What set the work origin is remembered (for "this setup wants / the machine has"); G10 L20 sets it at the head;
    the console sends one line as typed but never the blocked commands, settings or tool changes."""
    async def go():
        emu, mc, _ = await _setup()
        try:
            mc.profile.anchor1 = (-191.28, -194.45)
            assert mc.payload()["origin"].get("xy") is None          # unknown until this session sets it
            with pytest.raises(ml.Refused, match="Control is off"):
                await mc.set_origin(0, 0, None)
            await mc.arm(True); await mc.home(); await _until(lambda: mc.homed)
            with pytest.raises(ml.Refused, match="measured"):
                await mc.set_origin(None, None, 0)                   # Z0 depends on the tool length
            await mc.set_origin(0, 0, None)
            assert emu.lines[-1] == "G10 L20 P0 X0.000 Y0.000"
            o = mc.payload()["origin"]
            assert o["xy"]["ref"] == "typed" and "z" not in o and abs(o["wco"][0] - emu.mpos[0]) < 1e-6
            await mc.set_origin_anchor()
            o = mc.payload()["origin"]
            assert o["xy"]["ref"] == "anchor1" and not o["xy"].get("changed")
            emu.wco[0] += 5.0                                        # something else moves the offsets
            await asyncio.sleep(0.4)
            assert mc.payload()["origin"]["xy"]["changed"]
            await asyncio.sleep(0.3)
            w0 = mc._wco()
            await mc.shift_origin(101.42, 100.32, "back-right", "front-left corner, shifted by the block size")
            assert emu.lines[-1] == f"G10 L2 P0 X{w0[0] + 101.42:.3f} Y{w0[1] + 100.32:.3f}"
            o = mc.payload()["origin"]
            assert o["xy"]["ref"] == "back-right" and not o["xy"].get("changed")
            with pytest.raises(ml.Refused, match="outside the travel"):
                await mc.shift_origin(-400, 0, "x", "x")
            for bad, why in (("M490", "never sent"), ("M498.2", "never sent"), ("$H", "settings"), ("$100=80", "settings"),
                             ("$RST=*", "settings"), ("M6 T3", "M6"), ("M491", "tool changes"), ("G0 X1\nG0 Y1", "one line"), ("", "one line")):
                with pytest.raises(ml.Refused, match=why if why != "M6" else "tool changes|never|own steps"):
                    await mc.console(bad)
            n = len(emu.lines)
            await mc.console("g10 l20 p0 x0")
            await _until(lambda: len(emu.lines) > n)
            assert emu.lines[n] == "G10 L20 P0 X0" and mc.payload()["origin"]["xy"]["ref"] == "typed"
            await mc.console("$#")                                   # a read-only query is fine
        finally:
            await mc.disconnect(); await emu.stop()
    run(go())


def test_goto_raised_lifts_z_first_and_checks_before_moving():
    async def go():
        emu, mc, _ = await _setup()
        try:
            await mc.arm(True); await mc.home(); await _until(lambda: mc.homed)
            _measured(mc, emu)
            emu.mpos[2] = -12.0                                   # e.g. where a tool change leaves the head
            await asyncio.sleep(0.4)
            with pytest.raises(ml.Refused, match="Z at the top"):
                await mc.goto_xy(-100, -100, 300)                 # the plain go-to refuses
            n = len(emu.lines)
            with pytest.raises(ml.Refused, match="outside"):
                await mc.goto_xy_raised(-500, -100, 300)          # out of travel: refused before Z moves
            assert len(emu.lines) == n
            await mc.goto_xy_raised(-100, -100, 600)
            await _until(lambda: not mc.jog_busy, timeout=20)
            assert emu.lines[n].startswith("G53 G1 Z") and emu.lines[n + 1] == "G53 G1 X-100.000 Y-100.000 F600"
            assert abs(emu.mpos[0] + 100) < 1e-6 and emu.mpos[2] > -2
        finally:
            await mc.disconnect(); await emu.stop()
    run(go())


async def _at_top_over(mc, emu, x, y):
    await mc.goto_xy(x, y, 600)
    await _until(lambda: not mc.jog_busy, timeout=10)


def test_3d_probe_outside_corner_with_makeras_m480():
    async def go():
        emu, mc, _ = await _setup()
        try:
            mc.profile.anchor1 = (-191.28, -194.45)
            await mc.arm(True); await mc.home(); await _until(lambda: mc.homed)
            _measured(mc, emu)
            with pytest.raises(ml.Refused, match="3D probe \\(T9999\\)"):
                mc.check_probe_3d("outside_back_right")              # T6 fitted
            await mc.change_tool(9999)                               # the 3D probe is measured like any tool
            await _until(lambda: mc.routine["stage"] == "awaiting", timeout=15)
            assert "3D probe" in mc.routine["msg"]
            await mc.confirm_tool_change()
            await _until(lambda: mc.routine["stage"] in ("done", "failed"), timeout=30)
            assert mc.routine["stage"] == "done" and emu.tool == 9999 and mc.tool_ok()
            emu.wco[2] = -55.0                                       # work Z0 on the stock top (an earlier M495)
            await mc.raise_z_top(); await _until(lambda: not mc.jog_busy, timeout=10)   # the change ends at Z -3
            await _at_top_over(mc, emu, -96.28, -99.45)              # 5 mm in from the back-right corner
            with pytest.raises(ml.Refused, match="unknown 3D probe"):
                mc.check_probe_3d("outside_somewhere")
            with pytest.raises(ml.Refused, match="2 .. 80"):
                mc.check_probe_3d("outside_back_right", x=0)
            emu.wco[2] = -10.0                                       # pretend work Z0 is just below the head
            await asyncio.sleep(0.3)
            with pytest.raises(ml.Refused, match="only lowers"):
                await mc.lower_to(20)
            emu.wco[2] = -55.0
            await asyncio.sleep(0.3)
            await mc.lower_to(5)
            await _until(lambda: not mc.jog_busy, timeout=10)
            await asyncio.sleep(0.3)
            assert abs(emu.mpos[2] + 50.0) < 1e-3
            n = len(emu.lines)
            await mc.probe_3d("outside_back_right", 20, 20, 2, 2)
            await _until(lambda: len(emu.lines) > n)
            assert emu.lines[n:] == ["M480.2 X20 Y20 Z2 D2"]
            await _until(lambda: mc.routine["stage"] in ("done", "failed"), timeout=30)
            assert mc.routine["stage"] == "done", mc.routine
            assert abs(emu.wco[0] + 91.28) < 0.02 and abs(emu.wco[1] + 94.45) < 0.02 and abs(emu.wco[2] + 55.0) < 1e-6
            assert abs(mc.routine["origin"][0] + 91.28) < 0.02 and "+100.0" in mc.routine["msg"]
            # remembered for Find the block, and the origin record says what set it
            p = mc.payload()
            assert p["probes"][-1]["ref"] == "back-right" and abs(p["probes"][-1]["wco"][0] + 91.28) < 0.02
            assert p["origin"]["xy"]["ref"] == "back-right" and p["origin"]["z"]["ref"] == "top"
            # X0 Y0 at the front-left from faces (machine X / Y): nothing moves, the record follows
            mp = list(emu.mpos)
            await mc.origin_at(-191.28, -194.45, "front-left", "front-left corner from 3 probed corners")
            assert emu.lines[-1] == "G10 L2 P0 X-191.280 Y-194.450" and emu.mpos == mp
            assert mc.payload()["origin"]["xy"]["ref"] == "front-left"
            with pytest.raises(ml.Refused, match="outside the travel"):
                await mc.origin_at(-400, 0, "x", "x")
            assert not emu.crashes
        finally:
            await mc.disconnect(); await emu.stop()
    run(go())


def test_3d_probe_centres_inside_corner_and_travel_check():
    async def go():
        emu, mc, _ = await _setup()
        try:
            await mc.arm(True); await mc.home(); await _until(lambda: mc.homed)
            emu.tool = 9999
            await asyncio.sleep(0.3)
            _measured(mc, emu)
            emu.wco[2] = -55.0
            emu.pocket = (-150.0, -130.0, -150.0, -135.0, -60.0)     # 20 x 15 pocket, 5 deep, in the stock
            # boss centre on the 100 mm stock would step out past the travel (and into the L-bracket): refused
            await _at_top_over(mc, emu, -141.28, -144.45)
            with pytest.raises(ml.Refused, match="outside"):
                mc.check_probe_3d("boss_center", 60, 60)
            # pocket centre: start inside the pocket, below its edge (work Z0 on the pocket floor, 2 mm above it)
            await _at_top_over(mc, emu, -139.0, -143.0)
            emu.wco[2] = -60.0
            await asyncio.sleep(0.3)
            await mc.lower_to(2); await _until(lambda: not mc.jog_busy, timeout=10)
            await asyncio.sleep(0.3)
            await mc.probe_3d("pocket_center", 15, 12)
            await _until(lambda: mc.routine["stage"] in ("done", "failed"), timeout=30)
            assert mc.routine["stage"] == "done", mc.routine
            assert abs(emu.wco[0] + 140.0) < 0.02 and abs(emu.wco[1] + 142.5) < 0.02
            assert abs(mc.routine["size"]["x"] - 20.0) < 0.03 and abs(mc.routine["size"]["y"] - 15.0) < 0.03
            await mc.raise_z_top(); await _until(lambda: not mc.jog_busy, timeout=10)
            # inside corner (the pocket's back-left): start over the top, just outside the corner
            await _at_top_over(mc, emu, -155.0, -130.0)
            emu.wco[2] = -55.0
            await asyncio.sleep(0.3)
            await mc.lower_to(5); await _until(lambda: not mc.jog_busy, timeout=10)
            await asyncio.sleep(0.3)
            await mc.probe_3d("inside_back_left", 10, 10, 2, 2)
            await _until(lambda: mc.routine["stage"] in ("done", "failed"), timeout=30)
            assert mc.routine["stage"] == "done", mc.routine
            assert abs(emu.wco[0] + 150.0) < 0.02 and abs(emu.wco[1] + 135.0) < 0.02
            await mc.raise_z_top(); await _until(lambda: not mc.jog_busy, timeout=10)
            # boss centre on a small block
            emu.pocket = None
            emu.stock = (-120.0, -80.0, -120.0, -90.0)
            emu.surface_z = lambda x, y: -55.0 if (-120.0 <= x <= -80.0 and -120.0 <= y <= -90.0) else -75.0
            await _at_top_over(mc, emu, -101.0, -104.0)
            await mc.lower_to(5); await _until(lambda: not mc.jog_busy, timeout=10)
            await asyncio.sleep(0.3)
            await mc.probe_3d("boss_center", 30, 25)
            await _until(lambda: mc.routine["stage"] in ("done", "failed"), timeout=40)
            assert mc.routine["stage"] == "done", mc.routine
            assert abs(emu.wco[0] + 100.0) < 0.02 and abs(emu.wco[1] + 105.0) < 0.02
            assert abs(mc.routine["size"]["x"] - 40.0) < 0.03 and abs(mc.routine["size"]["y"] - 30.0) < 0.03
            assert not emu.crashes
        finally:
            await mc.disconnect(); await emu.stop()
    run(go())


def test_3d_probe_that_never_triggers_is_held():
    async def go():
        emu, mc, _ = await _setup()
        try:
            await mc.arm(True); await mc.home(); await _until(lambda: mc.homed)
            emu.tool = 9999
            await asyncio.sleep(0.3)
            _measured(mc, emu)
            emu.wco[2] = -55.0
            await _at_top_over(mc, emu, -96.28, -99.45)
            await mc.lower_to(5); await _until(lambda: not mc.jog_busy, timeout=10)
            await asyncio.sleep(0.3)
            emu.probe3d_fail, emu.speed = True, 1.0                  # real speed: 100 mm/min down, never touching
            await mc.probe_3d("outside_back_right", 20, 20, 2, 2, max_drop=3)
            await _until(lambda: mc.routine["stage"] in ("done", "failed"), timeout=10)
            assert mc.routine["stage"] == "failed" and "nothing touched" in mc.routine["msg"], mc.routine
            await _until(lambda: b"!" in emu.realtime and emu.state == "Hold", timeout=3)   # the emulator reads the byte a moment later
            assert emu.mpos[2] > -50.0 - 6.0                         # stopped a few mm down (3 + polling lag), not 108
        finally:
            await mc.disconnect(); await emu.stop()
    run(go())


PROG = """; AgenticCAD makera post: test
; tool T6 flat
G21 G90 G17
; Top: zero G54 at stock-top-left
G54
; Face [face] T6 flat D3.175
M6 T6
M3 S10000
G0 Z5
G0 X10 Y10
G1 Z-0.5 F300
G1 X40 F1200
G0 Z5
; Outline [contour] T3 flat D3.175
M6 T3
M3 S12000
G0 X5 Y5
G1 Z-1 F300
G1 X45 Y5 F900
G1 X45 Y45
G0 Z5
M5
G53 G0 Z-1
"""


PROG1 = PROG[:PROG.index("; Outline [contour]")] + "M5\nG53 G0 Z-1\n"      # Face only: one tool (T6)


def test_prepare_program_strips_maps_ops_and_refuses_unsafe():
    prep = ml.prepare_program(PROG, ["Face", "Outline"])
    lines = prep["text"].splitlines()
    assert not any(l.startswith(";") for l in lines) and all(len(l) <= ml.MAX_LINE for l in lines)
    assert [o["name"] for o in prep["ops"]] == ["Face", "Outline"] and prep["tools"] == [6, 3]
    f, o = prep["ops"]
    assert lines[f["start"] - 1] == "M6 T6" and lines[o["start"] - 1] == "M6 T3" and f["end"] == o["start"] - 1 and o["end"] == len(lines)
    assert prep["extents"]["x"] == [5.0, 45.0] and prep["extents"]["z"] == [-1.0, 5.0]
    air = ml.prepare_program(PROG, ["Face", "Outline"], air_cut=10)
    assert air["extents"]["z"] == [9.0, 15.0] and "G53 G0 Z-1" in air["text"]          # machine-coordinate moves untouched
    assert "G28" in ml.prepare_program("; Face [face]\nM6 T6\nG28\n", ["Face"])["text"]      # the Z1's park move
    for bad, why in (("G91 G0 X1", "relative"), ("G38.2 Z-5", "probing"), ("G10 L20 P0 Z0", "offset"), ("M490", "never sent"), ("G1 X" + "1" * 70, "characters")):
        with pytest.raises(ml.Refused, match=why):
            ml.prepare_program("; Face [face]\nM6 T6\n" + bad + "\n", ["Face"])
    with pytest.raises(ml.Refused, match="found 1 of 2"):
        ml.prepare_program("; Face [face]\nM6 T6\n", ["Face", "Gone"])
    assert ml.program_filename("obsidian logo - ops 2..5") == "obsidian_logo_ops_2..5.nc"


def test_run_program_one_tool_no_tool_change_in_the_file():
    """A program never changes tools on the machine: on 2026-10-18 the Z1 read ahead past M6 T2 and started the spindle
    (M3) while it was still changing and measuring the tool. One tool per run, fitted and measured beforehand; its M6
    line goes to the machine as M5, so the file can't change tools and the line numbers still map to the operations."""
    async def go():
        emu, mc, _ = await _setup(speed=60.0)
        try:
            await mc.arm(True); await mc.home(); await _until(lambda: mc.homed)
            _measured(mc, emu)                                       # T6 fitted and measured
            emu.wco = [-150.0, -150.0, -60.0]
            await asyncio.sleep(0.4)
            both = ml.prepare_program(PROG, ["Face", "Outline"])
            with pytest.raises(ml.Refused, match="T6 and T3: run one tool at a time"):
                mc.check_job(both)
            prep = ml.prepare_program(PROG1, ["Face"])
            far = ml.prepare_program(PROG1.replace("X40", "X160"), ["Face"])
            with pytest.raises(ml.Refused, match="outside"):
                mc.check_job(far)                                    # X 160 from a work origin at -150 runs past the top end
            other = ml.prepare_program(PROG1.replace("M6 T6", "M6 T4"), ["Face"])
            with pytest.raises(ml.Refused, match="fit and measure T4 first"):
                mc.check_job(other)                                  # the machine has T6
            await mc.run_program("test job", prep)
            await _until(lambda: mc.job["stage"] in ("running", "done", "failed"), timeout=20)
            sent = emu.sd["/sd/gcodes/test_job.nc"].decode()
            assert "M6" not in sent and sent.splitlines()[2] == "M5" and len(sent.splitlines()) == prep["lines"]
            assert sent.index("M5") < sent.index("M3 S10000")         # spindle off where the change was, on after it
            assert "play /sd/gcodes/test_job.nc" in emu.raw_lines            # sent without a newline
            await _until(lambda: mc.job["stage"] in ("done", "failed"), timeout=60)
            assert mc.job["stage"] == "done", mc.job
            assert emu.tool == 6 and not emu.crashes                 # no tool change happened
        finally:
            await mc.disconnect(); await emu.stop()
    run(go())


def test_program_pause_resume_and_abort():
    async def go():
        emu, mc, _ = await _setup(speed=2.0)
        try:
            await mc.arm(True); await mc.home(); await _until(lambda: mc.homed)
            _measured(mc, emu)
            emu.wco = [-150.0, -150.0, -60.0]
            await asyncio.sleep(0.4)
            prep = ml.prepare_program(PROG1, ["Face"])
            await mc.run_program("pause test", prep)
            await _until(lambda: mc.job["stage"] == "running", timeout=20)
            await mc.job_pause()
            await _until(lambda: mc.job["stage"] == "paused", timeout=10)
            with pytest.raises(ml.Refused, match="Idle"):
                await mc.jog("x", 1)                                   # nothing else moves the machine meanwhile
            await mc.job_resume()
            await _until(lambda: mc.job["stage"] == "running", timeout=10)
            await mc.job_abort()
            await _until(lambda: "abort" in emu.raw_lines)
            assert mc.job["stage"] == "aborted"
            await asyncio.sleep(1.0)
            assert mc.job["stage"] == "aborted" and emu.play is None
        finally:
            await mc.disconnect(); await emu.stop()
    run(go())


def test_spindle_fan_switch_and_states_read_back_from_diagnose(monkeypatch):
    """Makera's spindle fan (M811 S<%> / M812, as MakeraStudio sends it), and every switch's real state read back from
    `diagnose` while Idle - the firmware runs the fan by itself when the spindle is hot."""
    monkeypatch.setattr(ml, "DIAG_EVERY_S", 0.3)
    async def go():
        emu, mc, _ = await _setup()
        try:
            mc._diag_t = 0.0                                       # this test wants the background reads
            await mc.switch("spindle_fan", True, 40)               # no Control needed: nothing moves
            await _until(lambda: emu.lines and emu.lines[-1] == "M811S40")
            p = mc.payload()
            assert p["switches"]["spindle_fan"] and p["switch_power"]["spindle_fan"] == 40
            await mc.switch("spindle_fan", False)
            await _until(lambda: emu.lines[-1] == "M812")
            await mc.switch("spindle_fan", True)                    # back on at the last speed
            await _until(lambda: emu.lines[-1] == "M811S40")
            with pytest.raises(ml.Refused, match="0 .. 100"):
                await mc.switch("spindle_fan", True, 150)
            with pytest.raises(ml.Refused, match="unknown switch"):
                await mc.switch("coolant", True)
            # the machine changes it on its own: the next diagnose read shows it
            emu.switches.update(spindle_fan=True, light=False); emu.fan_power = 80
            await _until(lambda: mc.switch_power.get("spindle_fan") == 80 and mc.switch_state.get("light") is False, timeout=5)
            assert "diagnose" in emu.lines and not any("diagnose" in t or t.startswith("{S:") for t in mc.payload()["log"])
        finally:
            await mc.disconnect(); await emu.stop()
    run(go())


def test_a_long_upload_does_not_switch_control_off():
    """The real Z1 (2026-10-18) answers no status while a file comes in: a 45-packet upload took a few seconds, the
    watchdog called the link stale, switched Control off, and the uploaded program was refused at play. During an
    upload the missing status is expected; after it the app waits for the machine to answer again, then plays."""
    async def go():
        emu, mc, _ = await _setup(speed=60.0)
        try:
            await mc.arm(True); await mc.home(); await _until(lambda: mc.homed)
            _measured(mc, emu)
            emu.wco = [-150.0, -150.0, -60.0]
            await asyncio.sleep(0.4)
            body = "\n".join(f"G1 X{10 + (i % 20)} Y{10 + (i % 7)} F600" for i in range(9000))
            big = PROG1.replace("G1 X40 F1200", "G1 X40 F1200\n" + body)
            prep = ml.prepare_program(big, ["Face"])
            emu.quiet_upload, emu.upload_packet_delay = True, 0.25
            assert len(prep["text"].encode()) / ml.PACKET * emu.upload_packet_delay > ml.STALE_S + 0.5   # longer than "stale"
            await mc.run_program("long upload", prep)
            await _until(lambda: mc.job["stage"] in ("running", "awaiting", "done", "failed"), timeout=30)
            assert mc.job["stage"] != "failed", mc.job.get("msg")
            assert mc.armed and "play /sd/gcodes/long_upload.nc" in emu.raw_lines
            assert not any("Control off" in e for e in mc.events)
            await mc.job_abort()
        finally:
            await mc.disconnect(); await emu.stop()
    run(go())


def test_origin_record_is_right_when_the_machine_reports_new_offsets_late():
    """The real Z1 showed the old offsets for a moment after G10: the record caught those and then called the origin
    "changed since it was set here". It now records what was commanded and waits for the machine to show it."""
    async def go():
        emu, mc, _ = await _setup()
        try:
            emu.offset_lag = 1.0
            await mc.arm(True); await mc.home(); await _until(lambda: mc.homed)
            _measured(mc, emu)
            await mc.origin_at(-191.222, -194.211, "front-left", "front-left corner from 3 probed corners")
            o = mc.payload()["origin"]["xy"]
            assert o["wco"] == [-191.222, -194.211] and not o.get("changed")
            await asyncio.sleep(1.2)
            assert not mc.payload()["origin"]["xy"].get("changed")
            await mc.set_origin(0, 0, None)                         # the head's position becomes X0 Y0
            await asyncio.sleep(1.2)
            o = mc.payload()["origin"]["xy"]
            assert o["ref"] == "typed" and not o.get("changed") and abs(o["wco"][0] - emu.mpos[0]) < 1e-3
        finally:
            await mc.disconnect(); await emu.stop()
    run(go())
