"""AgenticCAD server: static UI + WebSocket bridge between browser and the CAD agent, plus design file API."""
from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import cad_kernel as ck
import cam_kernel as cam
import extensions as ext_mod
import machine_link
import phone_push
import script_edit
import slicer
from agent import CadAgent, claude_cli_candidates, find_claude_cli
from version import __version__

import contextvars
import os
import shutil
import sys
import urllib.parse

ROOT = Path(__file__).parent
WORKSPACE = Path(os.environ.get("AGENTICCAD_WORKSPACE") or (ROOT / "workspace"))   # override for tests
for sub in ("exports", "history", "designs"):
    (WORKSPACE / sub).mkdir(parents=True, exist_ok=True)


# Multiple open designs (tabs). Every HTTP request and WebSocket belongs to one design: the X-Doc header, a `doc`
# query parameter, or the page that made the request (its Referer carries ?doc=). `agent` below is a proxy to that
# design's CadAgent, so the endpoints read as before. Events an agent emits go to its design's pages only; machine,
# library and extension events go to every page.
_doc_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("agenticcad_doc", default=None)
GLOBAL_EVENTS = {"machine_link", "library", "library_parts", "extensions", "remote"}
SHELL = "shell"                                   # the tab strip page (static/tabs.html)


def _program_with_moves(program: dict[str, Any], blob: bytes) -> dict[str, Any]:
    """A moves=False program payload with its moves put back as JSON lists (from the binary frame)."""
    import cam_kernel as cam
    moves = cam.Program.moves_from_blob(blob)
    return {**program, "moves_blob": False, "ops": [{**op, "moves": mv} for op, mv in zip(program["ops"], moves)]}


def _cam_event(a) -> dict[str, Any]:
    """The CAM program a page gets when it connects (or the design is loaded)."""
    ev: dict[str, Any] = {"type": "cam", "code": a.cam_code, "source": "load", "program": None, "summary": "", "gcode_lines": _gcode_lines(a.program)}
    if a.program:
        ev.update(a._program_event())
        ev["summary"] = a.program.summary()
    return ev


class Bus:
    """Fan-out to browsers plus request/response for browser-rendered screenshots."""

    def __init__(self) -> None:
        self.clients: dict[WebSocket, str] = {}   # page -> the design it shows (SHELL for the tab strip)
        self.binary: set[WebSocket] = set()       # pages that take a program's moves as a binary frame (ws?bin=1)
        self.primary: dict[str, WebSocket] = {}   # per design: the page the user last chatted from
        self.pending: dict[str, asyncio.Future] = {}

    def _targets(self, event: dict[str, Any], doc: str | None) -> list[WebSocket]:
        if event.get("type") in GLOBAL_EVENTS or doc is None:
            return list(self.clients)
        return [ws for ws, d in self.clients.items() if d == doc]

    async def _send(self, targets: list[WebSocket], event: dict[str, Any]) -> None:
        """The event as JSON to every target. A CAM program's moves ("_moves", bytes from Program.moves_blob) go as one
        binary frame right before the event to pages that asked for binary frames, and as the JSON lists of old to the
        others (tests, pages of earlier versions): 3 MB of float32 instead of 6.5 MB of JSON for the latch job."""
        blob = event.get("_moves")
        lean = {k: v for k, v in event.items() if k != "_moves"} if blob is not None else event
        data = json.dumps(lean)
        text = data if blob is None else None
        dead = []
        for ws in targets:
            try:
                if blob is not None and ws in self.binary:
                    await ws.send_bytes(blob)
                    await ws.send_text(data)
                else:
                    if text is None:
                        text = json.dumps({**lean, "program": _program_with_moves(lean["program"], blob)})
                    await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.pop(ws, None); self.binary.discard(ws)

    async def emit(self, event: dict[str, Any]) -> None:
        """To the pages of the design this request / task belongs to (all pages for machine and library events)."""
        await self._send(self._targets(event, _doc_var.get()), event)

    async def emit_doc(self, doc: str, event: dict[str, Any]) -> None:
        await self._send(self._targets(event, doc), event)

    async def emit_all(self, event: dict[str, Any]) -> None:
        await self._send(list(self.clients), event)

    async def send(self, ws: WebSocket, event: dict[str, Any]) -> None:
        await self._send([ws], event)

    async def screenshot(self, spec: dict[str, Any], doc: str | None = None, timeout: float = 20.0) -> str:
        doc = doc or _doc_var.get()
        mine = [ws for ws, d in self.clients.items() if d == doc] if doc else [ws for ws, d in self.clients.items() if d != SHELL]
        if not mine:
            raise RuntimeError("no page of this design is open to render the view")
        rid = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        req = json.dumps({"type": "screenshot_request", "id": rid, **spec})
        try:
            # Prefer the page the user chats from; other pages of the design may answer with an empty render.
            p = self.primary.get(doc) if doc else None
            targets = [p] if p in mine else mine
            for ws in targets:
                try:
                    await ws.send_text(req)
                except Exception:
                    pass
            try:
                return await asyncio.wait_for(fut, timeout / 2)
            except asyncio.TimeoutError:
                if len(targets) == len(mine):
                    raise
                await self._send(mine, req)
                return await asyncio.wait_for(fut, timeout / 2)
        finally:
            self.pending.pop(rid, None)

    def resolve(self, rid: str, png_data_url: str) -> None:
        fut = self.pending.get(rid)
        b64 = png_data_url.split(",", 1)[-1] if png_data_url else ""
        if fut and not fut.done() and b64:   # ignore empty answers; another tab may still deliver
            try:  # keep a copy of what the agent sees (debugging / audit)
                shots = WORKSPACE / "screenshots"
                shots.mkdir(exist_ok=True)
                (shots / f"{int(time.time() * 1000)}.jpg").write_bytes(base64.b64decode(b64))
            except Exception as e:  # noqa: BLE001
                print("screenshot save failed:", e)
            fut.set_result(b64)


bus = Bus()

DOCS: dict[str, CadAgent] = {}                    # open designs (tabs), in tab order
OPEN_FILE = WORKSPACE / "open_docs.json"
_active: dict[str, str | None] = {"doc": None}    # the tab the user is looking at (the shell tells us)


def _doc_dir(doc_id: str) -> Path:
    """d1 keeps the workspace's own model.py / cam.py (a single-design workspace opens as one tab); others get docs/<id>."""
    return WORKSPACE if doc_id == "d1" else WORKSPACE / "docs" / doc_id


def _make_doc(doc_id: str) -> CadAgent:
    a = CadAgent(WORKSPACE, lambda ev, _i=doc_id: bus.emit_doc(_i, ev), lambda spec, _i=doc_id: bus.screenshot(spec, _i),
                 doc_dir=_doc_dir(doc_id))
    a.doc_id = doc_id
    if DOCS:                                      # one tool / machine / part library and one set of extensions for all
        first = next(iter(DOCS.values()))
        a.library, a.parts, a.extensions = first.library, first.parts, first.extensions
        ck.LIBRARY = first.parts
    if "machine" in globals():
        a.machine, a.camera = machine, camera
    DOCS[doc_id] = a
    return a


def _save_open() -> None:
    OPEN_FILE.write_text(json.dumps({"docs": list(DOCS)}))


def _default_doc() -> str:
    """For a request that names no design (the phone page, background work): the design whose program is on the
    machine, else the tab in front, else the first."""
    m = globals().get("machine")
    owner = (((m.job or {}).get("owner") or {}).get("doc") if m is not None and m.job_active else None)
    for d in (owner, _active["doc"]):
        if d in DOCS:
            return d
    return next(iter(DOCS))


def _cur_agent() -> CadAgent:
    d = _doc_var.get()
    return DOCS[d] if d in DOCS else DOCS[_default_doc()]


class _AgentProxy:
    """`agent` in the endpoints: the CadAgent of the design the request belongs to."""

    def __getattr__(self, name):
        return getattr(_cur_agent(), name)

    def __setattr__(self, name, value):
        setattr(_cur_agent(), name, value)


try:
    _ids = [i for i in json.loads(OPEN_FILE.read_text()).get("docs", []) if isinstance(i, str) and i.startswith("d")]
except (OSError, ValueError):
    _ids = []
for _i in (_ids or ["d1"]):
    _make_doc(_i)
agent = _AgentProxy()
machine = machine_link.MachineControl(bus.emit_all)    # one machine for every design; its events go to every page
machine.runs_dir = WORKSPACE / "runs"                  # every program sent is recorded there (run_recorder)
_live = machine_link.makera_live_config()               # this machine's calibrated anchor, if MakeraStudio has fetched it
if "coordinate.anchor1_x" in _live and "coordinate.anchor1_y" in _live:
    machine.profile.anchor1 = (_live["coordinate.anchor1_x"], _live["coordinate.anchor1_y"])
if "coordinate.clearance_x" in _live and "coordinate.clearance_y" in _live:
    machine.profile.change_xy = (_live["coordinate.clearance_x"], _live["coordinate.clearance_y"])
if all(k in _live for k in machine_link.ROTATION_KEYS):    # the rotary module's reference pad / centreline offsets
    machine.profile.rotation_offset = {k[-1]: float(_live[k]) for k in machine_link.ROTATION_KEYS} | {"source": "live config"}
camera = machine_link.CameraRelay()
for _a in DOCS.values():
    _a.machine, _a.camera = machine, camera


def _rotary_machine() -> "cam.Machine | None":
    """The machine record a 4th-axis zeroing belongs to: the CAM program's machine if it has a rotary, else the first
    library machine on the Makera post with one."""
    m = agent.program.setup.machine if agent.program is not None else None
    if m is not None and m.rotary:
        return m
    return next((x for x in agent.library.machines().values() if x.rotary and x.post == "makera"), None)


def _sync_machine_record() -> None:
    """Keep MachineControl's idea of the machine record (its `rotary`, `model.rotary`) in step with the CAM program."""
    try:
        m = agent.program.setup.machine if agent.program is not None else None
        machine.set_machine_record(dataclasses.asdict(m) if m is not None else None)
    except Exception:  # noqa: BLE001
        machine.set_machine_record(None)


async def _rotary_found(found: dict) -> None:
    """The 4th-axis Z probe found the chuck centreline: write it into the saved machine record (model.rotary.axis_my /
    axis_mz, machine coordinates with the wired probe) so the machine model and the simulation can place the module."""
    m = _rotary_machine()
    if m is None:
        machine._note("axis position found but no machine record has a 4th axis to save it to (Settings ▸ Machines)")
        return
    rec = dataclasses.asdict(m)
    model = dict(rec.get("model") or {})
    model["rotary"] = {**(model.get("rotary") or {}), **{k: found[k] for k in ("axis_my", "axis_mz", "pad_mz", "bed_mz") if found.get(k) is not None},
                       "found": time.strftime("%Y-%m-%d %H:%M"), "probed": time.strftime("%Y-%m-%d")}
    rec["model"] = model
    agent.library.save_machine(rec)
    machine._note(f"axis position saved to machine '{m.name}' (model.rotary: Y {found.get('axis_my')}, Z {found.get('axis_mz')})")
    _sync_machine_record()
    await bus.emit({"type": "library", **agent.library_payload()})


machine.on_rotary_found = _rotary_found


_loader: asyncio.Task | None = None


_boot: dict[str, asyncio.Task] = {}


async def _doc_boot(doc_id: str, open_name: str | None = None, code: str | None = None) -> None:
    """Build one design (in the background: the page shows a splash with progress), open a saved design or import code
    into it, then start its own Claude session."""
    _doc_var.set(doc_id)
    a = DOCS[doc_id]
    loop = asyncio.get_running_loop()
    a.on_startup = lambda st, _i=doc_id: loop.call_soon_threadsafe(lambda: asyncio.ensure_future(bus.emit_doc(_i, {"type": "startup", **st})))
    await _load_initial()
    try:
        if open_name:
            await a.open_design(open_name)
        elif code is not None:
            await a.new_design(code)
    except Exception as e:  # noqa: BLE001
        await bus.emit({"type": "error", "text": f"could not open {open_name or 'the design'}: {e}"})
    if os.environ.get("AGENTICCAD_NO_AGENT") != "1" and (doc_id == _active["doc"] or len(DOCS) == 1 or _active["doc"] is None and doc_id == next(iter(DOCS))):
        await _connect_agent()                             # the tab in front starts its assistant at once; others when shown


