"""Machine + tool libraries as JSON on disk (workspace/machines/*.json, workspace/tools.json)."""
from __future__ import annotations

import json
from dataclasses import asdict, fields
from pathlib import Path

from cam_kernel import Tool, Machine

DEFAULT_MACHINES = [
    Machine(name="Generic 3018", travel={"x": 300, "y": 180, "z": 45}, max_feed={"x": 1500, "y": 1500, "z": 400},
            rapid=1500, spindle={"min": 1000, "max": 10000}, safe_z=5, clearance_z=15,
            notes="Small desktop router. Light cuts: 0.5-1 mm stepdown in wood/plastic, ~0.3 mm in aluminium."),
    Machine(name="Shapeoko-class router", travel={"x": 800, "y": 800, "z": 80}, max_feed={"x": 5000, "y": 5000, "z": 1000},
            rapid=5000, spindle={"min": 8000, "max": 30000}, safe_z=5, clearance_z=20,
            notes="Trim-router spindle; GRBL 1.1 with Carbide/Shapeoko defaults."),
]

DEFAULT_TOOLS = [
    Tool(1, "6 mm flat endmill", "flat", 6.0, 2, 20, rpm=10000, feed=1200, plunge=300, stepdown=2.0, stepover=0.45),
    Tool(2, "3 mm flat endmill", "flat", 3.0, 2, 12, rpm=10000, feed=800, plunge=200, stepdown=1.0, stepover=0.4),
    Tool(3, '1/8" flat endmill', "flat", 3.175, 2, 12, rpm=10000, feed=800, plunge=200, stepdown=1.0, stepover=0.4),
    Tool(4, "6 mm ball endmill", "ball", 6.0, 2, 20, rpm=10000, feed=1000, plunge=300, stepdown=1.5, stepover=0.15),
    Tool(5, "3 mm drill", "drill", 3.0, 2, 30, rpm=8000, feed=300, plunge=150, stepdown=3.0, stepover=1.0, angle=118),
    Tool(6, "90° V-bit", "vbit", 12.7, 2, 10, rpm=10000, feed=800, plunge=200, stepdown=1.0, stepover=0.3, angle=90),
]


class Library:
    def __init__(self, workspace: Path):
        self.machines_dir = workspace / "machines"
        self.tools_path = workspace / "tools.json"
        self.machines_dir.mkdir(parents=True, exist_ok=True)
        if not any(self.machines_dir.glob("*.json")):
            for m in DEFAULT_MACHINES:
                self.save_machine(asdict(m))
        if not self.tools_path.exists():
            self.tools_path.write_text(json.dumps([asdict(t) for t in DEFAULT_TOOLS], indent=2))

    # ---- machines
    def machines(self) -> dict[str, Machine]:
        out = {}
        for p in sorted(self.machines_dir.glob("*.json")):
            try:
                out_m = _machine_from(json.loads(p.read_text()))
                out[out_m.name] = out_m
            except Exception:
                continue
        return out

    def save_machine(self, data: dict) -> Machine:
        m = _machine_from(data)
        (self.machines_dir / f"{_slug(m.name)}.json").write_text(json.dumps(asdict(m), indent=2))
        return m

    def delete_machine(self, name: str) -> bool:
        p = self.machines_dir / f"{_slug(name)}.json"
        if p.exists():
            p.unlink(); return True
        return False

    # ---- tools
    def tools(self) -> list[Tool]:
        try:
            return [_tool_from(t) for t in json.loads(self.tools_path.read_text())]
        except Exception:
            return []

    def tool_map(self) -> dict:
        """Lookup by number, by name, and by 'T<number>'."""
        m: dict = {}
        for t in self.tools():
            m[t.number] = t; m[t.name] = t; m[f"T{t.number}"] = t
        return m

    def save_tool(self, data: dict) -> Tool:
        tools = self.tools()
        t = _tool_from(data)
        tools = [x for x in tools if x.number != t.number] + [t]
        tools.sort(key=lambda x: x.number)
        self.tools_path.write_text(json.dumps([asdict(x) for x in tools], indent=2))
        return t

    def delete_tool(self, number: int) -> bool:
        tools = self.tools()
        keep = [x for x in tools if x.number != number]
        self.tools_path.write_text(json.dumps([asdict(x) for x in keep], indent=2))
        return len(keep) != len(tools)

    def describe(self) -> str:
        lines = ["Machines:"]
        for m in self.machines().values():
            lines.append(f"  - {m.name}: travel {m.travel} mm, max feed {m.max_feed}, rapid {m.rapid}, spindle {m.spindle} rpm, "
                         f"tool_change={m.tool_change}, safe_z={m.safe_z}, clearance_z={m.clearance_z}"
                         + (f" — {m.notes}" if m.notes else ""))
        lines.append("Tools:")
        for t in self.tools():
            lines.append(f"  - T{t.number} '{t.name}': {t.type} Ø{t.diameter} mm, {t.flutes}F, flute length {t.flute_length}, "
                         f"rpm {t.rpm}, feed {t.feed}, plunge {t.plunge}, stepdown {t.stepdown}, stepover {t.stepover}"
                         + (f", angle {t.angle}°" if t.angle else "") + (f" — {t.notes}" if t.notes else ""))
        return "\n".join(lines)


def _filter(cls, data: dict) -> dict:
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in names}


def _machine_from(data: dict) -> Machine:
    d = _filter(Machine, data)
    if "name" not in d:
        raise ValueError("machine needs a name")
    return Machine(**d)


def _tool_from(data: dict) -> Tool:
    d = _filter(Tool, data)
    if "number" not in d or "name" not in d:
        raise ValueError("tool needs number and name")
    d["number"] = int(d["number"])
    return Tool(**d)


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in name.strip()).strip("-").lower() or "machine"
