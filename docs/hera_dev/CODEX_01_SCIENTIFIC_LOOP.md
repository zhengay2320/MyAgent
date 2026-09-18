# Codex P1：在MyAgent中实现最小科学调查闭环

这是代码任务。请读取仓库各级AGENTS、CODEX_GLOBAL_GUARDRAILS.md、DEVELOPMENT_PLAN.md、ACCEPTANCE_MATRIX.md和docs/hera_progress.md，直接创建/修改真实代码并运行测试。先核查P0结果；若有必要前置阻塞，处理或明确记录，不用删测试绕过。

## 一、本轮范围

保留现有eo_agent包、TaskService、legacy CLI/API、LLM工厂、工具注册、SQLite、报告。新增显式scientific模式：

**任务契约 → 初始候选/证据 → LLM假设 → 实验前承诺 → 参数化诊断实验 → 来源更新 → 风险摘要 → LLM反思/重规划 → 停止或拒判 → 科学事件报告。**

本轮真实实现机制，遥感测量使用确定性模拟。默认不联网、不训练、不下载影像、不引入GPU依赖。不要把论文全部能力一次实现。

## 二、兼容入口

1. 原命令保持行为与默认值：`python -m eo_agent demo --scenario cloudy ...`。
2. 新增待实现命令：

```bash
python -m eo_agent demo --workflow scientific --episode EP001 --model mock --output-dir outputs/hera
```

3. TaskRequest增加有默认值的workflow_mode=legacy和可选episode_id；science专有契约单独类型。旧请求不需增加字段。科学模式不使用旧scenario来暗示答案。
4. API通过同一TaskService路由，保留原端点。可在现有请求中选择新模式，不另开一个与旧存储无关的服务。
5. 新增`scientific/schemas.py`、`workflow/scientific_graph.py`等合理模块；保留根schemas.py，避免模块/包同名冲突。

## 三、领域类型（必须可序列化并严格校验）

实现：InvestigationSpec、EventState、ObservationRef、EvidenceItem、Hypothesis、HypothesisRevision、ExperimentSpec、Precommitment、ExperimentResult、RiskEstimate、ActionValueEstimate、Decision、ScientificClaim、BudgetLedger与AgentObservationView。

关键约束：

- 事件标签stable/persistent_change/transient_change；abstain为decision；干扰标签多选，unknown保留。
- InvestigationSpec包装已确认任务，含analysis_mode、时区规范化cutoff、allowed_reference_windows、持续期与预算。
- Hypothesis同时声明可观测支持、冲突、适用条件；文本不是证据，priority不是概率。
- ExperimentSpec使用注册ID和有限参数，如window_id/control_window_id/subregion_id/source_group_id。LLM不能传路径、代码或未经注册参数。
- Precommitment在执行前持久化，包含完整Spec、判读条件与hash；后续改计划必须生成新版本。结果绑定承诺ID。
- ExperimentResult分开execution_status和evidence_validity；支持/冲突指向具体命题；insufficient、not_applicable、non_discriminative独立。
- Evidence包含measurement、单位、coverage、原始root_source_ids、空间支持、acquired_at/available_at、模型/参数签名与mock状态。
- 初始RiskEstimate明确heuristic、calibration_status=unavailable、class_probabilities=null；ActionValue中expected_risk_reduction=null，只能使用命名透明的proxy_priority。

## 四、模拟环境与可见状态隔离

新增EpisodeEnvironment：初始观测、有限可用资产、实验执行和测量反馈。环境与评测标签分开。

实现白名单AgentObservationView，所有科学LLM调用只接收此投影。不传原始TaskRequest/Settings/scenario_config/隐藏世界。禁止ground_truth、expected_next_action、main_confounder真实标签、未来结果、泄露答案的场景名称进入prompt。

episode使用EP001等中性ID；真实生成的event_id与episode标签映射只在环境/评测侧。模型仍可看到科学必要的地物背景、时间、质量、数值和资产ID。

模拟返回由世界观测、实际实验类型和规范参数决定，不按“第N轮”改变正确性；同参数重复结果稳定，不读模型名/策略名决定难度。改变对照窗口必须有可观察影响或明确不适用。

本轮至少4个完整世界：

- EP001：跨月份候选，经可用对照与真实观测摘要可削弱持续变化解释；
- EP002：恢复路径与覆盖充分的原始观测相冲突；
- EP003：多期实际观测摘要支持定义期限内的持续变化；
- EP004：观测始终不足，应该拒判。

再以单元fixture覆盖完全重复、部分同源、真实变化与伪影共存、技术失败、截止时间与审查候选。不要在任何policy内写`if episode_id == ...`。

环境只返回测量与可用性，不返回最终真值。模拟判读规则必须透明且不依赖评测私有标签。

## 五、先实现三个可执行诊断实验

E1 raw_observation_test：比较有效原始观测与共同支持范围，coverage不足必须insufficient，不能用没有观测推导没有变化。

E2 seasonal_control_test：选择授权窗口/对照，检查目标不被替换，返回两组差异、可比性与覆盖；错误窗口为not_applicable或参数无效。

