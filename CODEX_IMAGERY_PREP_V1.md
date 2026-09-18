# Codex 实施任务：MyAgent 交互式影像准备 V1

> 本文件是开发指令。请在现有 MyAgent 工作区创建、修改真实代码并执行测试，不要只生成方案、目录树或伪代码。
> 本轮只完成任务拆解、影像检索、预览、质量检查、SAR 建议、用户确认与本地下载。不要继续实现去云、变化检测、原因归因或论文方法。
> 参考审查日期：2026-09-18。参考远程提交：`5b8990c878eb77afb8074475156e5c716cbedc9e`。必须以当前工作区为准，不得回退到该提交。

## 0. 开始前与任务边界

1. 阅读当前及上级目录适用的 `AGENTS.md`、README、pyproject.toml；检查 `git status`、当前 HEAD 和现有文件。
2. 保留用户未提交修改；不重建项目、不重命名 `eo_agent` 包，不覆盖历史任务书，不自动提交或推送。
3. 保留现有 `legacy`、`scientific`、CLI、API、TaskService 和旧测试。新增独立 imagery 功能，不改变旧请求默认语义。
4. 优先复用现有 FastAPI、Pydantic、LLM 兼容适配器、配置、文件与 SQLite 能力。不将已有 `api.py` 或 `schemas.py` 改成同名目录。
5. 缺少真实凭据不是省略真实实现的理由：完成真实 Earth Engine 适配器与测试替身；真实验证单独标为未验证。不得静默退回 mock。
6. 开发测试默认不访问外网、不自动调用收费模型、不自动认证、不下载真实影像。依赖安装与官方文档查阅按当前 Codex 环境权限处理；运行真实服务需显式配置与授权。
7. 不新增 PyTorch、去云模型、变化检测模型、MCP、Redis、Celery、PostGIS、复杂多智能体或 Node 前端工程。

## 1. 用户最终要看到什么

启动现有服务后，在 `/imagery` 打开中文页面：

输入任务、选择研究区与模型
→ 看见大模型实际输入及输出
→ 看见光学候选列表、缩略图与研究区质量
→ 看见模型选择影像和是否检索 SAR 的建议
→ 必要时看到 SAR 候选与时间匹配
→ 勾选下载文件并修改保存目录
→ 点击确认才开始正式下载
→ 实时看到逐文件进度与校验
→ 打开本地缩略图与下载清单。

必须同时具备：
- `mock` 数据后端与 `mock` LLM 的离线完整演示；
- `gee` 真实数据后端的实际代码；
- 现有 DeepSeek/第二兼容模型 profile 在新流程中的真实调用路径；
- 数据后端与 LLM profile 独立配置。真实影像配 mock planner 要分别标记，不能把真实影像伪称模拟，或把模拟影像伪称真实。

## 2. 采用一个页面和一套服务，不再拆成大平台

建议新增 `src/eo_agent/imagery/`，按职责组织：

```text
imagery/
  schemas.py         # 本流程独立契约
  service.py         # 分阶段执行、等待确认、继续、取消
  router.py          # API和页面接入
  repository.py      # 任务、计划、确认、事件和文件记录
  events.py          # 持久化事件与SSE
  planner.py         # LLM任务解析、选图、SAR建议
  aoi.py             # GeoJSON与真实范围校验
  quality.py         # 质量指标约定及数值检查
  download.py        # 计划、分块、下载、校验
  providers/
    base.py
    mock.py
    earth_engine.py
  templates/imagery.html
  static/imagery.js
  static/imagery.css
```

目录允许合理合并，不要为凑目录制造空文件。

- 页面采用现有 Jinja2＋原生 HTML/CSS/JavaScript，由 FastAPI 同源提供，不依赖 CDN。
- 实时输出采用 SSE/EventSource；用户修改与确认使用普通 POST/PATCH。
- 耗时同步 SDK/文件操作在有并发上限的本地执行器中运行，不阻塞 API/SSE 事件循环。
- 单进程本地原型即可。等待用户确认时持久化状态并结束当前执行段，不占用一直等待的 worker。
- 进程重启：能恢复任务详情、计划和日志；未完成传输标为 INTERRUPTED，需要用户显式继续。不要声称已经支持透明断点恢复。
- 新可选依赖放在 `imagery` extra，建议 earthengine-api、numpy、rasterio、Pillow、pyproj、shapely；只在新功能需要时导入。安装原有 dev 依赖不得被迫初始化 Earth Engine。
- 包含新模板、静态文件和必要默认配置的打包设置。默认根目录不能依赖随意变化的工作目录。

