// "SQL" tab: free-form read-only DuckDB queries over the table `data`.
import { post } from './api.js';
import { DataTable, mediaColumns, renderGallery } from './grid.js';
import { clear, el, errorBox, fmtDuration, fmtInt, icon, noteBox } from './util.js';

const HISTORY_KEY = 'dv-sql-history';
const q = (name) => `"${String(name).replace(/"/g, '""')}"`;

function loadHistory() {
  try {
    const v = JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]');
    return Array.isArray(v) ? v.filter((x) => typeof x === 'string') : [];
  } catch (e) {
    return [];
  }
}

function saveHistory(sql) {
  try {
    const h = [sql, ...loadHistory().filter((x) => x !== sql)].slice(0, 30);
    localStorage.setItem(HISTORY_KEY, JSON.stringify(h));
  } catch (e) { /* ignore */ }
}

export function examplesFor(desc) {
  const cols = desc.columns;
  const visible = cols.filter((c) => !c.name.startsWith('__'));
  const str = visible.find((c) => c.kind === 'string');
  const num = visible.find((c) => ['int', 'float', 'decimal'].includes(c.kind));
  const vec = visible.find((c) => c.kind === 'vector');
  const bin = visible.find((c) => c.kind === 'binary' && !desc.lazy[c.name]);
  const json = visible.find((c) => c.kind === 'json');
  const struct = visible.find((c) => c.kind === 'struct' || c.kind === 'map');
  const list = visible.find((c) => c.kind === 'list');
  const ex = [
    { t: '前 100 行', sql: 'SELECT * FROM data LIMIT 100' },
    { t: '总行数', sql: 'SELECT count(*) AS rows FROM data' },
    { t: '列名与类型', sql: 'DESCRIBE data' },
  ];
  if (str) ex.push({ t: `按 ${str.name} 分组计数`, sql: `SELECT ${q(str.name)}, count(*) AS n\nFROM data\nGROUP BY 1\nORDER BY n DESC\nLIMIT 20` });
  if (num) ex.push({ t: `${num.name} 高于平均值的行`, sql: `SELECT *\nFROM data\nWHERE ${q(num.name)} > (SELECT avg(${q(num.name)}) FROM data)\nLIMIT 100` });
  if (num) ex.push({ t: `${num.name} 的分位数`, sql: `SELECT\n  min(${q(num.name)}) AS min,\n  quantile_cont(${q(num.name)}, [0.25, 0.5, 0.75]) AS quartiles,\n  max(${q(num.name)}) AS max\nFROM data` });
  if (vec) ex.push({ t: `与第 0 行最相似的 ${vec.name}（余弦）`, sql: `SELECT *,\n  array_cosine_similarity(${q(vec.name)}, (SELECT ${q(vec.name)} FROM data LIMIT 1)) AS similarity\nFROM data\nORDER BY similarity DESC\nLIMIT 20` });
  if (bin) ex.push({ t: `${bin.name} 最大的 20 个`, sql: `SELECT *, octet_length(${q(bin.name)}) AS bytes\nFROM data\nORDER BY bytes DESC\nLIMIT 20` });
  if (json) ex.push({ t: `解析 ${json.name} 字段`, sql: `SELECT __key__, json_keys(${q(json.name)}::JSON) AS keys, ${q(json.name)}::JSON AS value\nFROM data\nLIMIT 50` });
  if (struct) ex.push({ t: `展开 ${struct.name}`, sql: `SELECT ${q(struct.name)}.*\nFROM data\nLIMIT 100` });
  if (list) ex.push({ t: `展开列表 ${list.name}`, sql: `SELECT unnest(${q(list.name)}) AS item, count(*) AS n\nFROM data\nGROUP BY 1\nORDER BY n DESC\nLIMIT 50` });
  if (cols.some((c) => c.name === '__index__') || desc.format === 'hdf5' || desc.format === 'webdataset') {
    ex.push({ t: '按 __index__ 取行', sql: 'SELECT *\nFROM data\nWHERE __index__ BETWEEN 10 AND 19' });
  }
  ex.push({ t: '列统计汇总（DuckDB SUMMARIZE）', sql: 'SUMMARIZE data' });
  return ex;
}

