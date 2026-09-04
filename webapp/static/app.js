'use strict';
// Cost knobs, grouped and labelled. Order and grouping mirror CutConfig so the form
// reads like the cost model rather than like a JSON dump.
const FIELDS = [
  ['Sheet', [
    ['sheet_width_in', 'sheet width (in)', 0.125],
    ['sheet_length_in', 'sheet length (in)', 0.125],
    ['sheet_edge_trim_across_in', 'edge trim across (in)', 0.03125],
    ['sheet_edge_trim_along_in', 'edge trim along (in)', 0.03125],
  ]],
  ['Saws', [
    ['kerf_track_saw_in', 'track saw kerf (in)', 0.03125],
    ['kerf_mitre_saw_in', 'mitre saw kerf (in)', 0.03125],
    ['mitre_max_crosscut_width_in', 'mitre crosscut capacity (in)', 0.25],
  ]],
  ['Money', [
    ['labour_dollars_per_hour', 'labour ($/hour)', 1],
  ]],
  ['Track saw (minutes)', [
    ['min_per_track_rip', 'rip, stop unchanged', 0.25],
    ['extra_min_per_track_stop_change', 'extra to move the stop', 0.25],
    ['min_per_track_crosscut', 'crosscut (strip too wide for mitre)', 0.25],
  ]],
  ['Mitre saw (minutes)', [
    ['min_per_mitre_crosscut', 'crosscut', 0.05],
    ['extra_min_per_mitre_stop_change', 'extra to reset the stop', 0.25],
  ]],
  ['Third cut (minutes)', [
    ['min_per_trim_rip', 'trim rip', 0.25],
    ['extra_min_per_trim_stop_change', 'extra to set the trim stop', 0.25],
  ]],
  ['Handling (minutes)', [
    ['min_per_sheet_setup', 'per sheet', 0.5],
    ['min_per_strip_handling', 'per strip', 0.25],
    ['min_per_saw_changeover', 'per trip between saws', 0.25],
  ]],
];

const S = { stepId: null, cfg: null, layout: null, ref: null, baseline: null,
            job: null, sheetCosts: {} };
const $ = s => document.querySelector(s);
const el = (t, a = {}, ...kids) => {
  const n = document.createElement(t);
  for (const [k, v] of Object.entries(a)) {
    if (k === 'class') n.className = v;
    else if (k === 'html') n.innerHTML = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  }
  for (const c of kids.flat()) if (c != null) n.append(c.nodeType ? c : String(c));
  return n;
};
const money = v => (v < 0 ? '-$' : '$') + Math.abs(v).toLocaleString(undefined,
  { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const IN = u => u / 32;
// 1/32" units to a readable fraction
function fmt(u) {
  const whole = Math.floor(u / 32); let n = u % 32, d = 32;
  while (n && n % 2 === 0) { n /= 2; d /= 2; }
  return n ? `${whole}-${n}/${d}"` : `${whole}"`;
}
const widthColor = i => `hsl(${(i * 137.508) % 360} 58% 60%)`;
function toast(m, ms = 2200) {
  const t = $('#toast'); t.textContent = m; t.classList.add('on');
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove('on'), ms);
}
async function api(url, opts = {}) {
  const r = await fetch(url, opts);
  const j = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
  if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}

// ---------- config form ----------
function buildConfigForm() {
  const host = $('#cfgform'); host.textContent = '';
  for (const [group, fields] of FIELDS) {
    const fs = el('fieldset', {}, el('legend', {}, group));
    for (const [key, label, step] of fields) {
      const inp = el('input', { type: 'number', step, value: S.cfg[key], 'data-k': key });
      inp.addEventListener('input', onCostEdit);
      fs.append(el('label', { class: 'f' }, el('span', {}, label), inp));
    }
    host.append(fs);
  }
  const fs = el('fieldset', {}, el('legend', {}, 'Sheet price ($) by thickness'));
  for (const [t, c] of Object.entries(S.cfg.sheet_cost_by_thickness)) {
    const inp = el('input', { type: 'number', step: 1, value: c, 'data-t': t });
    inp.addEventListener('input', onCostEdit);
    fs.append(el('label', { class: 'f' }, el('span', {}, `${t}" ply`), inp));
  }
  host.append(fs);
}
function readConfigForm() {
  const c = JSON.parse(JSON.stringify(S.cfg));
  for (const i of $('#cfgform').querySelectorAll('input[data-k]'))
    c[i.dataset.k] = parseFloat(i.value);
  for (const i of $('#cfgform').querySelectorAll('input[data-t]'))
    c.sheet_cost_by_thickness[i.dataset.t] = parseFloat(i.value);
  return c;
}
let repriceTimer = null;
function onCostEdit() {
  S.cfg = readConfigForm();
  if (!S.ref) return;
  clearTimeout(repriceTimer);
  repriceTimer = setTimeout(reprice, 350);   // milliseconds, so just do it live
}
async function reprice() {
  try {
    const j = await api('/api/rescore', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ref: S.ref, config: S.cfg }),
    });
    S.layout = j; render();
  } catch (e) { toast('reprice failed: ' + e.message, 4000); }
}

