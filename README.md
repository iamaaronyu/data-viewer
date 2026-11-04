# Data Viewer

本地只读数据查看器。在浏览器里浏览、筛选、查询和统计常见的 AI / 数据工程文件格式，图片、音频、视频和数组直接在表格里预览。所有计算都在本机完成（DuckDB + PyArrow），数据不会上传。

## 支持的格式

| 格式 | 打开方式 | 说明 |
|---|---|---|
| Lance | `.lance` 目录 | 按版本或标签查看，显示索引、Fragment 信息；向量列显示维度和范数 |
| Parquet | 单个文件，或分区目录（`year=2024/…`） | 单文件按 Row Group 直接翻页；目录自动识别 hive 分区列 |
| CSV / TSV | `.csv` `.tsv`（可带 `.gz` / `.zst`） | 自动识别分隔符、表头、列类型；类型识别出错时可切换为全部按文本读取 |
| JSON / JSONL | `.json` `.jsonl` `.ndjson` | 数组、对象或逐行 JSON；嵌套字段以可折叠的树展示 |
| Arrow / Feather | `.arrow` `.feather` `.ipc` `.arrows` | IPC 文件、IPC 流、Feather V1/V2，内存映射读取 |
| HDF5 | `.h5` `.hdf5` `.h5ad` `.nc` | 任意组或数据集都能作为表格打开：复合类型拆成列，宽二维数组变成向量列，三维以上数组按行显示（像图像的直接渲染成缩略图） |
| WebDataset | `.tar` 分片或分片目录 | 同一 key 的文件组成一个样本（`000001.jpg` + `000001.json` …）；后台索引全部分片 |
| RLDS / TFRecord | `.tfrecord` 或 TFDS 分片文件 | 按 episode 读取观测、动作与图像，检查 CRC32C，按元数据恢复形状与类型 |

## 功能

- **数据**：分页浏览；`WHERE` 条件筛选（DuckDB SQL 表达式）；点击表头排序；选择显示的列；图片列可切换到画廊视图；点击任意行打开详情面板（↑/↓ 切换行，Esc 关闭）。
- **结构**：字段树、Arrow 类型、字段和表级元数据（例如 Hugging Face、pandas 写入的 JSON）。
- **统计**：一次扫描得到所有列的非空率、唯一值、范围、分位数；每列的数值 / 时间分布直方图、出现最多的值、字符串长度、二进制大小与内容类型、向量范数分布。可以只统计当前筛选结果或前 10 万行。
- **SQL**：对当前数据写只读 DuckDB 查询，表名固定为 `data`；提供按当前列生成的示例（分组计数、分位数、向量余弦相似度检索、展开嵌套字段等）和历史记录。
- **信息**：各格式的专属信息，如 Lance 的版本 / 标签 / 索引，Parquet 的压缩与 Row Group，HDF5 的文件结构树和属性，WebDataset 的分片与字段统计。
- **轨迹**：识别具身 episode 结构后，在同一数据页面显示轨迹标签，支持多相机画面、动作曲线、逐帧数值和 episode 切换；普通数据保留原有视图。支持 RLDS、HDF5 和 LeRobot Parquet，详见 [具身数据适配说明](EMBODIED.md)。
- **媒体预览**：
  - 二进制列按文件头识别 PNG / JPEG / GIF / WebP / BMP / TIFF / AVIF / SVG、WAV / MP3 / FLAC / OGG / AAC / M4A、MP4 / WebM / MOV、PDF、NPY；
  - 字符串列里的本地媒体路径（相对数据集目录或根目录）直接显示缩略图，远程 URL 只在点击后加载；
  - 嵌套结构里的图片（例如 Hugging Face 的 `{bytes, path}` 图像列）同样可以预览；
  - 大图片由服务端生成缩略图，音视频支持拖动进度（HTTP Range）。

## 安装

需要 Python 3.10+。

```bash
cd data-viewer
uv venv --python 3.12 .venv
UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple uv pip install --python .venv/bin/python -e ".[dev]"
```

