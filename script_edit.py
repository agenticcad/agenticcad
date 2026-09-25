"""
Small, safe edits to a design script without the agent: rename / delete a body or component.

Works when bodies are declared as dict keys, i.e.
    result = {"Bracket": bracket.part, "Pin": pin}
    result = {"Base": {"Bracket": ..., "Washer": ...}, "Pin": pin}
or  bodies = {...}; result = bodies  (one level of indirection).
Anything else (auto-named bodies, split solids, computed dicts) raises Unsupported so the
caller can hand the job to the agent instead.
"""
from __future__ import annotations

import re

import ast


class Unsupported(Exception):
    """The script shape isn't one we can edit mechanically — hand it to the agent."""


class Refused(Unsupported):
    """The edit is not allowed at all (would leave an empty design, name clash, bad name)."""


def _lines_offsets(code: str) -> list[int]:
    offs = [0]
    for line in code.splitlines(keepends=True):
        offs.append(offs[-1] + len(line))
    return offs


def _pos(offs: list[int], line: int, col: int) -> int:
    # ast columns are byte offsets in the utf-8 encoding; convert per line
    return offs[line - 1] + col


def _find_result_dict(tree: ast.Module) -> ast.Dict:
    """Return the ast.Dict assigned (directly or via one variable) to `result`."""
    assigns: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            assigns[node.targets[0].id] = node.value
    if "result" not in assigns:
        raise Unsupported("no top-level `result = ...` assignment")
    value = assigns["result"]
    if isinstance(value, ast.Name) and value.id in assigns:
        value = assigns[value.id]
    if not isinstance(value, ast.Dict):
        raise Unsupported("`result` is not a dict literal of named bodies")
    return value


def _walk_path(d: ast.Dict, path: str) -> tuple[ast.Dict, int]:
    """Return (dict node, index of key) for a 'Comp/Sub/Body' path."""
    parts = path.split("/")
    node = d
    for depth, seg in enumerate(parts):
        idx = None
        for i, k in enumerate(node.keys):
            if isinstance(k, ast.Constant) and isinstance(k.value, str) and k.value == seg:
                idx = i
                break
        if idx is None:
            raise Unsupported(f"'{seg}' is not a literal key in the result dict")
        if depth == len(parts) - 1:
            return node, idx
        child = node.values[idx]
        if not isinstance(child, ast.Dict):
            raise Unsupported(f"'{seg}' is not a component (nested dict)")
        node = child
    raise Unsupported("empty path")


def rename(code: str, path: str, new_name: str) -> str:
    new_name = new_name.strip()
    if not new_name or "/" in new_name or '"' in new_name or "\\" in new_name:
        raise Refused("invalid name")
    tree = ast.parse(code)
    d = _find_result_dict(tree)
    node, idx = _walk_path(d, path)
    for k in node.keys:
        if isinstance(k, ast.Constant) and k.value == new_name:
            raise Refused(f"a body named '{new_name}' already exists at that level")
    key = node.keys[idx]
    b = code.encode("utf-8")
    boffs = [0]
    for line in b.splitlines(keepends=True):
        boffs.append(boffs[-1] + len(line))
    start = boffs[key.lineno - 1] + key.col_offset
    end = boffs[key.end_lineno - 1] + key.end_col_offset
    return (b[:start] + ('"' + new_name + '"').encode("utf-8") + b[end:]).decode("utf-8")


def delete(code: str, path: str) -> str:
    tree = ast.parse(code)
    d = _find_result_dict(tree)
    node, idx = _walk_path(d, path)
    if len(node.keys) == 1:
        raise Refused("cannot delete the last body at that level (delete its component instead, or start a new design)")
    key, val = node.keys[idx], node.values[idx]
    b = code.encode("utf-8")
    boffs = [0]
    for line in b.splitlines(keepends=True):
        boffs.append(boffs[-1] + len(line))
    start = boffs[key.lineno - 1] + key.col_offset
    end = boffs[val.end_lineno - 1] + val.end_col_offset
    # swallow the following comma (and whitespace) if present, else the preceding comma
    tail = b[end:]
    stripped = tail.lstrip(b" \t")
    if stripped.startswith(b","):
        end += len(tail) - len(stripped) + 1
        rest = b[end:]
        rest_stripped = rest.lstrip(b" \t")
        if rest_stripped.startswith(b"\n") or rest_stripped.startswith(b"\r"):
            end += len(rest) - len(rest_stripped)
        # remove a now-empty line remainder
        if b[start:end].endswith(b",") and b[end:end + 1] in (b"\n", b"\r"):
            end += 1
            # also strip leading indentation of the removed line
            ls = b.rfind(b"\n", 0, start) + 1
            if b[ls:start].strip() == b"":
                start = ls
    else:
        head = b[:start].rstrip(b" \t\r\n")
        if head.endswith(b","):
            start = len(head) - 1
    return (b[:start] + b[end:]).decode("utf-8")


