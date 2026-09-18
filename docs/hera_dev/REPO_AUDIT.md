# MyAgent 静态审查与迁移定位

审查基线：`29d7633909cea282b442ad203398e92456cb6e47`；日期：2026-09-17。
仓库：https://github.com/zhengay2320/MyAgent

本审查通过 GitHub 读取 README、AGENTS、目录、主要工作流、LLM适配器、模拟工具、服务、报告和集成测试。未在本地安装或运行该仓库；README 的测试说明不等于本次实测。以下源码结论限定于该快照，Codex实施前须复核。

## 一、可保留的工程基础

- 包名 `eo_agent` 与 Python 3.11+ 项目布局；`python -m eo_agent` CLI。
- `src/eo_agent/api.py`、`cli.py` 共用 `service.py` 的应用入口。
- `workflow/graph.py` 的真实 LangGraph 状态图；不是只输出固定报告。
- `llm/base.py`、`llm/factory.py`、`llm/mock.py`、`llm/openai_compatible.py`。
- `configs/models.yaml` 的 `mock`、`deepseek_dev`、`second_compatible` 模型配置方式。
- `tools/registry.py`、`tools/executor.py` 的注册、阶段和参数检查。
- `storage/` 的 SQLite 和文件产物；`reports/` 的事实表和HTML生成。
- `tests/integration/test_workflow.py` 中离线分支、补证、无数据、隔离与产物回归测试。
- 根目录 `AGENTS.md` 中不擅改区域时间、不静默回退、不暴露任意执行的约束。

这些能力应作为基础设施或旧基线保留，不重新包装为论文创新。

## 二、需要针对性修改的内容

| 编号 | 实际文件/行为 | 对新研究的影响 | 建议与阶段 |
|---|---|---|---|
| A01 | `workflow/graph.py`：parse→validate→search→prepare→quality→choose_reconstruction→detect→verify；补证返回prepare | 固定流水线，不具备事件级实验设计 | 保留legacy图；P1新增scientific图与事件子流程 |
| A02 | `workflow/nodes.py`：LLM主要选重建/不重建、补证/保持partial | 语言推理无法对实验目标、时间对照和参数产生实质作用 | P1/P2新增假设提出、实验提案、结果反思三类结构化调用 |
| A03 | `schemas.py`：没有Hypothesis、Experiment、EvidenceGraph、EventState等类型；事件是宽松字典 | 很难做学术约束和事件级评测 | P1在`scientific/schemas.py`新增类型，不让同名schemas.py和schemas/并存 |
| A04 | `llm/base.py` 只提供parse_task/choose_action/summarize；适配器已有泛型私有`_invoke` | 能复用传输，但缺通用公开结构输出接口 | 新增公开generate_structured，并让旧方法保持兼容 |
| A05 | `mock_tools.py`：needs_evidence在round_no>0时改为sufficient | 只能验证回环，不能证明取证有效 | 保留legacy测试；scientific环境按观测与实验参数返回测量，不按轮数判成功 |
| A06 | `mock_tools.py`：变化面积由固定seed产生，比例使用area/100 | 与不同AOI面积或可判读面积不一致 | P0修正分母来源与单元测试；P1统一显式area_denominator |
| A07 | `WorkflowNodes._record_llm`：响应返回后累加调用数并检查预算 | 可能先发生外部调用再发现已超额 | P0/P2在每次HTTP尝试前预占额度；重试/修复逐次记录 |
| A08 | `openai_compatible.py`：返回成功时主要保留最终响应usage；终止异常没有完整逐尝试ledger | 多模型成本、失败率与延迟对比不完整 | P2增加attempt-level observer/ledger，未知usage记null，不记成0 |
| A09 | `WorkflowNodes.parse` 的步骤文本固定称MockLLM规则解析器 | 真模型运行也会被错误描述 | P0按实际metadata生成说明 |
| A10 | `service.py`、`tools/executor.py`、`reports/facts.py` 多处固定contains_mock=True和.mock.json | 现阶段安全，但真实模块接入后不能区分混合来源 | P1/P4按产物依赖传播；未知来源保守处理，不直接清除标记 |
| A11 | `service.py`：主要在graph.invoke结束或异常后写任务与轨迹；README明确无checkpointer | 完成文件持久化不等于断点恢复，复杂调查可能缺中间耐久记录 | P1逐事件/逐步骤追加记录；完整resume另行验收，不先宣称已实现 |
| A12 | `reports/facts.py`：扫描结果取最后一个ChangeData和VerificationData | 多事件、多版本结果可能被压成最后一个输出 | 新增科学报告事实，按事件与版本聚合并保留支持/冲突 |
| A13 | `after_verify` 路由函数对state写状态 | 新图不宜依赖路由副作用来持久化状态 | 新图采用显式update节点和纯路由；旧图经回归再调整 |
| A14 | README与代码只支持两时期任务，连续监测明确拒绝 | 不能只加任务枚举就声称实现在线时序 | P1先做事件历史调查和截止测试，P4再接真实连续时序 |
| A15 | 仓库跟踪了部分IDE配置 | 不应把本机连接或部署元数据扩散进提示包/日志 | 审查时不复述值；按项目授权清理跟踪范围，不重写历史 |