## 3. 固定业务顺序，但让LLM真实参与选择

### A. 解析输入

- 模型读取用户原话、用户明确选择的 AOI 摘要、支持的数据类型与限制，返回结构化检索任务。
- 至少支持“两个月份对比准备”和“一段日期范围内的影像检索”。用户只要求检索一个时期时也不能强造第二个时期。
- 明确指定基准与目标时遵守用户角色；两个未指定角色的月份可按时间排序，但必须展示解释。不能只按句子里第一个日期当目标期。
- 月份转成左闭右开的区间；保存用户时区、查询时区与原始表达。统一使用实际采集时间，不将月中某日当默认目标。
- 默认只检索用户授权的窗口；允许提出扩展建议，但必须由用户修改并确认任务后生效。
- 大模型不能造出行政边界、影像ID、云量、下载链接或本地路径。

### B. 明确研究区

- 首版支持上传/粘贴 WGS84 GeoJSON Polygon/MultiPolygon，或选择已登记且有真实几何的 AOI。Feature/单研究区 FeatureCollection 可规范化；多对象如何合并必须展示。
- 检查几何有效性、经纬度范围、面积、空几何和复杂度；面积由程序计算，不由LLM填写。
- 一个地名但没有确定几何：进入 WAITING_INPUT，不开始真实检索。
- 旧 `wuhan_demo_100km2` 若只有演示元数据，不可用于真实 GEE 请求。可生成明确标为“合成测试方框，非行政边界”的离线样例。
- 默认推荐25—100平方公里试验区，普通模式面积软上限沿用可配置值；越界时要求用户调整，不偷偷裁剪。
- 不引入地名搜索服务或行政边界下载作为本轮额外依赖。

### C. 检索光学，不立即下载正式影像

- 第一数据源：`COPERNICUS/S2_SR_HARMONIZED`。
- 按已验证 AOI 与授权时间查询，使用稳定排序、限量和分页/加载更多。不得整库 getInfo；显示是否只是候选子集。
- 保存产品ID、system:index、采集时间、MGRS分幅、覆盖、scene cloud统计、产品/处理元数据和数据级别。
- 整景云量仅作粗筛排序，不默认把所有高云量目标删除。
- 建议每个时期先展示5组候选；真实查询限量和在线质检数量另外配置。不可把“展示5景”描述为全部数据。
- 对跨分幅AOI检测覆盖：允许用户选择多个分幅作为一个时期的数据。展示组合覆盖，不承诺已经完成拼接或去云。
- 搜索任务收到用户“开始检索”才运行；按钮旁说明将读取元数据、质量层与少量预览，但不下载正式多波段影像。

### D. 在线预览与质量检查

- 对预算内候选生成原始RGB缩略图与云/阴影/无数据叠加图，最大边长默认768，可配置。
- 使用 Earth Engine getThumbURL，缩略图可缓存到 `outputs/imagery/<task_id>/previews/`；标注预览缓存不等于正式数据下载。
- 原始预览保留云，不能为了好看先抹云。预览失败独立报告，不把不存在的PNG路径返回给用户。
- 质量使用 SCL，并可结合 Cloud Score+；优先按 system:index 匹配对应质量产品，检查匹配是否真实存在。
- 默认 `cs_cdf >= 0.60` 作为可配置清晰判据之一，实际使用方法、阈值、尺度必须记录。无匹配质量产品时可显式降为 SCL-only，而不是填0。
- SCL中云、云影、雪、无数据、饱和像元应区分；缺失和雪不能全算成云。有效光谱观测与“是否清晰”分别建mask。
- 至少返回：
  1. valid_coverage_fraction：有效像元面积/AOI面积；
  2. clear_aoi_fraction：满足质量规则的清晰像元面积/AOI面积；
  3. cloud_shadow_fraction_on_valid：有效覆盖中云影比例；
  4. quality_assessed_fraction：有质量信息的AOI比例；
  5. method、threshold、scale、状态和警示。
- 用程序按面积计算；零分母/无覆盖/质量失败返回null及原因，不能返回0%云。
- 不用PNG肉眼或LLM计算精确云量。不静默使用bestEffort改变计算尺度。
- 多景清晰并集只叫“所选窗口的组合可用性”，不能称“某一日期无云率”。首版不制作月度科学合成影像。

