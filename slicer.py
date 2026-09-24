"""3D-printing hand-off: drive an installed slicer (OrcaSlicer, or Bambu Studio which shares its CLI) headlessly,
and parse the resulting G-code into layers the viewer can draw. Nothing here is bundled: if no slicer is installed
the feature is simply absent (no tool, no UI section).

OrcaSlicer's CLI wants *flattened* profiles (its system profiles use `inherits` chains), so we resolve the chain
ourselves, apply the user's overrides (layer height, infill, supports, brim, walls) on top of a real process profile,
and call `--slice 0 --arrange 1 --export-3mf`. The 3MF carries the object transform, so the G-code can be mapped
back into model coordinates for the viewer.
"""
from __future__ import annotations

import glob
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


class SlicerError(Exception):
    pass


@dataclass
class SlicerInfo:
    name: str                     # OrcaSlicer | BambuStudio
    exe: str
    profiles_dir: str             # <resources>/profiles
    user_dir: str | None          # .../user/default (may not exist)
    conf: str | None              # OrcaSlicer.conf / BambuStudio.conf
    version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _candidates() -> list[tuple[str, str, str, str, str]]:
    """(name, exe, profiles_dir, user_dir, conf) for each known install location on this OS."""
    home = Path.home()
    out = []
    if sys.platform == "darwin":
        for name, app, cfg in (("OrcaSlicer", "OrcaSlicer", "OrcaSlicer"), ("BambuStudio", "BambuStudio", "BambuStudio")):
            for base in (Path("/Applications"), home / "Applications"):
                a = base / f"{app}.app"
                out.append((name, str(a / "Contents" / "MacOS" / app), str(a / "Contents" / "Resources" / "profiles"),
                            str(home / "Library" / "Application Support" / cfg / "user" / "default"),
                            str(home / "Library" / "Application Support" / cfg / f"{cfg}.conf")))
    elif sys.platform == "win32":
        pf = Path(os.environ.get("ProgramFiles", r"C:\Program Files")); appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        for name, folder, exe, cfg in (("OrcaSlicer", "OrcaSlicer", "orca-slicer.exe", "OrcaSlicer"), ("BambuStudio", "Bambu Studio", "bambu-studio.exe", "BambuStudio")):
            for base in (pf, Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local")) / "Programs"):
                out.append((name, str(base / folder / exe), str(base / folder / "resources" / "profiles"),
                            str(appdata / cfg / "user" / "default"), str(appdata / cfg / f"{cfg}.conf")))
    else:
        for name, exe, cfg in (("OrcaSlicer", "orca-slicer", "OrcaSlicer"), ("BambuStudio", "bambu-studio", "BambuStudio")):
            w = shutil.which(exe)
            if w:
                res = Path(w).resolve().parent.parent / "share" / cfg / "profiles"
                out.append((name, w, str(res), str(home / ".config" / cfg / "user" / "default"), str(home / ".config" / cfg / f"{cfg}.conf")))
    return out


_cache: dict[str, Any] = {}


def find_slicer(refresh: bool = False) -> SlicerInfo | None:
    """The installed slicer, or None. AGENTICCAD_SLICER=/path/to/exe[:profiles_dir] overrides detection."""
    if not refresh and "info" in _cache:
        return _cache["info"]
    info = None
    env = os.environ.get("AGENTICCAD_SLICER")
    if env:
        exe, _, prof = env.partition("::")
        if Path(exe).is_file():
            prof = prof or str(Path(exe).resolve().parent.parent / "Resources" / "profiles")
            info = SlicerInfo("OrcaSlicer", exe, prof, None, None)
    if info is None:
        for name, exe, prof, user, conf in _candidates():
            if Path(exe).is_file() and Path(prof).is_dir():
                info = SlicerInfo(name, exe, prof, user if Path(user).is_dir() else None, conf if Path(conf).is_file() else None)
                break
    if info is not None:
        try:
            r = subprocess.run([info.exe, "--help"], capture_output=True, text=True, timeout=20)
            m = re.search(r"(OrcaSlicer|BambuStudio|Bambu Studio)[- ]?([0-9][0-9.]*)", (r.stdout + r.stderr))
            info.version = m.group(2) if m else ""
        except Exception:  # noqa: BLE001
            pass
    _cache["info"] = info
    return info


# ---------------------------------------------------------------------------- profiles
def _index(info: SlicerInfo) -> dict[str, dict[str, str]]:
    """{kind: {profile name: path}} over the system vendors and the user's presets (user wins)."""
    key = ("index", info.profiles_dir, info.user_dir)
    if key in _cache:
        return _cache[key]
    idx: dict[str, dict[str, str]] = {"machine": {}, "process": {}, "filament": {}}
    for kind in idx:
        for p in glob.glob(os.path.join(info.profiles_dir, "*", kind, "*.json")):
            idx[kind][Path(p).stem] = p
        if info.user_dir:
            for p in glob.glob(os.path.join(info.user_dir, kind, "*.json")):
                idx[kind][Path(p).stem] = p
    _cache[key] = idx
    return idx


def _load(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def flatten(info: SlicerInfo, kind: str, name: str, _seen: tuple = ()) -> dict[str, Any]:
    """Resolve an Orca profile's `inherits` chain into one flat dict (child keys win)."""
    idx = _index(info)
    path = idx[kind].get(name)
    if path is None:
        raise SlicerError(f"no {kind} profile named '{name}'")
    d = _load(path)
    parent = d.pop("inherits", None)
    if parent and parent not in _seen and parent in idx[kind]:
        base = flatten(info, kind, parent, _seen + (parent,))
        base.update(d)
        d = base
    d.pop("inherits", None)
    d["type"] = kind
    return d


def _vendor_of(path: str, info: SlicerInfo) -> str:
    rel = os.path.relpath(path, info.profiles_dir)
    return "user" if rel.startswith("..") else rel.split(os.sep)[0]


def machines(info: SlicerInfo) -> list[dict[str, Any]]:
    """Instantiable printer profiles: name, vendor, model, nozzle."""
    out = []
    for name, path in sorted(_index(info)["machine"].items()):
        try:
            d = _load(path)
        except Exception:  # noqa: BLE001
            continue
        if str(d.get("instantiation", "false")).lower() != "true":
            continue
        out.append({"name": name, "vendor": _vendor_of(path, info), "model": d.get("printer_model", ""),
                    "nozzle": (d.get("nozzle_diameter") or [""])[0], "bed": d.get("printable_area"), "height": d.get("printable_height"),
                    "default_process": d.get("default_print_profile", ""), "default_filament": (d.get("default_filament_profile") or [""])[0]})
    return out


def default_machine(info: SlicerInfo) -> str | None:
    """The printer currently selected in the slicer's GUI, if its config is readable."""
    if info.conf and Path(info.conf).is_file():
        try:
            return (_load(info.conf).get("presets") or {}).get("machine") or None
        except Exception:  # noqa: BLE001
            return None
    return None


def profiles_for(info: SlicerInfo, machine: str) -> dict[str, Any]:
    """Process and filament profiles compatible with `machine` (Orca's compatible_printers lists; generic
    filaments with an empty list are compatible with everything)."""
    idx = _index(info)
    procs, fils = [], []
    for name, path in sorted(idx["process"].items()):
        try:
            d = _load(path)
        except Exception:  # noqa: BLE001
            continue
        if str(d.get("instantiation", "false")).lower() == "true" and machine in (d.get("compatible_printers") or []):
            procs.append({"name": name, "layer_height": d.get("layer_height")})
    for name, path in sorted(idx["filament"].items()):
        try:
            d = _load(path)
        except Exception:  # noqa: BLE001
            continue
        if str(d.get("instantiation", "false")).lower() != "true":
            continue
        comp = d.get("compatible_printers") or []
        if machine in comp or (not comp and _vendor_of(path, info) in ("OrcaFilamentLibrary", "user")):
            fils.append({"name": name, "type": (d.get("filament_type") or [""])[0] if isinstance(d.get("filament_type"), list) else d.get("filament_type") or "",
                         "vendor": _vendor_of(path, info)})
    return {"machine": machine, "processes": procs, "filaments": fils}


# ---------------------------------------------------------------------------- slicing
OVERRIDE_KEYS = {"layer_height": lambda v: f"{float(v):g}", "sparse_infill_density": lambda v: f"{int(round(float(str(v).rstrip('%'))))}%",
                 "enable_support": lambda v: "1" if str(v).lower() in ("1", "true", "yes", "on") else "0",
                 "brim_type": lambda v: str(v), "wall_loops": lambda v: str(int(v)), "top_shell_layers": lambda v: str(int(v)),
                 "bottom_shell_layers": lambda v: str(int(v)), "initial_layer_print_height": lambda v: f"{float(v):g}"}


@dataclass
class SliceResult:
    gcode: str                      # path
    three_mf: str                   # path
    machine: str
    process: str
    filament: str
    overrides: dict[str, str]
    layers: int = 0
    height_mm: float = 0.0
    time_s: float = 0.0
    filament_mm: float = 0.0
    filament_g: float = 0.0
    filament_cm3: float = 0.0
    translate: tuple[float, float, float] = (0.0, 0.0, 0.0)   # STL → bed
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        t = f"{int(self.time_s // 3600)}h {int(self.time_s % 3600 // 60)}m" if self.time_s >= 3600 else f"{int(self.time_s // 60)}m {int(self.time_s % 60)}s"
        ov = ", ".join(f"{k}={v}" for k, v in self.overrides.items()) or "profile defaults"
        return (f"Sliced for {self.machine} · {self.process} · {self.filament} ({ov}): {self.layers} layers, {self.height_mm:.2f} mm tall, "
                f"est. {t}, filament {self.filament_g:.1f} g / {self.filament_mm / 1000:.2f} m" + ("; warnings: " + "; ".join(self.warnings) if self.warnings else ""))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def slice_file(info: SlicerInfo, stl_path: str | Path, machine: str, process: str | None, filament: str | None,
               overrides: dict[str, Any] | None, out_dir: str | Path, timeout: float = 900) -> SliceResult:
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*"):
        if old.suffix in (".gcode", ".3mf", ".log", ".json", ".md5"):
            old.unlink()
    mach = flatten(info, "machine", machine)
    process = process or mach.get("default_print_profile") or ""
    filament = filament or (mach.get("default_filament_profile") or [""])[0] or ""
    if not process:
        raise SlicerError(f"machine '{machine}' has no default process profile; pass one")
    if not filament:
        raise SlicerError(f"machine '{machine}' has no default filament profile; pass one")
    proc = flatten(info, "process", process)
    fil = flatten(info, "filament", filament)
    applied: dict[str, str] = {}
    for k, v in (overrides or {}).items():
        if v is None or v == "":
            continue
        if k not in OVERRIDE_KEYS:
            raise SlicerError(f"unsupported override '{k}'; allowed: {', '.join(OVERRIDE_KEYS)}")
        applied[k] = OVERRIDE_KEYS[k](v)
        proc[k] = applied[k]
    if "layer_height" in applied and "initial_layer_print_height" not in applied:
        proc["initial_layer_print_height"] = applied["layer_height"]
    mach["name"], proc["name"], fil["name"] = machine, process, filament
    files = {}
    for kind, d in (("machine", mach), ("process", proc), ("filament", fil)):
        p = out_dir / f"{kind}.json"; p.write_text(json.dumps(d, indent=1)); files[kind] = str(p)
    three_mf = out_dir / "sliced.3mf"
    cmd = [info.exe, "--load-settings", f"{files['machine']};{files['process']}", "--load-filaments", files["filament"],
           "--slice", "0", "--arrange", "1", "--export-3mf", three_mf.name, "--outputdir", str(out_dir), "--debug", "1", str(stl_path)]
    t0 = time.time()
    try:
        r = subprocess.run(cmd, cwd=out_dir, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SlicerError(f"slicer timed out after {timeout:.0f} s")
    gcodes = sorted(out_dir.glob("plate_*.gcode"))
    if r.returncode != 0 or not gcodes or not three_mf.exists():
        logs = "\n".join(p.read_text(errors="replace").strip().splitlines()[-3:] for p in sorted(out_dir.glob("*.log")))
        raise SlicerError("slicer failed: " + (logs or (r.stdout + r.stderr).strip()[-500:] or f"exit {r.returncode}"))
    res = SliceResult(str(gcodes[0]), str(three_mf), machine, process, filament, applied)
    _read_results(res)
    res.warnings = [w for w in res.warnings]
    res.time_s = res.time_s or 0.0
    return res


def _read_results(res: SliceResult) -> None:
    text = Path(res.gcode).read_text(errors="replace")
    m = re.search(r"; total layer number: (\d+)", text); res.layers = int(m.group(1)) if m else 0
    m = re.search(r"; max_z_height: ([\d.]+)", text); res.height_mm = float(m.group(1)) if m else 0.0
    m = re.search(r"; filament used \[mm\] = ([\d.]+)", text); res.filament_mm = float(m.group(1)) if m else 0.0
    m = re.search(r"; filament used \[cm3\] = ([\d.]+)", text); res.filament_cm3 = float(m.group(1)) if m else 0.0
    m = re.search(r"; filament used \[g\] = ([\d.]+)", text); res.filament_g = float(m.group(1)) if m else 0.0
    m = re.search(r"; estimated printing time.*?= (.+)", text)
    if m:
        res.time_s = _parse_time(m.group(1))
    try:
        with zipfile.ZipFile(res.three_mf) as z:
            names = z.namelist()
            if "Metadata/slice_info.config" in names:
                info = z.read("Metadata/slice_info.config").decode("utf-8", "replace")
                mm = re.search(r'key="prediction" value="([\d.]+)"', info)
                if mm and not res.time_s:
                    res.time_s = float(mm.group(1))
                mm = re.search(r'key="weight" value="([\d.]+)"', info)
                if mm and not res.filament_g:
                    res.filament_g = float(mm.group(1))
                for w in re.findall(r'<warning msg="([^"]+)"', info):
                    res.warnings.append(w.replace("_", " "))
            if "3D/3dmodel.model" in names:
                model = z.read("3D/3dmodel.model").decode("utf-8", "replace")
                mm = re.search(r'<item [^>]*transform="([^"]+)"', model)
                if mm:
                    nums = [float(x) for x in mm.group(1).split()]
                    if len(nums) == 12:
                        res.translate = (nums[9], nums[10], nums[11])
    except zipfile.BadZipFile:
        res.warnings.append("3MF could not be read")


def _parse_time(s: str) -> float:
    total = 0.0
    for val, unit in re.findall(r"(\d+)\s*([dhms])", s):
        total += int(val) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
    return total


# ---------------------------------------------------------------------------- G-code → layers for the viewer
FEATURE_COLORS = {                       # Orca-like palette
    "Outer wall": "#ff7d38", "Inner wall": "#ffd23f", "Overhang wall": "#2f7bff", "Sparse infill": "#b45fd6",
    "Internal solid infill": "#c85cff", "Top surface": "#ff4d4d", "Bottom surface": "#4dd0ff", "Internal Bridge": "#6ab0ff",
    "Bridge": "#6ab0ff", "Gap infill": "#ffffff", "Skirt": "#7aa0b8", "Brim": "#7aa0b8", "Support": "#4cd37a",
    "Support interface": "#7ee0a0", "Custom": "#666666", "Prime tower": "#888888",
}
_G1 = re.compile(r"^G[01]\s+(.*)")
_ARC = re.compile(r"^G([23])\s+(.*)")


def parse_gcode(text: str, translate=(0.0, 0.0, 0.0), walls_only: bool = False, max_segments: int = 600_000) -> dict[str, Any]:
    """Extrusion moves grouped by layer with a feature index per segment, in MODEL coordinates (bed − translate).
    Returns {layers:[{z,h,start}], pos:[x,y,z,...] (segment pairs), feat:[idx per segment], types:[...], colors:[...]}."""
    tx, ty, tz = translate
    types: list[str] = []; type_idx: dict[str, int] = {}
    pos: list[float] = []; feat: list[int] = []; layers: list[dict[str, Any]] = []
    x = y = z = 0.0; e_rel = True; cur = "Custom"; cur_i = -1; have = False; last_e = -1e9
    skip_types = {"Skirt", "Brim", "Prime tower", "Custom"} if walls_only else set()
    keep = None if not walls_only else {"Outer wall", "Inner wall", "Overhang wall", "Top surface", "Bottom surface", "Support", "Support interface"}
    n_seg = 0

    def tidx(name: str) -> int:
        if name not in type_idx:
            type_idx[name] = len(types); types.append(name)
        return type_idx[name]

    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line[0] == ";":
            if line.startswith(";TYPE:"):
                cur = line[6:].strip(); cur_i = tidx(cur)
            elif line.startswith(";LAYER_CHANGE"):
                layers.append({"z": None, "h": None, "start": len(feat)})
            elif line.startswith(";Z:") and layers:
                try: layers[-1]["z"] = float(line[3:])
                except ValueError: pass
            elif line.startswith(";HEIGHT:") and layers:
                try: layers[-1]["h"] = float(line[8:])
                except ValueError: pass
            continue
        if line.startswith("M82"): e_rel = False; last_e = -1e9; continue
        if line.startswith("G92") and "E" in line: last_e = 0.0; continue
        if line.startswith("M83"): e_rel = True; continue
        m = _G1.match(line)
        arc = None if m else _ARC.match(line)
        if not m and not arc:
            continue
        args = (m.group(1) if m else arc.group(2)).split(";")[0].split()
        nx, ny, nz, e, i, j = x, y, z, None, 0.0, 0.0
        for a in args:
            c, v = a[0], a[1:]
            try:
                if c == "X": nx = float(v)
                elif c == "Y": ny = float(v)
                elif c == "Z": nz = float(v)
                elif c == "E": e = float(v)
                elif c == "I": i = float(v)
                elif c == "J": j = float(v)
            except ValueError:
                pass
        extruding = e is not None and (e > 0 if e_rel else e > last_e)
        if not e_rel and e is not None:
            last_e = e
        moved = (nx != x or ny != y)
        if extruding and moved and have and (keep is None or cur in keep) and cur not in skip_types and n_seg < max_segments:
            if arc:                                                     # G2/G3: interpolate the arc into short chords
                cx, cy = x + i, y + j; r = math.hypot(i, j)
                a0 = math.atan2(y - cy, x - cx); a1 = math.atan2(ny - cy, nx - cx)
                cw = arc.group(1) == "2"
                da = a1 - a0
                if cw and da > 0: da -= 2 * math.pi
                if not cw and da < 0: da += 2 * math.pi
                steps = max(2, int(abs(da) * r / 0.4))
                px, py = x, y
                for k in range(1, steps + 1):
                    a = a0 + da * k / steps; qx, qy = cx + r * math.cos(a), cy + r * math.sin(a)
                    pos.extend((round(px - tx, 3), round(py - ty, 3), round(nz - tz, 3), round(qx - tx, 3), round(qy - ty, 3), round(nz - tz, 3))); feat.append(cur_i); n_seg += 1
                    px, py = qx, qy
            else:
                pos.extend((round(x - tx, 3), round(y - ty, 3), round(z - tz, 3), round(nx - tx, 3), round(ny - ty, 3), round(nz - tz, 3))); feat.append(cur_i); n_seg += 1
        x, y, z = nx, ny, nz; have = True
    if layers and layers[0]["start"] > 0:                           # purge line / skirt before the first LAYER_CHANGE belongs to layer 1
        layers[0]["start"] = 0
    return {"layers": layers, "pos": pos, "feat": feat, "types": types,
            "colors": [FEATURE_COLORS.get(t, "#9fb0c2") for t in types], "segments": n_seg,
            "truncated": n_seg >= max_segments, "walls_only": walls_only, "translate": list(translate)}
