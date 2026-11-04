// Small, dependency-free SVG charts following the house mark specs:
// one series colour, bars <= 24px with 4px rounded data-ends (square at the baseline),
// 2px surface gaps between touching bars, hairline grid, hover tooltips on every mark.
import { el, fmtCompact, fmtInt, fmtNum, fmtPct, svg, tip } from './util.js';

function niceMax(v) {
  if (v <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}

function barPath(x, y, w, h, base) {
  if (h <= 0) return '';
  const r = Math.min(4, w / 2, h);
  return `M${x},${base}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${base}Z`;
}

function tipNodes(value, label, extra) {
  return [
    el('div', { class: 'tv' }, value),
    el('div', { class: 'tl' }, label),
    extra ? el('div', { class: 'tl' }, extra) : null,
  ];
}

/**
 * Histogram from the backend shape:
 *   {type: 'bins', edges: [...n+1], counts: [...n]} or {type: 'discrete', values: [...], counts: [...]}
 */
export function histogram(hist, opts = {}) {
  const {
    width = 520, height = 170, compact = false,
    formatX = (v) => fmtNum(v, 4), unit = '行', ariaLabel = '分布直方图',
  } = opts;
  const counts = hist.counts || [];
  const n = counts.length;
  const total = counts.reduce((a, b) => a + b, 0);
  const m = compact ? { t: 2, r: 0, b: 1, l: 0 } : { t: 10, r: 8, b: 22, l: 44 };
  const iw = width - m.l - m.r;
  const ih = height - m.t - m.b;
  const base = m.t + ih;
  const ymax = compact ? Math.max(1, ...counts) : niceMax(Math.max(1, ...counts));
  const slot = n ? iw / n : iw;
  const bw = Math.max(1, Math.min(24, slot - (slot > 4 ? 2 : 0.5)));
  const root = svg('svg', {
    class: 'chart', viewBox: `0 0 ${width} ${height}`, width: compact ? width : '100%', height,
    preserveAspectRatio: compact ? 'none' : 'xMidYMid meet', role: 'img', 'aria-label': ariaLabel,
  });

  if (!compact) {
    for (const f of [0.5, 1]) {
      const y = base - f * ih;
      root.append(svg('line', { class: 'grid-line', x1: m.l, x2: width - m.r, y1: y, y2: y }));
      root.append(svg('text', { class: 'tick', x: m.l - 6, y: y + 3.5, 'text-anchor': 'end' }, fmtCompact(ymax * f)));
    }
    root.append(svg('text', { class: 'tick', x: m.l - 6, y: base + 3.5, 'text-anchor': 'end' }, '0'));
  }

  const bars = [];
  counts.forEach((c, i) => {
    const h = (c / ymax) * ih;
    const x = m.l + i * slot + (slot - bw) / 2;
    const path = svg('path', { class: 'bar', d: barPath(x, base - h, bw, h, base) });
    bars.push(path);
    root.append(path);
  });
  root.append(svg('line', { class: 'baseline', x1: m.l, x2: width - m.r, y1: base + 0.5, y2: base + 0.5 }));

  const label = (i) => {
    if (hist.type === 'discrete') return `值 = ${formatX(hist.values[i])}`;
    const a = hist.edges[i];
    const b = hist.edges[i + 1];
    return `${formatX(a)} ～ ${formatX(b)}${i === n - 1 ? '（含）' : ''}`;
  };

  // hit areas: the whole slot, full height (bigger than the mark)
  counts.forEach((c, i) => {
    const hit = svg('rect', { class: 'hit', x: m.l + i * slot, y: m.t, width: Math.max(slot, 1), height: ih + 2, tabindex: compact ? null : 0 });
    const enter = (evt) => {
      bars[i].classList.add('hover');
      tip.show(evt, ...tipNodes(`${fmtInt(c)} ${unit}`, label(i), total ? `占 ${fmtPct(c, total)}` : null));
    };
    hit.addEventListener('pointerenter', enter);
    hit.addEventListener('pointermove', (evt) => tip.move(evt));
    hit.addEventListener('pointerleave', () => { bars[i].classList.remove('hover'); tip.hide(); });
    hit.addEventListener('focus', () => {
      const r = hit.getBoundingClientRect();
      enter({ clientX: r.right, clientY: r.top });
    });
    hit.addEventListener('blur', () => { bars[i].classList.remove('hover'); tip.hide(); });
    root.append(hit);
  });

  if (!compact && n) {
    const ty = height - 6;
    if (hist.type === 'discrete') {
      const every = Math.ceil(n / Math.max(1, Math.floor(iw / 44)));
      hist.values.forEach((v, i) => {
        if (i % every !== 0 && i !== n - 1) return;
        root.append(svg('text', { class: 'tick', x: m.l + i * slot + slot / 2, y: ty, 'text-anchor': 'middle' }, formatX(v)));
      });
    } else {
      root.append(svg('text', { class: 'tick', x: m.l, y: ty, 'text-anchor': 'start' }, formatX(hist.edges[0])));
      root.append(svg('text', { class: 'tick', x: m.l + iw / 2, y: ty, 'text-anchor': 'middle' }, formatX(hist.edges[Math.floor(n / 2)])));
      root.append(svg('text', { class: 'tick', x: width - m.r, y: ty, 'text-anchor': 'end' }, formatX(hist.edges[n])));
    }
  }
  return root;
}

/** Table view of a histogram (accessibility / exact numbers). */
export function histogramTable(hist, { formatX = (v) => fmtNum(v, 6), unit = '行' } = {}) {
  const total = hist.counts.reduce((a, b) => a + b, 0);
  const rows = hist.counts.map((c, i) => {
    const label = hist.type === 'discrete' ? formatX(hist.values[i]) : `${formatX(hist.edges[i])} ～ ${formatX(hist.edges[i + 1])}`;
    return el('tr', null, el('td', { class: 'mono' }, label), el('td', { class: 'num' }, fmtInt(c)), el('td', { class: 'num' }, fmtPct(c, total)));
  });
  return el('div', { class: 'tbl-wrap', style: { maxHeight: '260px', overflow: 'auto' } },
    el('table', { class: 'tbl' }, el('thead', null, el('tr', null, el('th', null, hist.type === 'discrete' ? '值' : '区间'), el('th', { class: 'num' }, unit), el('th', { class: 'num' }, '占比'))), el('tbody', null, rows)));
}

/** Histogram card with a "table" toggle. */
export function histogramBlock(hist, opts = {}) {
  const box = el('div');
  let showTable = false;
  const toggle = el('button', { class: 'btn ghost small', type: 'button' }, '表格');
  const body = el('div');
  const draw = () => {
    body.replaceChildren(showTable ? histogramTable(hist, opts) : histogram(hist, opts));
    toggle.textContent = showTable ? '图表' : '表格';
  };
  toggle.addEventListener('click', () => { showTable = !showTable; draw(); });
  draw();
  box.append(el('div', { style: { display: 'flex', justifyContent: 'flex-end', marginTop: '-26px' } }, toggle), body);
  return box;
}

/** Horizontal bars for top values. items: [{value, count}] */
export function barList(items, { total = null, formatLabel = (v) => String(v), unit = '行' } = {}) {
  const max = Math.max(1, ...items.map((x) => x.count));
  const wrap = el('div', { class: 'barlist' });
  for (const it of items) {
    const isNull = it.value === null || it.value === undefined;
    const label = isNull ? 'null' : formatLabel(it.value);
    const pct = Math.max(0.4, (100 * it.count) / max);
    const row = el('div', { class: 'bl-row', tabindex: 0 },
      el('div', { class: `bl-label${isNull ? ' null' : ''}`, title: label }, label),
      el('div', { class: 'bl-track' },
        el('div', { class: 'bl-bar', style: { width: `calc(${pct}% - 70px)` } }),
        el('span', { class: 'bl-val' }, total ? `${fmtCompact(it.count)} · ${fmtPct(it.count, total)}` : fmtCompact(it.count))));
    const show = (evt) => tip.show(evt, ...tipNodes(`${fmtInt(it.count)} ${unit}`, label, total ? `占 ${fmtPct(it.count, total)}` : null));
    row.addEventListener('pointerenter', show);
    row.addEventListener('pointermove', (evt) => tip.move(evt));
    row.addEventListener('pointerleave', () => tip.hide());
    row.addEventListener('focus', () => { const r = row.getBoundingClientRect(); show({ clientX: r.right - 80, clientY: r.top }); });
    row.addEventListener('blur', () => tip.hide());
    wrap.append(row);
  }
  return wrap;
}

/** 2px line for a numeric vector, with a crosshair that snaps to the nearest index. */
export function sparkline(values, { width = 400, height = 80, label = '值', indexLabel = '索引' } = {}) {
  const n = values.length;
  const finite = values.filter(Number.isFinite);
  const lo = Math.min(0, ...finite);
  const hi = Math.max(0, ...finite);
  const span = hi - lo || 1;
  const m = { t: 6, r: 4, b: 6, l: 4 };
  const iw = width - m.l - m.r;
  const ih = height - m.t - m.b;
  const xs = (i) => m.l + (n <= 1 ? iw / 2 : (i / (n - 1)) * iw);
  const ys = (v) => m.t + (1 - (v - lo) / span) * ih;
  const root = svg('svg', { class: 'chart', viewBox: `0 0 ${width} ${height}`, width: '100%', height, preserveAspectRatio: 'none', role: 'img', 'aria-label': `${label}折线` });
  root.append(svg('line', { class: 'baseline', x1: m.l, x2: width - m.r, y1: ys(0), y2: ys(0) }));
  let d = '';
  values.forEach((v, i) => {
    if (!Number.isFinite(v)) return;
    d += `${d ? 'L' : 'M'}${xs(i).toFixed(2)},${ys(v).toFixed(2)}`;
  });
  root.append(svg('path', { class: 'line', d, 'vector-effect': 'non-scaling-stroke' }));
  const cross = svg('line', { class: 'cross', y1: m.t, y2: m.t + ih, x1: -10, x2: -10, 'vector-effect': 'non-scaling-stroke' });
  root.append(cross);
  const hit = svg('rect', { class: 'hit', x: 0, y: 0, width, height });
  hit.addEventListener('pointermove', (evt) => {
    const r = root.getBoundingClientRect();
    const fx = ((evt.clientX - r.left) / r.width) * width;
    const i = Math.max(0, Math.min(n - 1, Math.round(((fx - m.l) / iw) * (n - 1))));
    cross.setAttribute('x1', xs(i));
    cross.setAttribute('x2', xs(i));
    tip.show(evt, ...tipNodes(fmtNum(values[i], 6), `${indexLabel} ${i}`));
  });
  hit.addEventListener('pointerleave', () => { cross.setAttribute('x1', -10); cross.setAttribute('x2', -10); tip.hide(); });
  root.append(hit);
  return root;
}
