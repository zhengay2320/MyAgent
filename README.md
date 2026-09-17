# EO-Agent V0.1

EO-Agent V0.1 是一个默认离线运行的“两时期地表变化对比”原型。它真实执行
LangGraph 节点、LLM 统一接口、工具注册表、模拟遥感工具、报告生成和 SQLite 持久化；
不是一份预先写死的报告。

> 所有默认观测、去云、变化检测和核验结果都是模拟结果，不代表武汉或任何真实区域的
> 地表变化。程序执行成功不等于科学有效。

## 环境与安装

要求 Python 3.11+。建议创建独立虚拟环境。

Windows PowerShell：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

Linux/macOS：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
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

打开 `http://127.0.0.1:8000/docs`。这是同步本地原型，不适合长时间计算、公网或多用户生产服务。

请求示例：

```bash
curl -X POST http://127.0.0.1:8000/api/tasks/run \
  -H "Content-Type: application/json" \
  -d '{"query":"对武汉演示区2025年10月与2024年9月进行变化检测","aoi_id":"wuhan_demo_100km2","scenario":"cloudy","model_profile":"mock"}'
```

接口包括 `GET /health`、同步 `POST /api/tasks/run`、任务、轨迹、报告和按 `artifact_id`
下载产物的查询接口。下载接口不接受文件路径。

## 产物与数据库

每次运行创建独立 `<output-dir>/<task_id>/`，包含：

- `task.json`、脱敏的 `effective_config.json`；
- `execution_trace.jsonl`、`tool_results.json`；
- `report_facts.json`、`report.json`、`report.md`、`report.html`；
- `artifacts/*.mock.json`，即每次模拟工具调用的真实产物。

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
- 当前不包含真实影像查询、真实去云、真实变化算法、训练、城市级计算、断点续跑或异步队列。
  SQLite/文件持久化只保存完成的执行信息，不等于工作流检查点恢复。

