// Rendering of cell values: compact (table / gallery) and full (row detail drawer).
import { getText, get } from './api.js';
import { browserImage, el, fmtBytes, fmtInt, fmtNum, icon, mimeCategory, shortMime, toast } from './util.js';
import { sparkline } from './charts.js';

export const NUMERIC = new Set(['int', 'float', 'decimal']);

export function cellUrl(page, row, col, extra) {
  let u = page.cell_url
    .replaceAll('{row}', String(row))
    .replaceAll('{col}', encodeURIComponent(col))
    .replaceAll('{abs}', String((page.offset || 0) + row));
  if (extra) {
    for (const [k, v] of Object.entries(extra)) {
      if (v === undefined || v === null) continue;
      const s = typeof v === 'string' || typeof v === 'number' ? String(v) : JSON.stringify(v);
      u += `&${k}=${encodeURIComponent(s)}`;
    }
  }
  return u;
}

const isRich = (v) => v && typeof v === 'object' && !Array.isArray(v) && typeof v.$t === 'string';

// ---------------------------------------------------------------------------
// audio playback (one clip at a time)
// ---------------------------------------------------------------------------
let current = null;
function playButton(url) {
  const btn = el('button', { class: 'play-btn', type: 'button', title: '播放', 'aria-label': '播放音频' }, icon('play', 12));
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (current && current.btn === btn) {
      current.audio.pause();
      reset();
      return;
    }
    if (current) { current.audio.pause(); reset(); }
    const audio = new Audio(url);
    current = { audio, btn };
    btn.replaceChildren(icon('stop', 12));
    audio.addEventListener('ended', reset);
    audio.play().catch(() => { toast('浏览器无法播放这段音频'); reset(); });
  });
  function reset() {
    if (current && current.btn === btn) {
      btn.replaceChildren(icon('play', 12));
      current = null;
    }
  }
  return btn;
}

function pixelOnLoad(img) {
  img.addEventListener('load', () => {
    if (img.naturalWidth && img.naturalWidth <= 96 && img.naturalHeight <= 96) img.classList.add('pixel');
  });
  img.addEventListener('error', () => {
    img.replaceWith(el('span', { class: 'chip', title: '浏览器无法显示这个图像' }, icon('image', 12), '无法预览'));
  });
  return img;
}

// ---------------------------------------------------------------------------
// text summaries for nested values
// ---------------------------------------------------------------------------
export function summarize(v, budget = 140) {
  let out = '';
  const push = (s) => {
    if (out.length < budget) out += s;
  };
  const walk = (x, depth) => {
    if (out.length >= budget) return;
    if (x === null || x === undefined) return push('null');
    if (typeof x === 'number') return push(fmtNum(x, 5));
    if (typeof x === 'boolean') return push(String(x));
    if (typeof x === 'string') return push(JSON.stringify(x.length > 60 ? `${x.slice(0, 60)}…` : x));
    if (Array.isArray(x)) {
      push('[');
      x.forEach((item, i) => {
        if (out.length >= budget) return;
        if (i) push(', ');
        walk(item, depth + 1);
      });
      return push(']');
    }
    if (isRich(x)) {
      switch (x.$t) {
        case 'bin': return push(`‹${shortMime(x.mime) || 'BLOB'} ${fmtBytes(x.size)}›`);
        case 'vec': return push(`‹vec ${x.dim}d›`);
        case 'nd': return push(`‹${x.dtype}[${x.shape.join('×')}]›`);
        case 'str': return push(JSON.stringify(`${x.v.slice(0, 60)}…`));
        case 'path': return push(JSON.stringify(x.v));
        case 'more': return push(`… +${x.n}`);
        default: break;
      }
    }
    push('{');
    Object.entries(x).forEach(([k, val], i) => {
      if (out.length >= budget) return;
      if (i) push(', ');
      push(`${k}: `);
      walk(val, depth + 1);
    });
    push('}');
  };
  walk(v, 0);
  return out.length >= budget ? `${out.slice(0, budget)}…` : out;
}

// First image-like value inside a nested structure.
export function findMedia(v, depth = 0) {
  if (depth > 3 || v === null || typeof v !== 'object') return null;
  if (isRich(v)) {
    if (v.$t === 'bin' && mimeCategory(v.mime) === 'image') return v;
    if (v.$t === 'nd' && v.img) return v;
    if (v.$t === 'bin' && v.nd && v.nd.img) return v.nd;
    if (v.$t === 'path' && mimeCategory(v.mime) === 'image' && !v.remote) return v;
    return null;
  }
  const items = Array.isArray(v) ? v.slice(0, 20) : Object.values(v).slice(0, 30);
  for (const item of items) {
    const m = findMedia(item, depth + 1);
    if (m) return m;
  }
  return null;
}

