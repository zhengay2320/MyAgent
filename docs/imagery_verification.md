# 交互式影像准备 V1 验证记录

日期：2026-09-18。

## 环境与安装

- Python：`C:\Users\45376\anaconda3\envs\rs-mcp-client\python.exe`。
- 首次在受限网络中安装失败，错误为无法连接 PyPI；获得依赖安装许可后，
  `python -m pip install -e ".[dev,imagery]"` 成功。
- 已安装 `earthengine-api`、NumPy、Pillow、pyproj、Rasterio 和 Shapely。
- pip 同时报告当前环境中另一个 `rs-mcp-agent` 包仍缺少 Fiona、GeoPandas、Pandas、
  Rasterstats、Rich、Typer；这些不是 EO-Agent 本轮声明或运行影像准备所需的依赖，未为其擅自补装。

## 已验证行为

- 旧测试在影像路由接入后仍通过；legacy/scientific 默认行为未改变。
- Mock 任务真实经历解析、目录检索、PNG 预览、AOI 质量、光学/SAR 推荐、计划与审批门。
- 审批前目标任务目录和 GeoTIFF 不存在。
- 审批后实际写入多波段 GeoTIFF，Rasterio 可打开，网格、波段、数据类型、有效像元和 SHA-256
  均校验；本地 PNG 从这些文件生成。
- 修改目录会生成新版本/哈希；旧审批被拒绝；同一幂等键不会派发第二个下载。
- 在线质量失败会形成仅光学计划；本地 SCL-only 复核若建议 SAR，则停在第二次审批，确认前没有
  SAR 文件。
- SSE 事件在动作发生时持久化；LLM 日志包含实际脱敏 messages、Schema、候选、参数、返回、
  校验与动作处置。
- OpenAI-compatible 使用 `MockTransport` 验证 429 有限重试，两个 HTTP 尝试各自记录且调用前
  受预算限制；测试未访问真实模型。
- Earth Engine 默认 doctor 不初始化；合成 AOI 不能进入真实检索计划；provider 代码和测试替身
  覆盖真实数据集、质量统计、SAR 过滤及审批后下载参数。
- 使用本机浏览器实际完成了输入、检索、展开模型日志、将目录从默认值改为
  `outputs/browser_downloads`、生成计划 v2、确认并下载。任务
  `IMG-7af2375ef2d04a58b7b6` 为 `COMPLETED`，6/6 个 GeoTIFF 校验通过；刷新后从同一 SQLite
  任务继续显示，没有重复执行。
- 浏览器回归同时发现并修复了模型日志曾显示“未知 profile”以及正文仅显示事件包装的问题；当前
  页面显示 `mock-rule-parser-v1`，并直接展示持久化的脱敏 messages、JSON Schema、候选上下文、
  实际响应、解析校验和程序处置。

## 最终命令

- `python -m pytest tests/imagery -q`：16 passed in 9.72s。
- `python -m pytest -q`：最终复核 60 passed in 12.94s。
- `python -m ruff check .`：All checks passed。
- `python -m eo_agent demo --scenario cloudy --output-dir outputs/final_legacy`：退出码 0，任务
  `88952663ad8c4135b498e7f2e10885a6` 为 `COMPLETED`。
- `python -m eo_agent demo --workflow scientific --episode EP001 --model mock --output-dir
  outputs/final_scientific`：退出码 0，任务 `da843aaa22984d718fef937437eb26e3` 为
  `COMPLETED`。
- `python -m eo_agent imagery doctor --provider mock`：ready=true，且未远程检查。
- `python -m eo_agent imagery doctor --provider gee`：依赖可用，但因未设置 `EE_PROJECT_ID` 以退出码 1
  明确报告 `configuration_missing`；没有尝试认证或远程访问。

浏览器任务的 SQLite 记录为计划 v2、哈希前缀 `8686f600730a`、129 条事件、3 次模型调用、
1 条审批和 6 个 completed 文件。下载清单位于
`outputs/browser_downloads/IMG-7af2375ef2d04a58b7b6/download_manifest.json`。

## 未验证

- 未提供 `DEEPSEEK_API_KEY`、第二兼容模型密钥或 Earth Engine 凭据，没有真实付费模型调用。
- 未设置并验证 `EE_PROJECT_ID`，没有真实 Earth Engine 连接、目录查询、预览或卫星 GeoTIFF 下载。
- 当前只支持单进程本地执行器和文件级恢复，不承诺字节级 Range 续传或跨进程锁。