MAX_LIVE = int(os.environ.get("AGENTICCAD_MAX_ASSISTANTS") or 4)


async def _wake(doc_id: str) -> None:
    """The user looks at this tab: make sure its assistant runs; above MAX_LIVE, stop the idle assistant that was
    looked at longest ago (its conversation is kept in the design's history and the session resumes when needed)."""
    a = DOCS.get(doc_id)
    if a is None or os.environ.get("AGENTICCAD_NO_AGENT") == "1":
        return
    a.last_seen = time.time()
    live = [i for i, x in DOCS.items() if x.client is not None]
    if a.client is None:
        while len(live) >= MAX_LIVE:
            idle = sorted((i for i in live if i != doc_id and not DOCS[i].busy and not DOCS[i].pending_machine_requests()),
                          key=lambda i: getattr(DOCS[i], "last_seen", 0))
            if not idle:
                break
            try:
                await DOCS[idle[0]].stop()
            except Exception:  # noqa: BLE001
                pass
            live.remove(idle[0])
        if doc_id in _boot and not _boot[doc_id].done():
            return                                         # still building: its boot starts the assistant
        tok = _doc_var.set(doc_id)
        try:
            await _connect_agent()
        finally:
            _doc_var.reset(tok)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Every open design is built in the background so the window can show a splash with progress at once (a big design
    # with real threads takes minutes). Clients get `startup` events, then the model when it lands.
    global _loader
    for i in list(DOCS):
        _boot[i] = asyncio.create_task(_doc_boot(i))
    _loader = _boot[next(iter(DOCS))]
    if os.environ.get("AGENTICCAD_STARTUP_SYNC") == "1":  # tests: the model exists before the first request
        await asyncio.gather(*_boot.values())
        await asyncio.sleep(0.05)                          # and its progress / model broadcasts have gone out (to nobody) too,
        #                                                    not into the first client's preamble as a stray `model`
    task = None
    watcher = asyncio.create_task(_watch_extensions())
    dreamer = asyncio.create_task(_dream_loop()) if os.environ.get("AGENTICCAD_NO_AGENT") != "1" else None
    if os.environ.get("AGENTICCAD_NO_REMOTE") != "1" and _remote_cfg().get("enabled"):
        await _remote_start()
    yield
    await _remote_stop()
    if task:
        task.cancel()
    watcher.cancel()
    if dreamer:
        dreamer.cancel()
    await machine.disconnect()
    camera.set_host(None)
    for a in list(DOCS.values()):
        try:
            await a.stop()
        except Exception:  # noqa: BLE001
            pass


async def _load_initial() -> None:
    try:
        await asyncio.to_thread(agent.load_initial)
    except Exception as e:  # noqa: BLE001
        await bus.emit({"type": "error", "text": f"could not build the last design at startup: {e}"})
    await bus.emit({"type": "startup", **agent.startup_payload()})
    if agent.model:
        await bus.emit(_model_event())
        await bus.emit(agent.design_state())
        await bus.emit(await asyncio.to_thread(_cam_event, agent))
        agent.start_real_threads()                       # the draft showed at once; the real threads (then the program) follow


async def _watch_extensions() -> None:
    """Hot reload: when an extension file changes (the agent saved one, or you edited it), browsers get the new list."""
    last = None
    while True:
        try:
            st = agent.extensions.stamp()
            if last is not None and st != last:
                await bus.emit(agent.extensions.payload())
            last = st
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(1.5)


_auth_lock = asyncio.Lock()
_auth_cache: dict[str, Any] = {}


async def _connect_agent() -> None:
    """Start the Claude session of the design this task belongs to (after its model is built)."""
    if find_claude_cli() is None:
        await bus.emit({"type": "agent_missing_cli"})
        return
    async with _auth_lock:                                  # one sign-in check for every tab (a good answer holds 10 min)
        if _auth_cache.get("ok") and time.time() - _auth_cache.get("t", 0) < 600:
            auth = _auth_cache["auth"]
        else:
            auth = await asyncio.to_thread(agent.auth_status)
            _auth_cache.update(ok=bool(auth.get("logged_in")), t=time.time(), auth=auth)
    if not auth.get("logged_in"):
        agent.auth_problem = "Claude Code is installed but not signed in."
        await bus.emit({"type": "agent_auth_required", "text": agent.auth_problem})
        return
    try:
        await agent.start()
        await bus.emit({"type": "agent_ready"})
    except Exception as e:  # noqa: BLE001
        await bus.emit({"type": "error", "text": f"agent failed to start: {e}"})


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


def _doc_of_request(headers, query) -> str | None:
    d = headers.get("x-doc") or query.get("doc")
    if not d:
        ref = headers.get("referer") or ""
        if "doc=" in ref:
            d = (urllib.parse.parse_qs(urllib.parse.urlsplit(ref).query).get("doc") or [None])[0]
    return d if d in DOCS else None


@app.middleware("http")
async def _doc_scope(request, call_next):
    if request.method != "GET":                            # the user did something (status polling doesn't count)
        _activity["t"] = time.time()
    tok = _doc_var.set(_doc_of_request(request.headers, request.query_params))
    try:
        return await call_next(request)
    finally:
        _doc_var.reset(tok)


@app.get("/tabs")
async def tabs_page() -> HTMLResponse:
    """The tab strip: one page per open design, plus the machine."""
    return HTMLResponse((ROOT / "static" / "tabs.html").read_text())


_gcode_n: dict[int, tuple] = {}


def _gcode_lines(p) -> int:
    """G-code line count of a program, worked out once per program (posting 36,000 lines takes seconds, and every page
    of every tab that connects asks)."""
    if p is None:
        return 0
    if getattr(p, "gcode_lines", None) is not None and not _gcode_n.get(id(p)):
        _gcode_n[id(p)] = (p, p.gcode_lines)              # counted when it was built
    hit = _gcode_n.get(id(p))
    if hit is None or hit[0] is not p:
        if len(_gcode_n) > 64:
            _gcode_n.clear()
        hit = (p, p.gcode().count("\n"))
        _gcode_n[id(p)] = hit
    return hit[1]


_prog_info: dict[int, dict] = {}                   # id(program) -> its summary (a program's time takes a while to add up)


def _doc_info(doc_id: str, a: CadAgent) -> dict[str, Any]:
    p = a.program
    prog = None
    if p is not None:
        prog = _prog_info.get(id(p))
        if prog is None or prog.get("_p") is not p:
            try:
                prog = {"name": p.name, "ops": len(p.ops), "setups": [st.name for st in p.setups], "minutes": round(p.time_minutes(), 1)}
            except Exception:  # noqa: BLE001
                prog = {"name": p.name, "ops": len(p.ops), "setups": [], "minutes": None}
            prog["_p"] = p
            if len(_prog_info) > 64:
                _prog_info.clear()
            _prog_info[id(p)] = prog
        prog = {k: v for k, v in prog.items() if k != "_p"}
    return {"id": doc_id, "name": a.design_name, "dirty": bool(a.model) and a.dirty, "busy": a.busy,
            "ready": bool(a.startup.get("done")), "program": prog, "pending": len(a.pending_machine_requests())}


@app.get("/api/docs")
async def docs_list():
    owner = ((machine.job or {}).get("owner") or {}) if machine.job_active else {}
    return {"docs": [_doc_info(i, a) for i, a in DOCS.items()], "machine_doc": owner.get("doc"),
            "machine_doc_name": owner.get("name"), "active": _active["doc"]}


class DocBody(BaseModel):
    id: str | None = None
    name: str | None = None        # a saved design to open in a new tab (an open tab with it is reused)
    code: str | None = None        # or a design's code (Import .py into a new tab)


@app.post("/api/docs/new")
async def docs_new(body: DocBody):
    if body.name:
        for i, a in DOCS.items():
            if body.name in (a.design_name, getattr(a, "opening", None)):     # open already, or still opening
                return {"id": i, "existing": True}
        if not (WORKSPACE / "designs" / f"{body.name}.py").exists():
            return JSONResponse({"error": f"no saved design named {body.name}"}, status_code=404)
    n = 1
    while f"d{n}" in DOCS:
        n += 1
    doc_id = f"d{n}"
    d = _doc_dir(doc_id)
    if d != WORKSPACE and d.exists():
        shutil.rmtree(d, ignore_errors=True)       # a closed tab's leftovers
    d.mkdir(parents=True, exist_ok=True)
    # the tab starts from the saved design (its splash says so, its CAM program is rebuilt as part of the start)
    # rather than building an empty design first
    if body.name:
        src = WORKSPACE / "designs"
        shutil.copy2(src / f"{body.name}.py", d / "model.py")
        if (src / f"{body.name}.cam.py").exists():
            shutil.copy2(src / f"{body.name}.cam.py", d / "cam.py")
        (d / "state.json").write_text(json.dumps({"design": body.name, "config": None}))
    elif body.code is not None:
        (d / "model.py").write_text(body.code)
    _make_doc(doc_id).opening = body.name
    _save_open()
    _boot[doc_id] = asyncio.create_task(_doc_boot(doc_id))
    return {"id": doc_id}


@app.post("/api/docs/close")
async def docs_close(body: DocBody):
    doc_id = body.id or ""
    if doc_id not in DOCS:
        return JSONResponse({"error": "no such tab"}, status_code=404)
    owner = ((machine.job or {}).get("owner") or {}) if machine.job_active else {}
    if owner.get("doc") == doc_id:
        return JSONResponse({"error": "its program is running on the machine: close it when the program has ended"}, status_code=409)
    if len(DOCS) == 1:
        return JSONResponse({"error": "the last tab stays open (File ▸ New starts a fresh design in it)"}, status_code=409)
    a = DOCS.pop(doc_id)
    t = _boot.pop(doc_id, None)
    if t is not None and not t.done():
        t.cancel()
    try:
        await a.stop()
    except Exception:  # noqa: BLE001
        pass
    d = _doc_dir(doc_id)
    if d != WORKSPACE:
        shutil.rmtree(d, ignore_errors=True)
    if _active["doc"] == doc_id:
        _active["doc"] = None
    _save_open()
    return {"ok": True}


if os.environ.get("AGENTICCAD_TEST_HOOKS") == "1":         # development only: deliver an event to one design's pages
    class _EmitBody(BaseModel):
        doc: str
        event: dict

    @app.post("/api/_test/emit")
    async def _test_emit(body: _EmitBody):
        await bus.emit_doc(body.doc, body.event)
        return {"ok": True}


@app.post("/api/docs/active")
async def docs_active(body: DocBody):
    _active["doc"] = body.id if body.id in DOCS else None
    if _active["doc"]:
        asyncio.create_task(_wake(_active["doc"]))
    return {"ok": True}


@app.get("/")
async def index() -> HTMLResponse:
    return HTMLResponse((ROOT / "static" / "index.html").read_text())


@app.get("/setup")
async def setup_page() -> HTMLResponse:
    """First-run page when Claude Code is not installed (the desktop app does not bundle it)."""
    return HTMLResponse((ROOT / "static" / "setup.html").read_text())


@app.get("/api/agent/cli")
async def agent_cli():
    """Where Claude Code was found (or not), so the setup page can poll while the user installs it."""
    path = find_claude_cli()
    auth = await asyncio.to_thread(agent.auth_status) if path else {"logged_in": False, "auth_method": "none", "error": None}
    return {"found": path is not None, "path": path, "connected": agent.client is not None,
            "logged_in": auth["logged_in"], "auth_method": auth["auth_method"], "auth_error": auth.get("error"),
            "api_key_set": bool(agent.settings.get("api_key")),
            "packaged": os.environ.get("AGENTICCAD_PACKAGED") == "1", "workspace": str(WORKSPACE),
            "platform": sys.platform, "searched": claude_cli_candidates()}


def _model_event(source: str = "load") -> dict[str, Any]:
    m = agent.model
    return {"type": "model", "mesh": m.mesh, "code": m.code, "summary": m.summary(6), "source": source,
            "history_len": len(agent._history_files())}


# ---------------------------------------------------------------- exports
@app.get("/api/export/{fmt}")
async def export(fmt: str, tol: float = 0.01, ang: float = 0.05, body: str | None = None):
    """STEP: exact. STL: tessellated at ?tol= (mm chordal) and ?ang= (rad) deviation. ?body= for one body."""
    if agent.model is None:
        return JSONResponse({"error": "no model"}, status_code=400)
    try:
        paths = await asyncio.to_thread(ck.export, agent.model, WORKSPACE / "exports",
                                        agent.design_name or "model", [fmt], tol, ang, body)
    except ck.CadError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return FileResponse(paths[0], filename=paths[0].name)


# ---------------------------------------------------------------- CAM
class MachineBody(BaseModel):
    machine: dict


class ToolBody(BaseModel):
    tool: dict


@app.get("/api/cam/library")
async def cam_library():
    return agent.library_payload()


@app.post("/api/cam/machine")
async def cam_machine(body: MachineBody):
    try:
        m = agent.library.save_machine(body.machine)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=400)
    await bus.emit({"type": "library", **agent.library_payload()})
    return {"name": m.name}