### E. LLM选择候选并判断SAR需求

- 将实际候选与质量摘要送入现有统一LLM接口，返回所选候选ID、未选原因、信息缺口与 `sar_recommendation`。
- `sar_recommendation` 至少含 not_needed / recommended / quality_unknown / unavailable，不输出“已实现去云”。
- 先看同一授权窗口的替代光学或组合覆盖；持续遮挡时才建议SAR。有效覆盖缺口不简单等同云量高。
- 模型只能引用候选池真实存在的ID；后端检查时期、覆盖、权限与参数。拒绝不合法建议，记录拒绝原因，最多按配置修复。
- 选图结果必须影响默认勾选清单，不能无论LLM返回什么都采用固定列表。
- SAR检索属于目录读取，可在“开始检索”的授权范围内执行；SAR正式数据下载仍必须由最终确认批准。

### F. 检索SAR并形成计划

- 使用 `COPERNICUS/S1_GRD`，IW、VV/VH。保留实际采集时间、升降轨、相对轨道和必要观测几何。
- 在授权窗口内查询，按覆盖、相对光学日期的绝对时间差和轨道可比性展示；同轨优先，不静默将不同轨道当同一时序。
- 保存每景SAR对应的目标光学及 `delta_days`；没有近时数据则如实警示，需要扩展时间时回到用户确认。
- 先不规定所有方法共用的严格同步阈值；准备候选不等于已经满足未来去云模型输入。
- 可第三次调用LLM整理最终下载建议。实际文件、波段、网格、大小估计由程序构造，不由LLM生成。

### G. 用户确认后下载，完成后检查

具体规则见第6—8节。没有确认时任务停留 WAITING_DOWNLOAD_APPROVAL。

## 4. 模型参与与完整输入输出日志——本轮重点

复用 `llm/openai_compatible.py`、factory 和 profiles，必要时添加向后兼容的 observer/event sink 与可配置请求消息入口。

- 新任务至少真实走过“解析任务”和“依据候选作选择”的模型接口；查询SAR后可再整理最终计划。
- 默认mockLLM只用于离线演示，明确标识，不能把规则输出冒充DeepSeek。
- DeepSeek/第二兼容profile由环境变量与配置选择，实际model ID不写死，不自行切换供应商。
- API请求前立即持久化 `llm.request`：本应用真正发送的messages、schema/tool定义（实际发送时）、可见上下文、脱敏生效参数、模型与prompt版本。
- 返回后立即记录 `llm.response`：实际content、结构化结果、finish_reason、返回模型、耗时、usage（服务未返回则null）与校验状态。
- 网络失败、HTTP错误、结构修复均逐次记录；调用预算在每次HTTP尝试前预占。401/403不盲目重试，429及临时5xx按有上限策略处理。
- 日志的“完整输入”是完整脱敏请求，不是事后改写的摘要。界面显示摘要＋可展开原文，长内容存受控artifact后按ID读取。
- 不要求提取或伪造不可见的模型内部思维；展示应用输入、实际输出和简短决策理由即可。
- API Key、Authorization/Cookie、访问令牌、签名下载URL参数等必须在落盘和SSE前脱敏；不记录全部环境变量。
- 日志通过请求task_id/call_id绑定，禁止全局变量导致并发任务串线。
- profile声明支持流式时，实现兼容stream读取并发出真实 `llm.delta`。默认禁用未经验证的供应商参数。
- 不支持流式则显示请求已发送/等待响应，再完整显示返回，不能用假的打字动画冒充流式。
- 流式JSON完整且验证通过之前，不执行动作。缺少usage不能计为零费用。
- 审批路径等用户本地信息不送入模型，除非确实必要；下载目录始终由程序和用户处理。
- 真实LLM失败时任务明确失败或等待用户处理，不静默回退Mock。仍然保留已完成的查询和日志。

## 5. 实时事件、异步执行与用户修改

新增任务状态，独立于旧TaskStatus，至少涵盖：

```text
CREATED -> PARSING -> WAITING_INPUT / SEARCHING_OPTICAL
-> PREVIEWING -> ASSESSING_QUALITY -> PLANNING
-> SEARCHING_SAR（可选） -> WAITING_DOWNLOAD_APPROVAL
-> DOWNLOADING -> VERIFYING -> COMPLETED / PARTIAL
另有 FAILED / CANCELLED / INTERRUPTED
```

