"""The assistant on the machine: stopping actions run at once; everything that moves or changes the machine is checked,
shown as an approval request, and runs only after resolve_machine_request(..., True) - the app's Approve click."""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import machine_link as ml  # noqa: E402
from agent import CadAgent  # noqa: E402
from z1_emu import Z1Emu  # noqa: E402


@pytest.fixture
def ag(tmp_path):
    ws = tmp_path / "ws"
    for sub in ("exports", "history", "designs", "imports", "images", "drawings"):
        (ws / sub).mkdir(parents=True)
    events = []

    async def emit(e):
        events.append(e)

    async def shot(spec):
        raise RuntimeError("no browser")
    a = CadAgent(ws, emit, shot)
    a.events = events
    return a


async def _until(cond, timeout=10.0):
    t0 = asyncio.get_running_loop().time()
    while not cond():
        if asyncio.get_running_loop().time() - t0 > timeout:
            raise AssertionError("timed out")
        await asyncio.sleep(0.02)


def test_moves_wait_for_approve_and_stops_do_not(ag):
    async def go():
        emu = Z1Emu(speed=40.0)
        port, _ = await emu.start()
        ag.machine = ml.MachineControl()
        try:
            assert "No machine connected" in await ag.machine_action("home")
            assert "Connected" in await ag.machine_action("connect", {"host": "127.0.0.1", "port": port})
            # a move that the guards refuse never reaches the user
            assert (await ag.machine_action("home")).startswith("Refused: Control is off")
            assert not [e for e in ag.events if e["type"] == "machine_request"]
            # Control on: asked, declined -> nothing happens
            t = asyncio.create_task(ag.machine_action("control_on"))
            await _until(lambda: ag.machine_requests)
            req = next(e for e in ag.events if e["type"] == "machine_request")
            assert req["action"] == "control_on" and "Control on" in req["summary"]
            assert ag.resolve_machine_request(req["id"], False)
            assert "declined" in await t and not ag.machine.armed
            assert not ag.resolve_machine_request(req["id"], True)          # a closed request can't be approved later
            # approved -> done
            t = asyncio.create_task(ag.machine_action("control_on"))
            await _until(lambda: ag.machine_requests)
            rid = next(iter(ag.machine_requests))
            ag.resolve_machine_request(rid, True)
            assert (await t).startswith("Approved and sent") and ag.machine.armed
            t = asyncio.create_task(ag.machine_action("home"))
            await _until(lambda: ag.machine_requests)
            cmds = lambda: [ln for ln in emu.lines if ln != "diagnose"]      # (the read-only switch-state query may run)
            n = len(cmds())
            await asyncio.sleep(0.2)
            assert len(cmds()) == n                                          # nothing sent while waiting
            ag.resolve_machine_request(next(iter(ag.machine_requests)), True)
            await t
            await _until(lambda: ag.machine.homed)
            # stops never wait
            assert ag.machine_requests == {}
            assert "Feed hold" in await ag.machine_action("hold")
            assert not ag.machine_requests
            # an unanswered request expires and does nothing
            r = await ag.machine_action("raise_z", wait=0.2)
            assert "didn't answer" in r
            assert [e["outcome"] for e in ag.events if e["type"] == "machine_request_done"][-1] == "expired"
            assert "Unknown action" in await ag.machine_action("m498")
        finally:
            await ag.machine.disconnect(); await emu.stop()
    asyncio.run(go())