@app.delete("/api/cam/machine/{name}")
async def cam_machine_delete(name: str):
    ok = agent.library.delete_machine(name)
    if not ok:
        return JSONResponse({"error": f"no machine '{name}'"}, status_code=404)
    await bus.emit({"type": "library", **agent.library_payload()})
    return {"deleted": ok}


@app.post("/api/cam/tool")
async def cam_tool(body: ToolBody):
    try:
        t = agent.library.save_tool(body.tool)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=400)
    await bus.emit({"type": "library", **agent.library_payload()})
    return {"number": t.number}


@app.delete("/api/cam/tool/{number}")
async def cam_tool_delete(number: int):
    ok = agent.library.delete_tool(number)
    if not ok:
        return JSONResponse({"error": f"no tool T{number}"}, status_code=404)
    await bus.emit({"type": "library", **agent.library_payload()})
    return {"deleted": ok}


@app.get("/api/cam/materials")
async def cam_materials():
    return {"materials": list(cam.MATERIALS), "aliases": cam.MATERIAL_ALIASES}


@app.get("/api/cam/feeds")
async def cam_feeds(tool: int, material: str, machine: str | None = None, aggressiveness: float = 0.5,
                    radial_engagement: float | None = None, depth: float | None = None):
    tm = agent.library.tool_map()
    t = tm.get(tool)
    if t is None:
        return JSONResponse({"error": "unknown tool"}, status_code=400)
    m = agent.library.machines().get(machine or "")
    try:
        return cam.feeds(t, material, m, aggressiveness, radial_engagement, depth)
    except cam.CamError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


class FeedsBody(BaseModel):
    tool: dict                            # a tool's fields (need not be saved in the library)
    material: str
    machine: str | None = None
    aggressiveness: float = 0.5
    radial_engagement: float | None = None
    depth: float | None = None            # the cut depth (sizes V / ball tools by their width there)


@app.post("/api/cam/feeds")
async def cam_feeds_for(body: FeedsBody):
    from cam_data import _tool_from
    try:
        t = _tool_from({"number": 0, "name": "tool", **body.tool})
        m = agent.library.machines().get(body.machine or "")
        return cam.feeds(t, body.material, m, body.aggressiveness, body.radial_engagement, body.depth)
    except (cam.CamError, ValueError, TypeError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)


# ---- tool photos: a picture of each tool (or its packet), cropped in Settings ▸ Tools, shown when the machine asks for it
TOOL_PHOTOS = WORKSPACE / "tool_photos"
_PHOTO_NAME = re.compile(r"^[0-9a-f]{16}\.(jpg|png)$")


class ToolPhotoBody(BaseModel):
    data: str                             # base64 JPEG or PNG (a data: URL is fine)


@app.post("/api/cam/tool_photo")
async def cam_tool_photo_save(body: ToolPhotoBody):
    raw = body.data.split(",", 1)[1] if body.data.startswith("data:") else body.data
    try:
        img = base64.b64decode(raw, validate=True)
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "not base64"}, status_code=400)
    ext = "jpg" if img[:3] == b"\xff\xd8\xff" else "png" if img[:8] == b"\x89PNG\r\n\x1a\n" else None
    if ext is None or len(img) > 4_000_000:
        return JSONResponse({"error": "a JPEG or PNG under 4 MB"}, status_code=400)
    import hashlib
    name = hashlib.sha1(img).hexdigest()[:16] + "." + ext
    TOOL_PHOTOS.mkdir(parents=True, exist_ok=True)
    (TOOL_PHOTOS / name).write_bytes(img)
    return {"name": name}


def _tool_photo(name: str):
    if not _PHOTO_NAME.match(name or "") or not (TOOL_PHOTOS / name).is_file():
        return JSONResponse({"error": "no such photo"}, status_code=404)
    return FileResponse(TOOL_PHOTOS / name, headers={"Cache-Control": "max-age=31536000, immutable"})


@app.get("/api/cam/tool_photo/{name}")
async def cam_tool_photo(name: str):
    return _tool_photo(name)


class ToolShapeBody(BaseModel):
    tool: dict


@app.post("/api/cam/tool_shape")
async def cam_tool_shape(body: ToolShapeBody):
    """A tool's profile for the tool editor's drawing (or why the definition is impossible)."""
    from cam_data import _tool_from
    try:
        t = _tool_from({"number": 0, "name": "tool", **body.tool})
        return {"shape": t.shape.payload()}
    except (ValueError, TypeError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/gcode")
async def gcode_download():
    if agent.program is None:
        return JSONResponse({"error": "no CAM program"}, status_code=400)
    name = (agent.design_name or "program") + ".nc"
    return PlainTextResponse(agent.program.gcode(), headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/gcode/preview")
async def gcode_preview(lines: int = 400):
    if agent.program is None:
        return PlainTextResponse("")
    g = agent.program.gcode().splitlines()
    return PlainTextResponse("\n".join(g[:lines]) + (f"\n; ... {len(g) - lines} more lines" if len(g) > lines else ""))


class InterferenceBody(BaseModel):
    bodies: list[str] | None = None       # body paths; None = all


@app.post("/api/interference")
async def interference(body: InterferenceBody):
    if agent.model is None:
        return JSONResponse({"error": "no model"}, status_code=400)
    import analysis
    try:
        res = await agent.interference(body.bodies or None)
    except ck.CadError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return res | {"summary": analysis.summary(res)}


class RenderSaveBody(BaseModel):
    name: str
    ext: str                              # png | webm | mp4
    data: str                             # base64


@app.post("/api/render/save")
async def render_save(body: RenderSaveBody):
    import base64
    import re as _re
    ext = body.ext.lower().lstrip(".")
    if ext not in ("png", "webm", "mp4", "jpg"):
        return JSONResponse({"error": "png, jpg, webm or mp4 only"}, status_code=400)
    stem = _re.sub(r"[^A-Za-z0-9_.-]+", "-", body.name).strip("-.") or "render"
    out = WORKSPACE / "exports" / "renders"
    out.mkdir(parents=True, exist_ok=True)
    f = out / f"{stem}.{ext}"
    f.write_bytes(base64.b64decode(body.data))
    return {"file": f.name, "path": str(f), "url": f"/api/render/file/{f.name}", "bytes": f.stat().st_size}


@app.get("/api/render/file/{fname}")
async def render_file(fname: str):
    f = (WORKSPACE / "exports" / "renders" / fname).resolve()
    if f.parent != (WORKSPACE / "exports" / "renders").resolve() or not f.exists():
        return JSONResponse({"error": "no such render"}, status_code=404)
    return FileResponse(f, filename=f.name)


class MotionCheckBody(BaseModel):
    steps: int = 24


@app.post("/api/motion/check")
async def motion_check(body: MotionCheckBody):
    if agent.model is None:
        return JSONResponse({"error": "no model"}, status_code=400)
    import analysis
    try:
        res = await agent.motion_check(max(2, min(body.steps, 360)))
    except ck.CadError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return res | {"summary": analysis.motion_summary(res)}


class SimBody(BaseModel):
    ops: list[int] | None = None          # 0-based op indices; None = all
    resolution: float | None = None


@app.post("/api/cam/simulate")
async def cam_simulate(body: SimBody):
    if agent.program is None:
        return JSONResponse({"error": "no CAM program"}, status_code=400)
    try:
        res = await agent.simulate(body.ops, body.resolution)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=400)
    return res.to_payload() | {"ops": agent.sim_ops}


class LimitBody(BaseModel):
    floor: float = 0.25
    margin: float = 2.5
    finishing_factor: float = 3.0


@app.post("/api/cam/limit_feeds")
async def cam_limit_feeds(body: LimitBody):
    """Slow the moves the last simulation showed removing more than their reference (Program.limit_feeds)."""
    try:
        return await agent.limit_feeds(body.floor, body.margin, body.finishing_factor)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/cam/machine_model")
async def cam_machine_model(quality: str = "normal", name: str | None = None, info: bool = False):
    """The kinematic 3D model of the program's machine (or `name`) for the viewer's Machine toggle: tessellated parts,
    materials, nodes, home/travel/nose profile and the accuracy flags of every number."""
    import machine_models
    m = agent.library.machines().get(name) if name else (agent.program.setup.machine if agent.program is not None else None)
    if m is None:
        return JSONResponse({"error": "no CAM program (or unknown machine name)"}, status_code=404)
    try:
        if info:                                                       # numbers and sources only (Settings ▸ Machines ▸ Model)
            return {"machine": (await asyncio.to_thread(machine_models.model_for, m, agent.workspace)).to_payload()}
        return await asyncio.to_thread(machine_models.cached_payload, m, quality if quality in ("draft", "normal", "fine") else "normal", agent.workspace)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/startup")
async def startup_status():
    """Startup progress for the splash screen: steps done so far, the one in progress, and `done` once the first design is built."""
    return agent.startup_payload()


@app.get("/api/cam/fixtures")
async def cam_fixtures():
    import fixture_lib
    return {"fixtures": fixture_lib.list_fixtures(agent.workspace)}


@app.get("/api/cam/fixture_code")
async def cam_fixture_code(name: str):
    try:
        return agent.fixture_code(name)
    except cam.CamError as e:
        return JSONResponse({"error": str(e)}, status_code=404)


@app.get("/api/cam/fixture_model")
async def cam_fixture_model(name: str, quality: str = "normal", params: str | None = None, stock: str | None = None):
    """A fixture's tessellated parts for the viewer, built for the given params / stock size (JSON)."""
    import fixture_lib
    try:
        pr = json.loads(params) if params else {}
        stk = json.loads(stock) if stock else None
        return await asyncio.to_thread(fixture_lib.cached_payload, name, pr, stk, quality if quality in ("draft", "normal", "fine") else "normal", agent.workspace)
    except fixture_lib.FixtureScriptError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/cam/machine_code")
async def cam_machine_code(name: str):
    """A library machine's model script (the user's own, or the built-in it uses)."""
    try:
        return agent.machine_code(name)
    except cam.CamError as e:
        return JSONResponse({"error": str(e)}, status_code=404)


@app.get("/api/cam/sim/frame/{k}")
async def cam_sim_frame(k: int):
    if agent.sim is None:
        return JSONResponse({"error": "no simulation"}, status_code=404)
    return agent.sim.frame_payload(k)


# ---------------------------------------------------------------- parameters / measure / drawings / library / imports
class ParamsBody(BaseModel):
    values: dict


class MeasureBody(BaseModel):
    faces: list[int] | None = None
    edges: list[int] | None = None
    points: list[list[float]] | None = None
    bodies: list[int] | None = None
    entities: list[dict] | None = None


class ImportBody2(BaseModel):
    name: str
    data: str            # base64 (optionally a data URL)
    mode: str = "add"    # add | new | library
    description: str = ""


class LibInsert(BaseModel):
    name: str
    params: dict | None = None
    body_name: str | None = None


class LibAdd(BaseModel):
    name: str
    body: str | None = None
    description: str = ""
    tags: list[str] = []
    thumb: str | None = None


class DrawBody(BaseModel):
    material: str = ""          # "" -> Settings ▸ Shop drawings
    density: float | None = None
    sheet: str = ""             # "" -> Settings ▸ Shop drawings
    notes: str = ""
    units: str = ""             # "" -> Settings ▸ Units


@app.get("/api/sketches")
async def sketches_get():
    return {"sketches": agent.sketch_defs()}


class ExtEnableBody(BaseModel):
    enabled: bool


@app.get("/api/extensions")
async def extensions_list():
    return agent.extensions.payload() | {"api": ext_mod.API_DOC}


@app.get("/api/extensions/{module}/code")
async def extensions_code(module: str):
    try:
        return PlainTextResponse(agent.extensions.read(module))
    except ext_mod.ExtError as e:
        return JSONResponse({"error": str(e)}, status_code=404)


@app.post("/api/extensions/{module}")
async def extensions_enable(module: str, body: ExtEnableBody):
    agent.extensions.set_enabled(module, body.enabled)
    await bus.emit(agent.extensions.payload())
    return agent.extensions.payload()


@app.delete("/api/extensions/{module}")
async def extensions_delete(module: str):
    try:
        agent.extensions.delete(module)
    except ext_mod.ExtError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    await bus.emit(agent.extensions.payload())
    return agent.extensions.payload()


@app.get("/api/params")
async def params_get():
    return {"params": agent.get_params(), "units": agent.units(), "config": agent.config, "configurations": list(agent.configurations())}


class ConfigBody(BaseModel):
    action: str                                   # activate | set_all | export
    name: str | None = None
    configurations: dict | None = None
    formats: list[str] | None = None
    names: list[str] | None = None
    tolerance: float = 0.01


@app.get("/api/configs")
async def configs_get():
    return agent.config_state()


@app.post("/api/configs")
async def configs_post(body: ConfigBody):
    try:
        if body.action == "activate":
            await agent.activate_configuration(body.name or None)
        elif body.action == "set_all":
            await agent.set_configurations(body.configurations or {})
        elif body.action == "export":
            paths = await agent.export_configurations([f.lower() for f in (body.formats or ["step"])], body.names, body.tolerance)
            return agent.config_state() | {"files": [p.name for p in paths]}
        else:
            return JSONResponse({"error": f"unknown action {body.action!r}"}, status_code=400)
    except (ck.CadError, script_edit.Unsupported, ValueError) as e:
        return JSONResponse({"error": str(e), **agent.config_state()}, status_code=400)
    return agent.config_state()


@app.post("/api/params")
async def params_set(body: ParamsBody):
    await agent.set_params(body.values)
    return {"params": agent.get_params()}


@app.post("/api/measure")
async def measure(body: MeasureBody):
    try:
        return ck.measure(agent.model, body.faces, body.points, body.bodies, body.edges, body.entities) if agent.model else {}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/import/step")
async def import_step(body: ImportBody2):
    data = body.data.split(",", 1)[-1]
    try:
        raw = base64.b64decode(data)
        res = await agent.import_step(body.name, raw, body.mode, body.description)
    except ck.CadError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=400)
    return res


