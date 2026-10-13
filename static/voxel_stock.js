// AgenticCAD voxel stock — a Web Worker that owns a signed-distance voxel field of the stock during simulation
// playback and meshes it with chunked surface nets. The viewer sends the server's height-map keyframe to reset the
// field and then stamps every tool move into it (a swept vertical cylinder with the tool's end profile), so walls get
// sub-voxel positions from the lateral distance and the mesh is a smooth closed surface instead of stepped columns.
//
// Messages in:  {type:'reset', gen, h, grid, zhi, zlo, gouge, left, final, colors}
//               {type:'stamp', gen, segs:[{ax,ay,az,bx,by,bz,side,r,type,angle}, ...]}
//               {type:'color', gen, colors}
// Messages out: {type:'mesh', gen, chunks:[{id, pos, nrm, col, idx}, ...]}   (empty idx = chunk has no surface)
'use strict';
const CS = 24;              // chunk size in cells
const SCALE = 127;          // field value = clamp(distance / h, -1, 1) * SCALE; > 0 means material
let G = null;

self.onmessage = (e) => {
  const d = e.data;
  if (d.type === 'reset') { if (d.edgeDeg !== undefined) EDGE_COS = Math.cos(d.edgeDeg * Math.PI / 180); reset(d); }
  else if (d.type === 'stamp') { if (!G || d.gen !== G.gen) return; for (const s of d.segs) stamp(s); flush(); }
  else if (d.type === 'color') {                       // recolour (and, with gouge/left maps, mark the final frame's tints)
    if (!G || d.gen !== G.gen) return; G.colors = d.colors; if (d.final !== undefined) { G.final = !!d.final; G.gouge = d.gouge || null; G.left = d.left || null; }
    if (d.edgeDeg !== undefined) EDGE_COS = Math.cos(d.edgeDeg * Math.PI / 180);
    for (let c = 0; c < G.ncx * G.ncy * G.ncz; c++) G.dirty.add(c); flush();
  }
};

