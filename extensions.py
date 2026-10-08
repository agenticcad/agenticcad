"""Extensions: Python files that extend the app itself, written by the agent (or you) while you work.

Two layers, like the Design Kit:
  built-in   ext_builtin/*.py            shipped with the app (examples and reference), read-only to the agent
  workspace  <workspace>/extensions/*.py  written through the agent's `extension save` (validated before it lands)

An extension file is exec'd with the design-script namespace (build123d, kit, helpers) plus the decorators below,
so it can build geometry too. Each decorated function registers one entry:

  @helper                         a function every design script can call by name
  @tool(name, description)        an agent-callable function fn(ctx, **args); its JSON schema comes from the
                                  signature (annotations float/int/str/bool, defaults = optional) unless given.
                                  agent=False keeps it UI-only (buttons can still call it).
  @panel(title, ...)              fn(ctx) -> a UI schema (see UI below). A ribbon button in the Extensions group opens
                                  it; the panel re-renders whenever one of its controls changes.
  @on_build                       fn(ctx) runs after every successful build; return a str or list[str] of warnings.

A broken file never breaks a design: it shows as FAILED TO LOAD in Settings ▸ Extensions and is skipped.
Everything an extension does to the model goes through the result dict it returns (applied by the host, see
RESULT KEYS); extension functions stay plain synchronous Python.
"""
from __future__ import annotations

import inspect
import json
import math
import re
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).parent
BUILTIN_DIR = ROOT / "ext_builtin"
UI_VERSION = 1

API_DOC = """# Extension API (v1)

An extension is one Python file. build123d, math, the script helpers and `kit` are pre-imported, plus these decorators:

```python
@helper                                   # callable from any design script as wall_thickness(...)
def wall_thickness(shape, point): ...

@tool("hole_report", "Every hole with diameter, depth and thread")   # agent-callable (mcp: ext_hole_report after a restart)
def hole_report(ctx, body: str = None):   # (ctx, **args); annotations -> JSON schema; defaults -> optional
    return {"table": ...}                 # return a str, or a dict of RESULT KEYS (below)

@tool("apply", "...", agent=False)        # UI-only: buttons and controls can `call` it, the agent can't
def apply(ctx, pcd: float, count: int, face=None): ...

@panel("Bolt pattern", icon="◎", description="Holes on a pitch circle", group="Extensions")
def bolt_pattern(ctx):                    # re-rendered whenever a control in the panel changes
    s = ctx.state                         # the panel's control values (by control id), persists while open
    return ui.panel(ui.picker("face", "Face", what="face"),
                    ui.number("pcd", "PCD", s.get("pcd", 40), min=1, unit="mm"),
                    ui.button("Apply", call="apply", primary=True))

@on_build                                 # after every successful build; return warnings (str or list)
def check(ctx): ...
```

## ctx
- `ctx.model` (cad_kernel.Model or None), `ctx.bodies`, `ctx.faces` (FaceInfo list), `ctx.code` (the script)
- `ctx.body(path)` -> Body: `.path` ("Housing/Cover"), `.name`, `.shape` (the build123d Solid/Compound: `.edges()`, `.faces()`,
  `.volume`, `.bounding_box()`), `.volume` (mm³), `.bbox_min` / `.bbox_max` (tuples), `.face_ids`
- `ctx.face(id)` -> FaceInfo: `.id`, `.kind` (PLANE | CYLINDER | CONE | SPHERE | TORUS | BSPLINE…), `.center`, `.normal` (planes),
  `.radius` (cylinders/spheres), `.area`, `.bbox_min` / `.bbox_max`, `.body_name`; `ctx.face_shape(id)` -> the build123d Face
- `ctx.params()` -> [{name, value, comment}] numeric parameters; `ctx.holes(body=None)` -> [{face, body, d, depth, through, center, axis, thread}]
- `ctx.threads` -> ThreadSpec list; `ctx.selection` -> {"faces": [ids], "bodies": [paths]} (what the user has selected)
- `ctx.state` (panel state dict, by control id), `ctx.event` (id of the control that changed, or None), `ctx.settings`, `ctx.workspace`
- `ctx.units` -> "mm" | "in"

## RESULT KEYS (a tool or button may return a dict with any of these; a plain str is a notify)
- `notify`: str (chat message) · `error`: str
- `select`: {"faces": [ids], "bodies": [paths]} · `highlight`: same shape, lights them without selecting · `fit`: true
- `set_params`: {name: value} (rebuilds) · `code`: full new script (rebuilds)
- `wrap`: {"body": path, "template": "{body} - Cylinder(3, 20)"} rewrites one body's expression (rebuilds)
- `append`: code added before `result` · `edits`: [{"old": .., "new": ..}] exact-text edits (rebuild)
- `chat`: str -> sent to the agent as if the user typed it (for "ask the agent to…" buttons)
- `ui`: a UI schema to show (replaces the panel's content, or opens an ephemeral panel) · `state`: dict merged into the panel state
- `open_panel`: panel id

## UI schema (JSON; `ui.*` builders return these dicts)
- `ui.panel(*children, title=None)` root · `ui.section(title, *children)` · `ui.row(*children)` horizontal
- `ui.text(text, muted=False, md=False)` · `ui.kv([[k, v], ...])` · `ui.badge(text, kind="ok|warn|err|info")`
- `ui.number(id, label, value, min=None, max=None, step=None, unit=None, call=None)`
- `ui.slider(id, label, value, min, max, step=None, unit=None, call=None)` (`call` runs that tool with the panel state on change)
- `ui.text_input(id, label, value="", placeholder="")` · `ui.select(id, label, value, options=[v | [v, label]])` · `ui.checkbox(id, label, value)`
- `ui.picker(id, label, what="face"|"body", hint=None)`: click → next click in the viewer fills it (face: {id, body, label, center, normal, radius}; body: path)
- `ui.table(columns, rows, on_row=None)`: rows = [[cells]] or [{"cells": [...], "face": id, "body": path, "data": {...}}]; a row with face/body selects it on click; `on_row` = a tool name called with the row's data
- `ui.button(label, call=None, args=None, chat=None, action=None, primary=False)`: `call` a tool (gets the panel state + args), `chat` a prompt, `action` a RESULT dict applied directly
- `ui.image(src, height=None)` (data: URL or /api path)
Controls carry their `id`; the value the user sets lands in `ctx.state[id]`.
"""


