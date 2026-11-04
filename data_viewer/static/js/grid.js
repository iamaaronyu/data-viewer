// Data grid, gallery and the row-detail drawer.
import { NUMERIC, cellUrl, findMedia, mediaSrc, renderCell, renderFull } from './cells.js';
import { clear, copyText, el, fmtInt, icon, mimeCategory } from './util.js';

export class DataTable {
  constructor(container, { onRowClick, onSort } = {}) {
    this.container = container;
    this.onRowClick = onRowClick;
    this.onSort = onSort;
    this.wrap = false;
    this.page = null;
  }

  render(page, { sort = [], rowBase = 0, selected = null, empty = '没有数据' } = {}) {
    this.page = page;
    const cols = page.columns;
    const head = el('tr', null, el('th', { class: 'rn', scope: 'col' }, '#'), cols.map((c) => this.header(c, sort)));
    const tbody = el('tbody');
    page.rows.forEach((r, i) => {
      const tr = el('tr', { dataset: { i }, class: selected === i ? 'selected' : null });
      tr.append(el('td', { class: 'rn' }, String(rowBase + i)));
      r.forEach((v, j) => {
        const col = cols[j];
        tr.append(el('td', { class: NUMERIC.has(col.kind) ? 'num' : null }, renderCell(v, { page, row: i, col })));
      });
      tbody.append(tr);
    });
    tbody.addEventListener('click', (e) => {
      if (e.target.closest('button, a, audio, video, input')) return;
      const tr = e.target.closest('tr');
      if (tr) this.onRowClick?.(Number(tr.dataset.i));
    });
    const table = el('table', { class: `grid${this.wrap ? ' wrap' : ''}` }, el('thead', null, head), tbody);
    clear(this.container).append(table);
    if (!page.rows.length) this.container.append(el('div', { class: 'grid-empty' }, empty));
  }

  header(c, sort) {
    const s = sort.find((x) => x.column === c.name);
    const title = [c.name, c.type, c.note, c.lazy ? 'SQL 中为占位值，预览由 __index__ 补上' : null].filter(Boolean).join('\n');
    const th = el('th', { class: this.onSort ? 'sortable' : null, title, scope: 'col', 'aria-sort': s ? (s.dir === 'desc' ? 'descending' : 'ascending') : null },
      el('div', { class: 'th-inner' },
        el('span', null, c.name),
        el('span', { class: 'ty' }, c.type),
        s ? el('span', { class: 'arrow' }, s.dir === 'desc' ? '↓' : '↑') : null));
    if (this.onSort) th.addEventListener('click', () => this.onSort(c.name));
    return th;
  }

  select(i) {
    this.container.querySelectorAll('tbody tr.selected').forEach((tr) => tr.classList.remove('selected'));
    const tr = this.container.querySelector(`tbody tr[data-i="${i}"]`);
    if (tr) {
      tr.classList.add('selected');
      tr.scrollIntoView({ block: 'nearest' });
    }
  }
}

// ---------------------------------------------------------------------------
// gallery
// ---------------------------------------------------------------------------
const CAPTION_NAMES = ['caption', 'txt', 'text', 'label_name', 'title', 'prompt', 'name', 'label', 'cls', '__key__'];

export function mediaColumns(page) {
  const out = [];
  page.columns.forEach((c, j) => {
    for (const r of page.rows.slice(0, 20)) {
      const v = r[j];
      if (v && typeof v === 'object' && findMedia(v)) {
        out.push(j);
        break;
      }
    }
  });
  return out;
}

