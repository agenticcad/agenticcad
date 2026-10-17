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
  * No free-form G-code, no spindle, no program running from here (yet).
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
        if n < 3 or n > 8192:
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

    async def _poll(self) -> None:
        try:
            while self.connected:
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


Z1 = Profile("Makera Z1", travel={"x": 200.0, "y": 200.0, "z": 100.0}, max_rate={"x": 1200.0, "y": 1200.0, "z": 600.0},
             switches={"dust": ("M851", "M852"), "light": ("M821", "M822"), "probe_laser": ("M831", "M832")})

JOG_MAX = {"xy": 10.0, "z_up": 10.0, "z_down": 1.0}          # mm per click
GOTO_FEED_MAX = 600.0                                        # mm/min cap for go-to moves
Z_TOP_BAND = 1.5                                             # go-to moves only while machine Z is within this of the top
JOG_SCALE = {"xy": 0.5, "z_up": 0.5, "z_down": 0.25}         # fraction of the axis max rate ($J F)
ARM_IDLE_S = 300.0                                           # Control turns itself off after this long unused
STALE_S = 2.0                                                # status older than this: treat the link as lost


class Refused(Exception):
    """A command the safety rules don't allow right now; the message says why."""


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
                "log": [t for _, t in (self.link.log[-30:] if self.link else [])],
                "limits": {"jog_max": JOG_MAX, "travel": self.profile.travel, "margin": self.profile.margin, "top": self.profile.top},
                "profile": self.profile.name}

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

    def check_goto_xy(self, x: float, y: float, feed: float) -> tuple[float, float, float]:
        """Validate an absolute machine-coordinate XY move made with Z at the top of travel."""
        for v in (x, y, feed):
            if not isinstance(v, (int, float)) or v != v:
                raise Refused("go-to needs numbers")
        st = self._need(armed=True, idle=True, homed=True)
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

    async def goto_xy(self, x: float, y: float, feed: float = 300.0) -> dict:
        """Move to machine X, Y (G53) at `feed`, Z untouched (it must already be at the top)."""
        x, y, feed = self.check_goto_xy(x, y, feed)
        st = self._status()
        target = [x, y, st["mpos"][2]]
        dist = ((x - st["mpos"][0]) ** 2 + (y - st["mpos"][1]) ** 2) ** 0.5
        self.last_action = time.time()
        self.jog_busy = {"target": target, "t0": time.time(), "deadline": time.time() + dist / feed * 60.0 * 2 + 5.0}
        await self.link.line(f"G53 G1 X{x:.3f} Y{y:.3f} F{feed:g}")
        self._note(f"go to machine X{x:.2f} Y{y:.2f} at {feed:g} mm/min")
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
