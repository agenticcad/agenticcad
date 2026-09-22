"""
Headless eval harness: runs the real CadAgent (Claude Agent SDK session) against task prompts with no
browser, grades the resulting geometry / program / answer with deterministic checks, and records
cost, turns and duration per case.

The viewer's screenshot tool is replaced by an offscreen matplotlib render of the display mesh so the
agent can still look at what it built.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cad_kernel as ck  # noqa: E402
import cam_kernel as cam  # noqa: E402
from agent import CadAgent  # noqa: E402
from library import PartLibrary  # noqa: E402


# --------------------------------------------------------------------------- offscreen screenshot
def render_mesh(model: ck.Model, spec: dict[str, Any]) -> str:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection
    import numpy as np

    fig = plt.figure(figsize=(8, 6), dpi=100)
    ax = fig.add_subplot(111, projection="3d")
    highlight = set(spec.get("highlight") or [])
    tints = ["#9fb0c2", "#b7b09a", "#9fc0b0", "#b0a0c0", "#c0a89a"]
    for i, bd in enumerate(model.mesh["bodies"]):
        P = np.array(bd["positions"]).reshape(-1, 3)
        I = np.array(bd["indices"]).reshape(-1, 3)
        colors = np.full(len(I), tints[i % len(tints)], dtype=object)
        for fid, start, count in bd["faceRanges"]:
            if fid in highlight:
                colors[start // 3:(start + count) // 3] = "#ff8c42"
        step = max(1, len(I) // 6000)
        polys = P[I[::step]]
        pc = Poly3DCollection(polys, facecolors=list(colors[::step]), edgecolors="none", alpha=1.0)
        ax.add_collection3d(pc)
        if spec.get("showEdges", True):
            for pl in bd["edges"]:
                pts = np.array(pl).reshape(-1, 3)
                ax.plot(pts[:, 0], pts[:, 1], pts[:, 2], color="#111", lw=0.5)
    lo, hi = np.array(model.bbox_min), np.array(model.bbox_max)
    c = (lo + hi) / 2; r = max(hi - lo) / 2 or 1
    ax.set_xlim(c[0] - r, c[0] + r); ax.set_ylim(c[1] - r, c[1] + r); ax.set_zlim(c[2] - r, c[2] + r)
    views = {"iso": (30, -60), "iso_back": (30, 120), "front": (0, -90), "back": (0, 90), "left": (0, 180),
             "right": (0, 0), "top": (90, -90), "bottom": (-90, -90)}
    el, az = views.get(spec.get("view") or "iso", views["iso"])
    ax.view_init(elev=el, azim=az)
    ax.set_xlabel("X"); ax.set_ylabel("Y"); ax.set_zlabel("Z")
    buf = io.BytesIO()
    fig.savefig(buf, format="jpeg", pil_kwargs={"quality": 70})
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


# --------------------------------------------------------------------------- run one case
@dataclass
class Case:
    id: str
    prompt: str
    graders: list[Callable[["Run"], list[tuple[str, bool, str]]]]
    tags: list[str] = field(default_factory=list)
    initial_code: str | None = None            # design to start from (default: NEW_DESIGN_CODE)
    setup: Callable[["Run"], Awaitable[None]] | None = None   # e.g. seed the library, pick a selection
    selection: dict[str, Any] | None = None
    images: list[dict[str, Any]] | None = None
    description: str = ""


@dataclass
class Run:
    case: Case
    workspace: Path
    agent: CadAgent
    events: list[dict[str, Any]] = field(default_factory=list)
    text: str = ""
    cost: float = 0.0
    turns: int = 0
    duration_s: float = 0.0
    tool_errors: int = 0
    tools_used: list[str] = field(default_factory=list)
    error: str | None = None
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    @property
    def model(self) -> ck.Model | None:
        return self.agent.model

    @property
    def program(self) -> cam.Program | None:
        return self.agent.program

    @property
    def passed(self) -> bool:
        return self.error is None and bool(self.checks) and all(ok for _, ok, _ in self.checks)


async def run_case(case: Case, model_name: str | None = None) -> Run:
    ws = Path(tempfile.mkdtemp(prefix=f"eval_{case.id}_")) / "workspace"
    for sub in ("exports", "history", "designs", "imports", "images", "drawings"):
        (ws / sub).mkdir(parents=True)
    # machine/tool libraries: copy the defaults from the real workspace when present
    src = ROOT / "workspace"
    if (src / "tools.json").exists():
        shutil.copy(src / "tools.json", ws / "tools.json")
    if (src / "machines").exists():
        shutil.copytree(src / "machines", ws / "machines")
    run: Run | None = None
    events: list[dict[str, Any]] = []

    async def emit(e: dict[str, Any]) -> None:
        events.append(e)

    async def screenshot(spec: dict[str, Any]) -> str:
        assert run is not None and run.model is not None
        return await asyncio.to_thread(render_mesh, run.model, spec)

    if model_name:
        os.environ["AGENTICCAD_MODEL"] = model_name
    agent = CadAgent(ws, emit, screenshot)
    run = Run(case, ws, agent, events)
    try:
        await agent.build(case.initial_code or ck.NEW_DESIGN_CODE, source="load")
        if case.setup:
            await case.setup(run)
        events.clear()
        t0 = time.time()
        await agent.chat(case.prompt, case.selection, case.images)
        run.duration_s = time.time() - t0
        for e in events:
            t = e.get("type")
            if t == "text":
                run.text += e.get("text", "") + "\n"
            elif t == "result":
                run.cost = float(e.get("cost") or 0); run.turns = int(e.get("turns") or 0)
            elif t == "tool_use":
                run.tools_used.append(e.get("name", ""))
            elif t == "tool_result" and e.get("is_error"):
                run.tool_errors += 1
            elif t == "error":
                run.error = e.get("text")
        for g in case.graders:
            try:
                run.checks.extend(g(run))
            except Exception as ex:  # noqa: BLE001
                run.checks.append((getattr(g, "__name__", "grader"), False, f"grader raised {type(ex).__name__}: {ex}"))
    except Exception as ex:  # noqa: BLE001
        run.error = f"{type(ex).__name__}: {ex}"
    finally:
        try:
            await agent.stop()
        except Exception:
            pass
    return run


# --------------------------------------------------------------------------- grader helpers
def check(name: str, ok: bool, detail: str = "") -> tuple[str, bool, str]:
    return (name, bool(ok), detail)


def bbox_size(m: ck.Model) -> tuple[float, float, float]:
    return tuple(b - a for a, b in zip(m.bbox_min, m.bbox_max))


def expect_bbox(size, tol=0.5, body: str | None = None):
    def g(run: Run):
        m = run.model
        if m is None:
            return [check("bbox", False, "no model")]
        if body:
            b = m.body_by_name(body)
            if b is None:
                return [check("bbox", False, f"no body {body}")]
            s = tuple(hi - lo for lo, hi in zip(b.bbox_min, b.bbox_max))
        else:
            s = bbox_size(m)
        ok = all(abs(a - b) <= tol for a, b in zip(s, size))
        return [check("bbox " + ("×".join(f"{v:g}" for v in size)), ok, f"got {tuple(round(v, 2) for v in s)}")]
    return g


def expect_bodies(names: list[str] | None = None, count: int | None = None):
    def g(run: Run):
        m = run.model
        if m is None:
            return [check("bodies", False, "no model")]
        got = [b.path for b in m.bodies]
        out = []
        if count is not None:
            out.append(check(f"{count} bodies", len(got) == count, f"got {got}"))
        if names is not None:
            out.append(check("body names " + "/".join(names), all(n in got or any(x.endswith("/" + n) for x in got) for n in names), f"got {got}"))
        return out
    return g


def expect_holes(count: int, diameter: float | None = None, tol=0.15, through: bool | None = None, body: str | None = None):
    def g(run: Run):
        m = run.model
        if m is None:
            return [check("holes", False, "no model")]
        shape = m.body_by_name(body).shape if body else m.shape
        hs = cam.holes(shape, dmin=diameter - tol if diameter else 0, dmax=diameter + tol if diameter else 1e9)
        if through is not None:
            hs = [h for h in hs if h.through == through]
        return [check(f"{count} hole(s)" + (f" Ø{diameter:g}" if diameter else ""), len(hs) == count, f"got {hs}")]
    return g


def expect_volume(v: float, rel=0.02, body: str | None = None):
    def g(run: Run):
        m = run.model
        if m is None:
            return [check("volume", False, "no model")]
        got = m.body_by_name(body).volume if body else m.volume
        return [check(f"volume ≈ {v:g}", abs(got - v) <= rel * v, f"got {got:.1f}")]
    return g


def expect_face_kinds(*kinds: str):
    def g(run: Run):
        m = run.model
        present = {f.kind for f in m.faces} if m else set()
        return [check("has " + k, k in present, f"kinds {sorted(present)}") for k in kinds]
    return g


def expect_params(*names: str, values: dict[str, float] | None = None):
    import script_edit
    def g(run: Run):
        m = run.model
        ps = {p["name"]: p["value"] for p in script_edit.params(m.code)} if m else {}
        out = [check("has parameters", len(ps) >= max(1, len(names)), f"params {ps}")]
        for n in names:
            out.append(check(f"param {n}", n in ps, ""))
        for k, v in (values or {}).items():
            out.append(check(f"param {k} = {v:g}", k in ps and abs(ps[k] - v) < 1e-6, f"got {ps.get(k)}"))
        return out
    return g


def expect_script_contains(*needles: str):
    def g(run: Run):
        code = run.model.code if run.model else ""
        return [check(f"script has {n!r}", n in code, "") for n in needles]
    return g


def expect_answer(pattern: str, flags=re.I):
    def g(run: Run):
        return [check(f"answer matches /{pattern}/", re.search(pattern, run.text, flags) is not None, run.text.strip()[:200])]
    return g


def expect_program(kinds: list[str] | None = None, min_ops: int = 1, no_warnings_matching: str | None = None):
    def g(run: Run):
        p = run.program
        if p is None:
            return [check("program built", False, "no program")]
        out = [check(f"≥{min_ops} ops", len(p.ops) >= min_ops, f"{len(p.ops)} ops: {[o.kind for o in p.ops]}")]
        got = [o.kind for o in p.ops]
        for k in kinds or []:
            out.append(check(f"op kind {k}", k in got, f"kinds {got}"))
        w = p.check()
        if no_warnings_matching:
            out.append(check(f"no warning /{no_warnings_matching}/", not any(re.search(no_warnings_matching, x) for x in w), "; ".join(w)))
        return out
    return g


def expect_op(kind: str, **params):
    """An op of this kind exists and its params match (e.g. tabs=4, side='inside')."""
    def g(run: Run):
        p = run.program
        ops = [o for o in (p.ops if p else []) if o.kind == kind]
        if not ops:
            return [check(f"{kind} op", False, "missing")]
        for o in ops:
            if all(o.params.get(k) == v for k, v in params.items()):
                return [check(f"{kind} op with {params}", True, "")]
        return [check(f"{kind} op with {params}", False, f"got {[o.params for o in ops]}")]
    return g


def expect_gcode(max_tool_changes: int | None = None, z_floor: float | None = None):
    def g(run: Run):
        p = run.program
        if p is None:
            return [check("gcode", False, "no program")]
        gc = p.gcode(); lines = gc.splitlines()
        out = [check("gcode header/footer", "G21 G90 G94 G17" in lines and lines[-1] == "M30", "")]
        tc = sum(1 for l in lines if l.startswith("M6 "))
        seq = [o.tool.number for o in p.ops]
        transitions = sum(1 for a, b in zip(seq, seq[1:]) if a != b)
        out.append(check("one M6 per tool transition", tc == transitions, f"{tc} M6 for {transitions} transitions {seq}"))
        if max_tool_changes is not None:
            out.append(check(f"≤{max_tool_changes} tool changes", tc <= max_tool_changes, str(tc)))
        zs = [float(m.group(1)) for l in lines for m in [re.search(r"\bZ(-?[\d.]+)", l)] if m and l[:2] in ("G0", "G1", "G2", "G3")]
        oz = p.setup.origin_point()[2]
        if z_floor is not None:
            out.append(check(f"no Z below {z_floor:g} (model)", min(zs) + oz >= z_floor - 1e-6, f"min z {min(zs) + oz:.2f}"))
        out.append(check("has motion", len(zs) > 10, f"{len(lines)} lines"))
        return out
    return g


def expect_tools_used(*names: str):
    def g(run: Run):
        return [check(f"used tool {n}", n in run.tools_used, f"used {run.tools_used}") for n in names]
    return g


def no_agent_error():
    def g(run: Run):
        return [check("turn completed", run.error is None, run.error or "")]
    return g
