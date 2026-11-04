# 具身数据适配与验证

## 功能

统一使用数据浏览入口 `/`。在同一文件列表打开数据后，识别到具身episode结构时，在“数据、结构、统计、SQL、信息”旁增加“轨迹”视图；普通数据不显示轨迹标签。轨迹视图使用当前打开的文件，查看语言指令、多相机画面、状态/动作原始值及最多前8维的曲线。切换文件或离开轨迹标签时暂停播放。旧 `/embodied` 地址重定向到首页。

- RLDS / TFRecord：DROID、OXE、BridgeData 等 TFDS Example 格式。检查 TFRecord 长度及 payload CRC32C，按 episode 读取，使用同目录 `features.json` 恢复原始形状和 dtype。原字段名、单位和坐标含义不变。原表格入口也可直接打开分片，通过 `params.episode` 指定 episode。
- HDF5：LIBERO `data/demo_*`、RoboMIND 类时间序列、EgoDex 姿态。保留组和字段；不把4×4姿态矩阵误判成图像；支持原始图像数组和编码图像。非对齐静态字段明确提示。压缩包需要先完整下载、验证并解压。
- LeRobot Parquet：按实际 `episode_index` 分组，按 `frame_index` 排序；v2使用逐episode视频模板，v3使用 episode metadata 中各相机的 chunk/file/from/to timestamp 读取连续视频。显示实际时间戳；task_index 按 tasks.jsonl 映射语言。
- EgoDex：读取同名 HDF5 / MP4 配对，按该数据集公开的30Hz姿态采样率关联视频。变换矩阵保留原始 ARKit 坐标，未转换成机器人关节动作。

HM3D三维场景漫游不在本次轨迹适配范围。其他不符合这些存储约定的数据会报告错误，不推测未知布局。RLDS没有真实时间戳时显示帧号；不会假设控制频率。部分下载造成的缺失视频会显示警告。

## 接口

- `GET /api/embodied/files`：列出完整可识别文件，排除临时分片。
- `GET /api/embodied/episode?path=...&episode=0`：帧数、相机、字段形状、语言和警告。
- `GET /api/embodied/step?path=...&episode=0&step=0`：原始数值与真实时间戳（无则null）。
- `GET /api/embodied/frame?path=...&episode=0&step=0&camera=...`：指定相机图像。
- `GET /api/embodied/trajectory?path=...&episode=0&field=...`：最多5000点、前8维轨迹；图形横轴为帧号。

所有路径限制在配置的数据根目录内；沿用访问令牌和Host校验。验证与预览只读打开原始数据。

## 验证

`python -m pytest tests -q` 包含原有99项测试和7项具身测试：独立protobuf编码的RLDS解析、CRC损坏、截断、HDF5 episode边界、LeRobot分组排序、API边界与根目录隔离、共享视频非零时间偏移，以及统一入口的能力识别、普通数据排除和HDF5表格/轨迹同时读取。2026-11-04本地和ARM服务器106项全部通过。

真实样本报告：

```bash
python -m data_viewer.validate_embodied --root /datasets --output /reports/real-data.json --per-dataset 2
```

报告逐数据集记录可用文件数、所检样本路径、帧数、dtype/形状、非有限值数量、首/中/末帧图像尺寸与SHA256、API和直接读取的一致性、警告与错误。缺失样本记录为 `not_available`，不算通过。有限抽样不等于全部episode完整验证，也不验证动作物理语义。

## 离线部署

建议在服务器独立虚拟环境中安装，在无外网环境使用预先下载的wheel。通过只读容器挂载或systemd的 `ProtectSystem=strict` 保持原始数据只读。服务只监听服务器 `127.0.0.1:8765`，例如：

```bash
data-viewer /path/to/datasets --root /path/to/datasets --host 127.0.0.1 --port 8765 --no-browser
```

离线环境可设置 `DATA_VIEWER_DUCKDB_EXTENSIONS` 为已下载的官方DuckDB扩展目录。本次验证使用DuckDB1.4.3和其签名ICU扩展；限制依赖 `<2`，避免新主版本缺少离线ICU导致无法启动。

从工作站访问：

```bash
ssh -N -L 8765:127.0.0.1:8765 user@server
```

浏览器打开 `http://127.0.0.1:8765/`。数据目录由启动参数指定，验证报告保存在 `--output` 指定的位置。

部署为systemd服务时，可使用 `systemctl status <service-name>` 查看状态，使用 `journalctl -u <service-name>` 查看日志。
