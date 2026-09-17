# 验证记录

验证日期：2026-09-17；平台：Windows；解释器：Python 3.11.15。

实测依赖版本：FastAPI 0.141.1、httpx 0.28.1、Jinja2 3.1.6、LangGraph 1.2.11、
Pydantic 2.13.4、PyYAML 6.0.3、Uvicorn 0.49.0、pytest 8.4.2、Ruff 0.16.8。

已执行：

```text
python -m pip install -e ".[dev]"   -> 成功
python -m pytest -q                 -> 21 passed in 1.77s
python -m ruff check .              -> All checks passed!
```

首次受限网络安装失败后，获得安装依赖许可并重试成功。安装日志还提示该既有 Python 环境中的
另一个无关包 `rs-mcp-agent` 缺少其可选地理依赖；EO-Agent 本身的安装、测试和运行未依赖这些包。

四个 CLI 场景最终复跑均为退出码 0：

| 场景 | 任务 ID | 状态 | 关键核验 |
|---|---|---|---|
| cloudy | `2ce86cea9937402aa7ab47e06b18d103` | COMPLETED | 含 `reconstruct_optical` |
| clear | `3833594915ec401ab408385c9fa3e5f1` | COMPLETED | 跳过 `reconstruct_optical` |
| needs_evidence | `751dca76bda64ae09b73ae9f9f6bd55f` | COMPLETED | 补证后重新 prepare/detect/verify |
| no_data | `2adcdada28964115ba11d37e3254dc6a` | PARTIAL | `change_area_km2=null`，unavailable |

最新默认多云报告：
`outputs/demo/2ce86cea9937402aa7ab47e06b18d103/report.html`。四个 HTML 均实际存在，四条任务均从
`outputs/demo/tasks.sqlite3` 查询成功。

API 已用 FastAPI `TestClient` 验证健康检查、同步执行、任务/轨迹/JSON/HTML 报告和非法产物 ID。
另实际启动 Uvicorn 于 `127.0.0.1:8765`，`GET /health` 返回 HTTP 200 与
`{"status":"ok","mode":"offline-first"}`，随后已停止该进程。
兼容 LLM 已用 `httpx.MockTransport` 验证两套 profile 的地址、模型、鉴权头、JSON 模式开关、
有限格式修复、超时重试、401 不重试和空响应失败。

未验证：未配置或调用真实 DeepSeek/第二服务，不存在真实 API、模型效果或跨模型性能结论；
没有运行真实遥感算法、真实影像或科学精度评估。
