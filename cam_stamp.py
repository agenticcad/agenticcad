"""
Faster back ends for the simulator's stamping (cam_sim._stamp_z_run: every sample's tool footprint lowered into the
height map, with the per-sample engagement - what each sample cut against the stock as all earlier samples left it).
The numpy code in cam_sim stays the reference and the fallback; these do the same thing:

  numba   the reference's loop as one compiled pass, float64, the same arithmetic in the same order: bit-identical
          height maps and engagement (A/B on the latch, live and obsidian jobs). Optional dependency (`numba`).
  metal   Apple GPU through `metalcompute` (macOS only): one thread per (sample, footprint cell), 100x the numpy
          stamp on the latch's biggest ops (8.8 M sample-cells in 4 ms). float32, so heights agree with the reference
          to the float32 ulp (measured max 1.2e-7 mm); verdicts unchanged. Only runs without engagement bookkeeping
          (see _run_metal for what was tried for the rest), so on its own it shortens little; it is the fallback where
          numba is missing. The grid and the samples are uploaded per run and released after it: nothing stays on
          the GPU between simulations.

AGENTICCAD_SIM_BACKEND = auto | numpy | numba | metal picks one ("auto": numba if importable, else metal where it
works, else numpy). Anything a back end can't do (or any failure) returns False and the caller runs the numpy code.
"""
from __future__ import annotations

import math
import os
import sys
import time
from typing import Any

import numpy as np

CHUNK_PAIRS = 2_000_000          # the reference processes samples in chunks of this many (sample, cell) pairs; the
                                 # rapid-hit check and the "has material" test read the grid as it was at a chunk start
_state: dict[str, Any] = {"numba": None, "metal": None, "reason": {}}
stats: dict[str, float] = {"numba_runs": 0, "metal_runs": 0, "numpy_runs": 0, "metal_s": 0.0, "numba_s": 0.0, "compile_s": 0.0}


def requested() -> str:
    return (os.environ.get("AGENTICCAD_SIM_BACKEND") or "auto").strip().lower()


def _numba():
    """The compiled kernels, or False when numba is missing / fails to compile."""
    if _state["numba"] is None:
        try:
            if "NUMBA_CACHE_DIR" not in os.environ:          # a packaged app's modules sit in a read-only bundle
                import platformdirs
                os.environ["NUMBA_CACHE_DIR"] = os.path.join(platformdirs.user_cache_dir("AgenticCAD"), "numba")
            import numba  # noqa: F401
            t = time.perf_counter()
            _state["numba"] = _build_numba()
            stats["compile_s"] += time.perf_counter() - t
        except Exception as e:  # noqa: BLE001 - optional dependency
            _state["numba"] = False
            _state["reason"]["numba"] = f"{type(e).__name__}: {e}"
    return _state["numba"]


def _metal():
    if _state["metal"] is None:
        _state["metal"] = False
        if sys.platform == "darwin":
            try:
                import metalcompute as mc
                t = time.perf_counter()
                dev = mc.Device()
                lib = dev.kernel(_METAL_SRC)
                _state["metal"] = {"dev": dev, "stamp": lib.function("stamp"), "name": str(dev)}
                stats["compile_s"] += time.perf_counter() - t
            except Exception as e:  # noqa: BLE001 - no metalcompute, no GPU, a compile error: not available
                _state["reason"]["metal"] = f"{type(e).__name__}: {e}"
    return _state["metal"]


def available() -> dict[str, Any]:
    return {"numba": bool(_numba()), "metal": bool(_metal()), "requested": requested(), "reasons": dict(_state["reason"])}


def backend() -> str:
    """The back end stamping runs use now (resolves "auto")."""
    want = requested()
    if want == "numpy":
        return "numpy"
    if want in ("auto", "numba") and _numba():
        return "numba"
    if want in ("auto", "metal") and _metal():
        return "metal"
    return "numpy"