export function mediaSrc(m, ctx, thumb) {
  if (m.$t === 'nd') return `data:image/png;base64,${m.img}`;
  if (m.$t === 'path') return m.url + (thumb ? `&thumb=${thumb}` : '');
  if (m.data) return `data:${m.mime};base64,${m.data}`;
  return cellUrl(ctx.page, ctx.row, ctx.col.name, { sub: m.sub && m.sub.length ? m.sub : undefined, thumb });
}

// ---------------------------------------------------------------------------
// compact cells
// ---------------------------------------------------------------------------
export function renderCell(v, ctx) {
  const kind = ctx.col.kind;
  if (v === null || v === undefined) return el('span', { class: 'v-null' }, 'null');
  if (typeof v === 'number') return el('span', { class: 'v-num' }, fmtNum(v));
  if (typeof v === 'boolean') return el('span', { class: `v-bool v-${v}` }, String(v));
  if (typeof v === 'string') {
    if (NUMERIC.has(kind)) return el('span', { class: /^-?\d/.test(v) ? 'v-num' : 'v-num v-special' }, v);
    return el('span', { class: 'v-str' }, v.length > 400 ? `${v.slice(0, 400)}…` : v);
  }
  if (!isRich(v)) return nestedCompact(v, ctx);
  switch (v.$t) {
    case 'str':
      return el('span', { class: 'v-str' }, v.v.slice(0, 400), el('span', { class: 'v-more' }, `… 共 ${fmtInt(v.len)} 字`));
    case 'bin': return binCompact(v, ctx);
    case 'vec': return vecCompact(v);
    case 'nd': return ndCompact(v);
    case 'path': return pathCompact(v, ctx);
    case 'more': return el('span', { class: 'v-more' }, `… +${v.n}`);
    default: return nestedCompact(v, ctx);
  }
}

function binCompact(v, ctx) {
  const cat = mimeCategory(v.mime);
  const meta = el('span', { class: 'v-meta' }, `${shortMime(v.mime) || 'BLOB'} · ${fmtBytes(v.size)}`);
  if (cat === 'image') {
    const img = pixelOnLoad(el('img', { class: 'thumb', src: mediaSrc(v, ctx, 192), loading: 'lazy', decoding: 'async', alt: '' }));
    return el('span', { class: 'v-media' }, img, meta);
  }
  if (cat === 'audio') {
    const url = cellUrl(ctx.page, ctx.row, ctx.col.name, { sub: v.sub && v.sub.length ? v.sub : undefined });
    return el('span', { class: 'v-media' }, playButton(url), meta);
  }
  if (cat === 'video') return el('span', { class: 'v-media' }, el('span', { class: 'chip' }, icon('video', 12), '视频'), meta);
  if (v.nd) return el('span', { class: 'v-media' }, ndCompact(v.nd), el('span', { class: 'v-meta' }, `NPY · ${fmtBytes(v.size)}`));
  if (cat === 'pdf') return el('span', { class: 'v-media' }, el('span', { class: 'chip' }, 'PDF'), meta);
  if (v.text !== undefined) {
    return el('span', null, el('span', { class: 'v-str' }, v.text.slice(0, 200)), el('span', { class: 'v-more' }, fmtBytes(v.size)));
  }
  return el('span', { class: 'v-blob' }, `BLOB ${fmtBytes(v.size)}`, v.hex ? el('span', { class: 'v-hex' }, v.hex) : null);
}

function vecCompact(v) {
  const head = v.head.map((x) => (typeof x === 'number' ? fmtNum(x, 3) : String(x)));
  return el('span', { class: 'v-vec', title: `L2 范数 ${fmtNum(v.norm, 4)}` },
    `[${head.join(', ')}${v.dim > head.length ? ', …' : ''}]`,
    el('span', { class: 'dim' }, `${v.dim}d`));
}