# ---------------------------------------------------------------------------
# Parameters: top-level `name = <number>` (and tuple) assignments
# ---------------------------------------------------------------------------
def _num_of(node):
    """Numeric value of a Constant / -Constant node, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        v = _num_of(node.operand)
        return -v if v is not None else None
    return None


_UNIT_NAMES = {"inch": "in", "IN": "in", "mm": "mm"}


def _num_unit_of(node):
    """(value, unit, literal_node) for `2.5`, `-2.5`, `2.5 * inch`, `inch * 2.5`; unit 'mm' for a bare number. None otherwise."""
    v = _num_of(node)
    if v is not None:
        return v, "mm", node
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        for lit, name in ((node.left, node.right), (node.right, node.left)):
            if isinstance(name, ast.Name) and name.id in _UNIT_NAMES and _num_of(lit) is not None:
                return _num_of(lit), _UNIT_NAMES[name.id], lit
    return None


def params(code: str) -> list[dict]:
    """Editable numeric parameters: top-level assignments of numeric literals.
    Returns [{name, value, line, comment}] in source order."""
    tree = ast.parse(code)
    lines = code.splitlines()
    out = []
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        tgt, val = node.targets[0], node.value
        comment = ""
        src_line = lines[node.lineno - 1] if node.lineno - 1 < len(lines) else ""
        if "#" in src_line:
            comment = src_line.split("#", 1)[1].strip()
        if isinstance(tgt, ast.Name):
            nu = _num_unit_of(val)
            if nu is not None:
                out.append({"name": tgt.id, "value": nu[0], "unit": nu[1], "line": node.lineno, "comment": comment})
        elif isinstance(tgt, ast.Tuple) and isinstance(val, ast.Tuple) and len(tgt.elts) == len(val.elts):
            for t_, v_ in zip(tgt.elts, val.elts):
                nu = _num_unit_of(v_)
                if isinstance(t_, ast.Name) and nu is not None:
                    out.append({"name": t_.id, "value": nu[0], "unit": nu[1], "line": node.lineno, "comment": comment})
    return out


def set_params(code: str, values: dict) -> str:
    """Rewrite the numeric literals of the given top-level parameters in place."""
    tree = ast.parse(code)
    b = code.encode("utf-8")
    boffs = [0]
    for line in b.splitlines(keepends=True):
        boffs.append(boffs[-1] + len(line))
    edits = []   # (start, end, text)
    seen: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        tgt, val = node.targets[0], node.value
        pairs = []
        if isinstance(tgt, ast.Name):
            pairs = [(tgt.id, val)]
        elif isinstance(tgt, ast.Tuple) and isinstance(val, ast.Tuple):
            pairs = [(t_.id, v_) for t_, v_ in zip(tgt.elts, val.elts) if isinstance(t_, ast.Name)]
        for name, vnode in pairs:
            nu = _num_unit_of(vnode)
            if name not in values or nu is None:
                continue
            seen.add(name)
            new = values[name]
            lit = nu[2]                                   # only the number changes; `* inch` stays
            new_txt = _fmt_num(new, isinstance(nu[0], int) and float(new).is_integer())
            start = boffs[lit.lineno - 1] + lit.col_offset
            end = boffs[lit.end_lineno - 1] + lit.end_col_offset
            edits.append((start, end, new_txt.encode("utf-8")))
    missing = [k for k in values if k not in seen]
    if missing:
        raise Refused(f"no numeric top-level parameter named {', '.join(missing)}; parameters: "
                      + ", ".join(p['name'] for p in params(code)))
    for start, end, txt in sorted(edits, key=lambda e: -e[0]):
        b = b[:start] + txt + b[end:]
    return b.decode("utf-8")


def _fmt_num(v, as_int: bool) -> str:
    if as_int:
        return str(int(round(float(v))))
    s = f"{float(v):.6g}"
    return s if ("." in s or "e" in s) else s + ".0"


# ---------------------------------------------------------------------------
# Add a body: `var = expr` before `result = ...`, and register it in the result dict
# ---------------------------------------------------------------------------
def add_body(code: str, var: str, expr: str, body_name: str) -> str:
    tree = ast.parse(code)
    lines = code.splitlines(keepends=True)
    result_node = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id == "result":
            result_node = node
    if result_node is None:
        code2 = code.rstrip("\n") + f"\n{var} = {expr}\nresult = {{\"{body_name}\": {var}}}\n"
        return code2
    val = result_node.value
    insert_at = result_node.lineno - 1                       # 0-based line index of `result = ...`
    if isinstance(val, ast.Dict):
        # append key inside the dict literal (before its closing brace)
        b = code.encode("utf-8")
        boffs = [0]
        for line in b.splitlines(keepends=True):
            boffs.append(boffs[-1] + len(line))
        end = boffs[val.end_lineno - 1] + val.end_col_offset - 1   # position of the closing '}'
        head = b[:end].rstrip()
        sep = b"" if head.endswith(b"{") else (b"," if not head.endswith(b",") else b"")
        multiline = val.lineno != val.end_lineno
        entry = (sep + (b"\n    " if multiline else b" ") + f'"{body_name}": {var}'.encode("utf-8") + (b",\n" if multiline else b""))
        b = head + entry + b[end:]
        code = b.decode("utf-8")
        lines = code.splitlines(keepends=True)
    else:
        seg = ast.get_source_segment(code, val) or "result"
        lines[insert_at] = f'result = {{"Body1": {seg}, "{body_name}": {var}}}\n'
    lines.insert(insert_at, f"{var} = {expr}\n")
    return "".join(lines)


# ---------------------------------------------------------------------------
# Sketches: `# sketch:NAME {json}` ... `# /sketch:NAME` blocks generated from the UI's 2D editor
# ---------------------------------------------------------------------------
import json as _json
import re as _re

_SK_HEAD = _re.compile(r"^# sketch:([A-Za-z_][A-Za-z0-9_]*) (\{.*\})\s*$", _re.M)


def _f(v: float) -> str:
    return f"{float(v):.4g}" if abs(float(v)) >= 1e-9 else "0"


def sketch_code(name: str, plane: dict, items: list[dict]) -> str:
    """build123d BuildSketch block for the editor's plane + items (coordinates are plane-local mm)."""
    o, x, z = plane["origin"], plane["x_dir"], plane["z_dir"]
    lines = [f"# sketch:{name} " + _json.dumps({"plane": plane, "items": items}, separators=(",", ":")),
             f"with BuildSketch(Plane(origin=({_f(o[0])}, {_f(o[1])}, {_f(o[2])}), x_dir=({_f(x[0])}, {_f(x[1])}, {_f(x[2])}), "
             f"z_dir=({_f(z[0])}, {_f(z[1])}, {_f(z[2])}))) as _{name}:"]
    for it in items:
        try:
            _sketch_item(lines, it)
        except KeyError as e:
            raise Unsupported(f"sketch item {it.get('type')!r} is missing field {e}; expected rect(cx,cy,w,h,angle) | "
                              f"circle(cx,cy,r) | polygon(pts) | slot(x1,y1,x2,y2,w)")
    if not items:
        lines.append("    pass")
    lines.append(f"{name} = _{name}.sketch")
    lines.append(f"# /sketch:{name}")
    return "\n".join(lines) + "\n"


