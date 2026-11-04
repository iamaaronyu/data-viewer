// Application shell: routing, source header, tabs, sidebar, preview, theme.
import { get, post } from './api.js';
import { FileBrowser, FORMAT_LABEL } from './browser.js';
import { DataView } from './dataview.js';
import { Drawer } from './grid.js';
import { InfoView, SchemaView } from './infoview.js';
import { SqlView } from './sqlview.js';
import { StatsView } from './statsview.js';
import { TrajectoryView } from './trajectoryview.js';
import { $, clear, el, errorBox, fmtBytes, fmtInt, noteBox, spinner, toast } from './util.js';

const FORMATS = [
  ['Lance', '.lance 目录：版本、标签、索引、向量列'],
  ['Parquet', '单文件或分区目录（hive 风格）'],
  ['CSV / TSV', '自动识别分隔符和列类型，支持 .gz'],
  ['JSON / JSONL', '数组、对象或逐行 JSON，嵌套字段可展开'],
  ['Arrow / Feather', 'IPC 文件、IPC 流、Feather V1/V2'],
  ['HDF5', '组和数据集按表格查看，图像数组直接预览'],
  ['WebDataset', 'tar 分片，同名文件组成一个样本'],
  ['RLDS / TFRecord', '机器人逐帧观测与动作，支持轨迹视图'],
];

class App {
  constructor() {
    this.source = null;
    this.count = null;
    this.tab = 'data';
    this.warnings = { source: [], page: [] };
    this.drawer = new Drawer($('#drawer'), { onNavigate: (i) => this.onDrawerRow(i) });
    this.dataView = new DataView($('#panel-data'), this);
    this.schemaView = new SchemaView($('#panel-schema'), this);
    this.statsView = new StatsView($('#panel-stats'), this);
    this.sqlView = new SqlView($('#panel-sql'), this);
    this.infoView = new InfoView($('#panel-info'), this);
    this.trajectoryView = new TrajectoryView($('#panel-trajectory'), this);
    this.browser = new FileBrowser({
      list: $('#fs-list'), filter: $('#fs-filter'), banner: $('#dir-banner'), foot: $('#side-foot'),
      onOpen: (path, format) => this.go({ open: path, format }),
      onNavigate: (path) => this.go({ dir: path }),
      onPreview: (entry) => this.go({ preview: entry.path }),
    });
    this.countTimer = null;
  }

  async init() {
    this.setupChrome();
    try {
      this.config = await get('/api/config');
    } catch (e) {
      $('#welcome').replaceChildren(errorBox(e));
      return;
    }
    $('#format-list').replaceChildren(...FORMATS.map(([t, d]) => el('li', null, el('b', null, t), el('span', null, d))));
    window.addEventListener('hashchange', () => this.route());
    if (!location.hash && this.config.initial) {
      this.go({ open: this.config.initial.path, format: this.config.initial.format }, true);
    } else {
      this.route();
    }
  }

  setupChrome() {
    $('#tabs').addEventListener('click', (e) => {
      const b = e.target.closest('button[data-tab]');
      if (b) this.setTab(b.dataset.tab, true);
    });
    $('#theme-toggle').addEventListener('click', () => {
      const root = document.documentElement;
      const order = ['system', 'light', 'dark'];
      const cur = root.dataset.theme || 'system';
      const next = order[(order.indexOf(cur) + 1) % order.length];
      if (next === 'system') delete root.dataset.theme;
      else root.dataset.theme = next;
      try { localStorage.setItem('dv-theme', next); } catch (e) { /* ignore */ }
      toast({ system: '主题：跟随系统', light: '主题：浅色', dark: '主题：深色' }[next], 1400);
    });
    $('#toggle-sidebar').addEventListener('click', () => $('#sidebar').classList.toggle('open'));
    $('#brand').addEventListener('click', (e) => {
      e.preventDefault();
      this.go({ dir: '' });
    });
    // resizable sidebar
    const resizer = $('#resizer');
    resizer.addEventListener('pointerdown', (e) => {
      resizer.setPointerCapture(e.pointerId);
      resizer.classList.add('active');
      const move = (ev) => {
        const w = Math.max(200, Math.min(560, ev.clientX));
        document.documentElement.style.setProperty('--sidebar-w', `${w}px`);
      };
      const up = () => {
        resizer.classList.remove('active');
        resizer.removeEventListener('pointermove', move);
        resizer.removeEventListener('pointerup', up);
      };
      resizer.addEventListener('pointermove', move);
      resizer.addEventListener('pointerup', up);
    });
  }

