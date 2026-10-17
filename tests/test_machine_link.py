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
    return emu, mc, cam


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
            await mc.arm(True); await mc.home(); await _until(lambda: mc.homed)
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
            with pytest.raises(ml.Refused):
                await mc.arm(True)                         # can't arm in Alarm
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