def _sketch_item(lines: list[str], it: dict) -> None:
    if True:
        mode = ", mode=Mode.SUBTRACT" if it.get("mode") == "subtract" else ""
        t = it.get("type")
        if t == "rect":
            rot = f", rotation={_f(it['angle'])}" if it.get("angle") else ""
            lines.append(f"    with Locations(({_f(it['cx'])}, {_f(it['cy'])})):")
            lines.append(f"        Rectangle({_f(it['w'])}, {_f(it['h'])}{rot}{mode})")
        elif t == "circle":
            lines.append(f"    with Locations(({_f(it['cx'])}, {_f(it['cy'])})):")
            lines.append(f"        Circle({_f(it['r'])}{mode})")
        elif t == "polygon":
            pts = ", ".join(f"({_f(px)}, {_f(py)})" for px, py in it["pts"])
            lines.append(f"    Polygon({pts}, align=None{mode})")
        elif t == "slot":   # editor gives the two end centres; build123d wants centre + one end
            mx, my = (it["x1"] + it["x2"]) / 2, (it["y1"] + it["y2"]) / 2
            lines.append(f"    SlotCenterPoint(({_f(mx)}, {_f(my)}), ({_f(it['x2'])}, {_f(it['y2'])}), {_f(it['w'])}{mode})")
        else:
            raise Unsupported(f"unknown sketch item type {t!r}")


def sketches(code: str) -> list[dict]:
    """[{name, plane, items}] for every sketch block in the script."""
    out = []
    for m in _SK_HEAD.finditer(code):
        try:
            d = _json.loads(m.group(2))
            out.append({"name": m.group(1), "plane": d.get("plane"), "items": d.get("items", [])})
        except Exception:
            continue
    return out