  // -- routing ---------------------------------------------------------------
  go(state, replace = false) {
    const p = new URLSearchParams();
    if (state.open !== undefined) {
      p.set('open', state.open);
      if (state.format) p.set('format', state.format);
      if (state.params && Object.keys(state.params).length) p.set('params', JSON.stringify(state.params));
      if (state.tab && state.tab !== 'data') p.set('tab', state.tab);
    } else if (state.preview !== undefined) {
      p.set('preview', state.preview);
    } else {
      p.set('dir', state.dir || '');
    }
    const hash = `#${p.toString()}`;
    if (hash === location.hash) {
      this.route();
      return;
    }
    if (replace) {
      history.replaceState(null, '', hash);
      this.route();
    } else {
      location.hash = hash;
    }
  }

  async route() {
    const p = new URLSearchParams(location.hash.slice(1));
    $('#sidebar').classList.remove('open');
    if (p.has('open')) {
      let params = {};
      try { params = JSON.parse(p.get('params') || '{}') || {}; } catch (e) { params = {}; }
      await this.openSource(p.get('open'), p.get('format') || null, params, p.get('tab') || 'data');
    } else if (p.has('preview')) {
      await this.preview(p.get('preview'));
    } else {
      await this.showDir(p.get('dir') || '');
    }
  }

  parentOf(path) {
    if (!path) return '';
    const i = path.lastIndexOf('/');
    return i < 0 ? '' : path.slice(0, i);
  }

  async showDir(path) {
    this.show('welcome');
    this.source = null;
    this.drawer.hide();
    try {
      await this.browser.load(path);
    } catch (e) {
      return;
    }
    this.browser.setActive(null);
    this.renderCrumbs(path, null);
    document.title = `${this.config.root_name}${path ? ` / ${path}` : ''} · Data Viewer`;
  }

  show(which) {
    this.trajectoryView.stop();
    $('#welcome').hidden = which !== 'welcome';
    $('#source').hidden = which !== 'source';
    $('#preview').hidden = which !== 'preview';
  }

  renderCrumbs(dir, leaf) {
    const crumbs = $('#crumbs');
    const parts = dir ? dir.split('/') : [];
    const nodes = [];
    const root = el('a', { href: '#dir=' }, this.config.root_name);
    nodes.push(root);
    let acc = '';
    parts.forEach((seg) => {
      acc = acc ? `${acc}/${seg}` : seg;
      nodes.push(el('span', { class: 'sep' }, '/'));
      nodes.push(el('a', { href: `#${new URLSearchParams({ dir: acc }).toString()}` }, seg));
    });
    if (leaf) {
      nodes.push(el('span', { class: 'sep' }, '/'));
      nodes.push(el('span', { class: 'current' }, leaf));
    } else if (nodes.length) {
      nodes[nodes.length - 1].classList.add('current');
    }
    crumbs.replaceChildren(...nodes);
  }