function reset(d) {
  const t0 = Date.now(), g = d.grid, h = d.h, zhi = d.zhi, zlo = d.zlo;
  // crop the field to the stock's footprint (material never grows back, so later keyframes fit inside it)
  let fx0 = Infinity, fx1 = -Infinity, fy0 = Infinity, fy1 = -Infinity;
  for (let n = 0; n < g.nx * g.ny; n++) if (isFinite(zhi[n])) { const jx = n % g.nx, jy = Math.floor(n / g.nx); if (jx < fx0) fx0 = jx; if (jx > fx1) fx1 = jx; if (jy < fy0) fy0 = jy; if (jy > fy1) fy1 = jy; }
  if (!isFinite(fx0)) { fx0 = fy0 = 0; fx1 = fy1 = 1; }
  const sx = (fx1 - fx0) * g.res, sy = (fy1 - fy0) * g.res, sz = g.zmax - g.zmin;
  const nx = Math.ceil(sx / h) + 4, ny = Math.ceil(sy / h) + 4, nz = Math.ceil(sz / h) + 3;
  const x0 = g.x0 + fx0 * g.res - h * 1.5, y0 = g.y0 + fy0 * g.res - h * 1.5, z0 = g.zmin - h;
  const m = new Int8Array(nx * ny * nz).fill(-SCALE);
  const nrm = new Int8Array(nx * ny * nz * 3);          // Hermite data: the outward normal of the surface nearest each voxel
  if (d.box) {                                           // the untouched stock is a box: exact field, flat faces, sharp edges
    const B = d.box;
    for (let iz = 0; iz < nz; iz++) { const z = z0 + iz * h;
      for (let iy = 0; iy < ny; iy++) { const y = y0 + iy * h;
        for (let ix = 0; ix < nx; ix++) { const x = x0 + ix * h;
          const ds = [B.xmax - x, x - B.xmin, B.ymax - y, y - B.ymin, B.zmax - z, z - B.zmin];
          let v = ds[0], w = 0; for (let q = 1; q < 6; q++) if (ds[q] < v) { v = ds[q]; w = q; }
          const i = (iz * ny + iy) * nx + ix; v /= h;
          m[i] = v >= 1 ? SCALE : v <= -1 ? -SCALE : Math.round(v * SCALE);
          nrm[i * 3 + (w >> 1)] = (w & 1) ? -127 : 127; } } }
  } else {
  // column SDF from the height map: for every nearby cell level (top, bottom) the material is bounded above and below
  // and, laterally, by the distance to the nearest cell whose top is lower (or bottom higher) — so flat areas are exact
  // and walls the keyframe already holds land between voxels at the server's resolution instead of on them
  const reach = h + g.res * 0.5, kr = Math.ceil(reach / g.res);
  const tops = new Float64Array(128), bots = new Float64Array(128), caps = new Float64Array(128), capx = new Float64Array(128), capy = new Float64Array(128);
  const wx = new Int32Array(256), wy = new Int32Array(256), wt = new Float64Array(256), wb = new Float64Array(256), wd = new Float64Array(256);
  for (let iy = 0; iy < ny; iy++) {
    const y = y0 + iy * h, cy = Math.round((y - g.y0) / g.res);
    for (let ix = 0; ix < nx; ix++) {
      const x = x0 + ix * h, cx = Math.round((x - g.x0) / g.res);
      // window of server cells around the column (empty cells count as top -inf / bottom +inf)
      let nw = 0, any = false;
      for (let oy = -kr; oy <= kr; oy++) for (let ox = -kr; ox <= kr; ox++) {
        const jx = cx + ox, jy = cy + oy; if (nw >= 256) break;
        const inside = jx >= 0 && jx < g.nx && jy >= 0 && jy < g.ny, n = jy * g.nx + jx;
        let top = -Infinity, bot = Infinity;
        if (inside && isFinite(zhi[n])) { top = zhi[n]; bot = zlo ? zlo[n] : g.zmin; if (!(top > bot)) { top = -Infinity; bot = Infinity; } }
        wx[nw] = jx; wy[nw] = jy; wt[nw] = top; wb[nw] = bot;
        wd[nw] = Math.hypot(g.x0 + jx * g.res - x, g.y0 + jy * g.res - y) - g.res * 0.75;   // distance to the cell as a disc (overlapping discs: smooth walls, no square staircase)
        if (top > bot) any = true; nw++;
      }
      if (!any) continue;
      let nc = 0;
      for (let w = 0; w < nw; w++) {
        if (!(wt[w] > wb[w]) || wd[w] > h) continue;                          // only cells that can reach this column
        let dup = false; for (let c = 0; c < nc; c++) if (tops[c] === wt[w] && bots[c] === wb[w]) { dup = true; break; }
        if (dup || nc >= 128) continue;
        let cap = h, cx_ = 0, cy_ = 0;                                        // lateral bound: nearest lower top / higher bottom
        for (let v = 0; v < nw; v++) if (wt[v] < wt[w] - 1e-6 || wb[v] > wb[w] + 1e-6) { if (wd[v] < cap) { cap = wd[v]; cx_ = g.x0 + wx[v] * g.res - x; cy_ = g.y0 + wy[v] * g.res - y; } }
        if (cap <= -h) continue;
        const cl = Math.hypot(cx_, cy_) || 1;
        tops[nc] = wt[w]; bots[nc] = wb[w]; caps[nc] = cap; capx[nc] = cx_ / cl; capy[nc] = cy_ / cl; nc++;
      }
      if (!nc) continue;
      const base = iy * nx + ix;
      for (let iz = 0; iz < nz; iz++) {
        const z = z0 + iz * h; let v = -Infinity, kind = 0, bc = 0;
        for (let c = 0; c < nc; c++) { const a = tops[c] - z, b = z - bots[c]; let w = a < b ? a : b, k = a < b ? 0 : 1; if (caps[c] < w) { w = caps[c]; k = 2; } if (w > v) { v = w; kind = k; bc = c; } }
        const i = iz * nx * ny + base; v /= h; m[i] = v >= 1 ? SCALE : v <= -1 ? -SCALE : Math.round(v * SCALE);
        if (kind === 0) nrm[i * 3 + 2] = 127; else if (kind === 1) nrm[i * 3 + 2] = -127; else { nrm[i * 3] = Math.round(capx[bc] * 127); nrm[i * 3 + 1] = Math.round(capy[bc] * 127); }
      }
    }
  }
  }
  const ncx = Math.ceil((nx - 1) / CS), ncy = Math.ceil((ny - 1) / CS), ncz = Math.ceil((nz - 1) / CS);
  G = { gen: d.gen, h, x0, y0, z0, nx, ny, nz, m, nrm, ncx, ncy, ncz, dirty: new Set(), srv: g, gouge: d.gouge || null, left: d.left || null, final: !!d.final, colors: d.colors };
  for (let c = 0; c < ncx * ncy * ncz; c++) G.dirty.add(c);
  G.resetMs = Date.now() - t0;
  if (!d.quiet) flush();                                 // quiet: stamps follow at once, mesh after them (no flash of the uncut stock)
}

