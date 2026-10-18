"""AgenticCAD server: static UI + WebSocket bridge between browser and the CAD agent, plus design file API."""
from __future__ import annotations

import asyncio
import base64
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
import script_edit
import slicer
from agent import CadAgent, claude_cli_candidates, find_claude_cli
from version import __version__

import os
import sys

ROOT = Path(__file__).parent
WORKSPACE = Path(os.environ.get("AGENTICCAD_WORKSPACE") or (ROOT / "workspace"))   # override for tests
for sub in ("exports", "history", "designs"):
    (WORKSPACE / sub).mkdir(parents=True, exist_ok=True)


class Bus:
    """Fan-out to browsers plus request/response for browser-rendered screenshots."""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.primary: WebSocket | None = None      # the tab the user last chatted from
        self.pending: dict[str, asyncio.Future] = {}

    async def emit(self, event: dict[str, Any]) -> None:
        data = json.dumps(event)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    async def screenshot(self, spec: dict[str, Any], timeout: float = 20.0) -> str:
        if not self.clients:
            raise RuntimeError("no browser connected to render the view")
        rid = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        req = json.dumps({"type": "screenshot_request", "id": rid, **spec})
        try:
            # Prefer the tab the user is working in; other tabs (or hidden preview webviews) may
            # answer with an empty render. Fall back to everyone if the primary is gone.
            targets = [self.primary] if self.primary in self.clients else list(self.clients)
            for ws in targets:
                try:
                    await ws.send_text(req)
                except Exception:
                    pass
            try:
                return await asyncio.wait_for(fut, timeout / 2)
            except asyncio.TimeoutError:
                if len(targets) == len(self.clients):
                    raise
                await self.emit({"type": "screenshot_request", "id": rid, **spec})
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
agent = CadAgent(WORKSPACE, bus.emit, bus.screenshot)
machine = machine_link.MachineControl(bus.emit)        # the Machine tab; the agent only reads it
_live = machine_link.makera_live_config()               # this machine's calibrated anchor, if MakeraStudio has fetched it
if "coordinate.anchor1_x" in _live and "coordinate.anchor1_y" in _live:
    machine.profile.anchor1 = (_live["coordinate.anchor1_x"], _live["coordinate.anchor1_y"])
if "coordinate.clearance_x" in _live and "coordinate.clearance_y" in _live:
    machine.profile.change_xy = (_live["coordinate.clearance_x"], _live["coordinate.clearance_y"])
camera = machine_link.CameraRelay()
agent.machine, agent.camera = machine, camera


