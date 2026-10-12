/* ProjectorCal — calibration report charts (plain SVG, no libraries).
 *
 * renderReport(container, chart) draws the summary tiles and the charts for one
 * saved report's chart data (GET /api/reports/{file} → .chart, see reports.py):
 * CIE 1931 gamut, grey RGB balance, gamma, luminance tracking, ΔE per patch and
 * a measurement table. Marks carry data-tip; one delegated listener shows it.
 */

'use strict';

// CIE 1931 2° spectral locus, 380–700 nm (xy)
const CIE_LOCUS = [
  [0.17411, 0.00496], [0.1738, 0.00492], [0.17334, 0.0048], [0.17258, 0.0048], [0.17141, 0.0051],
  [0.16888, 0.0069], [0.16441, 0.01086], [0.15664, 0.0177], [0.14396, 0.0297], [0.12412, 0.0578],
  [0.09129, 0.1327], [0.04539, 0.29498], [0.00817, 0.53842], [0.01387, 0.75019], [0.0743, 0.8338],
  [0.15472, 0.80586], [0.22962, 0.75433], [0.3016, 0.69231], [0.3731, 0.62445], [0.44406, 0.55471],
  [0.51249, 0.48659], [0.57515, 0.42423], [0.62704, 0.37249], [0.66576, 0.33401], [0.6915, 0.30834],
  [0.70792, 0.29203], [0.71903, 0.28093], [0.72599, 0.27401], [0.72997, 0.27003], [0.73199, 0.26801],
  [0.73342, 0.26658], [0.73439, 0.26561], [0.73469, 0.26531],
];

const CH = {
  // Channel identity is literal here: red, green and blue mean the projector's channels
  red: '#ff6b6b', green: '#4cd97b', blue: '#5b9bff',
  grid: '#2a2d3e', axis: '#8890a8', target: '#e2e4ef', surface: '#1a1d27',
};

const SVG_NS = 'http://www.w3.org/2000/svg';

function svgEl(tag, attrs = {}, parent = null) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v !== null && v !== undefined) el.setAttribute(k, v);
  }
  if (parent) parent.appendChild(el);
  return el;
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

function fmt(v, d = 2) {
  return v === null || v === undefined ? '—' : Number(v).toFixed(d);
}

function patchFill(row) {
  return '#' + row.rgb.map(c => c.toString(16).padStart(2, '0')).join('');
}

// Mark colour: the patch's own colour, but dark greys lifted so they stay
// visible on the dark surface (the table swatch keeps the true colour)
function markFill(row) {
  if (row.group !== 'grey') return patchFill(row);
  const v = Math.max(row.rgb[0], 80).toString(16).padStart(2, '0');
  return '#' + v.repeat(3);
}

function patchLabel(name) {
  if (name.startsWith('grey')) return `${name.slice(4)}% grey`;
  const [c, s] = name.split('_sat');
  return s ? `${c} ${s}%` : name;
}

// ---- Plot frame -------------------------------------------------------------

/** An SVG with a margin box, linear scales and recessive grid/ticks. */
function plotFrame(parent, { width = 520, height = 300, x, y, xTicks, yTicks, xLabel, yLabel,
                             xFmt = v => v, yFmt = v => v, margin = {} }) {
  const m = { top: 12, right: 14, bottom: 40, left: 48, ...margin };
  const svg = svgEl('svg', { viewBox: `0 0 ${width} ${height}`, class: 'chart-svg', role: 'img' }, parent);
  const iw = width - m.left - m.right, ih = height - m.top - m.bottom;
  const sx = v => m.left + (v - x[0]) / (x[1] - x[0]) * iw;
  const sy = v => m.top + ih - (v - y[0]) / (y[1] - y[0]) * ih;
  const g = svgEl('g', { class: 'gridlines' }, svg);
  for (const t of yTicks) {
    svgEl('line', { x1: m.left, x2: m.left + iw, y1: sy(t), y2: sy(t) }, g);
    svgEl('text', { x: m.left - 6, y: sy(t) + 4, 'text-anchor': 'end' }, g).textContent = yFmt(t);
  }
  for (const t of xTicks) {
    svgEl('line', { x1: sx(t), x2: sx(t), y1: m.top, y2: m.top + ih, class: 'v' }, g);
    svgEl('text', { x: sx(t), y: m.top + ih + 16, 'text-anchor': 'middle' }, g).textContent = xFmt(t);
  }
  if (xLabel) {
    svgEl('text', { x: m.left + iw / 2, y: height - 4, 'text-anchor': 'middle', class: 'axis-label' }, svg)
      .textContent = xLabel;
  }
  if (yLabel) {
    svgEl('text', { x: 12, y: m.top + ih / 2, 'text-anchor': 'middle', class: 'axis-label',
                    transform: `rotate(-90 12 ${m.top + ih / 2})` }, svg).textContent = yLabel;
  }
  return { svg, sx, sy, m, iw, ih };
}

