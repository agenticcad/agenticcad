"""Machine control over the network: live status, camera, and a small set of guarded commands.

First (and so far only) driver: the Makera Z1 over Wi-Fi. What it speaks, read from Makera's firmware
(MakeraZ1Firmware, a Smoothieware fork running the motion board) and checked against a real Z1:

  TCP 2222 on the machine's ESP32 board, framed packets  86 68 | len(2, BE) | type | payload | crc16(2, BE) | 55 aa
    len = 1 + len(payload) + 2, CRC-16/XMODEM (poly 0x1021, init 0) over len + type + payload.
    0xA1  one realtime byte: '?' status, '!' feed hold, '~' resume, 0x18 halt (stops motion and spindle, Alarm)
    0xA2  a console / G-code line
    0x81  status reply  <Idle|MPos:x,y,z,a,b|WPos:x,y,z,a,b|F:cur,req,ovr|S:rpm,target,ovr,vac,...|T:tool,offset,..|...>
    0x90  console text
  UDP 3333  broadcasts "name,ip,port,busy,state" about once a second
  WS 82 /ws_video  send "start_stream" / "stop_stream", JPEG frames (640 x 480, ~20 fps) as binary messages

  Machine coordinates: every axis homes to its maximum end (0) and the work area is negative, X/Y about -206..0,
  Z about -102..0 (soft_endstop.* in configZ1.default). Soft limits are OFF in the firmware (soft_endstop.enable
  false) and there are no switches at the far ends: nothing in the machine stops a jog running into the far end, or
  the tool into the bed. This module is what stops it.

Safety model (MachineControl):
  * Connecting is monitor-only: status, camera, feed hold and halt. Hold and halt always work.
  * Anything that moves the machine needs `armed` (the user turns Control on). Control turns itself off on
    disconnect, on a halt or alarm, when status stops arriving, and after ARM_IDLE_S without a command.
  * Jogging needs a homing cycle seen in this session (positions mean nothing before it) and an Idle machine; one
    jog at a time (the next is refused until this one has finished), small steps (JOG_MAX), slow Z-down, every
    target checked against the travel box in machine coordinates AND a Z floor in work coordinates (default 2 mm
    above work zero, so the tool can't be jogged into the stock top or the bed under it).
  * No free-form G-code and no manual spindle control. Programs run only as the CAM program's operations, prepared
    (prepare_program: comments out, 62-character lines, no relative moves / probing / offset changes), checked
    against the travel from the current work origin, uploaded to the machine's SD and played; Pause / Resume /
    Stop program and STOP stay available.
"""
from __future__ import annotations

import asyncio
import re
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Callable

SYNC, TAIL = b"\x86\x68", b"\x55\xaa"
T_REALTIME, T_LINE, T_STATUS, T_TEXT = 0xA1, 0xA2, 0x81, 0x90
HALT = b"\x18"
# file transfer (the ESP32 holds the SD card; MakeraStudio's "XMODEM" class, really a machine-driven pull protocol):
# host B0 "upload <path>", B1 md5 hex; machine B2 -> host B2 [count u32][size u16]; machine B3 [seq u32] -> host B3 [seq][data];
# machine B4 = done, B5 = cancel (either side), B6 = resend the last frame, 0x91 = alarm during the transfer
T_FILE_START, T_FILE_MD5, T_FILE_VIEW, T_FILE_DATA, T_FILE_END, T_FILE_CAN, T_FILE_RETRY, T_ALARM = 0xB0, 0xB1, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6, 0x91
FILE_TYPES = (T_FILE_MD5, T_FILE_VIEW, T_FILE_DATA, T_FILE_END, T_FILE_CAN, T_FILE_RETRY, T_ALARM)
PACKET = 8192
MAX_LINE = 62                     # the Z1 plays lines from 64-byte slots: 62 characters + newline + NUL
PATH_ESC = {" ": "\x01", "?": "\x02", "*": "\x03", "!": "\x04", "~": "\x05"}


# ------------------------------------------------------------------ framing
def crc16(data: bytes, crc: int = 0) -> int:
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def frame(ptype: int, payload: bytes) -> bytes:
    body = bytes([ptype]) + payload
    hdr = (len(body) + 2).to_bytes(2, "big")
    return SYNC + hdr + body + crc16(hdr + body).to_bytes(2, "big") + TAIL


def parse_frames(buf: bytearray):
    """Yield (type, payload) for every complete, valid frame in buf and remove them from it (bad frames are dropped)."""
    while True:
        i = buf.find(SYNC)
        if i < 0:
            del buf[:max(0, len(buf) - 1)]
            return
        if i:
            del buf[:i]
        if len(buf) < 4:
            return
        n = int.from_bytes(buf[2:4], "big")
        total = n + 6
        if n < 3 or n > PACKET + 64:
            del buf[:2]
            continue
        if len(buf) < total:
            return
        pkt = bytes(buf[:total])
        del buf[:total]
        if pkt[-2:] != TAIL or crc16(pkt[2:total - 4]) != int.from_bytes(pkt[total - 4:total - 2], "big"):
            continue
        yield pkt[4], pkt[5:total - 4]


# ------------------------------------------------------------------ status
STATES = ("Idle", "Run", "Hold", "Home", "Alarm", "Tool", "Pause", "Wait", "Sleep")
# Commands never sent to a Z1 (firmware research 2026-10-17): bare M490 drives the Z motor's step pin through the ATC
# clamp routine of a machine that has no ATC; M498.2 erases the EEPROM block holding tool lengths and G54.
BLOCKED = re.compile(r"(?i)\bM490(?:\.0)?(?![.\d])|\bM498\.2\b")
# Kernel.h HALT_REASON: why the machine is in Alarm (the status carries |H:n while halted). 1-15 clear with $X / M999,
# 21-26 need a reset on the machine, 41 a power cycle.
HALT_REASONS = {1: "manual stop", 2: "homing failed", 3: "probe failed", 4: "calibration failed", 5: "ATC homing failed",
                6: "invalid tool number", 7: "no tool in the spindle", 8: "a tool is already in the spindle",
                9: "spindle overheated", 10: "soft limit", 11: "cover open", 12: "probe invalid", 13: "emergency stop",
                14: "power supply overheated", 15: "not homed", 21: "hard limit", 22: "X motor error", 23: "Y motor error",
                24: "Z motor error", 25: "spindle stalled", 26: "SD card error", 41: "spindle alarm"}


def halt_advice(code: int | None) -> str:
    if code == 13:
        return "release the emergency stop (turn it clockwise), then press Unlock and home"
    if code == 11:
        return "close the cover, then press Unlock and home"
    if code is None or code < 21:
        return "press Unlock to clear it, then home"
    if code < 41:
        return "it needs a reset on the machine (its button), then home"
    return "switch the machine off and on again"


def parse_status(text: str) -> dict[str, Any] | None:
    """'<Idle|MPos:...|WPos:...|...>' -> dict. Unknown fields are kept raw under their letter."""
    m = re.search(r"<([^>]*)>", text)
    if not m:
        return None
    parts = m.group(1).split("|")
    out: dict[str, Any] = {"state": parts[0].strip(), "raw": m.group(0)}
    for p in parts[1:]:
        if ":" not in p:
            continue
        k, v = p.split(":", 1)
        nums = []
        for x in v.split(","):
            try:
                nums.append(float(x))
            except ValueError:
                nums.append(x.strip())
        if k == "MPos":
            out["mpos"] = nums[:3]; out["mpos_ab"] = nums[3:5]
        elif k == "WPos":
            out["wpos"] = nums[:3]; out["wpos_ab"] = nums[3:5]
        elif k == "F":
            out["feed"] = {"current": nums[0], "requested": nums[1] if len(nums) > 1 else None, "override": nums[2] if len(nums) > 2 else None}
        elif k == "S":
            out["spindle"] = {"rpm": nums[0], "target": nums[1] if len(nums) > 1 else None, "override": nums[2] if len(nums) > 2 else None,
                              "vacuum_mode": bool(nums[3]) if len(nums) > 3 else None,
                              "temp": nums[4] if len(nums) > 4 else None, "power_temp": nums[5] if len(nums) > 5 else None}
        elif k == "T":
            out["tool"] = {"number": int(nums[0]), "offset": nums[1] if len(nums) > 1 else None}
        elif k == "H":
            code = int(nums[0]) if nums and isinstance(nums[0], float) else None
            out["halt"] = {"code": code, "reason": HALT_REASONS.get(code, f"reason {code}"), "advice": halt_advice(code)}
        elif k == "P":
            out["progress"] = {"lines": nums[0], "percent": nums[1] if len(nums) > 1 else None, "seconds": nums[2] if len(nums) > 2 else None}
        else:
            out.setdefault("other", {})[k] = v
    return out