function profile(type, r, angle, d) {                 // height of the cutting surface above the tip at radius d
  if (type === 'ball') return r - Math.sqrt(Math.max(r * r - d * d, 0));
  if ((type === 'vbit' || type === 'chamfer' || type === 'drill') && angle) return d / Math.tan(angle / 2 * Math.PI / 180);
  return 0;
}

function markDirty(ix, iy, izA, izB) {               // voxel column (ix, iy) changed between izA..izB: cells ix-1..ix etc.
  const cxA = Math.max(0, Math.floor((ix - 1) / CS)), cxB = Math.min(G.ncx - 1, Math.floor(ix / CS));
  const cyA = Math.max(0, Math.floor((iy - 1) / CS)), cyB = Math.min(G.ncy - 1, Math.floor(iy / CS));
  const czA = Math.max(0, Math.floor((izA - 1) / CS)), czB = Math.min(G.ncz - 1, Math.floor(izB / CS));
  for (let cz = czA; cz <= czB; cz++) for (let cy = cyA; cy <= cyB; cy++) for (let cx = cxA; cx <= cxB; cx++) G.dirty.add((cz * G.ncy + cy) * G.ncx + cx);
}

function slope(type, r, angle, d) {                  // d(profile)/d(d): how the cutting surface rises with radius
  if (type === 'ball') return d / Math.sqrt(Math.max(r * r - d * d, 1e-6));
  if ((type === 'vbit' || type === 'chamfer' || type === 'drill') && angle) return 1 / Math.tan(angle / 2 * Math.PI / 180);
  return 0;
}