class ExtError(Exception):
    pass


# ------------------------------------------------------------------ registration (module-level, collected per exec)
_PENDING: list["Entry"] = []


@dataclass
class Entry:
    kind: str                     # helper | tool | panel | hook
    name: str                     # id within the extension: helper/tool function name, panel slug
    fn: Callable | None
    title: str = ""
    description: str = ""
    schema: dict[str, Any] | None = None
    agent: bool = True            # tools: visible to the agent
    icon: str = ""
    group: str = "Extensions"
    module: str = ""              # extension (file) id
    layer: str = "workspace"
    path: Path | None = None
    error: str = ""

    @property
    def id(self) -> str:
        return f"{self.module}.{self.name}" if self.module else self.name

    def to_dict(self) -> dict[str, Any]:
        d = {"kind": self.kind, "name": self.name, "id": self.id, "title": self.title or self.name, "description": self.description,
             "module": self.module, "layer": self.layer}
        if self.kind == "panel":
            d.update(icon=self.icon, group=self.group)
        if self.kind == "tool":
            d.update(agent=self.agent, schema=self.schema)
        return d


def _json_type(ann: Any) -> dict[str, Any]:
    if ann in (float,):
        return {"type": "number"}
    if ann in (int,):
        return {"type": "integer"}
    if ann in (bool,):
        return {"type": "boolean"}
    if ann in (str,):
        return {"type": "string"}
    if ann in (list, tuple):
        return {"type": "array"}
    if ann in (dict,):
        return {"type": "object"}
    return {}


def schema_from_signature(fn: Callable) -> dict[str, Any]:
    """JSON schema for fn(ctx, **args) from annotations and defaults."""
    props: dict[str, Any] = {}
    req: list[str] = []
    sig = inspect.signature(fn)
    for i, (n, p) in enumerate(sig.parameters.items()):
        if i == 0 or p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        t = _json_type(p.annotation) if p.annotation is not inspect._empty else {}
        if p.default is inspect._empty:
            req.append(n)
        elif p.default is not None and not t:
            t = _json_type(type(p.default))
        props[n] = t or {}
    return {"type": "object", "properties": props, **({"required": req} if req else {})}


