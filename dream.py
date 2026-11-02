"""Dreaming: a background pass after each CNC run that draws the lessons out of that run and consolidates them with the
lessons learnt (lessons.py), once the app has gone quiet (so the user's reaction to the run is in).

Modelled on Letta's sleep-time agents and Claude Code's Auto Dream (a second pass between sessions that merges, sharpens
and retires memories) with the two risks the research on reflective memory names: a wrong merge or deletion is permanent,
and a wrong lesson keeps steering the agent. So:
  - it runs once per finished run (any outcome), after IDLE_S without a chat turn, a click or a machine job - never on a
    timer; the runs that existed before it first ran are not reprocessed;
  - the model sees the lessons and the evidence: the run (outcome, ops, measured findings and load events, suggestions,
    events), the machine journal's refusals and failures from the run's start, and the conversation from the run's start
    (what the user said about it) - and answers with structured changes - add, merge, rewrite, retire - each with a reason;
  - nothing is deleted: merged and stale lessons are retired with a pointer; a new lesson must cite evidence from its
    inputs (a run id, a journal entry or a chat message); at most MAX_CHANGES per pass;
  - lessons.json is snapshotted before every pass (dreams/<stamp>.lessons.json) and the pass is logged
    (dreams/<stamp>.json); undo() puts the snapshot back.
The model call is injected (`llm(system, prompt) -> str`), so the pass is testable without Claude.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import lessons as L

MAX_CHANGES = 10
IDLE_S = 10 * 60                 # after a run: wait this long without a chat turn, a click or a machine job

SYSTEM = """You keep the lessons learnt of AgenticCAD, a CAD/CAM app whose assistant drives a real CNC machine. Lessons are
rules the assistant must follow, written after things went wrong. A CNC run just finished. You get every lesson (with its
id, status, tags, check and evidence) and the evidence: the run (outcome, operations, measured findings and load events,
suggestions, events), the machine journal's refusals and failures from the run's start, and the conversation from the
run's start - the user's own words about what went wrong matter most. First find what this run teaches (mistakes in the
CAM program, the machine handling or the assistant's behaviour that cost time, quality or safety - and what worked
against an existing lesson), then fold it into the set without losing anything true:
- merge lessons that say the same thing (one rule, the union of their evidence, the union of their tags);
- rewrite a rule that is vague, too narrow or contradicted by newer evidence (keep it one actionable rule);
- retire a lesson only when newer evidence shows it wrong or no longer needed, and say which evidence;
- add a lesson only when the evidence shows a repeated or costly mistake no lesson covers, citing the evidence refs given
  (run ids like design/run, journal refs like J12, chat refs like C3); prefer sharpening or extending an existing lesson
  (rewrite, adding the new evidence) over a near-duplicate;
