# MyAgent → HERA-Change 增量开发方案

## 0. 目标、依据与实施边界

目标不是重写一个更大的Agent平台，而是在现有EO-Agent V0.1上增加可检验的事件调查能力：

**候选事件 → 可检验解释 → 实验前承诺 → 来源约束下选实验 → 数值观测 → 来源更新与去重 → 假设修订 → 停止/拒判 → 证据报告。**

方法依据是用户提供的《HERA-Change论文技术路线与实验设计——逐环节细化版》M0—M9。现有代码依据提交`29d7633909cea282b442ad203398e92456cb6e47`。审查范围、具体源码链接与问题见REPO_AUDIT.md。

本阶段交付开发计划和Codex任务书，未实施仓库变更。下面的新文件、接口、命令均为拟开发要求。

## 1. 三层目标必须分开

| 层次 | 交付 | 可得出的结论 |
|---|---|---|
| 工程正确性 | 有约束的工作流、模拟环境、来源图、测试、日志 | 流程和接口按规范运行 |
| 研究机制可测性 | 不同策略在相同交互环境下产生不同合法实验轨迹 | 可以开展公平机制比较，但模拟器偏好不代表真实优势 |
| 遥感科学有效性 | 真实事件、独立真值、拟合/校准风险与价值模型、跨域实验 | 经实验支持后，才可讨论误报、漏检、校准与成本收益 |

不要把前两层结果写成第三层结论。先建立科学机制，再逐个替换观测工具；真实算法与数值学习可分阶段加入。

## 2. 保留、改造、新增

### 保留

`eo_agent`包名；CLI/API/TaskService；现有LLM工厂及兼容适配器；工具注册与参数/资源检查；SQLite/ArtifactStore；Jinja报告；全部旧测试及legacy场景。

### 改造

- service按workflow_mode选择legacy或scientific，不替换唯一公共入口。
- LLM公开泛型结构输出入口，旧parse_task/choose_action/summarize保留为兼容包装。
- 状态从单一current_resource_id扩展为事件集合与各自来源图，新状态不要覆盖旧状态类型。
- 每个HTTP尝试和工具动作都进行事前预算检查与事后记录。
- 报告按事件与版本聚合，不继续只取最后一份ChangeData。
- 科学模式的工具结果按来源传播模拟状态，不能全靠全局contains_mock=True。

### 新增

假设/证据/实验/事件数据契约；可见状态投影；交互模拟环境；实验前承诺；七类诊断实验；来源去重与依赖检查；风险/价值接口及明确标记的启发式占位；科学策略；多模型/多策略评测。

## 3. 目标结构与调用关系

```text
现有CLI/API
   └─ TaskService（模式分发、共享任务存储）
       ├─ legacy：保留V0.1流程和测试
       └─ scientific：区域入口 + 事件级调查子流程
           ├─ M0任务契约与M1观测摘要
           ├─ M2候选事件/M3初始证据
           └─ 对每个事件运行：
               M4提出命题与预期
               M5来源感知风险状态
               M6提出合法实验计划
               M7选择实验/停止
               执行实验
               M8更新来源、风险、反思与停止
               回到M4/M6或进入M9
```

阶段编号描述科学职责，不强迫实现为十个一次性节点。M5在每次新证据后重算，M6/M7/M8形成回环；区域候选发现与事件调查分离。

## 4. M0—M9开发映射

| 方法环节 | 现有可复用代码 | 新版交付 | 首次出现阶段 |
|---|---|---|---|
| M0任务契约 | schemas.py、validate节点 | 截止时间、持续期、允许参考窗、空间支持与分层预算；旧TaskSpec适配 | P1 |
| M1观测构建 | search/prepare/quality工具接口 | 独立S1/S2时间轴、原始产品ID、质量与有效性；先模拟后真实 | P1/P4 |
| M2候选发现 | detect结果 | 多事件候选、审查候选、未调查状态；不以检测器正例为唯一起点 | P1/P4 |
| M3初始证据 | 工具结果、quality摘要 | 白名单AgentObservationView、数值/单位/缺失/来源 | P1 |
| M4假设建模 | 通用LLM传输 | 假设集合、可观测预期、适用条件、不可变实验承诺与修订 | P1/P2 |
| M5证据融合 | provenance基础字段 | 来源DAG、支持/冲突语义关系、重复不变性、风险接口 | P1/P4 |
| M6实验设计 | 工具注册/执行器 | E1—E7实验库；LLM选科学问题和有限参数，后端调用算法 | P1/P2 |
| M7价值选择 | choose_action | 合法动作/预算门控、启发式优先级、后续学习型决策风险下降 | P1/P4 |
| M8更新停止 | verify/evidence回环 | 显式状态更新、矛盾处理、修订、成本、无进展/预算/信息不足停止 | P1 |
| M9结论报告 | reports与ArtifactStore | 事件卡、来源图、支持/冲突、区间、拒判、区域分母 | P1/P2 |

## 5. 领域契约

新增类型建议集中于`src/eo_agent/scientific/schemas.py`，避免破坏现有`src/eo_agent/schemas.py`导入。