_loader: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The first design is built in the background so the window can show a splash with progress at once (a big design
    # with real threads takes minutes). Clients get `startup` events, then the model when it lands.
    loop = asyncio.get_running_loop()
    agent.on_startup = lambda st: loop.call_soon_threadsafe(lambda: asyncio.ensure_future(bus.emit({"type": "startup", **st})))
    global _loader
    _loader = asyncio.create_task(_load_initial())
    if os.environ.get("AGENTICCAD_STARTUP_SYNC") == "1":  # tests: the model exists before the first request
        await _loader
    task = None
    if os.environ.get("AGENTICCAD_NO_AGENT") != "1":      # tests run the API without a Claude session
        task = asyncio.create_task(_connect_agent())
    watcher = asyncio.create_task(_watch_extensions())
    if os.environ.get("AGENTICCAD_NO_REMOTE") != "1" and _remote_cfg().get("enabled"):
        await _remote_start()
    yield
    await _remote_stop()
    if task:
        task.cancel()
    watcher.cancel()
    await machine.disconnect()
    camera.set_host(None)
    await agent.stop()


async def _load_initial() -> None:
    try:
        await asyncio.to_thread(agent.load_initial)
    except Exception as e:  # noqa: BLE001
        await bus.emit({"type": "error", "text": f"could not build the last design at startup: {e}"})
    await bus.emit({"type": "startup", **agent.startup_payload()})
    if agent.model:
        await bus.emit(_model_event())
        await bus.emit(agent.design_state())
        await bus.emit({"type": "cam", "code": agent.cam_code, "source": "load",
                        "program": agent.program.to_payload() if agent.program else None,
                        "summary": agent.program.summary() if agent.program else "",
                        "gcode_lines": agent.program.gcode().count("\n") if agent.program else 0})


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


async def _connect_agent() -> None:
    if _loader is not None:
        await _loader                                         # the agent's first context needs the built model
    if find_claude_cli() is None:
        await bus.emit({"type": "agent_missing_cli"})
        return
    auth = await asyncio.to_thread(agent.auth_status)
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


def _program_for_ops(body: MachineBody) -> tuple[dict, dict, str]:
    """The CAM program's chosen operations (one setup) as the Z1 will play them: (prepared program, info, file label)."""
    return agent.program_for_ops(body.ops or [], body.air or 0.0)


@app.get("/api/machine")
async def machine_get():
    saved = json.loads(MACHINE_FILE.read_text()) if MACHINE_FILE.exists() else {}
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
                                         body.y if body.y is not None else 20.0, body.depth or 2.0, body.dia or 2.0)
        elif a == "check_ops":                            # what running these operations would do (nothing is sent)
            prep, info, label = await asyncio.to_thread(_program_for_ops, body)   # big programs: keep the loop (and status) running
            chk = machine.check_job(prep)
            return {**machine.payload(), "check": {"label": machine_link.program_filename(label), "lines": prep["lines"],
                                                   "ops": prep["ops"], "tools": prep["tools"], "extents": prep["extents"],
                                                   "notes": chk["notes"], **info}}
        elif a == "run_ops":                              # upload the chosen operations to the machine and play them
            prep, info, label = await asyncio.to_thread(_program_for_ops, body)   # big programs: keep the loop (and status) running
            res = await machine.run_program(label, prep, info)
        elif a in ("check_file", "run_file"):              # any .nc file, through the same checks as CAM operations
            if not body.text:
                raise machine_link.Refused("no file")
            air = float(body.air or 0.0)
            if not (0.0 <= air <= 50.0):
                raise machine_link.Refused("air-cut lift: 0 .. 50 mm")
            prep = machine_link.prepare_program(body.text, [], air_cut=air)
            label = Path(body.name or "file").stem + ("_air" if air else "")
            if a == "check_file":
                chk = machine.check_job(prep)
                return {**machine.payload(), "check": {"label": machine_link.program_filename(label), "lines": prep["lines"],
                                                       "ops": [], "tools": prep["tools"], "extents": prep["extents"],
                                                       "notes": chk["notes"], "program": body.name or "file"}}
            res = await machine.run_program(label, prep, {"program": body.name or "file", "file": True})
        elif a == "set_origin":                            # the head's position becomes these work coordinates (G10 L20)
            res = await machine.set_origin(body.x, body.y, body.z)
        elif a == "origin_at":                             # X0 Y0 at machine x, y (e.g. the block corner from 3 probed corners)
            res = await machine.origin_at(body.x if body.x is not None else float('nan'), body.y if body.y is not None else float('nan'),
                                          body.kind or "typed", body.name or "set")
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
    cfg = {"enabled": False, "port": 8790, "token": "", "require_key": True}
    try:
        cfg.update(json.loads(REMOTE_FILE.read_text()))
    except (OSError, ValueError):
        pass
    if not cfg.get("token"):
        cfg["token"] = secrets.token_urlsafe(18)
        REMOTE_FILE.write_text(json.dumps(cfg))
    return cfg


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
    return {"enabled": cfg["enabled"], "port": cfg["port"], "running": running, "ip": ip, "error": _remote["error"],
            "require_key": cfg["require_key"],
            "url": f"http://{ip}:{cfg['port']}/m" + (f"?k={cfg['token']}" if cfg["require_key"] else "")}


