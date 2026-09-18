# Codex P0：基线保护、针对性修复与迁移准备

请直接在现有MyAgent工作区实现本阶段。先读各级AGENTS.md、CODEX_GLOBAL_GUARDRAILS.md、REPO_AUDIT.md、DEVELOPMENT_PLAN.md。不新建项目，不执行P1—P4，不只输出建议。

## 目标

在保持EO-Agent V0.1现有行为可用的前提下，建立准确基线，修复已核实的小问题，为新增scientific模式准备明确扩展点。

## 1. 先审查再修改

运行并记录git status、git rev-parse HEAD；不要打印敏感配置。读取实际：

- README.md、AGENTS.md、pyproject.toml；
- src/eo_agent/schemas.py、service.py、config.py、api.py、cli.py；
- workflow/graph.py、nodes.py、state.py；
- llm/base.py、factory.py、mock.py、openai_compatible.py；
- tools/base.py、registry.py、executor.py、mock_tools.py；
- storage/、reports/、tests/。

核对当前HEAD与审查提交是否一致；有差异时基于本地现实重新定位，不回退代码。

## 2. 运行已有验证

在允许的依赖安装条件下执行：

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
python -m ruff check .
python -m eo_agent demo --scenario cloudy --output-dir outputs/p0_baseline
python -m eo_agent demo --scenario clear --output-dir outputs/p0_baseline
python -m eo_agent demo --scenario needs_evidence --output-dir outputs/p0_baseline
python -m eo_agent demo --scenario no_data --output-dir outputs/p0_baseline
```

安装失败、环境不可达或没有依赖时明确记录阻塞，不用假结果填验证文档。不调用DeepSeek或其他付费服务。

## 3. 定点修复

只修复复核确实存在的问题，并补测试：

A. change比例不应固定以100km²作为分母。把可信TaskSpec面积或明确的可判读面积通过ToolContext传给工具，保留分母类别；对非100km²合成AOI测试。不要扩大LLM权限让其填写面积。

B. parse步骤不能在真实provider下固定显示“MockLLM规则解析器”。使用实际metadata标识。

C. 增加LLM预算预检查。最少保证每次真实HTTP尝试前检查/预占额度，覆盖网络重试与Schema修复；max_calls=0时HTTP MockTransport应收到0次请求。若使用callback/observer应保持旧LLM接口可兼容，不让业务层import httpx或SDK。P2将扩展完整attempt账本，但本轮不能保留先调用后检查的硬预算问题。

D. 若路由函数依赖state写入来改变终态，改为显式更新节点或经验证的state返回路径；只在有回归测试的情况下修改legacy图。

E. 可逐步追加事件记录以保留失败前信息，但不要在没有resume测试时声称断点恢复已完成。

F. 除明确小修复外，不重写旧mock场景，不删除旧集成测试；旧needs_evidence用于流程演示的语义留在legacy。

## 4. 形成迁移文档

新增/更新：

- docs/hera_migration_audit.md：实际模块、保留/修改/新增、已验证问题和未验证问题；
- docs/hera_progress.md：P0状态、命令、结果、决策、阻塞、下阶段入口；
- docs/architecture.md：legacy仍在，scientific为下一阶段计划；
- AGENTS.md：追加模式兼容、科学分数不冒充概率、隐藏标签隔离等稳定规则；
- README：准确说明目前仍未实现科学模式，不提前列为可用功能。

不要移动或覆盖历史CODEX_PHASE1_PROMPT.md；注明它是V0.1历史任务书，后续科学要求来自新任务包。

## 5. 退出条件

旧测试和演示实际通过，或明确保留环境阻塞与失败日志；新增测试覆盖本轮修复；未改变包名、旧CLI、API字段默认值和数据目录；没有真实API请求。

最终中文回复：修改文件、复核的问题、运行命令与结果、是否仍有失败、下一阶段建议。停止，不自动执行P1。