def stamp_run(zhi, zlo, P, K, I, tool, side, res, x0, y0, rapid_hits, op_name, eng=None) -> bool:
    """Do one run (all rapids or all feeds) with the chosen back end. False = not done (the caller runs the numpy code)."""
    be = backend()
    if be == "numpy" or len(P) == 0:
        return False
    import cam_kernel as cam
    rapid = bool(K[0] == cam.RAPID)
    if np.any((K == cam.RAPID) != rapid):            # a mixed run: the reference handles it, the kernels assume one kind
        return False
    try:
        if be == "numba":
            return _run_numba(zhi, zlo, P, I, tool, side, res, x0, y0, rapid_hits, op_name, eng, rapid)
        return _run_metal(zhi, zlo, P, I, tool, side, res, x0, y0, rapid_hits, op_name, eng, rapid)
    except Exception as e:  # noqa: BLE001 - never let an accelerator break a simulation
        _state["reason"][be] = f"run failed: {type(e).__name__}: {e}"
        _state[be] = False
        return False


def _geometry(tool, res):
    R = float(tool.shape.cut_radius)
    edge = max(0.01, (res / 4) ** 2 / max(R, 1e-3))
    k = int(math.ceil(R / res)) + 1
    cu, zf, zl = tool.shape.knots
    return R, edge, k, np.ascontiguousarray(cu, dtype=float), np.ascontiguousarray(zf, dtype=float), np.ascontiguousarray(zl, dtype=float)


# ------------------------------------------------------------------------------------------------ numba
def _build_numba():
    from numba import njit

    @njit(cache=True, nogil=True)
    def profile(cu, zf, zl, d):
        n = cu.shape[0]
        if n == 1:
            return zf[0] if d <= cu[0] else np.inf
        lo, hi = 0, n
        while lo < hi:                               # first knot with cu >= d (searchsorted side="left")
            mid = (lo + hi) >> 1
            if cu[mid] < d:
                lo = mid + 1
            else:
                hi = mid
        if lo >= n:
            return np.inf
        if lo == 0:
            return zf[0]
        c0 = cu[lo - 1]; c1 = cu[lo]
        f = (d - c0) / (c1 - c0) if c1 > c0 else 0.0
        if f < 0.0:
            f = 0.0
        elif f > 1.0:
            f = 1.0
        return zl[lo - 1] + f * (zf[lo] - zl[lo - 1])

    @njit(cache=True, nogil=True)
    def run(zhi, zlo, P, I, cu, zf, zl, R, edge, side, res, x0, y0, k, rapid, eng, has_eng, chunk, hits):
        """The reference run, sample by sample. hits: (n_chunks, 2) rapid hits per chunk (depth, sample) or depth -1."""
        ny, nx = zhi.shape
        n = P.shape[0]
        rlim = R - 1e-4
        inner_lim = R - edge
        mat0 = np.empty((ny, nx), np.bool_)
        nch = 0
        for c0 in range(0, n, chunk):
            c1 = min(n, c0 + chunk)
            for iy in range(ny):
                for ix in range(nx):
                    mat0[iy, ix] = zhi[iy, ix] > zlo[iy, ix]
            if rapid:
                bestd = 0.0; bests = -1
                for s in range(c0, c1):              # the depth into the stock as the chunk found it (before any of its stamps)
                    px = P[s, 0]; py = P[s, 1]; pz = P[s, 2]
                    bx = int(math.floor((px - x0) / res)); by = int(math.floor((py - y0) / res))
                    for dy in range(-k, k + 1):
                        cy = by + dy
                        if cy < 0 or cy >= ny:
                            continue
                        for dx in range(-k, k + 1):
                            cx = bx + dx
                            if cx < 0 or cx >= nx:
                                continue
                            d = math.hypot(x0 + cx * res - px, y0 + cy * res - py)
                            if not d < rlim:
                                continue
                            h = profile(cu, zf, zl, d)
                            if not math.isfinite(h):
                                continue
                            v = pz + h if side > 0 else pz - h
                            hi = zhi[cy, cx]; lo = zlo[cy, cx]
                            depth = (hi - v) if side > 0 else (v - lo)
                            if hi > lo and depth > 0.05 and depth > bestd:
                                bestd = depth; bests = s
                hits[nch, 0] = bestd if bests >= 0 else -1.0
                hits[nch, 1] = bests
            else:
                hits[nch, 0] = -1.0; hits[nch, 1] = -1
            nch += 1
            for s in range(c0, c1):
                px = P[s, 0]; py = P[s, 1]; pz = P[s, 2]
                bx = int(math.floor((px - x0) / res)); by = int(math.floor((py - y0) / res))
                ap = -np.inf; per = -np.inf; vol = 0.0
                for dy in range(-k, k + 1):
                    cy = by + dy
                    if cy < 0 or cy >= ny:
                        continue
                    for dx in range(-k, k + 1):
                        cx = bx + dx
                        if cx < 0 or cx >= nx:
                            continue
                        d = math.hypot(x0 + cx * res - px, y0 + cy * res - py)
                        if not d < rlim:
                            continue
                        h = profile(cu, zf, zl, d)
                        if not math.isfinite(h):
                            continue
                        if side > 0:
                            v = pz + h
                            if has_eng and not rapid:
                                ceil = zhi[cy, cx]
                                inc = ceil - v if mat0[cy, cx] else -np.inf
                                if inc > per:
                                    per = inc
                                if inc > 1e-6:
                                    if d < inner_lim:
                                        ax = ceil - pz
                                        if ax > ap:
                                            ap = ax
                                    vol += inc * res * res
                            if v < zhi[cy, cx]:
                                zhi[cy, cx] = v
                        else:
                            v = pz - h
                            if has_eng and not rapid:
                                ceil = zlo[cy, cx]
                                inc = v - ceil if mat0[cy, cx] else -np.inf
                                if inc > per:
                                    per = inc
                                if inc > 1e-6:
                                    if d < inner_lim:
                                        ax = pz - ceil
                                        if ax > ap:
                                            ap = ax
                                    vol += inc * res * res
                            if v > zlo[cy, cx]:
                                zlo[cy, cx] = v
                if has_eng and not rapid:
                    if ap > eng[s, 0]:
                        eng[s, 0] = ap
                    eng[s, 1] += vol
                    if per > eng[s, 2]:
                        eng[s, 2] = per

    return {"run": run}


