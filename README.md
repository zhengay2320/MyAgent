# EO-Agent V0.1

EO-Agent V0.1 是一个默认离线运行的“两时期地表变化对比”原型。它真实执行
LangGraph 节点、LLM 统一接口、工具注册表、模拟遥感工具、报告生成和 SQLite 持久化；
不是一份预先写死的报告。

> 所有默认观测、去云、变化检测和核验结果都是模拟结果，不代表武汉或任何真实区域的
> 地表变化。程序执行成功不等于科学有效。

## HERA-Change 迁移状态

当前完成 P1 最小科学调查闭环。原 EO-Agent 工作流仍是默认 `legacy`；HERA-Change
`scientific` 必须显式开启。P1 提供事件级候选与初始证据、LLM 白名单提案、实验前承诺、
E1 原始观测、E2 同季节对照、E3 持续性检验、来源 DAG、追加式假设修订、程序化停止/拒判及
事件级 JSON/Markdown/HTML 报告。所有科学观测和实验仍是确定性离线模拟，尚未实现论文完整
方法或科学性能验证。

## 环境与安装

要求 Python 3.11+。建议创建独立虚拟环境。

Windows PowerShell：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,imagery]"
```

若 PowerShell 管道或命令捕获把中文显示成 `æ¨¡å...` 一类乱码，可在当前终端先执行：

```powershell
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new()
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$OutputEncoding = [Console]::OutputEncoding
$env:PYTHONUTF8 = "1"
```

这只调整终端显示和 Python 标准流编码。SQLite/JSON 均按 UTF-8 保存，不应为修复终端显示而转码
数据库或已有产物。

Linux/macOS：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,imagery]'
```

若系统没有 `py` 启动器，直接使用 Python 3.11 可执行文件创建环境。本项目不需要
GPU、API Key、卫星影像或外部服务即可运行默认演示。

## CLI

默认多云演示：

```bash
python -m eo_agent demo --scenario cloudy --output-dir outputs/demo
```

其他验收场景：

```bash
python -m eo_agent demo --scenario clear --output-dir outputs/demo
python -m eo_agent demo --scenario needs_evidence --output-dir outputs/demo
python -m eo_agent demo --scenario no_data --output-dir outputs/demo
python -m eo_agent demo --scenario temporary_failure --output-dir outputs/demo
python -m eo_agent demo --scenario insufficient --output-dir outputs/demo
```

P1 科学模式（必须显式指定工作流和 Episode）：

```bash
python -m eo_agent demo --workflow scientific --episode EP001 --model mock --output-dir outputs/p1_science
python -m eo_agent demo --workflow scientific --episode EP002 --model mock --output-dir outputs/p1_science
python -m eo_agent demo --workflow scientific --episode EP003 --model mock --output-dir outputs/p1_science
python -m eo_agent demo --workflow scientific --episode EP004 --model mock --output-dir outputs/p1_science
```

EP001 检查同季节冲突，EP002 检查恢复伪影，EP003 检查持续变化，EP004 检查覆盖不足拒判。
Episode 名称和私有评估标签不参与数值计算或决策。

自定义任务：

```bash
python -m eo_agent run \
  --query "对武汉演示区2025年10月与2024年9月进行变化检测" \
  --aoi-id wuhan_demo_100km2 --scenario cloudy --model mock \
  --output-dir outputs/run
```

CLI 输出任务 ID、终态、模拟标识、完整步骤和实际报告绝对路径。`COMPLETED` 与明确标注的
`PARTIAL` 返回 0；`WAITING_INPUT` 返回 3；`FAILED` 返回 1。

## API

```bash
python -m eo_agent serve --host 127.0.0.1 --port 8000 --output-dir outputs/api
```

打开 `http://127.0.0.1:8000/docs` 查看旧接口，或打开
`http://127.0.0.1:8000/imagery` 使用中文交互式影像准备页面。影像页面创建任务后由有上限的
本地后台执行器处理，状态、模型实际输入输出、工具动作和逐文件进度通过 SSE 实时恢复；旧
legacy/scientific 端点仍保持原语义。本地服务默认只建议绑定 `127.0.0.1`，不适合公网或多用户生产。

请求示例：

```bash
curl -X POST http://127.0.0.1:8000/api/tasks/run \
  -H "Content-Type: application/json" \
  -d '{"query":"对武汉演示区2025年10月与2024年9月进行变化检测","aoi_id":"wuhan_demo_100km2","scenario":"cloudy","model_profile":"mock"}'
```

