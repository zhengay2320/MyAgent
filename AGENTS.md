# EO-Agent 项目约定

- Python 3.11+；安装：`python -m pip install -e ".[dev]"`。
- 测试：`python -m pytest -q`；检查：`python -m ruff check .`。
- 离线验收：`python -m eo_agent demo --scenario cloudy --output-dir outputs/demo`。
- 默认必须使用 `mock` profile，模拟信息必须传播到工具结果、JSON 与报告首屏。
- 不得把无数据解释为无变化，不得自动修改用户 AOI 或月份。
- LLM 只能选择工作流允许的有限动作；工具参数由 Pydantic 与执行阶段校验。
- 工具只接受任务资源 ID，不接受任意文件路径、Python 或 Shell。
- 真实兼容模型必须显式选 profile；失败不得回退到 Mock，密钥不得进入日志或产物。
- 新算法通过 `ToolRegistry` 替换实现；新模型通过 `LLMClient`/配置 profile 接入。
- `outputs/` 是运行时目录；不要把生成任务或 SQLite 数据提交为源码。

## HERA-Change 增量迁移稳定约束

- 当前公共行为仍是 `legacy`；后续 `scientific` 必须显式选择，旧请求缺省继续走 legacy。
- 保留 `eo_agent` 包名、CLI、API、`TaskService`、LLM 适配器、工具注册表和追加式存储。
- scientific 使用独立状态与 `scientific/schemas.py`，不得把现有 `schemas.py` 改成同名目录。
- 未拟合风险模型不得输出“校准置信度”或类别概率；启发式分数必须标为未校准。
- `abstain` 是决策而非事件类别；无数据、覆盖不足和不适用都不等于 stable。
- LLM 只接收白名单可见投影；场景名、隐藏标签、评分规则和未来实验响应不得进入提示词。
- 外部 LLM 每次 HTTP 尝试前必须预占预算；重试和结构修复不得绕过硬上限。
- legacy 测试与产物名称继续保留；科学模式不能复用“补证一轮即成功”的 legacy 真值规则。
- `CODEX_PHASE1_PROMPT.md` 是 V0.1 历史任务书；HERA-Change 行为以 `docs/hera_dev/` 为准。

## P1 已稳定的科学模式约束

- `scientific` 必须显式选择并提供 `episode_id`；不得改变 legacy 默认值或旧 CLI/API 语义。
- P1 只注册 E1 原始观测、E2 同季节对照、E3 持续性检验；E4—E7 必须保持不可用。
- 每个实验必须先持久化 `Precommitment`，结果只能追加 `HypothesisRevision`，不得覆盖历史。
- 模拟实验由可见观测与参数决定；Episode 名、私有评估标签和调用轮数不得参与数值或决策。
- 完全重复证据去重，共享根源写明依赖；采集时间和可用时间都必须不晚于分析截止。
- 未训练适配器仅输出 `heuristic_*` 分数，`class_probabilities` 与
  `expected_risk_reduction` 保持 `null`。
