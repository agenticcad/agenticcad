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
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from machine_link import T_LINE, T_REALTIME, T_STATUS, T_TEXT, frame, parse_frames  # noqa: E402


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
        self.switches = {"extendout": False, "light": True, "probe_laser": False}
        self.lines: list[str] = []                      # every console line received
        self.realtime: list[bytes] = []
        self.crashes: list[str] = []
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
                f"|S:0.0,10000.0,100.0,0,27.2,27.4,0,0,0,0|T:6,-17.297,-1|L:0, 0, 0, 0.0,100.0"
                + ("|H:1" if self.halted else "") + "|C:3,1,0,1|E:0,0,0,1,7613|OTA:0,0>")

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

    # ---- commands
    def on_line(self, line: str) -> str | None:
        s = line.strip()
        self.lines.append(s)
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
        if u.startswith("G53"):
            words = dict((t[0].lower(), float(t[1:])) for t in s.split()[1:] if t[0].upper() in "XYZF" and len(t) > 1)
            tgt = [words.get(a, m) for a, m in zip("xyz", self.mpos)]
            self._start(self._move_to(tgt, words.get("f", 1000.0)))
            return None
        if u in ("M831", "M832"):
            self.switches["probe_laser"] = u == "M831"
            return "ok"
        if u in ("M851", "M852"):
            self.switches["extendout"] = u == "M851"
            return "ok"
        if u in ("M821", "M822"):
            self.switches["light"] = u == "M821"
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