  // -- preview of plain files -----------------------------------------------
  async preview(path) {
    const dir = this.parentOf(path);
    this.show('preview');
    this.source = null;
    this.drawer.hide();
    this.browser.load(dir).then(() => this.browser.setActive(path)).catch(() => {});
    const name = path.split('/').pop();
    this.renderCrumbs(dir, name);
    document.title = `${name} · Data Viewer`;
    const box = $('#preview');
    const url = `/api/fs/raw?path=${encodeURIComponent(path)}`;
    const ext = name.toLowerCase().split('.').pop();
    const head = el('div', { class: 'preview-head' }, el('h1', null, name), el('a', { href: url, target: '_blank', rel: 'noopener' }, '在新标签页打开'));
    let body;
    if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg', 'avif', 'tif', 'tiff', 'ico'].includes(ext)) body = el('img', { src: url, alt: name });
    else if (['mp4', 'webm', 'mov', 'm4v', 'mkv', 'ogv'].includes(ext)) body = el('video', { src: url, controls: true });
    else if (['wav', 'mp3', 'flac', 'ogg', 'oga', 'opus', 'm4a', 'aac'].includes(ext)) body = el('audio', { src: url, controls: true });
    else if (ext === 'pdf') body = el('iframe', { src: url, title: name });
    else {
      body = spinner();
      get('/api/fs/text', { path }).then((r) => {
        body.replaceWith(el('pre', null, r.text + (r.truncated ? `\n\n…（文件共 ${fmtBytes(r.size)}，只显示开头部分）` : '')));
      }).catch(() => {
        body.replaceWith(noteBox('这个文件不是支持的数据格式，也不是可以预览的文本或媒体文件。'));
      });
    }
    box.replaceChildren(head, body);
  }