def helper(fn: Callable) -> Callable:
    _PENDING.append(Entry("helper", fn.__name__, fn, description=(fn.__doc__ or "").strip().splitlines()[0] if fn.__doc__ else ""))
    return fn


def tool(name: str, description: str = "", schema: dict[str, Any] | None = None, agent: bool = True) -> Callable:
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", name or ""):
        raise ExtError(f"tool name {name!r}: use lowercase letters, digits and _ (max 41 chars)")

    def deco(fn: Callable) -> Callable:
        _PENDING.append(Entry("tool", name, fn, description=description or (fn.__doc__ or "").strip(),
                              schema=schema or schema_from_signature(fn), agent=agent))
        return fn
    return deco


def panel(title: str, icon: str = "", description: str = "", group: str = "Extensions", name: str | None = None) -> Callable:
    def deco(fn: Callable) -> Callable:
        slug = name or re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_") or fn.__name__
        _PENDING.append(Entry("panel", slug, fn, title=title, description=description, icon=icon, group=group))
        return fn
    return deco


def on_build(fn: Callable) -> Callable:
    _PENDING.append(Entry("hook", fn.__name__, fn, description=(fn.__doc__ or "").strip()))
    return fn


# ------------------------------------------------------------------ UI builders (plain dicts; the browser renders them)
class _UI:
    version = UI_VERSION

    @staticmethod
    def _ctl(type_: str, id_: str, label: str, value: Any, **kw: Any) -> dict[str, Any]:
        return {"type": type_, "id": id_, "label": label, "value": value, **{k: v for k, v in kw.items() if v is not None}}

    def panel(self, *children: Any, title: str | None = None) -> dict[str, Any]:
        d: dict[str, Any] = {"type": "panel", "v": UI_VERSION, "children": [c for c in children if c]}
        if title:
            d["title"] = title
        return d

    def section(self, title: str, *children: Any) -> dict[str, Any]:
        return {"type": "section", "title": title, "children": [c for c in children if c]}

    def row(self, *children: Any) -> dict[str, Any]:
        return {"type": "row", "children": [c for c in children if c]}

    def text(self, text: str, muted: bool = False, md: bool = False) -> dict[str, Any]:
        return {"type": "text", "text": str(text), **({"muted": True} if muted else {}), **({"md": True} if md else {})}

    def kv(self, rows: list[list[Any]]) -> dict[str, Any]:
        return {"type": "kv", "rows": [[str(k), _fmt(v)] for k, v in rows]}

    def badge(self, text: str, kind: str = "info") -> dict[str, Any]:
        return {"type": "badge", "text": str(text), "kind": kind}

    def number(self, id: str, label: str, value: Any, min: float | None = None, max: float | None = None, step: float | None = None,
               unit: str | None = None, call: str | None = None) -> dict[str, Any]:
        return self._ctl("number", id, label, value, min=min, max=max, step=step, unit=unit, call=call)

    def slider(self, id: str, label: str, value: Any, min: float, max: float, step: float | None = None, unit: str | None = None,
               call: str | None = None) -> dict[str, Any]:
        return self._ctl("slider", id, label, value, min=min, max=max, step=step, unit=unit, call=call)

    def text_input(self, id: str, label: str, value: str = "", placeholder: str = "", call: str | None = None) -> dict[str, Any]:
        return self._ctl("text_input", id, label, value, placeholder=placeholder or None, call=call)

    def select(self, id: str, label: str, value: Any, options: list[Any], call: str | None = None) -> dict[str, Any]:
        opts = [o if isinstance(o, (list, tuple)) else [o, str(o)] for o in options]
        return self._ctl("select", id, label, value, options=[[a, str(b)] for a, b in opts], call=call)

    def checkbox(self, id: str, label: str, value: bool = False, call: str | None = None) -> dict[str, Any]:
        return self._ctl("checkbox", id, label, bool(value), call=call)

    def picker(self, id: str, label: str, what: str = "face", hint: str | None = None, call: str | None = None) -> dict[str, Any]:
        if what not in ("face", "body"):
            raise ExtError("picker what: face | body")
        return {"type": "picker", "id": id, "label": label, "what": what, **({"hint": hint} if hint else {}), **({"call": call} if call else {})}

    def table(self, columns: list[str], rows: list[Any], on_row: str | None = None) -> dict[str, Any]:
        out = []
        for r in rows:
            if isinstance(r, dict):
                out.append({**r, "cells": [_fmt(c) for c in r.get("cells", [])]})
            else:
                out.append({"cells": [_fmt(c) for c in r]})
        return {"type": "table", "columns": [str(c) for c in columns], "rows": out, **({"on_row": on_row} if on_row else {})}

    def button(self, label: str, call: str | None = None, args: dict[str, Any] | None = None, chat: str | None = None,
               action: dict[str, Any] | None = None, primary: bool = False) -> dict[str, Any]:
        d: dict[str, Any] = {"type": "button", "label": label}
        for k, v in (("call", call), ("args", args), ("chat", chat), ("action", action)):
            if v is not None:
                d[k] = v
        if primary:
            d["primary"] = True
        return d

    def image(self, src: str, height: int | None = None) -> dict[str, Any]:
        return {"type": "image", "src": src, **({"height": height} if height else {})}


