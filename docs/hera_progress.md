# HERA-Change 开发进度

## 当前阶段

- 阶段：P1 可离线运行的最小科学调查闭环。
- 状态：完成。
- 已完成：P0、P1。
- 明确未开始：P2、P3、P4。

## 基线记录（修改前）

- HEAD：`29d7633909cea282b442ad203398e92456cb6e47`。
- `pip install -e ".[dev]"`：沙箱网络首次失败；获准访问 PyPI 后成功。
- `pytest -q`：21 passed in 1.85s。
- `ruff check .`：失败 2 项，均为 import 排序（`config.py`、未跟踪 `test_deepseek.py`）。
- 离线演示：cloudy、clear、needs_evidence 均 COMPLETED；no_data 为 PARTIAL；退出码均为 0。
- 基线任务 ID：cloudy `42db2871b62c48c1ba05094de98f87e3`；clear
  `60fb1d6fe04d45bfb18e5def8052e235`；needs_evidence
  `9ce4a064a5cc408499ea3ec56b6ddb7a`；no_data `a859e9528ce64071901f05e58f6fc6e6`。
- PowerShell 捕获输出的中文受终端代码页影响出现乱码；UTF-8 报告和测试读取正常。

## P0 决策

1. legacy 保持默认且继续使用现有入口；P0 不暴露不可用的 scientific 选项。
2. 面积分母来自已校验任务契约，legacy 使用 `task_aoi_area`；未来可判读面积另用明确类别。
3. HTTP 尝试预算在适配器内预占，业务工作流继续只依赖 `LLMClient`。
4. 条件路由保持纯读，终态更新放显式节点。
5. 不修改 legacy needs_evidence 的演示规则，不运行真实 LLM 脚本。

## P0 新增验证

- 200 km² 合成 AOI 的比例分母与类别传播。
- fake provider metadata 不再被描述成 MockLLM。
- `max_calls=0` 时 HTTP 调用数为 0。
- 网络重试与 Schema 修复在下一次请求前受同一预算限制。
- insufficient 路径经过显式 `mark_partial` 节点。

## 最终验证记录

- `python -m pip install -e ".[dev]"`：成功；确认 `python-dotenv 1.2.2` 来自声明依赖。
- `python -m pytest -q`：最终复核 26 passed in 1.89s。
- `python -m ruff check .`：All checks passed。
- 最终 legacy 演示均为退出码 0：
  - cloudy：`42266a211cc9413ea661e1691a37da22`，COMPLETED；
  - clear：`0d077358bf2e43c2a6989f09e05c84b4`，COMPLETED；
  - needs_evidence：`9f648561ded14476bb1895fb32a270e5`，COMPLETED；
  - no_data：`c036665071b44fa8a45edc828c6245be`，PARTIAL，变化面积/比例/分母均为 null。
- 200 km² 分母演示：`1870589e458d4e65b54a03b8a5d32f2d`，变化面积 3.3 km²，
  比例 0.0165，分母 200 km²，类别 `task_aoi_area`。
- 上述五条最终任务均从 `outputs/p0_baseline/tasks.sqlite3` 查询成功。
- 默认未运行 `test_deepseek.py`，没有真实 API 请求。

## 阻塞与未验证

- 无真实 API 验证；未读取密钥，未访问付费服务。
- 无卫星影像、GPU 算法或科学准确性验证。
- 完整逐尝试成本 ledger 留到 P2；崩溃恢复未实现。

## P1 起点

从模式兼容和科学契约开始：先给 `TaskRequest` 增加默认 legacy 的模式字段，并在
`TaskService` 添加显式分发；随后创建独立 `scientific/schemas.py` 与
`workflow/scientific_graph.py`，实现 M0 任务契约、事件状态、白名单观察投影、预承诺和 E1—E3
模拟闭环。不得修改或复用 legacy 状态来伪装 scientific。

## P1 实现记录（2026-09-18）

- 公共请求新增默认 `legacy` 的 `workflow_mode` 和显式 `episode_id`；CLI/API/TaskService 共用同一
  分发入口，legacy 路径未替换。
- 新增独立 scientific LangGraph：提出假设、提出合法实验、保存预承诺、执行、摄取证据、反思
  修订、程序化继续/停止/拒判、生成声明。
- 实现确定性 E1 原始观测、E2 同季节对照、E3 持续性实验；结果由观测与登记参数决定。
- 实现任务/事件所有权来源 DAG、完全重复证据去重和共享根依赖；采集与可用时间同时受截止约束。
- 实现未训练启发式风险/动作排序；类别概率和预期风险下降均保持 null。
- 科学产物按事件保存，报告首屏和全部下游对象传播 `contains_mock=true`。
- OpenAI-compatible 通用结构输出入口仅做 `MockTransport` 离线协议验证；未发起真实 API 调用。

## P1 验证记录

- P1 开发前 legacy/provider 基线：`26 passed in 1.90s`。
- 新增不变量和集成测试后：`42 passed in 4.24s`（最终复核见本文件后续记录）。
- 已验证 EP001 冲突观测削弱初始解释、EP002 原始观测检查、EP003 持续性支持、EP004 覆盖不足
  拒判；EP004 使用 3 次实验、2 次重规划并在 8 次科学 LLM 预算处停止。
- 已验证不同合法 LLM 提案实际执行不同实验/参数、Episode 改名不改变模拟数值、修改观测而非
  私有标签会改变判断、预承诺轨迹先于实验调用。
- 最终 `python -m pytest -q`：44 passed in 4.32s；`python -m ruff check .`：All checks passed；
  `git diff --check` 无内容错误，仅提示仓库现有 Windows CRLF 转换警告。
- 最终 legacy CLI：cloudy 任务 `16b86604fd0d4b78803fd1dbf157fe18` 为 COMPLETED；no_data
  任务 `058d11bcbae24881874b02ace6a22b92` 为 PARTIAL，明确无法判断；退出码均为 0。
- 最终 scientific CLI（退出码均为 0）：EP001 `d032d59779d94a379defae65a2674a02` →
  transient_change/E2；EP002 `7494d51fe3ca4ac6b13c37767ca6d5ee` → stable/E1；EP003
  `25228f8e73ac499e961d528a0f160bf5` → persistent_change/E3；EP004
  `ac84622fd980451bb62edf00d719d47e` → abstain，依次执行 E1/E2/E3。
- 因最终模拟标记增强后复跑一次，`outputs/p1_science/tasks.sqlite3` 累计查询到 8 条 scientific
  COMPLETED 任务和 152 条产物索引；最新四个报告 HTML 与事件预承诺 JSONL 均实际存在。

## P1 未实现与 P2 起点

- E4—E7、真实数据连接、真实遥感算法、训练风险/价值模型、科学性能评价均未实现。
- 未进行真实兼容模型 API 验证；没有读取密钥或运行 `test_deepseek.py`。
- P2 应从完整逐 HTTP 尝试账本、通用科学提案协议强化、失败/重试成本记录和更多非法提案对抗
  测试开始；不得把 P1 启发式分数改名为概率。
