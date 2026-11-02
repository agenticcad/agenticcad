"""Lessons learnt: rules the assistant (or the user) wrote down after something went wrong, read back before the same kind
of work - not as a memory file the model may skip, but handed to it by the tools that start that work (cam_context,
build_cam, the first machine step of a conversation) and, where a rule can be checked, tied to the simulation's verdict
code so a broken lesson shows up by name in the result.

Kept in <workspace>/lessons.json, shared by every design (lessons are about the machine, the material and the process).
Each lesson: id, title, rule, why (the evidence: date, run, what happened), tags (where it applies: cam, machine, probe,
tabs, chamfer, flip, a machine slug, a material), check (a cam_sim verdict code that catches it, optional), source
(seed / assistant / user), status (active / retired), created, updated.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

FILE = "lessons.json"
CHECKS = ("GOUGE", "THIN_TAB", "RELEASED", "LEFT", "BITE", "COLLISION", "RAPID", "FREED")
DOMAINS = {"cam", "machine", "probe"}

# what 2026-11-02's boat-latch job on the Z1 taught (agenticcad-admin/latch-job-review-2026-11-02.md)
SEED: list[dict[str, Any]] = [
    {"title": "Finish every part before it is released",
     "rule": "Every chamfer, face, pocket and hole on a part runs BEFORE the cut that releases it (its tabbed profile). "
             "Order per part: clear, finish walls, chamfer, release last.",
     "why": "2026-11-02 latch: the chamfers ran after the tabbed profiles; the hub+ring broke loose and the chamfer bit "
            "ran on parts held only by tabs.", "tags": ["cam", "tabs", "chamfer"], "check": "RELEASED"},
    {"title": "Tabs need 2 mm of real metal",
     "rule": "In aluminium every tab keeps at least 2 mm of real metal, counted from the material actually there: a part "
             "whose back was relieved from the other setup has less under the tab than tab_height says. Use few, wide tabs "
             "on straight runs.",
     "why": "2026-11-02 latch: hub+ring tabs 2 × 4 mm wide had 0.9 mm of metal (side A had relieved the back) and broke "
            "on the release pass; the ring release again had 0.0-0.9 mm.", "tags": ["cam", "tabs", "flip"], "check": "THIN_TAB"},
    {"title": "Chamfer every edge the model chamfers",
     "rule": "Go through each body's chamfered and bevelled edges in the model and give each one a chamfer op (or say it is "
             "hand work). Chamfer only edges the model chamfers.",
     "why": "2026-11-02 latch: the cam arm (3 mm lead-in bevel), the lift ring and the hub chamfers were never cut; the "
            "later arm chamfer put a 0.8 mm chamfer on a boss rim the model leaves sharp (a 0.53 mm gouge).",
     "tags": ["cam", "chamfer"], "check": "LEFT"},
    {"title": "Profile each body on its own",
     "rule": "Never put one outline around two parts: each body gets its own profile and its own tabs.",
     "why": "2026-11-02 latch: hub and lift ring were unioned into one outline; the C ring never got its own contour.",
     "tags": ["cam", "tabs"]},
    {"title": "Take every level down to its design height",
     "rule": "A pocket region must include the islands and landings that stop at a higher level, cut to their own height "
             "(free_region within the whole recess) - otherwise they stand at full stock height and later ops (rim chamfers) "
             "cut through them.",
     "why": "2026-11-02 latch: B1 never cleared the stop island (its region excluded it), the island stood at z 10 and the "
            "recess rim chamfer cut through the landing.", "tags": ["cam"], "check": "LEFT"},
    {"title": "Simulate the ops you run, with their history, and fix every error",
     "rule": "Simulate exactly the ops you will run (earlier setups are chained in) and fix every GOUGE, BITE, THIN TAB, "
             "RELEASED and unmachined feature before running. Never explain an error away; if one must stay, the user decides.",
     "why": "2026-11-02 latch: the side-B simulation reported 671 mm² left (island, bevel, chamfers), BITEs and thin tabs; "
            "the fix ops ran with a reported gouge.", "tags": ["cam"], "check": "GOUGE"},
    {"title": "Z0 only from a probe or a touch-off",
     "rule": "Never work Z0 out from tool length offsets or earlier positions: each offset is relative to the tool fitted at "
             "the last Z-origin change and setting Z clears it. Probe (auto Z, probe ops, probe_descend) or touch off.",
     "why": "2026-11-02 14:29: Z0 computed from a probe position and two offsets measured against different reference tools "
            "was 9 mm off; T2 was driven into the plate.", "tags": ["machine", "probe"]},
    {"title": "Start probe routines where they expect to start",
     "rule": "Corner and boss routines start 1-10 mm above a probed Z0 (lower_to ~5 first); pocket_center starts INSIDE the "
             "hole: lower_to 5 over it, probe_descend {to_z: -depth}, then pocket_center with hole_dia. dia is the ball.",
     "why": "2026-11-02: a corner probe started 69 mm up and searched 108 mm; pocket_center from 5 mm above the hole moved "
            "20 mm sideways in air and alarmed.", "tags": ["machine", "probe"]},
    {"title": "A refusal is final",
     "rule": "When the app refuses a move, never send the same motion another way (console, goto, another action). Tell the "
             "user what was refused and why.",
     "why": "2026-11-02 17:02: lower_to -3 was refused and the same move went out as raw console G-code.", "tags": ["machine"]},
    {"title": "A flipped plate goes down turned",
     "rule": "After a flip, probe two register holes on the diagonal and turn the setup by the measured angle (setup orient "
             "rotation) before cutting; set XY from one hole and check the others.",
     "why": "2026-11-02 latch: the plate went down 0.17° clockwise after a front-to-back flip (0.4 mm at the far hole).",
     "tags": ["cam", "probe", "flip"]},
]


def _path(ws: Path) -> Path:
    return Path(ws) / FILE


def load(ws: Path) -> list[dict[str, Any]]:
    """All lessons; the file is created with the seed lessons the first time."""
    p = _path(ws)
    if not p.exists():
        now = time.strftime("%Y-%m-%d")
        rows = [{"id": f"L{i + 1}", **x, "check": x.get("check"), "source": "seed", "status": "active", "created": now, "updated": now}
                for i, x in enumerate(SEED)]
        _save(ws, rows)
        return rows
    try:
        return json.loads(p.read_text()).get("lessons", [])
    except (OSError, ValueError):
        return []


def _save(ws: Path, rows: list[dict[str, Any]]) -> None:
    p = _path(ws)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"lessons": rows}, indent=1))
    tmp.replace(p)


def _clean_tags(tags) -> list[str]:
    if isinstance(tags, str):
        tags = re.split(r"[,\s]+", tags)
    return sorted({re.sub(r"[^a-z0-9_.-]+", "-", str(t).lower()).strip("-") for t in (tags or []) if str(t).strip()} - {""})


def add(ws: Path, title: str, rule: str, why: str, tags, check: str | None = None, source: str = "assistant") -> dict[str, Any]:
    title, rule, why = (str(title or "").strip(), str(rule or "").strip(), str(why or "").strip())
    if not title or not rule or not why:
        raise ValueError("a lesson needs a title, the rule and why (the evidence: date, run, what happened)")
    if check and check not in CHECKS:
        raise ValueError(f"check: one of {', '.join(CHECKS)} (a simulation verdict code), or none")
    rows = load(ws)
    tags = _clean_tags(tags) or ["cam"]
    dup = next((r for r in rows if r["status"] == "active" and r["title"].lower() == title.lower()), None)
    if dup:
        raise ValueError(f"{dup['id']} already says this ('{dup['title']}'): update it instead")
    n = max((int(r["id"][1:]) for r in rows if re.fullmatch(r"L\d+", r.get("id", ""))), default=0) + 1
    now = time.strftime("%Y-%m-%d")
    row = {"id": f"L{n}", "title": title, "rule": rule, "why": why, "tags": tags, "check": check or None, "source": source,
           "status": "active", "created": now, "updated": now}
    rows.append(row)
    _save(ws, rows)
    return row


def update(ws: Path, lid: str, **fields) -> dict[str, Any]:
    rows = load(ws)
    row = next((r for r in rows if r["id"] == lid), None)
    if row is None:
        raise ValueError(f"no lesson {lid}")
    for k in ("title", "rule", "why", "status", "check"):
        if fields.get(k) is not None:
            v = str(fields[k]).strip()
            if k == "status" and v not in ("active", "retired"):
                raise ValueError("status: active | retired")
            if k == "check" and v and v not in CHECKS:
                raise ValueError(f"check: one of {', '.join(CHECKS)}")
            row[k] = v or None if k == "check" else v
    if fields.get("tags") is not None:
        row["tags"] = _clean_tags(fields["tags"]) or row["tags"]
    if fields.get("why_add"):
        row["why"] = row["why"] + " " + str(fields["why_add"]).strip()
    row["updated"] = time.strftime("%Y-%m-%d")
    _save(ws, rows)
    return row


def remove(ws: Path, lid: str) -> None:
    _save(ws, [r for r in load(ws) if r["id"] != lid])


def relevant(rows: list[dict[str, Any]], tags: set[str]) -> list[dict[str, Any]]:
    """Active lessons for work tagged `tags`: the domain tags (cam, machine, probe) must meet the work's, and a lesson with
    narrower tags (tabs, chamfer, flip, a machine, a material) needs at least one of them in the work."""
    tags = {t.lower() for t in tags}
    out = []
    for r in rows:
        rt = set(r.get("tags") or [])
        domain, rest = rt & DOMAINS, rt - DOMAINS
        if r.get("status") == "active" and (not domain or domain & tags) and (not rest or rest & tags):
            out.append(r)
    return out


def text(rows: list[dict[str, Any]], heading: str) -> str:
    if not rows:
        return ""
    return heading + "\n" + "\n".join(f"- {r['id']} {r['title']}: {r['rule']}" for r in rows)


def by_check(rows: list[dict[str, Any]], code: str) -> list[dict[str, Any]]:
    return [r for r in rows if r.get("status") == "active" and r.get("check") == code]