- 创建任务快速返回202与task_id，不让整个检索和下载在POST请求里同步完成。
- SSE只订阅日志，不创建任务、不重新调用模型、不触发下载。
- Event记录 task_id、递增sequence、timestamp、event_type、stage、call_id/action_id/file_id、plan_version、易读summary和details引用。
- 至少有：task.created、state.changed、llm.request/delta/response/error、action.proposed/accepted/rejected、tool.started/progress/finished/failed、plan.ready、approval.required/received、download.preparing/started/progress/finished/failed、verification.finished、task.completed/failed/cancelled。
- 真正执行的每个动作都必须有开始和结束/失败，LLM建议与工具已执行必须明显区分。
- SQLite按任务保存递增事件，SSE支持Last-Event-ID和after_seq补读、心跳。页面刷新恢复同task_id，不创建新任务。
- 事件在动作发生时写入，不能结束后统一回放伪装实时。文件传输进度可节流至约每250—500ms，但不能省略开始/结束。
- 工作线程通过线程安全事件服务写记录；SQLite连接按操作/线程管理，不直接跨线程复用不安全连接。
- 用户修改范围、日期或候选选择时增加request/plan版本；旧worker的过时结果不能覆盖新任务状态。
- 阶段级后台任务结束后再继续下一阶段或等待确认，不能用sleep循环占着worker等待用户点击。
- 取消是显式动作。浏览器关闭只断开订阅，不取消任务；服务器收到取消后停止启动新请求、检查流读取取消信号，不能声称已即时终止无法中断的远端计算。

## 6. 下载确认：必须真正由程序卡住

### 下载计划

`DownloadPlan` 至少包含：task_id、version、plan_hash、AOI/grid版本、各时期、所选产品、波段、导出数据类型/单位、文件分块清单、估计大小与估计方法、用户提交的目标根目录、解析后的任务目录、provider和是否模拟。

- 计划由程序根据真实候选构造；下载实现只接受approved plan，不接收LLM提供的链接或路径。
- 默认根目录 `<project_root>/data/downloads`，每任务独立子目录 `<root>/<task_id>`。
- 页面提供可编辑路径输入框，显示解析后绝对路径，并注明“目录位于运行Python服务的机器”。不用承诺浏览器能直接选择任意本机目录。
- POSIX后端收到Windows盘符路径要明确提示不匹配，不能将 `D:\...` 当作Linux相对目录。
- 用户可勾选光学、SAR和质量辅助文件，但核心数据/掩膜依赖要校验；更改勾选、波段、网格、范围或目录后生成新计划并重新确认。
- 估计大小不是精确下载量，未知显示未知。检查剩余空间，实际传输继续检查异常。

### 审批行为

确认请求必须包含服务端最新plan_version、plan_hash和幂等键；服务端核验并事务性记录一次Approval后才能派发下载任务。

审批记录绑定明确的文件内容定义、目录和版本。连续双击/请求重放不得启动两份下载。

- 无审批：禁止生成正式影像下载任务、禁止调用正式像元getDownloadURL、禁止创建目标大文件。
- 小型缩略图缓存、目录元数据与在线质量计算属于用户已点击的检索阶段；须在UI说明这也会联网和消耗小量资源，不称为零网络活动。
- 新SAR、换影像、扩大范围、变网格或目录：原审批失效，重新确认。
- 同一已批准文件的有限网络重试或临时URL续签不需重复审批，但必须保持同一数据表达式和计划签名，记录尝试。
- 下载中不能直接修改计划；先取消或生成新的待确认任务。已下载文件不隐式覆盖、不随取消删除。
- 计划校验不应为了检查目录就写入任意文件；正式确认后再创建目标子目录及必要的可写性探测。
- 旧页面提交过时版本返回409，展示最新计划；无批准下载请求返回明确错误。

### 本地服务保护

默认只绑定127.0.0.1；同源页面操作写接口需适当的session/CSRF防护和Origin/Host校验，不开放任意跨域写盘。不把目录、下载或artifact接口当公网文件服务。

用户可选其有权限的目录，但模型无权代选。禁止覆盖项目配置、密钥或其他已有文件；由服务生成任务子目录和文件名，不接受任意文件名。

