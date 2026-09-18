# 交互式影像准备 V1

## 目标与边界

该子系统在现有 `eo_agent` 包内增加一个可恢复的交互流程：用户给出自然语言、授权时间和明确
GeoJSON；模型只拆解任务并从程序提供的候选 ID 中建议；数据工具检索并计算 AOI 内质量；程序构造
文件与网格；用户最终确认后才下载。它不做去云、变化检测、灾害/政策解释或模型训练。

旧 `TaskService`、legacy、scientific、CLI 和旧 API 保持不变。新增代码位于
`src/eo_agent/imagery/`，通过 `api.create_app()` 安装到同一个 FastAPI 应用。

## 状态与持久化

影像任务使用独立状态：

```text
CREATED → PARSING → WAITING_INPUT / SEARCHING_OPTICAL
→ PREVIEWING → ASSESSING_QUALITY → PLANNING
→ SEARCHING_SAR（按建议）→ WAITING_DOWNLOAD_APPROVAL
→ DOWNLOADING → VERIFYING → COMPLETED / PARTIAL
另有 FAILED / CANCELLED / INTERRUPTED
```

`POST /api/imagery/tasks` 立即返回 task ID，有限线程池分阶段执行；等待用户时 worker 已退出。
`imagery.sqlite3` 保存任务、单调递增事件、每次 LLM 尝试、审批、文件和 artifact 索引。SSE 支持
`Last-Event-ID` 与 `after_seq` 续读，订阅和页面刷新不会重新执行任务。

## 模型边界与日志

至少两次结构调用分别完成任务解析和光学选择；若实际检索 SAR，再调用一次整理最终建议。
模型输入仅含原始任务、时区、AOI 是否已提供、授权时期、候选元数据和 AOI 内质量，不含密钥、
目录、签名 URL 或未来结果。程序再次检查候选 ID、时期、数据类型和 VV/VH，非法提案只记录并拒绝。

observer 在每次 HTTP 尝试前记录应用实际发送的脱敏请求；响应、结构校验、参数校验、接受/拒绝和
实际工具参数分别保存并通过事件展示。401/403 不重试，429/临时 5xx 与 JSON 修复共享逐尝试预算。
真实 profile 失败不会切换服务商或 Mock。

## 数据后端

`MockImageryProvider` 生成确定性目录元数据、AOI 质量、PNG 和小型多波段 GeoTIFF，但仍通过
相同 provider 接口、审批、分块传输和 Rasterio 校验。`EarthEngineProvider` 延迟导入 SDK，使用
现有凭据与 `EE_PROJECT_ID`，固定三套任务书数据集，不调用 `ee.Authenticate()`。

Sentinel-2 先检索和预览，再把 SCL 与 Cloud Score+ 按 `system:index` 匹配并在用户 AOI 内统计。
整景云量只作目录元数据展示，不代替 AOI 质量。SAR 仅在同一授权窗口内查询 IW、VV/VH，保留轨道、
相对轨道、目标光学和日期差。质量未知时先允许只批光学；下载 SCL 后进行明确标注的 SCL-only 本地
复核，若新增 SAR 会产生新版本与第二次审批。

## 审批、下载与安全

程序将 AOI bbox 投影到中心 UTM 带，按用户分辨率吸附原点，给全部产品使用同一 CRS 和仿射网格。
文件按 512 像元和 24 MiB 未压缩上限保守分块，不通过降分辨率绕过限制。计划哈希覆盖 AOI 版本、
时期、候选、波段、每块网格和后端绝对目录。

审批前只允许目录读取、在线面积质量和小型预览；不生成正式短时 URL，也不创建下载任务目录。
审批事务验证最新版本/哈希与幂等键。正式传输仅接受 provider 生成的受信任 HTTPS 地址（Mock 使用
内部字节迭代器），写 `.part`，报告实际字节/速率，有 Content-Length 才显示比例。完成后检查 TIFF
驱动、CRS、尺寸、仿射变换、波段名/顺序、数据类型与有效像元，计算 SHA-256 后原子改名，再从该
文件生成本地 PNG。取消不会删除已经验证的文件；恢复会核对哈希并跳过完成文件。

同源写接口要求页面 CSRF token，并在存在 Origin 时校验同源。artifact 只能由任务 ID 与 artifact ID
读取；API 不接受任意文件路径或任意下载 URL。

## API 与启动

```text
GET   /imagery
GET   /api/imagery/config
POST  /api/imagery/tasks
GET   /api/imagery/tasks/{id}
GET   /api/imagery/tasks/{id}/events
PATCH /api/imagery/tasks/{id}/request
GET   /api/imagery/tasks/{id}/candidates
GET   /api/imagery/tasks/{id}/plan
PATCH /api/imagery/tasks/{id}/plan
POST  /api/imagery/tasks/{id}/approve
POST  /api/imagery/tasks/{id}/cancel
POST  /api/imagery/tasks/{id}/resume
GET   /api/imagery/tasks/{id}/artifacts/{aid}
```

```bash
python -m pip install -e ".[dev,imagery]"
python -m eo_agent imagery doctor --provider mock
python -m eo_agent serve --host 127.0.0.1 --port 8000 --output-dir outputs/api
```

浏览器打开 `http://127.0.0.1:8000/imagery`。

