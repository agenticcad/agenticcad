// Small three.js viewer for the site: renders the real kernel meshes baked into tutorials/data/*.json
// (same shading, edges, tints and highlight colours as the app). Z-up, exact per-vertex normals.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const TINTS = [0x9fb0c2, 0xb7b09a, 0x9fc0b0, 0xb0a0c0, 0xc0a89a, 0x9ab5c8];
const COL = { hover: new THREE.Color(0xd2e0ee), sel: new THREE.Color(0xff8c42), ghost: new THREE.Color(0xc8783c), rapid: new THREE.Color(0xff5c5c) };
const ease = t => t < .5 ? 2 * t * t : -1 + (4 - 2 * t) * t;

export function createViewer(el, opts = {}) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: !!opts.alpha, powerPreference: 'high-performance' });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.domElement.style.cssText = 'display:block;width:100%;height:100%;outline:none';
  el.appendChild(renderer.domElement);
  const scene = new THREE.Scene();
  if (!opts.alpha) scene.background = new THREE.Color(opts.bg ?? 0x0f1319);
  const camera = new THREE.PerspectiveCamera(opts.fov ?? 36, 1, 0.1, 10000);
  camera.up.set(0, 0, 1);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true; controls.dampingFactor = 0.1;
  controls.enablePan = opts.pan ?? false; controls.enableZoom = opts.zoom ?? true; controls.enableRotate = opts.rotate ?? true;
  if (opts.autoRotate) { controls.autoRotate = true; controls.autoRotateSpeed = opts.autoRotate; }
  scene.add(new THREE.HemisphereLight(0xffffff, 0x334455, 0.9));
  const key = new THREE.DirectionalLight(0xffffff, 1.1); key.position.set(60, -80, 120); scene.add(key);
  const fill = new THREE.DirectionalLight(0xffffff, 0.4); fill.position.set(-80, 60, 40); scene.add(fill);

  let grid = null;
  function makeGrid(size) {
    if (grid) scene.remove(grid);
    if (opts.grid === false) return;
    const s = Math.max(50, Math.ceil(size * 2 / 10) * 10);
    grid = new THREE.GridHelper(s, s / 10, 0x2a3240, 0x1b2129); grid.rotation.x = Math.PI / 2; grid.position.z = -0.02; scene.add(grid);
  }
  const material = new THREE.MeshStandardMaterial({ vertexColors: true, metalness: 0.15, roughness: 0.55, side: THREE.DoubleSide, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1, transparent: true });
  const edgeMat = new THREE.LineBasicMaterial({ color: 0x0c0f13, transparent: true, opacity: 0.85 });
  const sketchMat = new THREE.LineBasicMaterial({ color: 0x3ee6c6, depthTest: false, transparent: true, opacity: 0.95 });
  const group = new THREE.Group(); scene.add(group);
  const overlay = new THREE.Group(); scene.add(overlay);

  // marker (orange dot + ring + normal stub, as in the app's chat-chip hover)
  const marker = new THREE.Group(); marker.visible = false; scene.add(marker);
  const mk = (g, m) => { const o = new THREE.Mesh(g, m); o.renderOrder = 10; return o; };
  marker.add(mk(new THREE.SphereGeometry(1, 24, 16), new THREE.MeshBasicMaterial({ color: 0xff8c42, depthTest: false, transparent: true, opacity: .9 })));
  marker.add(mk(new THREE.RingGeometry(1.6, 2.1, 48), new THREE.MeshBasicMaterial({ color: 0xff8c42, side: THREE.DoubleSide, depthTest: false, transparent: true, opacity: .8 })));
  const mline = new THREE.Line(new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3(0, 0, 4)]), new THREE.LineBasicMaterial({ color: 0xff8c42, depthTest: false })); mline.renderOrder = 10; marker.add(mline);

  let bodies = [], faces = new Map(), bbox = new THREE.Box3(), hovered = -1, fadeStart = 0;
  const selected = new Set(), ghost = new Set();
  let sketchObj = null, toolpath = null, stockObj = null, sketchReveal = null;

  function clearModel() { for (const b of bodies) { group.remove(b.mesh, b.lines); b.geom.dispose(); b.lines.geometry.dispose(); } bodies = []; faces = new Map(); selected.clear(); ghost.clear(); hovered = -1; if (sketchObj) { overlay.remove(sketchObj); sketchObj = null; } }
  function setModel(mesh, { fit = true, fade = true } = {}) {
    clearModel();
    faces = new Map(mesh.faces.map(f => [f.id, f]));
    mesh.bodies.forEach((bd, i) => {
      const g = new THREE.BufferGeometry();
      g.setAttribute('position', new THREE.Float32BufferAttribute(bd.positions, 3));
      g.setAttribute('normal', new THREE.Float32BufferAttribute(bd.normals, 3));
      g.setIndex(bd.indices);
      const n = bd.positions.length / 3, base = new THREE.Color(TINTS[i % TINTS.length]), colors = new Float32Array(n * 3);
      for (let k = 0; k < n; k++) { colors[k * 3] = base.r; colors[k * 3 + 1] = base.g; colors[k * 3 + 2] = base.b; }
      g.setAttribute('color', new THREE.BufferAttribute(colors, 3));
      const m = new THREE.Mesh(g, material.clone());
      const faceOfTri = new Int32Array(bd.indices.length / 3), ranges = new Map();
      for (const [fid, start, count] of bd.faceRanges) { ranges.set(fid, [start, count]); for (let t = start / 3; t < (start + count) / 3; t++) faceOfTri[t] = fid; }
      const ep = [];
      for (const pl of bd.edges) for (let k = 0; k + 5 < pl.length; k += 3) ep.push(pl[k], pl[k + 1], pl[k + 2], pl[k + 3], pl[k + 4], pl[k + 5]);
      const lg = new THREE.BufferGeometry(); lg.setAttribute('position', new THREE.Float32BufferAttribute(ep, 3));
      const lines = new THREE.LineSegments(lg, edgeMat.clone());
      group.add(m, lines);
      bodies.push({ id: bd.id, name: bd.name, mesh: m, lines, faceOfTri, ranges, colors, base, geom: g, attr: g.getAttribute('color') });
    });
    if (mesh.sketches && mesh.sketches.length) {
      const ep = [];
      for (const s of mesh.sketches) for (const pl of s.edges) for (let k = 0; k + 5 < pl.length; k += 3) ep.push(pl[k], pl[k + 1], pl[k + 2], pl[k + 3], pl[k + 4], pl[k + 5]);
      const lg = new THREE.BufferGeometry(); lg.setAttribute('position', new THREE.Float32BufferAttribute(ep, 3));
      sketchObj = new THREE.LineSegments(lg, sketchMat); sketchObj.renderOrder = 5; overlay.add(sketchObj);
    }
    bbox = new THREE.Box3(mesh.bboxMin ? new THREE.Vector3(...mesh.bboxMin) : undefined, mesh.bboxMax ? new THREE.Vector3(...mesh.bboxMax) : undefined);
    if (bbox.isEmpty()) bbox.setFromObject(group);
    if (bbox.isEmpty()) { bbox = new THREE.Box3(new THREE.Vector3(-30, -20, -5), new THREE.Vector3(30, 20, 20)); if (!bodies.length && fit) fitView(false); fit = false; }
    makeGrid(bbox.getSize(new THREE.Vector3()).length() / 2);
    if (fit) fitView(fit === 'animate');
    if (fade) { for (const b of bodies) { b.mesh.material.opacity = 0; b.lines.material.opacity = 0; } fadeStart = performance.now(); }
  }
  // ---- camera
  const home = new THREE.Vector3(1, -1.15, 0.8).normalize();
  let camAnim = null;
  function frameFor(dir, pad = 1.18) {
    dir = dir && dir.isVector3 ? dir : new THREE.Vector3(...(dir || [1, -1.15, 0.8])).normalize();
    const c = bbox.getCenter(new THREE.Vector3()), r = Math.max(bbox.getSize(new THREE.Vector3()).length() / 2, 5);
    const dist = r * pad / Math.sin(THREE.MathUtils.degToRad(camera.fov / 2));
    return { pos: c.clone().addScaledVector(dir, dist), target: c };
  }
  function fitView(animate = false, dir = home) { const f = frameFor(dir); if (animate) animateTo(f.pos, f.target, 700); else { camera.position.copy(f.pos); controls.target.copy(f.target); controls.update(); } }
  function animateTo(pos, target, ms = 700) { camAnim = { p0: camera.position.clone(), t0: controls.target.clone(), p1: pos, t1: target, start: performance.now(), ms }; return new Promise(r => camAnim.done = r); }
  function viewFrom(dir, ms = 700) { const f = frameFor(new THREE.Vector3(...dir).normalize()); return animateTo(f.pos, f.target, ms); }
  // ---- colours
  function paint(fid, color) {
    for (const b of bodies) { const rg = b.ranges.get(fid); if (!rg) continue;
      const idx = b.geom.index.array, c = color || b.base;
      for (let i = rg[0]; i < rg[0] + rg[1]; i++) { const v = idx[i]; b.colors[v * 3] = c.r; b.colors[v * 3 + 1] = c.g; b.colors[v * 3 + 2] = c.b; }
      b.attr.needsUpdate = true; return; }
  }
  function repaint() { for (const b of bodies) for (const fid of b.ranges.keys()) paint(fid, selected.has(fid) ? COL.sel : ghost.has(fid) ? COL.ghost : fid === hovered ? COL.hover : null); }
  function select(ids = [], { marker: showM = true } = {}) { selected.clear(); ids.forEach(i => selected.add(i)); repaint(); if (showM && ids.length) { const f = faces.get(ids[0]); if (f) showMarker(f.center, f.normal); } else hideMarker(); }
  function setGhost(ids = []) { ghost.clear(); ids.forEach(i => ghost.add(i)); repaint(); }
  function showMarker(center, normal) {
    const sz = Math.max(bbox.getSize(new THREE.Vector3()).length(), 20) * 0.012;
    marker.position.set(...center); marker.scale.setScalar(sz);
    const n = new THREE.Vector3(...(normal || [0, 0, 1])).normalize();
    marker.quaternion.setFromUnitVectors(new THREE.Vector3(0, 0, 1), n.lengthSq() ? n : new THREE.Vector3(0, 0, 1));
    mline.visible = !!normal; marker.visible = true;
  }
  function hideMarker() { marker.visible = false; }
  // ---- picking
  const ray = new THREE.Raycaster(), ptr = new THREE.Vector2();
  function pick(ev) {
    const r = renderer.domElement.getBoundingClientRect();
    ptr.set(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(ptr, camera);
    const hit = ray.intersectObjects(bodies.map(b => b.mesh), false)[0];
    if (!hit) return null;
    const b = bodies.find(x => x.mesh === hit.object); const fid = b.faceOfTri[hit.faceIndex];
    return { face: faces.get(fid), body: b, point: hit.point };
  }
  if (opts.pick) {
    renderer.domElement.addEventListener('pointermove', ev => { const h = pick(ev); const fid = h ? h.face.id : -1; if (fid !== hovered) { hovered = fid; repaint(); renderer.domElement.style.cursor = fid >= 0 ? 'pointer' : ''; } });
    renderer.domElement.addEventListener('pointerleave', () => { hovered = -1; repaint(); });
    let down = null;
    renderer.domElement.addEventListener('pointerdown', ev => down = [ev.clientX, ev.clientY]);
    renderer.domElement.addEventListener('pointerup', ev => { if (!down || Math.hypot(ev.clientX - down[0], ev.clientY - down[1]) > 4) return; const h = pick(ev); opts.pick(h, ev); });
  }
  // ---- toolpaths
  function setToolpaths(program, { progress = 1 } = {}) {
    clearToolpaths();
    if (!program) return;
    const pos = [], col = [];
    for (const op of program.ops) {
      const c = new THREE.Color(op.color); let prev = null;
      for (const m of op.moves) { if (prev) { const k = m[0] === 0 ? COL.rapid : c; pos.push(prev[1], prev[2], prev[3], m[1], m[2], m[3]); col.push(k.r, k.g, k.b, k.r, k.g, k.b); } prev = m; }
    }
    const g = new THREE.BufferGeometry(); g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3)); g.setAttribute('color', new THREE.Float32BufferAttribute(col, 3));
    toolpath = new THREE.LineSegments(g, new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, opacity: .95 })); toolpath.userData.total = pos.length / 3;
    overlay.add(toolpath); setToolpathProgress(progress);
    const s = program.stock; if (s) {
      const box = new THREE.Box3(new THREE.Vector3(s.xmin, s.ymin, s.zmin), new THREE.Vector3(s.xmax, s.ymax, s.zmax));
      const bg = new THREE.BoxGeometry(...box.getSize(new THREE.Vector3())); bg.translate(...box.getCenter(new THREE.Vector3()));
      stockObj = new THREE.LineSegments(new THREE.EdgesGeometry(bg), new THREE.LineBasicMaterial({ color: 0xffd166, transparent: true, opacity: .55 })); overlay.add(stockObj);
    }
  }
  function setToolpathProgress(f) { if (toolpath) toolpath.geometry.setDrawRange(0, Math.floor(toolpath.userData.total * Math.min(1, Math.max(0, f)) / 2) * 2); }
  function clearToolpaths() { if (toolpath) { overlay.remove(toolpath); toolpath.geometry.dispose(); toolpath = null; } if (stockObj) { overlay.remove(stockObj); stockObj = null; } }
  // ---- sketch overlay (plane-local items → world)
  function sketchSegments(plane, items) {
    const o = new THREE.Vector3(...plane.origin), x = new THREE.Vector3(...plane.x_dir).normalize(), z = new THREE.Vector3(...plane.z_dir).normalize(), y = z.clone().cross(x);
    const w = (u, v) => o.clone().addScaledVector(x, u).addScaledVector(y, v);
    const segs = [];
    const poly = pts => { for (let i = 0; i < pts.length; i++) { const a = w(...pts[i]), b = w(...pts[(i + 1) % pts.length]); segs.push(a.x, a.y, a.z + 0.05, b.x, b.y, b.z + 0.05); } };
    for (const it of items) {
      const ang = (it.angle || 0) * Math.PI / 180, cs = Math.cos(ang), sn = Math.sin(ang), rot = (u, v) => [it.cx + u * cs - v * sn, it.cy + u * sn + v * cs];
      if (it.type === 'rect') { const a = it.w / 2, b = it.h / 2; poly([rot(-a, -b), rot(a, -b), rot(a, b), rot(-a, b)]); }
      else if (it.type === 'circle') { const p = []; for (let i = 0; i < 72; i++) { const t = i / 72 * Math.PI * 2; p.push([it.cx + it.r * Math.cos(t), it.cy + it.r * Math.sin(t)]); } poly(p); }
      else if (it.type === 'polygon') { const p = []; const n = it.sides || 6; for (let i = 0; i < n; i++) { const t = i / n * Math.PI * 2; p.push(rot(it.r * Math.cos(t), it.r * Math.sin(t))); } poly(p); }
      else if (it.type === 'slot') { const L = it.length / 2, r = it.width / 2, p = []; for (let i = 0; i <= 24; i++) { const t = -Math.PI / 2 + i / 24 * Math.PI; p.push(rot(L + r * Math.cos(t), r * Math.sin(t))); } for (let i = 0; i <= 24; i++) { const t = Math.PI / 2 + i / 24 * Math.PI; p.push(rot(-L + r * Math.cos(t), r * Math.sin(t))); } poly(p); }
    }
    return segs;
  }
  function showSketch(plane, items, { reveal = 0 } = {}) {
    hideSketch();
    const g = new THREE.BufferGeometry(); g.setAttribute('position', new THREE.Float32BufferAttribute(sketchSegments(plane, items), 3));
    sketchReveal = new THREE.LineSegments(g, sketchMat.clone()); sketchReveal.renderOrder = 6; overlay.add(sketchReveal);
    if (reveal > 0) { const total = g.attributes.position.count; const start = performance.now(); sketchReveal.userData.anim = () => { const f = Math.min(1, (performance.now() - start) / reveal); g.setDrawRange(0, Math.floor(total * f / 2) * 2); return f >= 1; }; }
  }
  function hideSketch() { if (sketchReveal) { overlay.remove(sketchReveal); sketchReveal = null; } }
  // ---- loop
  const ro = new ResizeObserver(() => resize()); ro.observe(el);
  function resize() { const w = el.clientWidth || 300, h = el.clientHeight || 300; renderer.setSize(w, h, false); camera.aspect = w / h; camera.updateProjectionMatrix(); }
  resize();
  let running = true, visible = true;
  const io = new IntersectionObserver(es => { visible = es[0].isIntersecting; }, { threshold: 0.02 }); io.observe(el);
  function frame() {
    if (!running) return; requestAnimationFrame(frame); if (!visible) return;
    if (camAnim) { const t = Math.min(1, (performance.now() - camAnim.start) / camAnim.ms), e = ease(t); camera.position.lerpVectors(camAnim.p0, camAnim.p1, e); controls.target.lerpVectors(camAnim.t0, camAnim.t1, e); if (t >= 1) { const d = camAnim.done; camAnim = null; d && d(); } }
    if (fadeStart) { const f = Math.min(1, (performance.now() - fadeStart) / 420); for (const b of bodies) { b.mesh.material.opacity = f; b.lines.material.opacity = .85 * f; } if (f >= 1) fadeStart = 0; }
    if (sketchReveal && sketchReveal.userData.anim && sketchReveal.userData.anim()) sketchReveal.userData.anim = null;
    controls.update(); renderer.render(scene, camera);
  }
  frame();
  return { scene, camera, controls, renderer, setModel, fitView, viewFrom, animateTo, select, setGhost, showMarker, hideMarker, pick, setToolpaths, setToolpathProgress, clearToolpaths, showSketch, hideSketch, faces: () => faces, bbox: () => bbox, setAutoRotate: v => { controls.autoRotate = !!v; }, dispose: () => { running = false; ro.disconnect(); io.disconnect(); renderer.dispose(); el.innerHTML = ''; } };
}

