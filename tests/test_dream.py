"""Dreaming: the background pass that consolidates the lessons learnt while the app is idle - conservative (nothing
deleted, new lessons cite evidence, a capped number of changes), snapshotted and undoable."""
from __future__ import annotations

import json
import time

import pytest

import dream
import lessons as L
import run_recorder


def _failed_run(ws, msg="the machine alarmed: manual stop", program="latch"):
    rec = run_recorder.RunRecorder(ws / "runs", {"program": program, "owner": {"name": "latch"}}, {"name": "l.nc", "ops": [{"name": "B8 tabbed"}]}, "G0 X0\n")
    rec.close("failed", msg)
    return f"{rec.dir.parent.name}/{rec.dir.name}"


def test_it_runs_once_after_each_run_when_the_app_has_gone_quiet(tmp_path):
    L.load(tmp_path)
    _failed_run(tmp_path, program="old")                                        # before the feature: not reprocessed
    assert dream.pending_runs(tmp_path) == [] and dream.due(tmp_path, idle_for=3600)[0] == []
    ref = _failed_run(tmp_path)
    runs, why = dream.due(tmp_path, idle_for=60)
    assert runs == [] and "waiting for the app to go quiet" in why                # the user may still be talking about it
    runs, why = dream.due(tmp_path, idle_for=dream.IDLE_S + 1)
    assert [m["id"] for m in runs] == [ref] and "after the run latch" in why


async def test_a_pass_merges_sharpens_retires_and_adds_only_with_evidence(tmp_path):
    L.load(tmp_path)
    dream.pending_runs(tmp_path)
    ref = _failed_run(tmp_path)
    runs, _ = dream.due(tmp_path, idle_for=dream.IDLE_S + 1)
    chat = [{"role": "user", "text": "the tabs broke and the part came loose", "t": "18:29"}]
    seen = {}

    async def llm(system, prompt):
        seen["prompt"] = prompt
        return "Here you go:\n" + json.dumps({"summary": "merged the two LEFT lessons", "changes": [
            {"op": "merge", "ids": ["L3", "L5"], "title": "Machine every feature the model has", "rule": "Chamfers and levels.",
             "tags": ["cam", "chamfer"], "check": "LEFT", "reason": "same failure"},
            {"op": "rewrite", "id": "L9", "rule": "A refused move is final: explain it.", "reason": "shorter"},
            {"op": "retire", "id": "L10"},                                          # no reason: skipped
            {"op": "add", "title": "Watch the first release cut", "rule": "Stay at the machine for release cuts.",
             "why": "a part came loose", "evidence": [ref, "C1"], "tags": ["cam", "tabs"]},
            {"op": "add", "title": "Invented", "rule": "x", "why": "y", "evidence": ["made/up"], "tags": ["cam"]},
        ]})
    e = await dream.run(tmp_path, llm, "after the run", runs, chat)
    assert ref in seen["prompt"] and "LESSONS:" in seen["prompt"] and "the tabs broke" in seen["prompt"]
    assert e["runs"] == [ref] and dream.pending_runs(tmp_path) == []             # once per run
    rows = {r["id"]: r for r in L.load(tmp_path)}
    merged = next(r for r in rows.values() if r["title"] == "Machine every feature the model has")
    assert merged["source"] == "dream" and "L3:" in merged["why"] and "L5:" in merged["why"] and merged["check"] == "LEFT"
    assert rows["L3"]["status"] == rows["L5"]["status"] == "retired" and f"merged into {merged['id']}" in rows["L3"]["why"]
    assert rows["L9"]["rule"] == "A refused move is final: explain it." and rows["L10"]["status"] == "active"
    assert any(r["title"] == "Watch the first release cut" and ref in r["why"] for r in rows.values())
    assert not any(r["title"] == "Invented" for r in rows.values())
    skipped = [c for c in e["changes"] if c.get("skipped")]
    assert len(skipped) == 2 and e["summary"] == "merged the two LEFT lessons"
    assert dream.log(tmp_path)[0]["stamp"] == e["stamp"]
    dream.undo(tmp_path)                                                         # back to before the pass
    after = L.load(tmp_path)
    assert len(after) == len(L.SEED) and all(r["status"] == "active" for r in after)
    with pytest.raises(ValueError, match="already undone"):
        dream.undo(tmp_path)


async def test_an_unreadable_answer_changes_nothing(tmp_path):
    before = L.load(tmp_path)

    async def llm(system, prompt):
        return "I couldn't decide."
    e = await dream.run(tmp_path, llm)
    assert e["error"] and e["changes"] == [] and L.load(tmp_path) == before
    assert not (tmp_path / "dreams" / "lock").exists()