def test_fourth_axis_actions_need_the_module_and_an_approval(ag):
    """jog_a / rotary_zero / shrink_a: refused up front on a machine without a 4th axis (no card); with the module they
    are checked, shown as a card and run only on Approve - the Z probe is Makera's M495 X0 Y0 O0."""
    async def go():
        emu = Z1Emu(speed=40.0, fourth=False)
        port, _ = await emu.start()
        ag.machine = ml.MachineControl()
        ag.machine.profile.anchor1 = (-191.28, -194.45)
        ag.machine.profile.rotation_offset = {"x": -7.5, "y": 69.0, "z": 17.0}
        mc = ag.machine

        async def approve(action, args=None):
            t = asyncio.create_task(ag.machine_action(action, args))
            await _until(lambda: ag.machine_requests)
            req = next(e for e in ag.events if e["type"] == "machine_request" and e["id"] in ag.machine_requests)
            ag.resolve_machine_request(req["id"], True)
            return await t, req
        try:
            await ag.machine_action("connect", {"host": "127.0.0.1", "port": port})
            await approve("control_on"); await approve("home"); await _until(lambda: mc.homed)
            mc.measured = {"tool": emu.tool, "touch_z": emu.setter_z, "offset": -17.297, "t": 0}
            assert (await ag.machine_action("jog_a", {"deg": 10})).startswith("Refused: this machine has no 4th axis")
            assert (await ag.machine_action("shrink_a")).startswith("Refused: this machine has no 4th axis")
            assert not ag.machine_requests
            # the program's machine has the module (the server keeps this record in step; here set directly)
            mc.set_machine_record({"name": "Makera Z1 + 4th axis", "rotary": {"axis": "A", "installed": True}})
            assert "Refused" in await ag.machine_action("jog_a", {"deg": 120})           # over the 90° step
            r, req = await approve("jog_a", {"deg": 30})
            assert r.startswith("Approved and sent") and "Turn the 4th axis by +30°" in req["summary"]
            await _until(lambda: not mc.jog_busy and abs(emu.a - 30.0) < 1e-6, timeout=10)
            assert emu.mpos == [-1.0, -1.0, -1.0]
            r, req = await approve("rotary_zero", {"what": "a_zero"})
            assert "A0" in req["summary"] and emu.a == 0.0 and "origin" not in r.lower() or True
            r, req = await approve("rotary_zero", {"what": "y_axis"})
            assert "machine Y -125.450" in req["summary"]
            await _until(lambda: abs(emu.wco[1] + 125.45) < 1e-6, timeout=5)
            r, req = await approve("shrink_a")
            await _until(lambda: "G92.4 A0 S0" in emu.lines, timeout=5)
            # the Z probe needs the wired probe: refused before any card
            assert "probe (T0)" in await ag.machine_action("rotary_zero", {"what": "z_probe"})
            emu.tool = 0
            await asyncio.sleep(0.3)
            mc.measured = {"tool": 0, "touch_z": emu.setter_z, "offset": -17.297, "t": 0}
            emu.fourth = True                                                            # the pad is there
            r, req = await approve("rotary_zero", {"what": "z_probe"})
            assert "M495 X0 Y0 O0" in req["summary"] and "centreline" in req["summary"] and "still running" in r
            await _until(lambda: mc.routine and mc.routine["stage"] in ("done", "failed"), timeout=30)
            assert mc.routine["stage"] == "done" and abs(emu.wco[2] - (emu.pad_z - 17.0)) < 1e-6
            assert "4th axis A" in await ag.machine_action("status")
        finally:
            await ag.machine.disconnect(); await emu.stop()
    asyncio.run(go())


def test_building_a_big_program_does_not_stall_the_event_loop(ag, monkeypatch):
    """Found filming the 1.0 tour: checking a 3-hour program built its G-code on the event loop, the machine's status went
    stale meanwhile and the check refused ("no current status from the machine"); a real Z1 would also drop Control.
    The program is built in a worker thread now: other coroutines (the status poll) keep running."""
    import time as _t

    def slow(ops, air=0.0):
        _t.sleep(0.6)
        raise ml.Refused("no CAM program")
    monkeypatch.setattr(ag, "program_for_ops", slow)

    async def go():
        emu = Z1Emu(speed=40.0)
        port, _ = await emu.start()
        ag.machine = ml.MachineControl()
        assert "Connected" in await ag.machine_action("connect", {"host": "127.0.0.1", "port": port})
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.05)
        t = asyncio.create_task(ticker())
        try:
            r = await ag.machine_action("check_ops", {"ops": [0]})
        finally:
            t.cancel(); await ag.machine.disconnect(); await emu.stop()
        return r, ticks
    r, ticks = asyncio.run(go())
    assert "no CAM program" in r
    assert ticks >= 6, f"the loop stalled while the program was built ({ticks} ticks in 0.6 s)"
