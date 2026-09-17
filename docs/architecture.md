# EO-Agent V0.1 架构

> P1 迁移状态：`legacy` 仍是默认工作流；显式 `scientific` 已实现离线最小调查闭环。
> 两条链路共用服务、模型适配器和追加式存储，但状态图及科学契约独立。

## 模块边界

- `schemas.py`：外部输入、任务、动作、工具结果、专用领域数据、产物、轨迹与报告事实契约。
- `llm/`：`LLMClient` 边界；默认规则型 Mock 与可选 OpenAI-compatible HTTP 适配器。
- `tools/`：显式注册表、阶段/参数/资源所有权执行器、确定性模拟实现。
- `workflow/`：legacy 与 scientific 两个独立 LangGraph，不持有供应商 SDK 或真实遥感实现。
- `scientific/`：P1 契约、Episode 环境、实验注册/执行、来源 DAG、白名单投影和启发式适配器。
- `storage/`：每次操作单独打开 SQLite 连接；任务目录内写入并校验文件边界。
- `reports/`：只从 `ReportFacts` 填数字，Jinja 全量转义，无 CDN/脚本/外部资源。
- `service.py`：CLI 和 FastAPI 共用的唯一编排入口。

## legacy / scientific 边界

`TaskRequest.workflow_mode` 默认 `legacy`。`TaskService` 是唯一分发点：legacy 继续调用
`workflow/graph.py`；scientific 调用 `workflow/scientific_graph.py`。数据库只追加
`workflow_mode` 列，旧请求与旧产物名保持可读。根 `schemas.py` 保留公共契约，科学类型位于
`scientific/schemas.py`，没有把模块改成同名目录。

scientific 的 LLM 只接收 `AgentObservationView`：事件上下文、可见质量标记、已授权资产 ID、
已有证据摘要、假设、合法实验、参考窗口和剩余预算。Episode 私有评估、正确动作和未来结果
不进入视图。LLM 实验提案由 Pydantic 和 `ExperimentRegistry` 二次校验，并真实决定实验类型及
参数；工作流不会把提案降级为文字装饰。

## scientific P1 执行流

```text
task contract -> candidate + initial evidence -> propose hypotheses
 -> propose registered experiment -> persist precommitment -> execute E1/E2/E3
 -> update source/evidence DAG -> heuristic review -> append hypothesis revision
 -> continue within budget | decide/abstain -> event report
```

E1—E3 数值只由已授权观测和登记参数决定，不按调用轮数或 Episode 名称成功。时间截止同时
约束采集时间和可用时间。完全重复的证据签名被去重；共享根观测会写入依赖关系，不能当作独立
投票。覆盖或持续期不足产生 `insufficient`，最终可触发 `abstain`，而不是 `stable`。

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

P1 的实验前承诺、实验结果、证据图、风险历史和决策在任务目录按事件保存；单文件使用同目录
临时文件原子替换。该机制保证“先承诺后执行”的可审计顺序，但仍不声明进程崩溃后的自动恢复。

## P0 修复后的边界

- legacy 变化比例使用已校验 `TaskSpec.area_km2`，并在 `ChangeData`/报告中记录
  `task_aoi_area` 分母类别；未来可判读面积必须显式使用 `readable_area`，不能隐式替换。
- provider 步骤说明来自实际 `LLMMetadata`，不再把兼容 provider 写成 MockLLM。
- OpenAI-compatible 适配器在每次 HTTP 请求前预占任务级次数；重试与结构修复使用同一预算。
  完整逐尝试成本文件 `llm_attempts.jsonl` 仍属于 P2，不在 P0 冒充已完成。
- `after_verify` 条件路由只读取状态；不足证据终态由 `mark_partial` 节点显式写入。