### InvestigationSpec

包装现有TaskSpec，并补充analysis_mode、analysis_cutoff、allowed_reference_windows、persistence_horizon_days、spatial_support约束、预算配置。时间采用带时区的规范形式；对等效UTC时间比较。测试真值不能出现在该对象中。

### EventState

包含event_id、parent_event_id、geometry_ref、area与单位、candidate_source、调查状态、观测/证据引用、假设历史、承诺与结果、风险状态、剩余预算、停止原因。区域内多个事件状态隔离；原始数据缓存可共享，但证据ID和事件支持范围必须明确。

### Hypothesis与Precommitment

Hypothesis区分目标事件命题与可并存干扰；字段包含predicate_id、description、expected_signatures、contradictory_signatures、applicability。priority只能称优先级。

Precommitment包含event_id、hypothesis_version、目标假设、ExperimentSpec、预期支持/冲突判读、无数据处理、生成时间和内容hash。修订创建新版本，保留旧版本；执行结果绑定承诺ID。

### Observation与Evidence

Observation保留原始产品ID、传感器、acquired_at、available_at、产品版本、空间支持、质量和单位。

Evidence保留artifact_ref、measurement/uncertainty/coverage、根观测集合、派生活动/模型/参数签名、结果适用性。来源DAG与命题支持/冲突关系分离。文件名不同不能导致相同测量成为多个根来源。

### ExperimentSpec与ExperimentResult

Spec包括E1—E7实验类型、事件/假设目标、window_id、control_window_id、subregion_id、source_group_id、允许的参数profile、前置条件和预算。

Result分别记录execution_status与evidence_validity。证据含义允许支持、冲突、无区分力、信息不足、不适用；技术失败属于执行状态。支持/冲突必须指向具体命题，并有观测数值或注册规则依据。

### RiskEstimate与ActionValue

P1采用HeuristicRiskAdapter与HeuristicPriorityAdapter，只输出透明分数或排序；`class_probabilities=null`、`calibration_status=unavailable`、`expected_risk_reduction=null`。报告不能显示“93%可信”。

P4才添加拟合的q_phi与p_psi：存模型版本、训练/校准划分、输入特征、适用域与校准结果。数值模型输出，不让LLM直接编写后验。

### Decision、ScientificClaim与BudgetLedger

Decision是某事件类别或abstain，另有technical_status/scientific_validity。Claim区分观测事实、模型推断、支持/反对证据、时间区间、未解决问题与停止原因。预算按每次尝试预扣、结算，未知费用为null，不当作免费。

## 6. 交互模拟环境：从假工具升级为可检查实验

旧工具按轮数决定成功，仅保留为legacy演示。新科学模拟器不得固定“第2轮成功”，不得读取当前策略名调整难度。

环境结构：

- public：中性episode ID、初始测量、可用观测清单、质量、共同实验目录；
- world：按观测ID、时间、参数与输入决定可返回测量的私有环境；
- private labels：真实事件类别、已知干扰标签、评分信息，仅评测器访问。

LLM只获取投影后的AgentObservationView。原始episode名、未来结果、hidden_state、ground_truth、expected_next_action不得进入模型输入。环境测量也不应包含“正确动作是E3”等暗示。

同事件同输入同参数返回相同数值；改变窗口/对照/质量/来源可得到不同或不适用结果。重复执行不逐渐让真值更显露。失败计数等技术状态与科学状态分开。

建议首批8类测试世界：
1. 跨月份差异经有效对照不再支持持续变化；
2. 恢复支路与覆盖充分的原始观测冲突；
3. 多期真实观测支持持续转换；
4. 所有可用观测不足，最终拒判；
5. 完全重复或部分同源证据；
6. 真实变化与恢复伪影共存；
7. 首次实验技术失败，重试后仍要科学判断；
8. 被初始检测漏掉的审查事件。

对LLM使用EP001等中性标识，内部标签另存。仅这些少量世界通过不构成论文benchmark结果。

## 7. 七类实验分步接入

| 实验 | P1 | P2及以后 |
|---|---|---|
| E1原始观测 | 可执行模拟，覆盖不足返回insufficient | 局部真实观测与共同支持范围 |
| E2同季节/物候对照 | 可执行模拟，窗口和截止校验 | 参数可组合，比较对照适用性 |
| E3持续性/恢复性 | 可执行模拟，真实采集ID计数、删失 | 时序模型适配 |
| E4 SAR可比性/持续性 | 注册契约，未实现明确不可用 | 可执行模拟，再接轨道内统计 |
| E5替代恢复 | 注册契约，未实现明确不可用 | 相同输入与替代模型比较、同源跟踪 |
| E6配准敏感性 | 注册契约，未实现明确不可用 | 受限于估计误差范围的扰动 |
| E7掩膜敏感性 | 注册契约，未实现明确不可用 | 固定阈值profile对照 |

不能注册为可用却返回伪成功；不可用实验不得出现在当前allowed_actions中。

## 8. LLM参与方式

继续采用现有兼容适配器，先增加公开结构输出方法。科学角色为应用层函数，不是每个角色一个独立服务：