ui = _UI()


def _fmt(v: Any) -> Any:
    if isinstance(v, float):
        return round(v, 3)
    if isinstance(v, (int, str, bool)) or v is None:
        return v
    return str(v)


# ------------------------------------------------------------------ the context handed to extension functions
class Ctx:
    """What an extension function sees. Read access to the model; changes go back through the result dict."""

    def __init__(self, model: Any, workspace: Path, settings: dict[str, Any] | None = None, state: dict[str, Any] | None = None,
                 event: str | None = None, selection: dict[str, Any] | None = None, units: str = "mm"):
        self.model = model
        self.workspace = workspace
        self.settings = settings or {}
        self.state: dict[str, Any] = state if state is not None else {}
        self.event = event
        self.selection = selection or {"faces": [], "bodies": []}
        self.units = units

    # ---- model access
    @property
    def bodies(self) -> list[Any]:
        return list(self.model.bodies) if self.model else []

    @property
    def faces(self) -> list[Any]:
        return list(self.model.faces) if self.model else []

    @property
    def code(self) -> str:
        return self.model.code if self.model else ""

    @property
    def threads(self) -> list[Any]:
        return list(getattr(self.model, "threads", []) or []) if self.model else []

    def body(self, path: str) -> Any:
        for b in self.bodies:
            if b.path == path or b.name == path:
                return b
        raise ExtError(f"no body {path!r}; bodies: {[b.path for b in self.bodies]}")

    def face(self, fid: int) -> Any:
        for f in self.faces:
            if f.id == int(fid):
                return f
        raise ExtError(f"no face #{fid}")

    def face_shape(self, fid: int) -> Any:
        if not self.model:
            raise ExtError("no model")
        return self.model.get_face(int(fid))

    def params(self) -> list[dict[str, Any]]:
        if not self.model:
            return []
        import script_edit
        try:
            return script_edit.params(self.model.code)
        except SyntaxError:
            return []

    def holes(self, body: str | None = None) -> list[dict[str, Any]]:
        """Round holes: concave cylindrical faces, with the thread registered at that position when there is one."""
        if not self.model:
            return []
        from OCP.BRepAdaptor import BRepAdaptor_Surface
        from OCP.GeomAbs import GeomAbs_Cylinder
        out = []
        bodies = [self.body(body)] if body else self.bodies
        for b in bodies:
            for fid in b.face_ids:
                face = self.model.get_face(fid)
                ad = BRepAdaptor_Surface(face.wrapped)
                if ad.GetType() != GeomAbs_Cylinder:
                    continue
                cyl = ad.Cylinder()
                loc, d = cyl.Axis().Location(), cyl.Axis().Direction()
                ax = (d.X(), d.Y(), d.Z())
                p0 = (loc.X(), loc.Y(), loc.Z())
                c = face.center()
                try:
                    n = face.normal_at(c)
                except Exception:  # noqa: BLE001
                    continue
                # foot of the perpendicular from the face centre to the axis; concave when the normal points at the axis
                t = sum((c.to_tuple()[i] - p0[i]) * ax[i] for i in range(3))
                foot = [p0[i] + ax[i] * t for i in range(3)]
                to_axis = [foot[i] - c.to_tuple()[i] for i in range(3)]
                if sum(n.to_tuple()[i] * to_axis[i] for i in range(3)) <= 0:
                    continue
                lo, hi = _extent_along(face, ax)           # the hole's span along its axis
                depth = hi - lo
                bext = [b.bbox_max[i] - b.bbox_min[i] for i in range(3)]
                body_len = abs(sum(bext[i] * abs(ax[i]) for i in range(3))) if max(abs(a) for a in ax) > 0.999 else max(bext)
                thread = None
                for th in self.threads:                     # a thread registered on this axis, within the hole's span
                    rel = [th.at[i] - foot[i] for i in range(3)]
                    along = sum(rel[i] * ax[i] for i in range(3))
                    off = math.sqrt(max(sum(r * r for r in rel) - along * along, 0.0))
                    if off < 0.3 and lo - 0.5 <= along + sum(foot[i] * ax[i] for i in range(3)) <= hi + 0.5:
                        thread = th.size
                        break
                out.append({"face": fid, "body": b.path, "d": round(2 * cyl.Radius(), 3), "depth": round(depth, 3),
                            "through": depth >= body_len - 1e-3, "center": [round(v, 3) for v in foot], "axis": [round(v, 3) for v in ax],
                            "thread": thread})
        return out


