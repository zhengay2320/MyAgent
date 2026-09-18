# HERA-Change P1 科学契约

P1 的科学类型位于 `src/eo_agent/scientific/schemas.py`；根 `schemas.py` 继续承载公共与 legacy
契约。所有类型均使用 Pydantic 严格字段校验。

## 核心区分

- `EventClass` 只包含 stable、persistent_change、transient_change。
- `InterferenceFactor` 与事件类别分开；一个持续变化假设可同时带 reconstruction 等干扰因素。
- `DecisionLabel.abstain` 是调查决策，不是地表真实类别。
- `ExecutionStatus` 描述程序执行，`EvidenceValidity` 描述证据关系，两者不混用。

## 证据与来源

`ObservationRef` 同时记录 acquired_at、available_at、产品版本、空间支持、来源组、覆盖率和模拟
标记。`SourceNode` 形成无环来源图；`EvidenceItem` 保留根来源、处理签名、依赖证据和精确去重
签名。重复签名不增加观测数，共享根来源会记录为 dependent_with。

## 承诺、修订与预算

`Precommitment` 固化假设版本、完整实验参数、支持/冲突条件、信息不足处理方式及内容哈希；它在
实验执行前写盘。`HypothesisRevision` 要求版本严格递增。`BudgetLedger.reserve()` 在 LLM、实验、
数据读取和重规划动作前检查并占用预算。

## 风险输出边界

P1 没有训练风险或价值模型。`RiskEstimate` 的 method 固定为显式 heuristic，
`class_probabilities=null`；`ActionValueEstimate.expected_risk_reduction=null` 且 `is_learned=false`。