function ndCompact(v) {
  const label = `${v.dtype}[${v.shape.join('×')}]`;
  if (v.img) {
    const img = pixelOnLoad(el('img', { class: 'thumb', src: `data:image/png;base64,${v.img}`, alt: '' }));
    return el('span', { class: 'v-media' }, img, el('span', { class: 'v-meta' }, label));
  }
  const range = v.min !== undefined ? ` ${fmtNum(v.min, 3)}…${fmtNum(v.max, 3)}` : '';
  return el('span', { class: 'v-media' }, el('span', { class: 'chip' }, label), range ? el('span', { class: 'v-meta' }, range) : null);
}

function pathCompact(v, ctx) {
  const cat = mimeCategory(v.mime);
  if (v.remote) {
    return el('span', { class: 'v-media' }, el('span', { class: 'chip', title: '远程链接，在详情中可加载预览' }, icon('external', 12), 'URL'), el('span', { class: 'v-str' }, v.v));
  }
  if (cat === 'image') {
    const img = pixelOnLoad(el('img', { class: 'thumb', src: `${v.url}&thumb=192`, loading: 'lazy', decoding: 'async', alt: '' }));
    return el('span', { class: 'v-media' }, img, el('span', { class: 'v-str' }, v.v));
  }
  if (cat === 'audio') return el('span', { class: 'v-media' }, playButton(v.url), el('span', { class: 'v-str' }, v.v));
  if (cat === 'video') return el('span', { class: 'v-media' }, el('span', { class: 'chip' }, icon('video', 12), '视频'), el('span', { class: 'v-str' }, v.v));
  return el('span', { class: 'v-str' }, v.v);
}

function nestedCompact(v, ctx) {
  const m = findMedia(v);
  const text = el('span', { class: 'v-json' }, summarize(v));
  if (!m) return text;
  const img = pixelOnLoad(el('img', { class: 'thumb', src: mediaSrc(m, ctx, 192), loading: 'lazy', decoding: 'async', alt: '' }));
  return el('span', { class: 'v-media' }, img, text);
}

// ---------------------------------------------------------------------------
// full values (drawer)
// ---------------------------------------------------------------------------
export function renderFull(v, ctx) {
  const kind = ctx.col.kind;
  if (v === null || v === undefined) return el('span', { class: 'v-null' }, 'null');
  if (typeof v === 'number' || typeof v === 'boolean') return el('div', { class: 'v-num' }, String(v));
  if (typeof v === 'string') {
    if (NUMERIC.has(kind)) return el('div', { class: 'v-num' }, v);
    return el('div', { class: 'full-text' }, v);
  }
  if (!isRich(v)) return jsonBlock(v, ctx);
  switch (v.$t) {
    case 'str': {
      const box = el('div', { class: 'full-text' }, `${v.v}…`);
      getText(cellUrl(ctx.page, ctx.row, ctx.col.name)).then((t) => { box.textContent = t; }).catch(() => {});
      return box;
    }
    case 'bin': return binFull(v, ctx);
    case 'vec': return vecFull(v, ctx);
    case 'nd': return ndFull(v, cellUrl(ctx.page, ctx.row, ctx.col.name));
    case 'path': return pathFull(v);
    default: return jsonBlock(v, ctx);
  }
}

function binFull(v, ctx) {
  const url = cellUrl(ctx.page, ctx.row, ctx.col.name, { sub: v.sub && v.sub.length ? v.sub : undefined });
  const cat = mimeCategory(v.mime);
  const info = el('div', { class: 'v-meta' }, `${v.mime || '未知类型'} · ${fmtBytes(v.size)}`);
  const open = el('a', { href: url, target: '_blank', rel: 'noopener', class: 'v-meta' }, ' 在新标签页打开');
  if (cat === 'image') {
    const img = el('img', { class: 'full-img', src: v.data && browserImage(v.mime) ? `data:${v.mime};base64,${v.data}` : url, alt: '' });
    img.addEventListener('load', () => {
      info.textContent = `${v.mime} · ${img.naturalWidth}×${img.naturalHeight} · ${fmtBytes(v.size)}`;
      if (img.naturalWidth <= 96) img.classList.add('pixel');
    });
    return el('div', null, img, el('div', null, info, open));
  }
  if (cat === 'audio') return el('div', null, el('audio', { controls: true, preload: 'metadata', src: url }), el('div', null, info, open));
  if (cat === 'video') return el('div', null, el('video', { controls: true, preload: 'metadata', src: url }), el('div', null, info, open));
  if (v.nd) return ndFull(v.nd, url);
  if (cat === 'pdf') return el('div', null, info, el('a', { href: url, target: '_blank', rel: 'noopener' }, '打开 PDF'));
  if (v.text !== undefined) {
    const box = el('div', { class: 'full-text mono' }, v.text);
    getText(`${url}&format=text`).then((t) => { box.textContent = t.length > 200000 ? `${t.slice(0, 200000)}…` : t; }).catch(() => {});
    return el('div', null, box, info);
  }
  return el('div', null, el('div', { class: 'values-box' }, v.hex ? `${v.hex} …` : ''), el('div', null, info, open));
}