def discover(timeout: float = 3.0, port: int = 3333) -> dict[str, dict[str, Any]]:
    """Listen for Makera broadcasts ("name,ip,port,busy,state") for `timeout` seconds."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass
    s.bind(("", port))
    s.settimeout(0.3)
    seen: dict[str, dict[str, Any]] = {}
    t0 = time.time()
    try:
        while time.time() - t0 < timeout:
            try:
                data, addr = s.recvfrom(512)
            except socket.timeout:
                continue
            f = data.decode(errors="replace").strip().split(",")
            if len(f) >= 3:
                seen[addr[0]] = {"name": f[0], "host": f[1] or addr[0], "port": int(f[2]) if f[2].isdigit() else 2222,
                                 "busy": f[3] == "1" if len(f) > 3 else None, "state": f[4] if len(f) > 4 else None}
    finally:
        s.close()
    return seen


def escape_path(path: str) -> str:
    """The Z1's path escaping (space ? * ! ~ -> \\x01..\\x05), as MakeraStudio sends file names."""
    return "".join(PATH_ESC.get(c, c) for c in path)


# ------------------------------------------------------------------ connection
class Z1Link:
    """One TCP connection to a Makera Z1: polls status, collects console text. Knows nothing about safety."""

    def __init__(self, host: str, port: int = 2222, poll_s: float = 0.25):
        self.host, self.port, self.poll_s = host, port, poll_s
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.status: dict[str, Any] | None = None
        self.status_t = 0.0
        self.log: list[tuple[float, str]] = []
        self.error: str | None = None
        self.on_status: Callable[[dict], None] | None = None
        self._tasks: list[asyncio.Task] = []
        self._lock = asyncio.Lock()
        self.file_q: asyncio.Queue | None = None           # file-transfer frames while an upload runs
        self.poll_paused = False

    @property
    def connected(self) -> bool:
        return self.writer is not None and not self.writer.is_closing()

    async def open(self, timeout: float = 5.0) -> None:
        self.reader, self.writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), timeout)
        sock = self.writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._tasks = [asyncio.create_task(self._read()), asyncio.create_task(self._poll())]
        t0 = time.time()                                    # a status reply proves the machine is talking to us
        while time.time() - t0 < timeout and self.status is None and self.connected and not self.error:
            await asyncio.sleep(0.05)
        if self.error:
            msg = self.error
            await self.close()
            raise ConnectionError(msg)
        if self.status is None:
            await self.close()
            raise ConnectionError(f"{self.host}:{self.port} accepted the connection but sent no status")

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()
        self._tasks = []
        if self.writer is not None:
            try:
                self.writer.close()
                await asyncio.wait_for(self.writer.wait_closed(), 2)
            except Exception:  # noqa: BLE001
                pass
        self.writer = None

    async def send(self, ptype: int, payload: bytes) -> None:
        if not self.connected:
            raise ConnectionError("not connected")
        async with self._lock:
            self.writer.write(frame(ptype, payload))
            await self.writer.drain()

    async def realtime(self, ch: bytes) -> None:
        await self.send(T_REALTIME, ch)

    async def line(self, text: str) -> None:
        if BLOCKED.search(text):
            raise Refused(f"refused to send {text.strip()!r}: on the Z1 this command is unsafe (see BLOCKED)")
        self.log.append((time.time(), "> " + text))
        await self.send(T_LINE, (text.rstrip("\n") + "\n").encode())

    async def raw(self, text: str) -> None:
        """A console command sent exactly as given, no newline: the Z1 needs `play <path>`, `suspend`, `resume` and
        `abort` that way (the filename's CRC and the exact-match commands break on a trailing newline)."""
        if BLOCKED.search(text):
            raise Refused(f"refused to send {text!r}: on the Z1 this command is unsafe (see BLOCKED)")
        self.log.append((time.time(), "> " + text))
        await self.send(T_LINE, text.encode())

    async def upload(self, remote: str, data: bytes, progress: Callable[[float], Any] | None = None, timeout: float = 9.0) -> None:
        """Upload `data` to `remote` (e.g. /sd/gcodes/x.nc) the way MakeraStudio does; raises ConnectionError on failure.
        The status poll pauses during the transfer."""
        import hashlib
        if not data:
            raise ConnectionError("nothing to upload (empty file)")
        count = (len(data) + PACKET - 1) // PACKET
        self.file_q = asyncio.Queue()
        self.poll_paused = True
        last: tuple[int, bytes] | None = None
        ends = 0

        async def put(t: int, payload: bytes) -> None:
            nonlocal last
            last = (t, payload)
            await self.send(t, payload)
        try:
            self.log.append((time.time(), f"> upload {remote} ({len(data)} bytes, {count} packets)"))
            await self.send(T_FILE_START, f"upload {escape_path(remote)}".encode())
            await self.send(T_FILE_MD5, hashlib.md5(data).hexdigest().encode())
            while True:
                try:
                    t, payload = await asyncio.wait_for(self.file_q.get(), timeout)
                except asyncio.TimeoutError:
                    raise ConnectionError(f"the machine stopped answering during the upload ({timeout:g} s)") from None
                if t == T_FILE_VIEW:
                    await put(T_FILE_VIEW, count.to_bytes(4, "big") + PACKET.to_bytes(2, "big"))
                elif t == T_FILE_DATA:
                    seq = int.from_bytes(payload[:4], "big") if len(payload) >= 4 else 0
                    if seq < 1 or seq > count:
                        ends += 1
                        if ends > 3:
                            await self.send(T_FILE_CAN, b"")
                            raise ConnectionError(f"the machine asked for packet {seq} of {count}")
                        await put(T_FILE_END, b"")
                        continue
                    await put(T_FILE_DATA, seq.to_bytes(4, "big") + data[(seq - 1) * PACKET: seq * PACKET])
                    if progress:
                        progress(seq / count)
                elif t == T_FILE_RETRY and last is not None:
                    await self.send(*last)
                elif t == T_FILE_END:
                    break
                elif t == T_FILE_CAN:
                    raise ConnectionError("the machine cancelled the upload")
                elif t == T_ALARM:
                    await self.send(T_FILE_CAN, b"")
                    raise ConnectionError("the machine alarmed during the upload")
        finally:
            self.file_q = None
            self.poll_paused = False

    async def _poll(self) -> None:
        try:
            while self.connected:
                if not self.poll_paused:
                    await self.realtime(b"?")
                await asyncio.sleep(self.poll_s)
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass

    async def _read(self) -> None:
        buf = bytearray()
        try:
            while True:
                d = await self.reader.read(4096)
                if not d:
                    self.error = self.error or "the machine closed the connection"
                    break
                buf += d
                for t, p in parse_frames(buf):
                    if t in FILE_TYPES and self.file_q is not None:
                        self.file_q.put_nowait((t, p))
                        if t != T_ALARM:
                            continue
                    txt = p.decode(errors="replace")
                    if t == T_STATUS:
                        st = parse_status(txt)
                        if st:
                            self.status, self.status_t = st, time.time()
                            if self.on_status:
                                self.on_status(st)
                    else:
                        for ln in txt.replace("\r", "").split("\n"):
                            if ln.strip():
                                self.log.append((time.time(), ln.strip()))
                                if "maximum number of client" in ln:
                                    self.error = ("the machine already has the maximum number of connections "
                                                  "(close MakeraStudio's connection to it, or another AgenticCAD)")
                del self.log[:-300]
        except (OSError, asyncio.CancelledError):
            pass
        finally:
            if self.writer is not None and not self.writer.is_closing():
                self.writer.close()


# ------------------------------------------------------------------ safety
@dataclass
class Profile:
    """What MachineControl needs to know about a machine to keep it out of trouble."""
    name: str
    travel: dict                          # mm per axis; machine coordinates run from -travel to 0 (homes to max)
    max_rate: dict                        # mm/min per axis (firmware max)
    margin: float = 1.0                   # stay this far inside the travel box
    top: float = -0.5                     # highest machine coordinate a jog may reach (the home switches are at 0)
    switches: dict = field(default_factory=dict)    # name -> (on command, off command)
    anchor1: tuple = (-192.4, -194.3)      # machine XY of Anchor 1 (the L-bracket's inside corner); per machine
    setter_from_anchor: tuple = (181.0, 181.0)   # tool setter XY relative to Anchor 1 (ATCHandler: Z1 manual change)
    change_xy: tuple = (-11.6, -14.6)      # where the head waits for a manual tool change (coordinate.clearance_x/y)
    bed_anchor: tuple = (-88.0, -88.0)     # where Anchor 1 (the L-bracket's inside corner) is on the bed, from its centre
                                           # (Makera Z1 bed CAD): machine XY - anchor1 + bed_anchor = bed XY
    probe3d_tool: int = 9999               # tool number of the add-on 3D probe, as MakeraStudio uses it (its tool codes:
                                           # T0 wired probe, T8888 3D Probe Rod, T9999 3D probe; the firmware powers the
                                           # probe port for T9999 after a change)


