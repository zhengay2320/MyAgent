# Codex 开发指令：EO-Agent V0.1 可运行框架

请在当前工作目录直接实现一个可运行的 Python 项目，而不是只输出设计文档、伪代码或目录树。项目名称为 eo-agent，Python 包名为 eo_agent。

## 1. 本轮目标与边界

实现：自然语言任务 → 结构化解析 → 参数校验 → 模拟数据查询 → 模拟预处理 → 条件性去云 → 模拟变化检测 → 核验与有限补证 → 结构化报告 → 执行记录。

本轮只要求“两时期地表变化对比”闭环。连续长时序监测、真实遥感算法和正式大模型效果评测放到后续。

默认使用 MockLLM 和模拟遥感工具。安装依赖后，不需要 API Key、网络、卫星影像、GPU 或外部服务，就能跑通演示和全部默认测试。

同时实现可选的 OpenAI-compatible LLM 适配器，为开发阶段接入 DeepSeek 和其他兼容服务做准备。真实 API 调用不是本轮离线验收前提；没有密钥时不能假装已经验证在线模型。

必须完成：项目文件、可执行代码、CLI、最小 FastAPI 接口、SQLite/文件持久化、HTML 报告、测试、README。

本轮不引入 PyTorch、GDAL、Earth Engine、PostGIS、Redis、Celery、Docker、MCP、向量数据库或多智能体团队。不做正式前端，先使用 CLI、FastAPI /docs 和 HTML 报告。

## 2. 工作方式

- 先检查现有文件、Git 状态和已有 AGENTS.md，遵守现有仓库规范；不要覆盖用户已有修改。
- 以当前目录为项目根，不再额外嵌套一层 eo-agent。已有项目则做增量修改。
- 简述不超过 6 步的计划，然后直接实现，不停留在方案阶段。
- 细节采用合理默认值并写入文档；不要因未确定真实遥感算法而阻塞开发。
- 新建或合并简短的项目级 AGENTS.md，写清运行命令、测试命令、Mock 标识和接口边界；不修改全局 Codex 配置或权限。
- 在允许的本地虚拟环境中安装依赖、运行演示和测试，修复本次修改造成的问题。不要绕过环境权限或网络限制。
- 不执行远程部署、自动提交、推送或真实付费 LLM 调用。
- 如果环境阻止安装或测试，仍完成可完成的代码，并明确记录未运行项与错误，不能编造通过结果。

## 3. 技术栈与工程约定

使用 Python 3.11+、Pydantic v2、LangGraph、FastAPI、Uvicorn、httpx、PyYAML、Jinja2；SQLite 使用标准库 sqlite3。命令行优先使用 argparse，测试使用 pytest，静态检查使用 Ruff。

可使用 python-dotenv，但只能在明确的程序启动入口加载配置。不要在模块导入时创建数据库、调用网络或启动任务。

使用 pyproject.toml 和 src 布局，支持 python -m pip install -e ".[dev]"。选择并验证兼容依赖，记录实际测试版本；不伪造未验证的锁文件。

代码采用类型注解。文档、错误提示和示例以中文为主；类名、函数名和字段用英文。

目录可适当合并，但必须保留下面的职责边界：

    pyproject.toml
    README.md
    AGENTS.md
    .env.example
    .gitignore
    configs/
        app.yaml
        models.yaml
    fixtures/
        aois.json
        scenarios/
    prompts/
    src/eo_agent/
        __init__.py
        __main__.py
        cli.py
        config.py
        schemas.py
        service.py
        api.py
        workflow/
            graph.py
            nodes.py
            state.py
        llm/
            base.py
            factory.py
            mock.py
            openai_compatible.py
        tools/
            base.py
            registry.py
            executor.py
            mock_tools.py
        storage/
            repository.py
            artifacts.py
        reports/
            facts.py
            renderer.py
            templates/report.html.j2
    tests/
        unit/
        integration/
        provider_contract/
    docs/
        architecture.md
        verification.md
    outputs/                 # 运行时生成，不提交

不要为了目录完整创建大量空文件；允许合理合并。抽象接口可以使用 Protocol/ABC，已注册的实现不能只有 pass、TODO 或返回 None。

## 4. 统一数据契约

至少实现以下 Pydantic 类型，并为外部输入设置 extra="forbid"：