@app.get("/api/library/parts")
async def library_parts():
    return {"parts": agent.parts.list()}


@app.get("/api/library/thumb/{slug}")
async def library_thumb(slug: str):
    p = agent.parts.root / slug / "thumb.jpg"
    if not p.exists():
        return JSONResponse({"error": "no thumb"}, status_code=404)
    return FileResponse(p)


@app.post("/api/library/insert")
async def library_insert(body: LibInsert):
    try:
        await agent.insert_library_part(body.name, body.params, body.body_name)
    except ck.CadError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True}


@app.post("/api/library/add")
async def library_add(body: LibAdd):
    try:
        meta = await agent.add_to_library(body.name, body.body, body.description, body.tags, body.thumb)
    except ck.CadError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return meta


@app.delete("/api/library/{slug}")
async def library_delete(slug: str):
    ok = agent.parts.delete(slug)
    if not ok:
        return JSONResponse({"error": f"no library part '{slug}'"}, status_code=404)
    await bus.emit({"type": "library_parts", "parts": agent.parts.list()})
    return {"deleted": ok}


@app.post("/api/drawings")
async def drawings(body: DrawBody):
    try:
        res = await agent.make_drawings(body.material, body.density, body.sheet, body.notes, body.units)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"files": [{"body": r["body"], "svg": Path(r["svg"]).name, "dxf": Path(r["dxf"]).name if r.get("dxf") else None} for r in res],
            "design": (agent.design_name or "untitled")}


@app.get("/api/drawings/{design}/{fname}")
async def drawing_file(design: str, fname: str):
    p = WORKSPACE / "drawings" / design / fname
    if not p.exists() or ".." in fname:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(p, media_type="image/svg+xml" if fname.endswith(".svg") else "application/dxf")


# ---------------------------------------------------------------- 3D printing (external slicer; absent when none is installed)
class SliceBody(BaseModel):
    machine: str | None = None
    process: str | None = None
    filament: str | None = None
    body: str | None = None
    walls_only: bool = False
    overrides: dict = {}


@app.get("/api/slicer")
async def slicer_info(refresh: bool = False):
    if refresh:
        slicer.find_slicer(refresh=True)
    return agent.slicer_payload()


@app.get("/api/slicer/machines")
async def slicer_machines(q: str = ""):
    info = slicer.find_slicer()
    if info is None:
        return JSONResponse({"error": "no slicer installed"}, status_code=404)
    ms = await asyncio.to_thread(slicer.machines, info)
    q = q.lower()
    return {"machines": [m for m in ms if not q or q in m["name"].lower()], "default": slicer.default_machine(info)}


@app.get("/api/slicer/profiles")
async def slicer_profiles(machine: str):
    info = slicer.find_slicer()
    if info is None:
        return JSONResponse({"error": "no slicer installed"}, status_code=404)
    try:
        mach = await asyncio.to_thread(slicer.flatten, info, "machine", machine)
        pf = await asyncio.to_thread(slicer.profiles_for, info, machine)
    except slicer.SlicerError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    return pf | {"default_process": mach.get("default_print_profile", ""), "default_filament": (mach.get("default_filament_profile") or [""])[0],
                 "bed": mach.get("printable_area"), "height": mach.get("printable_height"), "overrides": list(slicer.OVERRIDE_KEYS)}