def makera_live_config(path=None) -> dict:
    """Values from the machine's own config as MakeraStudio downloads it on connect (ControllerConfig.txt): the anchor
    is calibrated per machine, so it beats the firmware default."""
    from pathlib import Path as _P
    path = _P(path) if path else _P.home() / "Library/Application Support/MakeraStudio/ControllerConfig.txt"
    out = {}
    try:
        for ln in path.read_text(errors="replace").splitlines():
            parts = ln.split("#")[0].split()
            if len(parts) >= 2:
                try:
                    out[parts[0]] = float(parts[1])
                except ValueError:
                    out[parts[0]] = parts[1]
    except OSError:
        pass
    return out


Z1 = Profile("Makera Z1", travel={"x": 200.0, "y": 200.0, "z": 100.0}, max_rate={"x": 1200.0, "y": 1200.0, "z": 600.0},
             switches={"dust": ("M851", "M852"), "light": ("M821", "M822"), "probe_laser": ("M831", "M832")})

JOG_MAX = {"xy": 10.0, "z_up": 10.0, "z_down": 1.0}          # mm per click
GOTO_FEED_MAX = 600.0                                        # mm/min cap for go-to moves
Z_TOP_BAND = 1.5                                             # go-to moves only while machine Z is within this of the top
JOG_SCALE = {"xy": 0.5, "z_up": 0.5, "z_down": 0.25}         # fraction of the axis max rate ($J F)
# Makera's 3D-probe routines (firmware ATCHandler M480.n, sent by MakeraStudio as `M480.n X<offset> Y<offset> Z<depth>
# D<ball dia>`, defaults X20 Y20 Z2 D2). Corners are named as seen from the front of the machine (Y+ is the back).
# sx, sy: the firmware's signed X/Y_distance for that subcode.
#   outside corner (.1-.4): start ~5 mm above the stock, ~5 mm in from the corner. Touch Z twice (100 mm/min) and set
#     Z0; back up to the start height, step out past the X edge by the offset, drop to Z = -depth (10 % speed), probe
#     back in X (100 then 50), set X to the edge; the same for Y; up to the start height, then G0 to the new X0 Y0.
#   inside corner (.5-.8): start above the top surface just outside the corner; touch Z twice, set Z0, move in by the
#     offsets, drop to -depth, probe back to the two walls, set X/Y there; up, then G0 to X0 Y0.
#   pocket centre (.9): start inside the pocket below its edge; probe both X walls, go to the middle, X0; then Y. No Z.
#   boss centre (.10): start above the boss centre; touch Z, set Z0; step out each side by the offset (+5 mm on the
#     far side), drop to -depth, probe in; X0 / Y0 at the middle. X or Y offset 0 skips that axis (centres only).
PROBE3D = {
    "outside_back_left": (1, 1, 1), "outside_back_right": (2, -1, 1), "outside_front_right": (3, -1, -1),
    "outside_front_left": (4, 1, -1),
    "inside_back_left": (5, 1, 1), "inside_back_right": (6, -1, 1), "inside_front_right": (7, -1, -1),
    "inside_front_left": (8, 1, -1),
    "pocket_center": (9, 1, 1), "boss_center": (10, 1, 1),
}
PROBE3D_MAX_DROP = 10.0                  # mm: hold if Z falls this far below the start before the probe touches anything
ARM_IDLE_S = 300.0                                           # Control turns itself off after this long unused
STALE_S = 2.0                                                # status older than this: treat the link as lost


class Refused(Exception):
    """A command the safety rules don't allow right now; the message says why."""


_WORD = re.compile(r"([A-Z])(-?\d*\.?\d+)")


def prepare_program(gcode: str, op_names: list[str], air_cut: float = 0.0) -> dict:
    """G-code from the CAM post -> what the Z1 plays: comments and blank lines removed (the machine stores lines in 64-byte
    slots), optionally lifted `air_cut` mm (every Z except machine-coordinate G53 moves), each operation's line range
    (from the post's "; <name> [kind] ..." header comments, in order) and the work-coordinate extents of the moves.
    Raises Refused for what the Z1 must not get: lines too long, relative moves, homing/probing, blocked commands."""
    out: list[str] = []
    ops: list[dict] = []
    k = 0
    x = y = z = None
    ext = {"x": [1e9, -1e9], "y": [1e9, -1e9], "z": [1e9, -1e9]}
    tools: list[int] = []
    for raw in gcode.splitlines():
        s_ = raw.strip()
        if s_.startswith(";"):
            if k < len(op_names) and s_[1:].strip().startswith(op_names[k] + " ["):
                ops.append({"name": op_names[k], "start": len(out) + 1, "tool": None})
                k += 1
            continue
        s_ = re.sub(r"\(.*?\)", "", s_.split(";")[0]).strip().upper()
        if not s_:
            continue
        if BLOCKED.search(s_):
            raise Refused(f"the program contains {s_!r}, which is never sent to a Z1")
        if re.search(r"\bG91\b(?!\.)", s_):
            raise Refused("the program uses relative moves (G91); only absolute programs are run")
        if re.search(r"\bG(38(\.\d)?|92|10)\b|\$H|\bM49[0-9]", s_):           # G28 is the Z1's park (clearance) move: fine
            raise Refused(f"the program contains {s_!r}: homing, probing and offset changes stay with the Machine tab")
        m6 = re.search(r"\bM0?6\b", s_)
        tw = re.search(r"\bT(\d+)", s_)
        if m6 and tw:
            tools.append(int(tw.group(1)))
            if ops and ops[-1]["tool"] is None:
                ops[-1]["tool"] = int(tw.group(1))
        g53 = re.search(r"\bG53\b", s_)
        if air_cut and not g53:
            s_ = re.sub(r"Z(-?\d*\.?\d+)", lambda mm: "Z" + f"{float(mm.group(1)) + air_cut:.3f}".rstrip("0").rstrip("."), s_)
        if not g53:
            for a, v in _WORD.findall(s_):
                if a == "X": x = float(v)
                elif a == "Y": y = float(v)
                elif a == "Z": z = float(v)
            if re.search(r"\bG0?[0-3]\b", s_) or re.match(r"^[XYZ]", s_):
                for a, v in (("x", x), ("y", y), ("z", z)):
                    if v is not None:
                        ext[a][0] = min(ext[a][0], v); ext[a][1] = max(ext[a][1], v)
        if len(s_) > MAX_LINE:
            raise Refused(f"line {len(out) + 1} is {len(s_)} characters; the Z1 plays at most {MAX_LINE}: {s_[:40]}…")
        out.append(s_)
    for i, o in enumerate(ops):
        o["end"] = ops[i + 1]["start"] - 1 if i + 1 < len(ops) else len(out)
        if o["tool"] is None and i:                       # no tool change of its own: it keeps the previous op's tool
            o["tool"] = ops[i - 1]["tool"]
    if k != len(op_names):
        raise Refused(f"found {k} of {len(op_names)} operations in the program (headers missing)")
    return {"text": "\n".join(out) + "\n", "lines": len(out), "ops": ops, "tools": tools,
            "extents": {a: v for a, v in ext.items() if v[0] <= v[1]}, "air_cut": air_cut}


def program_filename(name: str) -> str:
    """An ASCII file name the Z1 plays safely (no spaces, no ' -' option marker)."""
    base = re.sub(r"[^A-Za-z0-9_.]+", "_", name).strip("_.")[:40] or "program"
    return base + ".nc"


