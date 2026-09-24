"""AgenticCAD server: static UI + WebSocket bridge between browser and the CAD agent, plus design file API."""
from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import cad_kernel as ck
import cam_kernel as cam
import script_edit
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    agent.load_initial()
    task = None
    if os.environ.get("AGENTICCAD_NO_AGENT") != "1":      # tests run the API without a Claude session
        task = asyncio.create_task(_connect_agent())
    yield
    if task:
        task.cancel()
    await agent.stop()


async def _connect_agent() -> None:
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
                    radial_engagement: float | None = None):
    tm = agent.library.tool_map()
    t = tm.get(tool)
    if t is None:
        return JSONResponse({"error": "unknown tool"}, status_code=400)
    m = agent.library.machines().get(machine or "")
    try:
        return cam.feeds(t, material, m, aggressiveness, radial_engagement)
    except cam.CamError as e:
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
    material: str = ""
    density: float | None = None
    sheet: str = "A4"
    notes: str = ""


@app.get("/api/sketches")
async def sketches_get():
    return {"sketches": agent.sketch_defs()}


@app.get("/api/params")
async def params_get():
    return {"params": agent.get_params()}


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
        res = await agent.make_drawings(body.material, body.density, body.sheet, body.notes)
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


# ---------------------------------------------------------------- settings / agent
class SettingsBody(BaseModel):
    settings: dict
    restart: bool = False


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
        if agent.model:
            await ws.send_text(json.dumps(_model_event()))
        await ws.send_text(json.dumps(agent.design_state()))
        await ws.send_text(json.dumps({"type": "library", **agent.library_payload()}))
        await ws.send_text(json.dumps({"type": "library_parts", "parts": agent.parts.list()}))
        await ws.send_text(json.dumps({"type": "cam", "code": agent.cam_code, "source": "load",
                                       "program": agent.program.to_payload() if agent.program else None,
                                       "summary": agent.program.summary() if agent.program else "",
                                       "gcode_lines": agent.program.gcode().count("\n") if agent.program else 0}))
        await ws.send_text(json.dumps({"type": "status", "busy": agent.busy,
                                       "agent_ready": agent.client is not None, "auth_required": agent.auth_problem}))
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
                await _dispatch(ws, msg)
            except WebSocketDisconnect:
                raise
            except (ck.CadError, cam.CamError, script_edit.Refused, script_edit.Unsupported, KeyError, ValueError, TypeError) as e:
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
    elif t == "set_sketch":
        try:
            await agent.set_sketch(msg.get("name") or "sketch1", msg.get("plane") or {}, msg.get("items") or [])
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
        except (KeyError, ValueError, ck.CadError) as e:
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=int(os.environ.get("PORT") or 8765), reload=False)