phone = FastAPI()


def _key_ok(request: Request) -> bool:
    cfg = _remote_cfg()
    if not cfg["require_key"]:
        return True                                       # the user's choice: anyone on the network may use it
    k = request.headers.get("x-key") or request.query_params.get("k") or ""
    return hmac.compare_digest(k.encode(), cfg["token"].encode())


@phone.middleware("http")
async def _phone_auth(request: Request, call_next):
    if request.url.path in ("/m", "/m/") or request.url.path.startswith("/m/static/"):
        return await call_next(request)                   # the page itself; every API call carries the key
    if not _key_ok(request):
        return JSONResponse({"error": "not authorised: scan the QR code in AgenticCAD ▸ Settings ▸ Phone control"}, status_code=401)
    return await call_next(request)


@phone.get("/m")
async def phone_page():
    return FileResponse(ROOT / "static" / "mobile.html", headers={"Cache-Control": "no-store"})


@phone.get("/m/api/machine")
async def phone_machine_get():
    return await machine_get()


@phone.post("/m/api/machine")
async def phone_machine_post(body: MachineBody):
    return await machine_post(body)


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
    _remote["error"] = None
    server = uvicorn.Server(uvicorn.Config(phone, host="0.0.0.0", port=int(cfg["port"]), log_level="warning", lifespan="off"))
    server.install_signal_handlers = lambda: None         # the main server owns the signals

    async def run():
        try:
            await server.serve()
        except (OSError, SystemExit) as e:
            _remote["error"] = f"phone control could not start on port {cfg['port']}: {e}"
    _remote["server"], _remote["task"] = server, asyncio.create_task(run())
    await asyncio.sleep(0.3)


async def _remote_stop() -> None:
    srv, task = _remote["server"], _remote["task"]
    _remote["server"] = _remote["task"] = None
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
    REMOTE_FILE.write_text(json.dumps(cfg))
    if cfg["enabled"]:
        await _remote_start()
    else:
        await _remote_stop()
    return _remote_status()


@app.get("/api/version")
async def version():
    log = (ROOT / "CHANGELOG.md").read_text() if (ROOT / "CHANGELOG.md").exists() else ""
    return {"version": __version__, "changelog": log}


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
    code: str


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
    try:
        name = await agent.import_design(body.name, body.code)
    except ck.CadError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"name": name}


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
    bus.clients.add(ws)
    try:
        if not agent.startup["done"] or agent.startup["error"]:      # still building the first design: feed the splash
            await ws.send_text(json.dumps({"type": "startup", **agent.startup_payload()}))
        if agent.model:
            await ws.send_text(json.dumps(_model_event()))
        await ws.send_text(json.dumps(agent.design_state()))
        await ws.send_text(json.dumps({"type": "library", **agent.library_payload()}))
        await ws.send_text(json.dumps({"type": "library_parts", "parts": agent.parts.list()}))
        await ws.send_text(json.dumps({"type": "cam", "code": agent.cam_code, "source": "load",
                                       "program": agent.program.to_payload() if agent.program else None,
                                       "summary": agent.program.summary() if agent.program else "",
                                       "gcode_lines": agent.program.gcode().count("\n") if agent.program else 0}))
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
        bus.clients.discard(ws)


async def _dispatch(ws: WebSocket, msg: dict) -> None:
    t = msg.get("type")
    if t == "chat":
        bus.primary = ws
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
                bus.primary = ws
                asyncio.create_task(agent.chat(msg.get("text", ""), msg.get("selection_chips"), []))
        except (ext_mod.ExtError, ck.CadError, script_edit.Unsupported, KeyError, ValueError, TypeError) as e:
            await ws.send_text(json.dumps({"type": "error", "text": f"extension {op} failed: {e}"}))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=int(os.environ.get("PORT") or 8765), reload=False)
