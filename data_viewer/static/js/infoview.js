// "结构" (schema) and "信息" (format-specific details) tabs.
import { get } from './api.js';
import { clear, el, errorBox, fmtBytes, fmtInt, fmtNum, spinner } from './util.js';

const tbl = (headers, rows, opts = {}) => el('div', { class: 'tbl-wrap', style: opts.maxHeight ? { maxHeight: opts.maxHeight, overflow: 'auto' } : null },
  el('table', { class: 'tbl' },
    el('thead', null, el('tr', null, headers.map((h) => el('th', { class: h.num ? 'num' : null }, h.t || h)))),
    el('tbody', null, rows)));
const td = (v, cls) => el('td', { class: cls || null }, v === null || v === undefined || v === '' ? '–' : v);
const section = (title, ...body) => el('section', { class: 'section' }, el('h2', null, title), ...body);
const kvCards = (pairs) => el('div', { class: 'cards-kv' }, pairs.filter(([, v]) => v !== undefined && v !== null && v !== '').map(([k, v, mono]) =>
  el('div', null, el('div', { class: 'k' }, k), el('div', { class: `v${mono ? ' mono' : ''}` }, String(v)))));
const jsonPre = (v) => el('pre', { class: 'json' }, typeof v === 'string' ? v : JSON.stringify(v, null, 2));

// ---------------------------------------------------------------------------
export class SchemaView {
  constructor(panel, app) {
    this.panel = panel;
    this.app = app;
    this.loadedFor = null;
  }

  reset() { this.loadedFor = null; }

  async activate() {
    const sid = this.app.source.id;
    if (this.loadedFor === sid) return;
    this.loadedFor = sid;
    this.panel.className = 'panel scroll';
    this.panel.replaceChildren(spinner());
    try {
      const s = await get(`/api/s/${sid}/schema`);
      if (this.loadedFor !== sid) return;
      const rows = [el('div', { class: 'sf head' }, el('div', null, '字段'), el('div', null, '类型'), el('div', null, '可空'))];
      const walk = (f, depth) => {
        const meta = f.metadata ? Object.entries(f.metadata) : [];
        const extra = [];
        if (f.note) extra.push(f.note);
        if (f.extension) extra.push(`扩展类型：${f.extension}`);
        if (s.lazy && s.lazy[f.name] && depth === 0) extra.push(`SQL 中显示为占位值（${s.lazy[f.name]}）`);
        rows.push(el('div', { class: 'sf' },
          el('div', { class: 'nm', style: { paddingLeft: `${depth * 18}px` } }, depth ? '└ ' : '', f.name),
          el('div', { class: 'ty' }, f.type),
          el('div', { class: 'nl' }, f.nullable ? '是' : '否'),
          meta.length || extra.length ? el('div', { class: 'meta', style: { paddingLeft: `${depth * 18}px` } },
            extra.map((x) => el('div', null, x)),
            meta.map(([k, v]) => el('div', null, el('code', null, k), ' = ', v.length > 300 ? `${v.slice(0, 300)}…` : v))) : null));
        (f.children || []).forEach((c) => walk(c, depth + 1));
      };
      s.fields.forEach((f) => walk(f, 0));
      const parts = [section(['字段', el('span', { class: 'count' }, `${s.fields.length} 个`)], el('div', { class: 'schema-tree' }, rows))];
      const meta = Object.entries(s.metadata || {});
      if (meta.length) {
        parts.push(section('表级元数据', ...meta.map(([k, v]) => el('details', { style: { marginBottom: '8px' } },
          el('summary', { style: { cursor: 'pointer' } }, el('code', null, k)), jsonPre(v)))));
      }
      this.panel.replaceChildren(...parts);
    } catch (e) {
      this.panel.replaceChildren(errorBox(e));
    }
  }
}

// ---------------------------------------------------------------------------
export class InfoView {
  constructor(panel, app) {
    this.panel = panel;
    this.app = app;
    this.loadedFor = null;
  }

  reset() { this.loadedFor = null; }