def _block_span(code: str, name: str):
    m = _re.search(rf"^# sketch:{_re.escape(name)} .*$", code, _re.M)
    if not m:
        return None
    end = _re.search(rf"^# /sketch:{_re.escape(name)}\s*$", code[m.start():], _re.M)
    if not end:
        return None
    e = m.start() + end.end()
    if e < len(code) and code[e] == "\n":
        e += 1
    return m.start(), e


def set_sketch(code: str, name: str, plane: dict, items: list[dict]) -> str:
    """Replace the named sketch block, or insert a new one before the top-level `result = ...`."""
    if not _re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
        raise Refused("sketch name must be a Python identifier")
    block = sketch_code(name, plane, items)
    span = _block_span(code, name)
    if span:
        return code[:span[0]] + block + code[span[1]:]
    tree = ast.parse(code)
    lines = code.splitlines(keepends=True)
    result_line = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "result":
            result_line = node.lineno - 1
    if result_line is None:
        return code.rstrip("\n") + "\n\n" + block
    lines.insert(result_line, block + "\n")
    return "".join(lines)


def remove_sketch(code: str, name: str) -> str:
    span = _block_span(code, name)
    if not span:
        raise Unsupported(f"no sketch block named {name!r}")
    return code[:span[0]] + code[span[1]:]


def body_expr(code: str, path: str) -> str:
    """Source of the expression bound to a body in the result dict (e.g. 'bp.part')."""
    tree = ast.parse(code)
    d = _find_result_dict(tree)
    node, idx = _walk_path(d, path)
    return ast.get_source_segment(code, node.values[idx]) or "None"


def wrap_body_expr(code: str, path: str, template: str) -> str:
    """Rewrite a body's expression in the result dict. The template uses {expr} (the current expression,
    inlined) and/or {body} (the current body bound to a variable, for templates that reference it more than
    once, e.g. '{body} + extrude({body}.faces().sort_by_distance((0, 0, 4))[0], amount=6)').
    Works for any expression (bp.part, from_library(...), a name)."""
    tree = ast.parse(code)
    d = _find_result_dict(tree)
    node, idx = _walk_path(d, path)
    val = node.values[idx]
    b = code.encode("utf-8")
    boffs = [0]
    for line in b.splitlines(keepends=True):
        boffs.append(boffs[-1] + len(line))
    start = boffs[val.lineno - 1] + val.col_offset
    end = boffs[val.end_lineno - 1] + val.end_col_offset
    old = b[start:end].decode("utf-8")
    if "{body}" in template:
        if isinstance(val, ast.Name):
            var = val.id
            hoist = ""
        else:
            base = "_" + _re.sub(r"[^a-z0-9]+", "_", path.lower()).strip("_") or "_body"
            var, k = base, 2
            while _re.search(rf"\b{_re.escape(var)}\b", code):
                var = f"{base}{k}"; k += 1
            hoist = f"{var} = {old}\n"
        new = template.replace("{body}", var).replace("{expr}", var)
        out = (b[:start] + new.encode("utf-8") + b[end:]).decode("utf-8")
        if hoist:
            # insert the binding just before the top-level `result = ...` statement
            rnode = next(n for n in ast.parse(out).body if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "result")
            lines = out.splitlines(keepends=True)
            lines.insert(rnode.lineno - 1, hoist)
            out = "".join(lines)
        return out
    new = template.replace("{expr}", old)
    return (b[:start] + new.encode("utf-8") + b[end:]).decode("utf-8")


_RESULT_LINE = re.compile(r"^result\s*=", re.M)


def apply_edits(code: str, edits: list[dict], append: str = "") -> str:
    """Exact-text replacements (each `old` must occur exactly once) applied in order, then `append` inserted before the
    final top-level `result = ...` line (or at the end). Raises Refused on ambiguity, no match, or no change."""
    out = code
    for i, e in enumerate(edits):
        old, new = str(e.get("old", "")), str(e.get("new", ""))
        if not old:
            raise Refused(f"edit {i + 1}: `old` is empty")
        n = out.count(old)
        if n == 0:
            hint = " (leading/trailing whitespace or indentation differs?)" if old.strip() and out.count(old.strip()) else ""
            raise Refused(f"edit {i + 1}: `old` not found in the current script{hint}; call get_code and copy the text exactly")
        if n > 1:
            raise Refused(f"edit {i + 1}: `old` occurs {n} times; include more surrounding lines so it is unique")
        out = out.replace(old, new, 1)
    if append and append.strip():
        block = append.strip("\n") + "\n"
        ms = list(_RESULT_LINE.finditer(out))
        if ms:
            pos = ms[-1].start()
            out = out[:pos] + block + ("\n" if not block.endswith("\n\n") else "") + out[pos:]
        else:
            out = out.rstrip("\n") + "\n\n" + block
    if out == code:
        raise Refused("no change: the edits leave the script identical")
    return out
