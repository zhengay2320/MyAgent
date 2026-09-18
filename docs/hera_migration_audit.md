# HERA-Change P0 本地迁移审查

审查日期：2026-09-17。真实仓库 HEAD：
`29d7633909cea282b442ad203398e92456cb6e47`，与 `docs/hera_dev/REPO_AUDIT.md` 的参考提交一致。
该提交只用于定位，没有 reset、checkout 或回退。

## 工作区保护

P0 开始前工作区并非干净：`.env.example` 已删除，IDE 文件、`config.py`、`schemas.py`、
`service.py` 已修改，`docs/hera_dev/` 与 `test_deepseek.py` 为未跟踪内容。以上均按本地现实保留；
没有读取 `.env` 内容，没有运行 `test_deepseek.py`，也没有调用真实 provider。对该脚本仅执行了
Ruff import 排序，以恢复仓库级静态检查。

本地 `config.py` 已使用 `python-dotenv`，P0 将该依赖补入 `pyproject.toml`，避免只因当前环境偶然
预装而成功。这是对已有本地行为的依赖闭合，不是新增 provider 调用。

## 实际模块与迁移决策

| 边界 | P0 结论 | P1 扩展位置 |
|---|---|---|
| `schemas.py` / `TaskRequest` | 保留 legacy 契约 | 增加默认 legacy 的模式字段；科学类型放 `scientific/schemas.py` |
| `TaskService` | 保留 CLI/API 共用入口 | 成为 legacy/scientific 唯一分发点 |
| `workflow/graph.py` | 保留 legacy 图；路由改为纯读 | 新增独立 `scientific_graph.py`，不覆盖旧状态 |
| LLM adapters | 保留三个旧方法与 profile | P1/P2 增加公开结构输出；不让业务层依赖 httpx |
| ToolRegistry/Executor | 保留阶段、参数和资源所有权检查 | scientific 注册来源感知实验工具 |
| SQLite/ArtifactStore | 保留任务和产物格式 | 只做追加迁移，科学产物放任务目录 `science/` |
| reports | 保留 legacy JSON/Markdown/HTML | 新科学事实按事件/版本聚合，不复用“最后一条结果”逻辑 |

## 已确认并修复

1. `mock_tools.py` 的 `changed_fraction=area/100` 确实把武汉 100 km² 示例写成了算法常量。
   现由已校验 `TaskSpec.area_km2` 经 `ToolContext` 传入，并保留
   `area_denominator_km2`/`area_denominator_kind`。新增 200 km² 合成 AOI 回归测试。
2. `WorkflowNodes.parse` 的步骤文字确实固定为“MockLLM 规则解析器”。现从实际
   provider/profile/model metadata 生成，测试使用 fake DeepSeek metadata，不发网络请求。
3. 兼容适配器原先在 `client.post` 后才由工作流累计预算。现适配器持有任务级
   `LLMAttemptBudget`，每次 HTTP 尝试前预占；网络重试与 Schema 修复复用同一上限。
4. `after_verify` 路由确实写入终态。现路由只返回分支，`mark_partial` 节点显式更新状态。
5. 基线 Ruff 确认两个 import 排序问题；P0 机械修复，没有修改测试断言或 provider 行为。

## 保留但未在 P0 解决

- legacy `needs_evidence` 仍按轮次转为 sufficient，只用于旧流程演示；scientific 禁止复用。
- `contains_mock=True` 和 `.mock.json` 仍是 legacy 安全默认；混合来源传播在 P1 实现。
- 报告仍取最后一个 legacy ChangeData/VerificationData；事件/版本聚合在 P1 实现。
- 完整 `llm_attempts.jsonl`、失败调用 usage/cost ledger 在 P2 实现；P0 只保证请求前硬预算。
- 当前文件持久化不是崩溃恢复，没有 resume 声明。
- 未实现 scientific 模式、E1—E7、来源 DAG、风险/价值模型或真实遥感算法。

## P1 增量审查（2026-09-18）

P1 没有回退参考提交，也没有覆盖 P0 前已存在的 `.env.example` 删除、IDE 文件或本地脚本。
仍未读取 `.env` 内容，未运行 `test_deepseek.py`，未访问真实/付费 provider。

已兑现的扩展点：

- `TaskRequest` 默认 legacy；scientific 必须显式选择。`TaskService` 仍是 CLI/API 唯一入口。
- 根 `schemas.py` 保留；科学契约放入 `scientific/schemas.py`，科学状态图独立于 legacy。
- SQLite 仅追加 `workflow_mode` 列，并对已有数据库执行可重复列检查。
- `LLMClient` 增加通用结构输出；Mock 与兼容适配器均实现，业务不直接依赖 httpx。
- P1 实验通过 `ExperimentRegistry` 校验；只有 E1—E3 可用，LLM 提案控制实际类型和参数。
- ArtifactStore 改为同目录临时文件替换；实验前承诺在 executor 调用前落盘并写入轨迹。

仍保留的迁移边界：

- legacy 的按轮次补证规则只存在于旧图，scientific 数值与调用轮数无关。
- P1 来源图和证据去重是事件级；跨任务学习、训练风险模型和校准属于后续阶段。
- P1 文件快照可审计但不是 LangGraph checkpointer，不声明崩溃自动恢复。