function stamp(s) {
  const { h, x0, y0, z0, nx, ny, nz, m, nrm } = G, r = s.r, side = s.side;
  const ax = s.ax, ay = s.ay, az = s.az, bx = s.bx, by = s.by, bz = s.bz;
  const dx = bx - ax, dy = by - ay, dz = bz - az, L2 = dx * dx + dy * dy;
  const ix0 = Math.max(0, Math.floor((Math.min(ax, bx) - r - h - x0) / h)), ix1 = Math.min(nx - 1, Math.ceil((Math.max(ax, bx) + r + h - x0) / h));
  const iy0 = Math.max(0, Math.floor((Math.min(ay, by) - r - h - y0) / h)), iy1 = Math.min(ny - 1, Math.ceil((Math.max(ay, by) + r + h - y0) / h));
  const NXY = nx * ny;
  for (let iy = iy0; iy <= iy1; iy++) {
    const py = y0 + iy * h;
    for (let ix = ix0; ix <= ix1; ix++) {
      const px = x0 + ix * h;
      let t = L2 > 1e-12 ? ((px - ax) * dx + (py - ay) * dy) / L2 : 0; t = t < 0 ? 0 : t > 1 ? 1 : t;
      const d = Math.hypot(ax + dx * t - px, ay + dy * t - py), dmin = d - r;
      if (dmin >= h) continue;
      let zc;
      if (dmin < 0 && L2 > 1e-12) {                   // t-interval where the disc covers this column: lowest z on it
        const ex = ax - px, ey = ay - py, b = ex * dx + ey * dy, c = ex * ex + ey * ey - r * r;
        const sq = Math.sqrt(Math.max(b * b - L2 * c, 0));
        let t0 = (-b - sq) / L2, t1 = (-b + sq) / L2; if (t0 < 0) t0 = 0; if (t1 > 1) t1 = 1;
        zc = side > 0 ? Math.min(az + dz * t0, az + dz * t1) : Math.max(az + dz * t0, az + dz * t1);
      } else if (dmin < 0) zc = side > 0 ? Math.min(az, bz) : Math.max(az, bz);
      else zc = az + dz * t;
      const pr = profile(s.type, r, s.angle, Math.min(d, r)); zc += side > 0 ? pr : -pr;
      // outward normals of the two candidate surfaces: the tool's side (toward the axis) and its end (sloped by the profile)
      const ux = d > 1e-9 ? (ax + dx * t - px) / d : 0, uy = d > 1e-9 ? (ay + dy * t - py) / d : 0;   // toward the axis
      const kslope = slope(s.type, r, s.angle, Math.min(d, r)), fl = Math.hypot(kslope, 1);
      const fx = Math.round(ux * kslope / fl * 127), fy = Math.round(uy * kslope / fl * 127), fz = Math.round(side / fl * 127);   // end surface
      const lx = Math.round(ux * 127), ly = Math.round(uy * 127);                                                              // side surface
      const base = iy * nx + ix;
      let first = -1, last = -1;
      if (side > 0) {
        for (let iz = Math.max(0, Math.floor((zc - h - z0) / h)); iz < nz; iz++) {
          const z = z0 + iz * h; let rr = zc - z, lat = false; if (dmin > rr) { rr = dmin; lat = true; }
          const q = rr >= h ? SCALE : rr <= -h ? -SCALE : Math.round(rr / h * SCALE), i = iz * NXY + base;
          if (q < m[i]) { m[i] = q; if (first < 0) first = iz; last = iz; if (lat) { nrm[i * 3] = lx; nrm[i * 3 + 1] = ly; nrm[i * 3 + 2] = 0; } else { nrm[i * 3] = fx; nrm[i * 3 + 1] = fy; nrm[i * 3 + 2] = fz; } }
        }
      } else {
        for (let iz = Math.min(nz - 1, Math.ceil((zc + h - z0) / h)); iz >= 0; iz--) {
          const z = z0 + iz * h; let rr = z - zc, lat = false; if (dmin > rr) { rr = dmin; lat = true; }
          const q = rr >= h ? SCALE : rr <= -h ? -SCALE : Math.round(rr / h * SCALE), i = iz * NXY + base;
          if (q < m[i]) { m[i] = q; if (last < 0) last = iz; first = iz; if (lat) { nrm[i * 3] = lx; nrm[i * 3 + 1] = ly; nrm[i * 3 + 2] = 0; } else { nrm[i * 3] = fx; nrm[i * 3 + 1] = fy; nrm[i * 3 + 2] = fz; } }
        }
      }
      if (first >= 0) markDirty(ix, iy, first, last);
    }
  }
}

function flush() {
  if (!G.dirty.size) return;
  const t0 = Date.now(), chunks = [], transfer = [];
  for (const id of G.dirty) {
    const cx = id % G.ncx, cy = Math.floor(id / G.ncx) % G.ncy, cz = Math.floor(id / (G.ncx * G.ncy));
    const r = meshChunk(cx, cy, cz);
    chunks.push({ id, pos: r.pos, nrm: r.nrm, col: r.col, uv: r.uv, tan: r.tan, idx: r.idx }); transfer.push(r.pos.buffer, r.nrm.buffer, r.col.buffer, r.uv.buffer, r.tan.buffer, r.idx.buffer);
  }
  G.dirty.clear();
  self.postMessage({ type: 'mesh', gen: G.gen, chunks, ms: Date.now() - t0, resetMs: G.resetMs || 0, voxels: G.nx * G.ny * G.nz }, transfer); G.resetMs = 0;
}