  async activate(force = false) {
    const sid = this.app.source.id;
    if (this.loadedFor === sid && !force) return;
    this.loadedFor = sid;
    this.panel.className = 'panel scroll';
    this.panel.replaceChildren(spinner());
    try {
      const info = await get(`/api/s/${sid}/info`);
      if (this.loadedFor !== sid) return;
      const fmt = this.app.source.format;
      const render = { lance: this.lance, parquet: this.parquet, csv: this.csv, json: this.json, arrow: this.arrow, hdf5: this.hdf5, webdataset: this.wds }[fmt];
      this.panel.replaceChildren(...(render ? render.call(this, info) : [jsonPre(info)]));
    } catch (e) {
      this.panel.replaceChildren(errorBox(e));
    }
  }

  files(info) {
    if (!info.file_list || info.files <= 1) return [];
    return [section(`文件 `, tbl(['路径', { t: '大小', num: true }], info.file_list.map((f) => el('tr', null, td(f.path, 'mono'), td(fmtBytes(f.size), 'num'))), { maxHeight: '320px' }))];
  }

  lance(info) {
    const src = this.app.source;
    const parts = [section('概览', kvCards([
      ['当前版本', info.version], ['最新版本', info.latest_version], ['版本总数', info.version_count],
      ['行数', fmtInt(info.rows)], ['Fragment 数', info.fragments], ['存储格式版本', info.data_storage_version],
      ['URI', info.uri, true],
    ]))];
    if (info.versions && info.versions.length) {
      parts.push(section('版本', tbl(['版本', '时间', { t: '行数', num: true }, { t: '大小', num: true }, ''], info.versions.map((v) => {
        const isCur = v.version === info.version;
        const btn = el('button', { class: 'btn small', type: 'button', disabled: isCur }, isCur ? '当前' : '查看');
        btn.addEventListener('click', () => this.app.reopen({ version: v.version === info.latest_version ? null : v.version }));
        return el('tr', { class: isCur ? 'selected' : null }, td(String(v.version), 'mono'), td(v.timestamp), td(fmtInt(v.rows), 'num'), td(fmtBytes(v.size), 'num'), el('td', null, btn));
      }), { maxHeight: '360px' })));
    }
    if (info.tags && info.tags.length) {
      parts.push(section('标签', tbl(['标签', '版本', ''], info.tags.map((t) => {
        const btn = el('button', { class: 'btn small', type: 'button' }, '查看');
        btn.addEventListener('click', () => this.app.reopen({ version: t.name }));
        return el('tr', null, td(t.name, 'mono'), td(String(t.version ?? '–'), 'mono'), el('td', null, btn));
      }))));
    }
    if (info.indices && info.indices.length) {
      parts.push(section('索引', tbl(['名称', '类型', '字段'], info.indices.map((i) => el('tr', null, td(i.name, 'mono'), td(i.type), td((i.fields || []).join(', '), 'mono'))))));
    }
    const meta = Object.entries(info.schema_metadata || {});
    if (meta.length) parts.push(section('Schema 元数据', tbl(['键', '值'], meta.map(([k, v]) => el('tr', null, td(k, 'mono'), td(v, 'mono'))))));
    if (src.params && src.params.version) parts.unshift(el('p', null, el('button', { class: 'btn', type: 'button', onclick: () => this.app.reopen({ version: null }) }, '回到最新版本')));
    return parts;
  }

  parquet(info) {
    const parts = [section('概览', kvCards([
      ['文件数', info.files], ['总大小', info.total_size ? fmtBytes(info.total_size) : null],
      [info.files > 1 ? '查看的文件' : '文件', info.inspected_file, true],
      ['行数（该文件）', fmtInt(info.num_rows)], ['Row Group 数', info.num_row_groups], ['列数', info.num_columns],
      ['写入工具', info.created_by, true], ['格式版本', info.format_version],
      ['分区列', info.partition_columns && info.partition_columns.length ? info.partition_columns.join(', ') : null, true],
    ]))];
    if (info.parquet_error) parts.push(errorBox(info.parquet_error));
    if (info.columns && info.columns.length) {
      parts.push(section('列存储', tbl(['列', '物理类型', '压缩', '编码', { t: '压缩后', num: true }, { t: '原始', num: true }, { t: '压缩比', num: true }],
        info.columns.map((c) => el('tr', null, td(c.column, 'mono'), td(c.physical_type), td(c.compression), td(c.encodings.join(', '), 'mono'),
          td(fmtBytes(c.compressed), 'num'), td(fmtBytes(c.uncompressed), 'num'), td(c.compressed ? `${fmtNum(c.uncompressed / c.compressed, 3)}×` : '–', 'num'))))));
    }
    if (info.row_groups && info.row_groups.length) {
      parts.push(section('Row Group', tbl([{ t: '#', num: true }, { t: '行数', num: true }, { t: '原始大小', num: true }, { t: '压缩后', num: true }],
        info.row_groups.map((g) => el('tr', null, td(String(g.index), 'num'), td(fmtInt(g.rows), 'num'), td(fmtBytes(g.uncompressed), 'num'), td(fmtBytes(g.compressed), 'num'))), { maxHeight: '300px' })));
    }
    const kv = Object.entries(info.key_value_metadata || {});
    if (kv.length) parts.push(section('文件元数据', ...kv.map(([k, v]) => el('details', { style: { marginBottom: '8px' } }, el('summary', { style: { cursor: 'pointer' } }, el('code', null, k)), jsonPre(v)))));
    return parts.concat(this.files(info));
  }