// ---------- upload / solve ----------
async function loadRecent() {
  const ups = await api('/api/uploads');
  const sel = $('#recent'); sel.textContent = '';
  sel.append(el('option', { value: '' }, ups.length ? '— pick —' : 'none yet'));
  for (const u of ups)
    sel.append(el('option', { value: u.id }, `${u.filename} (${u.parts} parts)`));
  sel.onchange = () => { if (sel.value) { S.stepId = sel.value; $('#upinfo').textContent =
    sel.options[sel.selectedIndex].text; $('#solve').disabled = false; } };
}
$('#file').addEventListener('change', async ev => {
  const f = ev.target.files[0]; if (!f) return;
  $('#upinfo').textContent = 'reading…';
  const fd = new FormData(); fd.append('file', f);
  try {
    const j = await api('/api/upload', { method: 'POST', body: fd });
    S.stepId = j.id;
    $('#upinfo').innerHTML = `<b>${j.parts} parts</b>, ${j.types} distinct types<br>` +
      j.thicknesses.map(t =>
        `${t.thickness}" — ${t.parts} parts, ${t.sqft.toFixed(1)} sqft, ` +
        `floor ${t.floor.toFixed(2)}→${t.floor_int} sheets`).join('<br>');
    $('#solve').disabled = false;
    loadRecent();
  } catch (e) { $('#upinfo').innerHTML = `<span class="err">${e.message}</span>`; }
});

$('#solve').addEventListener('click', async () => {
  if (!S.stepId) return toast('upload a STEP file first');
  $('#solveerr').textContent = '';
  const seconds = parseFloat($('#seconds').value) || 30;
  $('#solve').disabled = true; $('#progress').style.display = 'block';
  try {
    const { job_id } = await api('/api/solve', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ step_id: S.stepId, config: S.cfg, seconds }),
    });
    S.job = job_id; pollSolve(job_id, seconds);
  } catch (e) {
    $('#solveerr').textContent = e.message;
    $('#solve').disabled = false; $('#progress').style.display = 'none';
  }
});
async function pollSolve(jobId, seconds) {
  try {
    const j = await api('/api/solve/' + jobId);
    const pct = Math.min(100, (j.elapsed / seconds) * 100);
    $('#progress').querySelector('i').style.width = pct + '%';
    $('#progtext').textContent =
      `${j.elapsed.toFixed(0)}s of ${seconds}s · ${j.done} restarts` +
      (j.best != null ? ` · best ${money(j.best)}` : '');
    if (j.status === 'running') return setTimeout(() => pollSolve(jobId, seconds), 600);
    $('#solve').disabled = false;
    if (j.status === 'error') { $('#solveerr').textContent = j.error; return; }
    $('#progress').querySelector('i').style.width = '100%';
    S.layout = j.result; S.ref = 'job_' + jobId;
    S.baseline = { dollars: j.result.score.dollars, name: 'this solve' };
    render(); toast('solved: ' + money(j.result.score.dollars));
  } catch (e) {
    $('#solveerr').textContent = e.message; $('#solve').disabled = false;
  }
}