function polyline(parent, pts, attrs) {
  return svgEl('polyline', { points: pts.map(p => p.join(',')).join(' '), fill: 'none', ...attrs }, parent);
}

function chartCard(container, title, subtitle, legend = []) {
  const card = document.createElement('div');
  card.className = 'chart-card';
  card.innerHTML = `
    <div class="chart-head">
      <div><div class="chart-title">${esc(title)}</div>
      ${subtitle ? `<div class="chart-sub">${subtitle}</div>` : ''}</div>
      ${legend.length ? `<div class="chart-legend">${legend.map(l =>
        `<span><i style="background:${l.color}${l.dashed ? ';height:0;border-top:2px dashed ' + l.color : ''}"></i>${esc(l.label)}</span>`
      ).join('')}</div>` : ''}
    </div>`;
  container.appendChild(card);
  return card;
}

// ---- Charts -----------------------------------------------------------------

function drawGamut(container, chart) {
  const hdr = chart.mode === 'hdr10';
  const card = chartCard(container, 'Color gamut — CIE 1931',
    `${hdr ? 'P3-D65' : 'Rec.709'} targets (rings) vs measured (dots), all saturation levels`,
    [{ label: 'Target gamut', color: CH.target, dashed: true }]);
  const f = plotFrame(card, {
    width: 520, height: 520, x: [0, 0.8], y: [0, 0.9],
    xTicks: [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8], yTicks: [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9],
    xLabel: 'x', yLabel: 'y', xFmt: v => v.toFixed(1), yFmt: v => v.toFixed(1),
  });
  const locus = [...CIE_LOCUS, CIE_LOCUS[0]].map(([x, y]) => [f.sx(x), f.sy(y)]);
  polyline(f.svg, locus, { class: 'locus' });

  const rows = chart.rows.filter(r => r.group !== 'grey');
  const prim = ['red', 'green', 'blue'].map(c => rows.find(r => r.patch === c)).filter(Boolean);
  if (prim.length === 3) {
    polyline(f.svg, [...prim, prim[0]].map(r => [f.sx(r.target_x), f.sy(r.target_y)]),
      { class: 'target-gamut' });
  }
  // Errors first (thin connectors), then target rings, then measured dots on top
  const marks = svgEl('g', {}, f.svg);
  for (const r of rows) {
    svgEl('line', { x1: f.sx(r.target_x), y1: f.sy(r.target_y), x2: f.sx(r.x), y2: f.sy(r.y),
                    class: 'err-line' }, marks);
  }
  for (const r of rows) {
    svgEl('circle', { cx: f.sx(r.target_x), cy: f.sy(r.target_y), r: 5, class: 'target-ring' }, marks);
  }
  const white = chart.rows.find(r => r.patch === 'white');
  for (const r of [...rows, ...(white ? [white] : [])]) {
    svgEl('circle', {
      cx: f.sx(r.x), cy: f.sy(r.y), r: r.group === 'sat100' || r.patch === 'white' ? 5 : 4,
      fill: markFill(r), class: 'dot',
      'data-tip': `<b>${esc(patchLabel(r.patch))}</b><br>x ${fmt(r.x, 4)} y ${fmt(r.y, 4)}<br>` +
                  `target ${fmt(r.target_x, 4)}, ${fmt(r.target_y, 4)}<br>ΔE ${fmt(r.delta_e)}`,
    }, marks);
  }
}

function greyRows(chart) {
  return chart.rows.filter(r => r.group === 'grey').sort((a, b) => a.stimulus_pct - b.stimulus_pct);
}

