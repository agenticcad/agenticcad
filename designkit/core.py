"""Design Kit core: component registry, guides, search, generated table of contents, usage + notes.

Two layers share one format:
  built-in   designkit/components/*.py and designkit/guides/<category>/*.md (read-only, shipped with the app)
  workspace  <workspace>/kit/parts/*.py and <workspace>/kit/guides/<category>/*.md (written by the agent and user)
The TOC files are generated from entry metadata; usage counts and notes live in <workspace>/kit/*.json.
"""
from __future__ import annotations

import functools
import importlib
import json
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent

CATEGORIES: dict[str, str] = {
    "fasteners": "Fasteners & threads",
    "bearings": "Bearings & bushings",
    "shafts": "Shafts & retention",
    "transmission": "Power transmission",
    "motors": "Motors & actuators",
    "structural": "Structural & framing",
    "electronics": "Electronics packaging",
    "enclosures": "Enclosures & housings",
    "mechanisms": "Mechanisms",
    "fluid": "Fluid & pneumatic",
    "manufacturing": "Manufacturing",
    "tolerances": "Tolerances, fits & materials",
    "reference": "Standards & reference",
    "modelling": "Modelling with build123d",
}

BUILTIN_MODULES = ["fasteners", "bearings", "shafts", "transmission", "motors", "electronics", "structural", "enclosures", "mechanisms", "fluid"]


class KitError(Exception):
    pass


@dataclass
class Entry:
    id: str                         # component: function name; guide: "<category>/<slug>"
    kind: str                       # "component" | "guide"
    title: str
    category: str
    tags: list[str]
    summary: str
    layer: str                      # "builtin" | "workspace"
    path: Path | None = None
    standard: str = ""
    example: str = ""
    related: list[str] = field(default_factory=list)
    fn: Callable[..., Any] | None = None

    def line(self, usage: dict[str, Any] | None = None) -> str:
        u = (usage or {}).get(self.id, {})
        used = f" · used {u['count']}×" if u.get("count") else ""
        std = f" · {self.standard}" if self.standard else ""
        tags = ", ".join(self.tags)
        return f"- `{self.id}` ({self.kind}{std}{used}): {self.summary}" + (f" [{tags}]" if tags else "")


# ------------------------------------------------------------------ component decorator
_PENDING: list[Entry] = []          # filled by @component while a workspace module is being exec'd
_BY_MODULE: dict[str, list[Entry]] = {}   # every registration, keyed by the module that called component()


def component(category: str, summary: str, tags: list[str] | tuple[str, ...] = (), standard: str = "",
              example: str = "", related: list[str] | tuple[str, ...] = ()):
    """Register a kit component. The function runs builder-safe (never adds to the caller's BuildPart/BuildSketch)."""
    if category not in CATEGORIES:
        raise KitError(f"unknown category '{category}' (one of {', '.join(CATEGORIES)})")
    import sys
    caller = sys._getframe(1).f_globals.get("__name__", "")       # the module registering (not the function's own)

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        from threads import builder_safe
        wrapped = builder_safe(fn)
        e = Entry(id=fn.__name__, kind="component", title=fn.__name__, category=category, tags=list(tags),
                  summary=summary, layer="builtin", standard=standard, example=example, related=list(related), fn=wrapped)
        _PENDING.append(e)
        lst = _BY_MODULE.setdefault(caller, [])
        lst[:] = [x for x in lst if x.id != e.id] + [e]
        return wrapped
    return deco


# ------------------------------------------------------------------ helpers shared by components
def place(obj: Any, loc: Any) -> Any:
    """Apply a Location (Pos/Rot/...) to a part or to a nested dict of parts (as detailed components return)."""
    if isinstance(obj, dict):
        return {k: place(v, loc) for k, v in obj.items()}
    return loc * obj


def _frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    meta: dict[str, Any] = {}
    for ln in text[3:end].strip().splitlines():
        if ":" not in ln:
            continue
        k, v = ln.split(":", 1)
        v = v.strip()
        if v.startswith("[") and v.endswith("]"):
            meta[k.strip()] = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
        else:
            meta[k.strip()] = v.strip("'\"")
    return meta, text[end + 4:].lstrip("\n")