- propose_hypotheses(visible_state, domain_catalog)
- propose_experiments(visible_state, hypotheses, allowed_parameters)
- reflect_on_results(precommitment, measurements, risk_snapshot)
- compose_claim(verified_claim_facts)

每一项都返回Pydantic对象和统一metadata。真实DeepSeek与第二provider复用相同语义prompt与schema。

LLM必须实际影响实验类型、时间对照或子区域参数。测试中替换为受控fake LLM，分别输出两种合法计划，确认执行器确实运行不同实验；不能workflow先决定路线、最后让LLM补理由。

策略与模型是两个独立实验轴。rule、enumerate_same_value等策略不需LLM；hypothesis与generic_react可切换不同LLM。科学比较不能只换报告写作模型。

## 9. 开发阶段与退出标准

### P0：审查与回归保护

复核真实代码与工作区；运行旧测试/演示；修复硬编码比例、错误Mock日志等确定问题；设计schema/模式迁移；记录网络重试/预算不足。退出条件：未破坏旧入口，有真实baseline验证记录。

### P1：最小科学闭环

新增科学契约、E1—E3模拟、事件图、预承诺、状态投影、启发式占位风险、假设和实验LLM调用、报告与新CLI分支。至少4核心世界离线运行；同源与截止单元测试。退出条件：可以重现正确支持、推翻初始解释、信息不足拒判三个不同终点。

### P2：LLM强化与完整实验库

完善DeepSeek/第二服务结构协议、逐尝试成本日志、prompt版本、E4—E7模拟和更复杂参数；扩展科学API与事件结果查看。退出条件：MockTransport证明profile切换无需改业务，用户授权时可运行真实LLM；失败无隐性fallback。

### P3：评测底座

相同世界、同数据权限、同动作域下比较固定/规则/通用ReAct/假设策略/枚举同价值。输出逐episode结果、轨迹成本、错误与空白论文表。退出条件：没有标签泄漏，模拟正确性与科学有效性分表；未执行/未拟合项明确跳过。

### P4：真实观测与学习型数值模块

P4a接本地标准化观测；P4b接一种恢复器和检测器并固定版本；P4c以真实事件拟合q_phi，校准与盲测隔离；P4d收集训练轨迹学习实验结果/价值；P4e冻结所有数值模块后做多LLM外部测试。一次只开发一个子阶段。

## 10. 测试与产物

保持legacy报告名和接口。科学运行新增：

```text
<task_id>/
  task.json
  effective_config.json
  execution_trace.jsonl
  llm_attempts.jsonl
  tool_results.json
  science/
    task_contract.json
    events.json
    sources.json
    budget_ledger.jsonl
    <event_id>/
      hypotheses.jsonl
      precommitments.jsonl
      experiments.jsonl
      evidence_graph.json
      risk_history.jsonl
      decision.json
  report_facts.json
  report.html
  artifacts/
```

P1 risk_history明确为启发式/未校准。每次结果写文件时校验其确实存在、hash一致、任务归属正确；不伪造GeoTIFF。

## 11. 目录建议

```text
src/eo_agent/
  api.py、cli.py、config.py、schemas.py、service.py   # 保留并扩展
  llm/                                             # 保留统一适配
  tools/                                           # 保留基础工具
  workflow/
    graph.py、nodes.py、state.py                    # legacy保留
    scientific_graph.py                            # 新增
  scientific/
    schemas.py
    context.py                                     # 可见投影和截止
    hypotheses.py
    precommitment.py
    provenance.py
    risk.py                                        # 接口+明确的占位
    value.py                                       # 接口+明确的占位
    policy.py
    experiments/
    environment.py
    engine.py
  reports/
    scientific_facts.py
    scientific_renderer.py
  evaluation/                                      # P3
  perception_adapters/                             # P4
```

可以合理合并小模块，禁止为了目录树创建大量空文件。关键边界、可执行链路和测试优先于文件数量。

## 12. 学术里程碑映射

| 学术主张 | 开发功能 | 必要证伪/对照 |
|---|---|---|
| 假设能影响取证 | 参数化实验提案、预承诺、反馈 | 固定计划、通用ReAct、相同输入的fake LLM干预 |
| 来源意识抑制过度自信 | 精确去重、根来源图、风险接口 | 完全复制与部分共享来源压力测试 |
| 主动策略节省预算 | 成本账本、价值接口与停止 | 随机/规则、枚举+相同价值、全实验高预算 |
| LLM具有迁移价值 | 统一schema、模型profile、冻结数值模块 | 非LLM学习策略、未见干扰组合、跨区域 |
| 真实监测收益 | M1—M3真数据、区域审查与真值 | 原始观测直检、固定恢复+检测、完整区域召回 |

## 13. 第一轮明确不做

不训练大模型、不同时训练三个恢复器、不部署MCP群/图数据库/Kubernetes、不做真实卫星调度、不承诺因果归因或严格FDR控制、不把缺少校准的数据写成高置信论文结论。所有新框架选择都应有具体必要性，不因“科研创新”增加软件层数。