## 7. Earth Engine真实适配与质量科学约束

### 认证与初始化

- 真实provider显式选择 `gee`。延迟导入/初始化，使用已配置的 `EE_PROJECT_ID` 和用户已有凭据。
- 不在启动时弹出认证、不自动搜凭据、不自动开通或修改云项目。
- 提供 doctor 检查：默认只检查依赖与配置；`--check-remote`才尝试初始化和小型只读检查，不下载影像、不调用LLM。
- 缺凭据、权限、网络或quota时分别报告，不称为“无影像”。

### 导出内容

- S2默认六波段B2/B3/B4/B8/B11/B12；波段白名单可配置，确认前可修改。
- 光学SR推荐一次性缩放到float32反射率，保留原始含云观测，不提前去云、插值或归一化拉伸。记录原始缩放、无数据值与导出变换。
- SCL/有效性/质量层单独输出，使用合适数据类型；不要把uint16光学、uint8 SCL与float32质量硬塞到不兼容的一份多波段文件。
- 默认分析网格20米。可见/近红外原生10米与短波红外20米须显式重采样，记录方法；掩膜/类别使用最近邻。不声称导出到10米就提高了原生20米信息量。
- 每个AOI确定共享CRS、仿射变换、分辨率和像元原点；对所有光学、SAR与质量分块保持一致。不是仅设置相同scale。
- S1为平台已处理的GRD后向散射，记录dB单位、IW、极化、轨道与数据级别；不要重复取对数，不能把地形校正描述为额外完成辐射地形平坦化。
- 不做散斑滤波、跨日期合成或训练算法。本轮只准备逐景分析产品。

### 分块直接下载

- getDownloadURL仅用于小块，官方说明单请求最大32MB、网格维度最大10000。程序采用更保守、可配置的未压缩估计上限（例如24MiB）并按实际波段数/类型计算，不能靠压缩率碰运气。
- 推荐从512像元tile起步；超限时按同一已批准网格细分，数据内容/范围不变；禁止偷偷降分辨率解决超限。
- 根据当前官方API签名处理crs_transform/dimensions/region组合。clip到用户AOI并严格控制tile输出尺寸；不能用仅scale导出假设自动对齐。
- 每块请求显式输出GeoTIFF；优先避免ZIP，若使用则验证内容和安全解压。没有实际拼接就按tile交付并生成manifest，不伪装整景文件。
- 未批准之前允许计算文件分块计划，不生成正式下载URL。短时URL只在批准后即时生成/续签，不发送给LLM，不明文落盘。
- 仅从可信provider生成的HTTPS端点读取下载；不暴露任意URL抓取接口。

## 8. 文件传输、取消、校验与本地缩略图

- 使用流式HTTP写 `.part`，不把整个大文件一次读入内存。
- 文件阶段：queued / preparing_remote / downloading / verifying / completed / failed / cancelled。
- 网络Content-Length可信且存在时显示该文件百分比；不存在时百分比=null，显示字节与速率。平台生成阶段只显示阶段和等待状态，不编造进度。
- 总体进度优先按已完成文件数；不能把像元数估计当HTTP实际总字节。
- 下载完成检查响应内容确实为影像，不接受HTML错误页为tif。用Rasterio验证可打开、CRS、宽高、波段、类型、网格和有效像元。
- 全无数据输出为无效或警告，不计作成功可用影像；预览生成失败也单独记录。
- 计算SHA-256，校验成功后原子改名。部分失败输出PARTIAL和明确的失败清单，不能宣布全部完成。
- 已校验同计划文件在恢复时跳过；未完成块可重新下载。首版不承诺字节级Range续传，只有实际支持时再启用。
- 刷新页面不重传。服务器重启后保留清单，显示INTERRUPTED；用户显式恢复后核对计划/审批/文件hash再继续，不静默续传。
- 完成后从真实本地文件读取RGB与质量层生成PNG，使用所有日期一致的预览拉伸配置，保留nodata透明。绝不拿在线PNG直接冒充本地文件检查结果。
- SAR生成清楚标注的VV灰度或VV/VH预览，与真彩色预览区分。

## 9. 在线质检失败的备用路径

