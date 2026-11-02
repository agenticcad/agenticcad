"""A conversation and the machine context survive a relaunch (2026-11-02: the app restarted mid-job and the assistant
started over without the zeroing plan, the probe results or the setup)."""
from __future__ import annotations

import json

import pytest

from agent import CadAgent
from tests.test_agent_ops import PLATE, Recorder
from tests.test_agent_tools import ag, text  # noqa: F401  (fixture)


class FakeMachine:
    connected = True

    def describe(self):
        return "Makera Z1 at 10.0.0.2: state Idle, tool T2; G54 Z0 by auto Z probe (wired probe)"

    def _status(self):
        return {}

    def origin_payload(self, st):
        return {"z": {"ref": "top", "how": "auto Z probe (wired probe)"}}


def _session_file(home, sid, lines):
    d = home / ".claude" / "projects" / "-tmp-ws"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")


async def test_the_session_is_kept_and_resumed_after_a_relaunch(ag, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "home" / ".claude"))
    ag.session_id = "abc"
    ag._write_state()
    assert json.loads((ag.doc_dir / "state.json").read_text())["session"] == "abc"
    b = CadAgent(ag.workspace, Recorder().emit, ag.screenshot_fn)
    b._load_initial()
    assert b.session_id == "abc"
    # its transcript is gone (another machine, cleared): the next start begins a fresh conversation
    assert not b._session_on_disk("abc")
    _session_file(tmp_path / "home", "abc", [])
    assert b._session_on_disk("abc")


async def test_the_resumed_conversation_is_shown_without_the_apps_preamble(ag, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "home" / ".claude"))
    ag.session_id = "s1"
    _session_file(tmp_path / "home", "s1", [
        {"type": "user", "message": {"content": "[Library parts available: X]\n[Note] the user did y\nprobe the plate"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Probing Z now."}, {"type": "tool_use", "name": "t"}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
        {"type": "user", "message": {"content": "[Image: source: x.jpg]"}},
        {"type": "assistant", "isSidechain": True, "message": {"content": [{"type": "text", "text": "side"}]}},
    ])
    assert ag.chat_history() == [{"role": "user", "text": "probe the plate"}, {"role": "assistant", "text": "Probing Z now."}]


async def test_a_new_conversation_starts_from_the_machine_journal(ag):
    ag.machine = FakeMachine()
    ag._journal("auto_z_probe", {}, "done", "Z0 on the plate top")
    ag._journal("status", {}, "done")                                   # reads aren't journalled
    ag._journal("lower_to", {"height": -3}, "refused", "height must be 2 .. 50 mm above work zero")
    rows = [json.loads(x) for x in (ag.doc_dir / "machine_journal.jsonl").read_text().splitlines()]
    assert [r["action"] for r in rows] == ["auto_z_probe", "lower_to"]
    ctx = ag.machine_context(new_session=True)
    assert "[Machine] Makera Z1" in ctx and "Z0 by auto Z probe" in ctx
    assert "auto_z_probe" in ctx and "lower_to" in ctx and "refused: height must be" in ctx
    resumed = ag.machine_context(new_session=False)                     # a resumed conversation has its own history
    assert "auto_z_probe" not in resumed and "[Machine] Makera Z1" in resumed


async def test_machine_status_with_a_camera_returns_the_picture(ag):
    class Cam:
        async def snapshot(self):
            return b"\xff\xd8jpeg"
    ag.machine = FakeMachine(); ag.camera = Cam()
    r = await ag.tool_handlers["machine_status"]({"camera": True})
    assert r["content"][0]["type"] == "image" and "camera unavailable" not in text(r)


async def test_a_run_waits_for_a_simulation_and_shows_its_errors(ag):
    import machine_link as ml
    from tests.test_agent_tools import CAM_CODE

    class M(FakeMachine):
        jog_busy = job_active = False
        job = None

        def check_job(self, prep, rotary=False, ack=False):
            return {"notes": ["runs in G54"]}

        def _need(self, **kw):
            pass
    ag.machine = M()
    T = ag.tool_handlers
    assert "2 ops" in text(await T["build_cam"]({"code": CAM_CODE}))
    with pytest.raises(ml.Refused, match="simulate these ops first"):
        ag._machine_plan("run_ops", {"ops": [0]})
    await T["simulate_cam"]({"ops": [1]})
    summary, _ = ag._machine_plan("run_ops", {"ops": [0]})
    assert summary.startswith("Run on the machine")
    name = ag.program.ops[0].name
    ag.sim.verdict = lambda: {"ok": False, "errors": [{"code": "THIN_TAB", "op": name, "message": f"{name} tab 1: 0.90 mm of metal"},
                                                      {"code": "GOUGE", "ops": ["Other op"], "message": "not this run's"}], "warnings": []}
    summary, _ = ag._machine_plan("run_ops", {"ops": [0]})
    assert summary.startswith("SIMULATION ERRORS") and "0.90 mm of metal" in summary and "not this run's" not in summary
    with pytest.raises(ml.Refused, match="simulate these ops first"):    # op 2 was never simulated
        ag._machine_plan("run_ops", {"ops": [1]})