def _extent_along(face: Any, ax: tuple[float, float, float]) -> tuple[float, float]:
    vs = [v.to_tuple() for v in face.vertices()]
    if not vs:
        return 0.0, 0.0
    proj = [sum(v[i] * ax[i] for i in range(3)) for v in vs]
    return min(proj), max(proj)


# ------------------------------------------------------------------ loading
def _exec_file(p: Path, module: str, layer: str) -> list[Entry]:
    import cad_kernel as ck
    _PENDING.clear()
    ns = ck.script_namespace()
    ns.update(helper=helper, tool=tool, panel=panel, on_build=on_build, ui=ui, ExtError=ExtError)
    ns["__name__"] = f"agenticcad_ext_{module}"
    ns["__file__"] = str(p)
    try:
        exec(compile(p.read_text(), str(p), "exec"), ns)
    except Exception as ex:  # noqa: BLE001
        _PENDING.clear()
        tb = traceback.format_exc().strip().splitlines()[-1]
        return [Entry("broken", module, None, title=module, module=module, layer=layer, path=p, error=f"{tb}")]
    out = list(_PENDING)
    _PENDING.clear()
    for e in out:
        e.module, e.layer, e.path = module, layer, p
    return out


class Extensions:
    """One per workspace: built-in examples + <workspace>/extensions/*.py, reloaded when a file changes."""

    def __init__(self, workspace: Path | None):
        self.ws = Path(workspace) / "extensions" if workspace else None
        self._cache: tuple[tuple, list[Entry]] | None = None
        self._state_path = self.ws / "state.json" if self.ws else None

    # ---- files
    def _files(self) -> list[tuple[Path, str]]:
        out = [(p, "builtin") for p in sorted(BUILTIN_DIR.glob("*.py"))] if BUILTIN_DIR.exists() else []
        if self.ws and self.ws.exists():
            out += [(p, "workspace") for p in sorted(self.ws.glob("*.py")) if not p.name.startswith(".")]
        return out

    def stamp(self) -> tuple:
        files = self._files()
        st = self._state_path.stat().st_mtime if self._state_path and self._state_path.exists() else 0.0
        return tuple((str(p), p.stat().st_mtime) for p, _ in files) + (st,)

    def _state(self) -> dict[str, Any]:
        if self._state_path and self._state_path.exists():
            try:
                return json.loads(self._state_path.read_text())
            except Exception:  # noqa: BLE001
                return {}
        return {}

    def disabled(self) -> set[str]:
        return set(self._state().get("disabled") or [])

    def set_enabled(self, module: str, enabled: bool) -> None:
        if not self.ws:
            raise ExtError("no workspace")
        self.ws.mkdir(parents=True, exist_ok=True)
        st = self._state()
        dis = set(st.get("disabled") or [])
        (dis.discard if enabled else dis.add)(module)
        st["disabled"] = sorted(dis)
        self._state_path.write_text(json.dumps(st, indent=1))
        self._cache = None

    def entries(self, include_disabled: bool = False) -> list[Entry]:
        stamp = self.stamp()
        if self._cache is None or self._cache[0] != stamp:
            out: list[Entry] = []
            for p, layer in self._files():
                out.extend(_exec_file(p, p.stem, layer))
            self._cache = (stamp, out)
        es = self._cache[1]
        if include_disabled:
            return list(es)
        dis = self.disabled()
        return [e for e in es if e.module not in dis]

    def modules(self) -> list[dict[str, Any]]:
        """Per file: id, layer, enabled, what it registers, load error."""
        dis = self.disabled()
        by: dict[str, dict[str, Any]] = {}
        for e in self.entries(include_disabled=True):
            m = by.setdefault(e.module, {"id": e.module, "layer": e.layer, "path": str(e.path), "enabled": e.module not in dis,
                                         "helpers": [], "tools": [], "panels": [], "hooks": [], "error": ""})
            if e.kind == "broken":
                m["error"] = e.error
            elif e.kind == "helper":
                m["helpers"].append(e.name)
            elif e.kind == "tool":
                m["tools"].append(e.name)
            elif e.kind == "panel":
                m["panels"].append({"id": e.id, "title": e.title, "icon": e.icon, "group": e.group, "description": e.description})
            elif e.kind == "hook":
                m["hooks"].append(e.name)
        return list(by.values())

    def payload(self) -> dict[str, Any]:
        """What the browser needs: the ribbon buttons and the Settings list."""
        return {"type": "extensions", "panels": [e.to_dict() for e in self.entries() if e.kind == "panel"],
                "modules": self.modules(), "ui_version": UI_VERSION}

    # ---- lookups
    def helpers(self) -> dict[str, Callable]:
        return {e.name: e.fn for e in self.entries() if e.kind == "helper"}

    def tools(self, agent_only: bool = False) -> list[Entry]:
        return [e for e in self.entries() if e.kind == "tool" and (e.agent or not agent_only)]

    def tool(self, name: str) -> Entry:
        """`name` is a tool's name, or module.name."""
        es = self.tools()
        for e in es:
            if e.id == name:
                return e
        hits = [e for e in es if e.name == name]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise ExtError(f"no extension tool {name!r}; tools: {', '.join(e.id for e in es) or 'none'}")
        raise ExtError(f"{name!r} is ambiguous: {', '.join(e.id for e in hits)}")

    def panels(self) -> list[Entry]:
        return [e for e in self.entries() if e.kind == "panel"]

    def panel(self, pid: str) -> Entry:
        for e in self.panels():
            if e.id == pid or e.name == pid or e.title == pid:
                return e
        raise ExtError(f"no panel {pid!r}; panels: {', '.join(e.id for e in self.panels()) or 'none'}")

    def hooks(self) -> list[Entry]:
        return [e for e in self.entries() if e.kind == "hook"]

    # ---- calls (synchronous; the host runs them in a thread and applies the result)
    def call(self, name: str, ctx: Ctx, args: dict[str, Any] | None = None) -> Any:
        e = self.tool(name)
        return _invoke(e, ctx, args or {})

    def render(self, pid: str, ctx: Ctx) -> dict[str, Any]:
        e = self.panel(pid)
        out = e.fn(ctx)
        if not isinstance(out, dict) or out.get("type") != "panel":
            raise ExtError(f"panel {e.id} must return ui.panel(...), got {type(out).__name__}")
        out.setdefault("title", e.title)
        validate_ui(out)
        return out

    def run_hooks(self, ctx: Ctx) -> list[str]:
        warns: list[str] = []
        for e in self.hooks():
            try:
                r = e.fn(ctx)
            except Exception as ex:  # noqa: BLE001
                warns.append(f"{e.id}: hook failed: {type(ex).__name__}: {ex}")
                continue
            if isinstance(r, str) and r.strip():
                warns.append(f"{e.name}: {r.strip()}")
            elif isinstance(r, (list, tuple)):
                warns.extend(f"{e.name}: {w}" for w in r if str(w).strip())
        return warns

    # ---- writing
    def read(self, module: str) -> str:
        for p, _ in self._files():
            if p.stem == module:
                return p.read_text()
        raise ExtError(f"no extension {module!r}")

    def save(self, module: str, code: str) -> dict[str, Any]:
        """Write <workspace>/extensions/<module>.py after checking it loads and registers something."""
        if not self.ws:
            raise ExtError("no workspace")
        module = re.sub(r"[^a-z0-9_]+", "_", module.lower()).strip("_")[:40]
        if not module or module[0].isdigit():
            raise ExtError("module: a short lowercase name such as hole_report")
        if BUILTIN_DIR.exists() and (BUILTIN_DIR / f"{module}.py").exists():
            raise ExtError(f"{module} is a built-in extension; pick another name")
        self.ws.mkdir(parents=True, exist_ok=True)
        tmp = self.ws / f".check_{module}.py"
        tmp.write_text(code)
        try:
            loaded = _exec_file(tmp, module, "workspace")
        finally:
            tmp.unlink(missing_ok=True)
        if any(e.kind == "broken" for e in loaded):
            raise ExtError("the file does not load: " + loaded[0].error)
        if not loaded:
            raise ExtError("the module registered nothing: decorate functions with @helper, @tool(...), @panel(...) or @on_build")
        others = [e for e in self.entries(include_disabled=True) if e.module != module]
        clash = sorted({e.name for e in loaded if e.kind in ("helper", "tool")} & {e.name for e in others if e.kind in ("helper", "tool")})
        if clash:
            raise ExtError(f"these names are taken by other extensions: {clash}")
        import cad_kernel as ck
        reserved = set(ck.script_namespace()) | {"result", "kit"}
        bad = sorted(e.name for e in loaded if e.kind == "helper" and e.name in reserved)
        if bad:
            raise ExtError(f"helper names shadow the script namespace: {bad}")
        (self.ws / f"{module}.py").write_text(code)
        self._cache = None
        self.set_enabled(module, True)
        return {"module": module, "helpers": [e.name for e in loaded if e.kind == "helper"], "tools": [e.name for e in loaded if e.kind == "tool"],
                "panels": [e.title for e in loaded if e.kind == "panel"], "hooks": [e.name for e in loaded if e.kind == "hook"]}

    def delete(self, module: str) -> None:
        if not self.ws:
            raise ExtError("no workspace")
        p = self.ws / f"{module}.py"
        if not p.exists():
            raise ExtError(f"no workspace extension {module!r} (built-in ones can be disabled, not deleted)")
        p.unlink()
        self._cache = None

    def describe(self) -> str:
        """Compact listing for the agent."""
        lines = []
        for m in self.modules():
            flag = "" if m["enabled"] else " (disabled)"
            if m["error"]:
                lines.append(f"- {m['id']} [{m['layer']}]{flag}: FAILED TO LOAD: {m['error']}")
                continue
            parts = []
            if m["helpers"]:
                parts.append("helpers " + ", ".join(m["helpers"]))
            if m["tools"]:
                parts.append("tools " + ", ".join(m["tools"]))
            if m["panels"]:
                parts.append("panels " + ", ".join(p["title"] for p in m["panels"]))
            if m["hooks"]:
                parts.append("on_build " + ", ".join(m["hooks"]))
            lines.append(f"- {m['id']} [{m['layer']}]{flag}: " + "; ".join(parts))
        return "\n".join(lines) or "(no extensions yet)"