function vecFull(v, ctx) {
  const url = cellUrl(ctx.page, ctx.row, ctx.col.name);
  const wrap = el('div', null, el('dl', { class: 'kv' },
    el('dt', null, '维度'), el('dd', null, String(v.dim)),
    el('dt', null, 'L2 范数'), el('dd', null, fmtNum(v.norm, 6))));
  const chartBox = el('div', { style: { margin: '10px 0 6px' } });
  const values = el('div', { class: 'values-box' }, '加载中…');
  wrap.append(chartBox, values);
  get(url).then((arr) => {
    if (!Array.isArray(arr)) return;
    const nums = arr.map(Number);
    chartBox.append(sparkline(nums, { height: 90, label: '分量值', indexLabel: '维度' }));
    values.textContent = `[${arr.map((x) => (typeof x === 'number' ? fmtNum(x, 6) : String(x))).join(', ')}]`;
  }).catch((e) => { values.textContent = String(e.message || e); });
  return wrap;
}

export function ndFull(v, url) {
  const wrap = el('div');
  if (v.img) {
    const img = el('img', { class: 'full-img', src: `${url}&format=png`, alt: '' });
    img.addEventListener('load', () => { if (img.naturalWidth <= 128) img.classList.add('pixel'); });
    wrap.append(img);
  }
  const kv = el('dl', { class: 'kv', style: { marginTop: '8px' } },
    el('dt', null, '形状'), el('dd', null, `[${v.shape.join(', ')}]`),
    el('dt', null, '类型'), el('dd', null, v.dtype));
  if (v.min !== undefined) {
    kv.append(el('dt', null, '最小 / 最大'), el('dd', null, `${fmtNum(v.min)} / ${fmtNum(v.max)}`));
    kv.append(el('dt', null, '均值'), el('dd', null, fmtNum(v.mean)));
  }
  wrap.append(kv);
  if (v.values) {
    wrap.append(el('div', { class: 'values-box', style: { marginTop: '8px' } }, JSON.stringify(v.values)));
  } else {
    const btn = el('button', { class: 'btn small', type: 'button', style: { marginTop: '8px' } }, '显示数值');
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        const res = await get(url);
        const box = el('div', { class: 'values-box', style: { marginTop: '8px' } });
        if (res.too_large) box.textContent = '数组太大，不在页面中显示全部数值。';
        else {
          const s = JSON.stringify(res.data);
          box.textContent = s.length > 60000 ? `${s.slice(0, 60000)}…` : s;
        }
        btn.replaceWith(box);
      } catch (e) {
        btn.disabled = false;
        toast(e.message);
      }
    });
    wrap.append(btn);
  }
  return wrap;
}

function pathFull(v) {
  const cat = mimeCategory(v.mime);
  const label = el('div', { class: 'full-text mono' }, v.v);
  if (v.remote) {
    const link = /^https?:\/\//i.test(v.v) ? el('a', { href: v.v, target: '_blank', rel: 'noopener noreferrer' }, '在新标签页打开') : null;
    const box = el('div', null, label, link);
    if (cat === 'image' || cat === 'video' || cat === 'audio') {
      const btn = el('button', { class: 'btn small', type: 'button', style: { marginLeft: '8px' } }, '加载预览');
      btn.addEventListener('click', () => {
        const tag = cat === 'image' ? 'img' : cat;
        const node = el(tag, cat === 'image' ? { class: 'full-img', src: v.v, alt: '', referrerpolicy: 'no-referrer' } : { controls: true, src: v.v });
        btn.replaceWith(node);
      });
      box.append(btn);
    }
    return box;
  }
  if (cat === 'image') return el('div', null, el('img', { class: 'full-img', src: v.url, alt: '' }), label);
  if (cat === 'audio') return el('div', null, el('audio', { controls: true, preload: 'metadata', src: v.url }), label);
  if (cat === 'video') return el('div', null, el('video', { controls: true, preload: 'metadata', src: v.url }), label);
  return label;
}