function vertexColor(x, y, out, o) {                   // gouge / material-left tints on the final frame, else the base
  const c = G.colors; let rgb = c.base;
  if (G.final && (G.gouge || G.left)) {
    const g = G.srv, sx = Math.round((x - g.x0) / g.res), sy = Math.round((y - g.y0) / g.res);
    if (sx >= 0 && sx < g.nx && sy >= 0 && sy < g.ny) {
      const n = sy * g.nx + sx;
      if (G.gouge && (G.gouge[n >> 3] >> (7 - (n & 7))) & 1) rgb = c.red; else if (G.left && (G.left[n >> 3] >> (7 - (n & 7))) & 1) rgb = c.amber;
    }
  }
  out[o] = rgb[0]; out[o + 1] = rgb[1]; out[o + 2] = rgb[2];
}

// surface nets over the cells of one chunk (plus a one-cell apron on the low side so quads at the border find their
// neighbours); a vertex per cell with a sign change at the mean of its edge crossings, a quad per sign-changing edge
function meshChunk(cx, cy, cz) {
  const { h, x0, y0, z0, nx, ny, nz, m, nrm: vn } = G, NXY = nx * ny;
  const cA = [cx * CS - 1, cy * CS - 1, cz * CS - 1], cB = [Math.min(nx - 2, cx * CS + CS - 1), Math.min(ny - 2, cy * CS + CS - 1), Math.min(nz - 2, cz * CS + CS - 1)];
  for (let a = 0; a < 3; a++) if (cA[a] < 0) cA[a] = 0;
  const W = CS + 1, vmap = new Int32Array(W * W * W).fill(-1);
  const pos = [], nrm = [], col = [], idx = [], vpl = [], vpc = [];   // vpl: up to 4 plane normals per vertex, vpc: how many
  const V = (i, j, k) => m[k * NXY + j * nx + i];
  const off = [[0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 1, 0], [0, 0, 1], [1, 0, 1], [0, 1, 1], [1, 1, 1]];
  const edges = [[0, 1], [2, 3], [4, 5], [6, 7], [0, 2], [1, 3], [4, 6], [5, 7], [0, 4], [1, 5], [2, 6], [3, 7]];
  const cv = new Float64Array(8);
  for (let k = cA[2]; k <= cB[2]; k++) for (let j = cA[1]; j <= cB[1]; j++) for (let i = cA[0]; i <= cB[0]; i++) {
    let pos8 = 0, neg8 = 0;
    for (let c = 0; c < 8; c++) { const v = V(i + off[c][0], j + off[c][1], k + off[c][2]); cv[c] = v; if (v > 0) pos8++; else neg8++; }
    if (!pos8 || !neg8) continue;
    let sx = 0, sy = 0, sz = 0, ne = 0, npl = 0;
    const pl = PL; pl.fill(0);                                                        // plane clusters: x y z weight ×4
    let axx = 0, axy = 0, axz = 0, ayy = 0, ayz = 0, azz = 0, bx = 0, by = 0, bz = 0;   // QEF normal equations
    for (const [a, b] of edges) {
      const va = cv[a], vb = cv[b]; if ((va > 0) === (vb > 0)) continue;
      const t = va / (va - vb), oa = off[a], ob = off[b];
      const px = oa[0] + (ob[0] - oa[0]) * t, py = oa[1] + (ob[1] - oa[1]) * t, pz = oa[2] + (ob[2] - oa[2]) * t;
      sx += px; sy += py; sz += pz; ne++;
      // the plane the surface follows at this crossing: the stored normal of the material-side voxel (exact Hermite data
      // from the stamp), else the field gradient (trilinear) as a fallback
      // both end voxels offer the normal of their nearest primitive; the one whose plane passes through this crossing
      // (its linearised field is ~0 here) is the surface the crossing lies on — at an edge the two differ
      let gx = 0, gy = 0, gz = 0, bestF = Infinity;
      for (const ci of [a, b]) {
        const co = off[ci], ni = ((k + co[2]) * NXY + (j + co[1]) * nx + (i + co[0])) * 3;
        const nx_ = vn[ni] / 127, ny_ = vn[ni + 1] / 127, nz_ = vn[ni + 2] / 127;
        if (nx_ === 0 && ny_ === 0 && nz_ === 0) continue;
        const f = Math.abs(cv[ci] / SCALE - (nx_ * (px - co[0]) + ny_ * (py - co[1]) + nz_ * (pz - co[2])));   // linearised field at p: m(c) − n·(p − c), the field grows inward
        if (f < bestF) { bestF = f; gx = -nx_; gy = -ny_; gz = -nz_; }
      }
      if (gx === 0 && gy === 0 && gz === 0) {
        const u = px, v = py, w = pz;
        gx = (1 - v) * (1 - w) * (cv[1] - cv[0]) + v * (1 - w) * (cv[3] - cv[2]) + (1 - v) * w * (cv[5] - cv[4]) + v * w * (cv[7] - cv[6]);
        gy = (1 - u) * (1 - w) * (cv[2] - cv[0]) + u * (1 - w) * (cv[3] - cv[1]) + (1 - u) * w * (cv[6] - cv[4]) + u * w * (cv[7] - cv[5]);
        gz = (1 - u) * (1 - v) * (cv[4] - cv[0]) + u * (1 - v) * (cv[5] - cv[1]) + (1 - u) * v * (cv[6] - cv[2]) + u * v * (cv[7] - cv[3]);
      }
      const gl = Math.hypot(gx, gy, gz); if (gl < 1e-9) continue; gx /= gl; gy /= gl; gz /= gl;
      const d = gx * px + gy * py + gz * pz;
      axx += gx * gx; axy += gx * gy; axz += gx * gz; ayy += gy * gy; ayz += gy * gz; azz += gz * gz; bx += gx * d; by += gy * d; bz += gz * d;
      // outward normal (−gradient) into a plane cluster
      let put = -1;
      for (let q = 0; q < npl; q++) { const w = pl[q * 4 + 3]; if ((-gx * pl[q * 4] - gy * pl[q * 4 + 1] - gz * pl[q * 4 + 2]) / w >= EDGE_COS * 0.98) { put = q; break; } }
      if (put < 0 && npl < 4) { put = npl++; }
      if (put >= 0) { pl[put * 4] -= gx; pl[put * 4 + 1] -= gy; pl[put * 4 + 2] -= gz; pl[put * 4 + 3] += 1; }
    }
    const mx = sx / ne, my = sy / ne, mz = sz / ne;
    // solve (AᵀA + λI) x = Aᵀb + λ m  (3×3, Cramer's rule); λ keeps flat areas at the mass point
    const lam = 0.02;
    const a11 = axx + lam, a12 = axy, a13 = axz, a22 = ayy + lam, a23 = ayz, a33 = azz + lam;
    const r1 = bx + lam * mx, r2 = by + lam * my, r3 = bz + lam * mz;
    const det = a11 * (a22 * a33 - a23 * a23) - a12 * (a12 * a33 - a23 * a13) + a13 * (a12 * a23 - a22 * a13);
    let qx = mx, qy = my, qz = mz;
    if (Math.abs(det) > 1e-12) {
      qx = (r1 * (a22 * a33 - a23 * a23) - a12 * (r2 * a33 - a23 * r3) + a13 * (r2 * a23 - a22 * r3)) / det;
      qy = (a11 * (r2 * a33 - a23 * r3) - r1 * (a12 * a33 - a23 * a13) + a13 * (a12 * r3 - r2 * a13)) / det;
      qz = (a11 * (a22 * r3 - r2 * a23) - a12 * (a12 * r3 - r2 * a13) + r1 * (a12 * a23 - a22 * a13)) / det;
      if (!(qx >= -0.5 && qx <= 1.5 && qy >= -0.5 && qy <= 1.5 && qz >= -0.5 && qz <= 1.5)) { qx = mx; qy = my; qz = mz; }   // far outside the cell: fall back
      else { qx = Math.min(1, Math.max(0, qx)); qy = Math.min(1, Math.max(0, qy)); qz = Math.min(1, Math.max(0, qz)); }   // keep it in the cell
    }
    const vx = x0 + (i + qx) * h, vy = y0 + (j + qy) * h, vz = z0 + (k + qz) * h;
    // outward normal = -gradient (the field grows into the material)
    let gx = (cv[1] + cv[3] + cv[5] + cv[7]) - (cv[0] + cv[2] + cv[4] + cv[6]);
    let gy = (cv[2] + cv[3] + cv[6] + cv[7]) - (cv[0] + cv[1] + cv[4] + cv[5]);
    let gz = (cv[4] + cv[5] + cv[6] + cv[7]) - (cv[0] + cv[1] + cv[2] + cv[3]);
    const gl = Math.hypot(gx, gy, gz) || 1; gx /= -gl; gy /= -gl; gz /= -gl;
    vmap[((k - cA[2]) * W + (j - cA[1])) * W + (i - cA[0])] = pos.length / 3;
    pos.push(vx, vy, vz); nrm.push(gx, gy, gz); const o = col.length; col.length += 3; vertexColor(vx, vy, col, o);
    vpc.push(npl);
    for (let q = 0; q < 4; q++) { if (q < npl) { const l = Math.hypot(pl[q * 4], pl[q * 4 + 1], pl[q * 4 + 2]) || 1; vpl.push(pl[q * 4] / l, pl[q * 4 + 1] / l, pl[q * 4 + 2] / l); } else vpl.push(0, 0, 0); }
  }
  if (!pos.length) return { pos: new Float32Array(0), nrm: new Float32Array(0), col: new Float32Array(0), uv: new Float32Array(0), tan: new Float32Array(0), idx: new Uint32Array(0) };
  const vat = (i, j, k) => (i < cA[0] || j < cA[1] || k < cA[2] || i > cB[0] || j > cB[1] || k > cB[2]) ? -1 : vmap[((k - cA[2]) * W + (j - cA[1])) * W + (i - cA[0])];
  const quad = (a, b, c, d, flip) => { if (a < 0 || b < 0 || c < 0 || d < 0) return; if (flip) idx.push(a, d, c, a, c, b); else idx.push(a, b, c, a, c, d); };
  const pA = [cx * CS, cy * CS, cz * CS], pB = [Math.min(nx - 2, cx * CS + CS - 1), Math.min(ny - 2, cy * CS + CS - 1), Math.min(nz - 2, cz * CS + CS - 1)];
  for (let k = pA[2]; k <= pB[2]; k++) for (let j = pA[1]; j <= pB[1]; j++) for (let i = pA[0]; i <= pB[0]; i++) {
    const v0 = V(i, j, k) > 0;
    if ((V(i + 1, j, k) > 0) !== v0) quad(vat(i, j - 1, k - 1), vat(i, j, k - 1), vat(i, j, k), vat(i, j - 1, k), !v0);
    if ((V(i, j + 1, k) > 0) !== v0) quad(vat(i - 1, j, k - 1), vat(i - 1, j, k), vat(i, j, k), vat(i, j, k - 1), !v0);
    if ((V(i, j, k + 1) > 0) !== v0) quad(vat(i - 1, j - 1, k), vat(i, j - 1, k), vat(i, j, k), vat(i - 1, j, k), !v0);
  }
  return hardEdges(pos, col, idx, vpl, vpc);
}

