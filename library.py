"""Part library: reusable parts as build123d scripts (parametric) or STEP files, with metadata + thumbnail."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import script_edit


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "part"


class PartLibrary:
    def __init__(self, workspace: Path):
        self.root = workspace / "library"
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, name: str) -> Path:
        return self.root / slug(name)

    def list(self) -> list[dict[str, Any]]:
        out = []
        for d in sorted(self.root.iterdir()):
            m = d / "meta.json"
            if d.is_dir() and m.exists():
                try:
                    meta = json.loads(m.read_text())
                    meta["slug"] = d.name
                    meta["thumb"] = (d / "thumb.jpg").exists()
                    out.append(meta)
                except Exception:
                    continue
        return out

    def get(self, name: str) -> dict[str, Any] | None:
        d = self._dir(name)
        if not (d / "meta.json").exists():
            return None
        meta = json.loads((d / "meta.json").read_text())
        meta["slug"] = d.name
        return meta

    def save_script(self, name: str, code: str, description: str = "", tags: list[str] | None = None,
                    thumb_b64: str | None = None) -> dict[str, Any]:
        d = self._dir(name)
        d.mkdir(parents=True, exist_ok=True)
        (d / "part.py").write_text(code)
        meta = {"name": name, "kind": "script", "description": description, "tags": tags or [],
                "params": {p["name"]: p["value"] for p in script_edit.params(code)},
                "created": int(time.time())}
        (d / "meta.json").write_text(json.dumps(meta, indent=2))
        self._thumb(d, thumb_b64)
        return meta

    def save_step(self, name: str, data: bytes, description: str = "", tags: list[str] | None = None,
                  thumb_b64: str | None = None) -> dict[str, Any]:
        d = self._dir(name)
        d.mkdir(parents=True, exist_ok=True)
        (d / "part.step").write_bytes(data)
        meta = {"name": name, "kind": "step", "description": description, "tags": tags or [], "params": {},
                "created": int(time.time())}
        (d / "meta.json").write_text(json.dumps(meta, indent=2))
        self._thumb(d, thumb_b64)
        return meta

    def _thumb(self, d: Path, thumb_b64: str | None) -> None:
        if thumb_b64:
            import base64
            b64 = thumb_b64.split(",", 1)[-1]
            try:
                (d / "thumb.jpg").write_bytes(base64.b64decode(b64))
            except Exception:
                pass

    def delete(self, name: str) -> bool:
        d = self._dir(name)
        if not d.exists():
            return False
        import shutil
        shutil.rmtree(d)
        return True

    def code_for(self, name: str) -> str | None:
        d = self._dir(name)
        return (d / "part.py").read_text() if (d / "part.py").exists() else None

    def load_shape(self, name: str, **params):
        """Build the part shape (script parts accept parameter overrides)."""
        import cad_kernel as ck
        import build123d as b3d
        meta = self.get(name)
        if meta is None:
            raise ck.CadError(f"no library part named '{name}'. Parts: " + ", ".join(m["name"] for m in self.list()))
        d = self._dir(name)
        if meta["kind"] == "step":
            return b3d.import_step(str(d / "part.step"))
        code = (d / "part.py").read_text()
        unknown = [k for k in params if k not in meta.get("params", {})]
        if unknown:
            raise ck.CadError(f"library part '{name}' has no parameter(s) {unknown}; available: {list(meta.get('params', {}))}")
        if params:
            code = script_edit.set_params(code, params)
        ns = ck.script_namespace()
        ns["__name__"] = "__libpart__"
        tok_w = ck._CTX_WORKSPACE.set(self.root.parent); tok_l = ck._CTX_LIBRARY.set(self)   # nested parts resolve here
        try:
            exec(compile(code, f"library/{d.name}/part.py", "exec"), ns)
        finally:
            ck._CTX_WORKSPACE.reset(tok_w); ck._CTX_LIBRARY.reset(tok_l)
        if "result" not in ns:
            raise ck.CadError(f"library part '{name}' did not assign `result`")
        pairs = ck.coerce_bodies(ns["result"])
        shapes = [sh for _, sh in pairs]
        return shapes[0] if len(shapes) == 1 else b3d.Compound(children=shapes)

    def describe(self) -> str:
        parts = self.list()
        if not parts:
            return "Part library: empty."
        lines = ["Part library (use from_library(\"name\", **params) in the design script):"]
        for m in parts:
            ps = ", ".join(f"{k}={v}" for k, v in m.get("params", {}).items())
            lines.append(f"  - {m['name']} [{m['kind']}]" + (f" params: {ps}" if ps else "") + (f" — {m['description']}" if m.get("description") else ""))
        return "\n".join(lines)