科学模式 API 请求在相同端点显式增加：

```json
{
  "query": "对武汉演示区2025年10月与2024年9月进行变化检测",
  "aoi_id": "wuhan_demo_100km2",
  "model_profile": "mock",
  "workflow_mode": "scientific",
  "episode_id": "EP001"
}
```

接口包括 `GET /health`、同步 `POST /api/tasks/run`、任务、轨迹、报告和按 `artifact_id`
下载产物的查询接口。下载接口不接受文件路径。

## 交互式影像准备 V1

该功能只负责“理解条件—检索候选—AOI 内质量检查—建议光学/SAR—用户审批—下载与校验”，
不执行去云推理、变化检测或原因分析。默认模型和数据后端都是 Mock，但会真实经过相同的任务状态、
LLM 结构输出、候选校验、审批哈希、分块写入、Rasterio 校验、本地缩略图和 SQLite 事件路径。

```bash
python -m eo_agent imagery doctor --provider mock
python -m eo_agent serve --host 127.0.0.1 --port 8000 --output-dir outputs/api
```

### 终端与 `log.txt` 调试日志

CLI、API、legacy、scientific 和交互式影像准备流程都会把中间执行信息同步输出到终端，
并以 UTF-8 JSON Lines 追加保存到所选输出目录的 `log.txt`。例如上述服务使用：

```text
outputs/api/log.txt
```

日志包含用户提交的任务条件、任务拆解、输入模型的白名单数据与结构要求、模型实际返回、
解析和业务校验、动作接受或拒绝、工具参数与结果、实验前承诺、科学调查修订、下载阶段事件
以及最终状态。每行都是一个独立 JSON 对象，可按 `task_id`、`stage` 或 `event_type` 检索。

日志不会记录模型不可见的内部思维过程；API Key、Authorization、Cookie、密码、Token、签名
以及 URL 查询参数会在写入终端和文件前脱敏。`log.txt` 可能包含完整用户查询和 AOI GeoJSON，
仅应用于本地调试，不应提交到版本库或发送给无权访问研究数据的人员。

页面支持粘贴/上传 WGS84 Polygon/MultiPolygon，或选择内置的
`wuhan_sample_plot_wgs84` 固定研究方框（不是武汉行政边界）。模糊地名不会被自动变成边界。
搜索和小型预览不等于正式下载：流程必须停在 `WAITING_DOWNLOAD_APPROVAL`，只有用户确认当前
`plan_version`、`plan_hash`、候选、波段、共享网格和目录后才创建正式文件。刷新页面只按 task ID
恢复；不会重新检索或重复下载。

光学选择要求每个 `required_period_id` 恰好一景。模型首次违反该硬约束时，程序会保留
`action.rejected` 轨迹，并把原输出、逐时期错误和同一白名单反馈给同一个模型修复；默认最多修复
一次，由 `configs/imagery.yaml` 的 `max_selection_repairs` 控制。修复仍占用同一 LLM 调用预算，
达到上限后明确失败，程序不会代替模型选择候选或放宽时期约束。

默认下载根目录是项目中的 `data/downloads/`，每个任务实际写入独立的
`data/downloads/<task_id>/`。页面可修改根目录，并会显示解析后的绝对路径；该目录位于运行
Python 后端的机器。目标目录在批准前不会创建，已有同名正式文件不会被隐式覆盖。

影像控制状态位于 `<output-dir>/imagery/`，SQLite 位于
`<output-dir>/imagery.sqlite3`。正式目录包含 GeoTIFF、质量层、产品/审批元数据、脱敏日志、SHA-256
清单和从实际下载文件生成的 PNG 缩略图。Mock 候选与文件始终标记为模拟来源。

### Earth Engine

真实后端仅在页面显式选择 `gee` 后启用，代码固定使用：

- `COPERNICUS/S2_SR_HARMONIZED`；
- `GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED`；
- `COPERNICUS/S1_GRD`（IW、VV/VH）。

先由用户在自己的环境完成 Earth Engine 认证，然后设置 Cloud 项目：

```powershell
earthengine authenticate
$env:EE_PROJECT_ID="你的已授权 Cloud Project ID"
python -m eo_agent imagery doctor --provider gee
python -m eo_agent imagery doctor --provider gee --check-remote
```