def _invoke(e: Entry, ctx: Ctx, args: dict[str, Any]) -> Any:
    sig = inspect.signature(e.fn)
    names = list(sig.parameters)
    accepts_kw = any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values())
    kw = {k: v for k, v in args.items() if accepts_kw or k in names[1:]}
    return e.fn(ctx, **kw)


_CTL = {"number", "slider", "text_input", "select", "checkbox", "picker"}
_TYPES = _CTL | {"panel", "section", "row", "text", "kv", "badge", "table", "button", "image"}


def validate_ui(node: dict[str, Any], ids: set[str] | None = None) -> None:
    ids = set() if ids is None else ids
    if not isinstance(node, dict) or node.get("type") not in _TYPES:
        raise ExtError(f"ui: unknown node {str(node)[:80]}")
    if node["type"] in _CTL:
        if not isinstance(node.get("id"), str) or not node["id"]:
            raise ExtError(f"ui: {node['type']} needs an id")
        if node["id"] in ids:
            raise ExtError(f"ui: duplicate control id {node['id']!r}")
        ids.add(node["id"])
    for c in node.get("children", []) or []:
        validate_ui(c, ids)
    json.dumps(node)   # must be serialisable


_EXTS: dict[str, Extensions] = {}


def for_workspace(workspace: Path | None) -> Extensions:
    key = str(workspace) if workspace else ""
    if key not in _EXTS:
        _EXTS[key] = Extensions(workspace)
    return _EXTS[key]
