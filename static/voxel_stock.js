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
  if (d.type === 'reset') reset(d);
  else if (d.type === 'stamp') { if (!G || d.gen !== G.gen) return; for (const s of d.segs) stamp(s); flush(); }
  else if (d.type === 'color') {                       // recolour (and, with gouge/left maps, mark the final frame's tints)
    if (!G || d.gen !== G.gen) return; G.colors = d.colors; if (d.final !== undefined) { G.final = !!d.final; G.gouge = d.gouge || null; G.left = d.left || null; }
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
  // column SDF from the height map: for every nearby cell level (top, bottom) the material is bounded above and below
  // and, laterally, by the distance to the nearest cell whose top is lower (or bottom higher) — so flat areas are exact
  // and walls the keyframe already holds land between voxels at the server's resolution instead of on them
  const reach = h + g.res * 0.5, kr = Math.ceil(reach / g.res);
  const tops = new Float64Array(128), bots = new Float64Array(128), caps = new Float64Array(128);
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
        let cap = h;                                                          // lateral bound: nearest lower top / higher bottom
        for (let v = 0; v < nw; v++) if (wt[v] < wt[w] - 1e-6 || wb[v] > wb[w] + 1e-6) { if (wd[v] < cap) cap = wd[v]; }
        if (cap <= -h) continue;
        tops[nc] = wt[w]; bots[nc] = wb[w]; caps[nc] = cap; nc++;
      }
      if (!nc) continue;
      const base = iy * nx + ix;
      for (let iz = 0; iz < nz; iz++) {
        const z = z0 + iz * h; let v = -Infinity;
        for (let c = 0; c < nc; c++) { const a = tops[c] - z, b = z - bots[c]; let w = a < b ? a : b; if (caps[c] < w) w = caps[c]; if (w > v) v = w; }
        v /= h; m[iz * nx * ny + base] = v >= 1 ? SCALE : v <= -1 ? -SCALE : Math.round(v * SCALE);
      }
    }
  }
  const ncx = Math.ceil((nx - 1) / CS), ncy = Math.ceil((ny - 1) / CS), ncz = Math.ceil((nz - 1) / CS);
  G = { gen: d.gen, h, x0, y0, z0, nx, ny, nz, m, ncx, ncy, ncz, dirty: new Set(), srv: g, gouge: d.gouge || null, left: d.left || null, final: !!d.final, colors: d.colors };
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

