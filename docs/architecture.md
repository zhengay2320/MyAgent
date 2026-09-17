# EO-Agent V0.1 架构

## 模块边界

- `schemas.py`：外部输入、任务、动作、工具结果、专用领域数据、产物、轨迹与报告事实契约。
- `llm/`：`LLMClient` 边界；默认规则型 Mock 与可选 OpenAI-compatible HTTP 适配器。
- `tools/`：显式注册表、阶段/参数/资源所有权执行器、确定性模拟实现。
- `workflow/`：真正的 LangGraph `StateGraph` 和条件边，不持有供应商 SDK 或遥感实现。
- `storage/`：每次操作单独打开 SQLite 连接；任务目录内写入并校验文件边界。
- `reports/`：只从 `ReportFacts` 填数字，Jinja 全量转义，无 CDN/脚本/外部资源。
- `service.py`：CLI 和 FastAPI 共用的唯一编排入口。

## 执行流

```text
parse -> validate -> search -> prepare -> quality -> choose_reconstruction
                                      |                    |
                                      |                    +-> reconstruct
                                      +--------------------------> detect -> verify
                                                                      |
                              report <---- sufficient / budget end ---+
                                                                      |
                             prepare <---- fetch additional evidence --+
```

`no_data` 从 search 进入 `PARTIAL` 报告。`needs_evidence` 获取新观测资源 ID 后，必须重新执行
prepare、quality、detect、verify。重试由执行器限制；补证回环由图和业务预算限制；LangGraph 的
`recursion_limit` 是独立的最后保护。

## 信任与科学边界

LLM 看不到场景 ID，只看到质量或核验工具的可见输出；它不能跳过验证、直接生成科学事实或
提供工具路径。场景状态属于单次任务，不在工具实例中共享。模拟领域数据由固定 seed 和规范化
输入决定，任务 ID 只用于资源所有权，不用于模拟数值。

`ToolResult.status` 表示程序执行，`scientific_validity` 表示科学有效性。模拟工具即使成功也固定为
`contains_mock=true` 与 `not_evaluated`。报告固定显示模拟警示。

## 持久化边界

SQLite 保存任务与产物索引，文件保存完整轨迹和报告。每个数据库操作都创建并释放连接，适应
FastAPI 线程。产物下载先按 `(task_id, artifact_id)` 查库，再验证解析路径仍在任务目录内。
V0.1 不含 LangGraph checkpointer、进程恢复或异步任务队列。