// per-vertex normals from the incident faces, split where faces disagree by more than 35° (a milled edge stays sharp,
// a curved wall stays smooth); uv = planar projection on the vertex's dominant axis, in cm (for brushed-finish maps)
let EDGE_COS = Math.cos(35 * Math.PI / 180);
const PL = new Float64Array(16);
function hardEdges(pos, col, idx, vpl, vpc) {
  const nv = pos.length / 3, nf = idx.length / 3, fn = new Float32Array(nf * 3);
  for (let f = 0; f < nf; f++) {
    const a = idx[f * 3] * 3, b = idx[f * 3 + 1] * 3, c = idx[f * 3 + 2] * 3;
    const ux = pos[b] - pos[a], uy = pos[b + 1] - pos[a + 1], uz = pos[b + 2] - pos[a + 2], vx = pos[c] - pos[a], vy = pos[c + 1] - pos[a + 1], vz = pos[c + 2] - pos[a + 2];
    const x = uy * vz - uz * vy, y = uz * vx - ux * vz, z = ux * vy - uy * vx, l = Math.hypot(x, y, z) || 1;
    fn[f * 3] = x / l; fn[f * 3 + 1] = y / l; fn[f * 3 + 2] = z / l;
  }
  const cnt = new Int32Array(nv + 1); for (let i = 0; i < idx.length; i++) cnt[idx[i] + 1]++;
  for (let v = 0; v < nv; v++) cnt[v + 1] += cnt[v];
  const around = new Int32Array(idx.length), fill = cnt.slice(0, nv);
  for (let i = 0; i < idx.length; i++) { const v = idx[i]; around[fill[v]++] = i; }   // corner index i (its face is i / 3)
  const P = [], N = [], C = [], UV = [], T = [], newIdx = new Uint32Array(idx.length), used = new Uint8Array(idx.length);
  // tangent = the brushed / anisotropy direction: along X on faces that look up or down, horizontal (around the wall)
  // elsewhere — continuous over a smooth surface, so the highlight frame never tears; uv follows the tangent frame
  const pushV = (v, nx, ny, nz) => {
    const k = P.length / 3, x = pos[v * 3], y = pos[v * 3 + 1], z = pos[v * 3 + 2];
    P.push(x, y, z); N.push(nx, ny, nz); C.push(col[v * 3], col[v * 3 + 1], col[v * 3 + 2]);
    let tx, ty, tz;
    if (Math.abs(nz) > 0.92) { tx = 1 - nx * nx; ty = -nx * ny; tz = -nx * nz; }         // X projected onto the surface
    else { tx = ny; ty = -nx; tz = 0; }                                                   // cross(n, Z): horizontal
    const tl = Math.hypot(tx, ty, tz) || 1; tx /= tl; ty /= tl; tz /= tl;
    const bx = ny * tz - nz * ty, by = nz * tx - nx * tz, bz = nx * ty - ny * tx;          // bitangent = n × t
    T.push(tx, ty, tz, 1); UV.push((x * tx + y * ty + z * tz) / 10, (x * bx + y * by + z * bz) / 10);
    return k;
  };
  const copies = new Int32Array(4);
  for (let v = 0; v < nv; v++) {
    const a0 = cnt[v], a1 = cnt[v + 1], np = vpc ? vpc[v] : 0;
    if (np > 0) {
      // the exact surface planes at this vertex (from the stamp's normals): each face corner takes the nearest one, so a
      // triangle that spans a fold blends from one plane to the other instead of showing its own facet
      copies.fill(-1);
      for (let j = a0; j < a1; j++) {
        const f = (around[j] / 3) | 0; let best = 0, bd = -2;
        for (let q = 0; q < np; q++) { const d = fn[f * 3] * vpl[(v * 4 + q) * 3] + fn[f * 3 + 1] * vpl[(v * 4 + q) * 3 + 1] + fn[f * 3 + 2] * vpl[(v * 4 + q) * 3 + 2]; if (d > bd) { bd = d; best = q; } }
        if (copies[best] < 0) copies[best] = pushV(v, vpl[(v * 4 + best) * 3], vpl[(v * 4 + best) * 3 + 1], vpl[(v * 4 + best) * 3 + 2]);
        newIdx[around[j]] = copies[best];
      }
      continue;
    }
    for (let j = a0; j < a1; j++) {                      // fallback: greedy clusters of agreeing faces, one vertex copy per cluster
      if (used[j]) continue;
      const f0 = (around[j] / 3) | 0; let sx = 0, sy = 0, sz = 0; const members = [];
      for (let k = j; k < a1; k++) {
        if (used[k]) continue;
        const f = (around[k] / 3) | 0;
        if (fn[f * 3] * fn[f0 * 3] + fn[f * 3 + 1] * fn[f0 * 3 + 1] + fn[f * 3 + 2] * fn[f0 * 3 + 2] >= EDGE_COS) { used[k] = 1; members.push(around[k]); sx += fn[f * 3]; sy += fn[f * 3 + 1]; sz += fn[f * 3 + 2]; }
      }
      const l = Math.hypot(sx, sy, sz) || 1, nvI = pushV(v, sx / l, sy / l, sz / l);
      for (const corner of members) newIdx[corner] = nvI;
    }
  }
  return { pos: new Float32Array(P), nrm: new Float32Array(N), col: new Float32Array(C), uv: new Float32Array(UV), tan: new Float32Array(T), idx: newIdx };
}