export function renderGallery(container, page, { rowBase = 0, onClick, selected = null } = {}) {
  const media = mediaColumns(page);
  clear(container);
  if (!media.length) {
    container.append(el('div', { class: 'grid-empty' }, '这一页没有可预览的图像列'));
    return;
  }
  const mj = media[0];
  const capIdx = (() => {
    for (const name of CAPTION_NAMES) {
      const j = page.columns.findIndex((c) => c.name === name);
      if (j >= 0 && j !== mj) return j;
    }
    return page.columns.findIndex((c, j) => j !== mj && (c.kind === 'string' || c.kind === 'int'));
  })();
  const grid = el('div', { class: 'gallery' });
  page.rows.forEach((r, i) => {
    const ctx = { page, row: i, col: page.columns[mj] };
    const m = findMedia(r[mj]);
    let mediaNode;
    if (m) {
      const src = m.$t === 'nd' ? cellUrl(page, i, page.columns[mj].name, { format: 'png', thumb: 320 }) : mediaSrc(m, ctx, 320);
      mediaNode = el('img', { src, alt: '', loading: 'lazy', decoding: 'async' });
      mediaNode.addEventListener('load', () => {
        if (mediaNode.naturalWidth <= 96) mediaNode.style.imageRendering = 'pixelated';
      });
    } else {
      mediaNode = el('span', { class: 'v-null' }, 'null');
    }
    let cap = '';
    if (capIdx >= 0) {
      const v = r[capIdx];
      cap = v === null || v === undefined ? '' : typeof v === 'object' ? (v.v ?? '') : String(v);
    }
    const card = el('div', { class: `card${selected === i ? ' selected' : ''}`, tabindex: 0, dataset: { i } },
      el('div', { class: 'media' }, mediaNode),
      el('div', { class: 'cap' }, el('span', { class: 'idx' }, String(rowBase + i)), el('span', { class: 'txt', title: cap }, cap)));
    card.addEventListener('click', () => onClick?.(i));
    card.addEventListener('keydown', (e) => { if (e.key === 'Enter') onClick?.(i); });
    grid.append(card);
  });
  container.append(grid);
}

// ---------------------------------------------------------------------------
// row detail drawer
// ---------------------------------------------------------------------------
export class Drawer {
  constructor(node, { onNavigate } = {}) {
    this.node = node;
    this.onNavigate = onNavigate;
    this.page = null;
    this.index = null;
    document.addEventListener('keydown', (e) => {
      if (this.node.hidden) return;
      const tag = (e.target && e.target.tagName) || '';
      if (['INPUT', 'TEXTAREA', 'SELECT'].includes(tag)) return;
      if (e.key === 'Escape') this.hide();
      if (e.key === 'ArrowDown' || e.key === 'j') { e.preventDefault(); this.step(1); }
      if (e.key === 'ArrowUp' || e.key === 'k') { e.preventDefault(); this.step(-1); }
    });
  }

  show(page, index, rowBase = 0) {
    this.page = page;
    this.index = index;
    this.rowBase = rowBase;
    const row = page.rows[index];
    const prev = el('button', { class: 'icon-btn', title: '上一行（↑）', 'aria-label': '上一行', disabled: index <= 0 }, icon('prev'));
    const next = el('button', { class: 'icon-btn', title: '下一行（↓）', 'aria-label': '下一行', disabled: index >= page.rows.length - 1 }, icon('next'));
    const close = el('button', { class: 'icon-btn', title: '关闭（Esc）', 'aria-label': '关闭' }, icon('close'));
    prev.addEventListener('click', () => this.step(-1));
    next.addEventListener('click', () => this.step(1));
    close.addEventListener('click', () => this.hide());
    const body = el('div', { class: 'drawer-body' });
    page.columns.forEach((col, j) => {
      const v = row[j];
      const ctx = { page, row: index, col };
      const tools = el('div', { class: 'tools' });
      const plain = plainText(v);
      if (plain !== null) {
        const copy = el('button', { class: 'icon-btn', title: '复制值', 'aria-label': `复制 ${col.name}` }, icon('copy', 14));
        copy.addEventListener('click', () => copyText(plain));
        tools.append(copy);
      }
      body.append(el('section', { class: 'field' },
        el('div', { class: 'field-head' }, el('span', { class: 'fname' }, col.name), el('span', { class: 'ftype' }, col.type), tools),
        renderFull(v, ctx)));
    });
    this.node.replaceChildren(
      el('div', { class: 'drawer-head' }, el('h2', null, `第 ${fmtInt(rowBase + index)} 行`), prev, next, close),
      body,
    );
    this.node.hidden = false;
    this.onNavigate?.(index);
  }

  step(d) {
    if (!this.page) return;
    const i = this.index + d;
    if (i < 0 || i >= this.page.rows.length) return;
    this.show(this.page, i, this.rowBase);
  }

  hide() {
    this.node.hidden = true;
    this.page = null;
    this.onNavigate?.(null);
  }
}

function plainText(v) {
  if (v === null || v === undefined) return null;
  if (typeof v !== 'object') return String(v);
  if (v.$t === 'str') return null; // truncated: copy from the full text instead
  if (v.$t === 'path') return v.v;
  if (v.$t) return null;
  return JSON.stringify(v, null, 2);
}

export { mimeCategory };
