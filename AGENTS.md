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

