// "数据" tab: paged browsing with WHERE filter, sorting, column picker, table/gallery.
import { post } from './api.js';
import { DataTable, mediaColumns, renderGallery } from './grid.js';
import { clear, el, errorBox, fmtDuration, fmtInt, icon } from './util.js';

const PAGE_SIZES = [25, 50, 100, 200, 500];

function loadLimit() {
  try {
    const v = Number(localStorage.getItem('dv-limit'));
    return PAGE_SIZES.includes(v) ? v : 50;
  } catch (e) {
    return 50;
  }
}

export class DataView {
  constructor(panel, app) {
    this.panel = panel;
    this.app = app;
    this.limit = loadLimit();
    this.seq = 0;
    this.build();
  }

  build() {
    this.whereInput = el('input', {
      class: 'input', type: 'text', spellcheck: 'false', autocomplete: 'off', 'aria-label': '筛选条件（SQL WHERE 表达式）',
      placeholder: '例如：label = 3 AND name ILIKE \'%abc%\'（回车筛选）',
    });
    this.whereInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') this.applyWhere(); });
    const apply = el('button', { class: 'btn', type: 'button' }, '筛选');
    apply.addEventListener('click', () => this.applyWhere());
    this.clearBtn = el('button', { class: 'btn ghost', type: 'button', hidden: true }, '清除');
    this.clearBtn.addEventListener('click', () => {
      this.whereInput.value = '';
      this.where = '';
      this.order = [];
      this.offset = 0;
      this.filteredTotal = null;
      this.load();
    });
    this.sortChips = el('div', { style: { display: 'flex', gap: '4px', flexWrap: 'wrap' } });

    this.colsBtn = el('button', { class: 'btn', type: 'button', 'aria-haspopup': 'true' }, '列');
    this.colsList = el('div', { class: 'menu-list', hidden: true });
    this.colsBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      this.colsList.hidden = !this.colsList.hidden;
    });
    document.addEventListener('click', (e) => { if (!this.colsList.contains(e.target)) this.colsList.hidden = true; });
    const colsMenu = el('div', { class: 'menu' }, this.colsBtn, this.colsList);

    this.tableBtn = el('button', { type: 'button', 'aria-pressed': 'true' }, '表格');
    this.galleryBtn = el('button', { type: 'button', 'aria-pressed': 'false', title: '以图像网格显示' }, '画廊');
    this.tableBtn.addEventListener('click', () => this.setView('table'));
    this.galleryBtn.addEventListener('click', () => this.setView('gallery'));
    const seg = el('div', { class: 'seg', role: 'group', 'aria-label': '视图' }, this.tableBtn, this.galleryBtn);

    this.wrapBtn = el('button', { class: 'btn ghost', type: 'button', 'aria-pressed': 'false', title: '单元格内容换行显示' }, '换行');
    this.wrapBtn.addEventListener('click', () => {
      this.table.wrap = !this.table.wrap;
      this.wrapBtn.setAttribute('aria-pressed', String(this.table.wrap));
      this.wrapBtn.classList.toggle('primary', this.table.wrap);
      if (this.page) this.renderPage();
    });

    const toolbar = el('div', { class: 'toolbar' },
      el('div', { class: 'where-box' }, el('span', { class: 'lbl' }, 'WHERE'), this.whereInput),
      apply, this.clearBtn, this.sortChips, colsMenu, seg, this.wrapBtn);
    this.errorSlot = el('div');
    this.gridWrap = el('div', { class: 'grid-wrap', tabindex: '-1' });
    this.pager = el('div', { class: 'pager' });
    this.panel.replaceChildren(toolbar, this.errorSlot, this.gridWrap, this.pager);
    this.table = new DataTable(this.gridWrap, {
      onRowClick: (i) => this.openRow(i),
      onSort: (name) => this.toggleSort(name),
    });
  }

  reset(desc) {
    this.desc = desc;
    this.offset = 0;
    this.where = '';
    this.order = [];
    this.columns = null;
    this.view = 'table';
    this.page = null;
    this.filteredTotal = null;
    this.whereInput.value = '';
    this.tableBtn.setAttribute('aria-pressed', 'true');
    this.galleryBtn.setAttribute('aria-pressed', 'false');
    this.buildColumnMenu();
    clear(this.gridWrap);
    this.load();
  }

  get sid() {
    return this.desc.id;
  }

  buildColumnMenu() {
    const cols = this.desc.columns;
    const selected = new Set(this.columns || cols.map((c) => c.name));
    this.colsBtn.textContent = this.columns ? `列 ${this.columns.length}/${cols.length}` : `列 ${cols.length}`;
    const all = el('button', { type: 'button' }, el('span', { class: 't' }, '显示全部列'));
    all.addEventListener('click', () => {
      this.columns = null;
      this.buildColumnMenu();
      this.load();
    });
    const items = cols.map((c) => {
      const cb = el('input', { type: 'checkbox', checked: selected.has(c.name) });
      cb.addEventListener('change', () => {
        const next = cols.map((x) => x.name).filter((n) => (n === c.name ? cb.checked : selected.has(n)));
        if (!next.length) {
          cb.checked = true;
          return;
        }
        this.columns = next.length === cols.length ? null : next;
        this.order = this.order.filter((o) => !this.columns || this.columns.includes(o.column));
        this.buildColumnMenu();
        this.colsList.hidden = false;
        this.load();
      });
      return el('label', { style: { display: 'flex', gap: '8px', padding: '4px 8px', alignItems: 'center', cursor: 'pointer' } },
        cb, el('span', null, c.name), el('span', { class: 'muted mono', style: { marginLeft: 'auto', fontSize: '11px' } }, c.kind));
    });
    this.colsList.replaceChildren(all, ...items);
  }

  setView(view) {
    this.view = view;
    this.tableBtn.setAttribute('aria-pressed', String(view === 'table'));
    this.galleryBtn.setAttribute('aria-pressed', String(view === 'gallery'));
    if (this.page) this.renderPage();
  }

  applyWhere() {
    this.where = this.whereInput.value.trim();
    this.offset = 0;
    this.filteredTotal = null;
    this.load();
  }

  toggleSort(name) {
    const cur = this.order.find((o) => o.column === name);
    if (!cur) this.order = [{ column: name, dir: 'asc' }];
    else if (cur.dir === 'asc') this.order = [{ column: name, dir: 'desc' }];
    else this.order = [];
    this.offset = 0;
    this.load();
  }

  renderSortChips() {
    clear(this.sortChips);
    for (const o of this.order) {
      const x = el('button', { type: 'button', title: '取消排序', 'aria-label': `取消按 ${o.column} 排序` }, '×');
      x.addEventListener('click', () => {
        this.order = this.order.filter((y) => y !== o);
        this.offset = 0;
        this.load();
      });
      this.sortChips.append(el('span', { class: 'sort-chip' }, `${o.column} ${o.dir === 'desc' ? '↓' : '↑'}`, x));
    }
    this.clearBtn.hidden = !(this.where || this.order.length);
  }

  async load() {
    const seq = ++this.seq;
    this.renderSortChips();
    this.gridWrap.classList.add('loading');
    this.app.drawer.hide();
    const filtering = !!(this.where || this.order.length);
    try {
      const page = await post(`/api/s/${this.sid}/rows`, {
        offset: this.offset,
        limit: this.limit,
        columns: this.columns,
        where: this.where || null,
        order: this.order.length ? this.order : null,
      });
      if (seq !== this.seq) return;
      this.page = page;
      clear(this.errorSlot);
      this.renderPage();
      if (this.where && this.filteredTotal === null) this.countFiltered(seq);
    } catch (e) {
      if (seq !== this.seq) return;
      this.page = null;
      this.errorSlot.replaceChildren(el('div', { style: { padding: '10px 14px 0' } }, errorBox(e)));
      clear(this.gridWrap);
    } finally {
      if (seq === this.seq) this.gridWrap.classList.remove('loading');
    }
    if (seq === this.seq) {
      this.renderPager(filtering);
      this.app.onWarnings(this.page ? this.page.warnings : []);
    }
  }

  async countFiltered(seq) {
    try {
      const r = await post(`/api/s/${this.sid}/count`, { where: this.where });
      if (seq !== this.seq) return;
      this.filteredTotal = r.count;
      this.renderPager(true);
    } catch (e) {
      /* the page itself shows query errors */
    }
  }

  renderPage() {
    const page = this.page;
    const hasMedia = mediaColumns(page).length > 0;
    this.galleryBtn.disabled = !hasMedia;
    if (this.view === 'gallery' && hasMedia) {
      renderGallery(this.gridWrap, page, { rowBase: this.offset, onClick: (i) => this.openRow(i) });
    } else {
      this.table.render(page, {
        sort: this.order,
        rowBase: this.offset,
        empty: this.where ? '没有符合条件的行' : '没有数据',
      });
    }
  }

  openRow(i) {
    if (!this.page) return;
    this.app.drawer.show(this.page, i, this.offset);
    this.table.select(i);
    this.gridWrap.querySelectorAll('.card.selected').forEach((c) => c.classList.remove('selected'));
    const card = this.gridWrap.querySelector(`.card[data-i="${i}"]`);
    if (card) card.classList.add('selected');
  }

  total() {
    if (this.where) return this.filteredTotal;
    const c = this.app.count;
    return c && c.exact ? c.count : null;
  }

  renderPager() {
    const page = this.page;
    const n = page ? page.rows.length : 0;
    const total = this.total();
    const approx = !this.where && this.app.count && !this.app.count.exact ? this.app.count : null;
    const go = (offset) => {
      this.offset = Math.max(0, offset);
      this.load();
    };
    const btn = (name, label, enabled, fn) => {
      const b = el('button', { class: 'icon-btn', type: 'button', title: label, 'aria-label': label, disabled: !enabled }, icon(name));
      b.addEventListener('click', fn);
      return b;
    };
    const lastOffset = total !== null ? Math.max(0, Math.floor((total - 1) / this.limit) * this.limit) : null;
    const size = el('select', { class: 'select', 'aria-label': '每页行数' }, PAGE_SIZES.map((s) => el('option', { value: s, selected: s === this.limit }, `${s} 行/页`)));
    size.addEventListener('change', () => {
      this.limit = Number(size.value);
      try { localStorage.setItem('dv-limit', String(this.limit)); } catch (e) { /* ignore */ }
      this.offset = Math.floor(this.offset / this.limit) * this.limit;
      this.load();
    });
    const jump = el('input', { class: 'input', type: 'number', min: 0, placeholder: '跳到行', 'aria-label': '跳到行号' });
    jump.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter') return;
      const r = Number(jump.value);
      if (!Number.isFinite(r) || r < 0) return;
      go(Math.floor(r / this.limit) * this.limit);
    });
    let totalText = '共 … 行';
    if (total !== null) totalText = `共 ${fmtInt(total)} 行`;
    else if (approx && approx.count !== null && approx.count !== undefined) totalText = `已索引 ${fmtInt(approx.count)} 行${approx.progress ? `（${approx.progress}）` : ''}`;
    else if (this.where) totalText = '计数中…';
    const range = n ? `${fmtInt(this.offset + 1)}–${fmtInt(this.offset + n)}` : '0';
    this.pager.replaceChildren(
      btn('first', '第一页', this.offset > 0, () => go(0)),
      btn('prev', '上一页', this.offset > 0, () => go(this.offset - this.limit)),
      el('span', { class: 'range' }, el('b', null, range), ` · ${totalText}`),
      btn('next', '下一页', !!(page && page.has_more), () => go(this.offset + this.limit)),
      btn('last', '最后一页', lastOffset !== null && lastOffset > this.offset, () => go(lastOffset)),
      el('span', { class: 'spacer' }),
      page ? el('span', { class: 'elapsed' }, `${page.mode === 'query' ? 'DuckDB 查询' : '直接读取'} · ${fmtDuration(page.elapsed)}`) : null,
      jump,
      size,
    );
  }
}