// ---------- rendering ----------
function render() {
  if (!S.layout) return;
  renderScorebar(); renderLegend(); renderSheets(); renderCuts(); renderCost();
  const p = S.layout.provenance || {};
  $('#curinfo').innerHTML =
    `<b>${money(S.layout.score.dollars)}</b> · ${S.layout.score.n_sheets} sheets · ` +
    `${S.layout.score.hours.toFixed(1)} h` +
    (p.restarts_completed ? `<br>${p.restarts_completed} restarts in ${p.seconds}s` : '');
}

function renderScorebar() {
  const s = S.layout.score, f = S.layout.floor;
  const host = $('#scorebar'); host.textContent = '';
  const d = S.baseline ? s.dollars - S.baseline.dollars : 0;
  host.append(
    el('div', { class: 'spread' },
      el('div', {},
        el('div', { class: 'big' }, money(s.dollars)),
        el('div', { class: 'small muted' },
          `${money(s.material)} material · ${money(s.labour)} labour ` +
          `(${s.hours.toFixed(2)} h)`)),
      el('div', { style: 'text-align:right' },
        el('div', { class: 'mono' }, `${s.n_sheets} sheets`),
        el('div', { class: 'small muted' },
          Object.entries(s.sheets_by_thickness).map(([t, n]) => `${t}"×${n}`).join('  ')),
        el('div', { class: 'small muted' }, `${(s.utilisation * 100).toFixed(1)}% used`)),
      el('div', { style: 'text-align:right' },
        el('div', { class: 'small muted' }, 'working floor'),
        el('div', { class: 'mono' }, money(f.working)),
        el('div', { class: 'small muted' },
          `${((s.dollars / f.working - 1) * 100).toFixed(0)}% above`))));
  if (S.baseline && Math.abs(d) > 0.005) {
    host.append(el('div', { class: 'small', style: 'margin-top:8px' },
      el('span', { class: 'delta ' + (d > 0 ? 'up' : 'down') },
        `${d > 0 ? '+' : ''}${money(d)}`),
      el('span', { class: 'muted' },
        ` vs ${S.baseline.name} — this layout repriced, NOT re-optimised. `),
      el('button', { onclick: () => $('#solve').click() },
        'Re-solve with these costs')));
  }
}

function renderLegend() {
  const host = $('#legend'); host.textContent = '';
  const card = el('div', { class: 'card' },
    el('h2', {}, 'Strip widths — equal widths share a colour and one stop setting'));
  const wrap = el('div', { class: 'legend' });
  for (const w of S.layout.widths)
    wrap.append(el('span', { class: 'chip', title: `${w.count} strip(s)` },
      el('i', { class: 'sw', style: `background:${widthColor(w.color)}` }),
      el('b', {}, fmt(w.width)), el('span', { class: 'muted' }, `×${w.count}`),
      el('span', { class: 'muted' }, w.saw === 'track' ? '· track' : '· mitre')));
  card.append(wrap);
  card.append(el('div', { class: 'cutkey' },
    el('span', {}, el('i', { style: 'border-top-color:var(--track)' }), 'track saw rip'),
    el('span', {}, el('i', { style: 'border-top-color:var(--mitre);border-top-style:dashed' }), 'mitre saw crosscut'),
    el('span', {}, el('i', { style: 'border-top-color:var(--track);border-top-style:dashed' }), 'track saw crosscut (strip too wide for mitre)'),
    el('span', {}, el('i', { style: 'border-top-color:var(--trim);border-top-style:dotted' }), 'trim rip')));
  host.append(card);
}

const SVGNS = 'http://www.w3.org/2000/svg';
const sv = (t, a) => { const n = document.createElementNS(SVGNS, t);
  for (const [k, v] of Object.entries(a)) n.setAttribute(k, v); return n; };

function renderSheets() {
  const host = $('#sheets'); host.textContent = '';
  // sheets arrive in rip-sequence order, so number them by that order -- it is the
  // order you actually cut them in. Labelling by the solver's internal index made the
  // first sheet on screen read "Sheet 14".
  S.layout.sheets.forEach((sh, i) => host.append(sheetCard(sh, i)));
}

