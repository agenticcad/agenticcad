"""PROTOTYPE (not wired into the app). GPU vs numpy for the simulator's stamping (the data-parallel core of cam_sim._stamp_z_run): every sample's tool
footprint lowered into the height map. Workload: an .npz written by tools/gpu_stamp_dump.py (a job's flat ops, real samples).
  python tools/gpu_stamp_dump.py <workspace> <cam.py> dump.npz   (needs the app venv)
  python tools/gpu_stamp_bench.py dump.npz                       (any venv with numpy; torch / metalcompute optional)
Runs whatever is importable: numpy stamp-only (reference), numpy full (with the engagement bookkeeping, if cam_sim is
importable), torch MPS (scatter_reduce amin), metalcompute (custom Metal kernel, atomic min on ordered uints).
Times include uploads/downloads and device sync; the first Metal call includes the kernel compile."""
import sys, time, math
import numpy as np
D = np.load(sys.argv[1], allow_pickle=True)
names = [s.split("|") for s in D["names"]]
res, x0, y0, nx, ny = float(D["res"]), float(D["x0"]), float(D["y0"]), int(D["nx"]), int(D["ny"])
zmax, zmin = float(D["zmax"]), float(D["zmin"])
ops = [(int(i), n, int(k)) for i, n, k in names]
ops_big = sorted(ops, key=lambda t: -t[2])[:6]
print(f"grid {nx}x{ny} res {res:.3f}; {len(ops)} ops, {sum(k for _, _, k in ops)} samples; benchmarking the 6 biggest: {[n for _, n, _ in ops_big]}")

def lookup(zc, cum, d):
    i = np.searchsorted(cum, d, side="left"); out = np.full(d.shape, np.inf); ok = i < len(cum)
    i = np.clip(i, 0, len(cum) - 1); i0 = np.maximum(i - 1, 0); c0, c1 = cum[i0], cum[i]
    f = np.where(c1 > c0, (d - c0) / np.where(c1 > c0, c1 - c0, 1.0), 0.0)
    zz = np.where(i == 0, zc[0], zc[i0] + np.clip(f, 0, 1) * (zc[i] - zc[i0])); out[ok] = zz[ok]; return out