  csv(info) {
    const parts = [];
    if (info.dialect) {
      const d = info.dialect;
      const pairs = [['分隔符', JSON.stringify(d.Delimiter)], ['引号', JSON.stringify(d.Quote)], ['转义', JSON.stringify(d.Escape)],
        ['换行', JSON.stringify(d.NewLineDelimiter)], ['跳过行数', d.SkipRows], ['有表头', d.HasHeader === undefined ? null : (d.HasHeader ? '是' : '否')],
        ['日期格式', d.DateFormat || null], ['时间格式', d.TimestampFormat || null]];
      parts.push(section('自动识别的格式', kvCards(pairs)));
    }
    if (info.dialect_error) parts.push(errorBox(info.dialect_error));
    const toggle = el('button', { class: 'btn', type: 'button' }, info.all_varchar ? '恢复自动识别类型' : '全部按文本读取');
    toggle.addEventListener('click', () => this.app.reopen({ all_varchar: info.all_varchar ? null : true }));
    parts.push(section('类型识别', el('p', { class: 'secondary' }, '如果某些行的值和自动识别的类型不符导致读取报错，可以把所有列按文本读取。'), toggle));
    return parts.concat(this.files(info));
  }

  json(info) {
    return [section('概览', kvCards([['布局', info.layout], ['文件数', info.files], ['总大小', info.total_size ? fmtBytes(info.total_size) : null]])), ...this.files(info)];
  }

  arrow(info) {
    const parts = [section('概览', kvCards([['格式', info.layout], ['RecordBatch 数', info.record_batches], ['行数', fmtInt(info.rows)],
      ['文件大小', fmtBytes(info.file_size)], ['内存中大小', fmtBytes(info.in_memory_bytes)]]))];
    const meta = Object.entries(info.schema_metadata || {});
    if (meta.length) parts.push(section('Schema 元数据', tbl(['键', '值'], meta.map(([k, v]) => el('tr', null, td(k, 'mono'), td(v.length > 400 ? `${v.slice(0, 400)}…` : v, 'mono'))))));
    return parts;
  }

