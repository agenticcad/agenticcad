// Simulated session player. Data comes from tutorials/data/<id>.json, baked by tools/gen_site_tutorials.py
// in the main repo from the real kernel — every mesh and toolpath here is genuine kernel output.
import { createViewer, hlPython, hlGcode } from './viewer.js';
import { ICONS, RIBBON } from './ribbon.js';
import { LOGO } from './nav.js';

const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const md = s => esc(s).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/`([^`]+)`/g, '<code>$1</code>');
const CUBE = `<svg viewBox="0 0 72 72" fill="none" stroke="#8a96a8" stroke-width="1.2"><path d="M36 8 62 22v28L36 64 10 50V22z" fill="#232830"/><path d="M36 36 62 22M36 36v28M36 36 10 22"/><text x="19" y="30" fill="#cfd6df" font-size="8" font-family="Inter,sans-serif" stroke="none">FRONT</text><text x="42" y="30" fill="#cfd6df" font-size="8" font-family="Inter,sans-serif" stroke="none">RIGHT</text><text x="30" y="20" fill="#cfd6df" font-size="8" font-family="Inter,sans-serif" stroke="none">TOP</text></svg>`;

export function mountPlayer(root, tut, { onDone, next } = {}) {
  root.innerHTML = `<div class="pl">
    <div class="pl-top"><b>${esc(tut.title)}</b><span class="st" data-st>step 0 / ${tut.steps.length}</span><div class="sp"><span class="st">speed</span><button data-speed="1" class="on">1×</button><button data-speed="2">2×</button><button data-speed="4">4×</button></div></div>
    <div class="pl-narr"><span class="n" data-narr-n>—</span><div data-narr>Press <b>Play</b> to start, or step with the arrows below.</div></div>
    <div class="pl-app">
      <div class="pl-side">
        <div class="pl-hdr">${LOGO}<span>AgenticCAD</span><span class="ver">v0.10.1</span><span class="doc" data-doc>plate</span></div>
        <div class="pl-tabs"><span class="on">Chat</span><span>Code</span><span>CAM</span><span>Design</span><span>Library</span></div>
        <div class="pl-msgs" data-msgs></div>
        <div class="pl-design" data-design></div>
        <div class="pl-comp"><div class="pl-chips" data-chips></div><div class="pl-input" data-input></div><div class="row"><span>📎 attach · ⏎ send · ⇧⏎ newline</span><b data-send>Send</b></div></div>
      </div>
      <div class="pl-view">
        <div class="vw" data-vw></div>
        <div class="pl-ribbon"><div class="ribbon" data-ribbon>${RIBBON.map(g => `<div class="grp"><div class="row">${g.tools.map(t => `<div class="tb" data-tool="${t.id}">${ICONS[t.icon]}<span>${t.label}</span><kbd>${t.key}</kbd></div>`).join('')}</div><div class="cap">${g.name}</div></div>`).join('')}</div></div>
        <div class="pl-browser" data-browser><div class="t"><span>Browser</span><span>+ Part</span></div><div data-tree><div class="row"><i></i><span>—</span></div></div></div>
        <div class="pl-cube">${CUBE}</div>
        <div class="pl-cmd" data-cmd></div>
        <div class="pl-sketch" data-sketch></div>
        <div class="pl-cam" data-cam></div>
        <div class="pl-panel" data-panel><div class="t"><span data-panel-title>code</span><span>×</span></div><pre><code data-panel-code></code></pre></div>
        <div class="pl-cap" data-cap></div>
        <div class="pl-bottom"><span>⤢ Fit</span><span class="on">Edges</span><span>Mesh: normal</span><span>↶ Undo</span><span data-tp>Toolpaths</span></div>
        <div class="pl-hud" data-hud></div>
        <div class="pl-end" data-end><div class="box"><h3>Tutorial complete</h3><p data-end-text></p><div class="row"><button class="btn small" data-restart>↻ Replay</button>${next ? `<a class="btn small primary" href="?t=${next.id}">Next: ${esc(next.title)} →</a>` : '<a class="btn small primary" href="../docs/">Read the docs →</a>'}</div></div></div>
      </div>
    </div>
    <div class="pl-bar"><button data-prev title="previous (←)">⏮</button><button data-play title="play / pause (space)">▶ Play</button><button data-next title="next (→)">⏭</button><div class="prog" data-prog>${tut.steps.map((s, i) => `<i title="${i + 1}. ${s.kind}"></i>`).join('')}</div><span class="lab" data-lab></span></div>
  </div>`;
  const $ = sel => root.querySelector(sel);
  const narr = $('[data-narr]'), narrN = $('[data-narr-n]'), design = $('[data-design]'), msgs = $('[data-msgs]'), chips = $('[data-chips]'), input = $('[data-input]'), cap = $('[data-cap]'), cmd = $('[data-cmd]'), panel = $('[data-panel]'), sk = $('[data-sketch]'), camBox = $('[data-cam]'), hud = $('[data-hud]'), tree = $('[data-tree]');
  const viewer = createViewer($('[data-vw]'), { pan: true });
  const state = { idx: 0, playing: false, speed: 1, tok: { c: false }, fitted: false, timers: new Set() };

  // ---- helpers
  const sleep = ms => new Promise(res => { if (state.tok.c) return res(); const t = setTimeout(() => { state.timers.delete(t); res(); }, ms / state.speed); state.timers.add(t); });
  const cancel = () => { state.tok.c = true; state.tok = { c: false }; for (const t of state.timers) clearTimeout(t); state.timers.clear(); };
  const scroll = () => { msgs.scrollTop = msgs.scrollHeight; };
  const KIND = { user: 'You', agent: 'Agent', tool: 'Tool call', model: 'Model', select: 'Select', note: 'Tip', code: 'Script', gcode: 'G-code', sketchmode: 'Sketch', ribbon: 'Ribbon', toolpaths: 'CAM', params: 'Design tab' };
  const caption = (html, s, i) => { if (!html) return; narr.innerHTML = (s ? `<span class="k">${KIND[s.kind] || s.kind}</span>` : '') + md(html); narrN.textContent = i != null ? `${i + 1} / ${tut.steps.length}` : '—'; narr.parentElement.classList.remove('pulse'); void narr.offsetWidth; narr.parentElement.classList.add('pulse'); };
  const tab = name => root.querySelectorAll('.pl-tabs span').forEach(t => t.classList.toggle('on', t.textContent === name));
  const ribbonOn = id => root.querySelectorAll('[data-tool]').forEach(el => el.classList.toggle('on', el.dataset.tool === id));
  const showPanel = (title, html) => { $('[data-panel-title]').textContent = title; $('[data-panel-code]').innerHTML = html; panel.classList.add('show'); };
  const hidePanel = () => panel.classList.remove('show');
  const setTree = mesh => {
    if (!mesh) return;
    tree.innerHTML = mesh.bodies.map(b => `<div class="row"><i></i><span>${esc(b.name)}</span><em>${b.faceRanges.length} faces</em></div>`).join('') +
      (mesh.sketches && mesh.sketches.length ? `<div class="grp">Sketches</div>` + mesh.sketches.map(s => `<div class="row sk"><i></i><span>${esc(s.name)}</span><em>⋯</em></div>`).join('') : '');
    hud.textContent = mesh.summary.replace('Model OK: ', '');
  };
  const cmdHtml = (tool, picks, fields, prompt, stepIdx = 1, nsteps = 2) => {
    const t = RIBBON.flatMap(g => g.tools).find(x => x.id === tool) || { label: tool, icon: 'box' };
    return `<div class="h">${ICONS[t.icon]}<span>${t.label}</span><div class="steps">${Array.from({ length: nsteps }, (_, i) => `<i class="${i <= stepIdx ? 'on' : ''}"></i>`).join('')}</div></div>
      <div class="p">${esc(prompt)}</div><div class="picks">${picks.map(p => `<span class="chip">${esc(p)} <span style="opacity:.6">×</span></span>`).join('')}</div>
      ${fields.map(([k, v]) => `<div class="f"><label>${esc(k)}</label><span>${esc(v)}</span></div>`).join('')}
      <div class="b"><span>Cancel</span><span class="ok" data-ok>OK ↵</span></div>`;
  };

  // ---- step implementations; `q` = quick (seek) mode: no animation
  async function stepUser(s, q) {
    tab('Chat'); design.classList.remove('show');
    chips.innerHTML = (s.chips || []).map(c => `<span class="chip">${esc(c)}</span>`).join('');
    if (!q) { input.innerHTML = '<span class="cur"></span>'; const per = Math.min(28, 2200 / s.text.length); for (let i = 1; i <= s.text.length; i++) { if (state.tok.c) return; input.innerHTML = esc(s.text.slice(0, i)) + '<span class="cur"></span>'; await sleep(per); } await sleep(350); const b = $('[data-send]'); b.classList.add('flash'); await sleep(140); b.classList.remove('flash'); }
    const m = document.createElement('div'); m.className = 'msg user'; m.dataset.label = 'You'; m.innerHTML = (s.chips ? `<div class="chips">${s.chips.map(c => `<span class="chip">${esc(c)}</span>`).join('')}</div>` : '') + esc(s.text);
    msgs.appendChild(m); input.innerHTML = ''; chips.innerHTML = ''; viewer.setGhost([...viewer.faces().keys()].filter(() => false)); hidePanel(); scroll();
    if (!q) { const th = document.createElement('div'); th.className = 'think'; th.innerHTML = '<i></i><i></i><i></i> thinking'; msgs.appendChild(th); scroll(); await sleep(700); th.remove(); }
  }
  async function stepAgent(s, q) {
    const m = document.createElement('div'); m.className = 'msg agent'; m.dataset.label = 'Agent'; msgs.appendChild(m);
    if (!q) { const words = s.text.split(/(\s+)/); const per = Math.min(60, 2600 / words.length); let acc = ''; for (const w of words) { if (state.tok.c) return; acc += w; m.innerHTML = md(acc) + '<span class="cur"></span>'; scroll(); await sleep(per); } }
    m.innerHTML = md(s.text); scroll();
  }
  async function stepTool(s, q) {
    const t = document.createElement('div'); t.className = 'tool closed'; t.dataset.label = 'Tool call · ' + s.name;
    t.innerHTML = `<div class="h"><span class="spin"></span><span class="name">${esc(s.name)}</span><span class="det">${esc(s.detail || '')}</span>${s.code ? '<span style="color:var(--dim)">code ▸</span>' : ''}</div>${s.code ? `<pre><code>${s.name === 'build_cam' || s.name === 'build_model' ? hlPython(s.code) : esc(s.code)}</code></pre>` : ''}`;
    t.querySelector('.h').onclick = () => t.classList.toggle('closed');
    msgs.appendChild(t); scroll();
    if (s.code && (s.name === 'build_model' || s.name === 'build_cam')) showPanel(s.name === 'build_cam' ? 'cam.py' : 'design.py', hlPython(s.code));
    if (!q) await sleep(s.name === 'build_model' || s.name === 'build_cam' ? 1400 : 600);
    if (state.tok.c) return;
    t.querySelector('.spin').outerHTML = '<span class="ok">✓</span>';
    const r = document.createElement('div'); r.className = 'res'; r.textContent = s.result || ''; t.appendChild(r); scroll();
  }
  async function stepModel(s, q) {
    const mesh = tut.meshes[s.mesh]; sk.classList.remove('show'); viewer.hideSketch(); ribbonOn(null); cmd.classList.remove('show'); viewer.hideMarker();
    viewer.setModel(mesh, { fit: state.fitted ? (state.sketchView ? (q ? true : 'animate') : false) : (q ? true : 'animate'), fade: !q }); state.fitted = true; state.sketchView = false;
    setTree(mesh); 
  }
  async function stepSelect(s, q) {
    viewer.select([s.face]); const f = viewer.faces().get(s.face);
    chips.innerHTML = `<span class="chip"><b>Plate</b> · ${esc(f ? f.label : '#' + s.face)} <span style="opacity:.6">×</span></span>`;
    if (!q && f) { const dir = f.normal ? [f.normal[0] + 0.9, f.normal[1] - 1.1, f.normal[2] + 0.8] : [1, -1.15, .8]; viewer.viewFrom(dir, 700); }
    
  }
  async function stepNote(s) { const m = document.createElement('div'); m.className = 'msg note'; m.dataset.label = 'Tip'; m.innerHTML = md(s.text); msgs.appendChild(m); scroll(); }
  async function stepCode(s) { showPanel('design.py', hlPython(s.text));  }
  async function stepGcode(s) { showPanel('plate.nc — head', hlGcode(tut.gcode_head || ''));  }
  async function stepSketch(s, q) {
    hidePanel();
    if (s.face != null) { viewer.select([s.face], { marker: false }); if (!q) await sleep(900); if (state.tok.c) return; }
    ribbonOn('sketch');
    if (!q) await viewer.viewFrom(s.plane.z_dir.map((v, i) => v + (i === 2 ? 0.0001 : 0)), 800); else viewer.viewFrom(s.plane.z_dir, 0);
    if (state.tok.c) return;
    sk.innerHTML = `<div class="h">✎ Sketch on ${esc(s.plane.label || 'face')}</div><div class="tools"><span class="on">Rect</span><span>Circle</span><span>Polygon</span><span>Slot</span><span>Subtract</span></div>${s.items.map(it => `<div class="it"><span>${it.type}${it.mode === 'subtract' ? ' −' : ''}</span><em>${it.type === 'rect' ? `${it.w}×${it.h} @ ${it.cx},${it.cy}` : it.type === 'circle' ? `r${it.r} @ ${it.cx},${it.cy}` : ''}</em></div>`).join('')}<div class="b"><span>Cancel</span><span class="ok">Finish</span></div>`;
    sk.classList.add('show'); viewer.showSketch(s.plane, s.items, { reveal: q ? 0 : 1800 / state.speed }); state.sketchView = true; 
  }
  async function stepRibbon(s, q) {
    ribbonOn(s.tool); hidePanel(); const picks = [];
    if (s.face != null) { viewer.select([s.face]); const f = viewer.faces().get(s.face); picks.push(f ? f.label : 'face'); }
    if (s.point) { viewer.showMarker(s.point, [0, 0, 1]); picks.push(`(${s.point.join(', ')})`); }
    const t = RIBBON.flatMap(g => g.tools).find(x => x.id === s.tool);
    cmd.innerHTML = cmdHtml(s.tool, [], [], t ? t.tip.split(' — ')[1] : 'Click a face…', 0); cmd.classList.add('show');
    if (!q) await sleep(900); if (state.tok.c) return;
    cmd.innerHTML = cmdHtml(s.tool, picks, s.fields, 'Set the values and press OK', 1);
    
    if (!q) { await sleep(1600); if (state.tok.c) return; const ok = cmd.querySelector('[data-ok]'); if (ok) { ok.classList.add('flash'); await sleep(150); } }
  }
  async function stepToolpaths(s, q) {
    const p = tut.program; viewer.setToolpaths(p, { progress: q ? 1 : 0 }); $('[data-tp]').classList.add('on'); hidePanel();
    camBox.innerHTML = `<div class="h"><span>${esc(p.name)}</span><em>${p.time} min</em></div>` + p.ops.map(o => `<div class="op"><i style="background:${o.color}"></i><span>${esc(o.name)}</span><em>T${o.tool.number} · ${o.time}m</em></div>`).join('') + `<div class="op"><i style="background:var(--red)"></i><span>Rapids</span><em>${esc(p.machine)}</em></div>`;
    camBox.classList.add('show'); 
    if (!q) { viewer.viewFrom([1, -1.15, 1.3], 700); const t0 = performance.now(), D = 4500 / state.speed; while (performance.now() - t0 < D) { if (state.tok.c) return; viewer.setToolpathProgress((performance.now() - t0) / D); await sleep(30 * state.speed); } viewer.setToolpathProgress(1); }
  }
  async function stepParams(s, q) {
    tab('Design'); hidePanel();
    design.innerHTML = `<div class="t">Parameters</div>` + s.items.map(([k, v]) => `<div class="prow" data-p="${k}"><label>${k}</label><span>${v}</span></div>`).join('') + `<div class="sec"><b>Measure</b>📐 pick faces, edges, points · <b style="margin-top:8px">Shop drawings</b>Generate SVG + DXF · <b style="margin-top:8px">Import</b>STEP…</div>`;
    design.classList.add('show');
    const row = design.querySelector(`[data-p="${s.change[0]}"]`); const val = row.querySelector('span');
    if (!q) { await sleep(900); if (state.tok.c) return; row.classList.add('hot'); await sleep(500); const to = String(s.change[1]); for (let i = 1; i <= to.length; i++) { if (state.tok.c) return; val.textContent = to.slice(0, i); await sleep(220); } await sleep(400); }
    else { row.classList.add('hot'); val.textContent = String(s.change[1]); }
  }
  const IMPL = { params: stepParams, user: stepUser, agent: stepAgent, tool: stepTool, model: stepModel, select: stepSelect, note: stepNote, code: stepCode, gcode: stepGcode, sketchmode: stepSketch, ribbon: stepRibbon, toolpaths: stepToolpaths };
  const dwell = s => Math.min(7000, 1200 + ((s.narr || s.caption || '').length) * 30);

  function reset() { cancel(); msgs.innerHTML = ''; chips.innerHTML = ''; input.innerHTML = ''; design.classList.remove('show'); tab('Chat'); narr.innerHTML = 'Press <b>Play</b> to start, or step with the arrows below.'; narrN.textContent = '—'; viewer.setModel({ bodies: [], faces: [], sketches: [], summary: 'empty design' }, { fit: false, fade: false }); tree.innerHTML = '<div class="row" style="color:var(--dim)">no bodies yet</div>'; hud.textContent = 'untitled · empty design'; cmd.classList.remove('show'); sk.classList.remove('show'); camBox.classList.remove('show'); hidePanel(); ribbonOn(null); viewer.clearToolpaths(); viewer.hideSketch(); viewer.hideMarker(); $('[data-tp]').classList.remove('on'); $('[data-end]').classList.remove('show'); state.fitted = false; }
  function ui() {
    $('[data-st]').textContent = `step ${state.idx} / ${tut.steps.length}`;
    [...$('[data-prog]').children].forEach((el, i) => { el.classList.toggle('done', i < state.idx); el.classList.toggle('cur', i === state.idx - 1); });
    const s = tut.steps[Math.max(0, state.idx - 1)]; $('[data-lab]').textContent = state.idx ? ({ params: 'Design tab', user: 'you type', agent: 'agent replies', tool: 'tool: ' + (s.name || ''), model: 'model rebuilt', select: 'you click a face', note: 'tip', code: 'the script', gcode: 'G-code', sketchmode: 'sketch mode', ribbon: 'ribbon: ' + (s.tool || ''), toolpaths: 'toolpaths' })[s.kind] : 'ready';
    $('[data-play]').textContent = state.playing ? '❚❚ Pause' : (state.idx >= tut.steps.length ? '↻ Replay' : '▶ Play');
  }
  async function runStep(i, q) { const s = tut.steps[i]; caption(s.narr || s.caption || '', s, i); await IMPL[s.kind](s, q); }
  async function play() {
    if (state.idx >= tut.steps.length) { reset(); state.idx = 0; }
    state.playing = true; ui(); const tok = state.tok;
    while (state.playing && state.idx < tut.steps.length && !tok.c) {
      const s = tut.steps[state.idx]; state.idx++; ui(); await runStep(state.idx - 1, false); if (tok.c) return; await sleep(dwell(s)); }
    if (!tok.c && state.idx >= tut.steps.length) { state.playing = false; ui(); $('[data-end-text]').textContent = next ? `Up next: ${next.title}.` : 'That is the end of this series — the docs cover everything in depth.'; $('[data-end]').classList.add('show'); onDone && onDone(); }
  }
  function pause() { state.playing = false; cancel(); ui(); }
  async function seek(k, thenPlay = false) {
    const wasPlaying = state.playing; reset(); state.idx = 0;
    for (let i = 0; i < k; i++) { state.idx = i + 1; await runStep(i, true); }
    ui(); if (thenPlay || wasPlaying) play();
  }
  $('[data-play]').onclick = () => state.playing ? pause() : play();
  $('[data-prev]').onclick = () => seek(Math.max(0, state.idx - 1));
  $('[data-next]').onclick = async () => { if (state.idx >= tut.steps.length) return; const p = state.playing; pause(); const i = state.idx; state.idx++; ui(); await runStep(i, false); if (p) play(); };
  $('[data-restart]').onclick = () => seek(0, true);
  [...$('[data-prog]').children].forEach((el, i) => el.onclick = () => seek(i + 1));
  root.querySelectorAll('[data-speed]').forEach(b => b.onclick = () => { state.speed = +b.dataset.speed; root.querySelectorAll('[data-speed]').forEach(x => x.classList.toggle('on', x === b)); });
  panel.querySelector('.t span:last-child').onclick = hidePanel;
  const key = e => { if (e.target.closest('input,textarea')) return; if (e.code === 'Space') { e.preventDefault(); state.playing ? pause() : play(); } else if (e.key === 'ArrowRight') $('[data-next]').click(); else if (e.key === 'ArrowLeft') $('[data-prev]').click(); };
  document.addEventListener('keydown', key);
  // start with the first model visible so the stage is never empty
  const first = tut.steps[0].kind === 'model' ? tut.steps[0] : null; if (first) { viewer.setModel(tut.meshes[first.mesh], { fit: true, fade: false }); setTree(tut.meshes[first.mesh]); } else { viewer.setModel({ bodies: [], faces: [], sketches: [], summary: 'empty design' }, { fit: false, fade: false }); tree.innerHTML = '<div class="row" style="color:var(--dim)">no bodies yet</div>'; hud.textContent = 'untitled · empty design'; }
  ui();
  return { play, pause, seek, viewer, destroy: () => { cancel(); document.removeEventListener('keydown', key); viewer.dispose(); } };
}