@app.post("/api/slicer/slice")
async def slicer_slice(body: SliceBody):
    try:
        res = await agent.slice_model(body.machine, body.process, body.filament, body.overrides, body.body, body.walls_only)
    except (slicer.SlicerError, ck.CadError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return res.to_dict() | {"summary": res.summary()}


@app.get("/api/slicer/gcode")
async def slicer_gcode():
    if agent.slice_result is None:
        return JSONResponse({"error": "nothing sliced yet"}, status_code=404)
    return FileResponse(agent.slice_result.gcode, filename=(agent.design_name or "model") + ".gcode", media_type="text/plain")


@app.get("/api/slicer/3mf")
async def slicer_3mf():
    if agent.slice_result is None:
        return JSONResponse({"error": "nothing sliced yet"}, status_code=404)
    return FileResponse(agent.slice_result.three_mf, filename=(agent.design_name or "model") + ".3mf", media_type="model/3mf")


# ---------------------------------------------------------------- settings / agent
class SettingsBody(BaseModel):
    settings: dict
    restart: bool = False


# ---------------------------------------------------------------- machine control (Machine tab)
MACHINE_FILE = WORKSPACE / "machine.json"


class MachineBody(BaseModel):
    action: str
    host: str | None = None
    port: int | None = None
    cam_port: int | None = None
    axis: str | None = None
    dist: float | None = None
    on: bool | None = None
    z: float | None = None
    x: float | None = None
    y: float | None = None
    feed: float | None = None
    kind: str | None = None
    depth: float | None = None
    dia: float | None = None
    ops: list[int] | None = None
    air: float | None = None
    line: str | None = None          # console: one line as typed
    text: str | None = None          # run_file: the .nc file's contents
    name: str | None = None          # run_file: its name
    power: float | None = None       # switch: speed in % (the spindle fan)
    ack: bool | None = None          # check_ops / run_ops: the user vouches for a rotary setup's origin ("I checked it")
    p: str | None = None             # set_origin / origin_at: the work offset (G54..G59; default the active one)
    hole_dia: float | None = None    # probe_3d pocket_center: the hole's Ø (sizes the search)
    to_z: float | None = None        # probe_descend: work Z to go down to (none: find the surface below)
    hz: float | None = None          # spindle_load_poll: M957 rate


def _program_for_ops(body: MachineBody) -> tuple[dict, dict, str]:
    """The CAM program's chosen operations (one setup) as the Z1 will play them: (prepared program, info, file label).
    `info["rotary"]`: the setup is on the 4th axis (its origin must be zeroed on the axis)."""
    prep, info, label = agent.program_for_ops(body.ops or [], body.air or 0.0)
    st = next((s for s in agent.program.setups if s.name == info.get("setup")), None) if agent.program is not None else None
    info["rotary"] = bool(st is not None and st.rotary)
    return prep, info, label


@app.get("/api/runs")
async def runs_list(design: str | None = None):
    """Recorded machine runs (run_recorder), newest first, with the name this design's runs are filed under."""
    import run_recorder
    return {"runs": run_recorder.list_runs(WORKSPACE / "runs", design), "design": agent.runs_design()}


@app.get("/api/runs/analysis")
async def runs_analysis(id: str):
    """Planned vs actual for one run (run_analysis), with the assistant's suggestions for it if any."""
    import run_analysis
    import run_recorder
    d = run_recorder.run_dir(WORKSPACE / "runs", id)
    if d is None:
        return JSONResponse({"error": "no such run"}, status_code=404)
    try:
        a = await asyncio.to_thread(run_analysis.analyse, d)
    except Exception as e:  # noqa: BLE001 - a broken record shows as an error, not a crash
        return JSONResponse({"error": f"analysis failed: {type(e).__name__}: {e}"}, status_code=500)
    sug = d / "suggestions.json"
    a = {**a, "id": id, "suggestions": json.loads(sug.read_text()).get("suggestions", []) if sug.exists() else []}
    return a


@app.get("/api/runs/power")
async def runs_power(id: str):
    """ESTIMATED spindle power for one run (power.py: simulated removal rate × specific cutting energy) - not measured."""
    import power
    import run_recorder
    d = run_recorder.run_dir(WORKSPACE / "runs", id)
    if d is None:
        return JSONResponse({"error": "no such run"}, status_code=404)
    mp = agent.doc_dir / "model.py"
    try:
        return await asyncio.to_thread(power.estimate, d, WORKSPACE, mp.read_text() if mp.exists() else None, agent.config)
    except Exception as e:  # noqa: BLE001
        return {"error": f"estimate failed: {type(e).__name__}: {e}"}


@app.get("/api/machine")
async def machine_get():
    saved = json.loads(MACHINE_FILE.read_text()) if MACHINE_FILE.exists() else {}
    _sync_machine_record()
    return {**machine.payload(), "saved": saved, "camera_error": camera.error}


@app.get("/api/machine/discover")
async def machine_discover():
    try:
        found = await asyncio.to_thread(machine_link.discover, 3.0)
    except OSError as e:
        return JSONResponse({"error": f"discovery failed: {e}"}, status_code=500)
    return {"machines": list(found.values())}


@app.post("/api/machine")
async def machine_post(body: MachineBody):
    a = body.action
    _sync_machine_record()
    try:
        if a == "connect":
            if not body.host:
                raise machine_link.Refused("give the machine's address")
            res = await machine.connect(body.host.strip(), body.port or 2222)
            camera.set_host(body.host.strip(), body.cam_port or 82)
            MACHINE_FILE.write_text(json.dumps({"host": body.host.strip(), "port": body.port or 2222, "cam_port": body.cam_port or 82}))
        elif a == "disconnect":
            await machine.disconnect()
            camera.set_host(None)
            res = machine.payload()
        elif a == "arm":
            res = await machine.arm(bool(body.on))
        elif a == "jog":
            res = await machine.jog(body.axis or "", body.dist if body.dist is not None else 0.0)
        elif a == "diagnose":
            return {**machine.payload(), "diagnose": await machine.diagnose()}
        elif a == "goto_setter":
            sx, sy = machine.setter_xy
            res = await machine.goto_xy(sx, sy, body.feed or 300.0)
        elif a == "raise_z":
            res = await machine.raise_z_top(body.feed or 300.0)
        elif a == "change_tool":                         # Makera's M6 Tn (or M491 to re-measure the same tool)
            res = await machine.change_tool(int(body.z) if body.z is not None else -99)
        elif a == "confirm_tool":                        # Makera's continue (M490.2), same as the front button
            res = await machine.confirm_tool_change()
        elif a == "origin_anchor":                       # G10 L2 P0 X Y at Anchor 1 (MakeraStudio's command)
            res = await machine.set_origin_anchor()
        elif a == "auto_z_probe":                        # Makera's M495 X Y O0 F0 at work X,Y (default: here)
            res = await machine.auto_z_probe(body.x, body.y)
        elif a == "lower_to":                            # Z down only, to z mm above work zero (a 3D-probe start)
            res = await machine.lower_to(body.z if body.z is not None else float('nan'))
        elif a == "probe_3d":                            # Makera's 3D-probe routine (M480.n) from where the head is
            res = await machine.probe_3d(body.kind or "", body.x if body.x is not None else 20.0,
                                         body.y if body.y is not None else 20.0, body.depth or 2.0, body.dia or 2.0, hole_dia=body.hole_dia)
        elif a == "probe_descend":                       # G38.3 with the 3D probe: find the surface below, or go into a hole
            res = await machine.probe_descend(body.to_z, body.dist or 20.0)
        elif a == "probe_tap_test":                      # the user taps the 3D probe's stylus; the app watches its pin
            res = await machine.probe_tap_test()
        elif a == "align":                               # Align setup: probe the top + the program's register holes, adjust
            spec = await asyncio.to_thread(agent.align_spec, body.kind or None, {"x": body.x, "y": body.y} if body.x is not None and body.y is not None else None)
            r = await machine.align(spec)
            res = {**machine.payload(), "align_result": r}
        elif a == "recover":                             # back to a known state after a hold / stopped routine / alarm
            res = await machine.recover()
        elif a == "spindle_load_poll":                   # M957 drive effort while the spindle runs (recorded with runs)
            res = {**machine.payload(), "poll": machine.set_spindle_load_poll(bool(body.on), body.hz)}
        elif a == "check_ops":                            # what running these operations would do (nothing is sent)
            prep, info, label = await asyncio.to_thread(_program_for_ops, body)   # big programs: keep the loop (and status) running
            chk = machine.check_job(prep, rotary=info["rotary"], ack=bool(body.ack))
            gate = agent.sim_gate(sorted(set(body.ops or [])))
            sim_notes = (["not simulated: run Simulate on these operations first to check them"] if not gate["simulated"]
                         else [f"SIMULATION: {e}" for e in gate["errors"]] or ["simulation: no errors"])
            return {**machine.payload(), "check": {"label": machine_link.program_filename(label), "lines": prep["lines"],
                                                   "ops": prep["ops"], "tools": prep["tools"], "extents": prep["extents"],
                                                   "notes": chk["notes"] + sim_notes, "sim": gate, "uses_a": prep["uses_a"],
                                                   **{k: v for k, v in info.items() if k != "cam_code"}}}
        elif a == "run_ops":                              # upload the chosen operations to the machine and play them
            prep, info, label = await asyncio.to_thread(_program_for_ops, body)   # big programs: keep the loop (and status) running
            info["owner"] = {"doc": _doc_var.get() or _default_doc(), "name": agent.design_name}
            res = await machine.run_program(label, prep, info, rotary=info["rotary"], ack=bool(body.ack))
        elif a in ("check_file", "run_file"):              # any .nc file, through the same checks as CAM operations
            if not body.text:
                raise machine_link.Refused("no file")
            air = float(body.air or 0.0)
            if not (0.0 <= air <= 50.0):
                raise machine_link.Refused("air-cut lift: 0 .. 50 mm")
            prep = machine_link.prepare_program(body.text, [], air_cut=air)
            label = Path(body.name or "file").stem + ("_air" if air else "")
            if a == "check_file":
                chk = machine.check_job(prep, ack=bool(body.ack))      # a file with A words is a 4th-axis program
                return {**machine.payload(), "check": {"label": machine_link.program_filename(label), "lines": prep["lines"],
                                                       "ops": [], "tools": prep["tools"], "extents": prep["extents"],
                                                       "notes": chk["notes"], "uses_a": prep["uses_a"], "program": body.name or "file"}}
            res = await machine.run_program(label, prep, {"program": body.name or "file", "file": True}, ack=bool(body.ack))
        # -- the 4th axis: Makera's own commands, nothing moves X/Y/Z except the firmware's 4th-axis Z probe (watched)
        elif a == "rotary_z_probe":                        # M495 X0 Y0 O0: Z0 on the chuck centreline (wired probe T0)
            res = await machine.rotary_z_probe(body.depth)  # depth: optional Z search limit below the clearance height
        elif a == "origin_axis_y":                         # G10 L2 P0 Y at anchor1_y + rotation_offset_y (or typed machine Y)
            res = await machine.set_origin_axis_y(body.y)
        elif a == "origin_x_at":                           # G10 L2 P0 X at machine x (e.g. chuck face + offset)
            res = await machine.origin_x_at(body.x if body.x is not None else float('nan'), body.kind or "typed", body.name or "set")
        elif a == "zero_a":                                # G92.4 A0 + G10 L20 P0 A0
            res = await machine.zero_a()
        elif a == "bed_z_record":                          # the bed, touched with the same probe: exact axis height
            res = await machine.record_bed_z()
        elif a == "shrink_a":                              # G92.4 A0 S0
            res = await machine.shrink_a()
        elif a == "read_rotation_offset":                  # config-get sd coordinate.rotation_offset_* (read-only)
            res = await machine.read_rotation_offset()
        elif a == "set_origin":                            # the head's position becomes these work coordinates (G10 L20)
            res = await machine.set_origin(body.x, body.y, body.z, p=body.p)
        elif a == "origin_at":                             # X0 Y0 at machine x, y (e.g. the block corner from 3 probed corners)
            res = await machine.origin_at(body.x if body.x is not None else float('nan'), body.y if body.y is not None else float('nan'),
                                          body.kind or "typed", body.name or "set", p=body.p)
        elif a == "shift_origin":                          # move X0 Y0 by x, y mm (e.g. probed corner -> the setup's corner)
            res = await machine.shift_origin(body.x if body.x is not None else float('nan'), body.y if body.y is not None else float('nan'),
                                             body.kind or "typed", body.name or "shifted")
        elif a == "console":                               # one line as typed (blocked commands refused)
            res = await machine.console(body.line or "")
        elif a == "goto_work_xy":                          # go to work X, Y (converted with the current work offsets)
            wco = machine._wco()
            if wco is None or body.x is None or body.y is None:
                raise machine_link.Refused("need the machine's position and X, Y")
            res = await machine.goto_xy_raised(body.x + wco[0], body.y + wco[1], body.feed or 300.0)
        elif a == "cleaning_position":                      # Z to the top, then the head over the front-middle of the bed
            res = await machine.cleaning_position()
        elif a == "goto_xy_raised":                         # Z up to the top first if needed, then machine X, Y
            res = await machine.goto_xy_raised(body.x if body.x is not None else float('nan'), body.y if body.y is not None else float('nan'), body.feed or 300.0)
        elif a == "job_pause":
            res = await machine.job_pause()
        elif a == "job_resume":
            res = await machine.job_resume()
        elif a == "job_abort":
            res = await machine.job_abort()
        elif a == "goto_xy":
            res = await machine.goto_xy(body.x if body.x is not None else float('nan'), body.y if body.y is not None else float('nan'), body.feed or 300.0)
        elif a == "home":
            res = await machine.home()
        elif a == "hold":
            res = await machine.hold()
        elif a == "resume":
            res = await machine.resume()
        elif a == "halt":
            res = await machine.halt()
        elif a == "unlock":
            res = await machine.unlock()
        elif a == "switch":
            res = await machine.switch(body.axis or "", bool(body.on), body.power)
        elif a == "z_floor":
            res = machine.set_z_floor(body.z if body.z is not None else -1)
        else:
            raise machine_link.Refused(f"unknown action {a!r}")
    except machine_link.Refused as e:
        return JSONResponse({"error": str(e), **machine.payload()}, status_code=409)
    except (ConnectionError, OSError, asyncio.TimeoutError) as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}" if str(e) else type(e).__name__, **machine.payload()}, status_code=502)
    await bus.emit({"type": "machine_link", **res})
    return res


@app.get("/api/machine/camera")
async def machine_camera():
    """MJPEG stream of the machine camera (one upstream socket shared by every viewer)."""
    if not camera.host:
        return JSONResponse({"error": "not connected"}, status_code=409)

    async def gen():
        async for jpg in camera.frames():
            yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n"
    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame",
                             headers={"Cache-Control": "no-store"})