不用 uv 的话：`python -m venv .venv && .venv/bin/pip install -e ".[dev]"`。

## 使用

```bash
# 浏览当前目录
.venv/bin/data-viewer

# 直接打开一个数据集（根目录为它的上级目录）
.venv/bin/data-viewer ../sample-data/data/train.lance

# 生成各种格式的演示数据并打开
.venv/bin/data-viewer --demo
```

常用参数：

| 参数 | 说明 |
|---|---|
| `PATH` | 要打开的数据集或目录，默认当前目录 |
| `--root DIR` | 文件浏览的根目录（只能访问根目录以内的文件） |
| `--port 8765` | 端口，被占用时自动顺延 |
| `--host 127.0.0.1` | 监听地址；设为 `0.0.0.0` 时会自动生成访问令牌，并打印带令牌的链接 |
| `--no-browser` | 不自动打开浏览器 |
| `--timeout 120` | 单次查询超时（秒） |
| `--threads` / `--memory-limit 8GB` | DuckDB 线程数和内存上限 |

## SQL 说明

- 当前数据集在 SQL 中叫 `data`，例如 `SELECT label, count(*) FROM data GROUP BY 1`。
- 只允许一条只读语句：`SELECT`、`WITH`、`FROM …`、`DESCRIBE`、`SUMMARIZE`、`EXPLAIN`、`PRAGMA` 查询等。`COPY`、`CREATE`、`ATTACH`、`INSTALL`、`SET` 等会被拒绝。
- HDF5 和 WebDataset 在 SQL 中多一列 `__index__`（行号）。体积大的列（HDF5 张量、WebDataset 的图片 / 音频等二进制字段）在 SQL 里以占位值出现：WebDataset 二进制字段是字节数，HDF5 张量是形状描述。只要结果里带着 `__index__`，表格会自动补上原始内容的预览。
- WebDataset 的 `json` 字段在 SQL 中是文本，可以用 `json::JSON->>'$.key'` 取值。

## 安全

- 默认只监听 `127.0.0.1`，并校验 `Host` 头（防 DNS rebinding）、`Origin` 头和 `Content-Type`（防跨站请求）。
- 文件浏览、文件预览和打开数据集都限制在根目录以内。
- SQL 只能执行只读语句，但 DuckDB 的 `read_csv('/某个路径')` 这类函数仍能读取本机上当前用户可读的任意文件。不要在不可信的网络上用 `--host 0.0.0.0` 开放服务。

## 已知限制

- CSV / JSON 没有行索引，翻到很深的页需要从头扫描；行数在第一次需要时才统计。
- 压缩的 WebDataset 分片（`.tar.gz` 等）随机读取较慢；建议使用未压缩的 `.tar`。
- HEIC 图片无法预览；视频能否播放取决于浏览器的解码能力（MP4 / WebM 最稳妥）。
- 大于 1M 行的数据，唯一值是 HyperLogLog 估计值（界面上标为“≈”）。
- DuckDB 暂时锁定在 1.x：DuckDB 2.0 会把二进制列、定长向量列和结构体子字段上的过滤条件下推给 Lance，而 Lance 不支持这类下推，查询会直接报错。

## 开发

```bash
.venv/bin/python -m pytest          # 99 个测试，覆盖所有格式和 API
.venv/bin/python -m data_viewer samples --no-browser   # samples/ 可用 data-viewer --demo samples 生成
```

```
data_viewer/
  cli.py          命令行入口
  server.py       FastAPI 路由、页面缓存、单元格内容（支持 Range）、安全校验
  engine.py       DuckDB：每个请求独立 cursor、只读校验、超时、分页
  encode.py       Arrow → JSON 单元格（富类型用 "$t" 标记）
  media.py        文件头识别、ndarray → PNG、缩略图
  stats.py        列统计
  demo.py         演示数据生成
  sources/        各格式适配器：lance / duck(parquet, csv, json) / arrow / hdf5 / wds
  static/         前端（原生 ES 模块，无构建步骤、无外部依赖）
tests/            pytest
```