export class SqlView {
  constructor(panel, app) {
    this.panel = panel;
    this.app = app;
    this.limit = 100;
    this.seq = 0;
    this.texts = new Map();
  }

  reset(desc) {
    this.desc = desc;
    this.page = null;
    this.offset = 0;
    this.total = null;
    this.built = false;
  }

  activate() {
    if (!this.built) this.build();
    this.editor.focus();
  }

  build() {
    this.built = true;
    const desc = this.desc;
    this.editor = el('textarea', { class: 'sql-editor', spellcheck: 'false', autocomplete: 'off', 'aria-label': 'SQL 查询' });
    this.editor.value = this.texts.get(desc.path) || 'SELECT * FROM data LIMIT 100';
    this.editor.addEventListener('input', () => this.texts.set(desc.path, this.editor.value));
    this.editor.addEventListener('keydown', (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
        e.preventDefault();
        this.run(0);
      } else if (e.key === 'Tab' && !e.shiftKey) {
        e.preventDefault();
        const { selectionStart: s, selectionEnd: t, value } = this.editor;
        this.editor.value = `${value.slice(0, s)}  ${value.slice(t)}`;
        this.editor.selectionStart = this.editor.selectionEnd = s + 2;
      }
    });
    const runBtn = el('button', { class: 'btn primary', type: 'button' }, icon('play', 12), '运行');
    runBtn.addEventListener('click', () => this.run(0));
    const exMenu = this.menu('示例', () => examplesFor(this.desc).map((x) => ({ t: x.t, q: x.sql })));
    const histMenu = this.menu('历史', () => {
      const h = loadHistory();
      return h.length ? h.map((s) => ({ t: s.split('\n')[0].slice(0, 80), q: s })) : [{ t: '还没有历史记录', q: null }];
    });
    const lazy = Object.entries(desc.lazy || {});
    const notes = el('div');
    if (lazy.length) {
      notes.append(noteBox(`在 SQL 中，这些列以占位值出现：${lazy.map(([k, v]) => `${k}（${v}）`).join('、')}。结果包含 __index__ 列时，表格会自动补上原始内容的预览。`, 'info'));
    }
    this.statusLine = el('div', { class: 'status-line' });
    this.errorSlot = el('div');
    this.gridWrap = el('div', { class: 'grid-wrap' });
    this.pager = el('div', { class: 'pager', hidden: true });
    this.table = new DataTable(this.gridWrap, { onRowClick: (i) => this.openRow(i) });
    this.panel.replaceChildren(
      el('div', { class: 'sql-top' }, this.editor,
        el('div', { class: 'sql-actions' }, runBtn, exMenu, histMenu,
          el('span', { class: 'hint' }, '表名是 ', el('code', null, 'data'), ' · 只读查询 · ', el('span', { class: 'kbd' }, 'Ctrl/⌘ + Enter'), ' 运行')),
        notes),
      this.statusLine, this.errorSlot, this.gridWrap, this.pager);
  }

  menu(label, itemsFn) {
    const btn = el('button', { class: 'btn', type: 'button', 'aria-haspopup': 'true' }, label, ' ▾');
    const list = el('div', { class: 'menu-list', hidden: true });
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const open = list.hidden;
      document.querySelectorAll('.menu-list').forEach((m) => { m.hidden = true; });
      if (!open) return;
      list.replaceChildren(...itemsFn().map((it) => {
        const b = el('button', { type: 'button', disabled: !it.q }, el('span', { class: 't' }, it.t), it.q ? el('span', { class: 'q' }, it.q.replace(/\s+/g, ' ')) : null);
        b.addEventListener('click', () => {
          list.hidden = true;
          if (!it.q) return;
          this.editor.value = it.q;
          this.texts.set(this.desc.path, it.q);
          this.run(0);
        });
        return b;
      }));
      list.hidden = false;
    });
    document.addEventListener('click', (e) => { if (!list.contains(e.target)) list.hidden = true; });
    return el('div', { class: 'menu' }, btn, list);
  }

  async run(offset) {
    const sql = this.editor.value.trim();
    if (!sql) return;
    const seq = ++this.seq;
    if (offset === 0) {
      this.total = null;
      saveHistory(sql);
    }
    this.offset = offset;
    this.sql = sql;
    this.gridWrap.classList.add('loading');
    this.statusLine.replaceChildren(el('span', { class: 'spinner', 'aria-hidden': 'true' }), '运行中…');
    this.app.drawer.hide();
    try {
      const page = await post(`/api/s/${this.desc.id}/sql`, { sql, offset, limit: this.limit });
      if (seq !== this.seq) return;
      this.page = page;
      clear(this.errorSlot);
      const hasMedia = mediaColumns(page).length > 0;
      if (this.gallery && hasMedia) renderGallery(this.gridWrap, page, { rowBase: offset, onClick: (i) => this.openRow(i) });
      else this.table.render(page, { rowBase: offset, empty: '查询结果为空' });
      this.renderStatus();
      this.renderPager();
      if (offset === 0) this.count(seq, sql);
    } catch (e) {
      if (seq !== this.seq) return;
      this.page = null;
      clear(this.gridWrap);
      this.statusLine.replaceChildren();
      this.pager.hidden = true;
      this.errorSlot.replaceChildren(el('div', { style: { padding: '10px 14px' } }, errorBox(e)));
    } finally {
      if (seq === this.seq) this.gridWrap.classList.remove('loading');
    }
  }

  async count(seq, sql) {
    try {
      const r = await post(`/api/s/${this.desc.id}/count`, { sql });
      if (seq !== this.seq) return;
      this.total = r.count;
      this.renderStatus();
      this.renderPager();
    } catch (e) { /* counting is best effort */ }
  }

  renderStatus() {
    const p = this.page;
    if (!p) return;
    const galleryBtn = el('button', { class: 'btn ghost small', type: 'button', disabled: !mediaColumns(p).length }, this.gallery ? '表格视图' : '画廊视图');
    galleryBtn.addEventListener('click', () => {
      this.gallery = !this.gallery;
      this.run(this.offset);
    });
    this.statusLine.replaceChildren(
      el('span', null, this.total !== null ? `共 ${fmtInt(this.total)} 行` : '计数中…'),
      el('span', { class: 'muted' }, `${p.columns.length} 列 · ${fmtDuration(p.elapsed)}`),
      el('span', { style: { marginLeft: 'auto' } }, galleryBtn));
  }

  renderPager() {
    const p = this.page;
    if (!p) return;
    this.pager.hidden = false;
    const n = p.rows.length;
    const btn = (name, label, enabled, fn) => {
      const b = el('button', { class: 'icon-btn', type: 'button', title: label, 'aria-label': label, disabled: !enabled }, icon(name));
      b.addEventListener('click', fn);
      return b;
    };
    const last = this.total !== null ? Math.max(0, Math.floor((this.total - 1) / this.limit) * this.limit) : null;
    this.pager.replaceChildren(
      btn('first', '第一页', this.offset > 0, () => this.run(0)),
      btn('prev', '上一页', this.offset > 0, () => this.run(Math.max(0, this.offset - this.limit))),
      el('span', { class: 'range' }, el('b', null, n ? `${fmtInt(this.offset + 1)}–${fmtInt(this.offset + n)}` : '0'), this.total !== null ? ` · 共 ${fmtInt(this.total)} 行` : ''),
      btn('next', '下一页', p.has_more, () => this.run(this.offset + this.limit)),
      btn('last', '最后一页', last !== null && last > this.offset, () => this.run(last)));
  }

  openRow(i) {
    if (!this.page) return;
    this.app.drawer.show(this.page, i, this.offset);
    this.table.select(i);
  }
}
