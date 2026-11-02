"""Lessons learnt (2026-11-02): rules written down after a failure, handed back by the tools that start the same kind of
work, and named when the simulation catches one broken."""
from __future__ import annotations

import json

import pytest

import lessons as L
from tests.test_agent_tools import CAM_CODE, ag, text  # noqa: F401  (fixture)


def test_seeded_and_kept_with_the_workspace(tmp_path):
    rows = L.load(tmp_path)
    assert len(rows) == len(L.SEED) and all(r["status"] == "active" and r["source"] == "seed" for r in rows)
    r = L.add(tmp_path, "Spot drill before tapping", "Spot every tapped hole first.", "2026-11-03: tap wandered", ["cam", "Makera Z1"], None)
    assert r["id"] == f"L{len(L.SEED) + 1}" and r["tags"] == ["cam", "makera-z1"]
    with pytest.raises(ValueError, match="update it instead"):
        L.add(tmp_path, "spot drill before tapping", "x", "y", ["cam"])
    with pytest.raises(ValueError, match="evidence"):
        L.add(tmp_path, "t", "r", "", ["cam"])
    with pytest.raises(ValueError, match="check"):
        L.add(tmp_path, "t", "r", "w", ["cam"], check="NOPE")
    L.update(tmp_path, r["id"], status="retired", why_add="the machine has a spot drill op now")
    assert next(x for x in L.load(tmp_path) if x["id"] == r["id"])["status"] == "retired"
    assert json.loads((tmp_path / "lessons.json").read_text())["lessons"][-1]["why"].endswith("spot drill op now")


def test_only_the_lessons_for_the_work_come_back(tmp_path):
    rows = L.load(tmp_path)
    plain = {x["title"] for x in L.relevant(rows, {"cam"})}
    assert "Simulate the ops you run, with their history, and fix every error" in plain
    assert "Tabs need 2 mm of real metal" not in plain and "Z0 only from a probe or a touch-off" not in plain
    tabbed = {x["title"] for x in L.relevant(rows, {"cam", "tabs"})}
    assert "Tabs need 2 mm of real metal" in tabbed and "Finish every part before it is released" in tabbed
    machine = {x["title"] for x in L.relevant(rows, {"machine", "probe"})}
    assert "Z0 only from a probe or a touch-off" in machine and "Tabs need 2 mm of real metal" not in machine


async def test_the_cam_tools_hand_the_lessons_over_and_name_a_broken_one(ag):
    T = ag.tool_handlers
    assert "LESSONS LEARNT" in text(await T["cam_context"]({}))
    r = text(await T["build_cam"]({"code": CAM_CODE}))
    assert "LESSONS for this program" in r and "Simulate the ops you run" in r and "Tabs need 2 mm" not in r   # no tabs here
    await T["simulate_cam"]({"ops": [1]})
    ag.sim.verdict = lambda: {"ok": False, "errors": [{"code": "THIN_TAB", "op": "x", "message": "m"}], "warnings": []}
    assert "BREAKS LESSON" in ag.broken_lessons(ag.sim) and "Tabs need 2 mm" in ag.broken_lessons(ag.sim)
    r = await T["lessons"]({"action": "add", "title": "Clamp the plate", "rule": "Use two clamps.", "why": "it moved", "tags": ["cam"]})
    assert "add: L" in text(r) and ag.rec.last("lessons")
    assert "Clamp the plate" in text(await T["lessons"]({"action": "list"}))
    assert (await T["lessons"]({"action": "add", "title": "x"})).get("is_error")


async def test_a_failed_run_asks_for_a_lesson_once(ag):
    import run_recorder
    root = ag.workspace / "runs"
    ag.design_name = "latch"
    rec = run_recorder.RunRecorder(root, {"program": "p", "owner": {"name": "latch"}}, {"name": "p.nc", "ops": []}, "G0 X0\n")
    rec.close("failed", "the machine alarmed: manual stop")
    notes = ag._run_lesson_notes()
    assert len(notes) == 1 and "ended failed" in notes[0] and "lessons tool" in notes[0]
    assert ag._run_lesson_notes() == []