E3 persistence_test：使用不同原始采集ID和日期计算持续性；重复生成帧不增加观测数量；后续跨度不足返回censored/insufficient。

为E4—E7预留Spec和Registry接口；若没有实现，明确unavailable并不放入本轮allowed实验列表。不要伪造成功。

实验选择以科学目标为入口，内部可组合多个旧工具/新模拟器调用。不要把七个实验强行放入旧ALLOWED_STAGES后任意放开所有权限；可增ExperimentExecutor并复用基础工具检查。

## 六、LLM的真实作用边界

在现有LLMClient增加通用结构输出能力，或引入与旧接口兼容的StructuredLLMClient扩展。对兼容适配器将既有泛型_invoke安全包装为公开方法；保留旧三方法。

应用层实现：
1. propose_hypotheses：输出有限候选命题与预期；
2. propose_experiments：输出目标假设、实验类型、时间/空间/源参数、分支条件与简短理由；
3. reflect_on_results：比较承诺与测量，返回保留/削弱/修订及下一争议；
4. compose_claim：只整理已经验证的claim事实。

MockLLM也通过同一方法调用，依据visible measurements/domain catalog生成确定性响应；不访问隐藏fixture。

注意：不得先由固定workflow决定下一实验，再让LLM说“我选择了它”。添加两个受控fake LLM，返回不同合法Spec，验证实际执行分别使用了不同实验/参数。LLM不是最后的数值裁判，不能改measurement、source或score。

## 七、来源图和风险占位

1. 分开来源DAG和命题支持/冲突关系。
2. DAG拒绝环、缺失来源与跨任务资源；根来源以实际原始产品和采集支持定义，不只文件名。
3. 精确重复signature由根源、时间/空间支持、处理/model/param hash、观测量签名组成；event ownership单独校验。
4. 完全复制加入图后，唯一测量数、风险输入和启发式分数保持不变。
5. 部分同源的不同结果保留关联，不能计作独立观测，也不要粗暴删除一个。
6. HeuristicRiskAdapter与HeuristicPriorityAdapter只依据可见数值、可用性、条件与成本；透明文档说明是调试规则，不冒充q_phi/p_psi。
7. No-data不增加stable支持；LLM重复文字不增加证据节点；工具技术失败不成为对某个事件类别的反证。

## 八、工作流、预算与停止

先顺序调查事件，不做并行或多Agent服务。纯router只选边，状态更新在节点。循环至少包含commit→execute→ingest→assess→reflect。

LLM、实验次数、重规划与数据读取预算都有上限。每次请求前预占；记录cache_hit，重复实验不新增证据；额外费用未知时记null。

停止原因明确：evidence_sufficient_in_demo、insufficient_observation、budget_exhausted、no_applicable_experiment、no_progress、technical_failure。即使演示支持某结论，scientific_validity仍not_evaluated。

新实验必须回到原始资源，不能将上轮生成文件加入真实观测目录。在线cutoff对采集与可用时间同时检查；不能只看采集日期。

## 九、持久化与报告

沿用TaskRepository/ArtifactStore；追加科学对象表或JSON索引，不删旧数据。至少每次承诺、实验完成和决策更新后追加事件记录并原子保存快照；完整断点恢复不是本轮承诺。

生成DEVELOPMENT_PLAN.md列出的science产物。HTML事件卡包含：假设版本、预承诺、实际测量、支持/冲突、不足/不适用、源依赖、分数未校准提示、停止原因。

报告不得只有最后一个事件；多事件统计分开。模拟地块可使用已登记且彼此不重叠的合成空间支持，不能生成看似真实武汉坐标。比例分母、未判定区域明确；真实空间合并留待真实几何模块，不假造GeoTIFF。

## 十、验收

先运行全量旧测试，再新增测试，至少覆盖ACCEPTANCE_MATRIX的T07—T34中本轮相关项。

必须运行：

```bash
python -m pytest -q
python -m ruff check .
python -m eo_agent demo --scenario cloudy --output-dir outputs/p1_legacy
python -m eo_agent demo --scenario no_data --output-dir outputs/p1_legacy
python -m eo_agent demo --workflow scientific --episode EP001 --model mock --output-dir outputs/p1_science
python -m eo_agent demo --workflow scientific --episode EP002 --model mock --output-dir outputs/p1_science
python -m eo_agent demo --workflow scientific --episode EP003 --model mock --output-dir outputs/p1_science
python -m eo_agent demo --workflow scientific --episode EP004 --model mock --output-dir outputs/p1_science
```

重点测试：参数真影响执行、实验前承诺不可覆盖、复制证据不增分、低覆盖非反证、compound标签可共存、未来不可读、信息不足拒判、预算终止、各任务隔离、产物真实存在。

另做“重命名episode不改变决策”和“测量反转改变决策”测试，防止背答案。不要要求所有模型输出同一固定工具序列。

## 十一、交付

更新README、架构图、science schema说明、AGENTS与hera_progress。保留历史需求。报告本阶段实测与未验证：仍是模拟遥感、风险/价值未训练、没有真实API调用、没有科学性能结论。

完成后停止，不自动接入P2或真实遥感模型。