def _run_numba(zhi, zlo, P, I, tool, side, res, x0, y0, rapid_hits, op_name, eng, rapid) -> bool:
    nb = _numba()
    if not nb:
        return False
    R, edge, k, cu, zf, zl = _geometry(tool, res)
    chunk = max(1, int(CHUNK_PAIRS // (2 * k + 1) ** 2))
    nch = (len(P) + chunk - 1) // chunk
    hits = np.empty((nch, 2))
    has_eng = eng is not None
    t = time.perf_counter()
    nb["run"](zhi, zlo, np.ascontiguousarray(P, dtype=float), I, cu, zf, zl, R, edge, int(side), float(res), float(x0), float(y0),
              k, rapid, eng if has_eng else np.zeros((1, 3)), has_eng, chunk, hits)
    stats["numba_s"] += time.perf_counter() - t; stats["numba_runs"] += 1
    if rapid:
        for c in range(nch):
            if hits[c, 0] >= 0 and len(rapid_hits) < 50:
                rapid_hits.append({"op": op_name, "move": int(I[int(hits[c, 1])]) + 1, "depth": round(float(hits[c, 0]), 3)})
    return True


# ------------------------------------------------------------------------------------------------ metal
_METAL_SRC = """
#include <metal_stdlib>
using namespace metal;
// heights live in the grid as float32 encoded so unsigned min/max order them: positive floats get the sign bit set,
// negative ones are inverted
inline uint enc(float v) { uint u = as_type<uint>(v); return (u & 0x80000000u) ? ~u : (u | 0x80000000u); }
inline float dec(uint u) { uint v = (u & 0x80000000u) ? (u & 0x7FFFFFFFu) : ~u; return as_type<float>(v); }
// first knot with cum >= d, then the linear piece between it and the knot before (cam_sim's tool profile lookup)
inline float profile(device const float* cu, device const float* zf, device const float* zl, uint m, float d) {
    if (m == 1) return d <= cu[0] ? zf[0] : INFINITY;
    uint lo = 0, hi = m;
    while (lo < hi) { uint mid = (lo + hi) >> 1; if (cu[mid] < d) lo = mid + 1; else hi = mid; }
    if (lo >= m) return INFINITY;
    if (lo == 0) return zf[0];
    float c0 = cu[lo - 1], c1 = cu[lo];
    float f = c1 > c0 ? clamp((d - c0) / (c1 - c0), 0.0f, 1.0f) : 0.0f;
    return zl[lo - 1] + f * (zf[lo] - zl[lo - 1]);
}
// par: m (knots), nx, ny, k (footprint half width in cells), res, x0, y0, R, nsamples
struct Par { float m, nx, ny, k, res, x0, y0, R, ns; };

// one thread per (sample, footprint cell): the cell goes down to the tool's cutting surface (upper grid: min; the
// caller hands a negated lower grid and negated z for the other side, so this is always a min)
kernel void stamp(device const float* P [[buffer(0)]], device atomic_uint* Z [[buffer(1)]],
                  device const float* cu [[buffer(2)]], device const float* zf [[buffer(3)]], device const float* zl [[buffer(4)]],
                  device const Par* par [[buffer(5)]], uint gid [[thread_position_in_grid]]) {
    uint m = (uint)par->m, nx = (uint)par->nx, ny = (uint)par->ny, k = (uint)par->k;
    float res = par->res, x0 = par->x0, y0 = par->y0, R = par->R;
    uint w = 2 * k + 1, cells = w * w;
    uint s = gid / cells, c = gid % cells;
    if (s >= (uint)par->ns) return;
    float px = P[3 * s], py = P[3 * s + 1], pz = P[3 * s + 2];
    int cx = (int)floor((px - x0) / res) + (int)(c % w) - (int)k, cy = (int)floor((py - y0) / res) + (int)(c / w) - (int)k;
    if (cx < 0 || cy < 0 || cx >= (int)nx || cy >= (int)ny) return;
    float dx = x0 + cx * res - px, dy = y0 + cy * res - py;
    float d = sqrt(dx * dx + dy * dy);
    if (d >= R - 1e-4f) return;
    float h = profile(cu, zf, zl, m, d);
    if (!isfinite(h)) return;
    atomic_fetch_min_explicit(&Z[cy * nx + cx], enc(pz + h), memory_order_relaxed);
}
"""


def _enc(z):
    u = np.ascontiguousarray(z, dtype=np.float32).view(np.uint32)
    return np.where(u & 0x80000000, ~u, u | 0x80000000).astype(np.uint32)


def _dec(u):
    v = np.where(u & 0x80000000, u & 0x7FFFFFFF, ~u).astype(np.uint32)
    return v.view(np.float32).astype(np.float64)


def _par(m, nx, ny, k, res, x0, y0, R, ns):
    return np.array([m, nx, ny, k, res, x0, y0, R, ns], dtype=np.float32)


def _run_metal(zhi, zlo, P, I, tool, side, res, x0, y0, rapid_hits, op_name, eng, rapid) -> bool:
    """Runs that only stamp (silent history ops, probes): one dispatch, the grid up and back. A feed run that also
    needs its engagement is declined: that bookkeeping is a per-cell prefix over the samples in order, and the GPU
    version tried (pieces of 64 samples, each thread scanning back over its piece, two dispatches a piece) was
    dispatch-bound: 29 s against numpy's 20 s on the obsidian sheet's 1883 runs, 20 against 11 on the latch's 88-op
    script. Rapids are declined too (their hit report reads the grid the way the reference's chunks do)."""
    mt = _metal()
    if not mt or rapid or eng is not None:
        return False
    R, edge, k, cu, zf, zl = _geometry(tool, res)
    ny, nx = zhi.shape
    t = time.perf_counter()
    grid = zhi if side > 0 else -zlo                    # the lower side as a min problem on the negated grid
    Pf = np.ascontiguousarray(P, dtype=np.float32).copy()
    if side < 0:
        Pf[:, 2] = -Pf[:, 2]
    dev = mt["dev"]
    cells = (2 * k + 1) ** 2
    Zb = dev.buffer(_enc(grid))
    mt["stamp"](len(P) * cells, Pf.ravel(), Zb, cu.astype(np.float32), zf.astype(np.float32), zl.astype(np.float32),
                _par(len(cu), nx, ny, k, res, x0, y0, R, len(P)))
    out = _dec(np.frombuffer(Zb, dtype=np.uint32).copy()).reshape(ny, nx)
    del Zb                                              # nothing stays on the GPU between runs
    if side > 0:
        np.minimum(zhi, out, out=zhi)
    else:
        np.maximum(zlo, -out, out=zlo)
    stats["metal_s"] += time.perf_counter() - t; stats["metal_runs"] += 1
    return True