function drawRgbBalance(container, chart) {
  const rows = greyRows(chart);
  const card = chartCard(container, 'RGB balance',
    'Each channel relative to luminance on the grey ramp — 100 % is neutral D65',
    [{ label: 'Red', color: CH.red }, { label: 'Green', color: CH.green }, { label: 'Blue', color: CH.blue }]);
  const dev = Math.max(5, ...rows.flatMap(r => [r.red_pct, r.green_pct, r.blue_pct].map(v => Math.abs(v - 100))));
  const span = Math.min(30, Math.ceil(dev / 5) * 5);
  const step = span <= 10 ? 2 : 5;
  const yTicks = [];
  for (let t = 100 - span; t <= 100 + span; t += step) yTicks.push(t);
  const f = plotFrame(card, {
    x: [0, 100], y: [100 - span, 100 + span], xTicks: [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100],
    yTicks, xLabel: 'Stimulus (%)', yLabel: 'Channel (%)',
  });
  svgEl('line', { x1: f.sx(0), x2: f.sx(100), y1: f.sy(100), y2: f.sy(100), class: 'ref-line' }, f.svg);
  const clamp = v => Math.max(100 - span, Math.min(100 + span, v));
  for (const ch of ['red', 'green', 'blue']) {
    const key = ch + '_pct';
    polyline(f.svg, rows.map(r => [f.sx(r.stimulus_pct), f.sy(clamp(r[key]))]),
      { stroke: CH[ch], 'stroke-width': 2 });
    for (const r of rows) {
      const off = r[key] !== clamp(r[key]);
      svgEl(off ? 'path' : 'circle', off ? {
        // Off-scale: an arrow at the edge pointing the way the value went
        d: r[key] > 100 ? `M${f.sx(r.stimulus_pct) - 5},${f.sy(100 + span) + 7}l5,-7l5,7z`
                        : `M${f.sx(r.stimulus_pct) - 5},${f.sy(100 - span) - 7}l5,7l5,-7z`,
        fill: CH[ch],
      } : { cx: f.sx(r.stimulus_pct), cy: f.sy(r[key]), r: 3.5, fill: CH[ch], class: 'dot' }, f.svg)
        .setAttribute('data-tip', `<b>${esc(patchLabel(r.patch))}</b><br>R ${fmt(r.red_pct, 1)} %<br>` +
                      `G ${fmt(r.green_pct, 1)} %<br>B ${fmt(r.blue_pct, 1)} %<br>ΔE ${fmt(r.delta_e)}`);
    }
  }
}

function drawGamma(container, chart) {
  const hdr = chart.mode === 'hdr10';
  const rows = greyRows(chart).filter(r => r.point_gamma !== null);
  const card = chartCard(container, hdr ? 'Tone tracking' : 'Gamma',
    hdr ? 'Effective gamma of each grey against the 2.2-coded HDR stimulus (white = 100 nits)'
        : `Point gamma per grey level, target ${chart.target_gamma}`,
    [{ label: 'Measured', color: CH.target }, { label: 'Target', color: CH.axis, dashed: true }]);
  const vals = rows.map(r => r.point_gamma);
  const lo = Math.min(1.8, Math.floor(Math.min(...vals, chart.target_gamma) * 10) / 10 - 0.1);
  const hi = Math.max(2.6, Math.ceil(Math.max(...vals, chart.target_gamma) * 10) / 10 + 0.1);
  const yTicks = [];
  for (let t = Math.ceil(lo * 10); t <= Math.floor(hi * 10); t += (hi - lo > 1.2 ? 2 : 1)) yTicks.push(t / 10);
  const f = plotFrame(card, {
    x: [0, 100], y: [lo, hi], xTicks: [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100],
    yTicks, xLabel: 'Stimulus (%)', yLabel: 'Gamma', yFmt: v => v.toFixed(1),
  });
  svgEl('line', { x1: f.sx(0), x2: f.sx(100), y1: f.sy(chart.target_gamma), y2: f.sy(chart.target_gamma),
                  class: 'target-line' }, f.svg);
  polyline(f.svg, rows.map(r => [f.sx(r.stimulus_pct), f.sy(r.point_gamma)]),
    { stroke: CH.target, 'stroke-width': 2 });
  for (const r of rows) {
    svgEl('circle', { cx: f.sx(r.stimulus_pct), cy: f.sy(r.point_gamma), r: 4, fill: markFill(r), class: 'dot',
      'data-tip': `<b>${esc(patchLabel(r.patch))}</b><br>gamma ${fmt(r.point_gamma)}<br>` +
                  `luminance ${fmt(r.luminance_pct, 2)} % (target ${fmt(r.target_luminance_pct, 2)} %)` }, f.svg);
  }
}