默认 doctor 只检查本地依赖和配置；只有显式 `--check-remote` 才执行小型只读初始化检查，不下载
影像、不调用 LLM。应用永远不会调用 `ee.Authenticate()`、不会自动弹出认证，也不会在失败时切换
到 Mock。当前仓库完成了真实目录、预览、AOI 面积质量、SAR 和审批后短时下载 URL 的实现及测试替身，
但本次开发没有凭据，因此没有验证真实连接或真实影像下载。

## 产物与数据库

每次运行创建独立 `<output-dir>/<task_id>/`，包含：

- `task.json`、脱敏的 `effective_config.json`；
- `execution_trace.jsonl`、`tool_results.json`；
- `report_facts.json`、`report.json`、`report.md`、`report.html`；
- `artifacts/*.mock.json`，即每次模拟工具调用的真实产物。

scientific 任务还包含 `task_contract.json`、`events.json`、`sources.json`、
`budget_ledger.jsonl`，以及 `events/<event_id>/` 下的假设、修订、实验前承诺、实验结果、
证据图、风险历史和最终决策。文件采用同目录临时文件替换，预承诺在实验调用前保存。

`<output-dir>/tasks.sqlite3` 保存任务和产物索引。任务目录从不复用，因此重复执行不会覆盖旧任务。

## DeepSeek / 兼容模型

默认 profile 是 `mock`，不会读取密钥或访问网络。要显式选择 DeepSeek 开发 profile：

```powershell
$env:DEEPSEEK_API_KEY="..."
$env:DEEPSEEK_BASE_URL="https://api.deepseek.com/v1"
$env:DEEPSEEK_MODEL="填写实际模型 ID"
python -m eo_agent demo --model deepseek_dev --scenario cloudy --output-dir outputs/deepseek
```

Linux/macOS 使用 `export` 设置同名变量。第二兼容服务使用 `SECOND_API_KEY`、
`SECOND_BASE_URL`、`SECOND_MODEL` 和 `--model second_compatible`。profile 定义位于
`configs/models.yaml`；业务流程不依赖供应商 SDK 或模型名称。只有显式选择真实 profile 时才校验
其配置；缺项、鉴权或网络失败都会明确失败，绝不会静默切换模型或退回 Mock。

适配器调用可信配置中的 `<base_url>/chat/completions`，只发送 profile 明确声明的生成参数。
JSON 修复、网络重试均有上限，401/403 不盲目重试。当前只用 `httpx.MockTransport` 验证了
兼容协议，没有密钥时不会做真实 API 验证。

影像页面同样复用 `deepseek_dev` 与 `second_compatible`。完整脱敏 messages、Schema、可见候选、
生效参数、实际响应、解析校验、动作是否接受和实际工具参数均按 task/call/attempt 保存并展示；密钥、
认证头和签名 URL 不落盘。当前 profile 未声明经过验证的流式协议，因此页面明确等待完整响应，不伪造
逐字输出；完整 JSON 通过校验前不会执行动作。

## 测试与检查

```bash
python -m pytest -q
python -m ruff check .
```

测试使用临时目录和 `MockTransport`，不访问外网，不污染 `outputs/`。覆盖分支、回环、预算、
契约、注册表替换、任务隔离、产物、SQLite、API 和 HTML 转义。

## 扩展方式

- 新遥感算法：实现 `DomainTool` 协议并在 `ToolRegistry` 中替换同名工具。工作流不需要修改。
- 新兼容模型：在 `configs/models.yaml` 新增可信 profile 和环境变量映射。业务节点仍只依赖
  `LLMClient`。
- 新 AOI：在 `fixtures/aois.json` 登记明确范围；系统不会自行把“武汉”缩成演示区。

## 故障排查与边界

- `profile ... 缺少配置`：补齐该 profile 的三个环境变量，或明确使用 `--model mock`。
- `WAITING_INPUT`：检查 AOI、两个月份、面积上限或任务是否超出“两时期对比”。
- `PARTIAL`：查看报告中的无数据/证据不足原因；它不表示“没有变化”。
- 交互式影像准备已包含真实 Earth Engine 适配代码，但未在本次开发环境验证凭据、配额、真实连接或
  真实下载；不包含导出任务、字节级 Range 续传、多进程协调或公网部署。
- 当前不包含真实去云、真实变化算法、训练或城市级计算。影像准备的 Mock GeoTIFF 是确定性合成数据，
  不代表任何地点的真实观测。
- scientific P1 只包含 E1—E3 离线模拟；E4—E7、训练风险/价值模型、完整调用成本账本、
  真实数据接入、基准评估和论文结论均未实现。
