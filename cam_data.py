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
    # Haas vertical machining centres (standard configurations; options such as spindle speed and tool changer size
    # vary by order). Travels, spindle and rapids from haascnc.com spec sheets as listed by dealers (2026): VF-2 30x16x20 in,
    # 8100 rpm, 1000 ipm, 20-pocket carousel; VF-2SS 12000 rpm, 1400 ipm, 24+1 side-mount; VF-4 50x20x25 in; Mini Mill
    # 16x12x10 in, 6000 rpm, 600 ipm, 10 tools; TM-1 30x12x16 in, 4000 rpm, 200 ipm feeds and rapids. CAT 40 taper.
    Machine(name="Haas VF-2", controller="haas", post="haas", travel={"x": 762, "y": 406, "z": 508},
            max_feed={"x": 12700, "y": 12700, "z": 12700}, rapid=25400, spindle={"min": 1, "max": 8100},
            tool_change="atc", coolant=True, safe_z=5, clearance_z=25, collet=20,
            notes="Haas VF-2 (standard): 30 x 16 x 20 in travels, CAT 40, 30 hp vector drive, 8,100 rpm, 1,000 ipm rapids, "
                  "20-pocket carousel changer, flood coolant. Program: % / O-number / ( ) comments; every number carries a "
                  "decimal point; Tn M06 then G43 Hn; G53 G0 Z0. retracts; M00 between setups. collet=20 assumes ER-32 "
                  "holders; side-lock holders take larger shanks. Verify G54 and tool offsets on the control before running."),
    Machine(name="Haas VF-2SS", controller="haas", post="haas", travel={"x": 762, "y": 406, "z": 508},
            max_feed={"x": 21000, "y": 21000, "z": 21000}, rapid=35560, spindle={"min": 1, "max": 12000},
            tool_change="atc", coolant=True, safe_z=5, clearance_z=25, collet=20,
            notes="Haas VF-2SS Super Speed: 30 x 16 x 20 in, CAT 40, 12,000 rpm inline direct-drive, 1,400 ipm rapids, "
                  "833 ipm cutting feed, 24+1 side-mount tool changer, flood coolant. Same program format as the VF-2."),
    Machine(name="Haas VF-4", controller="haas", post="haas", travel={"x": 1270, "y": 508, "z": 635},
            max_feed={"x": 12700, "y": 12700, "z": 12700}, rapid=25400, spindle={"min": 1, "max": 8100},
            tool_change="atc", coolant=True, safe_z=5, clearance_z=25, collet=20,
            notes="Haas VF-4 (standard): 50 x 20 x 25 in travels, CAT 40, 8,100 rpm, 1,000 ipm rapids, 20-pocket carousel, "
                  "flood coolant. Same program format as the VF-2."),
    Machine(name="Haas Mini Mill", controller="haas", post="haas", travel={"x": 406, "y": 305, "z": 254},
            max_feed={"x": 15240, "y": 15240, "z": 15240}, rapid=15240, spindle={"min": 1, "max": 6000},
            tool_change="atc", coolant=True, safe_z=5, clearance_z=25, collet=20,
            notes="Haas Mini Mill: 16 x 12 x 10 in travels, CAT 40, 7.5 hp, 6,000 rpm (10,000 rpm option), 600 ipm rapids, "
                  "10-pocket tool changer, flood coolant. Same program format as the VF-2."),
    Machine(name="Haas TM-1", controller="haas", post="haas", travel={"x": 762, "y": 305, "z": 406},
            max_feed={"x": 5080, "y": 5080, "z": 5080}, rapid=5080, spindle={"min": 1, "max": 4000},
            tool_change="pause", coolant=True, safe_z=5, clearance_z=25, collet=20,
            notes="Haas TM-1 Toolroom Mill: 30 x 12 x 16 in travels, CAT 40, 7.5 hp, 4,000 rpm (6,000 option), 200 ipm feeds "
                  "and rapids. No tool changer as standard (10-pocket option): the post stops with M00 before each Tn M06 so "
                  "you can swap the holder; set tool_change to atc if yours has the changer."),
    # LinuxCNC (rs274ngc). Generic envelopes: edit travel, feeds and spindle to match your machine.
    Machine(name="LinuxCNC mill", controller="linuxcnc", post="linuxcnc", travel={"x": 600, "y": 400, "z": 200},
            max_feed={"x": 5000, "y": 5000, "z": 2500}, rapid=6000, spindle={"min": 100, "max": 24000},
            tool_change="atc", coolant=False, safe_z=5, clearance_z=20,
            notes="Generic LinuxCNC 3-axis mill or router (rs274ngc dialect): % wrapper, ( ) comments, (MSG, ...) operator "
                  "messages, G64 P0.01 path blending, Tn M6 then G43 Hn (with hal_manualtoolchange LinuxCNC prompts at "
                  "M6; with an ATC it changes), G53 G0 Z0 retracts, G93 inverse-time feed for rotary moves, M2. Edit the "
                  "travels, feeds and spindle range to match your machine; set coolant if you have M8."),
    Machine(name="LinuxCNC mill + 4th axis", controller="linuxcnc", post="linuxcnc", travel={"x": 600, "y": 400, "z": 200},
            max_feed={"x": 5000, "y": 5000, "z": 2500}, rapid=6000, spindle={"min": 100, "max": 24000},
            tool_change="atc", coolant=False, safe_z=5, clearance_z=20,
            rotary={"axis": "A", "about": "x", "max_diameter": 150, "max_length": 300, "max_speed": 7200, "installed": True},
            notes="LinuxCNC mill with an A-axis rotary table along X (edit the sizes). Rotary moves use G93 inverse-time "
                  "feed so the true tool path runs at the programmed feed; indexed setups use plain G0 A moves."),
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