function sheetCard(sh, pos) {
  const M = 26, W = sh.along, H = sh.across;
  const svg = sv('svg', { viewBox: `${-M} ${-M} ${W + 2 * M} ${H + 2 * M}` });

  svg.append(sv('rect', { x: 0, y: 0, width: W, height: H, fill: '#fff',
    stroke: '#333', 'stroke-width': 3 }));

  for (const st of sh.strips) {
    // the strip band: shows the width group, and therefore which rips share a stop
    svg.append(sv('rect', { x: 0, y: st.y, width: W, height: st.width,
      fill: widthColor(st.color), opacity: 0.16 }));
    for (const p of st.parts) {
      svg.append(sv('rect', { x: p.x, y: p.y, width: p.len, height: p.w,
        fill: widthColor(st.color), opacity: 0.62, stroke: '#2a2a2a',
        'stroke-width': 1.5 }));
      if (p.len > 300 && p.w > 90) {
        const t = sv('text', { x: p.x + p.len / 2, y: p.y + p.w / 2 + 14,
          'text-anchor': 'middle', 'font-size': 40, fill: '#111' });
        t.textContent = p.label;
        svg.append(t);
      }
      if (p.trim) {
        const t = sv('text', { x: p.x + 12, y: p.y + p.w - 12, 'font-size': 30,
          fill: 'var(--trim)' });
        t.textContent = '↕trim';
        svg.append(t);
      }
    }
    if (st.offcut > 0) {
      const x = W - st.offcut;
      svg.append(sv('rect', { x, y: st.y, width: st.offcut, height: st.width,
        fill: 'url(#hatch)', opacity: 0.5 }));
    }
  }

  const defs = sv('defs', {});
  const pat = sv('pattern', { id: 'hatch', width: 12, height: 12,
    patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' });
  pat.append(sv('line', { x1: 0, y1: 0, x2: 0, y2: 12, stroke: '#9a9a9a',
    'stroke-width': 4 }));
  defs.append(pat); svg.append(defs);

  for (const c of sh.cuts) {
    const colour = c.saw === 'track' ? 'var(--track)' : 'var(--mitre)';
    const a = { stroke: c.type === 'trim' ? 'var(--trim)' : colour,
      'stroke-width': c.type === 'rip' ? 5 : 3.5, 'stroke-linecap': 'round' };
    if (c.type === 'cross') a['stroke-dasharray'] = '18 12';
    if (c.type === 'trim') a['stroke-dasharray'] = '5 9';
    if (c.orient === 'along')
      svg.append(sv('line', { x1: c.from, y1: c.at, x2: c.to, y2: c.at, ...a }));
    else
      svg.append(sv('line', { x1: c.at, y1: c.from, x2: c.at, y2: c.to, ...a }));
  }

  const lab = (x, y, s, anchor = 'middle', size = 34) => {
    const t = sv('text', { x, y, 'text-anchor': anchor, 'font-size': size,
      fill: '#666' }); t.textContent = s; return t;
  };
  svg.append(lab(W / 2, -8, `${sh.along_in}"`));
  const yl = lab(-8, H / 2, `${sh.across_in}"`, 'middle');
  yl.setAttribute('transform', `rotate(-90 -8 ${H / 2})`);
  svg.append(yl);

  const c = sh.counts;
  return el('div', { class: 'sheet' },
    el('div', { class: 'spread', style: 'margin-bottom:6px' },
      el('div', {}, el('b', {}, `Sheet ${pos + 1} of ${S.layout.sheets.length}`),
        el('span', { class: 'muted small' },
          ` · ${sh.thickness}" ply · ${sh.along_in}×${sh.across_in}"` +
          (sh.swapped ? ' · rips run the short way' : ''))),
      el('div', { class: 'small muted mono' },
        `${(sh.utilisation * 100).toFixed(0)}% used · ${c.n_rips} rips · ` +
        `${c.n_cross} mitre · ${c.n_wide_cross} track crosscuts` +
        (c.n_trims ? ` · ${c.n_trims} trims` : ''))),
    svg);
}

function renderCuts() {
  const host = $('#cutlist'); host.textContent = '';
  for (const p of S.layout.rip_plan) {
    const sh = S.layout.sheets.find(s => s.index === p.sheet);
    const card = el('div', { class: 'card' },
      el('div', { class: 'spread' },
        el('b', {}, `Sheet ${p.position + 1} of ${S.layout.rip_plan.length}`),
        el('span', { class: 'small muted' }, `${p.thickness}" ply`)));
    for (const g of p.groups) {
      const strips = sh.strips.filter(s => s.width === g.width);
      card.append(el('div', { style: 'margin:9px 0 3px' },
        el('i', { class: 'sw', style:
          `background:${widthColor(g.color)};display:inline-block;margin-right:6px` }),
        el('b', {}, `RIP ${g.count} strip${g.count > 1 ? 's' : ''} @ ${fmt(g.width)}`),
        el('span', { class: 'small', style: `margin-left:8px;color:${
          g.reuses_stop ? 'var(--good)' : 'var(--warn)'}` },
          g.reuses_stop ? 'stop already set' : 'move the stop'),
        el('span', { class: 'small muted', style: 'margin-left:8px' },
          `→ crosscut on ${g.saw === 'track' ? 'TRACK SAW (too wide for mitre)' : 'mitre saw'}`)));
      for (const st of strips) {
        const runs = [];
        for (const q of st.parts) {
          const last = runs[runs.length - 1];
          if (last && last.len === q.len && last.trim === q.trim) last.n++;
          else runs.push({ len: q.len, n: 1, trim: q.trim, to: q.trim_to });
        }
        card.append(el('div', { class: 'small mono', style: 'margin-left:22px' },
          runs.map(r => `${r.n}× ${fmt(r.len)}${r.trim ? ` +trim to ${fmt(r.to)}` : ''}`)
            .join(',  ') +
          (st.offcut > 0 ? `   [offcut ${fmt(st.offcut)}]` : '   [exact]')));
      }
    }
    host.append(card);
  }
}

function renderCost() {
  const host = $('#costpane'); host.textContent = '';
  const s = S.layout.score, f = S.layout.floor;
  const t = el('table', {}, el('thead', {}, el('tr', {},
    el('th', {}, 'operation'), el('th', { class: 'n' }, 'count'),
    el('th', { class: 'n' }, 'minutes'), el('th', { class: 'n' }, 'cost'))));
  const tb = el('tbody');
  for (const r of S.layout.breakdown)
    tb.append(el('tr', {}, el('td', {}, r.label), el('td', { class: 'n' }, r.count),
      el('td', { class: 'n' }, r.minutes.toFixed(1)),
      el('td', { class: 'n' }, money(r.dollars))));
  tb.append(el('tr', {}, el('td', {}, el('b', {}, 'labour total')),
    el('td', { class: 'n' }, ''), el('td', { class: 'n' }, s.minutes.toFixed(1)),
    el('td', { class: 'n' }, el('b', {}, money(s.labour)))));
  tb.append(el('tr', {}, el('td', {}, el('b', {}, 'material')),
    el('td', { class: 'n' }, s.n_sheets), el('td', { class: 'n' }, ''),
    el('td', { class: 'n' }, el('b', {}, money(s.material)))));
  t.append(tb);
  host.append(el('div', { class: 'card' }, el('h2', {}, 'Where the money goes'), t));
  host.append(el('div', { class: 'card' }, el('h2', {}, 'Bounds'),
    el('div', { class: 'small' },
      `Hard floor ${money(f.hard)} — provable from area and forced cuts alone. ` +
      `Working floor ${money(f.working)} — adds one crosscut per part. ` +
      `Minimum possible sheets ${f.min_sheets}. ` +
      `${f.forced_track_parts} of ${f.forced_track_parts + f.mitre_reachable_parts} ` +
      `parts are too wide for the mitre saw whatever the layout.`)));
}

// ---------- saved & compare ----------
$('#save').addEventListener('click', async () => {
  if (!S.ref) return toast('nothing to save');
  try {
    const j = await api('/api/saved', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ref: S.ref, name: $('#savename').value }),
    });
    $('#savename').value = ''; toast('saved “' + j.name + '”'); renderSaved();
  } catch (e) { toast('save failed: ' + e.message, 4000); }
});