def _guide_entries(root: Path, layer: str) -> list[Entry]:
    out = []
    if not root.exists():
        return out
    for p in sorted(root.glob("*/*.md")):
        meta, _ = _frontmatter(p.read_text())
        cat = meta.get("category") or p.parent.name
        out.append(Entry(id=f"{p.parent.name}/{p.stem}", kind="guide", title=meta.get("title", p.stem), category=cat,
                         tags=list(meta.get("tags", [])), summary=meta.get("summary", ""), layer=layer, path=p,
                         related=list(meta.get("related", []))))
    return out


@functools.lru_cache(maxsize=1)
def _builtin_components() -> tuple[Entry, ...]:
    out: list[Entry] = []
    for mod in BUILTIN_MODULES:
        name = f"designkit.components.{mod}"
        importlib.import_module(name)                      # registers into _BY_MODULE (whenever it was first imported)
        for e in _BY_MODULE.get(name, []):
            e.path = ROOT / "components" / f"{mod}.py"
            out.append(e)
    _PENDING.clear()
    return tuple(out)


def _load_workspace_components(parts_dir: Path) -> list[Entry]:
    """exec each <workspace>/kit/parts/*.py with the script namespace + `component`; collect what it registers."""
    out: list[Entry] = []
    if not parts_dir.exists():
        return out
    import cad_kernel as ck
    for p in sorted(parts_dir.glob("*.py")):
        _PENDING.clear()
        ns = ck.script_namespace()
        ns["component"] = component
        ns["place"] = place
        ns["__name__"] = f"kit_workspace_{p.stem}"
        try:
            exec(compile(p.read_text(), str(p), "exec"), ns)
        except Exception as ex:  # a broken workspace part must not break every design
            _PENDING.clear()
            out.append(Entry(id=p.stem, kind="component", title=p.stem, category="reference", tags=["broken"],
                             summary=f"FAILED TO LOAD: {type(ex).__name__}: {ex}", layer="workspace", path=p))
            continue
        for e in _PENDING:
            e.layer, e.path = "workspace", p
        out.extend(_PENDING)
        _PENDING.clear()
    return out


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower().replace("_", " ")))


def _hit(word: str, tokens: set[str]) -> bool:
    """Whole-word match; words of 4+ letters also match as a prefix (bearing -> bearings)."""
    return word in tokens or (len(word) >= 4 and not word.isdigit() and any(t.startswith(word) for t in tokens))