def np_stamp(zhi, P, zc, cum, R):
    k = int(math.ceil(R / res)) + 1
    oy, ox = np.mgrid[-k:k + 1, -k:k + 1]; oy = oy.ravel(); ox = ox.ravel()
    bx = np.floor((P[:, 0] - x0) / res).astype(np.int64); by = np.floor((P[:, 1] - y0) / res).astype(np.int64)
    chunk = max(1, int(2_000_000 // len(ox)))
    for s in range(0, len(P), chunk):
        e = s + chunk
        CX = bx[s:e, None] + ox[None, :]; CY = by[s:e, None] + oy[None, :]
        d = np.hypot(x0 + CX * res - P[s:e, 0:1], y0 + CY * res - P[s:e, 1:2])
        ok = (CX >= 0) & (CX < nx) & (CY >= 0) & (CY < ny) & (d < R - 1e-4)
        prof = lookup(zc, cum, np.where(ok, d, 0.0)); ok &= np.isfinite(prof)
        flat = (CY * nx + CX)[ok]; vals = (np.broadcast_to(P[s:e, 2:3], d.shape) + prof)[ok]
        np.minimum.at(zhi.ravel(), flat, vals)

have = {}
try:
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import cam_sim, cam_kernel as cam
    have["cam_sim"] = True
except Exception as e:  # noqa: BLE001
    print("cam_sim not importable here:", type(e).__name__)
try:
    import torch
    have["torch"] = torch.backends.mps.is_available()
except Exception:  # noqa: BLE001
    pass
try:
    import metalcompute as mc
    have["metal"] = True
except Exception:  # noqa: BLE001
    pass
print("available:", have)

METAL_SRC = """
#include <metal_stdlib>
using namespace metal;
kernel void stamp(device const float* P [[buffer(0)]], device atomic_uint* Z [[buffer(1)]],
                  device const float* cum [[buffer(2)]], device const float* zc [[buffer(3)]],
                  device const float* par [[buffer(4)]], uint gid [[thread_position_in_grid]]) {
    uint m = (uint)par[0], nx = (uint)par[1], ny = (uint)par[2], k = (uint)par[3];
    float res = par[4], x0 = par[5], y0 = par[6], R = par[7];
    uint w = 2 * k + 1, cells = w * w;
    uint s = gid / cells, c = gid % cells;
    if (s >= (uint)par[8]) return;                       // the dispatch is rounded up to whole threadgroups
    float px = P[3 * s], py = P[3 * s + 1], pz = P[3 * s + 2];
    int cx = (int)floor((px - x0) / res) + (int)(c % w) - (int)k;
    int cy = (int)floor((py - y0) / res) + (int)(c / w) - (int)k;
    if (cx < 0 || cy < 0 || cx >= (int)nx || cy >= (int)ny) return;
    float dx = x0 + cx * res - px, dy = y0 + cy * res - py;
    float d = sqrt(dx * dx + dy * dy);
    if (d >= R - 1e-4f) return;
    uint i = 0; while (i < m && cum[i] < d) i++;
    if (i >= m) return;
    uint i0 = i > 0 ? i - 1 : 0;
    float c0 = cum[i0], c1 = cum[i];
    float f = c1 > c0 ? clamp((d - c0) / (c1 - c0), 0.0f, 1.0f) : 0.0f;
    float h = (i == 0) ? zc[0] : zc[i0] + f * (zc[i] - zc[i0]);
    float v = pz + h;
    uint u = as_type<uint>(v);
    u = (u & 0x80000000u) ? ~u : (u | 0x80000000u);
    atomic_fetch_min_explicit(&Z[cy * nx + cx], u, memory_order_relaxed);
}
"""
def enc(z):
    u = z.astype(np.float32).view(np.uint32)
    return np.where(u & 0x80000000, ~u, u | 0x80000000).astype(np.uint32)
def dec(u):
    v = np.where(u & 0x80000000, u & 0x7FFFFFFF, ~u).astype(np.uint32)
    return v.view(np.float32).astype(np.float64)

dev = fn = None
def metal_stamp(zhi, P, zc, cum, R):
    global dev, fn
    if dev is None:
        dev = mc.Device(); fn = dev.kernel(METAL_SRC).function("stamp")
    k = int(math.ceil(R / res)) + 1
    Zb = dev.buffer(enc(zhi).tobytes())           # the grid, ordered-uint encoded
    par = np.array([len(cum), nx, ny, k, res, x0, y0, R, len(P)], dtype=np.float32)
    n = len(P) * (2 * k + 1) ** 2
    fn(n, P.astype(np.float32).ravel(), Zb, cum.astype(np.float32), zc.astype(np.float32), par)
    out = dec(np.frombuffer(Zb, dtype=np.uint32).copy()).reshape(ny, nx)
    return out

def torch_stamp(zhi, P, zc, cum, R):
    dv = torch.device("mps")
    k = int(math.ceil(R / res)) + 1
    oy, ox = torch.meshgrid(torch.arange(-k, k + 1, device=dv), torch.arange(-k, k + 1, device=dv), indexing="ij")
    oy = oy.reshape(-1); ox = ox.reshape(-1)
    Pt = torch.tensor(P, dtype=torch.float32, device=dv); Z = torch.tensor(zhi, dtype=torch.float32, device=dv).reshape(-1)
    cumt = torch.tensor(cum, dtype=torch.float32, device=dv); zct = torch.tensor(zc, dtype=torch.float32, device=dv)
    bx = torch.floor((Pt[:, 0] - x0) / res).long(); by = torch.floor((Pt[:, 1] - y0) / res).long()
    chunk = max(1, int(2_000_000 // len(ox)))
    for s in range(0, len(P), chunk):
        e = s + chunk
        CX = bx[s:e, None] + ox[None, :]; CY = by[s:e, None] + oy[None, :]
        d = torch.hypot(x0 + CX * res - Pt[s:e, 0:1], y0 + CY * res - Pt[s:e, 1:2])
        ok = (CX >= 0) & (CX < nx) & (CY >= 0) & (CY < ny) & (d < R - 1e-4)
        i = torch.searchsorted(cumt, d.reshape(-1), right=False).reshape(d.shape)
        inside = i < len(cumt); i = i.clamp(0, len(cumt) - 1); i0 = (i - 1).clamp(min=0)
        c0, c1 = cumt[i0], cumt[i]
        f = torch.where(c1 > c0, (d - c0) / torch.where(c1 > c0, c1 - c0, torch.ones_like(c1)), torch.zeros_like(d)).clamp(0, 1)
        h = torch.where(i == 0, zct[0], zct[i0] + f * (zct[i] - zct[i0]))
        ok &= inside
        flat = (CY * nx + CX)[ok]; vals = (Pt[s:e, 2:3] + h)[ok]
        Z.scatter_reduce_(0, flat, vals, reduce="amin")
    torch.mps.synchronize()
    return Z.reshape(ny, nx).cpu().numpy().astype(np.float64)

def fresh():
    z = np.full((ny, nx), -np.inf); z[:] = zmax; return z

tot = {}
for i, name, n in ops_big:
    P, K, zc, cum, R = D[f"op{i}_P"], D[f"op{i}_K"], D[f"op{i}_zc"], D[f"op{i}_cumc"], float(D[f"op{i}_R"])
    if int(D[f"op{i}_side"]) != 1:
        continue
    k = int(math.ceil(R / res)) + 1
    print(f"\n{name}: {n} samples, footprint {(2 * k + 1) ** 2} cells -> {n * (2 * k + 1) ** 2 / 1e6:.1f} M sample-cells")
    z_ref = fresh(); t = time.perf_counter(); np_stamp(z_ref, P, zc, cum, R); t_np = time.perf_counter() - t
    print(f"  numpy stamp-only          {t_np * 1000:7.0f} ms"); tot["numpy stamp"] = tot.get("numpy stamp", 0) + t_np
    if have.get("cam_sim"):
        class T:  # the tool as cam_sim's stamp sees it
            class shape:
                cut_radius = R
                @staticmethod
                def cut_height(d):
                    return lookup(zc, cum, np.asarray(d, dtype=float))
        zhi = fresh(); zlo = np.full((ny, nx), zmin); eng = np.full((len(P), 3), -np.inf); eng[:, 1] = 0.0
        I = D[f"op{i}_I"]
        t = time.perf_counter(); cam_sim._stamp_z_run(zhi, zlo, P, K, I, T(), 1, res, x0, y0, [], name, eng); t_full = time.perf_counter() - t
        print(f"  numpy full (+engagement)  {t_full * 1000:7.0f} ms   (same map as stamp-only: {np.allclose(zhi, z_ref)})")
        tot["numpy full"] = tot.get("numpy full", 0) + t_full
    if have.get("torch"):
        for rep in range(2):
            t = time.perf_counter(); z_t = torch_stamp(fresh(), P, zc, cum, R); t_t = time.perf_counter() - t
            print(f"  torch MPS {'(first call)' if rep == 0 else '            '}  {t_t * 1000:7.0f} ms   max|diff| {np.abs(z_t - z_ref).max():.2e}")
        tot["torch"] = tot.get("torch", 0) + t_t
    if have.get("metal"):
        for rep in range(2):
            t = time.perf_counter(); z_m = metal_stamp(fresh(), P, zc, cum, R); t_m = time.perf_counter() - t
            print(f"  Metal kernel {'(first call)' if rep == 0 else '            '} {t_m * 1000:7.0f} ms   max|diff| {np.abs(z_m - z_ref).max():.2e}")
        tot["metal"] = tot.get("metal", 0) + t_m
print("\nTOTAL over the 6 ops:", {k: f"{v:.2f} s" for k, v in tot.items()})