function drawLuminance(container, chart) {
  const hdr = chart.mode === 'hdr10';
  const rows = greyRows(chart);
  const card = chartCard(container, 'Luminance tracking',
    hdr ? 'Grey luminance relative to 100-nit white vs the PQ-encoded target'
        : 'Grey luminance relative to white vs the target curve',
    [{ label: 'Measured', color: CH.target }, { label: 'Target', color: CH.axis, dashed: true }]);
  const f = plotFrame(card, {
    x: [0, 100], y: [0, 100], xTicks: [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100],
    yTicks: [0, 20, 40, 60, 80, 100], xLabel: 'Stimulus (%)', yLabel: 'Luminance (% of white)',
  });
  const pts = [];
  for (let s = 0; s <= 100; s += 2) pts.push([f.sx(s), f.sy(100 * (s / 100) ** chart.target_gamma)]);
  polyline(f.svg, pts, { class: 'target-line' });
  polyline(f.svg, rows.map(r => [f.sx(r.stimulus_pct), f.sy(r.luminance_pct)]),
    { stroke: CH.target, 'stroke-width': 2 });
  for (const r of rows) {
    const err = r.target_luminance_pct > 0 ? (r.luminance_pct / r.target_luminance_pct - 1) * 100 : 0;
    svgEl('circle', { cx: f.sx(r.stimulus_pct), cy: f.sy(r.luminance_pct), r: 4, fill: markFill(r), class: 'dot',
      'data-tip': `<b>${esc(patchLabel(r.patch))}</b><br>${fmt(r.luminance_pct, 2)} % ` +
                  `(target ${fmt(r.target_luminance_pct, 2)} %, ${err >= 0 ? '+' : ''}${err.toFixed(1)} %)<br>` +
                  `${fmt(r.luminance_cdm2, 3)} cd/m²` }, f.svg);
  }
}

const DE_ORDER_GROUPS = ['grey', 'sat100', 'sat75', 'sat50', 'sat25'];
const GROUP_LABELS = { grey: 'Greys', sat100: '100 %', sat75: '75 %', sat50: '50 %', sat25: '25 %' };

function drawDeltaE(container, chart) {
  const rows = [];
  for (const g of DE_ORDER_GROUPS) {
    const rs = chart.rows.filter(r => r.group === g);
    if (g === 'grey') rs.sort((a, b) => b.stimulus_pct - a.stimulus_pct);
    rows.push(...rs.map(r => ({ ...r, first: rs[0] === r })));
  }
  const card = chartCard(container, 'ΔE 2000 per patch',
    'Under 1 is invisible, under 3 is hard to see on real content', []);
  card.classList.add('wide');
  const maxDe = Math.max(4, Math.ceil(Math.max(...rows.map(r => r.delta_e))));
  const yTicks = [];
  for (let t = 0; t <= maxDe; t += maxDe > 8 ? 2 : 1) yTicks.push(t);
  const f = plotFrame(card, {
    width: 1080, height: 280, x: [0, rows.length], y: [0, maxDe], xTicks: [], yTicks, yLabel: 'ΔE',
    margin: { bottom: 64 },
  });
  for (const [v, cls] of [[1, 'de-line-1'], [3, 'de-line-3']]) {
    svgEl('line', { x1: f.sx(0), x2: f.sx(rows.length), y1: f.sy(v), y2: f.sy(v), class: cls }, f.svg);
  }
  const bw = f.iw / rows.length;
  rows.forEach((r, i) => {
    const x = f.sx(i) + 1, w = bw - 2, top = f.sy(r.delta_e), base = f.sy(0);
    const h = Math.max(0, base - top), rr = Math.min(4, w / 2, h);
    // Rounded top, square baseline
    svgEl('path', {
      d: `M${x},${base}V${top + rr}a${rr},${rr} 0 0 1 ${rr},${-rr}H${x + w - rr}a${rr},${rr} 0 0 1 ${rr},${rr}V${base}z`,
      fill: markFill(r), class: 'bar',
      'data-tip': `<b>${esc(patchLabel(r.patch))}</b><br>ΔE ${fmt(r.delta_e)}`,
    }, f.svg);
    const lbl = r.group === 'grey' ? (r.patch === 'white' ? 'W' : r.patch.slice(4))
                                   : r.color.slice(0, 1).toUpperCase();
    svgEl('text', { x: x + w / 2, y: base + 14, 'text-anchor': 'middle', class: 'bar-label' }, f.svg).textContent = lbl;
    if (r.first) {
      svgEl('text', { x: x, y: base + 34, class: 'group-label' }, f.svg).textContent = GROUP_LABELS[r.group];
      if (i > 0) svgEl('line', { x1: x - 1, x2: x - 1, y1: f.m.top, y2: base + 40, class: 'group-sep' }, f.svg);
    }
  });
}

