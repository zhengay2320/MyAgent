# MyAgent → HERA-Change：增量开发指令包

编制日期：2026-09-17。代码审查基线：`zhengay2320/MyAgent`，`main` 的提交 `29d7633909cea282b442ad203398e92456cb6e47`。

本包是开发计划、静态审查和 Codex 任务书，不是已经实现的源码补丁。编制时未修改远程仓库、未运行仓库测试，也未调用付费大模型。执行时必须重新检查本地 HEAD 和未提交修改，不能回退到该审查提交。

## 使用方式

把本包的 `hera_dev/` 文件夹放入 MyAgent 的 `docs/` 下，形成 `docs/hera_dev/`。不覆盖仓库现有 `AGENTS.md`、`README.md` 或 `CODEX_PHASE1_PROMPT.md`。

推荐顺序：

| 顺序 | Codex 读取的任务书 | 本次交付边界 |
|---|---|---|
| P0 | `CODEX_00_BASELINE.md` | 审查、回归、必要小修复、迁移约定 |
| P1 | `CODEX_01_SCIENTIFIC_LOOP.md` | 可离线执行的事件级科学调查闭环 |
| P2 | `CODEX_02_LLM_AND_EXPERIMENTS.md` | 真实多模型路径、七类实验、逐次调用记录 |
| P3 | `CODEX_03_EVALUATION.md` | 同环境多策略、多LLM评测与论文表导出 |
| P4 | `CODEX_04_REAL_DATA_AND_RISK.md` | 分小步接入真实数据、感知与学习型风险/价值模型 |

每个阶段均先读 `CODEX_GLOBAL_GUARDRAILS.md`、`DEVELOPMENT_PLAN.md` 与仓库各级 `AGENTS.md`。每轮只完成用户指定阶段，不把所有阶段一次性交给 Codex。

## 现在建议发送给 Codex 的第一条指令

```text
这是现有 MyAgent 的增量重构，不是创建新项目。请先读取仓库各级 AGENTS.md，以及
  docs/hera_dev/CODEX_GLOBAL_GUARDRAILS.md
  docs/hera_dev/REPO_AUDIT.md
  docs/hera_dev/DEVELOPMENT_PLAN.md
  docs/hera_dev/CODEX_00_BASELINE.md
执行 P0：核查当前代码，运行旧测试与演示，按任务书完成必要修复和迁移说明。
不要删除原框架，不执行付费API，不开始P1—P4。请直接修改真实文件和运行命令，
最终报告修改列表、实测结果与尚未验证内容。若审查基线与本地不同，以本地代码为准。
```

P0 完成后发送：

```text
继续在当前 MyAgent 上执行 docs/hera_dev/CODEX_01_SCIENTIFIC_LOOP.md。
先读 CODEX_GLOBAL_GUARDRAILS.md、DEVELOPMENT_PLAN.md、仓库 AGENTS.md 和
本地 docs/hera_progress.md。保持原 legacy CLI/API 与测试可用，新增 scientific
模式。实际实现假设—预承诺—实验—证据—修订—停止闭环，不把假设作为报告装饰。
本轮仅执行P1，不接真实遥感网络、不训练风险模型、不调用付费API。
```

后续阶段也按相同方式逐个指定任务书。不要只说“按照论文把所有功能都实现”。

## 首个科学闭环验收命令（P1完成后才应存在）

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
python -m ruff check .
python -m eo_agent demo --scenario cloudy --output-dir outputs/legacy
python -m eo_agent demo --workflow scientific --episode EP001 --model mock --output-dir outputs/hera
```

新的 `--workflow`、`--episode` 是待开发接口，不是目前仓库已经支持的命令。新模式默认模拟；旧命令无参数改动时仍走 legacy。切换默认模式应另行版本化。

## 最重要的完成定义

P1 的成功不是“报告写了多个假设”，而是：模型提出可检验的差异化计划，执行器真正按参数运行实验，结果加入来源图、触发假设修订或拒判，重复来源不虚增独立证据，完整过程留下可复核记录。

模拟实验只能证明系统行为与接口符合要求；不能证明遥感精度、LLM科学优势或置信度校准。没有实测数据的论文表必须留空。
