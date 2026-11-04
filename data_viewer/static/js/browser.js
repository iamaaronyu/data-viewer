// Sidebar file browser.
import { get } from './api.js';
import { clear, el, errorBox, fmtBytes, icon, spinner } from './util.js';

const FORMAT_SHORT = { lance: 'LANCE', parquet: 'PARQUET', csv: 'CSV', json: 'JSON', arrow: 'ARROW', hdf5: 'HDF5', webdataset: 'WDS', rlds: 'RLDS' };
const FORMAT_LABEL = { lance: 'Lance', parquet: 'Parquet', csv: 'CSV', json: 'JSON', arrow: 'Arrow', hdf5: 'HDF5', webdataset: 'WebDataset', rlds: 'RLDS / TFRecord' };

export class FileBrowser {
  constructor({ list, filter, banner, foot, onOpen, onNavigate, onPreview }) {
    Object.assign(this, { list, filter, banner, foot, onOpen, onNavigate, onPreview });
    this.data = null;
    this.active = null;
    this.filter.addEventListener('input', () => this.render());
    this.filter.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        const first = this.list.querySelector('.fs-item:not(.up)');
        if (first) first.click();
      }
    });
  }

  async load(path) {
    if (this.data && this.data.path === path) return this.data;
    clear(this.list).append(spinner());
    try {
      this.data = await get('/api/fs/list', { path });
      this.filter.value = '';
      this.render();
      return this.data;
    } catch (e) {
      clear(this.list).append(el('div', { style: { padding: '8px' } }, errorBox(e)));
      this.data = null;
      throw e;
    }
  }

  setActive(path) {
    this.active = path;
    this.list.querySelectorAll('.fs-item').forEach((n) => n.classList.toggle('active', n.dataset.path === path));
  }

  render() {
    const d = this.data;
    if (!d) return;
    const term = this.filter.value.trim().toLowerCase();
    const items = [];
    if (d.parent !== null && d.parent !== undefined && !term) {
      const up = el('button', { class: 'fs-item up', type: 'button', dataset: { path: d.parent } }, icon('up'), el('span', { class: 'name' }, '..'), el('span'));
      up.addEventListener('click', () => this.onNavigate(d.parent));
      items.push(up);
    }
    const entries = d.entries.filter((e) => !term || e.name.toLowerCase().includes(term));
    for (const e of entries) {
      const ic = e.dir ? (e.format ? 'table' : 'folder') : e.format ? 'table' : (e.media === 'image' ? 'image' : e.media === 'audio' ? 'audio' : e.media === 'video' ? 'video' : e.media === 'text' ? 'text' : 'file');
      const right = e.format ? el('span', { class: 'fmt' }, FORMAT_SHORT[e.format] || e.format) : el('span', { class: 'size' }, e.dir ? '' : fmtBytes(e.size));
      const btn = el('button', {
        class: `fs-item${e.format ? ' dataset' : ''}${this.active === e.path ? ' active' : ''}`, type: 'button',
        dataset: { path: e.path }, title: e.format ? `${e.name}\n${FORMAT_LABEL[e.format]}${e.dir ? '' : ` · ${fmtBytes(e.size)}`}` : e.name,
        role: 'option',
      }, icon(ic), el('span', { class: 'name' }, e.name), right);
      btn.addEventListener('click', () => {
        if (e.dir && !e.format) this.onNavigate(e.path);
        else if (e.format) this.onOpen(e.path, e.format);
        else this.onPreview(e);
      });
      items.push(btn);
    }
    clear(this.list).append(...items);
    if (!entries.length) this.list.append(el('div', { class: 'fs-empty' }, term ? '没有匹配的文件' : '空目录'));
    if (d.truncated) this.list.append(el('div', { class: 'fs-empty' }, '文件太多，只显示了前 3000 项'));

    if (d.dir_format && d.dir_format !== 'lance') {
      const open = el('button', { class: 'btn small primary', type: 'button' }, '打开');
      open.addEventListener('click', () => this.onOpen(d.path, d.dir_format));
      this.banner.replaceChildren(el('span', null, `这个目录可以作为 ${FORMAT_LABEL[d.dir_format]} 数据集打开`), open);
      this.banner.hidden = false;
    } else {
      this.banner.hidden = true;
    }
    const dirs = d.entries.filter((e) => e.dir && !e.format).length;
    const data = d.entries.filter((e) => e.format).length;
    this.foot.textContent = `${data} 个数据集 · ${dirs} 个目录 · ${d.entries.length - dirs - data} 个其他文件`;
  }
}

export { FORMAT_LABEL };