- 质量失败记录为unknown，不推断无云，不直接判断一定需要SAR。
- 用户可以选择“先下载光学，再本地质检”，此时形成仅光学＋必要SCL/质量文件的待审批计划。
- 下载后按可获得的SCL等做本地质量评估；无Cloud Score+时明确SCL-only，不假装离线运行Cloud Score+模型。
- 再把真实本地质量摘要给LLM。若建议新增SAR，生成补充下载计划，保留既有文件并再次请求确认。
- 先批准光学不表示已批准后续SAR。备用路径也必须可用mock场景测试。

## 10. 数据契约、API与页面最低要求

### 核心契约

采用Pydantic，至少有：ImageryTaskRequest、ParsedRequest、AOIRef、SearchPlan、SceneCandidate、QualitySummary、SceneRecommendation、SarRecommendation、DownloadPlan、ApprovalRecord、DownloadFile、InteractionEvent、LLMCallRecord。

- 场景数据、模型建议、用户选择与审批分开，不将LLM对象直接当下载计划。
- 所有时间、单位、百分比分母、模拟标识明确。
- task_version、plan_version和plan_hash参与异步更新和审批校验。
- 文件按ID访问，不通过API接受任意本地路径读取；下载目录输入仅在计划和审批中处理。

### API（新增，不替换旧端点）

```text
GET   /imagery                                  中文页面
GET   /api/imagery/config                        脱敏profile/后端能力/默认目录说明
POST  /api/imagery/tasks                         创建，202返回task_id
GET   /api/imagery/tasks/{id}                     当前状态/条件/摘要
GET   /api/imagery/tasks/{id}/events              SSE，支持续读
PATCH /api/imagery/tasks/{id}/request             澄清或修改条件、显式推进查询
GET   /api/imagery/tasks/{id}/candidates          候选与质量
GET   /api/imagery/tasks/{id}/plan                当前计划
PATCH /api/imagery/tasks/{id}/plan                勾选/波段/目录修改，产生新版本
POST  /api/imagery/tasks/{id}/approve             明确审批与幂等控制
POST  /api/imagery/tasks/{id}/cancel              取消
POST  /api/imagery/tasks/{id}/resume              显式恢复中断文件，检查原审批
GET   /api/imagery/tasks/{id}/artifacts/{aid}     受控访问预览/清单/脱敏日志
```

端点允许小幅调整，但文档、UI与测试必须一致。用户调整自然语言时应重新解析受影响条件；单纯勾选或改目录不必强行再调用LLM。

### 页面

一页四区域：
1. 任务输入、模型/数据后端、AOI上传或粘贴、开始检索；
2. 条件卡、候选影像勾选表、RGB/质量缩略图；
3. 时间线日志，每次LLM输入/输出和实际工具参数可展开；
4. 下载目录、计划差异、确认按钮、逐文件进度与结果。

- 固定显示本次模型与数据模式，区分真实/模拟；按后端机器解释目录。
- 使用真实交互，不用一串setTimeout播放预制日志，不用固定成功列表替代工具运行。
- 按task_id恢复页面；状态刷新不触发第二次执行。
- 用户/LLM/产品metadata使用textContent或模板转义，不插入未过滤HTML。
- 模型提案被拒绝、质量检查未知、文件失败都能在页面看到，不仅在后端stdout。
- 原生选择文件用于GeoJSON；路径框用于后端目录。不要承诺跨浏览器的原生目录选择器。
- 本轮不强制底图或地图SDK，AOI名称/面积/边界摘要＋缩略图已满足核心；可增加本地SVG边界预览，但不能依赖在线地图让页面无法启动。

## 11. 日志与文件落盘

控制状态与预览先存 `<output_root>/imagery/<task_id>/`，下载目录在用户确认后才创建。

控制目录建议：
```text
task.json
search_plan.json
quality_summary.json
selection_plan.json
approvals.jsonl
events.jsonl                 # 或以SQLite为主、支持导出
llm_calls/<call_id>/<attempt_id>.json
previews/
download_manifest.json
```

确认后的交付目录：
```text
<user_root>/<task_id>/
  optical/
  sar/
  quality/
  thumbnails/
  task.json
  selection_plan.json
  approval.json
  download_manifest.json
  quality_summary.json
  logs/                      # 脱敏导出，不含密钥和签名URL
```

SQLite为任务/事件/审批一致性来源，文件快照用原子替换，避免SSE读取半写JSON。实际名称可少量调整但必须记录。

## 12. 配置、命令与依赖