class MachineControl:
    def __init__(self, emit: Callable[[dict], Any] | None = None):
        self.emit = emit
        self.link: Z1Link | None = None
        self.profile = Z1
        self.armed = False
        self.homed = False                   # seen a homing cycle finish in this session
        self._homing = False
        self._saw_home_state = False
        self.jog_busy: dict | None = None    # {"target": [x,y,z], "t0": .., "deadline": ..}
        self.z_floor = 2.0                   # lowest work Z (mm) a jog may take the tool to
        self.last_action = 0.0
        self.switch_state: dict[str, bool] = {}
        self.measured: dict | None = None    # {"tool": n, "touch_z": z, "t": ..}: the tool measured on the setter this session
        self.routine: dict | None = None     # a Makera routine being followed: {kind, command, stage, msg, ...}
        self.job: dict | None = None         # a program sent to the machine: {name, stage, ops, lines, line, percent, ...}
        self._job_task: asyncio.Task | None = None
        self._rt_task: asyncio.Task | None = None
        self.events: list[str] = []
        self._watch: asyncio.Task | None = None

    # -- connection
    async def connect(self, host: str, port: int = 2222) -> dict:
        await self.disconnect()
        link = Z1Link(host, port)
        link.on_status = self._on_status
        await link.open()
        self.link = link
        self.armed = self.homed = self._homing = False
        self.jog_busy = None
        self.measured = self.routine = self.job = None
        self._note(f"connected to {host}:{port}")
        self._watch = asyncio.create_task(self._watchdog())
        return self.payload()

    async def disconnect(self) -> None:
        if self._watch:
            self._watch.cancel()
            self._watch = None
        if self.link:
            await self.link.close()
            self._note("disconnected")
        self.link = None
        self.armed = False
        self.homed = False
        self.jog_busy = None

    @property
    def connected(self) -> bool:
        return bool(self.link and self.link.connected)

    def _status(self) -> dict | None:
        if not self.link or not self.link.status:
            return None
        return self.link.status

    def fresh(self) -> bool:
        return bool(self.link and self.link.status and time.time() - self.link.status_t < STALE_S)

    def _note(self, text: str) -> None:
        self.events.append(time.strftime("%H:%M:%S ") + text)
        del self.events[:-100]

    async def _push(self) -> None:
        if self.emit:
            r = self.emit({"type": "machine_link", **self.payload()})
            if asyncio.iscoroutine(r):
                await r

    def _on_status(self, st: dict) -> None:
        s = st["state"]
        if self._homing:
            if s == "Home":
                self._saw_home_state = True
            elif s == "Idle" and (self._saw_home_state or time.time() - self._homing_t0 > 1.0):
                self._homing = False
                self.homed = True
                self._note("homed")
            elif s == "Alarm":
                self._homing = False
                self._note("homing ended in an alarm")
        if s in ("Alarm", "Sleep") and (self.armed or self.homed):
            if self.armed:
                self._note(f"machine state {s}: Control off")
            self.armed = False
            if s == "Alarm":
                self.homed = False           # after a halt the position may be off: home again before jogging
        if self.jog_busy:
            tgt = self.jog_busy["target"]
            mp = st.get("mpos") or [0, 0, 0]
            if s == "Idle" and max(abs(a - b) for a, b in zip(mp, tgt)) < 0.02:
                self.jog_busy = None
            elif s in ("Alarm", "Hold", "Pause", "Sleep"):
                self.jog_busy = None

    async def _watchdog(self) -> None:
        last_push = 0.0
        try:
            while True:
                await asyncio.sleep(0.25)
                if not self.connected:
                    if self.link is not None:
                        err = self.link.error or "connection lost"
                        self._note(err)
                        self.armed = False
                        self.homed = False
                        self.jog_busy = None
                        await self._push()
                    break
                if self.armed and not self.fresh():
                    self.armed = False
                    self._note("no status from the machine: Control off")
                if self.job_active:
                    self.last_action = time.time()                 # a running program keeps Control on
                if self.armed and time.time() - self.last_action > ARM_IDLE_S:
                    self.armed = False
                    self._note("Control off after 5 minutes unused")
                if self.jog_busy and time.time() > self.jog_busy["deadline"]:
                    self._note("a jog did not finish where expected: Control off, check the machine")
                    self.jog_busy = None
                    self.armed = False
                if time.time() - last_push > 0.25:
                    last_push = time.time()
                    await self._push()
        except asyncio.CancelledError:
            pass

    # -- read
    def payload(self) -> dict:
        st = self._status()
        return {"connected": self.connected, "host": self.link.host if self.link else None, "fresh": self.fresh(),
                "armed": self.armed, "homed": self.homed, "homing": self._homing, "jogging": self.jog_busy is not None,
                "status": {k: v for k, v in st.items() if k != "raw"} if st else None, "raw": st.get("raw") if st else None,
                # in Hold / Pause / Alarm the Z1 reports its last planned position, not where the head stopped (seen on a
                # real Z1 2026-10-17: held at about -57,-58 while the status said -1,-1): show it as unreliable
                "position_reliable": bool(st) and st.get("state") not in ("Hold", "Pause", "Alarm"),
                "z_floor": self.z_floor, "switches": dict(self.switch_state), "events": self.events[-20:],
                "measured": self.measured, "tool_ok": self.tool_ok(), "routine": self.routine,
                "job": {k: v for k, v in self.job.items() if k != "text"} if self.job else None,
                "log": [t for _, t in (self.link.log[-30:] if self.link else [])],
                "limits": {"jog_max": JOG_MAX, "travel": self.profile.travel, "margin": self.profile.margin, "top": self.profile.top},
                "profile": self.profile.name, "probe3d_tool": self.profile.probe3d_tool,
                "anchor1": list(self.profile.anchor1), "bed_anchor": list(self.profile.bed_anchor)}

    def describe(self) -> str:
        """Plain-text status for the agent."""
        if not self.connected:
            return "No machine connected (Machine tab ▸ Connect)."
        st = self._status() or {}
        mp, wp = st.get("mpos"), st.get("wpos")
        f = lambda v: ", ".join(f"{a:.3f}" for a in v) if v else "?"
        sp = st.get("spindle") or {}
        tl = st.get("tool") or {}
        return (f"{self.profile.name} at {self.link.host}: state {st.get('state')}, machine position {f(mp)}, work position {f(wp)}, "
                f"tool T{tl.get('number')} (length offset {tl.get('offset')}), spindle {sp.get('rpm')} rpm, "
                f"Control {'on' if self.armed else 'off'}, homed this session: {'yes' if self.homed else 'no'}"
                + ("" if self.fresh() else " (status is stale)"))

    # -- guards
    def _need(self, armed: bool = True, idle: bool = True, homed: bool = False) -> dict:
        if not self.connected:
            raise Refused("not connected to a machine")
        if not self.fresh():
            raise Refused("no current status from the machine")
        st = self._status()
        if armed and not self.armed:
            raise Refused("Control is off: turn it on first")
        if homed and not self.homed:
            raise Refused("home the machine first (positions are only trusted after a homing cycle in this session)")
        if idle and st["state"] != "Idle":
            if st["state"] == "Alarm":
                h = st.get("halt") or {}
                raise Refused(f"the machine is in Alarm ({h.get('reason', 'halted')}): {h.get('advice', halt_advice(None))}")
            raise Refused(f"the machine is {st['state']}, not Idle")
        return st

    def check_jog(self, axis: str, dist: float) -> tuple[list[float], float]:
        """Validate a jog; returns (target machine position, $J speed scale) or raises Refused."""
        axis = axis.lower()
        if axis not in ("x", "y", "z"):
            raise Refused(f"unknown axis {axis!r}")
        if not isinstance(dist, (int, float)) or dist != dist or dist == 0:
            raise Refused("jog distance must be a non-zero number")
        st = self._need(armed=True, idle=True, homed=True)
        self._need_tool_measured()
        if self.jog_busy:
            raise Refused("the previous jog hasn't finished")
        kind = "xy" if axis in "xy" else ("z_up" if dist > 0 else "z_down")
        if abs(dist) > JOG_MAX[kind] + 1e-9:
            raise Refused(f"{axis.upper()} jog of {abs(dist):g} mm is over the {JOG_MAX[kind]:g} mm step limit"
                          + (" for Z down" if kind == "z_down" else ""))
        mp = list(st["mpos"])
        i = "xyz".index(axis)
        target = list(mp)
        target[i] = mp[i] + dist
        lo = -self.profile.travel[axis] + self.profile.margin
        hi = self.profile.top
        if not (lo - 1e-9 <= target[i] <= hi + 1e-9):
            raise Refused(f"{axis.upper()} would go to {target[i]:.2f} (machine), outside {lo:.1f} .. {hi:.1f}")
        if axis == "z" and dist < 0:
            wz = st.get("wpos", [None, None, None])[2]
            if wz is None:
                raise Refused("no work position from the machine: Z down is not allowed")
            if wz + dist < self.z_floor - 1e-9:
                raise Refused(f"Z down would take the tool to {wz + dist:.2f} mm in work coordinates, below the "
                              f"{self.z_floor:g} mm Z floor (above work zero)")
        return target, JOG_SCALE[kind]

    # -- commands
    async def arm(self, on: bool) -> dict:
        if on:
            self._need(armed=False, idle=False)
            st = self._status()
            if st["state"] == "Alarm":
                h = st.get("halt") or {}
                raise Refused(f"the machine is in Alarm ({h.get('reason', 'halted')}): {h.get('advice', halt_advice(None))}")
            if st["state"] == "Sleep":
                raise Refused("the machine is asleep: wake it on the machine first")
            self.armed = True
            self.last_action = time.time()
            self._note("Control on")
        else:
            self.armed = False
            self._note("Control off")
        return self.payload()

    async def jog(self, axis: str, dist: float) -> dict:
        target, scale = self.check_jog(axis, dist)
        self.last_action = time.time()
        rate = self.profile.max_rate[axis.lower()] * scale
        t_est = abs(dist) / max(rate, 1.0) * 60.0
        self.jog_busy = {"target": target, "t0": time.time(), "deadline": time.time() + t_est * 3 + 3.0}
        await self.link.line(f"$J {axis.upper()}{dist:.3f} F{scale:g}")
        self._note(f"jog {axis.upper()}{dist:+g}")
        return self.payload()

    def tool_ok(self) -> bool:
        """The rule: every tool change (the probe included) is measured on the setter before the machine moves."""
        st = self._status() or {}
        tl = (st.get("tool") or {}).get("number")
        return bool(self.measured) and self.measured["tool"] == tl

    def _need_tool_measured(self) -> None:
        if not self.tool_ok():
            tl = ((self._status() or {}).get("tool") or {}).get("number")
            raise Refused(f"the fitted tool (machine says T{tl}) hasn't been measured on the tool setter this session: "
                          "use Change tool (it measures every tool, the probe included) before moving")

    def check_goto_xy(self, x: float, y: float, feed: float, internal: bool = False) -> tuple[float, float, float]:
        """Validate an absolute machine-coordinate XY move made with Z at the top of travel."""
        for v in (x, y, feed):
            if not isinstance(v, (int, float)) or v != v:
                raise Refused("go-to needs numbers")
        st = self._need(armed=True, idle=True, homed=True)
        if not internal:
            self._need_tool_measured()
        if self.jog_busy:
            raise Refused("the previous move hasn't finished")
        mz = st["mpos"][2]
        if mz < self.profile.top - Z_TOP_BAND:
            raise Refused(f"go-to moves XY only with Z at the top of travel (machine Z {mz:.2f}); raise Z first")
        for ax, v in (("x", x), ("y", y)):
            lo, hi = -self.profile.travel[ax] + self.profile.margin, self.profile.top
            if not (lo - 1e-9 <= v <= hi + 1e-9):
                raise Refused(f"{ax.upper()} {v:.2f} (machine) is outside {lo:.1f} .. {hi:.1f}")
        if not (0 < feed <= GOTO_FEED_MAX):
            raise Refused(f"go-to feed must be 0 .. {GOTO_FEED_MAX:g} mm/min")
        return x, y, feed

    async def goto_xy(self, x: float, y: float, feed: float = 300.0, internal: bool = False) -> dict:
        """Move to machine X, Y (G53) at `feed`, Z untouched (it must already be at the top). `internal`: a step of
        the change-tool flow (allowed before the new tool is measured; XY only, Z at the top)."""
        x, y, feed = self.check_goto_xy(x, y, feed, internal)
        st = self._status()
        target = [x, y, st["mpos"][2]]
        dist = ((x - st["mpos"][0]) ** 2 + (y - st["mpos"][1]) ** 2) ** 0.5
        self.last_action = time.time()
        self.jog_busy = {"target": target, "t0": time.time(), "deadline": time.time() + dist / feed * 60.0 * 2 + 5.0}
        await self.link.line(f"G53 G1 X{x:.3f} Y{y:.3f} F{feed:g}")
        self._note(f"go to machine X{x:.2f} Y{y:.2f} at {feed:g} mm/min")
        return self.payload()

    @property
    def setter_xy(self) -> tuple[float, float]:
        a = self.profile.anchor1
        return a[0] + self.profile.setter_from_anchor[0], a[1] + self.profile.setter_from_anchor[1]

    async def raise_z_top(self, feed: float = 300.0) -> dict:
        """Raise Z to the top of travel (machine Z = profile.top - 0.5) at `feed`, XY untouched."""
        st = self._need(armed=True, idle=True, homed=True)
        z = self.profile.top - 0.5
        if st["mpos"][2] >= z - 1e-3:
            return self.payload()
        self.last_action = time.time()
        self.jog_busy = {"target": [st["mpos"][0], st["mpos"][1], z], "t0": time.time(),
                         "deadline": time.time() + (z - st["mpos"][2]) / feed * 60.0 * 2 + 5.0}
        await self.link.line(f"G53 G1 Z{z:.3f} F{min(feed, 600.0):g}")
        self._note(f"raise Z to the top at {feed:g} mm/min")
        return self.payload()

    # -- Makera's own routines (firmware ATCHandler), wrapped: checks before, one command, watch, checks after.
    #    M6 Tn: Z up, park, TOOL wait (front button or M490.2), measure on the setter, store, "Done ATC".
    #    M491: re-measure the fitted tool (M6 to the same number does nothing). M495 X Y O0 F0: auto Z probe at work
    #    X,Y: probe on, touch the stock (500 then 100 mm/min), set work Z zero there, probe off. Each saves and restores
    #    the modal state (push/pop_state), so absolute mode etc. come back unless it is halted part-way.
    def _routine_done(self, since: float) -> bool:
        return any(t >= since and "Done ATC" in ln for t, ln in self.link.log)

    def _prbs(self, since: float) -> list:
        out = []
        for t, ln in self.link.log:
            if t >= since:
                m = re.search(r"\[PRB:([-\d.]+),([-\d.]+),([-\d.]+):(\d)\]", ln)
                if m:
                    out.append(([float(m.group(i)) for i in (1, 2, 3)], m.group(4) == "1"))
        return out

    async def _watch_routine(self, kind: str, since: float, timeout: float) -> None:
        """Follow a firmware routine to its 'Done ATC' (or an alarm), keeping self.routine up to date."""
        r = self.routine
        try:
            t0 = time.time()
            while time.time() - t0 < timeout:
                await asyncio.sleep(0.2)
                st = self._status() or {}
                if st.get("state") == "Alarm":
                    h = st.get("halt") or {}
                    raise Refused(f"the machine alarmed during {kind}: {h.get('reason', 'halted')} ({h.get('advice', 'Unlock, then home')})")
                if st.get("state") == "Tool" and r["stage"] == "running":
                    what = {0: "the probe", self.profile.probe3d_tool: "the 3D probe"}.get(r.get("tool"), f"T{r.get('tool')}")
                    r.update(stage="awaiting", msg=f"fit {what} with the lever, "
                             "then press the machine's front button (or Confirm here): the machine measures it on the setter")
                    await self._push()
                g = r.get("guard")
                if g and st.get("state") in ("Run", "Idle") and self.fresh():
                    why = self._guard_breach(g, st["mpos"], since)
                    if why:
                        await self.link.realtime(b"!")
                        raise Refused(f"{why}: feed hold sent, check the machine")
                if self._routine_done(since) and st.get("state") == "Idle":
                    break
            else:
                raise Refused(f"{kind} didn't finish in time: check the machine")
            await asyncio.sleep(0.6)                        # let the status catch up with the routine's last move
            prbs = self._prbs(since)
            if not prbs or not all(ok for _, ok in prbs):
                raise Refused(f"{kind} finished without a confirmed touch: check the machine")
            st = self._status()
            if kind in ("tool change", "re-measure"):
                tl = (st.get("tool") or {})
                if tl.get("number") != r["tool"]:
                    raise Refused(f"after the change the machine reports T{tl.get('number')}, not T{r['tool']}")
                z = prbs[-1][0][2]
                self.measured = {"tool": r["tool"], "touch_z": z, "offset": tl.get("offset"), "t": time.time()}
                r.update(stage="done", msg=f"T{r['tool']} measured by the machine: setter touch at machine Z {z:.3f}, length offset {tl.get('offset')}")
            elif kind == "3D probe":
                r.update(stage="done", **self._probe3d_result(r, prbs, st))
            else:
                z = prbs[-1][0][2]
                r.update(stage="done", touch=prbs[-1][0], touches=[p_[0][2] for p_ in prbs], wpos=st.get("wpos"),
                         msg=f"surface touched at machine Z {z:.3f} (first {prbs[0][0][2]:.3f}); work Z zero set there "
                             f"(the probe is now {st['wpos'][2]:.3f} above it)")
            self._note(r["msg"])
        except Refused as e:
            r.update(stage="failed", msg=str(e))
            self.armed = False
            self._note(f"{kind} failed: {e}")
        except asyncio.CancelledError:
            r.update(stage="failed", msg="stopped watching (cancelled)")
        await self._push()

    def _start_routine(self, kind: str, command: str, timeout: float, **info) -> None:
        if self.routine and self.routine["stage"] in ("running", "awaiting"):
            raise Refused(f"{self.routine['kind']} is still running")
        since = time.time()
        self.routine = {"kind": kind, "command": command, "stage": "running", "msg": f"{kind}: {command}", **info}
        self._rt_task = asyncio.create_task(self._watch_routine(kind, since, timeout))

    async def change_tool(self, number: int) -> dict:
        """Makera's manual tool change (M6 Tn) - or M491 to re-measure when the number isn't changing. Either way the
        machine measures the tool on its setter (the rule: every tool, the probe included)."""
        if not isinstance(number, int) or not (number in (0, self.profile.probe3d_tool) or 1 <= number <= 99):
            raise Refused(f"tool number: 0 (probe), {self.profile.probe3d_tool} (3D probe) or 1..99")
        st = self._need(armed=True, idle=True, homed=True)
        current = (st.get("tool") or {}).get("number")
        self.measured = None
        cmd = "M491" if current == number else f"M6 T{number}"
        self._start_routine("re-measure" if cmd == "M491" else "tool change", cmd, 600.0, tool=number)
        await self.link.line(cmd)
        self._note(f"{cmd} sent")
        return self.payload()

    async def confirm_tool_change(self) -> dict:
        """The 'continue' of Makera's tool change (same as the front button): M490.2."""
        r = self.routine
        st = self._status() or {}
        j = self.job
        if j and j["stage"] == "awaiting" and st.get("state") == "Tool":
            await self.link.line("M490.2")
            j.update(stage="running", confirmed=True, msg=f"measuring T{j.get('want_tool')} on the setter, then the program continues")
            return self.payload()
        if not r or r["stage"] != "awaiting" or st.get("state") != "Tool":
            raise Refused("the machine isn't waiting for a tool change")
        await self.link.line("M490.2")
        r.update(stage="running", msg="measuring the tool on the setter")
        return self.payload()

    async def set_origin_anchor(self) -> dict:
        """Work X/Y origin = Anchor 1 (the L-bracket's inside corner), as MakeraStudio does: G10 L2 P0 X Y. Z untouched."""
        self._need(armed=True, idle=True, homed=True)
        ax, ay = self.profile.anchor1
        await self.link.line(f"G10 L2 P0 X{ax:.3f} Y{ay:.3f}")
        self._note(f"work XY origin set to Anchor 1 (machine X{ax:.2f} Y{ay:.2f})")
        return self.payload()

    async def auto_z_probe(self, wx: float | None = None, wy: float | None = None) -> dict:
        """Makera's auto Z probe (M495 X Y O0 F0) at work X,Y (default: where the head is). Sets work Z zero on the
        surface. Only with the probe (T0) fitted and measured by the machine."""
        st = self._need(armed=True, idle=True, homed=True)
        self._need_tool_measured()
        if (st.get("tool") or {}).get("number") != 0:
            raise Refused("the auto Z probe needs the probe (T0) fitted and measured: Change tool → 0")
        wp = st.get("wpos") or [None, None, None]
        wx = wp[0] if wx is None else wx
        wy = wp[1] if wy is None else wy
        mx, my = st["mpos"][0] + (wx - wp[0]), st["mpos"][1] + (wy - wp[1])
        for ax, v in (("x", mx), ("y", my)):
            lo, hi = -self.profile.travel[ax] + self.profile.margin, self.profile.top
            if not (lo - 1e-9 <= v <= hi + 1e-9):
                raise Refused(f"that point is outside the travel ({ax.upper()} {v:.2f} machine)")
        cmd = f"M495 X{wx:.3f} Y{wy:.3f} O0 F0"
        self._start_routine("auto Z probe", cmd, 300.0, at=[mx, my])
        await self.link.line(cmd)
        self._note(f"{cmd} sent (Makera's auto Z probe at machine X{mx:.2f} Y{my:.2f})")
        return self.payload()

    async def lower_to(self, height: float, feed: float = 300.0) -> dict:
        """Lower Z (only down, XY untouched) to `height` mm above work Z zero, e.g. the 5 mm a 3D-probe routine starts
        from. Needs the fitted tool measured, so the work Z includes its length; never below the Z floor."""
        if not isinstance(height, (int, float)) or height != height:
            raise Refused("give a height above work zero")
        st = self._need(armed=True, idle=True, homed=True)
        self._need_tool_measured()
        if self.jog_busy:
            raise Refused("the previous move hasn't finished")
        if not (self.z_floor - 1e-9 <= height <= 50.0):
            raise Refused(f"height must be {self.z_floor:g} (the Z floor) .. 50 mm above work zero")
        wz = (st.get("wpos") or [None] * 3)[2]
        if wz is None:
            raise Refused("no work position from the machine")
        if wz <= height + 0.01:
            raise Refused(f"the tool is already at {wz:.2f} mm above work zero: this only lowers")
        mz = st["mpos"][2] - (wz - height)
        if mz < -self.profile.travel["z"] + self.profile.margin:
            raise Refused(f"that is machine Z {mz:.2f}, below the travel")
        feed = min(max(feed, 1.0), 300.0)
        self.last_action = time.time()
        self.jog_busy = {"target": [st["mpos"][0], st["mpos"][1], mz], "t0": time.time(),
                         "deadline": time.time() + (wz - height) / feed * 60.0 * 2 + 5.0}
        await self.link.line(f"G53 G1 Z{mz:.3f} F{feed:g}")
        self._note(f"lower to {height:g} mm above work zero (machine Z {mz:.2f}) at {feed:g} mm/min")
        return self.payload()

    def check_probe_3d(self, kind: str, x: float = 20.0, y: float = 20.0, depth: float = 2.0, dia: float = 2.0,
                       max_drop: float = PROBE3D_MAX_DROP) -> dict:
        """Validate a 3D-probe routine from where the head is now; returns the routine's info (command, guard box)."""
        if kind not in PROBE3D:
            raise Refused(f"unknown 3D probe routine {kind!r} (one of {', '.join(PROBE3D)})")
        for v in (x, y, depth, dia, max_drop):
            if not isinstance(v, (int, float)) or v != v:
                raise Refused("3D probe settings must be numbers")
        st = self._need(armed=True, idle=True, homed=True)
        self._need_tool_measured()
        if self.jog_busy:
            raise Refused("the previous move hasn't finished")
        tl = (st.get("tool") or {}).get("number")
        if tl != self.profile.probe3d_tool:
            raise Refused(f"the 3D probe routines need the 3D probe (T{self.profile.probe3d_tool}) fitted and measured: "
                          f"the machine says T{tl}. Change tool → {self.profile.probe3d_tool}")
        sub, sx, sy = PROBE3D[kind]
        centre = sub >= 9
        lo_off = 0.0 if centre else 2.0
        if not (lo_off <= x <= 80 and lo_off <= y <= 80) or (centre and x == 0 and y == 0):
            raise Refused("X/Y offsets: 2 .. 80 mm" + (" (0 skips that axis, not both)" if centre else ""))
        if not (0.5 <= depth <= 10):
            raise Refused("probing depth: 0.5 .. 10 mm below the touched surface")
        if not (0.5 <= dia <= 10):
            raise Refused("probe ball diameter: 0.5 .. 10 mm")
        if not (2 <= max_drop <= 30):
            raise Refused("Z search limit: 2 .. 30 mm")
        mx, my, mz = st["mpos"]
        xd, yd = sx * x, sy * y
        if sub <= 4:                                     # out past the edge, then back in
            xs, ys = (mx, mx - xd), (my, my + yd)
        elif sub <= 8:                                   # in from the outside, then back to the walls
            xs, ys = (mx, mx + xd), (my, my - yd)
        elif sub == 9:                                   # both walls of a pocket from the middle
            xs, ys = (mx - x, mx + x), (my - y, my + y)
        else:                                            # both sides of a boss: out by the offset, the far side +5
            xs, ys = (mx - x, mx + 2 * x + 5 if x else mx), (my - y, my + 2 * y + 5 if y else my)
        pad = 1.5                                        # retracts and overtravel
        box = (min(xs) - pad, max(xs) + pad, min(ys) - pad, max(ys) + pad)
        for ax, lo_, hi_ in (("x", box[0], box[1]), ("y", box[2], box[3])):
            lo, hi = -self.profile.travel[ax] + self.profile.margin, self.profile.top
            if lo_ < lo - 1e-9 or hi_ > hi + 1e-9:
                raise Refused(f"the routine would reach {ax.upper()} {lo_:.1f} .. {hi_:.1f} (machine), outside "
                              f"{lo:.1f} .. {hi:.1f}: start further from the edge of the travel or use smaller offsets")
        n_axes = (x > 0) + (y > 0) if centre else 2
        expect = (0 if sub == 9 else 2) + n_axes * (4 if centre else 2)
        cmd = f"M480.{sub} X{x:g} Y{y:g} Z{depth:g} D{dia:g}"
        return {"command": cmd, "sub": sub, "label": kind.replace("_", " "), "start": [mx, my, mz], "dia": dia,
                "expect_prbs": expect, "x_off": x, "y_off": y,
                "guard": {"box": box, "start_z": mz, "max_drop": max_drop, "depth": depth, "z_probe": sub != 9}}

    async def probe_3d(self, kind: str, x: float = 20.0, y: float = 20.0, depth: float = 2.0, dia: float = 2.0,
                       max_drop: float = PROBE3D_MAX_DROP) -> dict:
        """Makera's 3D-probe routine (M480.n) from where the head is now. The firmware moves, probes and overwrites the
        work origin itself; we check before, then watch: the head must stay in the routine's area and Z may not drop
        more than `max_drop` before the first touch (a probe that never triggers) - either sends a feed hold."""
        info = self.check_probe_3d(kind, x, y, depth, dia, max_drop)
        cmd = info.pop("command")
        self.last_action = time.time()
        self._start_routine("3D probe", cmd, 300.0, **info)
        await self.link.line(cmd)
        self._note(f"{cmd} sent (Makera's 3D probe, {info['label']}, from machine X{info['start'][0]:.2f} "
                   f"Y{info['start'][1]:.2f} Z{info['start'][2]:.2f})")
        return self.payload()

    def _guard_breach(self, g: dict, mp: list, since: float) -> str | None:
        x, y, z = mp
        xlo, xhi, ylo, yhi = g["box"]
        if not (xlo <= x <= xhi and ylo <= y <= yhi):
            return f"the head left the routine's area (machine X{x:.2f} Y{y:.2f})"
        prbs = self._prbs(since)
        if not g["z_probe"]:
            floor = g["start_z"] - 0.5
        elif prbs:
            floor = prbs[0][0][2] - g["depth"] - 0.5
        else:
            floor = g["start_z"] - g["max_drop"]
        if z < floor:
            return (f"Z went to {z:.2f} (machine), below {floor:.2f}"
                    + (" with nothing touched: is the probe triggering?" if g["z_probe"] and not prbs else ""))
        return None

    def _probe3d_result(self, r: dict, prbs: list, st: dict) -> dict:
        """What the firmware set, read back from the machine: work origin = MPos - WPos (Z includes the tool length)."""
        if len(prbs) != r["expect_prbs"]:
            raise Refused(f"the routine reported {len(prbs)} touches, not {r['expect_prbs']}: the work origin may be "
                          "wrong, check it before cutting")
        wco = [m - w for m, w in zip(st["mpos"], st["wpos"])]
        out = {"origin": wco[:2], "touches": [p_[0] for p_ in prbs]}
        sub = r["sub"]
        msg = f"{r['label']}: work X0 Y0 now at machine X{wco[0]:.3f} Y{wco[1]:.3f}"
        if sub != 9:
            out["surface_z"] = prbs[0][0][2]
            msg += f", Z0 on the surface (touched at machine Z {prbs[0][0][2]:.3f})"
        if sub <= 8:
            ax, ay = self.profile.anchor1
            msg += f"; that is {wco[0] - ax:+.3f}, {wco[1] - ay:+.3f} from Anchor 1"
        else:
            sizes, k = {}, (0 if sub == 9 else 2)
            for i, (ax_, off) in enumerate((("x", r["x_off"]), ("y", r["y_off"]))):
                if off:
                    a_, b_ = prbs[k + 1][0][i], prbs[k + 3][0][i]
                    sizes[ax_] = abs(b_ - a_) + (r["dia"] if sub == 9 else -r["dia"])
                    k += 4
            out["size"] = sizes
            msg += "; measured " + ", ".join(f"{a.upper()} {v:.3f} mm" for a, v in sizes.items())
        out["msg"] = msg
        return out

    # -- programs: upload to the machine's SD, then play (as MakeraStudio does); the machine runs them on its own
    @property
    def job_active(self) -> bool:
        return bool(self.job and self.job["stage"] in ("uploading", "starting", "running", "awaiting", "paused"))

    def check_job(self, prep: dict) -> dict:
        """Everything that must be true before a program is sent: returns notes for the confirmation, raises Refused."""
        st = self._need(armed=True, idle=True, homed=True)
        if self.job_active:
            raise Refused(f"a program is already {self.job['stage']}")
        if self.routine and self.routine["stage"] in ("running", "awaiting"):
            raise Refused(f"{self.routine['kind']} is still running")
        if self.jog_busy:
            raise Refused("the previous move hasn't finished")
        if not prep["lines"]:
            raise Refused("the program is empty")
        notes = []
        fitted = (st.get("tool") or {}).get("number")
        first = prep["tools"][0] if prep["tools"] else None
        if first is None:
            raise Refused("the program never selects a tool (no M6 Tn)")
        if first == fitted and not self.tool_ok():
            raise Refused(f"T{fitted} is fitted but hasn't been measured this session: Change tool → {fitted} first")
        notes.append(f"starts with T{first}" + (" (fitted and measured)" if first == fitted else
                                                f": the machine will stop for the change from T{fitted} and measure it"))
        wco = [m - w for m, w in zip(st["mpos"], st["wpos"])]
        for ax, i in (("x", 0), ("y", 1)):
            e = prep["extents"].get(ax)
            if not e:
                continue
            lo, hi = -self.profile.travel[ax] + self.profile.margin, self.profile.top
            a, b = e[0] + wco[i], e[1] + wco[i]
            if a < lo - 1e-6 or b > hi + 1e-6:
                raise Refused(f"the program reaches machine {ax.upper()} {a:.1f} .. {b:.1f} from the current work origin, "
                              f"outside {lo:.1f} .. {hi:.1f}: check the work origin (Machine tab) and the setup")
        ez = prep["extents"].get("z")
        if ez:
            notes.append(f"Z {ez[0]:.2f} .. {ez[1]:.2f} above work zero" + (f" (lifted {prep['air_cut']:g} mm: air cut)" if prep["air_cut"] else ""))
            if ez[1] + wco[2] > self.profile.top + 1e-6:
                raise Refused(f"the program's highest Z ({ez[1]:.1f} in work coordinates) is above the machine's travel "
                              "with the current work zero" + (": use a smaller air-cut lift" if prep["air_cut"] else ""))
        return {"notes": notes, "work_origin": wco}

    async def run_program(self, name: str, prep: dict, info: dict | None = None) -> dict:
        """Upload `prep["text"]` as /sd/gcodes/<name> and play it. `prep` comes from prepare_program()."""
        chk = self.check_job(prep)
        fname = program_filename(name)
        path = "/sd/gcodes/" + fname
        self.last_action = time.time()
        self.job = {"name": fname, "path": path, "stage": "uploading", "upload": 0.0, "lines": prep["lines"],
                    "ops": prep["ops"], "tools": prep["tools"], "air_cut": prep["air_cut"], "line": 0, "percent": 0,
                    "seconds": 0, "op": None, "msg": "uploading", "notes": chk["notes"], **(info or {})}
        self._note(f"program {fname}: {prep['lines']} lines, {len(prep['ops'])} operation(s)" + (f", air cut +{prep['air_cut']:g} mm" if prep["air_cut"] else ""))
        self._job_task = asyncio.create_task(self._job_flow(path, prep["text"].encode()))
        return self.payload()

    async def _job_flow(self, path: str, data: bytes) -> None:
        j = self.job
        try:
            def prog(f):
                j["upload"] = round(f, 3)
            await self.link.upload(path, data, prog)
            j.update(stage="starting", msg="starting")
            st = self._status() or {}
            if st.get("state") != "Idle" or not self.armed:
                raise Refused("the machine is no longer ready (Control off or not Idle): the program was uploaded but not started")
            await self.link.raw(f"play {escape_path(path)}")
            self._note(f"play {path}")
            t0, seen_run, last_progress = time.time(), False, 0.0
            while True:
                await asyncio.sleep(0.2)
                if j["stage"] == "aborted":
                    break
                if not self.connected:
                    raise Refused("connection lost while the program runs: the machine carries on by itself; reconnect to watch it")
                st = self._status() or {}
                s = st.get("state")
                pr = st.get("progress")
                if pr:
                    seen_run, last_progress = True, time.time()
                    j.update(line=int(pr["lines"] or 0), percent=pr.get("percent") or 0, seconds=pr.get("seconds") or 0)
                    j["op"] = next((i for i, o in enumerate(j["ops"]) if o["start"] <= j["line"] <= o["end"]), j.get("op"))
                if s == "Alarm":
                    h = st.get("halt") or {}
                    raise Refused(f"the machine alarmed: {h.get('reason', 'halted')} ({h.get('advice', 'Unlock, then home')})")
                if s == "Tool":
                    o = j["ops"][j["op"]] if j.get("op") is not None and j["op"] < len(j["ops"]) else None
                    want = (o or {}).get("tool") or (j["tools"][0] if j["tools"] else None)
                    j["changing"] = True
                    if j["stage"] != "awaiting" and not j.get("confirmed"):
                        j.update(stage="awaiting", want_tool=want, msg=f"tool change: fit T{want}, then press the front button (or Confirm): the machine measures it")
                        await self._push()
                    continue
                if j.get("changing") and s in ("Run", "Idle", "Hold"):
                    tl = st.get("tool") or {}                     # T-1 while it measures; the new number once it has
                    if tl.get("number") == j.get("want_tool"):
                        self.measured = {"tool": tl.get("number"), "touch_z": None, "offset": tl.get("offset"), "t": time.time()}
                        j.update(changing=False, confirmed=False, stage="running", msg="running")
                    elif j["stage"] == "awaiting":
                        j.update(stage="running", msg=f"measuring T{j.get('want_tool')} on the setter")
                if s == "Pause":
                    if j["stage"] != "paused":
                        j.update(stage="paused", msg="paused: Resume continues, Stop aborts")
                    continue
                if s in ("Run", "Hold") or pr:
                    seen_run = True
                    if j["stage"] in ("starting", "paused"):
                        j.update(stage="running", msg="running")
                if s == "Idle" and seen_run and not pr and time.time() - last_progress > 1.5:
                    j.update(stage="done", percent=100, msg=f"finished: {j['lines']} lines in {int(j['seconds']) // 60}:{int(j['seconds']) % 60:02d}")
                    break
                if s == "Idle" and not seen_run and time.time() - t0 > 6:
                    raise Refused("the machine didn't start the program (is it homed? the file name accepted?)")
        except Refused as e:
            j.update(stage="failed", msg=str(e))
        except (ConnectionError, OSError) as e:
            j.update(stage="failed", msg=f"{type(e).__name__}: {e}")
        except asyncio.CancelledError:
            j.update(stage="failed", msg="stopped watching (cancelled)")
        self._note(f"program {j['name']}: {j['stage']} — {j['msg']}")
        await self._push()

    async def job_pause(self) -> dict:
        if not self.job or self.job["stage"] != "running":
            raise Refused("no program is running")
        await self.link.raw("suspend")
        self._note("pause (suspend): the machine finishes the current moves, then stops")
        return self.payload()

    async def job_resume(self) -> dict:
        self._need(armed=True, idle=False)
        if not self.job or self.job["stage"] != "paused":
            raise Refused("the program isn't paused")
        self.last_action = time.time()
        await self.link.raw("resume")
        self._note("resume")
        return self.payload()

    async def job_abort(self) -> dict:
        if not self.connected:
            raise Refused("not connected")
        if not self.job_active:
            raise Refused("no program is running")
        await self.link.raw("abort")
        self._note("abort: the machine stops the program and the spindle")
        if self.job["stage"] == "uploading" and self._job_task:
            self._job_task.cancel()
        self.job.update(stage="aborted", msg="aborted")
        return self.payload()

    async def home(self) -> dict:
        st = self._need(armed=True, idle=False)
        if st["state"] not in ("Idle", "Alarm"):
            raise Refused(f"the machine is {st['state']}: it can only home from Idle or Alarm")
        self.last_action = time.time()
        self.homed = False
        self._homing, self._saw_home_state, self._homing_t0 = True, False, time.time()
        await self.link.line("$H")
        self._note("homing (Z goes up first)")
        return self.payload()

    async def unlock(self) -> dict:
        st = self._need(armed=False, idle=False)
        if st["state"] != "Alarm":
            raise Refused("unlock is only for an Alarm")
        await self.link.line("$X")
        self._note("unlock ($X): home before jogging")
        self.homed = False
        return self.payload()

    async def hold(self) -> dict:
        if not self.connected:
            raise Refused("not connected")
        await self.link.realtime(b"!")
        self._note("feed hold")
        return self.payload()

    async def resume(self) -> dict:
        st = self._need(armed=True, idle=False)
        if st["state"] not in ("Hold", "Pause"):
            raise Refused(f"nothing to resume (the machine is {st['state']})")
        self.last_action = time.time()
        await self.link.realtime(b"~")
        self._note("resume")
        return self.payload()

    async def halt(self) -> dict:
        """Stop now: the firmware halts motion and the spindle and goes to Alarm. Always allowed."""
        if not self.connected:
            raise Refused("not connected")
        await self.link.realtime(HALT)
        self.armed = False
        self.homed = False
        self.jog_busy = None
        self._note("HALT sent: Control off; unlock and home before moving again")
        return self.payload()

    async def switch(self, name: str, on: bool) -> dict:
        if name not in self.profile.switches:
            raise Refused(f"unknown switch {name!r}")
        self._need(armed=False, idle=False)
        on_cmd, off_cmd = self.profile.switches[name]
        await self.link.line(on_cmd if on else off_cmd)
        self.switch_state[name] = bool(on)
        self._note(f"{name} {'on' if on else 'off'}")
        return self.payload()

    async def diagnose(self, timeout: float = 2.0) -> dict:
        """Read-only pin report (`diagnose`): |P:a,b = probe / tool-setter pins, |I:n = e-stop, |E: endstops, ..."""
        if not self.connected:
            raise Refused("not connected")
        t0 = time.time()
        await self.link.line("diagnose")
        while time.time() - t0 < timeout:
            for t, ln in reversed(self.link.log):
                if t >= t0 and "|P:" in ln:
                    out: dict[str, Any] = {"raw": ln}
                    for part in ln.strip("{}").split("|"):
                        if ":" in part:
                            k, v = part.split(":", 1)
                            out[k.strip()] = [int(x) if x.strip().lstrip("-").isdigit() else x for x in v.split(",")]
                    return out
            await asyncio.sleep(0.05)
        raise Refused("no diagnose reply from the machine")

    def set_z_floor(self, z: float) -> dict:
        if not isinstance(z, (int, float)) or z != z or z < 0 or z > 100:
            raise Refused("the Z floor is 0 .. 100 mm above work zero")
        self.z_floor = float(z)
        return self.payload()