1. TaskRequest：query、可选 aoi_id、scenario、model_profile。
2. TaskDraft：LLM 解析阶段的候选字段，允许缺失；不能直接执行。
3. TaskSpec：经过程序校验的任务，包含 task_id、原始指令、已登记 AOI、基准期、目标期、网格、预算与版本。
4. ActionSpec：action、arguments、reason_summary；参数按动作使用专用 Schema 校验。
5. ToolResult：status、tool_name、implementation_id、schema_version、data、artifacts、contains_mock、scientific_validity、warnings、error、provenance。
6. ArtifactRef：artifact_id、相对路径、media_type、校验值、contains_mock。
7. ReportFacts：范围、时间、执行步骤、变化事件、模拟统计、核验状态、限制和产物引用。
8. TraceEvent：事件序号、task_id、阶段、事件类型、实现/模型标识、输入输出摘要、耗时、错误与用量。

ToolResult 外层统一，data 按工具使用专用类型，不把所有内容都塞成无约束 dict。

执行成功与科学有效性分开：占位工具可以 status=succeeded，但必须 contains_mock=true、scientific_validity=not_evaluated。下游只要依赖模拟输入，就必须继承模拟标识。

内部时间窗口统一左闭右开，禁止颠倒或空区间。示例“2024年9月”表示 [2024-09-01, 2024-10-01)，“2025年10月”表示 [2025-10-01, 2025-11-01)。保留月度代表状态语义，不虚构月中某日观测。

## 5. 任务解析与范围限制

主示例指令：对武汉演示区2025年10月与2024年9月进行变化检测。

fixtures/aois.json 登记 wuhan_demo_100km2：名称为“武汉演示区”，面积字段为 100，明确标注为合成测试元数据，不代表武汉行政边界或真实面积测量。另准备面积 500 平方公里的合成测试 AOI，用于超限测试。

默认网格 20 米、普通任务面积软上限 400 平方公里，全部放配置文件，不写死在提示词。

仅输入“武汉”且没有明确 aoi_id 时，返回 WAITING_INPUT 和待确认字段；不能自动缩为演示区。显式指定的 AOI 与文本明显矛盾时也不应静默覆盖。

未知区域、缺日期或模糊任务不得调用遥感工具。面积超限进入 WAITING_INPUT，说明需要缩小或后续批处理能力；不得悄悄裁剪。非法日期返回明确验证错误。

只有“两时期变化对比”可执行。“从A至B连续监测”等未实现任务返回 UNSUPPORTED_TASK_TYPE，不伪装成已完成长时序分析。

MockLLM 可以使用规则或正则支持上述中英文日期示例及固定测试用例，但要明确它只是模拟解析器，不声称具备通用语义理解。

## 6. 可切换 LLM 接口

业务节点只依赖统一 LLMClient，不直接访问供应商 SDK 或具体模型名称。

统一接口至少支持：
- parse_task：生成 TaskDraft。
- choose_action：在 allowed_actions 中选择 ActionSpec。
- summarize：依据 ReportFacts 生成简短报告文字。

返回对象同时包含结构化载荷与调用元数据，便于后续多模型比较。

实现 MockLLMAdapter 和 OpenAICompatibleAdapter：

- Mock 为默认，不读取密钥、不访问网络；根据当前可见任务和工具反馈决策。
- 兼容适配器使用 httpx 调用配置 API 前缀下的 /chat/completions；处理前缀尾斜杠，不重复拼接 /v1。
- 提供 mock、deepseek_dev、second_compatible 三个可配置 profile。后两者从各自环境变量读取模型 ID、API 前缀和密钥，不写死模型版本。
- DeepSeek 在 .env.example 使用 DEEPSEEK_API_KEY、DEEPSEEK_BASE_URL、DEEPSEEK_MODEL；前缀示例为 https://api.deepseek.com/v1，模型 ID 由用户填写。第二服务使用 SECOND_API_KEY、SECOND_BASE_URL、SECOND_MODEL。只显式选择相应 profile 时校验密钥，Mock 模式不要求这些变量存在。
- 服务地址、密钥只能来自可信配置，LLM 或普通任务参数不能指定任意服务地址。
- 输出协议首版统一为 JSON 动作；支持按 profile 开关 JSON response_format，关闭时仍提示 JSON 并由本地校验。
- 不假定所有模型都支持相同生成参数。仅发送配置中明确声明的参数，保存生效参数。
- 空响应、非法 JSON、Schema 不匹配应明确报错；最多允许一次带校验错误的格式修复，不使用 eval，不无声修正业务字段。
- 网络失败有限重试；401/403 等配置或权限错误不要盲目重试。失败后不得静默切换模型或转 Mock。
- 默认不发起真实 API 调用；真实运行必须显式选择 profile 且配置完整。缺少配置时启动前给出缺失项。
- 用 httpx MockTransport 或等价方式测试兼容接口、超时、鉴权错误、空响应及格式修复，不调用付费服务。

记录请求模型名、返回模型名（若有）、provider、profile、提示词版本、输出协议、用量、耗时、重试、格式修复和错误。缺失的 Token/价格记录 null；Mock 不伪造真实 Token 或性能。