新增 `configs/imagery.yaml`，延续现有配置加载约定。工程初值（可修改，不是科学阈值结论）：

```yaml
provider: mock
model_profile: mock
default_download_root: data/downloads
grid_meters: 20
preview_max_dimension: 768
quality_scale_meters: 20
cloud_score_band: cs_cdf
cloud_score_clear_threshold: 0.60
candidate_display_limit_per_window: 5
catalog_limit_per_window: 100
initial_quality_limit_per_window: 10
max_download_concurrency: 2
max_download_retries: 2
max_llm_schema_repairs: 1
max_llm_attempts: 12
max_request_uncompressed_mib: 24
single_process: true
```

清晰覆盖达到何程度推荐SAR可配置，但要求显示规则，仅是建议，不能绕过LLM解释和用户决定。没有实际质量时不使用阈值。

`.env.example` 中记录环境变量名，不写实际密钥：

```dotenv
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
DEEPSEEK_MODEL=
SECOND_API_KEY=
SECOND_BASE_URL=
SECOND_MODEL=
EE_PROJECT_ID=
EO_IMAGERY_DOWNLOAD_ROOT=
```

环境变量默认根目录覆盖配置，但用户当前任务可在确认卡里修改。加载.env不打印内容，不将.env跟踪到Git。

必须保留并使用现有serve入口：

```bash
python -m pip install -e ".[dev,imagery]"
python -m eo_agent serve --host 127.0.0.1 --port 8000 --output-dir outputs/api
# 浏览器打开 http://127.0.0.1:8000/imagery
```

新增无副作用的诊断命令：

```bash
python -m eo_agent imagery doctor --provider mock
python -m eo_agent imagery doctor --provider gee
python -m eo_agent imagery doctor --provider gee --check-remote
```

前两者不访问网络，第三者只在用户显式调用时执行小型平台读检查。认证步骤由README指导用户在自己机器完成；不要在后端请求里自动调用交互认证。

## 13. 离线演示与自动化验收

mock必须真实运行相同的API、事件、审批和下载文件处理流程。

- 准备中性小型合成AOI和确定性小型多波段GeoTIFF、SCL与PNG。合成数据明示mock，不冠以实际观测产品身份。
- mock下载可从测试流或字节生成器写入文件，真实发出传输/校验事件，不用sleep伪造百分比。
- 模型mock的选择根据其收到的可见质量摘要决定，不按场景名称直接输出正确清单。
- 真LLM＋mock数据是支持的调试组合；原始资料仍是mock，不能变成真实监测结果。

至少覆盖以下测试，建议集中在 `tests/imagery/`，另保留旧测试：

1. 明确双月份、顺序倒置、单期窗口和非法日期解析。
2. 模糊武汉地名等待AOI，不能擅自制造真实边界。
3. 合成AOI元数据不能进入gee真实请求。
4. 影像只覆盖局部且清晰时，clear_aoi_fraction仍低；无数据/质量未知不会变成0%云。
5. 不同LLM合法选择导致不同候选推荐；不存在ID、越界日期被拒绝。
6. clear不推荐SAR、cloudy推荐SAR、同月替代更优时优先使用替代；纯覆盖不足不盲目触发去云。
7. 检索与预览结束停在WAITING_DOWNLOAD_APPROVAL：spy确认正式getDownloadURL与像元下载从未被调用，目标tif未创建。
8. 用户修改目录后文件实际写入新目录；默认根目录保留；不覆盖已有文件。
9. 旧版本确认409；并发双击/重放审批只有一个下载任务。
10. 审批后模型不能追加文件；新增SAR或改参数重新审批。
11. 每次LLM尝试有脱敏请求和响应/错误；与实际HTTP请求一致；密钥与签名URL不落盘。
12. 真实stream分段输出可见，但半段JSON不执行；非stream不伪装流式。
13. SSE在长动作结束前已有事件；断开再订阅不重新执行；sequence续读无跨任务串线。
14. 确认前取消不下载，下载中取消不启动新文件；刷新页面不取消也不重启。
15. 已知长度与未知长度均正确显示进度；临时错误重试记录真实次数。
16. 错误HTML响应、损坏tif、全nodata、网格不一致被发现；不输出全部成功。
17. 全部影像、SAR、质量tile网格对齐；请求大小估计与拆分在限制内。
18. 本地缩略图来自下载文件；PNG可实际打开；mock标识不丢失。
19. 在线质检失败→用户先批光学→本地质检→新增SAR需要第二次批准。
20. GEE配置失败不退回mock、不伪报无数据；真实适配器调用用测试替身检查SDK参数。
21. DeepSeek与第二profile的兼容请求使用MockTransport验证；无密钥时不访问真实API。
22. 新旧路由共存、旧legacy/scientific测试可用；模板/static可访问。
23. 新流程状态和审批持久化；重启后只显示中断和恢复选项，不偷偷重下。
24. 页面端到端：输入→检索→展开日志→修改目录→确认→文件完成；可用Playwright时实际执行并留截图，不可用则明确未做浏览器验证，不能以API测试替代声称UI已验证。

