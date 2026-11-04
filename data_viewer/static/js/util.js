// DOM helpers and formatting. All data values are inserted as text, never as HTML.

export function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  setAttrs(node, attrs);
  append(node, children);
  return node;
}

const SVG_NS = 'http://www.w3.org/2000/svg';
export function svg(tag, attrs, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  setAttrs(node, attrs);
  append(node, children);
  return node;
}

function setAttrs(node, attrs) {
  if (!attrs) return;
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') node.setAttribute('class', v);
    else if (k === 'text') node.textContent = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(node.style, v);
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? '' : String(v));
  }
}

function append(node, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

export function clear(node) {
  node.replaceChildren();
  return node;
}

export const $ = (sel, root = document) => root.querySelector(sel);

export function debounce(fn, ms = 200) {
  let t = null;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

export function fmtInt(n) {
  if (n === null || n === undefined || n === '') return '–';
  const x = Number(n);
  if (!Number.isFinite(x)) return String(n);
  return x.toLocaleString('en-US');
}

export function fmtCompact(n) {
  if (n === null || n === undefined) return '–';
  const x = Number(n);
  if (!Number.isFinite(x)) return String(n);
  const a = Math.abs(x);
  if (a < 10000) return fmtNum(x, 4);
  const units = [[1e12, 'T'], [1e9, 'B'], [1e6, 'M'], [1e3, 'K']];
  for (const [v, u] of units) if (a >= v) return `${Number((x / v).toPrecision(3))}${u}`;
  return String(x);
}

export function fmtNum(v, digits = 6) {
  if (v === null || v === undefined) return '–';
  if (typeof v !== 'number') return String(v);
  if (Number.isInteger(v)) return String(v);
  if (!Number.isFinite(v)) return String(v);
  const a = Math.abs(v);
  if (a !== 0 && (a < 1e-4 || a >= 1e15)) return v.toExponential(Math.max(1, digits - 2));
  return String(Number(v.toPrecision(digits)));
}

export function fmtBytes(n) {
  if (n === null || n === undefined) return '–';
  let x = Number(n);
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (Math.abs(x) >= 1024 && i < units.length - 1) {
    x /= 1024;
    i++;
  }
  return i === 0 ? `${x} B` : `${x.toFixed(x < 10 ? 2 : 1)} ${units[i]}`;
}

export function fmtPct(part, whole) {
  if (!whole) return '–';
  const p = (100 * part) / whole;
  if (p === 0) return '0%';
  if (p < 0.1) return '<0.1%';
  if (p > 99.9 && p < 100) return '>99.9%';
  return `${p.toFixed(p < 10 ? 1 : 0)}%`;
}

export function fmtTime(epochSeconds) {
  const d = new Date(epochSeconds * 1000);
  if (Number.isNaN(d.getTime())) return String(epochSeconds);
  return d.toISOString().replace('T', ' ').replace(/\.000Z$/, 'Z').replace(/:00Z$/, 'Z');
}

export function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined) return '';
  if (seconds < 1) return `${Math.max(1, Math.round(seconds * 1000))} ms`;
  return `${seconds.toFixed(2)} s`;
}

export function mimeCategory(mime) {
  if (!mime) return null;
  if (mime.startsWith('image/')) return 'image';
  if (mime.startsWith('audio/')) return 'audio';
  if (mime.startsWith('video/')) return 'video';
  if (mime === 'application/pdf') return 'pdf';
  if (mime === 'application/x-npy') return 'npy';
  return null;
}

export function shortMime(mime) {
  if (!mime) return '';
  const tail = mime.split('/')[1] || mime;
  return tail.replace(/^x-/, '').replace('svg+xml', 'svg').replace('quicktime', 'mov').replace('mpeg', 'mp3').toUpperCase();
}

const BROWSER_IMAGES = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/bmp', 'image/avif', 'image/svg+xml', 'image/x-icon']);
export function browserImage(mime) {
  return BROWSER_IMAGES.has(mime);
}

let toastTimer = null;
export function toast(message, ms = 3200) {
  const t = document.getElementById('toast');
  t.textContent = message;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, ms);
}