function drawTable(container, chart) {
  const det = document.createElement('details');
  det.className = 'chart-card wide';
  det.innerHTML = `<summary class="chart-title">Measurements (${chart.rows.length} patches)</summary>
    <div class="table-scroll"><table class="measure-table">
      <thead><tr><th>Patch</th><th>x</th><th>y</th><th>Target x</th><th>Target y</th>
        <th>Y %</th><th>Target Y %</th><th>cd/m²</th><th>ΔE</th></tr></thead>
      <tbody>${chart.rows.map(r => `<tr>
        <td><i class="swatch" style="background:${patchFill(r)}"></i>${esc(patchLabel(r.patch))}</td>
        <td>${fmt(r.x, 4)}</td><td>${fmt(r.y, 4)}</td><td>${fmt(r.target_x, 4)}</td><td>${fmt(r.target_y, 4)}</td>
        <td>${fmt(r.luminance_pct, 2)}</td><td>${fmt(r.target_luminance_pct, 2)}</td>
        <td>${fmt(r.luminance_cdm2, 3)}</td><td class="${deClass(r.delta_e)}">${fmt(r.delta_e)}</td>
      </tr>`).join('')}</tbody></table></div>`;
  container.appendChild(det);
}

function renderTiles(container, chart) {
  const s = chart.summary, st = chart.settings;
  const tiles = [
    ['Mean ΔE', fmt(s.mean_de), `${s.patches} patches`, deClass(s.mean_de)],
    ['Max ΔE', fmt(s.max_de), s.worst_patch ? patchLabel(s.worst_patch) : '', deClass(s.max_de)],
    ['Under ΔE 1', `${s.under_1}/${s.patches}`, `${Math.round(100 * s.under_1 / (s.patches || 1))} %`, ''],
    ['White', `${fmt(chart.white_cdm2, 1)}`, chart.mode === 'hdr10' ? 'cd/m² at 100 nits' : 'cd/m²', ''],
    ['Picture', `${st.color_mode || '—'}`,
     `${st.lamp || '—'} lamp · ${st.dynamic_range || '—'} · B ${st.brightness ?? '—'} / C ${st.contrast ?? '—'}`, ''],
  ];
  const wrap = document.createElement('div');
  wrap.className = 'stat-tiles';
  wrap.innerHTML = tiles.map(([label, value, sub, cls]) => `
    <div class="stat-tile"><div class="stat-label">${esc(label)}</div>
      <div class="stat-value ${cls}">${esc(value)}</div><div class="stat-sub">${esc(sub)}</div></div>`).join('');
  container.appendChild(wrap);
  const g = s.group_mean_de || {};
  const groups = DE_ORDER_GROUPS.filter(k => g[k] !== undefined);
  if (groups.length) {
    const p = document.createElement('div');
    p.className = 'group-means text-dim';
    p.innerHTML = 'Mean ΔE — ' + groups.map(k =>
      `${GROUP_LABELS[k]}${k.startsWith('sat') ? ' colours' : ''}: <span class="${deClass(g[k])}">${fmt(g[k])}</span>`
    ).join(' · ') + (chart.ambient_compensation ? ' · room light subtracted' : '');
    container.appendChild(p);
  }
}

function renderReport(container, chart) {
  container.innerHTML = '';
  if (!chart.rows.length) {
    container.innerHTML = '<div class="text-dim">This report has no verification measurements.</div>';
    return;
  }
  renderTiles(container, chart);
  const grid = document.createElement('div');
  grid.className = 'chart-grid';
  container.appendChild(grid);
  drawGamut(grid, chart);
  drawRgbBalance(grid, chart);
  drawGamma(grid, chart);
  drawLuminance(grid, chart);
  drawDeltaE(grid, chart);
  drawTable(grid, chart);
}

// ---- Tooltip (one for all charts) --------------------------------------------

(function initChartTooltip() {
  const tip = document.createElement('div');
  tip.className = 'chart-tip';
  document.body.appendChild(tip);
  document.addEventListener('mouseover', e => {
    const el = e.target.closest && e.target.closest('[data-tip]');
    if (!el) { tip.style.display = 'none'; return; }
    tip.innerHTML = el.getAttribute('data-tip');
    tip.style.display = 'block';
  });
  document.addEventListener('mousemove', e => {
    if (tip.style.display !== 'block') return;
    const w = tip.offsetWidth;
    const left = e.clientX + 14 + w > window.innerWidth ? e.clientX - 14 - w : e.clientX + 14;
    tip.style.left = left + 'px';
    tip.style.top = (e.clientY + 14) + 'px';
  });
})();