  // -- sources -----------------------------------------------------------------
  async openSource(path, format, params, tab) {
    const key = JSON.stringify([path, format, params]);
    if (this.source && this.sourceKey === key) {
      this.setTab(tab || 'data');
      return;
    }
    this.sourceKey = key;
    this.show('source');
    this.drawer.hide();
    clearTimeout(this.countTimer);
    const dir = this.parentOf(path);
    this.browser.load(dir).then(() => this.browser.setActive(path)).catch(() => {});
    this.renderCrumbs(dir, path.split('/').pop() || this.config.root_name);
    $('#src-name').textContent = path.split('/').pop() || this.config.root_name;
    $('#src-format').textContent = FORMAT_LABEL[format] || format || '';
    $('#src-meta').replaceChildren(spinner('正在打开…'));
    clear($('#src-warnings'));
    clear($('#src-switch'));
    this.trajectoryView.stop();
    document.querySelector('#tabs [data-tab="trajectory"]').hidden = true;
    ['data', 'trajectory', 'schema', 'stats', 'sql', 'info'].forEach((t) => { $(`#panel-${t}`).hidden = t !== 'data'; });
    document.querySelectorAll('#tabs button').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === 'data')));
    clear($('#panel-data'));
    let desc;
    try {
      desc = await post('/api/open', { path, format, params });
    } catch (e) {
      if (this.sourceKey !== key) return;
      this.source = null;
      $('#src-meta').replaceChildren();
      $('#panel-data').replaceChildren(el('div', { class: 'panel scroll' }, errorBox(e)));
      return;
    }
    if (this.sourceKey !== key) return;
    this.source = desc;
    this.count = desc.count;
    document.title = `${desc.name} · Data Viewer`;
    $('#src-name').textContent = desc.name;
    $('#src-format').textContent = desc.label;
    this.warnings = { source: desc.warnings || [], page: [] };
    this.renderHeader();
    this.dataView.build();
    this.statsView.reset();
    this.schemaView.reset();
    this.infoView.reset();
    this.sqlView.reset(desc);
    this.dataView.reset(desc);
    this.trajectoryView.reset(desc);
    document.querySelector('#tabs [data-tab="trajectory"]').hidden = !(desc.views || []).includes('trajectory');
    this.setTab(tab || 'data');
    this.pollCount();
  }

  reopen(paramsPatch) {
    const p = new URLSearchParams(location.hash.slice(1));
    let params = {};
    try { params = JSON.parse(p.get('params') || '{}') || {}; } catch (e) { params = {}; }
    for (const [k, v] of Object.entries(paramsPatch)) {
      if (v === null || v === undefined || v === '') delete params[k];
      else params[k] = v;
    }
    this.go({ open: this.source.path, format: this.source.format, params, tab: this.tab });
  }

  renderHeader() {
    const d = this.source;
    const meta = [];
    meta.push(el('span', null, el('b', { id: 'meta-rows' }, this.countText()), ' 行'));
    meta.push(el('span', null, el('b', null, fmtInt(d.columns.length)), ' 列'));
    if (d.hdf5) meta.push(el('span', null, '节点 ', el('b', { class: 'mono' }, d.hdf5.node)));
    if (d.lance) meta.push(el('span', null, '版本 ', el('b', null, String(d.lance.version)), d.lance.latest_version && d.lance.latest_version !== d.lance.version ? `（最新 ${d.lance.latest_version}）` : ''));
    meta.push(el('span', { class: 'path' }, d.path || '/'));
    $('#src-meta').replaceChildren(...meta);
    const sw = $('#src-switch');
    clear(sw);
    if (d.format === 'lance') this.lanceSwitch(sw);
    if (d.format === 'hdf5') {
      const b = el('button', { class: 'btn small', type: 'button' }, '切换节点…');
      b.addEventListener('click', () => this.setTab('info', true));
      sw.append(b);
    }
    this.renderWarnings();
  }

  async lanceSwitch(sw) {
    const d = this.source;
    try {
      const info = await get(`/api/s/${d.id}/info`);
      if (this.source !== d) return;
      const sel = el('select', { class: 'select', 'aria-label': '选择版本' });
      const current = d.params.version !== undefined ? String(d.params.version) : '';
      sel.append(el('option', { value: '' }, `最新（v${info.latest_version}）`));
      for (const t of info.tags || []) sel.append(el('option', { value: t.name, selected: current === t.name }, `标签 ${t.name}（v${t.version}）`));
      for (const v of (info.versions || []).slice(0, 300)) sel.append(el('option', { value: String(v.version), selected: current === String(v.version) }, `v${v.version} · ${v.timestamp || ''}`));
      sel.addEventListener('change', () => this.reopen({ version: sel.value || null }));
      sw.append(el('label', null, '版本'), sel);
    } catch (e) { /* header switch is optional */ }
  }

  countText() {
    const c = this.count;
    if (!c) return '…';
    if (c.exact) return fmtInt(c.count);
    if (c.count !== null && c.count !== undefined) return `≥ ${fmtInt(c.count)}`;
    return '计数中…';
  }

  async pollCount() {
    const d = this.source;
    if (!d || (this.count && this.count.exact)) return;
    try {
      const c = await post(`/api/s/${d.id}/count`, {});
      if (this.source !== d) return;
      this.count = c;
      const n = $('#meta-rows');
      if (n) n.textContent = this.countText();
      this.dataView.renderPager();
      if (!c.exact) this.countTimer = setTimeout(() => this.pollCount(), 1500);
    } catch (e) {
      if (this.source === d) {
        const n = $('#meta-rows');
        if (n) n.textContent = '未知';
      }
    }
  }

  onWarnings(list) {
    this.warnings.page = list || [];
    this.renderWarnings();
  }

  renderWarnings() {
    const all = [...new Set([...(this.warnings.source || []), ...(this.warnings.page || [])])];
    $('#src-warnings').replaceChildren(...all.map((w) => noteBox(w)));
  }

  setTab(tab, updateHash = false) {
    if (!['data', 'trajectory', 'schema', 'stats', 'sql', 'info'].includes(tab)) tab = 'data';
    if (tab === 'trajectory' && !(this.source?.views || []).includes('trajectory')) tab = 'data';
    if (tab !== 'trajectory') this.trajectoryView.stop();
    this.tab = tab;
    document.querySelectorAll('#tabs button').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === tab)));
    ['data', 'trajectory', 'schema', 'stats', 'sql', 'info'].forEach((t) => { $(`#panel-${t}`).hidden = t !== tab; });
    if (tab !== 'data' && tab !== 'sql') this.drawer.hide();
    if (!this.source) return;
    if (tab === 'trajectory') this.trajectoryView.activate();
    if (tab === 'schema') this.schemaView.activate();
    if (tab === 'stats') this.statsView.activate();
    if (tab === 'sql') this.sqlView.activate();
    if (tab === 'info') this.infoView.activate();
    if (updateHash) {
      const p = new URLSearchParams(location.hash.slice(1));
      if (tab === 'data') p.delete('tab');
      else p.set('tab', tab);
      history.replaceState(null, '', `#${p.toString()}`);
    }
  }

  onDrawerRow() { /* selection highlighting is handled by the views */ }
}

const app = new App();
window.dataViewer = app;
app.init();