async function renderSaved() {
  const host = $('#savedpane'); host.textContent = 'loading…';
  const { layouts } = await api('/api/saved');
  host.textContent = '';
  if (!layouts.length) {
    host.append(el('div', { class: 'card muted' },
      'No saved layouts yet. Solve one, then Save it — every saved layout is ' +
      'repriced under the costs currently in the form, so they stay comparable.'));
    return;
  }
  const best = Math.min(...layouts.filter(l => l.valid).map(l => l.dollars));
  const t = el('table', {}, el('thead', {}, el('tr', {},
    el('th', {}, 'name'), el('th', { class: 'n' }, 'total'),
    el('th', { class: 'n' }, 'vs best'), el('th', { class: 'n' }, 'sheets'),
    el('th', { class: 'n' }, 'material'), el('th', { class: 'n' }, 'labour'),
    el('th', { class: 'n' }, 'rips'), el('th', { class: 'n' }, 'stops'),
    el('th', { class: 'n' }, 'mitre'), el('th', { class: 'n' }, 'track x'),
    el('th', { class: 'n' }, 'trims'), el('th', {}, ''))));
  const tb = el('tbody');
  for (const l of layouts) {
    if (!l.valid) {
      tb.append(el('tr', {}, el('td', {}, l.name),
        el('td', { class: 'err', colspan: 11 },
          'not valid under the current cost model: ' + l.error)));
      continue;
    }
    const d = l.dollars - best;
    tb.append(el('tr', { class: l.id === (S.ref || '').replace('job_', '') ? 'sel' : '' },
      el('td', {}, el('a', { href: '#', onclick: ev => { ev.preventDefault(); open_(l.id); } },
        l.name), el('div', { class: 'small muted' }, l.filename || '')),
      el('td', { class: 'n' }, money(l.dollars)),
      el('td', { class: 'n' }, d < 0.005 ? '—' :
        el('span', { class: 'delta up' }, '+' + money(d))),
      el('td', { class: 'n' }, l.n_sheets), el('td', { class: 'n' }, money(l.material)),
      el('td', { class: 'n' }, money(l.labour)), el('td', { class: 'n' }, l.n_rips),
      el('td', { class: 'n' }, l.n_track_stops), el('td', { class: 'n' }, l.n_cross),
      el('td', { class: 'n' }, l.n_wide_cross), el('td', { class: 'n' }, l.n_trims),
      el('td', {}, el('button', { class: 'danger small', onclick: async () => {
        await api('/api/saved/' + l.id, { method: 'DELETE' }); renderSaved();
      } }, 'delete'))));
  }
  t.append(tb);
  host.append(el('div', { class: 'card' },
    el('h2', {}, 'Saved layouts, all repriced under the costs in the form'), t,
    el('div', { class: 'small muted', style: 'margin-top:8px' },
      'Click a name to open it. Repricing compares plans fairly under one cost ' +
      'model; it does not tell you the best plan for those costs — Solve does.')));
}