@app.get("/api/machine/snapshot")
async def machine_snapshot():
    try:
        jpg = await camera.snapshot()
    except (machine_link.Refused, asyncio.TimeoutError) as e:
        return JSONResponse({"error": str(e) or "no frame"}, status_code=409)
    return Response(jpg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- phone control (off by default)
# A second listener on the local network serving only the phone page and the machine endpoints, behind a random key
# carried in the QR code's URL. The desktop app itself stays on 127.0.0.1. Same MachineControl, same rules.
import hmac
import secrets
import socket as _socket

from fastapi import Request

REMOTE_FILE = WORKSPACE / "remote.json"
_remote: dict[str, Any] = {"server": None, "task": None, "error": None}


def _remote_cfg() -> dict:
    cfg = {"enabled": False, "port": int(os.environ.get("AGENTICCAD_PHONE_PORT") or 8790), "token": "", "require_key": True, "push": False}
    try:
        cfg.update(json.loads(REMOTE_FILE.read_text()))
    except (OSError, ValueError):
        pass
    if not cfg.get("token"):
        cfg["token"] = secrets.token_urlsafe(18)
        REMOTE_FILE.write_text(json.dumps(cfg))
    return cfg


# ---- phone notifications, in the app: the phone page over HTTPS (this computer's own CA, trusted once on the phone)
# plus Web Push from here to the phone's push service (Apple's / Google's: the only relay, carrying ciphertext).
# Certificates, keys and the phones' subscriptions live in the workspace, never in the package; upgrades reuse them.
PUSH_SUBJECT = "https://agenticcad.github.io/agenticcad/"
PUSH_TEXT = {                                             # kind -> (title, TTL s, urgency)
    "tool_change": ("Tool change: fit {what}", 3600, "high"),
    "program_done": ("Program finished", 3600, "normal"),
    "program_failed": ("Program stopped", 3600, "high"),
    "test": ("AgenticCAD notifications work", 300, "normal"),
}
_push_store: phone_push.PushStore | None = None


def _push() -> phone_push.PushStore:
    global _push_store
    if _push_store is None or _push_store.dir != WORKSPACE / "phone_push":
        _push_store = phone_push.PushStore(WORKSPACE / "phone_push")
    return _push_store


def _https_port(cfg: dict) -> int:
    return int(cfg["port"]) + 1


def _page_path(cfg: dict) -> str:
    """The phone page's path with the key in it (/m/k/<key>): a path survives iOS's Add to Home Screen, a query
    string does not, and a Home Screen app has its own storage so it cannot rely on a key saved in the browser."""
    return f"/m/k/{cfg['token']}" if cfg["require_key"] else "/m"


def _https_url(cfg: dict, host: str | None = None) -> str:
    host = host or phone_push.local_hosts(_lan_ip())[0]
    return f"https://{host}:{_https_port(cfg)}" + _page_path(cfg)


def _tls_running() -> bool:
    return _remote.get("tls") is not None and _remote.get("tls_task") is not None and not _remote["tls_task"].done()


def _push_payload(kind: str, info: dict, cfg: dict) -> tuple[dict, int, str]:
    """(what the service worker shows, TTL, urgency). Tapping opens the secure page; a tool change also carries the
    one-shot confirm URL for its 'Tool changed · continue' button (browsers that show action buttons)."""
    title, ttl, urgency = PUSH_TEXT.get(kind, (kind, 3600, "normal"))
    title = title.format(what=info.get("what") or f"T{info.get('tool')}")
    body = str(info.get("msg") or title)
    if info.get("program"):
        body = f"{info['program']}" + (f" · {info['op']}" if info.get("op") else "") + f"\n{body}"
    p = {"kind": kind, "title": title, "body": body, "tag": kind, "url": "/m"}     # the phone's own origin, key already there
    if kind == "tool_change" and info.get("nonce"):
        p["confirm_url"] = f"https://{phone_push.local_hosts(_lan_ip())[0]}:{_https_port(cfg)}/m/api/confirm/{info['nonce']}"
    return p, ttl, urgency


def _push_one(st: phone_push.PushStore, sub: dict, payload: dict, ttl: int, urgency: str, topic: str) -> dict:
    try:
        code = st.send(sub, payload, PUSH_SUBJECT, ttl=ttl, urgency=urgency, topic=topic)
        return {"endpoint": sub["endpoint"], "code": code, "ok": 200 <= code < 300, "gone": code in (404, 410)}
    except Exception as e:                                    # URLError, timeout, bad keys: reported, never raised
        return {"endpoint": sub["endpoint"], "ok": False, "error": f"{type(e).__name__}: {e}"}


async def _phone_notify(kind: str, info: dict, only: str | None = None) -> dict:
    cfg = _remote_cfg()
    res = {"kind": kind, "t": time.time(), "sent": 0, "failed": 0, "gone": 0}
    if kind != "test" and not cfg["push"]:
        return res
    st = _push()
    subs = st.subscriptions()
    if only:
        subs = [s for s in subs if s["endpoint"] == only]
    if not subs:
        res["error"] = "no phone has notifications on"
        _remote["push_last"] = res
        return res
    payload, ttl, urgency = _push_payload(kind, info, cfg)
    outs = await asyncio.gather(*(asyncio.to_thread(_push_one, st, s, payload, ttl, urgency, kind) for s in subs))
    now = time.time()
    keep = []
    for s, o in zip(subs, outs):
        if o.get("gone"):
            res["gone"] += 1                                  # the phone unsubscribed or the browser dropped it
            continue
        if o["ok"]:
            res["sent"] += 1
            s["last"] = now
        else:
            res["failed"] += 1
            res["error"] = o.get("error") or f"push service answered {o.get('code')}"
        keep.append(s)
    if only:
        keep = [x for x in st.subscriptions() if x["endpoint"] != only] + keep
    st._save(keep)
    if res["failed"] or res["gone"]:
        machine._note(f"phone notification: {res['sent']} sent, {res['failed']} failed, {res['gone']} gone" + (f" ({res['error']})" if res.get("error") else ""))
    _remote["push_last"] = res
    return res


machine.notify = _phone_notify


def _lan_ip() -> str:
    s_ = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    try:
        s_.connect(("10.255.255.255", 1))            # no packet is sent; this picks the outward interface
        return s_.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s_.close()


def _remote_status() -> dict:
    cfg = _remote_cfg()
    running = _remote["server"] is not None and _remote["task"] is not None and not _remote["task"].done()
    ip = _lan_ip()
    st = _push()
    cert = st.cert_info() if st.ca_crt.exists() and st.leaf() is not None else None
    return {"enabled": cfg["enabled"], "port": cfg["port"], "running": running, "ip": ip, "error": _remote["error"],
            "require_key": cfg["require_key"],
            "url": f"http://{ip}:{cfg['port']}" + _page_path(cfg),
            "https": {"running": _tls_running(), "port": _https_port(cfg), "url": _https_url(cfg), "ip_url": _https_url(cfg, ip),
                      "host": phone_push.local_hosts(ip)[0], "error": _remote.get("tls_error")},
            "push": {"enabled": cfg["push"], "cert": cert, "last": _remote.get("push_last"),
                     "devices": [{"label": s.get("label") or "phone", "added": s.get("added"), "last": s.get("last"), "endpoint": s["endpoint"]}
                                 for s in st.subscriptions()]}}




phone = FastAPI()


def _key_ok(request: Request) -> bool:
    cfg = _remote_cfg()
    if not cfg["require_key"]:
        return True                                       # the user's choice: anyone on the network may use it
    k = request.headers.get("x-key") or request.query_params.get("k") or ""
    return hmac.compare_digest(k.encode(), cfg["token"].encode())


@phone.middleware("http")
async def _phone_auth(request: Request, call_next):
    if (request.url.path in ("/m", "/m/", "/m/ca.crt", "/m/sw.js", "/m/api/ping") or request.url.path.startswith(("/m/static/", "/m/k/"))
            or request.url.path.startswith("/m/api/confirm/")):
        return await call_next(request)                   # the page, the public CA certificate and the service worker;
                                                          # every API call carries the key (confirm: its one-shot code)
    if not _key_ok(request):
        return JSONResponse({"error": "not authorised: scan the QR code in AgenticCAD ▸ Settings ▸ Phone control"}, status_code=401)
    return await call_next(request)


@phone.get("/m")
@phone.get("/m/k/{token}")
async def phone_page(token: str = ""):
    """The page (the key, when the link carries one, is in the path: the page reads it and keeps it)."""
    return FileResponse(ROOT / "static" / "mobile.html", headers={"Cache-Control": "no-store"})


@phone.get("/m/api/machine")
async def phone_machine_get():
    return await machine_get()


@phone.post("/m/api/machine")
async def phone_machine_post(body: MachineBody):
    return await machine_post(body)


@phone.get("/m/api/ping")
async def phone_ping(request: Request):
    """Public, answers from any origin: the plain page probes the secure address with it, and the fetch only succeeds
    once the phone trusts this computer's certificate - so the page can move itself over without being told."""
    return JSONResponse({"ok": True, "secure": request.url.scheme == "https"}, headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})


@phone.get("/m/ca.crt")
async def phone_ca_cert():
    """This computer's own CA certificate, for the phone to trust once (public: it holds no secret)."""
    return Response(_push().ca_der(), media_type="application/x-x509-ca-cert",
                    headers={"Content-Disposition": 'attachment; filename="AgenticCAD-Phone-CA.crt"', "Cache-Control": "no-store"})


@phone.get("/m/sw.js")
async def phone_service_worker():
    return FileResponse(ROOT / "static" / "mobile_sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-store", "Service-Worker-Allowed": "/m"})


@phone.get("/m/manifest.webmanifest")
async def phone_manifest():
    """The web app manifest (with the key in start_url: a Home Screen app has its own storage, so the page can't rely
    on the key it saved in the browser). iOS grants web push to Home Screen apps only."""
    cfg = _remote_cfg()
    m = {"name": "AgenticCAD Machine", "short_name": "Machine", "start_url": _page_path(cfg),
         "scope": "/m", "display": "standalone", "background_color": "#0f1115", "theme_color": "#0f1115"}
    return Response(json.dumps(m), media_type="application/manifest+json", headers={"Cache-Control": "no-store"})


class PushBody(BaseModel):
    subscription: dict | None = None
    label: str = ""
    endpoint: str | None = None


@phone.get("/m/api/push")
async def phone_push_get(request: Request):
    cfg = _remote_cfg()
    ip = _lan_ip()
    return {"enabled": cfg["push"], "vapid": _push().vapid()[1], "secure": request.url.scheme == "https", "https_running": _tls_running(),
            "https_url": _https_url(cfg), "ip_url": _https_url(cfg, ip), "count": len(_push().subscriptions())}


@phone.post("/m/api/push/subscribe")
async def phone_push_subscribe(body: PushBody):
    try:
        _push().add(body.subscription or {}, body.label)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "count": len(_push().subscriptions())}


@phone.post("/m/api/push/unsubscribe")
async def phone_push_unsubscribe(body: PushBody):
    if body.endpoint:
        _push().remove(body.endpoint)
    return {"ok": True, "count": len(_push().subscriptions())}


@phone.post("/m/api/push/test")
async def phone_push_test(body: PushBody):
    return await _phone_notify("test", {"msg": "This is how a tool change will reach you. Tap to open the Machine page."}, only=body.endpoint)


@phone.post("/m/api/confirm/{nonce}")
async def phone_confirm(nonce: str):
    """The 'Tool changed - continue' button on a phone notification. The code is minted for one tool-change wait and
    dropped when that wait ends, so it stands in for the key and an old notification cannot continue a later change."""
    try:
        return await machine.confirm_by_nonce(nonce)
    except machine_link.Refused as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except (ConnectionError, OSError, asyncio.TimeoutError) as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=502)


@phone.get("/m/api/tool_photo/{number}")
async def phone_tool_photo(number: int):
    """The photo of library tool T<number> (the phone shows it when the machine asks for that tool)."""
    t = next((x for x in agent.library.tools() if x.number == number), None)
    return _tool_photo(t.photo if t else "")


@phone.get("/m/api/camera")
async def phone_camera():
    return await machine_camera()


@phone.get("/m/api/snapshot")
async def phone_snapshot():
    return await machine_snapshot()


async def _remote_start() -> None:
    import uvicorn
    await _remote_stop()
    cfg = _remote_cfg()
    _remote["error"] = _remote["tls_error"] = None
    server = uvicorn.Server(uvicorn.Config(phone, host="0.0.0.0", port=int(cfg["port"]), log_level="warning", lifespan="off"))
    server.install_signal_handlers = lambda: None         # the main server owns the signals

    async def run():
        try:
            await server.serve()
        except (OSError, SystemExit) as e:
            _remote["error"] = f"phone control could not start on port {cfg['port']}: {e}"
    _remote["server"], _remote["task"] = server, asyncio.create_task(run())
    # the same app over HTTPS on the next port, with this computer's own certificate (made or renewed here)
    try:
        st = _push()
        st.ensure_certs(phone_push.local_hosts(_lan_ip()))
        tls = uvicorn.Server(uvicorn.Config(phone, host="0.0.0.0", port=_https_port(cfg), log_level="warning", lifespan="off",
                                            ssl_certfile=str(st.srv_crt), ssl_keyfile=str(st.srv_key)))
        tls.install_signal_handlers = lambda: None

        async def run_tls():
            try:
                await tls.serve()
            except (OSError, SystemExit) as e:
                _remote["tls_error"] = f"the secure page could not start on port {_https_port(cfg)}: {e}"
        _remote["tls"], _remote["tls_task"] = tls, asyncio.create_task(run_tls())
    except Exception as e:                                # a certificate problem never stops the plain page
        _remote["tls_error"] = f"certificate: {type(e).__name__}: {e}"
    await asyncio.sleep(0.3)