日志和 effective_config 禁止出现密钥、Authorization 头或完整敏感环境变量。不保存供应商内部思维链，仅保存最终输出、简短动作理由及应用工具轨迹。

## 7. 工具注册与模拟场景

实现明确的工具注册表和统一执行器。建议工具为：

- search_observations
- prepare_data
- inspect_quality
- reconstruct_optical
- detect_change
- verify_change
- fetch_additional_observation

所有工具接受资源 ID，不接受由 LLM 任意拼出的文件路径、Python 代码或 Shell 命令。前置条件、参数和资源归属在执行器校验。

输入输出数据量保持很小，可使用 JSON 清单和模拟事件，不要求生成栅格。不要返回不存在的 GeoTIFF，也不要为了演示假装下载过 Sentinel 数据。

至少提供六个场景：

1. clear：质量充分，跳过去云，完成检测和报告。
2. cloudy：质量不足，执行去云后完成；作为默认 demo。
3. needs_evidence：首次核验要求补证；追加观测确实更新输入，重跑受影响的预处理/检测/核验后完成。
4. no_data：返回无数据，生成 PARTIAL 报告；变化面积应为 null/无法判断，不能写成零变化。
5. temporary_failure：某工具首次产生可重试错误，允许一次重试后完成，轨迹记录两次尝试。
6. insufficient：补证后仍不足，到达预算后输出 PARTIAL，不得无限循环。

场景 ID 和参考答案不发送给 LLM；模型只看到工具返回的可见质量和证据。Mock 决策也不得读取场景名来偷取正确路线。

模拟结果由场景种子、工具和规范化输入确定。相同可见状态下执行同动作，领域数据相同；UUID、时间戳等运行元数据除外。固定种子从任务配置传入，不能使用 task_id 作种子。不使用 Python 随进程变化的 hash() 作为稳定种子。

每次任务拥有独立场景状态；首次失败计数、补证次数等不能跨任务污染。不同模型运行不得共享前一个模型的补证状态。

## 8. LangGraph 工作流

真正使用 StateGraph 和条件边执行流程，而不是导入 LangGraph 后仍在 API 中写完整顺序流程。

固定主阶段：
parse → validate → search → prepare → quality → choose_reconstruction → [reconstruct 或跳过] → detect → verify → [报告 或选择补证]。

补证后更新数据引用，再回到 prepare 等受影响阶段，不能只修改最终 verdict 伪装成重新分析。

LLM 只控制需要重建/跳过、追加证据/保留结论等有限决策。工具执行器和图控制阶段依赖；不得直接跳过校验或检测后生成“可信结论”。

状态至少包含：任务、当前阶段、允许动作、数据引用、工具结果、事件、调用计数、补证计数、错误、模拟标识。

状态枚举至少支持 CREATED、RUNNING、WAITING_INPUT、REPORTING、COMPLETED、PARTIAL、FAILED。细分阶段放 stage 字段。

程序执行预算：max_tool_calls=20（包含重试）、max_replans=2、max_tool_retries=1、max_schema_repairs=1，并设置合理的总 LLM 调用上限；均可配置。业务上限与 LangGraph recursion_limit 分开，防止合法流程被图递归默认值提前中断。

预算耗尽且已有可展示信息时生成 PARTIAL 报告；程序故障生成 FAILED 记录和错误摘要。禁止吞异常后返回成功。

本轮不实现进程中断后的图恢复或异步队列；持久化任务与产物不等于支持断点续跑，文档中明确区分。

## 9. 持久化、报告与产物

SQLite 保存任务和结果索引；每个任务在输出根下建立独立 task_id 目录。数据库连接按操作创建并释放，不跨 FastAPI 线程复用一个全局连接。

报告阶段完成后至少存在：

    task.json
    effective_config.json        # 脱敏
    execution_trace.jsonl
    tool_results.json
    report_facts.json
    report.md
    report.html
    artifacts/                  # 必要的模拟数据/事件 JSON

报告必须在首屏固定显示：
“本报告为系统流程演示，包含模拟数据或占位算法输出，不代表研究区域的真实地表变化。”

报告包含任务范围、时间、LLM profile、各步骤实现、模拟事件、质量/核验状态、限制、调用次数与产物。数字由程序从 ReportFacts 填入，不让 LLM 随意重新生成。

LLM 只生成短说明；新增事实或数字不通过校验时使用明确标识的固定模板，并记录报告文字降级，不伪装成模型成功。核心任务解析/决策失败不得以模板偷偷替代。

HTML 开启转义，不把用户指令或模型输出直接当作可信 HTML。无需外部字体、CDN、底图或远程脚本。