# ------------------------------------------------------------------ the kit (one per workspace)
class Kit:
    def __init__(self, workspace: Path | None = None):
        self.ws = Path(workspace) / "kit" if workspace else None
        self._ws_cache: tuple[float, list[Entry]] | None = None

    # ---- entries
    def _ws_components(self) -> list[Entry]:
        if not self.ws:
            return []
        d = self.ws / "parts"
        stamp = max([p.stat().st_mtime for p in d.glob("*.py")], default=0.0) if d.exists() else 0.0
        if self._ws_cache is None or self._ws_cache[0] != stamp:
            self._ws_cache = (stamp, _load_workspace_components(d))
        return self._ws_cache[1]

    def entries(self) -> list[Entry]:
        es = list(_builtin_components()) + _guide_entries(ROOT / "guides", "builtin")
        if self.ws:
            es += self._ws_components() + _guide_entries(self.ws / "guides", "workspace")
        return es

    def get(self, entry_id: str) -> Entry:
        es = {e.id: e for e in self.entries()}
        if entry_id in es:
            return es[entry_id]
        hits = [e for e in es.values() if e.id.split("/")[-1] == entry_id]
        if len(hits) == 1:
            return hits[0]
        raise KitError(f"no kit entry '{entry_id}'" + (f" (did you mean {', '.join(h.id for h in hits)}?)" if hits else ""))

    # ---- workspace json state
    def _json(self, name: str) -> dict[str, Any]:
        if not self.ws:
            return {}
        p = self.ws / name
        try:
            return json.loads(p.read_text()) if p.exists() else {}
        except Exception:
            return {}

    def _write_json(self, name: str, data: dict[str, Any]) -> None:
        if not self.ws:
            raise KitError("no workspace kit")
        self.ws.mkdir(parents=True, exist_ok=True)
        (self.ws / name).write_text(json.dumps(data, indent=1, sort_keys=True))

    def usage(self) -> dict[str, Any]:
        return self._json("usage.json")

    def _bump(self, entry_id: str, how: str) -> None:
        if not self.ws:
            return
        u = self.usage()
        rec = u.setdefault(entry_id, {"count": 0, "reads": 0})
        rec["count" if how == "use" else "reads"] = rec.get("count" if how == "use" else "reads", 0) + 1
        rec["last"] = time.strftime("%Y-%m-%d")
        self._write_json("usage.json", u)

    def record_usage(self, code: str) -> list[str]:
        """Count the kit components a successfully built script calls (kit.<name>(...)); refresh the workspace TOC."""
        names = set(re.findall(r"\bkit\.([A-Za-z_]\w*)\s*\(", code))
        known = {e.id for e in self.entries() if e.kind == "component"}
        used = sorted(n for n in names if n in known)
        for n in used:
            self._bump(n, "use")
        if used:
            self.write_toc()
        return used

    # ---- search / read / toc
    def search(self, query: str = "", kind: str | None = None, category: str | None = None,
               tags: list[str] | None = None, limit: int = 12) -> list[Entry]:
        words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 1]
        usage = self.usage()
        scored = []
        for e in self.entries():
            if kind and e.kind != kind:
                continue
            if category and e.category != category:
                continue
            if tags and not set(t.lower() for t in tags) <= set(t.lower() for t in e.tags):
                continue
            hay_id = _tokens(e.id)
            hay_tags = _tokens(" ".join(e.tags))
            hay = _tokens(f"{e.title} {e.summary} {e.standard}")
            s = 0.0
            for w in words:
                s += 5 * _hit(w, hay_id) + 4 * _hit(w, hay_tags) + 2 * _hit(w, hay)
            if words and s == 0:
                continue
            s += math.log1p(usage.get(e.id, {}).get("count", 0)) * 0.5
            s += 0.3 if e.layer == "workspace" else 0
            scored.append((s, e))
        scored.sort(key=lambda t: (-t[0], t[1].id))
        return [e for _, e in scored[:limit]]

    def read(self, entry_id: str) -> str:
        e = self.get(entry_id)
        notes = self._json("notes.json").get(e.id, [])
        self._bump(e.id, "read")
        head = f"# {e.title}\n{e.kind} · {CATEGORIES.get(e.category, e.category)} · {e.layer}" + (f" · {e.standard}" if e.standard else "")
        if e.tags:
            head += f"\ntags: {', '.join(e.tags)}"
        if e.kind == "guide":
            _, body = _frontmatter(e.path.read_text())
            text = f"{head}\n\n{body}"
        else:
            import inspect
            fn = e.fn
            target = getattr(fn, "__wrapped__", fn)
            sig = f"kit.{e.id}{inspect.signature(target)}"
            doc = inspect.getdoc(target) or ""
            text = f"{head}\n\n{e.summary}\n\n```python\n{sig}\n```\n\n{doc}"
            if e.example:
                text += f"\n\nExample:\n```python\n{e.example}\n```"
        if e.related:
            text += "\n\nRelated: " + ", ".join(e.related)
        if notes:
            text += "\n\nNotes from this workspace:\n" + "\n".join(f"- {n['date']}: {n['text']}" for n in notes)
        return text

    def toc(self, layer: str | None = None, category: str | None = None) -> str:
        usage = self.usage()
        es = [e for e in self.entries() if (layer is None or e.layer == layer) and (category is None or e.category == category)]
        lines = ["# Design Kit contents", "",
                 f"{sum(e.kind == 'component' for e in es)} components, {sum(e.kind == 'guide' for e in es)} guides."
                 " Generated from each entry's metadata; do not edit by hand.", ""]
        top = sorted((e for e in es if usage.get(e.id, {}).get("count")), key=lambda e: -usage[e.id]["count"])[:8]
        if top:
            lines += ["## Most used here", ""] + [e.line(usage) for e in top] + [""]
        for slug, title in CATEGORIES.items():
            ce = [e for e in es if e.category == slug]
            if not ce:
                continue
            lines += [f"## {title}", ""]
            lines += [e.line(usage) for e in sorted(ce, key=lambda e: (e.kind != "guide", e.id))]
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def categories_summary(self) -> str:
        es = self.entries()
        parts = []
        for slug, title in CATEGORIES.items():
            n_c = sum(1 for e in es if e.category == slug and e.kind == "component")
            n_g = sum(1 for e in es if e.category == slug and e.kind == "guide")
            if n_c or n_g:
                parts.append(f"{slug} ({n_c} components, {n_g} guides)")
        return "; ".join(parts)

    def write_toc(self) -> Path | None:
        if not self.ws:
            return None
        self.ws.mkdir(parents=True, exist_ok=True)
        p = self.ws / "TOC.md"
        p.write_text(self.toc())
        return p

    # ---- writing (workspace layer only)
    def note(self, entry_id: str, text: str) -> str:
        e = self.get(entry_id)
        notes = self._json("notes.json")
        notes.setdefault(e.id, []).append({"date": time.strftime("%Y-%m-%d"), "text": text.strip()})
        self._write_json("notes.json", notes)
        return e.id

    def save_guide(self, slug: str, title: str, category: str, summary: str, body: str,
                   tags: list[str] | None = None, related: list[str] | None = None) -> str:
        if not self.ws:
            raise KitError("no workspace kit")
        if category not in CATEGORIES:
            raise KitError(f"unknown category '{category}' (one of {', '.join(CATEGORIES)})")
        slug = re.sub(r"[^a-z0-9]+", "-", slug.lower()).strip("-")[:60] or "guide"
        d = self.ws / "guides" / category
        d.mkdir(parents=True, exist_ok=True)
        fm = ["---", f"title: {title}", f"category: {category}", f"tags: [{', '.join(tags or [])}]",
              f"summary: {summary}", f"related: [{', '.join(related or [])}]", "---", ""]
        (d / f"{slug}.md").write_text("\n".join(fm) + body.strip() + "\n")
        self.write_toc()
        return f"{category}/{slug}"

    def save_part(self, module: str, code: str) -> list[str]:
        """Write <workspace>/kit/parts/<module>.py (functions decorated with @component) after checking it loads."""
        if not self.ws:
            raise KitError("no workspace kit")
        module = re.sub(r"[^a-z0-9_]+", "_", module.lower()).strip("_")[:40] or "parts"
        d = self.ws / "parts"
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".check_{module}.py"
        tmp.write_text(code)
        try:
            loaded = _load_workspace_components_file(tmp)
        finally:
            tmp.unlink(missing_ok=True)
        if not loaded:
            raise KitError("the module registered no components: decorate each function with @component(category=..., summary=...)")
        builtin = {e.id for e in _builtin_components()}
        clash = [e.id for e in loaded if e.id in builtin]
        if clash:
            raise KitError(f"these names are built-in kit components already: {clash}; pick other names")
        (d / f"{module}.py").write_text(code)
        self._ws_cache = None
        self.write_toc()
        return [e.id for e in loaded]

    # ---- the object scripts see as `kit`
    def namespace(self) -> "ScriptKit":
        return ScriptKit(self)


