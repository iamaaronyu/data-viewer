// "统计" tab: per-column summary (one DuckDB pass) and per-column distributions.
import { post } from './api.js';
import { barList, histogram, histogramBlock } from './charts.js';
import { clear, el, errorBox, fmtBytes, fmtCompact, fmtInt, fmtNum, fmtPct, fmtTime, noteBox, spinner } from './util.js';

const SAMPLE = 100000;

export class StatsView {
  constructor(panel, app) {
    this.panel = panel;
    this.app = app;
    this.key = null;
    this.cache = new Map();
    this.seq = 0;
  }

  reset() {
    this.key = null;
    this.cache.clear();
    this.scope = 'all';
    this.sample = false;
    this.selected = null;
  }

  activate() {
    const where = this.app.dataView.where || '';
    if (this.scope === 'filter' && !where) this.scope = 'all';
    const key = this.currentKey();
    if (key !== this.key) this.load();
  }

  currentKey() {
    const where = this.scope === 'filter' ? this.app.dataView.where || '' : '';
    return JSON.stringify([this.app.source.id, where, this.sample]);
  }

  body() {
    const where = this.scope === 'filter' ? this.app.dataView.where || null : null;
    return { where, sample: this.sample ? SAMPLE : null };
  }

  toolbar() {
    const where = this.app.dataView.where || '';
    const scopeAll = el('button', { type: 'button', 'aria-pressed': String(this.scope === 'all') }, '全部数据');
    const scopeFilter = el('button', { type: 'button', 'aria-pressed': String(this.scope === 'filter'), disabled: !where, title: where ? `WHERE ${where}` : '在“数据”页设置筛选条件后可用' }, '当前筛选');
    scopeAll.addEventListener('click', () => { this.scope = 'all'; this.load(); });
    scopeFilter.addEventListener('click', () => { this.scope = 'filter'; this.load(); });
    const sAll = el('button', { type: 'button', 'aria-pressed': String(!this.sample) }, '全部行');
    const sPart = el('button', { type: 'button', 'aria-pressed': String(this.sample), title: '只用前 10 万行计算，适合很大的文本文件' }, `前 ${fmtCompact(SAMPLE)} 行`);
    sAll.addEventListener('click', () => { this.sample = false; this.load(); });
    sPart.addEventListener('click', () => { this.sample = true; this.load(); });
    const refresh = el('button', { class: 'btn', type: 'button' }, '重新计算');
    refresh.addEventListener('click', () => { this.cache.clear(); this.load(); });
    return el('div', { class: 'toolbar' },
      el('span', { class: 'secondary' }, '范围'), el('div', { class: 'seg' }, scopeAll, scopeFilter),
      el('span', { class: 'secondary' }, '行数'), el('div', { class: 'seg' }, sAll, sPart),
      refresh, this.status = el('span', { class: 'muted' }));
  }

  async load() {
    const seq = ++this.seq;
    this.key = this.currentKey();
    const content = el('div', { class: 'panel scroll' });
    this.panel.replaceChildren(this.toolbar(), content);
    content.append(spinner('正在计算统计…'));
    const t0 = performance.now();
    try {
      const res = await post(`/api/s/${this.app.source.id}/stats`, this.body());
      if (seq !== this.seq) return;
      this.summary = res;
      this.status.textContent = `${fmtInt(res.rows)} 行${res.sample ? '（采样）' : ''} · ${Math.round(performance.now() - t0)} ms`;
      this.render(content, res);
    } catch (e) {
      if (seq !== this.seq) return;
      content.replaceChildren(errorBox(e));
      if (String(e.message).includes('超时')) content.append(el('p', { class: 'muted' }, '可以切换到“前 10 万行”后重试。'));
    }
  }