// tiny python highlighter for code panels (keywords, strings, numbers, comments, calls)
export function hlPython(src) {
  const esc = s => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const KW = /\b(with|as|for|in|if|else|elif|import|from|def|return|not|and|or|None|True|False|class|lambda)\b/g;
  return esc(src).split('\n').map(line => {
    const ci = line.indexOf('#'); let code = line, com = '';
    if (ci >= 0 && !/["'][^"']*#/.test(line.slice(0, ci))) { code = line.slice(0, ci); com = line.slice(ci); }
    code = code.replace(/("[^"]*"|'[^']*')/g, '<span class="hl-s">$1</span>')
      .replace(/\b(\d+(?:\.\d+)?)\b/g, '<span class="hl-n">$1</span>')
      .replace(KW, '<span class="hl-k">$1</span>')
      .replace(/\b([A-Z][A-Za-z0-9_]*)(?=\()/g, '<span class="hl-t">$1</span>')
      .replace(/\b([a-z_][a-z0-9_]*)(?=\()/g, '<span class="hl-f">$1</span>');
    return code + (com ? `<span class="hl-c">${com}</span>` : '');
  }).join('\n');
}
export function hlGcode(src) {
  const esc = s => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  return esc(src).split('\n').map(l => l.startsWith(';') ? `<span class="hl-c">${l}</span>` : l.replace(/\b([GM]\d+)/g, '<span class="hl-k">$1</span>').replace(/\b([XYZIJFS])(-?\d+(?:\.\d+)?)/g, '<span class="hl-v">$1</span><span class="hl-n">$2</span>')).join('\n');
}