def _load_workspace_components_file(p: Path) -> list[Entry]:
    import cad_kernel as ck
    _PENDING.clear()
    ns = ck.script_namespace()
    ns["component"] = component
    ns["place"] = place
    try:
        exec(compile(p.read_text(), str(p), "exec"), ns)
    except Exception as ex:
        _PENDING.clear()
        raise KitError(f"part module failed to load: {type(ex).__name__}: {ex}")
    out = list(_PENDING)
    _PENDING.clear()
    return out


class ScriptKit:
    """`kit` inside a design script: kit.<component>(...), kit.place(obj, loc), kit.search("...")."""

    def __init__(self, k: Kit):
        self._kit = k
        self._fns = {e.id: e.fn for e in k.entries() if e.kind == "component" and e.fn is not None}

    def __getattr__(self, name: str) -> Any:
        fns = self.__dict__.get("_fns", {})
        if name in fns:
            return fns[name]
        close = [n for n in fns if name.lower() in n.lower() or n.lower() in name.lower()]
        raise AttributeError(f"kit has no component '{name}'" + (f"; similar: {', '.join(sorted(close)[:6])}" if close else "")
                             + ". Use the kit tool (search/read) to find components.")

    def __dir__(self) -> list[str]:
        return sorted(self._fns) + ["place"]

    @staticmethod
    def place(obj: Any, loc: Any) -> Any:
        return place(obj, loc)