async def _remote_stop() -> None:
    for sk, tk in (("server", "task"), ("tls", "tls_task")):
        srv, task = _remote.get(sk), _remote.get(tk)
        _remote[sk] = _remote[tk] = None
        if srv is not None:
            srv.should_exit = True
        if task is not None:
            try:
                await asyncio.wait_for(task, 5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()


class RemoteBody(BaseModel):
    enabled: bool | None = None
    port: int | None = None
    new_link: bool = False
    require_key: bool | None = None
    push: bool | None = None                              # phone notifications on/off
    push_test: bool = False                               # a test notification to every phone
    push_forget: bool = False                             # drop every phone's subscription
    new_cert: bool = False                                # a new CA (phones must trust it again) and no subscriptions


@app.get("/api/remote")
async def remote_get():
    return _remote_status()


@app.post("/api/remote")
async def remote_set(body: RemoteBody):
    cfg = _remote_cfg()
    if body.port is not None:
        if not (1024 <= body.port <= 65535):
            return JSONResponse({"error": "port 1024..65535"}, status_code=400)
        cfg["port"] = body.port
    if body.new_link:
        cfg["token"] = secrets.token_urlsafe(18)          # old QR codes stop working
    if body.require_key is not None:
        cfg["require_key"] = body.require_key
    if body.enabled is not None:
        cfg["enabled"] = body.enabled
    if body.push is not None:
        cfg["push"] = bool(body.push)
    REMOTE_FILE.write_text(json.dumps(cfg))
    if body.push_forget:
        _push().forget_all()
    if body.new_cert:
        _push().new_ca()
        _push().forget_all()
    if cfg["enabled"]:
        await _remote_start()
    else:
        await _remote_stop()
    if body.push_test:
        await _phone_notify("test", {"msg": "This is how a tool change will reach you. Tap to open the Machine page."})
    return _remote_status()


@app.get("/api/version")
async def version():
    log = (ROOT / "CHANGELOG.md").read_text() if (ROOT / "CHANGELOG.md").exists() else ""
    return {"version": __version__, "changelog": log}


class LessonBody(BaseModel):
    action: str                      # add | update | retire | activate | delete
    id: str | None = None
    title: str | None = None
    rule: str | None = None
    why: str | None = None
    tags: list[str] | str | None = None
    check: str | None = None


@app.get("/api/lessons")
async def lessons_get():
    import lessons
    return {"lessons": lessons.load(WORKSPACE), "checks": list(lessons.CHECKS)}


@app.post("/api/lessons")
async def lessons_post(body: LessonBody):
    """The user's own edits to the lessons the assistant reads before CAM and machine work."""
    import lessons
    try:
        if body.action == "add":
            lessons.add(WORKSPACE, body.title, body.rule, body.why, body.tags, body.check or None, source="user")
        elif body.action in ("update", "retire", "activate"):
            st = {"retire": "retired", "activate": "active"}.get(body.action)
            row = next((r for r in lessons.load(WORKSPACE) if r["id"] == body.id), None)
            if row is None:
                raise ValueError(f"no lesson {body.id}")
            lessons.update(WORKSPACE, body.id, title=body.title, rule=body.rule, tags=body.tags, status=st,
                           check=body.check if body.check is not None else None)
            if body.why is not None:
                rows = lessons.load(WORKSPACE)
                next(r for r in rows if r["id"] == body.id)["why"] = body.why.strip()
                lessons._save(WORKSPACE, rows)
        elif body.action == "delete":
            lessons.remove(WORKSPACE, body.id or "")
        else:
            raise ValueError("action: add | update | retire | activate | delete")
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    rows = lessons.load(WORKSPACE)
    await bus.emit({"type": "lessons", "lessons": rows})
    return {"lessons": rows}


# ---------------------------------------------------------------- dreaming: consolidate the lessons while idle
_activity = {"t": time.time()}
_dreaming = {"running": False, "last_check": None}


async def _dream_llm(system: str, prompt: str) -> str:
    """One answer from Claude with no tools (the consolidation pass), on the user's model and login."""
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, query
    from claude_agent_sdk.types import TextBlock
    st = agent.settings
    d = WORKSPACE / "dreams"
    d.mkdir(exist_ok=True)
    opts = ClaudeAgentOptions(system_prompt=system, allowed_tools=[], tools=[], setting_sources=[], permission_mode="dontAsk",
                              max_turns=1, cwd=str(d), model=st.get("model") or os.environ.get("AGENTICCAD_MODEL") or None,
                              env={"ENABLE_TOOL_SEARCH": "false", **({"ANTHROPIC_API_KEY": st["api_key"]} if st.get("api_key") else {})})
    cli = find_claude_cli()
    if not cli:
        raise RuntimeError("Claude Code is not installed")
    opts.cli_path = cli
    out: list[str] = []
    async for msg in query(prompt=prompt, options=opts):
        if isinstance(msg, AssistantMessage):
            out += [b.text for b in msg.content if isinstance(b, TextBlock)]
    return "".join(out)


async def _dream_now(reason: str, runs: list[dict] | None = None) -> dict:
    import dream
    _dreaming["running"] = True
    await bus.emit({"type": "dreaming", "running": True})
    try:
        since = min((m.get("t0") or 0) for m in runs) if runs else (dream.state(WORKSPACE).get("last_t") or time.time() - 86400)
        chat = []
        for a in DOCS.values():                            # what was said about the run(s), in every open design
            chat += await asyncio.to_thread(a.chat_history, 60, since)
        entry = await dream.run(WORKSPACE, _dream_llm, reason, runs, chat)
    finally:
        _dreaming["running"] = False
    import lessons
    await bus.emit({"type": "lessons", "lessons": lessons.load(WORKSPACE)})
    await bus.emit({"type": "dreaming", "running": False, "entry": entry})
    return entry


async def _dream_loop() -> None:
    """After each CNC run (any outcome), once nobody has used the app for a while (no chat turn, no machine job, no
    clicks - so the user's reaction to the run is in): draw the lessons out of that run and fold them into the others
    (dream.py). Never on a timer: the loop only looks for finished runs (a file listing), the model runs once per run.
    Settings ▸ Lessons can switch it off."""
    import dream
    await asyncio.to_thread(dream.pending_runs, WORKSPACE)     # first start: runs already on disk count as seen
    while True:
        await asyncio.sleep(120)
        try:
            if agent.settings.get("dream", True) is False or _dreaming["running"]:
                continue
            busy = any(a.busy for a in DOCS.values()) or machine.job_active or bool(machine.routine)
            idle = 0.0 if busy else time.time() - _activity["t"]
            runs, why = await asyncio.to_thread(dream.due, WORKSPACE, idle)
            _dreaming["last_check"] = {"t": time.time(), "due": bool(runs), "why": why}
            if runs:
                await _dream_now(why, runs)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - a failed pass never takes the app down; it shows in the log
            _dreaming["last_check"] = {"t": time.time(), "due": False, "why": f"failed: {type(e).__name__}: {e}"}


class DreamBody(BaseModel):
    action: str                      # run | undo | enable | disable


@app.get("/api/dreams")
async def dreams_get():
    import dream
    return {"log": dream.log(WORKSPACE), "state": dream.state(WORKSPACE), "running": _dreaming["running"],
            "last_check": _dreaming["last_check"], "enabled": agent.settings.get("dream", True) is not False}


@app.post("/api/dreams")
async def dreams_post(body: DreamBody):
    import dream
    import lessons
    try:
        if body.action == "run":
            if _dreaming["running"]:
                raise ValueError("a consolidation pass is already running")
            import dream
            runs = await asyncio.to_thread(dream.pending_runs, WORKSPACE)
            return {"entry": await _dream_now("asked by the user", runs or None)}
        if body.action == "undo":
            e = dream.undo(WORKSPACE)
            await bus.emit({"type": "lessons", "lessons": lessons.load(WORKSPACE)})
            return {"entry": e}
        if body.action in ("enable", "disable"):
            agent.save_settings({"dream": body.action == "enable"})
            return {"enabled": agent.settings.get("dream", True) is not False}
        raise ValueError("action: run | undo | enable | disable")
    except (ValueError, RuntimeError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/settings")
async def settings_get():
    return {"settings": agent.public_settings(), "models": agent.MODELS, "defaults": {k: v for k, v in agent.DEFAULT_SETTINGS.items() if k != "api_key"}}


@app.post("/api/settings")
async def settings_set(body: SettingsBody):
    patch = dict(body.settings)
    if patch.get("api_key") == "__keep__":           # the browser never sees the key; this sentinel means "unchanged"
        patch.pop("api_key")
    try:
        agent.save_settings(patch)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    st = agent.public_settings()
    if body.restart:
        try:
            await agent.restart()
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": f"restart failed: {type(e).__name__}: {e}", "settings": st}, status_code=500)
    return {"settings": st, "restarted": body.restart}


@app.get("/api/agent/status")
async def agent_status():
    return await agent.agent_status()


@app.post("/api/agent/restart")
async def agent_restart():
    try:
        await agent.restart()
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    return {"ok": True}


# ---------------------------------------------------------------- design files
class NameBody(BaseModel):
    name: str | None = None


class ImportBody(BaseModel):
    name: str
    code: str | None = None
    data: str | None = None      # a shared .zip, base64
    confirm: bool = False        # the user has seen what the scripts reach (share.risky) and imports anyway


@app.get("/api/designs")
async def designs():
    return {"designs": agent.list_designs(), "current": agent.design_name, "dirty": agent.dirty}


@app.post("/api/design/save")
async def design_save(body: NameBody):
    try:
        name = await agent.save_design(body.name)
    except ck.CadError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"name": name}


@app.post("/api/design/open")
async def design_open(body: NameBody):
    try:
        await agent.open_design(body.name or "")
    except ck.CadError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"name": agent.design_name}


@app.post("/api/design/new")
async def design_new():
    try:
        await agent.new_design()
    except ck.CadError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True}


@app.post("/api/design/import")
async def design_import(body: ImportBody):
    """A design script (.py), or a shared bundle (.zip: design + CAM script). Scripts run as Python here, so what they
    reach past geometry (imports, files, eval) is shown to the user first: {confirm: [...]} until they agree."""
    import share
    cam_code = None
    if body.data is not None:
        try:
            b = share.read_bundle(base64.b64decode(body.data.split(",", 1)[-1]))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        name, code, cam_code = b["name"], b["code"], b["cam_code"]
    else:
        name, code = body.name, body.code or ""
    if not body.confirm:
        risks = share.risky(code, "design") + (share.risky(cam_code, "CAM") if cam_code else [])
        if risks:
            return {"confirm": risks, "name": name}
    try:
        name = await agent.import_design(name, code, cam_code)
    except ck.CadError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"name": name, "cam": bool(cam_code)}


@app.post("/api/design/share")
async def design_share():
    """Bundle the design as it is on screen (script, CAM script, STEP, README) and open a new email with it attached in
    the user's own mail app. The app sends nothing: the user addresses it and presses Send."""
    import share
    if agent.model is None:
        return JSONResponse({"error": "no design to share"}, status_code=400)
    name = agent.runs_design()
    out = WORKSPACE / "shared" / share._safe(name)
    step = None
    try:
        step = (await asyncio.to_thread(ck.export, agent.model, out, share._safe(name), ["step"]))[0]
    except Exception:  # noqa: BLE001 - the scripts still go; the README says what's in it
        step = None
    cam_info = None
    p = agent.program
    if agent.cam_code.strip() and p is not None:
        try:
            cam_info = {"summary": f"{p.machine.name}, {len(p.ops)} operation{'s' if len(p.ops) != 1 else ''}, about {round(p.time_minutes())} min"}
        except Exception:  # noqa: BLE001
            cam_info = {"summary": ""}
    path = share.bundle(out, name, agent.model.code, agent.cam_code, step, __version__, cam_info)
    subject, text = share.message(name, __version__, cam_info if agent.cam_code.strip() else None, step is not None)
    if os.environ.get("AGENTICCAD_NO_MAIL"):                      # tests: build the bundle, open nothing
        return {"file": str(path), "how": "none", "note": ""}
    r = await asyncio.to_thread(share.compose, path, subject, text)
    return {"file": str(path), **r}


