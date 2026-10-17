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
            n = len(emu.lines)
            await asyncio.sleep(0.2)
            assert len(emu.lines) == n                                       # nothing sent while waiting
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