# ------------------------------------------------------------------ camera
class CameraRelay:
    """One upstream camera socket shared by every viewer; it runs only while someone is watching."""

    def __init__(self):
        self.host: str | None = None
        self.port = 82
        self.frame: bytes | None = None
        self.frame_t = 0.0
        self.viewers = 0
        self.error: str | None = None
        self._task: asyncio.Task | None = None
        self._cond = asyncio.Condition()

    def set_host(self, host: str | None, port: int = 82) -> None:
        if host != self.host or port != self.port:
            self.host, self.port = host, port
            self.frame, self.frame_t = None, 0.0
            if self._task:
                self._task.cancel()
                self._task = None

    def _ensure(self) -> None:
        if self.host and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        import websockets
        url = f"ws://{self.host}:{self.port}/ws_video"
        while self.viewers > 0 and self.host:
            try:
                async with websockets.connect(url, open_timeout=6, max_size=4_000_000, ping_interval=None) as ws:
                    await ws.send("start_stream")
                    self.error = None
                    try:
                        while self.viewers > 0:
                            m = await asyncio.wait_for(ws.recv(), 6)
                            if isinstance(m, (bytes, bytearray)) and m[:2] == b"\xff\xd8":
                                async with self._cond:
                                    self.frame, self.frame_t = bytes(m), time.time()
                                    self._cond.notify_all()
                    finally:
                        try:
                            await ws.send("stop_stream")
                        except Exception:  # noqa: BLE001
                            pass
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001
                self.error = f"camera: {type(e).__name__}: {e}"
                await asyncio.sleep(2.0)

    async def frames(self, max_fps: float = 12.0):
        """Async generator of JPEG frames for one viewer."""
        self.viewers += 1
        self._ensure()
        last = 0.0
        try:
            while True:
                async with self._cond:
                    try:
                        await asyncio.wait_for(self._cond.wait_for(lambda: self.frame is not None and self.frame_t > last), 8)
                    except asyncio.TimeoutError:
                        continue
                    fr, last = self.frame, self.frame_t
                yield fr
                await asyncio.sleep(1.0 / max_fps)
        finally:
            self.viewers -= 1

    async def snapshot(self, timeout: float = 8.0) -> bytes:
        """One current frame (starts the stream briefly if nobody is watching)."""
        if not self.host:
            raise Refused("not connected to a machine")
        if self.frame and time.time() - self.frame_t < 1.0:
            return self.frame
        gen = self.frames()
        try:
            return await asyncio.wait_for(gen.__anext__(), timeout)
        finally:
            await gen.aclose()