export function errorBox(err) {
  const msg = err && err.message ? err.message : String(err);
  return el('div', { class: 'error-box', role: 'alert' }, el('span', { class: 'ic', 'aria-hidden': 'true' }, '!'), el('div', null, msg));
}

export function noteBox(text, kind = 'warn') {
  return el('div', { class: `note ${kind === 'info' ? 'info' : ''}` }, el('span', { class: 'ic', 'aria-hidden': 'true' }, kind === 'info' ? 'i' : '!'), el('div', null, text));
}

export function spinner(label = '加载中…') {
  return el('div', { class: 'loading-row' }, el('span', { class: 'spinner', 'aria-hidden': 'true' }), label);
}

// Shared tooltip ---------------------------------------------------------
export const tip = {
  show(evt, ...children) {
    const t = document.getElementById('tooltip');
    t.replaceChildren(...children.flat().filter(Boolean).map((c) => (c instanceof Node ? c : document.createTextNode(String(c)))));
    t.hidden = false;
    this.move(evt);
  },
  move(evt) {
    const t = document.getElementById('tooltip');
    if (t.hidden) return;
    const pad = 14;
    const r = t.getBoundingClientRect();
    let x = evt.clientX + pad;
    let y = evt.clientY + pad;
    if (x + r.width > window.innerWidth - 8) x = evt.clientX - r.width - pad;
    if (y + r.height > window.innerHeight - 8) y = evt.clientY - r.height - pad;
    t.style.left = `${Math.max(4, x)}px`;
    t.style.top = `${Math.max(4, y)}px`;
  },
  hide() {
    document.getElementById('tooltip').hidden = true;
  },
};

// Icons -------------------------------------------------------------------
const ICONS = {
  folder: 'M2.5 5.5a1.5 1.5 0 0 1 1.5-1.5h3.6l1.6 1.8H16a1.5 1.5 0 0 1 1.5 1.5v7.2A1.5 1.5 0 0 1 16 16H4a1.5 1.5 0 0 1-1.5-1.5z',
  up: 'M10 15V5M5.5 9.5 10 5l4.5 4.5',
  table: 'M3 4.5h14v11H3zM3 8.5h14M8 8.5v7',
  file: 'M5 2.8h6.5L15 6.3v10.9H5zM11.3 2.8v3.7H15',
  image: 'M3 4h14v12H3zM3 13l4-4 3 3 2.5-2.5L17 14M12.5 7.5h.01',
  audio: 'M8 14.5V4.8l8-1.8v9.7M8 14.5a2 2 0 1 1-4 0 2 2 0 0 1 4 0zM16 12.7a2 2 0 1 1-4 0 2 2 0 0 1 4 0z',
  video: 'M3 5h10v10H3zM13 8.5l4-2.5v8l-4-2.5',
  text: 'M5 2.8h6.5L15 6.3v10.9H5zM7.5 9h5M7.5 12h5M7.5 15h3',
  close: 'M5 5l10 10M15 5 5 15',
  prev: 'M12.5 4.5 7 10l5.5 5.5',
  next: 'M7.5 4.5 13 10l-5.5 5.5',
  first: 'M14 4.5 8.5 10l5.5 5.5M6 4.5v11',
  last: 'M6 4.5 11.5 10 6 15.5M14 4.5v11',
  play: 'M7 5v10l8-5z',
  stop: 'M6 6h8v8H6z',
  copy: 'M7 7h9v9H7zM4 13V4h9',
  external: 'M11 4h5v5M16 4l-7 7M14 11.5V16H4V6h4.5',
};

export function icon(name, size = 16) {
  const path = ICONS[name] || ICONS.file;
  const filled = name === 'play' || name === 'stop';
  return svg('svg', { viewBox: '0 0 20 20', width: size, height: size, 'aria-hidden': 'true' },
    svg('path', filled
      ? { d: path, fill: 'currentColor' }
      : { d: path, fill: 'none', stroke: 'currentColor', 'stroke-width': 1.5, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }));
}

export function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(() => toast('已复制'), () => toast('复制失败'));
  } else {
    toast('浏览器不支持复制');
  }
}