  hdf5(info) {
    const current = info.node;
    const nodeRows = info.tree.map((n) => {
      const depth = n.path === '/' ? 0 : n.path.split('/').length - 1;
      const name = n.path === '/' ? '/（根）' : n.path.split('/').pop();
      const desc = n.type === 'group' ? `组 · ${n.children} 项` : `${n.shape ? `[${n.shape.join('×')}]` : '标量'} ${n.dtype}`;
      const extra = n.type === 'dataset' ? [n.compression ? `${n.compression}${n.compression_opts !== null && n.compression_opts !== undefined ? `(${n.compression_opts})` : ''}` : '', n.storage !== undefined ? fmtBytes(n.storage) : ''].filter(Boolean).join(' · ') : '';
      const row = el('div', { class: `h5-node${n.path === current ? ' current' : ''}`, tabindex: 0, title: n.tabular ? '点击作为表格打开' : '' },
        el('span', { class: 'p', style: { paddingLeft: `${depth * 16}px` } }, n.type === 'group' ? '▸ ' : '· ', name),
        el('span', { class: 'd' }, desc),
        el('span', { class: 'd muted' }, extra));
      const open = () => { if (n.tabular) this.app.reopen({ node: n.path }); };
      row.addEventListener('click', open);
      row.addEventListener('keydown', (e) => { if (e.key === 'Enter') open(); });
      const attrs = n.attrs || [];
      if (!attrs.length) return row;
      const det = el('details', { class: 'h5-attrs' }, el('summary', { style: { cursor: 'pointer', padding: '2px 10px 2px 34px', fontSize: '12px', color: 'var(--text-2)' } }, `${attrs.length} 个属性`),
        el('div', { style: { padding: '2px 10px 8px 34px' } }, el('dl', { class: 'kv' }, attrs.map((a) => [el('dt', null, a.name), el('dd', null, typeof a.value === 'string' ? a.value : JSON.stringify(a.value))]))));
      return [row, det];
    });
    const parts = [
      section('当前表格', kvCards([['节点', current, true], ['类型', info.node_kind === 'group' ? '组（子数据集合并成列）' : '数据集'], ['行数', fmtInt(info.rows)], ['列数', info.columns.length]])),
    ];
    if (info.columns.length) {
      parts.push(section('列的来源', tbl(['列', '数据集', '字段', '方式', '每行形状'], info.columns.map((c) => el('tr', null, td(c.name, 'mono'), td(c.dataset, 'mono'), td(c.field, 'mono'), td(modeLabel(c.mode)), td(c.shape.length ? c.shape.join('×') : '标量', 'mono'))), { maxHeight: '260px' })));
    }
    if (info.skipped && info.skipped.length) parts.push(section('未放进表格的数据集', el('ul', null, info.skipped.map((s) => el('li', null, s)))));
    parts.push(section(`文件结构 `, el('p', { class: 'secondary', style: { marginTop: 0 } }, '点击组或数据集即可把它作为表格打开。'), el('div', { class: 'h5-tree' }, nodeRows.flat())));
    if (info.tree_truncated) parts.push(el('p', { class: 'muted' }, '节点太多，只显示了前 5000 个。'));
    if (info.file_attrs && info.file_attrs.length) {
      parts.push(section('文件属性', el('dl', { class: 'kv' }, info.file_attrs.map((a) => [el('dt', null, a.name), el('dd', null, typeof a.value === 'string' ? a.value : JSON.stringify(a.value))]))));
    }
    return parts;
  }

  wds(info) {
    const parts = [section('概览', kvCards([
      ['分片数', info.shard_count], ['已索引分片', `${info.indexed_shards} / ${info.shard_count}`],
      ['样本数', `${fmtInt(info.samples_indexed)}${info.fully_indexed ? '' : '（索引中）'}`], ['总大小', fmtBytes(info.total_size)],
    ]))];
    if (info.fields && info.fields.length) {
      parts.push(section('字段（按扩展名）', tbl(['扩展名', '类型', { t: '出现次数', num: true }, { t: '总字节', num: true }],
        info.fields.map((f) => el('tr', null, td(f.ext, 'mono'), td({ text: '文本', json: 'JSON', binary: '二进制' }[f.kind] || f.kind), td(fmtInt(f.count), 'num'), td(fmtBytes(f.bytes), 'num'))))));
    }
    parts.push(section('分片', tbl(['文件', { t: '大小', num: true }, '压缩', { t: '样本数', num: true }, '错误'],
      info.shards.map((s) => el('tr', null, td(s.name, 'mono'), td(fmtBytes(s.size), 'num'), td(s.compression || '无'), td(s.samples === null ? '索引中…' : fmtInt(s.samples), 'num'), td(s.error))), { maxHeight: '320px' })));
    if (info.example_keys && info.example_keys.length) parts.push(section('样本 key 示例', el('div', { class: 'values-box' }, info.example_keys.join('\n'))));
    if (!info.fully_indexed) {
      const again = el('button', { class: 'btn small', type: 'button' }, '刷新');
      again.addEventListener('click', () => this.activate(true));
      parts.push(again);
    }
    return parts;
  }
}

function modeLabel(m) {
  return {
    scalar: '标量', split: '按列拆分', vector: '向量', tensor: '张量（逐行）', string: '字符串', enum: '枚举',
    binary: '二进制', vlen: '变长数组', ref: '引用', repr: '文本表示',
  }[m] || m;
}

export { clear };