## 三、需要保留的测试语义

`test_workflow.py` 已包含：clear跳过去云，cloudy调用去云；补证产生新资源并重新检测；no_data为PARTIAL且面积为null；有限重试；模拟标记、产物和SQLite；任务之间隔离；非法输入先于工具停止；HTML转义。

旧测试在legacy模式保留。不要把“补证一轮成功”的旧fixture规则偷偷用于scientific模式。若修复原测试暴露的错误，记录修改理由，不删除断言来制造通过。

## 四、具体源码核查链接

所有链接固定于审查提交：

- [README](https://github.com/zhengay2320/MyAgent/blob/29d7633909cea282b442ad203398e92456cb6e47/README.md)
- [AGENTS](https://github.com/zhengay2320/MyAgent/blob/29d7633909cea282b442ad203398e92456cb6e47/AGENTS.md)
- [工作流图](https://github.com/zhengay2320/MyAgent/blob/29d7633909cea282b442ad203398e92456cb6e47/src/eo_agent/workflow/graph.py)
- [工作流节点](https://github.com/zhengay2320/MyAgent/blob/29d7633909cea282b442ad203398e92456cb6e47/src/eo_agent/workflow/nodes.py)
- [数据契约](https://github.com/zhengay2320/MyAgent/blob/29d7633909cea282b442ad203398e92456cb6e47/src/eo_agent/schemas.py)
- [LLM边界](https://github.com/zhengay2320/MyAgent/blob/29d7633909cea282b442ad203398e92456cb6e47/src/eo_agent/llm/base.py)
- [兼容适配器](https://github.com/zhengay2320/MyAgent/blob/29d7633909cea282b442ad203398e92456cb6e47/src/eo_agent/llm/openai_compatible.py)
- [模拟工具](https://github.com/zhengay2320/MyAgent/blob/29d7633909cea282b442ad203398e92456cb6e47/src/eo_agent/tools/mock_tools.py)
- [工具执行器](https://github.com/zhengay2320/MyAgent/blob/29d7633909cea282b442ad203398e92456cb6e47/src/eo_agent/tools/executor.py)
- [服务](https://github.com/zhengay2320/MyAgent/blob/29d7633909cea282b442ad203398e92456cb6e47/src/eo_agent/service.py)
- [报告事实](https://github.com/zhengay2320/MyAgent/blob/29d7633909cea282b442ad203398e92456cb6e47/src/eo_agent/reports/facts.py)
- [集成测试](https://github.com/zhengay2320/MyAgent/blob/29d7633909cea282b442ad203398e92456cb6e47/tests/integration/test_workflow.py)

## 五、审查边界

本包没有“所有测试通过”“算法精度达标”或“当前模型最优”的结论。后续技术依赖以实施时官方文档、项目锁定版本和真实验证为准。科研方法以用户提供的HERA-Change细化稿M0—M9为依据；这里将其转换为可交付需求，而不是追加未经验证的文献主张。