返回的每个 ArtifactRef 都对应真实文件且限定在所属任务目录。下载通过 artifact_id 查库映射，不接受任意文件路径，不暴露密钥文件。重复执行不覆盖已有任务。

## 10. CLI 与最小 API

实现以下命令，CLI 与 API 必须复用同一个 TaskService：

    python -m eo_agent demo --scenario cloudy --output-dir outputs/demo
    python -m eo_agent demo --scenario clear --output-dir outputs/demo
    python -m eo_agent demo --scenario needs_evidence --output-dir outputs/demo
    python -m eo_agent demo --scenario no_data --output-dir outputs/demo
    python -m eo_agent serve --host 127.0.0.1 --port 8000 --output-dir outputs/api

另提供 run 子命令，支持 --query、--aoi-id、--scenario、--model、--output-dir。demo 自动使用主示例 query 和 wuhan_demo_100km2。

CLI 输出 task_id、终态、contains_mock、执行步骤、报告文件绝对路径。COMPLETED 返回退出码 0；PARTIAL 也允许返回 0 但必须明确标出；FAILED 返回非零；WAITING_INPUT 使用单独非零码并说明。

FastAPI 提供：

- GET /health
- POST /api/tasks/run：首版同步执行，返回 task_id、状态、模拟标识和报告引用。
- GET /api/tasks/{task_id}
- GET /api/tasks/{task_id}/trace
- GET /api/tasks/{task_id}/report?format=html|json
- GET /api/tasks/{task_id}/artifacts/{artifact_id}

POST 输入示例：
    {"query":"对武汉演示区2025年10月与2024年9月进行变化检测","aoi_id":"wuhan_demo_100km2","scenario":"cloudy","model_profile":"mock"}

不要返回 202 并假装已有后台任务系统。启动时不连接真实 LLM。同步接口只用于本地原型，README 明确不面向长时间计算和公网多用户服务。

## 11. 测试与验收

测试使用临时目录和临时 SQLite，不污染正式 outputs。默认测试禁止外网；HTTP 服务仅用本地 TestClient 或 MockTransport。

至少覆盖：
- clear 跳过去云，cloudy 执行去云，且真实调用注册工具而不是直接返回固定报告。
- needs_evidence 的补证更新输入并触发重新处理。
- no_data 不报告“未变化”；insufficient 有限终止；temporary_failure 记录重试。
- 缺日期、未知/歧义 AOI、面积超限和非法日期在工具执行前被阻止。
- 模拟标识一直传递到 JSON/HTML，程序成功不被当成科学可信。
- 未注册工具、未知参数、越阶段动作、资源不属于当前任务被拒绝。
- 相同种子领域结果可重复，两个任务之间状态隔离。
- JSON 错误有限修复、缺少真实模型配置明确失败、不静默换模型。
- 两套不同兼容 profile 在 MockTransport 中构造正确的模型/地址请求，且不修改业务代码；这只能证明适配契约，不能标为真实跨模型验证。
- 所有产物存在且可读取；路径越界下载被阻止；HTML 注入内容被转义。
- CLI、API 主流程、SQLite 查询、日志和报告数字一致性。
- 通过替换 registry 中的测试工具实现改变输出，无需修改工作流，证明算法可替换。

不要只测试“文件存在”或“status=completed”；必须断言关键动作、结果和约束。不得为了通过测试删除验收条件或写始终为真的断言。

完成后实际执行并记录：

    python -m pip install -e ".[dev]"
    python -m pytest -q
    python -m ruff check .
    python -m eo_agent demo --scenario cloudy --output-dir outputs/demo
    python -m eo_agent demo --scenario clear --output-dir outputs/demo
    python -m eo_agent demo --scenario needs_evidence --output-dir outputs/demo
    python -m eo_agent demo --scenario no_data --output-dir outputs/demo

用 TestClient 验证同步 API；条件允许再启动本地服务做一次 /health 冒烟检查，结束后关闭本次启动的服务。没有真实密钥时不要尝试线上 LLM 验收。

## 12. 交付说明

README 包含虚拟环境、安装、CLI、API /docs、请求示例、报告位置、DeepSeek 环境变量配置、故障排查以及添加真实算法/新 LLM 的方法。提供 Windows PowerShell 与 Linux/macOS 的环境激活说明。

architecture.md 说明模块边界和流程；verification.md 写入真实测试命令、结果、未验证项。提示词和配置均版本化，便于之后批量比较多个 LLM，但本轮不实现正式模型排行榜。

完成后用中文总结：创建/修改的主要文件、实际执行的命令和结果、默认 demo 报告路径、启动命令、DeepSeek 配置方式、哪些部分仍为模拟以及尚未验证的内容。

请立即开始实现，并以“默认离线可跑通、接口可替换、验证结果真实”为完成标准。