新功能测试如需imagery extra应明确标记；最终必须尝试安装该extra运行完整新测试，不能用全部skip宣称通过。

## 14. 实施顺序——同一轮完成，按小闭环推进

1. 基线：检查现有代码、运行旧测试，记录实际结果。
2. 新任务契约、SQLite事件、SSE、审批、页面；以合成影像走通离线交互，不先堆真正平台调用。
3. 扩展LLM observer与结构输出/stream，完成两次关键决策和MockTransport测试。
4. 实现真实Earth Engine目录、预览、质量、SAR和分块下载代码，与同一流程连接。
5. 完成本地缩略图、校验、异常与取消、目录确认的端到端测试。
6. 更新README、`docs/imagery_preparation.md`、`docs/imagery_verification.md`、AGENTS稳定约定和测试记录。

不要完成mock就结束并将真实provider留成raise NotImplementedError。没有凭据时，真实实现完成程度与真实连接验证程度分别报告。

若工作中遇到环境限制，继续完成不依赖限制的部分；最后准确列出缺项与可重跑命令，不编造“已验证”。

## 15. 最终必须执行与报告

建议验收命令：

```bash
python -m pip install -e ".[dev,imagery]"
python -m pytest -q
python -m ruff check .
python -m eo_agent demo --scenario cloudy --output-dir outputs/legacy_check
python -m eo_agent demo --workflow scientific --episode EP001 --model mock --output-dir outputs/scientific_check
python -m eo_agent imagery doctor --provider mock
```

真实doctor、真实LLM、真实影像下载默认不执行，除非当前任务另有明确授权；下载本身仍必须经过应用审批。

完成后用中文给出：
- 改动文件与主要行为，说明旧流程是否保持；
- 启动页面的实际命令与地址；
- 默认目录与修改方式，明确后端机器位置；
- 真实模型与Earth Engine的配置/认证方法；
- 逐条列出实际执行的测试结果，区分通过、失败、跳过与未验证；
- 截图或实际生成的示例文件路径（存在才提供）；
- 尚未完成的功能，不以“以后优化”掩盖关键缺失；
- 特别说明是否真的验证过Earth Engine连接和真实下载。

本轮结束，不继续开发变化检测、去云推理、政策/灾害联网解释或风险模型。

## 16. 实现前核对的官方资料

下列链接用于API事实核查，不要求复制其完整示例。若文档或SDK版本变化，以实际安装版本及官方文档为准，并记录变化。

```text
Codex仓库指令：
https://developers.openai.com/codex/guides/agents-md/
Earth Engine认证与初始化：
https://developers.google.com/earth-engine/guides/auth
S2 L2A产品与波段：
https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_S2_SR_HARMONIZED
Cloud Score+：
https://developers.google.com/earth-engine/datasets/catalog/GOOGLE_CLOUD_SCORE_PLUS_V1_S2_HARMONIZED
S1预处理和过滤：
https://developers.google.com/earth-engine/guides/sentinel1
缩略图：
https://developers.google.com/earth-engine/apidocs/ee-image-getthumburl
小块直接下载与限制：
https://developers.google.com/earth-engine/apidocs/ee-image-getdownloadurl
网格与导出：
https://developers.google.com/earth-engine/guides/exporting_images
SSE：
https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events
DeepSeek JSON与Chat接口，实施时重新核对可达性和模型能力：
https://api-docs.deepseek.com/guides/json_mode/
https://api-docs.deepseek.com/api/create-chat-completion
```

备注：本任务书根据仓库静态代码与官方资料编制；不是已完成的软件，不附带真实凭据或卫星影像。没有在用户机器上运行代码。