function stamp(s) {
  const { h, x0, y0, z0, nx, ny, nz, m } = G, r = s.r, side = s.side;
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
      const base = iy * nx + ix;
      let first = -1, last = -1;
      if (side > 0) {
        for (let iz = Math.max(0, Math.floor((zc - h - z0) / h)); iz < nz; iz++) {
          const z = z0 + iz * h; let rr = zc - z; if (dmin > rr) rr = dmin;
          const q = rr >= h ? SCALE : rr <= -h ? -SCALE : Math.round(rr / h * SCALE), i = iz * NXY + base;
          if (q < m[i]) { m[i] = q; if (first < 0) first = iz; last = iz; }
        }
      } else {
        for (let iz = Math.min(nz - 1, Math.ceil((zc + h - z0) / h)); iz >= 0; iz--) {
          const z = z0 + iz * h; let rr = z - zc; if (dmin > rr) rr = dmin;
          const q = rr >= h ? SCALE : rr <= -h ? -SCALE : Math.round(rr / h * SCALE), i = iz * NXY + base;
          if (q < m[i]) { m[i] = q; if (last < 0) last = iz; first = iz; }
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
    chunks.push({ id, pos: r.pos, nrm: r.nrm, col: r.col, idx: r.idx }); transfer.push(r.pos.buffer, r.nrm.buffer, r.col.buffer, r.idx.buffer);
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
  const { h, x0, y0, z0, nx, ny, nz, m } = G, NXY = nx * ny;
  const cA = [cx * CS - 1, cy * CS - 1, cz * CS - 1], cB = [Math.min(nx - 2, cx * CS + CS - 1), Math.min(ny - 2, cy * CS + CS - 1), Math.min(nz - 2, cz * CS + CS - 1)];
  for (let a = 0; a < 3; a++) if (cA[a] < 0) cA[a] = 0;
  const W = CS + 1, vmap = new Int32Array(W * W * W).fill(-1);
  const pos = [], nrm = [], col = [], idx = [];
  const V = (i, j, k) => m[k * NXY + j * nx + i];
  const off = [[0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 1, 0], [0, 0, 1], [1, 0, 1], [0, 1, 1], [1, 1, 1]];
  const edges = [[0, 1], [2, 3], [4, 5], [6, 7], [0, 2], [1, 3], [4, 6], [5, 7], [0, 4], [1, 5], [2, 6], [3, 7]];
  const cv = new Float64Array(8);
  for (let k = cA[2]; k <= cB[2]; k++) for (let j = cA[1]; j <= cB[1]; j++) for (let i = cA[0]; i <= cB[0]; i++) {
    let pos8 = 0, neg8 = 0;
    for (let c = 0; c < 8; c++) { const v = V(i + off[c][0], j + off[c][1], k + off[c][2]); cv[c] = v; if (v > 0) pos8++; else neg8++; }
    if (!pos8 || !neg8) continue;
    let sx = 0, sy = 0, sz = 0, ne = 0;
    for (const [a, b] of edges) {
      const va = cv[a], vb = cv[b]; if ((va > 0) === (vb > 0)) continue;
      const t = va / (va - vb), oa = off[a], ob = off[b];
      sx += oa[0] + (ob[0] - oa[0]) * t; sy += oa[1] + (ob[1] - oa[1]) * t; sz += oa[2] + (ob[2] - oa[2]) * t; ne++;
    }
    const vx = x0 + (i + sx / ne) * h, vy = y0 + (j + sy / ne) * h, vz = z0 + (k + sz / ne) * h;
    // outward normal = -gradient (the field grows into the material)
    let gx = (cv[1] + cv[3] + cv[5] + cv[7]) - (cv[0] + cv[2] + cv[4] + cv[6]);
    let gy = (cv[2] + cv[3] + cv[6] + cv[7]) - (cv[0] + cv[1] + cv[4] + cv[5]);
    let gz = (cv[4] + cv[5] + cv[6] + cv[7]) - (cv[0] + cv[1] + cv[2] + cv[3]);
    const gl = Math.hypot(gx, gy, gz) || 1; gx /= -gl; gy /= -gl; gz /= -gl;
    vmap[((k - cA[2]) * W + (j - cA[1])) * W + (i - cA[0])] = pos.length / 3;
    pos.push(vx, vy, vz); nrm.push(gx, gy, gz); const o = col.length; col.length += 3; vertexColor(vx, vy, col, o);
  }
  if (!pos.length) return { pos: new Float32Array(0), nrm: new Float32Array(0), col: new Float32Array(0), idx: new Uint32Array(0) };
  const vat = (i, j, k) => (i < cA[0] || j < cA[1] || k < cA[2] || i > cB[0] || j > cB[1] || k > cB[2]) ? -1 : vmap[((k - cA[2]) * W + (j - cA[1])) * W + (i - cA[0])];
  const quad = (a, b, c, d, flip) => { if (a < 0 || b < 0 || c < 0 || d < 0) return; if (flip) idx.push(a, d, c, a, c, b); else idx.push(a, b, c, a, c, d); };
  const pA = [cx * CS, cy * CS, cz * CS], pB = [Math.min(nx - 2, cx * CS + CS - 1), Math.min(ny - 2, cy * CS + CS - 1), Math.min(nz - 2, cz * CS + CS - 1)];
  for (let k = pA[2]; k <= pB[2]; k++) for (let j = pA[1]; j <= pB[1]; j++) for (let i = pA[0]; i <= pB[0]; i++) {
    const v0 = V(i, j, k) > 0;
    if ((V(i + 1, j, k) > 0) !== v0) quad(vat(i, j - 1, k - 1), vat(i, j, k - 1), vat(i, j, k), vat(i, j - 1, k), !v0);
    if ((V(i, j + 1, k) > 0) !== v0) quad(vat(i - 1, j, k - 1), vat(i - 1, j, k), vat(i, j, k), vat(i, j, k - 1), !v0);
    if ((V(i, j, k + 1) > 0) !== v0) quad(vat(i - 1, j - 1, k), vat(i, j - 1, k), vat(i, j, k), vat(i - 1, j, k), !v0);
  }
  return { pos: new Float32Array(pos), nrm: new Float32Array(nrm), col: new Float32Array(col), idx: new Uint32Array(idx) };
}