@app.get("/api/design/download")
async def design_download():
    if agent.model is None:
        return JSONResponse({"error": "no model"}, status_code=400)
    name = (agent.design_name or "untitled") + ".py"
    return PlainTextResponse(agent.model.code, headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------------------------------------------------------------- websocket
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    doc = ws.query_params.get("doc")
    if doc == SHELL:                                       # the tab strip: machine / library events only
        bus.clients[ws] = SHELL
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            bus.clients.pop(ws, None); bus.binary.discard(ws)
        return
    doc = doc if doc in DOCS else _default_doc()
    _doc_var.set(doc)
    bus.clients[ws] = doc
    if ws.query_params.get("bin") == "1":
        bus.binary.add(ws)
    try:
        if not agent.startup["done"] or agent.startup["error"]:      # still building the first design: feed the splash
            await ws.send_text(json.dumps({"type": "startup", **agent.startup_payload()}))
        if agent.model:
            await ws.send_text(json.dumps(_model_event()))
        await ws.send_text(json.dumps(agent.design_state()))
        hist = await asyncio.to_thread(agent.chat_history)
        if hist:                                           # a page opened after a relaunch: the conversation that resumes
            await ws.send_text(json.dumps({"type": "chat_history", "items": hist}))
        await ws.send_text(json.dumps({"type": "library", **agent.library_payload()}))
        await ws.send_text(json.dumps({"type": "library_parts", "parts": agent.parts.list()}))
        await bus.send(ws, await asyncio.to_thread(_cam_event, agent))
        await ws.send_text(json.dumps(agent.slicer_payload()))
        await ws.send_text(json.dumps(agent.extensions.payload()))
        if agent.slice_layers is not None:
            await ws.send_text(json.dumps({"type": "sliced", "result": agent.slice_result.to_dict() | {"summary": agent.slice_result.summary()},
                                           "layers": agent.slice_layers, "restore": True}))
        await ws.send_text(json.dumps({"type": "status", "busy": agent.busy,
                                       "agent_ready": agent.client is not None, "auth_required": agent.auth_problem}))
        for req in agent.pending_machine_requests():                  # approvals still waiting (a reload must not lose them)
            await ws.send_text(json.dumps({"type": "machine_request", **req}))
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
                await _dispatch(ws, msg)
            except WebSocketDisconnect:
                raise
            except (ck.CadError, cam.CamError, slicer.SlicerError, script_edit.Refused, script_edit.Unsupported, KeyError, ValueError, TypeError) as e:
                # a bad or failed manual operation must never drop the connection
                await ws.send_text(json.dumps({"type": "error", "text": f"{msg.get('type', '?') if isinstance(msg, dict) else '?'} failed: {e}"}))
    except WebSocketDisconnect:
        pass
    finally:
        bus.clients.pop(ws, None); bus.binary.discard(ws)


async def _dispatch(ws: WebSocket, msg: dict) -> None:
    t = msg.get("type")
    _activity["t"] = time.time()                            # the user is here (the lessons pass waits for quiet)
    if t == "chat":
        bus.primary[_doc_var.get() or _default_doc()] = ws
        asyncio.create_task(agent.chat(msg.get("text", ""), msg.get("selection"), msg.get("images") or []))
    elif t == "get_model" and agent.model:
        await ws.send_text(json.dumps(_model_event()))
    elif t == "set_quality":
        await agent.set_quality(msg.get("quality", "normal"))
    elif t == "run_cam":
        try:
            await agent.set_cam_code(msg.get("code", ""), rebuild=bool(msg.get("code", "").strip()), source="user")
            agent.notes.append("The user edited and rebuilt the CAM script by hand in the Code panel.")
        except cam.CamError as e:
            await ws.send_text(json.dumps({"type": "cam_error", "text": str(e)}))
        except Exception as e:  # noqa: BLE001
            await ws.send_text(json.dumps({"type": "cam_error", "text": f"{type(e).__name__}: {e}"}))
    elif t == "run_machine":
        try:
            await agent.set_machine_code(msg.get("name", ""), msg.get("code", ""), source="user")
            agent.notes.append(f"The user edited and rebuilt the machine model script of '{msg.get('name', '')}' by hand in the Code panel.")
        except cam.CamError as e:
            await ws.send_text(json.dumps({"type": "machine_error", "name": msg.get("name", ""), "text": str(e)}))
        except Exception as e:  # noqa: BLE001
            await ws.send_text(json.dumps({"type": "machine_error", "name": msg.get("name", ""), "text": f"{type(e).__name__}: {e}"}))
    elif t == "get_machine_code":
        try:
            await ws.send_text(json.dumps({"type": "machine", **agent.machine_code(msg.get("name", "")), "source": "load", "show": False}))
        except cam.CamError as e:
            await ws.send_text(json.dumps({"type": "machine_error", "name": msg.get("name", ""), "text": str(e)}))
    elif t == "set_fixture":
        try:
            await agent.set_setup_fixture(str(msg.get("setup") or ""), msg.get("fixture"))
        except cam.CamError as e:
            await ws.send_text(json.dumps({"type": "cam_error", "text": str(e)}))
        except Exception as e:  # noqa: BLE001
            await ws.send_text(json.dumps({"type": "cam_error", "text": f"{type(e).__name__}: {e}"}))
    elif t == "run_fixture":
        try:
            await agent.set_fixture_code(msg.get("code", ""), source="user")
            agent.notes.append("The user edited and rebuilt a fixture script by hand in the Code panel.")
        except cam.CamError as e:
            await ws.send_text(json.dumps({"type": "fixture_error", "name": msg.get("name", ""), "text": str(e)}))
        except Exception as e:  # noqa: BLE001
            await ws.send_text(json.dumps({"type": "fixture_error", "name": msg.get("name", ""), "text": f"{type(e).__name__}: {e}"}))
    elif t == "get_fixture_code":
        try:
            await ws.send_text(json.dumps({"type": "fixture", **agent.fixture_code(msg.get("name", "")), "source": "load", "show": False}))
        except cam.CamError as e:
            await ws.send_text(json.dumps({"type": "fixture_error", "name": msg.get("name", ""), "text": str(e)}))
    elif t == "set_override":
        try:
            await agent.set_op_override(str(msg.get("key") or ""), msg.get("values"))
        except cam.CamError as e:
            await ws.send_text(json.dumps({"type": "cam_error", "text": str(e)}))
        except Exception as e:  # noqa: BLE001
            await ws.send_text(json.dumps({"type": "cam_error", "text": f"{type(e).__name__}: {e}"}))
    elif t == "machine_approve":                         # the user's Approve / Decline on an assistant's machine request
        agent.resolve_machine_request(str(msg.get("id") or ""), bool(msg.get("ok")))
    elif t == "add_op":                                   # CAM tab ▸ Add operation
        try:
            await agent.add_cam_op(msg.get("spec") or {})
        except cam.CamError as e:
            await ws.send_text(json.dumps({"type": "cam_error", "text": str(e)}))
        except Exception as e:  # noqa: BLE001
            await ws.send_text(json.dumps({"type": "cam_error", "text": f"{type(e).__name__}: {e}"}))
    elif t == "set_sketch":
        try:
            await agent.set_sketch(msg.get("name") or "sketch1", msg.get("plane") or {}, msg.get("items") or [], constraints=msg.get("constraints") or [])
        except (ck.CadError, Exception) as e:  # noqa: BLE001
            await ws.send_text(json.dumps({"type": "error", "text": f"sketch failed: {e}"}))
    elif t == "extrude_sketch":
        await agent.extrude_sketch(msg.get("name", ""), float(msg.get("amount") or 0), msg.get("op", "new"),
                                   msg.get("body"), bool(msg.get("both")), msg.get("body_name"))
    elif t == "add_primitive":
        await agent.add_primitive(msg.get("kind", "box"), msg.get("dims") or {}, msg.get("at") or [0, 0, 0], msg.get("name"))
    elif t == "op":
        k = msg.get("kind")
        try:
            if k == "primitive":
                await agent.op_primitive(msg.get("shape", "box"), msg.get("dims") or {}, msg.get("at") or [0, 0, 0], msg.get("normal"), msg.get("body"), msg.get("mode", "join"), msg.get("name"))
            elif k == "extrude_face":
                await agent.op_extrude_face(msg["body"], msg["point"], msg["normal"], float(msg.get("amount") or 0), msg.get("mode", "join"))
            elif k == "hole":
                await agent.op_hole(msg["body"], msg["at"], msg["normal"], float(msg.get("diameter") or 0), msg.get("depth"), bool(msg.get("through")), msg.get("thread"), msg.get("counterbore"))
            elif k in ("fillet", "chamfer"):
                await agent.op_edges(msg["body"], msg.get("points") or [], float(msg.get("radius") or 0), k, msg.get("faces"))
            elif k == "shell":
                await agent.op_shell(msg["body"], msg.get("faces") or [], float(msg.get("thickness") or 1))
            elif k == "mirror":
                await agent.op_mirror(msg["body"], msg["origin"], msg["normal"], msg.get("mode", "join"))
            elif k == "pattern":
                await agent.op_pattern(msg["body"], msg.get("pattern", "linear"), msg.get("params") or {}, msg.get("mode", "join"), msg.get("target"))
            elif k == "revolve":
                await agent.op_revolve(msg["sketch"], msg["axis_origin"], msg["axis_dir"], float(msg.get("angle") or 360), msg.get("mode", "new"), msg.get("target"))
            elif k == "loft":
                await agent.op_loft(msg.get("sketches") or [], bool(msg.get("ruled")), msg.get("mode", "new"), msg.get("target"))
            elif k == "joint":
                await agent.op_joint(msg["name"], msg.get("joint_type", "revolute"), msg.get("bodies") or [], msg.get("origin") or [0, 0, 0],
                                     msg.get("direction") or [0, 0, 1], msg.get("parent"), msg.get("limits"), msg.get("couple_to"), float(msg.get("ratio") or 1))
            elif k == "sweep":
                await agent.op_sweep(msg["path_body"], msg.get("points") or [], msg.get("sketch"), msg.get("diameter"), msg.get("wall"), msg.get("mode", "new"), msg.get("target"), msg.get("face_point"))
        except (KeyError, ValueError, ck.CadError, script_edit.Unsupported) as e:
            await ws.send_text(json.dumps({"type": "error", "text": f"{k} failed: {e}"}))
    elif t == "transform_body":
        await agent.transform_body(msg.get("path", ""), msg.get("move") or [0, 0, 0], msg.get("rotate") or [0, 0, 0])
    elif t == "delete_sketch":
        await agent.delete_sketch(msg.get("name", ""))
    elif t == "rename_body":
        await agent.edit_body("rename", msg.get("path", ""), msg.get("name", ""))
    elif t == "delete_body":
        await agent.edit_body("delete", msg.get("path", ""))
    elif t == "undo":
        await agent.undo()
    elif t == "interrupt":
        await agent.interrupt()
    elif t == "screenshot_result":
        bus.resolve(msg["id"], msg.get("png", ""))
    elif t == "run_code":
        try:
            await agent.build(msg.get("code", ""), source="user")
        except ck.CadError as e:
            await ws.send_text(json.dumps({"type": "build_error", "text": str(e)}))
    elif t == "ext":
        # panels: open / change (a control changed → re-render) / call (a button or control's `call`) / action (a static RESULT dict)
        op, pid, state, sel = msg.get("op"), msg.get("panel"), msg.get("state") or {}, msg.get("selection")
        try:
            if op in ("open", "change"):
                await agent.ext_render(pid, state, msg.get("event"), sel)
            elif op == "call":
                await agent.ext_call(msg["call"], msg.get("args") or {}, state, msg.get("event"), sel, panel=pid if pid and not pid.startswith("eph:") else None)
            elif op == "action":
                await agent.ext_apply(msg.get("action") or {}, panel=None, state=state)
            elif op == "chat":
                bus.primary[_doc_var.get() or _default_doc()] = ws
                asyncio.create_task(agent.chat(msg.get("text", ""), msg.get("selection_chips"), []))
        except (ext_mod.ExtError, ck.CadError, script_edit.Unsupported, KeyError, ValueError, TypeError) as e:
            await ws.send_text(json.dumps({"type": "error", "text": f"extension {op} failed: {e}"}))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=int(os.environ.get("PORT") or 8765), reload=False)
