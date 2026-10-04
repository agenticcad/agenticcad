"""Machine + tool libraries as JSON on disk (workspace/machines/*.json, workspace/tools.json)."""
from __future__ import annotations

import json
from dataclasses import asdict, fields
from pathlib import Path

from cam_kernel import Tool, Machine

DEFAULT_MACHINES = [
    Machine(name="Generic 3018", travel={"x": 300, "y": 180, "z": 45}, max_feed={"x": 1500, "y": 1500, "z": 400},
            rapid=1500, spindle={"min": 1000, "max": 10000}, safe_z=5, clearance_z=15,
            max_stepdown={"aluminium": 0.3, "*": 1.0},
            notes="Small desktop router. Light cuts: 0.5-1 mm stepdown in wood/plastic, ~0.3 mm in aluminium."),
    Machine(name="Shapeoko-class router", travel={"x": 800, "y": 800, "z": 80}, max_feed={"x": 5000, "y": 5000, "z": 1000},
            rapid=5000, spindle={"min": 8000, "max": 30000}, safe_z=5, clearance_z=20,
            notes="Trim-router spindle; GRBL 1.1 with Carbide/Shapeoko defaults."),
] + [
    # Makera machines. Sources: makera.com product pages (work area, 4th axis size, spindle, collets), the Makera Z1
    # firmware (github.com/MakeraInc/MakeraZ1Firmware: configZ1.default max rates 1200/1200/600 mm/min, A max
    # 3600 deg/min or 1800 with A homing, Y soft limit -160 with the 4th axis), core-electronics.com.au (Air 4000 mm/min).
    Machine(name="Makera Z1", controller="makera", post="makera", travel={"x": 200, "y": 200, "z": 100},
            max_feed={"x": 1200, "y": 1200, "z": 600}, rapid=1200, spindle={"min": 0, "max": 13000},
            tool_change="manual", safe_z=5, clearance_z=15, collet=3.175, max_stepdown={"aluminium": 0.8},
            notes="Makera Z1 desktop CNC: 150 W spindle 0-13,000 rpm, 1/8\" collet as standard (other collets "
                  "available), manual quick tool change: M6 Tn moves to the change position, waits for the button and "
                  "measures the tool length. Firmware max rates X/Y 1200, Z 600 mm/min. Makera: aluminium < 1 mm per "
                  "pass; not for ferrous metals. Programs must be <= 63 characters per line (the post enforces it)."),
    Machine(name="Makera Z1 + 4th axis", controller="makera", post="makera", travel={"x": 200, "y": 159, "z": 100},
            max_feed={"x": 1200, "y": 1200, "z": 600}, rapid=1200, spindle={"min": 0, "max": 13000},
            tool_change="manual", safe_z=5, clearance_z=15, collet=3.175, max_stepdown={"aluminium": 0.8},
            rotary={"axis": "A", "about": "x", "max_diameter": 80, "max_length": 150, "max_speed": 1800, "installed": True},
            notes="Makera Z1 with the 4th axis module (Ø80 x 150 mm, ~2.5 Nm). A rotates about X; the work origin's "
                  "Y and Z must be on the rotary centreline (Makera's 4th-axis probing sets Z0 on the axis). With the "
                  "module fitted the firmware limits Y to about 159 mm. A max 1800 deg/min used (3600 without A homing)."),
    Machine(name="Carvera Air", controller="makera", post="makera", travel={"x": 300, "y": 200, "z": 130},
            max_feed={"x": 4000, "y": 4000, "z": 2000}, rapid=4000, spindle={"min": 0, "max": 13000},
            tool_change="manual", safe_z=5, clearance_z=15, collet=3.175,
            notes="Makera Carvera Air: 200 W spindle 0-13,000 rpm, 1/8\" collet integrated (1/4\", 6 mm, 4 mm "
                  "optional), manual quick tool changer, ball screws, max travel 4000 mm/min. Same Makera firmware "
                  "family as the Z1 (M6 Tn change + automatic tool length, 63-character lines)."),
    Machine(name="Carvera Air + 4th axis", controller="makera", post="makera", travel={"x": 300, "y": 200, "z": 130},
            max_feed={"x": 4000, "y": 4000, "z": 2000}, rapid=4000, spindle={"min": 0, "max": 13000},
            tool_change="manual", safe_z=5, clearance_z=15, collet=3.175,
            rotary={"axis": "A", "about": "x", "max_diameter": 92, "max_length": 200, "max_speed": 2400, "installed": True},
            notes="Carvera Air with the harmonic-drive 4th axis module (Ø92 x 200 mm, ~10 Nm, 2400 deg/min). Work "
                  "origin Y/Z on the rotary centreline."),
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
        # seed each built-in machine once per workspace (so new built-ins appear in old workspaces, but a machine the
        # user deleted stays deleted)
        seeded_p = self.machines_dir / ".seeded.json"
        try:
            seeded = set(json.loads(seeded_p.read_text())) if seeded_p.exists() else set()
        except Exception:
            seeded = set()
        if not seeded:
            seeded = {m.name for m in self.machines().values()}
        for m in DEFAULT_MACHINES:
            if m.name not in seeded and not (self.machines_dir / f"{_slug(m.name)}.json").exists():
                self.save_machine(asdict(m))
            seeded.add(m.name)
        seeded_p.write_text(json.dumps(sorted(seeded)))
        # built-ins saved before a field existed get its default once (a value the user set, even {}, is kept)
        for m in DEFAULT_MACHINES:
            f = self.machines_dir / f"{_slug(m.name)}.json"
            if m.max_stepdown and f.exists():
                try:
                    d = json.loads(f.read_text())
                except Exception:
                    continue
                if d.get("name") == m.name and "max_stepdown" not in d:
                    d["max_stepdown"] = m.max_stepdown
                    f.write_text(json.dumps(d, indent=2))
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
            rot = (f", 4th axis {m.rotary.get('axis', 'A')} about X: Ø{m.rotary.get('max_diameter')} x {m.rotary.get('max_length')} mm, "
                   f"{m.rotary.get('max_speed')} deg/min") if m.rotary else ""
            lines.append(f"  - {m.name}: post {m.post}, travel {m.travel} mm, max feed {m.max_feed}, rapid {m.rapid}, spindle {m.spindle} rpm, "
                         f"tool_change={m.tool_change}, collet={m.collet or '-'}, safe_z={m.safe_z}, clearance_z={m.clearance_z}{rot}"
                         + (f", max stepdown {m.max_stepdown} mm" if m.max_stepdown else "")
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