async function open_(id) {
  const j = await api('/api/layout/' + id);
  S.layout = j; S.ref = id;
  S.baseline = { dollars: j.score.dollars, name: 'saved “' + (j.name || id) + '”' };
  render(); tab('layout'); toast('opened ' + (j.name || id));
}

// ---------- tabs / boot ----------
function tab(name) {
  for (const b of document.querySelectorAll('.tabs button'))
    b.classList.toggle('on', b.dataset.t === name);
  for (const p of document.querySelectorAll('.pane'))
    p.classList.toggle('on', p.id === 'p-' + name);
  if (name === 'saved') renderSaved();
}
for (const b of document.querySelectorAll('.tabs button'))
  b.addEventListener('click', () => tab(b.dataset.t));

$('#cfgsave').addEventListener('click', async () => {
  try { S.cfg = await api('/api/config', { method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(readConfigForm()) }); toast('costs saved to config.json'); }
  catch (e) { toast('save failed: ' + e.message, 5000); }
});
$('#cfgreset').addEventListener('click', async () => {
  S.cfg = await api('/api/config'); buildConfigForm();
  if (S.ref) reprice(); toast('costs reloaded');
});

(async function boot() {
  $('#lanhint').textContent = location.host;
  S.cfg = await api('/api/config');
  buildConfigForm();
  $('#solve').disabled = true;
  await loadRecent();
})();
