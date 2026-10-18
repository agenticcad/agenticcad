"""Makera Z1 emulator for developing and testing machine control without the machine.

Speaks what machine_link.py speaks (read from MakeraZ1Firmware and checked against a real Z1): framed TCP on 2222,
'?' status replies, $H (Z first, then XY, ends at MPos -1,-1,-1), $J relative jogs at a fraction of the axis max
rate, '!' hold / '~' resume / 0x18 halt -> Alarm, $X unlock, M851/M852 (external output: dust collector), M821/M822
(light), a connection cap, and the /ws_video camera on another port (test-card JPEGs). Like the real firmware it does
NOT enforce soft limits: a jog past the end just goes there (and is recorded in `crashes`) so tests can prove the
guard in machine_link refuses it first.

    python tools/z1_emu.py [--port 2222] [--cam-port 8282] [--speed 10]
"""
from __future__ import annotations

import argparse
import asyncio
import io
import math
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from machine_link import (T_FILE_CAN, T_FILE_DATA, T_FILE_END, T_FILE_MD5, T_FILE_START, T_FILE_VIEW, T_LINE,  # noqa: E402
                          T_REALTIME, T_STATUS, T_TEXT, PATH_ESC, frame, parse_frames)


class Z1Emu:
    TRAVEL = {"x": 206.0, "y": 206.0, "z": 102.0}       # firmware soft_endstop.* (where the real axes end)
    MAX_RATE = {"x": 1200.0, "y": 1200.0, "z": 600.0}   # mm/min

    def __init__(self, speed: float = 1.0, max_clients: int = 2, homed: bool = True):
        self.speed = speed                              # >1 runs moves faster than real time (tests)
        self.max_clients = max_clients
        self.mpos = [-1.0, -1.0, -1.0] if homed else [-50.0, -40.0, -20.0]
        self.wco = [-148.8, -125.4, -61.36]             # work offset: WPos = MPos - wco (as on the real Z1)
        self.state = "Idle"
        self.halted = False
        self.switches = {"extendout": False, "light": True, "probe_laser": False, "spindle_fan": False}
        self.fan_power = 0
        self.lines: list[str] = []                      # every console line received
        self.replies: list[str] = []                    # delayed console replies (probe results)
        self.realtime: list[bytes] = []
        self.crashes: list[str] = []
        self.setter_z = -61.0                           # machine Z at which the fitted tool trips the tool setter
        self.tool = 6
        self._tool_wait = asyncio.Event()
        # the surface under the probe tip, machine Z: stock 100x100 at the anchor, MDF elsewhere
        self.surface_z = lambda x, y: -55.0 if (-191.28 <= x <= -91.28 and -194.45 <= y <= -94.45) else -75.0
        self.stock = (-191.28, -91.28, -194.45, -94.45)  # the stock's XY box (machine), its top at surface_z
        self.pocket: tuple | None = None                # optional (x0, x1, y0, y1, floor machine Z) cut into the stock
        self.ball_r = 1.0                               # 3D probe ball radius (2 mm ruby)
        self.probe3d_fail = False                       # True: the probe never triggers (tests the Z-drop guard)
        self.probe_start_delay = 0.8                    # s of 'Idle' before a G38.6 starts moving (as seen on a real Z1)
        self.sd: dict[str, bytes] = {}                 # the machine's SD card (the ESP32 side): path -> file
        self._up: dict | None = None                    # an upload in progress {path, md5, count, size, got}
        self.play: dict | None = None                   # the program being played {path, lines, i, t0, pct}
        self._play_task: asyncio.Task | None = None
        self.raw_lines: list[str] = []                  # console lines exactly as received (to check: no newline on play)
        self.clients = 0
        self._move: asyncio.Task | None = None
        self._hold = asyncio.Event()
        self._hold.set()
        self.servers = []

    # ---- status
    def status(self) -> str:
        w = [m - o for m, o in zip(self.mpos, self.wco)]
        f = lambda v: ",".join(f"{a:.4f}" for a in v)
        return (f"<{self.state}|MPos:{f(self.mpos)},0.0000,0.0000|WPos:{f(w)},0.0000,0.0000|F:0.0,2000.0,100.0"
                f"|S:0.0,10000.0,100.0,0,27.2,27.4,0,0,0,0|T:{self.tool},-17.297,-1|L:0, 0, 0, 0.0,100.0"
                + ("|H:1" if self.halted else "")
                + (f"|P:{self.play['i']},{self.play['pct']},{int(time.time() - self.play['t0'])}" if self.play else "")
                + "|C:3,1,0,1|E:0,0,0,1,7613|OTA:0,0>")

    # ---- motion
    async def _move_to(self, target, rate_mm_min):
        start = list(self.mpos)
        dist = math.dist(start, target)
        dur = dist / max(rate_mm_min, 1e-6) * 60.0 / self.speed
        t0 = time.time()
        done = 0.0
        while done < 1.0:
            await self._hold.wait()
            if self.halted:
                return
            await asyncio.sleep(0.01)
            done = 1.0 if dur <= 0 else min(1.0, (time.time() - t0) / dur)
            self.mpos = [a + (b - a) * done for a, b in zip(start, target)]
        for ax, v in zip("xyz", self.mpos):
            if v > 0.0 + 1e-9 or v < -self.TRAVEL[ax] - 1e-9:
                self.crashes.append(f"{ax.upper()} ran to {v:.3f} (outside 0 .. {-self.TRAVEL[ax]:g})")
        self.state = "Idle"

    async def _go(self, x=None, y=None, z=None, rate=1500.0):
        tgt = [self.mpos[0] if x is None else x, self.mpos[1] if y is None else y, self.mpos[2] if z is None else z]
        await self._move_to(tgt, rate)
        self.state = "Run"

    async def _touch(self, surface, rate):
        stop = surface
        await self._move_to([self.mpos[0], self.mpos[1], stop], rate)
        self.state = "Run"
        self.replies.append(f"[PRB:{self.mpos[0]:.3f},{self.mpos[1]:.3f},{self.mpos[2]:.3f}:1]")

    async def _routine(self, change_to):
        """M6 Tn (change_to=n: park, TOOL wait, then measure) or M491 (change_to=None: measure the fitted tool)."""
        last = list(self.mpos)
        await self._go(z=-3.0)
        if change_to is not None:
            await self._go(-11.6, -14.6)
            self._tool_wait.clear()
            self.state = "Tool"
            await self._tool_wait.wait()
            self.state = "Run"
        await self._go(z=-1.0)
        await self._go(-191.28 + 181, -194.45 + 181)
        await self._touch(self.setter_z, 500.0)
        await self._go(z=self.mpos[2] + 1.0)
        await self._touch(self.setter_z, 100.0)
        if change_to is not None:
            self.tool = change_to
        await self._go(z=-20.0)
        await self._go(z=-3.0)
        await self._go(last[0], last[1])
        self.replies.append("Done ATC")
        self.state = "Idle"

    async def _z_probe(self, wx, wy):
        """M495 X Y O F: Z up, go to work X,Y, touch the surface (500 then 100), set work Z zero there, lift 1 mm."""
        await self._go(z=-1.0)
        await self._go(wx + self.wco[0], wy + self.wco[1])
        surf = self.surface_z(self.mpos[0], self.mpos[1])
        await self._touch(surf, 500.0)
        await self._go(z=self.mpos[2] + 1.0)
        await self._touch(surf, 100.0)
        self.wco[2] = self.mpos[2]
        await self._go(z=self.mpos[2] + 1.0)
        self.replies.append("Done ATC")
        self.state = "Idle"

    # ---- 3D probe (M480.n): the firmware's sequences (ATCHandler fill_OutCorner/InCorner/InPocket/OutPocket_scripts)
    def _in_pocket(self, x, y):
        p_ = self.pocket
        return bool(p_) and p_[0] <= x <= p_[1] and p_[2] <= y <= p_[3]

    def _surface(self, x, y):
        return self.pocket[4] if self._in_pocket(x, y) else self.surface_z(x, y)

    def _solid(self, x, y, z):
        x0, x1, y0, y1 = self.stock
        if not (x0 <= x <= x1 and y0 <= y <= y1) or z >= self.surface_z(x, y):
            return False
        return not (self._in_pocket(x, y) and z > self.pocket[4])

    async def _g38(self, axis, dist, rate):
        """G38.2 along one axis (relative, as on the Z1): stop where the ball first meets material; no touch = alarm."""
        i = "xyz".index(axis)
        sgn = 1.0 if dist > 0 else -1.0
        hit_at = None
        for k in range(int(abs(dist) / 0.01) + 1):
            q = list(self.mpos)
            q[i] += sgn * k * 0.01
            if self.probe3d_fail:
                break
            if axis == "z":
                touch = q[2] <= self._surface(q[0], q[1])
            else:
                lead = list(q)
                lead[i] += sgn * self.ball_r
                touch = self._solid(*lead)
            if touch:
                hit_at = q
                break
        end = hit_at or [m + (dist if j == i else 0.0) for j, m in enumerate(self.mpos)]
        await self._move_to(end, rate)
        self.state = "Run"
        self.replies.append(f"[PRB:{self.mpos[0]:.3f},{self.mpos[1]:.3f},{self.mpos[2]:.3f}:{int(hit_at is not None)}]")
        if hit_at is None:
            self.replies.append("ALARM: Probe fail")
            self.halted = True
            self.state = "Alarm"
            raise asyncio.CancelledError
        return list(self.mpos)

    def _set_work(self, i, v):
        self.wco[i] = self.mpos[i] - v                  # G10 L20 P0: the current position becomes v

    async def _rel(self, i, d, rate=1500.0):
        t = list(self.mpos)
        t[i] += d
        await self._move_to(t, rate)
        self.state = "Run"

    async def _probe3d(self, sub, X, Y, Z, D):
        r = D / 2
        mpos = list(self.mpos)
        sx, sy = {1: (1, 1), 2: (-1, 1), 3: (-1, -1), 4: (1, -1), 5: (1, 1), 6: (-1, 1), 7: (-1, -1), 8: (1, -1)}.get(sub, (1, 1))
        xd, yd = sx * X, sy * Y
        try:
            if sub != 9:                                # Z: touch twice, Z0 there
                await self._g38("z", -108.0, 100.0)
                self._set_work(2, 0.0)
                await self._rel(2, 1.0)
                await self._g38("z", -108.0, 100.0)
                self._set_work(2, 0.0)
                await self._go(z=mpos[2])
            if sub <= 4:
                await self._rel(0, -xd)
                await self._go(z=self.wco[2] - Z, rate=150.0)
                await self._g38("x", xd, 100.0)
                await self._rel(0, -sx * 1.0)
                await self._g38("x", xd, 50.0)
                self._set_work(0, -sx * r)
                await self._rel(0, -sx * 1.0)
                await self._go(z=mpos[2])
                await self._go(x=mpos[0])
                await self._rel(1, yd)
                await self._go(z=self.wco[2] - Z, rate=150.0)
                await self._g38("y", -yd, 100.0)
                await self._rel(1, sy * 1.0)
                await self._g38("y", -yd, 50.0)
                self._set_work(1, sy * r)
                await self._rel(1, sy * 1.0)
                await self._go(z=mpos[2])
                await self._go(self.wco[0], self.wco[1])
            elif sub <= 8:
                await self._go(mpos[0] + xd, mpos[1] - yd)
                await self._go(z=self.wco[2] - Z, rate=150.0)
                start2 = list(self.mpos)
                await self._g38("x", -xd, 100.0)
                await self._rel(0, sx * 1.0)
                await self._g38("x", -xd, 50.0)
                self._set_work(0, sx * r)
                await self._go(x=start2[0])
                await self._g38("y", yd, 100.0)
                await self._rel(1, -sy * 1.0)
                await self._g38("y", yd, 50.0)
                self._set_work(1, -sy * r)
                await self._rel(1, -sy * 1.0)
                await self._go(z=mpos[2])
                await self._go(self.wco[0], self.wco[1])
            else:
                for i, d in ((0, X), (1, Y)):
                    if not d:
                        continue
                    ax = "xy"[i]
                    if sub == 9:
                        await self._g38(ax, -d, 100.0)
                        await self._rel(i, 1.0)
                        left = await self._g38(ax, -d, 50.0)
                        await self._rel(i, 1.0)
                        await self._g38(ax, 2 * d, 100.0)
                        await self._rel(i, -1.0)
                        right = await self._g38(ax, d, 50.0)
                    else:
                        await self._rel(i, -d)
                        await self._go(z=self.wco[2] - Z, rate=150.0)
                        await self._g38(ax, d, 100.0)
                        await self._rel(i, -1.0)
                        left = await self._g38(ax, d, 50.0)
                        await self._rel(i, -1.0)
                        await self._go(z=mpos[2])
                        await self._rel(i, 2 * d + 5)
                        await self._go(z=self.wco[2] - Z, rate=150.0)
                        await self._g38(ax, -2 * d, 100.0)
                        await self._rel(i, 1.0)
                        right = await self._g38(ax, -d, 50.0)
                        await self._rel(i, 1.0)
                        await self._go(z=mpos[2])
                    tgt = [None, None]
                    tgt[i] = left[i] - (left[i] - right[i]) / 2
                    await self._go(*tgt)
                    self._set_work(i, 0.0)
        except asyncio.CancelledError:
            return
        self.replies.append("Done ATC")
        self.state = "Idle"

    async def _home(self):
        for axes in ("z", "xy"):                        # home_z_first true: Z up first, then X and Y
            tgt = list(self.mpos)
            for ax in axes:
                tgt["xyz".index(ax)] = 0.0
            s0 = list(self.mpos)
            dur = max(abs(a - b) for a, b in zip(s0, tgt)) / 1500.0 * 60.0 / self.speed
            t0 = time.time()
            while True:
                if self.halted:
                    return
                await asyncio.sleep(0.01)
                k = 1.0 if dur <= 0 else min(1.0, (time.time() - t0) / dur)
                self.mpos = [a + (b - a) * k for a, b in zip(s0, tgt)]
                if k >= 1.0:
                    break
        self.mpos = [-1.0, -1.0, -1.0]                  # back off the switches
        self.state = "Idle"

    def _start(self, coro, state="Run"):
        if self._move and not self._move.done():
            self._move.cancel()
        self.state = state                              # queued motion counts as running (a hold applies to it)
        self._move = asyncio.create_task(coro)

    # ---- files and programs (upload pull protocol, play / suspend / resume / abort, |P: progress)
    def on_file(self, t: int, p: bytes) -> list[tuple[int, bytes]]:
        """A file-transfer frame from the host -> the frames to send back."""
        out: list[tuple[int, bytes]] = []
        if t == T_FILE_START:
            txt = p.decode(errors="replace")
            for k, v in PATH_ESC.items():
                txt = txt.replace(v, k)
            self._up = {"path": txt.split(" ", 1)[1] if " " in txt else "", "md5": None, "count": 0, "size": 0, "got": {}}
        elif t == T_FILE_MD5 and self._up is not None:
            self._up["md5"] = p.decode()
            out.append((T_FILE_VIEW, b""))                # "send me the file info"
        elif t == T_FILE_VIEW and self._up is not None:
            self._up["count"], self._up["size"] = int.from_bytes(p[:4], "big"), int.from_bytes(p[4:6], "big")
            out.append((T_FILE_DATA, (1).to_bytes(4, "big")))
        elif t == T_FILE_DATA and self._up is not None:
            seq = int.from_bytes(p[:4], "big")
            self._up["got"][seq] = p[4:]
            if seq < self._up["count"]:
                out.append((T_FILE_DATA, (seq + 1).to_bytes(4, "big")))
            else:
                import hashlib
                data = b"".join(self._up["got"][i] for i in range(1, self._up["count"] + 1))
                if hashlib.md5(data).hexdigest() != self._up["md5"]:
                    out.append((T_FILE_CAN, b""))
                else:
                    self.sd[self._up["path"]] = data
                    out.append((T_FILE_END, b""))
                    out.append((T_TEXT, f"Info: upload success: {self._up['path']}.\n".encode()))
                self._up = None
        elif t == T_FILE_CAN:
            self._up = None
        return out

    async def _run_program(self):
        pl = self.play
        try:
            x = y = z = None
            for i, ln in enumerate(pl["lines"], 1):
                while pl.get("paused"):
                    self.state = "Pause"
                    await asyncio.sleep(0.02)
                pl["i"] = i
                pl["pct"] = round(100 * i / len(pl["lines"]))
                u = ln.strip().upper()
                m6 = re.search(r"M0?6\b", u) and re.search(r"T(\d+)", u)
                if m6:
                    n = int(m6.group(1))
                    if n != self.tool:
                        self.replies.append(f"Please change the tool to: T{n}")
                        await self._routine(change_to=n)
                    continue
                words = dict((a, float(v)) for a, v in re.findall(r"([XYZF])(-?\d*\.?\d+)", u))
                if not any(a in words for a in "XYZ"):
                    continue
                if "G53" in u:
                    tgt = [words.get(a, m) for a, m in zip("XYZ", self.mpos)]
                else:
                    x, y, z = words.get("X", x), words.get("Y", y), words.get("Z", z)
                    tgt = [self.mpos[k] if v is None else v + self.wco[k] for k, v in enumerate((x, y, z))]
                self.state = "Run"
                await self._move_to(tgt, words.get("F", 3000.0) if not re.match(r"G0\b", u) else 3000.0)
            pl["pct"] = 100
            self.state = "Idle"
            await asyncio.sleep(1.0)                    # the 100 % report is held for a second, then the field goes
        except asyncio.CancelledError:
            pass
        finally:
            if self.play is pl:
                self.play = None
            if self.state in ("Run", "Pause"):
                self.state = "Idle"

    # ---- commands
    def on_line(self, line: str) -> str | None:
        self.raw_lines.append(line)
        s = line.strip()
        self.lines.append(s)
        if s.startswith("play "):
            path = s[5:]
            for k, v in PATH_ESC.items():
                path = path.replace(v, k)
            if line.endswith("\n"):
                return "Error:start check failed.file does not exist or CRC wrong [P1]"
            if self.halted or self.play:
                return "Currently printing, abort print first" if self.play else None
            if path not in self.sd:
                return "Error:open file failed[P0]"
            self.play = {"path": path, "lines": self.sd[path].decode().splitlines(), "i": 0, "pct": 0, "t0": time.time()}
            self.state = "Run"
            self._play_task = asyncio.create_task(self._run_program())
            return None
        if s == "suspend" and self.play:
            self.play["paused"] = True
            return None
        if s == "resume" and self.play:
            self.play["paused"] = False
            self.state = "Run"
            return None
        if s == "abort" and self.play:
            if self._play_task:
                self._play_task.cancel()
            self.play = None
            self.state = "Idle"
            return "Aborted playing or paused file."
        u = s.upper()
        if u.startswith("$X"):
            if self.halted or self.state == "Alarm":
                self.halted = False
                self.state = "Idle"
                return "[Caution: Unlocked]\nok"
            return "ok"
        if self.halted:
            return "!! halted, use $X"
        if u.startswith("$H"):
            self._start(self._home(), "Home")
            return "ok"
        if u.startswith("$J"):
            delta = {"x": 0.0, "y": 0.0, "z": 0.0}
            scale = 1.0
            for tok in s.split()[1:]:
                k = tok[0].lower()
                if k == "f":
                    scale = float(tok[1:])
                elif k in delta:
                    delta[k] = float(tok[1:])
            moving = [a for a, d in delta.items() if d]
            if not moving:
                return "error:no delta jog specified"
            rate = min(self.MAX_RATE[a] for a in moving) * scale
            self._start(self._move_to([m + delta[a] for m, a in zip(self.mpos, "xyz")], rate))
            return None
        if u.startswith("DIAGNOSE"):
            sw = self.switches
            return ("{S:0,10000,0,0,34,30|L:0,0|V:1,45|F:%d,%d|G:%d,0,0,%d,%d|T:%d|C:1|E:0,0,0,0,0,1,1,0|P:%d,0|I:0|RSSI:-40}"
                    % (sw["spindle_fan"], self.fan_power if sw["spindle_fan"] else 0, sw["light"], sw["extendout"], 100 if sw["extendout"] else 0,
                       sw["probe_laser"], int(self.mpos[2] <= self.setter_z + 1e-6)))
        if re.match(r"^M6\b", u) or (u.startswith("T") and " M6" in u):
            n = int(float(re.search(r"T(-?\d+)", u).group(1)))
            if n == self.tool:
                return None                              # the firmware does nothing for the same tool number
            self.replies.append(f"Please change the tool to: T{n}")
            self._start(self._routine(change_to=n), "Run")
            return None
        if u.startswith("M490.2"):
            self._tool_wait.set()
            return "ok"
        if u.startswith("M491"):
            self._start(self._routine(change_to=None), "Run")
            return None
        if u.startswith("M480."):
            m = re.match(r"M480\.(\d+)", u)
            w = dict((t[0].upper(), float(t[1:])) for t in s.split()[1:] if len(t) > 1)
            self._start(self._probe3d(int(m.group(1)), w.get("X", 20.0), w.get("Y", 20.0), w.get("Z", 2.0), w.get("D", 2.0)), "Run")
            return None
        if u.startswith("M495"):
            w = dict((t[0].upper(), float(t[1:])) for t in s.split()[1:] if len(t) > 1)
            self._start(self._z_probe(w.get("X", 0) + w.get("O", 0), w.get("Y", 0) + w.get("F", 0)), "Run")
            return None
        if u.replace(" ", "").startswith("G10L20P0"):
            w = dict((t[0].upper(), float(t[1:])) for t in re.findall(r"[XYZ]-?[\d.]+", s.replace(" ", "")[8:]))
            for i, a_ in enumerate("XYZ"):
                if a_ in w:
                    self._set_work(i, w[a_])
            return "ok"
        if u.startswith("G10 L2 P0") or u.replace(" ", "").startswith("G10L2P0"):
            w = dict((t[0].upper(), float(t[1:])) for t in re.findall(r"[XYZ]-?[\d.]+", s.replace(" ", "")))
            for i, a_ in enumerate("XYZ"):
                if a_ in w:
                    self.wco[i] = w[a_]
            return "ok"
        if u.startswith("G38.2"):
            words = dict((t[0].lower(), float(t[1:])) for t in s.split()[1:] if len(t) > 1)
            z_target = self.mpos[2] + words.get("z", 0.0)
            surf = self.surface_z(self.mpos[0], self.mpos[1])
            stop = max(z_target, surf)
            hit = surf >= z_target
            async def probe2():
                self.state = "Idle"
                await asyncio.sleep(self.probe_start_delay)
                self.state = "Run"
                await self._move_to([self.mpos[0], self.mpos[1], stop], words.get("f", 100.0))
                self.replies.append(f"[PRB:{self.mpos[0]:.3f},{self.mpos[1]:.3f},{self.mpos[2]:.3f}:{int(hit)}]" + ("" if hit else "\nALARM: Probe fail"))
                if not hit:
                    self.halted = True
                    self.state = "Alarm"
            self._start(probe2())
            return None
        if u.startswith("G38.6"):
            words = dict((t[0].lower(), float(t[1:])) for t in s.split()[1:] if len(t) > 1)
            z_target = self.mpos[2] + words.get("z", 0.0)
            stop = max(z_target, self.setter_z)
            hit = self.setter_z >= z_target
            async def probe():
                self.state = "Idle"                     # the real Z1 still reports Idle for a moment after G38.6
                await asyncio.sleep(self.probe_start_delay)
                self.state = "Run"
                await self._move_to([self.mpos[0], self.mpos[1], stop], words.get("f", 100.0))
                self.replies.append(f"[PRB:{self.mpos[0]:.3f},{self.mpos[1]:.3f},{self.mpos[2]:.3f}:{int(hit)}]" + ("" if hit else "\nALARM: Calibrate fail!"))
                if not hit:
                    self.halted = True
                    self.state = "Alarm"
            self._start(probe())
            return None
        if u.startswith("G53"):
            words = dict((t[0].lower(), float(t[1:])) for t in s.split()[1:] if t[0].upper() in "XYZF" and len(t) > 1)
            tgt = [words.get(a, m) for a, m in zip("xyz", self.mpos)]
            self._start(self._move_to(tgt, words.get("f", 1000.0)))
            return None
        if u.startswith("M493.2"):
            t = [tok for tok in s.split() if tok.upper().startswith("T")]
            if t:
                self.tool = int(float(t[0][1:]))
            return "ok"
        if u.startswith("M493.1"):
            return "ok"
        if u in ("M831", "M832"):
            self.switches["probe_laser"] = u == "M831"
            return "ok"
        if u in ("M851", "M852"):
            self.switches["extendout"] = u == "M851"
            return "ok"
        if u in ("M821", "M822"):
            self.switches["light"] = u == "M821"
            return "ok"
        if re.match(r"^M811(\s*S\d+)?$", u.replace(" ", "")) or u == "M812":     # spindle fan: M811 S<0..100> / M812
            m = re.search(r"S(\d+)", u)
            self.switches["spindle_fan"] = u != "M812"
            self.fan_power = int(m.group(1)) if m and u != "M812" else (100 if u != "M812" else 0)
            return "ok"
        return "ok"

    def on_realtime(self, b: bytes) -> None:
        self.realtime.append(b)
        if b == b"!":
            if self.state in ("Run", "Home"):
                self._hold.clear()
                self.state = "Hold"
        elif b == b"~":
            if self.state == "Hold":
                self.state = "Run"
                self._hold.set()
        elif b == b"\x18":
            self.halted = True
            self._hold.set()
            if self._move and not self._move.done():
                self._move.cancel()
            self.state = "Alarm"

    # ---- servers
    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        if self.clients >= self.max_clients:
            writer.write(frame(T_TEXT, b"The maximum number of client connections has been reached. Please close other client first.\n"))
            await writer.drain()
            writer.close()
            return
        self.clients += 1
        buf = bytearray()
        try:
            while True:
                d = await reader.read(4096)
                if not d:
                    break
                buf += d
                for t, p in parse_frames(buf):
                    if t == T_REALTIME:
                        if p == b"?":
                            writer.write(frame(T_STATUS, self.status().encode()))
                        else:
                            self.on_realtime(p)
                    elif t == T_LINE:
                        reply = self.on_line(p.decode(errors="replace"))
                        if reply:
                            writer.write(frame(T_TEXT, (reply + "\n").encode()))
                    elif 0xB0 <= t <= 0xB6:
                        for t2, p2 in self.on_file(t, p):
                            writer.write(frame(t2, p2))
                while self.replies:
                    writer.write(frame(T_TEXT, (self.replies.pop(0) + "\n").encode()))
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self.clients -= 1
            writer.close()

    @staticmethod
    def test_card(k: int) -> bytes:
        from PIL import Image, ImageDraw
        im = Image.new("RGB", (640, 480), (30, 34, 40))
        d = ImageDraw.Draw(im)
        for x in range(0, 640, 40):
            d.line([(x, 0), (x, 480)], fill=(60, 70, 80))
        for y in range(0, 480, 40):
            d.line([(0, y), (640, y)], fill=(60, 70, 80))
        d.ellipse([280 + 60 * math.cos(k / 5), 200 + 60 * math.sin(k / 5), 360 + 60 * math.cos(k / 5), 280 + 60 * math.sin(k / 5)], fill=(230, 120, 40))
        d.text((12, 12), f"Z1 emulator frame {k}", fill=(220, 220, 220))
        b = io.BytesIO()
        im.save(b, "JPEG", quality=70)
        return b.getvalue()

    async def camera(self, ws):
        streaming = False
        k = 0

        async def pump():
            nonlocal k
            while True:
                if streaming:
                    await ws.send(self.test_card(k))
                    k += 1
                await asyncio.sleep(0.1)
        task = asyncio.create_task(pump())
        try:
            async for msg in ws:
                if msg == "start_stream":
                    streaming = True
                elif msg == "stop_stream":
                    streaming = False
        except Exception:  # noqa: BLE001
            pass
        finally:
            task.cancel()

    async def start(self, host="127.0.0.1", port=0, cam_port=0):
        import websockets
        tcp = await asyncio.start_server(self.handle, host, port)
        cam = await websockets.serve(self.camera, host, cam_port)
        self.servers = [tcp, cam]
        self.port = tcp.sockets[0].getsockname()[1]
        self.cam_port = next(iter(cam.sockets)).getsockname()[1]
        return self.port, self.cam_port

    async def stop(self):
        for s in self.servers:
            s.close()
        if self._move and not self._move.done():
            self._move.cancel()


async def _main(a):
    emu = Z1Emu(speed=a.speed)
    port, cam = await emu.start(a.host, a.port, a.cam_port)
    print(f"Z1 emulator: control {a.host}:{port}, camera ws://{a.host}:{cam}/ws_video")
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=2222)
    ap.add_argument("--cam-port", type=int, default=8282)
    ap.add_argument("--speed", type=float, default=1.0)
    asyncio.run(_main(ap.parse_args()))