// ---------------------------------------------------------------------------
// JSON tree
// ---------------------------------------------------------------------------
function hasTruncation(v, depth = 0) {
  if (depth > 30 || v === null || typeof v !== 'object') return false;
  if (isRich(v)) return v.$t === 'more' || v.$t === 'str';
  return (Array.isArray(v) ? v : Object.values(v)).some((x) => hasTruncation(x, depth + 1));
}

function jsonBlock(v, ctx) {
  const wrap = el('div');
  wrap.append(jsonTree(v, ctx));
  if (hasTruncation(v)) {
    const btn = el('button', { class: 'btn small', type: 'button', style: { marginTop: '6px' } }, '加载完整值');
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        const full = await get(cellUrl(ctx.page, ctx.row, ctx.col.name));
        wrap.replaceChildren(jsonTree(full, ctx, 2));
      } catch (e) {
        btn.disabled = false;
        toast(e.message);
      }
    });
    wrap.append(btn);
  }
  return wrap;
}

export function jsonTree(value, ctx, openDepth = 2) {
  const root = el('div', { class: 'jt' });
  root.append(node(null, value, 0));
  return root;

  function keyNode(key) {
    if (key === null) return null;
    return [el('span', { class: 'k' }, typeof key === 'number' ? String(key) : key), ': '];
  }

  function node(key, v, depth) {
    if (v === null || v === undefined) return el('div', { class: 'row' }, keyNode(key), el('span', { class: 'z' }, 'null'));
    if (typeof v === 'number') return el('div', { class: 'row' }, keyNode(key), el('span', { class: 'n' }, fmtNum(v, 15)));
    if (typeof v === 'boolean') return el('div', { class: 'row' }, keyNode(key), el('span', { class: 'b' }, String(v)));
    if (typeof v === 'string') return el('div', { class: 'row' }, keyNode(key), el('span', { class: 's' }, JSON.stringify(v)));
    if (isRich(v)) {
      if (v.$t === 'more') return el('div', { class: 'row' }, el('span', { class: 'sum' }, `… 还有 ${fmtInt(v.n)} 项`));
      let content;
      if (v.$t === 'bin' && mimeCategory(v.mime) === 'image') {
        const img = pixelOnLoad(el('img', { class: 'thumb', src: mediaSrc(v, ctx, 320), alt: '' }));
        img.style.height = '96px';
        img.style.maxWidth = '240px';
        content = el('span', { class: 'v-media' }, img, el('span', { class: 'v-meta' }, `${shortMime(v.mime)} · ${fmtBytes(v.size)}`));
      } else if (v.$t === 'bin' && mimeCategory(v.mime) === 'audio') {
        const url = cellUrl(ctx.page, ctx.row, ctx.col.name, { sub: v.sub && v.sub.length ? v.sub : undefined });
        content = el('audio', { controls: true, preload: 'metadata', src: url, style: { height: '32px' } });
      } else if (v.$t === 'str') {
        content = el('span', { class: 's' }, JSON.stringify(`${v.v}…`), el('span', { class: 'sum' }, ` （共 ${fmtInt(v.len)} 字）`));
      } else {
        content = renderCell(v, ctx);
      }
      return el('div', { class: 'row' }, keyNode(key), content);
    }
    const isArr = Array.isArray(v);
    const entries = isArr ? v.map((x, i) => [i, x]) : Object.entries(v);
    const realLen = isArr ? entries.filter(([, x]) => !(isRich(x) && x.$t === 'more')).length : entries.length;
    const more = isArr ? entries.find(([, x]) => isRich(x) && x.$t === 'more') : null;
    const label = isArr ? `[${fmtInt(realLen + (more ? more[1].n : 0))}]` : `{${fmtInt(entries.length)}}`;
    const det = el('details', depth < openDepth ? { open: true } : null);
    det.append(el('summary', null, keyNode(key), el('span', { class: 'sum' }, `${label} ${summarize(v, 60)}`)));
    if (!entries.length) {
      return el('div', { class: 'row' }, keyNode(key), el('span', { class: 'sum' }, isArr ? '[]' : '{}'));
    }
    // build children lazily when opened
    let built = false;
    const build = () => {
      if (built) return;
      built = true;
      for (const [k, x] of entries.slice(0, 2000)) det.append(node(k, x, depth + 1));
      if (entries.length > 2000) det.append(el('div', { class: 'row sum' }, `… 还有 ${entries.length - 2000} 项`));
    };
    if (det.open) build();
    det.addEventListener('toggle', build);
    return det;
  }
}