  render(content, res) {
    const rows = res.rows || 0;
    const detail = el('div', { class: 'stats-detail' });
    const tbody = el('tbody');
    const minis = [];
    res.columns.forEach((c) => {
      const nonnull = c.nonnull ?? null;
      const meter = nonnull !== null && rows
        ? el('span', null, el('span', { class: 'meter', 'aria-hidden': 'true' }, el('i', { style: { width: `${(100 * nonnull) / rows}%` } })), el('span', { class: 'pct' }, fmtPct(nonnull, rows)))
        : el('span', { class: 'muted' }, '–');
      const mini = el('td', { style: { width: '128px', paddingTop: '8px' } });
      minis.push([c, mini]);
      const lo = rangeText(c, 'min');
      const hi = rangeText(c, 'max');
      const range = lo === '–' && hi === '–' ? '–' : `${lo} … ${hi}`;
      const tr = el('tr', { class: 'clickable', tabindex: 0, dataset: { name: c.name } },
        el('td', { style: { maxWidth: '200px' }, title: c.name },
          el('div', { style: { fontWeight: 600, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' } }, c.name),
          el('div', { class: 'muted mono', style: { fontSize: '11px' } }, c.kind)),
        el('td', { style: { whiteSpace: 'nowrap' } }, meter),
        el('td', { class: 'num', style: { whiteSpace: 'nowrap' } }, c.distinct !== undefined && c.distinct !== null ? `${res.distinct_exact ? '' : '≈ '}${fmtCompact(c.distinct)}` : '–'),
        el('td', { class: 'mono', style: { fontSize: '11.5px', maxWidth: '220px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }, title: range }, range),
        mini);
      const pick = () => this.select(c, tbody, detail);
      tr.addEventListener('click', pick);
      tr.addEventListener('keydown', (e) => { if (e.key === 'Enter') pick(); });
      tbody.append(tr);
    });
    const table = el('div', { class: 'tbl-wrap' }, el('table', { class: 'tbl' },
      el('thead', null, el('tr', null, el('th', null, '列'), el('th', null, '非空'), el('th', { class: 'num' }, '唯一值'),
        el('th', null, '范围'), el('th', null, '分布'))),
      tbody));
    content.replaceChildren(el('div', { class: 'stats-layout' }, table, detail));
    const first = res.columns.find((c) => c.name === this.selected) || res.columns.find((c) => !c.name.startsWith('__')) || res.columns[0];
    if (first) this.select(first, tbody, detail);
    this.loadMinis(minis);
  }

  async columnDetail(name) {
    const key = `${this.key}|${name}`;
    if (!this.cache.has(key)) {
      const p = post(`/api/s/${this.app.source.id}/stats/column`, { ...this.body(), column: name });
      this.cache.set(key, p);
      p.catch(() => this.cache.delete(key));
    }
    return this.cache.get(key);
  }

  async loadMinis(minis) {
    const seq = this.seq;
    const queue = minis.slice();
    const worker = async () => {
      while (queue.length) {
        const [c, cell] = queue.shift();
        if (seq !== this.seq) return;
        try {
          const d = await this.columnDetail(c.name);
          if (seq !== this.seq) return;
          const h = d.histogram || d.norms || d.lengths;
          if (h && h.counts && h.counts.length > 1) {
            cell.append(histogram(h, { width: 120, height: 26, compact: true, formatX: formatterFor(d), ariaLabel: `${c.name} 分布` }));
          } else if (d.top && d.top.length) {
            const t = d.top[0];
            const nonnull = c.nonnull || 1;
            cell.append(el('span', { class: 'muted', style: { fontSize: '11.5px' } }, `${t.value === null ? 'null' : String(t.value).slice(0, 14)} · ${fmtPct(t.count, Math.max(nonnull, t.count))}`));
          }
        } catch (e) {
          cell.append(el('span', { class: 'muted' }, '–'));
        }
      }
    };
    await Promise.all([worker(), worker()]);
  }

  async select(c, tbody, detail) {
    this.selected = c.name;
    tbody.querySelectorAll('tr').forEach((tr) => tr.classList.toggle('selected', tr.dataset.name === c.name));
    const seq = this.seq;
    detail.replaceChildren(el('h3', null, c.name), el('div', { class: 'sub' }, c.type), spinner('正在计算分布…'));
    try {
      const d = await this.columnDetail(c.name);
      if (seq !== this.seq || this.selected !== c.name) return;
      detail.replaceChildren(el('h3', null, c.name), el('div', { class: 'sub' }, c.type), ...this.detailBody(c, d, detail.clientWidth - 28));
    } catch (e) {
      if (this.selected === c.name) detail.replaceChildren(el('h3', null, c.name), errorBox(e));
    }
  }

  detailBody(c, d, width) {
    const rows = this.summary.rows || 0;
    const parts = [];
    const kv = el('dl', { class: 'kv' });
    const add = (k, v) => { if (v !== undefined && v !== null && v !== '') kv.append(el('dt', null, k), el('dd', null, v)); };
    add('行数', fmtInt(rows));
    add('空值', c.nulls !== undefined ? `${fmtInt(c.nulls)}（${fmtPct(c.nulls, rows)}）` : null);
    if (c.nan) add('NaN', fmtInt(c.nan));
    if (c.inf) add('±Inf', fmtInt(c.inf));
    if (c.distinct !== undefined) add(this.summary.distinct_exact ? '唯一值' : '唯一值（近似）', fmtInt(c.distinct));
    if (c.true !== undefined) add('true', `${fmtInt(c.true)}（${fmtPct(c.true, c.nonnull)}）`);
    const fx = formatterFor(d);
    if (['int', 'float', 'decimal'].includes(c.kind)) {
      add('最小', fmtNum(c.min));
      if (Array.isArray(c.q)) {
        add('下四分位', fmtNum(c.q[0]));
        add('中位数', fmtNum(c.q[1]));
        add('上四分位', fmtNum(c.q[2]));
      }
      add('最大', fmtNum(c.max));
      add('均值', fmtNum(c.mean));
      add('标准差', fmtNum(c.std));
    } else if (['timestamp', 'date', 'time'].includes(c.kind)) {
      add('最早', c.min);
      add('最晚', c.max);
    } else if (c.kind === 'string') {
      add('长度', c.len_min !== undefined ? `${fmtInt(c.len_min)} – ${fmtInt(c.len_max)}，平均 ${fmtNum(c.len_mean, 3)}` : null);
    } else if (c.kind === 'binary') {
      add('大小', c.size_min !== undefined ? `${fmtBytes(c.size_min)} – ${fmtBytes(c.size_max)}` : null);
      add('平均大小', c.size_mean !== undefined ? fmtBytes(Math.round(c.size_mean)) : null);
      add('总大小', c.size_sum !== undefined ? fmtBytes(c.size_sum) : null);
    } else if (['list', 'vector', 'tensor'].includes(c.kind)) {
      add('长度', c.len_min !== undefined ? `${fmtInt(c.len_min)} – ${fmtInt(c.len_max)}` : null);
    }
    parts.push(kv);
    const w = Math.max(280, width || 480);
    if (d.histogram) {
      parts.push(el('h4', null, d.temporal ? '时间分布' : '数值分布'), histogramBlock(d.histogram, { width: w, formatX: fx, ariaLabel: `${c.name} 数值分布` }));
    }
    if (d.norms) {
      parts.push(el('h4', null, '向量 L2 范数分布'), histogramBlock(d.norms, { width: w, formatX: formatterFor(d, d.norms), ariaLabel: `${c.name} 范数分布` }));
    }
    const continuous = ['float', 'decimal'].includes(c.kind) || (c.kind === 'int' && (c.distinct ?? 0) > 50);
    const informativeTop = d.top && d.top.length && d.top[0].count > 1 && !continuous;
    if (informativeTop && !(d.histogram && d.histogram.type === 'discrete' && d.top.length >= d.histogram.counts.length)) {
      parts.push(el('h4', null, '出现最多的值'), barList(d.top, { total: c.nonnull ?? rows, formatLabel: (v) => (d.temporal ? String(v) : String(v)) }));
    }
    if (d.mime) {
      parts.push(el('h4', null, `内容类型（抽样 ${fmtInt(d.mime_sampled)} 个）`), barList(d.mime, { total: d.mime_sampled, unit: '个' }));
    }
    if (d.lengths) {
      const title = c.kind === 'binary' ? '大小分布' : c.kind === 'string' ? '长度分布（字符）' : '长度分布';
      parts.push(el('h4', null, title), histogramBlock(d.lengths, {
        width: w, formatX: c.kind === 'binary' ? (v) => fmtBytes(Math.round(v)) : (v) => fmtNum(v, 4), ariaLabel: `${c.name} ${title}`,
      }));
    }
    if (c.error) parts.push(noteBox(c.error));
    if (parts.length === 1) parts.push(el('p', { class: 'muted' }, '这一列的类型只统计空值。'));
    return parts;
  }
}

// Enough significant digits to tell the ticks of a narrow range apart (e.g. 0.9995 … 1.0004).
function digitsFor(h) {
  if (!h || h.min === undefined || h.max === undefined) return 4;
  const span = h.max - h.min;
  const mag = Math.max(Math.abs(h.min), Math.abs(h.max));
  if (!(span > 0) || !(mag > 0)) return 4;
  return Math.min(12, Math.max(4, Math.ceil(Math.log10(mag / span)) + 2));
}

function formatterFor(d, h) {
  if (d.temporal) return (v) => fmtTime(v);
  if (d.kind === 'binary') return (v) => fmtBytes(Math.round(v));
  const digits = digitsFor(h || d.histogram || d.norms);
  return (v) => fmtNum(v, digits);
}

function rangeText(c, which) {
  if (['int', 'float', 'decimal'].includes(c.kind)) return fmtNum(c[which], 5);
  if (['timestamp', 'date', 'time'].includes(c.kind)) return c[which] ? String(c[which]).slice(0, 16) : '–';
  if (c.kind === 'string' || ['list', 'vector', 'tensor'].includes(c.kind)) {
    const v = c[`len_${which}`];
    return v === undefined || v === null ? '–' : `len ${fmtInt(v)}`;
  }
  if (c.kind === 'binary') {
    const v = c[`size_${which}`];
    return v === undefined || v === null ? '–' : fmtBytes(v);
  }
  return '–';
}


export { clear };