- keep tags to: cam, machine, probe, tabs, chamfer, flip, a machine slug (makera-z1), a material (aluminium);
- check is one of GOUGE, THIN_TAB, RELEASED, LEFT, BITE, COLLISION, RAPID, FREED (the simulation code that catches it) or null.
Make at most %d changes; no change at all is a fine answer. Answer with JSON only:
{"summary": "<one or two sentences>", "changes": [
 {"op": "merge", "ids": ["L1", "L4"], "title": "...", "rule": "...", "tags": [...], "check": null, "reason": "..."},
 {"op": "rewrite", "id": "L3", "title": "...", "rule": "...", "tags": [...], "check": null, "reason": "..."},
 {"op": "retire", "id": "L7", "reason": "..."},
 {"op": "add", "title": "...", "rule": "...", "why": "<the evidence in words>", "evidence": ["design/run", "J12", "C3"], "tags": [...], "check": null}
]}""" % MAX_CHANGES


def _dir(ws: Path) -> Path:
    d = Path(ws) / "dreams"
    d.mkdir(parents=True, exist_ok=True)
    return d


def state(ws: Path) -> dict[str, Any]:
    try:
        return json.loads((_dir(ws) / "state.json").read_text())
    except (OSError, ValueError):
        return {}


def _save_state(ws: Path, st: dict[str, Any]) -> None:
    (_dir(ws) / "state.json").write_text(json.dumps(st, indent=1))


def pending_runs(ws: Path) -> list[dict[str, Any]]:
    """Finished runs no pass has looked at yet (oldest first). The first call marks every run already there as seen."""
    import run_recorder
    st = state(ws)
    runs = [m for m in run_recorder.list_runs(Path(ws) / "runs") if m.get("outcome") not in (None, "running")]
    if "seen_runs" not in st:
        st["seen_runs"] = sorted(m["id"] for m in runs)
        _save_state(ws, st)
        return []
    seen = set(st["seen_runs"])
    return sorted((m for m in runs if m["id"] not in seen), key=lambda m: m.get("t0") or 0)


def evidence(ws: Path, since: float, run_ids: list[str] | None = None, chat: list[dict] | None = None) -> dict[str, Any]:
    """The runs (the given ones, else all since `since`), machine-journal refusals / failures and the conversation since
    `since`, with refs the model can cite."""
    import run_analysis
    import run_recorder
    runs = []
    for m in run_recorder.list_runs(Path(ws) / "runs"):
        if (run_ids is not None and m["id"] not in run_ids) or (run_ids is None and (m.get("t0") or 0) < since):
            continue
        f = []
        try:
            a = run_analysis.analyse(Path(ws) / "runs" / m["id"])
            f = [x["title"] + ": " + x.get("detail", "")[:200] for x in (a.get("findings") or [])][:10]
        except Exception:  # noqa: BLE001 - a run that can't be analysed still counts by its outcome
            pass
        sug = Path(ws) / "runs" / m["id"] / "suggestions.json"
        try:
            s = [x["title"] for x in json.loads(sug.read_text()).get("suggestions", [])][:5] if sug.exists() else []
        except (OSError, ValueError):
            s = []
        try:
            ev_rows = [json.loads(x) for x in (Path(ws) / "runs" / m["id"] / "events.jsonl").read_text().splitlines() if x.strip()]
            evs = [f"{e.get('t', 0) / 60:.1f} min: {e.get('kind')} {e.get('text', '')[:120]}" for e in ev_rows if e.get("kind") != "console"][:30]
        except (OSError, ValueError):
            evs = []
        runs.append({"ref": m["id"], "started": m.get("started"), "program": m.get("program"), "outcome": m.get("outcome"),
                     "message": m.get("message"), "duration_min": m.get("duration_min"), "est_min": m.get("est_min"),
                     "ops": [o.get("name") for o in (m.get("ops") or [])][:30], "findings": f, "suggestions": s, "events": evs})
    journal, n = [], 0
    for p in sorted(Path(ws).glob("**/machine_journal.jsonl")):
        try:
            rows = [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
        except (OSError, ValueError):
            continue
        for r in rows:
            if r.get("t", 0) >= since and r.get("outcome") not in ("done", "sent"):
                n += 1
                journal.append({"ref": f"J{n}", "t": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["t"])), "action": r.get("action"),
                                "args": r.get("args"), "outcome": r.get("outcome"), "text": (r.get("text") or "")[:240]})
    talk = [{"ref": f"C{i + 1}", **c} for i, c in enumerate(chat or [])][-60:]
    return {"runs": runs[:20], "journal": journal[-80:], "chat": talk}


def due(ws: Path, idle_for: float = 0.0) -> tuple[list[dict[str, Any]], str]:
    """The finished runs a pass should look at now (empty: not now, and why)."""
    runs = pending_runs(ws)
    if not runs:
        return [], "no new run since the last pass"
    if idle_for < IDLE_S:
        return [], f"waiting for the app to go quiet after {runs[-1].get('program') or 'the run'}"
    return runs, f"after the run{'s' if len(runs) > 1 else ''} " + ", ".join(m.get("program") or m["id"] for m in runs)


def _parse(text: str) -> dict[str, Any]:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError("no JSON in the answer")
    d = json.loads(m.group(0))
    if not isinstance(d.get("changes", []), list):
        raise ValueError("changes must be a list")
    return d


def apply(ws: Path, plan: dict[str, Any], ev: dict[str, Any], stamp: str) -> list[dict[str, Any]]:
    """Apply the model's changes conservatively; returns what was done (and what was skipped, with why)."""
    refs = {r["ref"] for r in ev.get("runs", [])} | {j["ref"] for j in ev.get("journal", [])} | {c["ref"] for c in ev.get("chat", [])}
    by_id = {r["id"]: r for r in L.load(ws)}
    date = time.strftime("%Y-%m-%d")
    done: list[dict[str, Any]] = []
    for c in (plan.get("changes") or [])[:MAX_CHANGES]:
        op = c.get("op")
        try:
            if op == "merge":
                ids = [i for i in c.get("ids") or [] if i in by_id and by_id[i]["status"] == "active"]
                if len(ids) < 2:
                    raise ValueError("a merge needs two active lessons")
                why = " | ".join(f"{i}: {by_id[i]['why']}" for i in ids)
                tags = sorted(set(c.get("tags") or []) | {t for i in ids for t in by_id[i]["tags"]})
                chk = c.get("check") or next((by_id[i]["check"] for i in ids if by_id[i].get("check")), None)
                new = L.add(ws, c["title"], c["rule"], why, tags, chk if chk in L.CHECKS else None, source="dream")
                for i in ids:
                    L.update(ws, i, status="retired", why_add=f"[{date}: merged into {new['id']} by consolidation: {c.get('reason', '')}]")
                done.append({"op": op, "ids": ids, "into": new["id"], "reason": c.get("reason")})
            elif op == "rewrite":
                i = c.get("id")
                if i not in by_id or by_id[i]["status"] != "active":
                    raise ValueError(f"no active lesson {i}")
                chk = c.get("check")
                L.update(ws, i, title=c.get("title"), rule=c.get("rule"), tags=c.get("tags"),
                         check=(chk if chk in L.CHECKS else "") if "check" in c else None,
                         why_add=f"[{date}: rule reworded by consolidation: {c.get('reason', '')}]")
                done.append({"op": op, "id": i, "before": by_id[i]["rule"], "reason": c.get("reason")})
            elif op == "retire":
                i = c.get("id")
                if i not in by_id or by_id[i]["status"] != "active" or not c.get("reason"):
                    raise ValueError("retire needs an active lesson and a reason")
                L.update(ws, i, status="retired", why_add=f"[{date}: retired by consolidation: {c['reason']}]")
                done.append({"op": op, "id": i, "reason": c["reason"]})
            elif op == "add":
                cited = [e for e in c.get("evidence") or [] if e in refs]
                if not cited:
                    raise ValueError("a new lesson must cite evidence from this pass (run or journal refs)")
                chk = c.get("check")
                new = L.add(ws, c["title"], c["rule"], f"{c.get('why', '')} (evidence: {', '.join(cited)})", c.get("tags") or ["cam"],
                            chk if chk in L.CHECKS else None, source="dream")
                done.append({"op": op, "id": new["id"], "evidence": cited})
            else:
                raise ValueError(f"unknown op {op!r}")
        except (ValueError, KeyError) as e:
            done.append({"op": op, "skipped": str(e), "change": c})
        by_id = {r["id"]: r for r in L.load(ws)}
    return done


async def run(ws: Path, llm: Callable[[str, str], Awaitable[str]], reason: str = "asked", runs: list[dict] | None = None,
              chat: list[dict] | None = None) -> dict[str, Any]:
    """One pass: snapshot, ask, apply, log; the runs it looked at are marked seen. `runs` (from due()) and `chat` (the
    conversation since the first of them started) are the evidence; without runs, everything since the last pass."""
    d = _dir(ws)
    lock = d / "lock"
    if lock.exists() and time.time() - lock.stat().st_mtime < 3600:
        raise RuntimeError("a consolidation pass is already running")
    lock.write_text(str(time.time()))
    try:
        st = state(ws)
        rows = L.load(ws)
        ids = [m["id"] for m in runs] if runs else None
        since = min((m.get("t0") or 0) for m in runs) if runs else float(st.get("last_t") or 0)
        ev = evidence(ws, since, ids, chat)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        (d / f"{stamp}.lessons.json").write_text(json.dumps({"lessons": rows}, indent=1))
        prompt = ("LESSONS:\n" + json.dumps(rows, indent=1) + "\n\nEVIDENCE SINCE THE LAST CONSOLIDATION:\n" + json.dumps(ev, indent=1)
                  + "\n\nAnswer with the JSON only.")
        answer = await llm(SYSTEM, prompt)
        try:
            plan = _parse(answer)
            done = apply(ws, plan, ev, stamp)
            err = None
        except (ValueError, json.JSONDecodeError) as e:
            plan, done, err = {}, [], f"unreadable answer: {e}"
        entry = {"stamp": stamp, "t": time.time(), "reason": reason, "runs": [r["ref"] for r in ev["runs"]],
                 "summary": plan.get("summary", ""), "changes": done,
                 "error": err, "lessons_before": len([r for r in rows if r["status"] == "active"]),
                 "lessons_after": len([r for r in L.load(ws) if r["status"] == "active"])}
        (d / f"{stamp}.json").write_text(json.dumps(entry, indent=1))
        st = state(ws)
        st.update(last_t=time.time(), last_stamp=stamp,
                  seen_runs=sorted(set(st.get("seen_runs") or []) | {r["ref"] for r in ev["runs"]}))
        _save_state(ws, st)
        return entry
    finally:
        lock.unlink(missing_ok=True)


def log(ws: Path, n: int = 10) -> list[dict[str, Any]]:
    out = []
    for p in sorted(_dir(ws).glob("*.json"), reverse=True):
        if p.name == "state.json" or p.name.endswith(".lessons.json"):
            continue
        try:
            out.append(json.loads(p.read_text()))
        except (OSError, ValueError):
            continue
        if len(out) >= n:
            break
    return out


def undo(ws: Path) -> dict[str, Any]:
    """Put the lessons back as they were before the last pass (the pass stays in the log, marked undone)."""
    entries = log(ws, 1)
    if not entries:
        raise ValueError("no consolidation to undo")
    e = entries[0]
    snap = _dir(ws) / f"{e['stamp']}.lessons.json"
    if e.get("undone") or not snap.exists():
        raise ValueError("the last consolidation was already undone")
    L._save(ws, json.loads(snap.read_text())["lessons"])
    e["undone"] = time.time()
    (_dir(ws) / f"{e['stamp']}.json").write_text(json.dumps(e, indent=1))
    return e
