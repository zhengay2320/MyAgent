"use strict";

(() => {
  const API_ROOT = "/api/imagery";
  const TERMINAL_STATES = new Set(["COMPLETED", "FAILED", "CANCELLED"]);
  const WAITING_STATES = new Set(["WAITING_INPUT", "WAITING_DOWNLOAD_APPROVAL"]);
  const ACTIVE_STATES = new Set([
    "CREATED",
    "PARSING",
    "SEARCHING_OPTICAL",
    "PREVIEWING",
    "ASSESSING_QUALITY",
    "PLANNING",
    "SEARCHING_SAR",
    "DOWNLOADING",
    "VERIFYING",
  ]);
  const SSE_EVENT_TYPES = [
    "task.created",
    "state.changed",
    "llm.request",
    "llm.delta",
    "llm.response",
    "llm.error",
    "action.proposed",
    "action.accepted",
    "action.rejected",
    "tool.started",
    "tool.progress",
    "tool.finished",
    "tool.failed",
    "plan.ready",
    "approval.required",
    "approval.received",
    "download.preparing",
    "download.started",
    "download.progress",
    "download.finished",
    "download.failed",
    "verification.finished",
    "task.completed",
    "task.failed",
    "task.cancelled",
  ];

  const state = {
    config: null,
    taskId: null,
    task: null,
    candidates: null,
    plan: null,
    llmRecords: [],
    events: [],
    eventKeys: new Set(),
    lastSequence: 0,
    eventSource: null,
    reconnectTimer: null,
    refreshTimer: null,
    fileProgress: new Map(),
    approvalKeys: new Map(),
    planDirty: false,
  };

  const dom = {};

  function byId(id) {
    return document.getElementById(id);
  }

  function make(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function asArray(value) {
    if (Array.isArray(value)) return value;
    if (value === null || value === undefined) return [];
    return [value];
  }

  function firstDefined(...values) {
    return values.find((value) => value !== undefined && value !== null);
  }

  function objectValue(value) {
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  }

  function textValue(value, fallback = "—") {
    if (value === null || value === undefined || value === "") return fallback;
    if (typeof value === "string") return value;
    if (typeof value === "number" || typeof value === "boolean") return String(value);
    try {
      return JSON.stringify(value, null, 2);
    } catch (_error) {
      return String(value);
    }
  }

  function prettyJson(value) {
    if (typeof value === "string") {
      try {
        return JSON.stringify(JSON.parse(value), null, 2);
      } catch (_error) {
        return value;
      }
    }
    try {
      return JSON.stringify(value, null, 2);
    } catch (_error) {
      return String(value);
    }
  }

  function formatDate(value) {
    if (!value) return "—";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return String(value);
    return new Intl.DateTimeFormat("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(parsed);
  }

  function formatBytes(value) {
    const amount = Number(value);
    if (!Number.isFinite(amount) || amount < 0) return "未知";
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let size = amount;
    let unit = units[0];
    for (let index = 0; index < units.length - 1 && size >= 1024; index += 1) {
      size /= 1024;
      unit = units[index + 1];
    }
    return `${size >= 10 || unit === "B" ? size.toFixed(0) : size.toFixed(1)} ${unit}`;
  }

  function formatRate(value) {
    const rate = Number(value);
    return Number.isFinite(rate) && rate >= 0 ? `${formatBytes(rate)}/s` : "速率未知";
  }

  function formatFraction(value) {
    if (value === null || value === undefined || value === "") return "未知";
    const number = Number(value);
    if (!Number.isFinite(number)) return "未知";
    return `${(number * 100).toFixed(1)}%`;
  }

  function statusName(value) {
    const status = String(value || "UNKNOWN").toUpperCase();
    const labels = {
      CREATED: "已创建",
      PARSING: "模型解析中",
      WAITING_INPUT: "等待补充条件",
      SEARCHING_OPTICAL: "检索光学影像",
      PREVIEWING: "生成在线预览",
      ASSESSING_QUALITY: "研究区质量检查",
      PLANNING: "形成下载建议",
      SEARCHING_SAR: "检索 SAR",
      WAITING_DOWNLOAD_APPROVAL: "等待下载确认",
      DOWNLOADING: "下载中",
      VERIFYING: "校验中",
      COMPLETED: "已完成",
      PARTIAL: "部分完成",
      FAILED: "失败",
      CANCELLED: "已取消",
      INTERRUPTED: "已中断",
      QUEUED: "等待中",
      PREPARING_REMOTE: "远端正在生成",
    };
    return labels[status] || status;
  }

  function statusClass(value) {
    const status = String(value || "").toUpperCase();
    if (status === "COMPLETED" || status === "FINISHED") return "status-success";
    if (status === "FAILED" || status === "CANCELLED") return "status-failed";
    if (WAITING_STATES.has(status) || status === "INTERRUPTED" || status === "PARTIAL") {
      return "status-waiting";
    }
    if (ACTIVE_STATES.has(status) || status === "DOWNLOADING") return "status-active";
    return "status-neutral";
  }

  function showMessage(message, kind = "info") {
    dom.globalMessage.textContent = message;
    dom.globalMessage.className = `global-message${kind === "error" ? " error" : ""}`;
    dom.globalMessage.hidden = false;
  }

  function hideMessage() {
    dom.globalMessage.hidden = true;
  }

  function csrfToken() {
    return document.querySelector('meta[name="csrf-token"]')?.content || "";
  }

  async function apiRequest(path, options = {}) {
    const headers = new Headers(options.headers || {});
    const token = csrfToken();
    if (token) headers.set("X-CSRF-Token", token);
    if (options.body !== undefined && !(options.body instanceof FormData)) {
      headers.set("Content-Type", "application/json");
    }
    const response = await fetch(`${API_ROOT}${path}`, {
      ...options,
      headers,
      credentials: "same-origin",
      body:
        options.body !== undefined && !(options.body instanceof FormData)
          ? JSON.stringify(options.body)
          : options.body,
    });
    const contentType = response.headers.get("content-type") || "";
    let payload;
    if (contentType.includes("application/json")) {
      payload = await response.json();
    } else {
      payload = await response.text();
    }
    if (!response.ok) {
      const detail = objectValue(payload).detail || objectValue(payload).message || payload;
      const error = new Error(textValue(detail, `请求失败（HTTP ${response.status}）`));
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  function normalizeChoices(raw, fallback) {
    if (Array.isArray(raw)) {
      return raw.map((entry) => {
        if (typeof entry === "string") return { value: entry, label: entry, enabled: true };
        const item = objectValue(entry);
        return {
          value: firstDefined(item.value, item.id, item.name, item.profile, item.provider),
          label: firstDefined(item.label, item.display_name, item.name, item.id),
          enabled: firstDefined(item.enabled, item.available, true),
          note: firstDefined(item.note, item.description, ""),
        };
      });
    }
    if (raw && typeof raw === "object") {
      return Object.entries(raw).map(([value, details]) => {
        if (typeof details === "string") return { value, label: details, enabled: true };
        const item = objectValue(details);
        return {
          value,
          label: firstDefined(item.label, item.display_name, item.name, value),
          enabled: firstDefined(item.enabled, item.available, true),
          note: firstDefined(item.note, item.description, ""),
        };
      });
    }
    return fallback;
  }

  function populateSelect(select, choices, preferred) {
    const oldValue = select.value;
    clear(select);
    choices.forEach((choice) => {
      if (!choice.value) return;
      const option = make("option", null, choice.label || choice.value);
      option.value = String(choice.value);
      option.disabled = choice.enabled === false;
      if (choice.note) option.title = choice.note;
      select.appendChild(option);
    });
    const desired = preferred || oldValue;
    if (desired && Array.from(select.options).some((option) => option.value === desired && !option.disabled)) {
      select.value = desired;
    }
  }

  async function loadConfig() {
    try {
      const config = await apiRequest("/config");
      state.config = config;
      const profiles = normalizeChoices(
        firstDefined(config.model_profiles, config.llm_profiles, config.profiles),
        [{ value: "mock", label: "MockLLM（离线）", enabled: true }],
      );
      const providers = normalizeChoices(
        firstDefined(config.data_providers, config.providers, config.backends),
        [{ value: "mock", label: "Mock（离线合成数据）", enabled: true }],
      );
      populateSelect(dom.modelProfile, profiles, firstDefined(config.default_model_profile, "mock"));
      populateSelect(dom.dataProvider, providers, firstDefined(config.default_provider, "mock"));
      populateRegisteredAois(firstDefined(config.registered_aois, config.aois, []));
      const defaultRoot = firstDefined(
        config.default_download_root,
        config.download_root,
        config.default_download_root_resolved,
      );
      if (defaultRoot && !dom.downloadRoot.value) dom.downloadRoot.value = defaultRoot;
      dom.footerConfigNote.textContent = firstDefined(
        config.directory_notice,
        "保存目录位于运行 Python 后端的机器；真实模型和 Earth Engine 必须显式配置。",
      );
      updateModeBadge();
    } catch (error) {
      showMessage(`无法读取影像功能配置：${error.message}`, "error");
    }
  }

  function populateRegisteredAois(rawAois) {
    clear(dom.registeredAoi);
    const placeholder = make("option", null, "请从服务端登记列表选择");
    placeholder.value = "";
    dom.registeredAoi.appendChild(placeholder);
    asArray(rawAois).forEach((raw) => {
      const aoi = objectValue(raw);
      const id = firstDefined(aoi.aoi_id, aoi.id, aoi.name);
      if (!id) return;
      const hasGeometry = firstDefined(aoi.has_geometry, Boolean(aoi.geometry || aoi.geojson), false);
      const synthetic = Boolean(firstDefined(aoi.is_synthetic, aoi.synthetic, false));
      const label = firstDefined(aoi.display_name, aoi.label, aoi.name, id);
      const suffix = synthetic ? "（合成测试几何，非行政边界）" : "";
      const option = make("option", null, `${label}${suffix}`);
      option.value = String(id);
      option.disabled = !hasGeometry;
      option.dataset.synthetic = String(synthetic);
      option.dataset.note = firstDefined(aoi.geometry_summary, aoi.description, "");
      dom.registeredAoi.appendChild(option);
    });
  }

  function updateModeBadge() {
    const model = dom.modelProfile.value || "mock";
    const provider = dom.dataProvider.value || "mock";
    const modelLabel = model === "mock" ? "模拟模型" : `真实模型：${model}`;
    const providerLabel = provider === "mock" ? "模拟数据" : `真实数据：${provider}`;
    dom.modeBadge.textContent = `${modelLabel} · ${providerLabel}`;
    dom.modeBadge.className = `status-pill ${model === "mock" && provider === "mock" ? "status-mock" : "status-real"}`;
  }

  function activeAoiMode() {
    return document.querySelector('input[name="aoi-mode"]:checked')?.value || "geojson";
  }

  function switchAoiMode() {
    const geojson = activeAoiMode() === "geojson";
    dom.geojsonInputGroup.hidden = !geojson;
    dom.registeredInputGroup.hidden = geojson;
    if (geojson) updateGeojsonPreview();
    else updateRegisteredAoiPreview();
  }

  function extractGeometry(input) {
    if (!input || typeof input !== "object") throw new Error("GeoJSON 必须是对象");
    if (input.type === "Feature") return input.geometry;
    if (input.type === "FeatureCollection") {
      if (!Array.isArray(input.features) || input.features.length !== 1) {
        throw new Error("首版仅接受包含一个研究区的 FeatureCollection");
      }
      return input.features[0]?.geometry;
    }
    return input;
  }

  function geometryRings(geometry) {
    if (!geometry || !Array.isArray(geometry.coordinates)) return [];
    if (geometry.type === "Polygon") return geometry.coordinates;
    if (geometry.type === "MultiPolygon") return geometry.coordinates.flat();
    return [];
  }

  function parsedGeojson() {
    const raw = dom.geojsonText.value.trim();
    if (!raw) throw new Error("请粘贴或上传 GeoJSON");
    const parsed = JSON.parse(raw);
    const geometry = extractGeometry(parsed);
    if (!geometry || !["Polygon", "MultiPolygon"].includes(geometry.type)) {
      throw new Error("研究区必须是 Polygon 或 MultiPolygon");
    }
    const rings = geometryRings(geometry);
    const points = rings.flat().filter((point) => Array.isArray(point) && point.length >= 2);
    if (points.length < 4 || points.some((point) => !Number.isFinite(Number(point[0])) || !Number.isFinite(Number(point[1])))) {
      throw new Error("GeoJSON 坐标无效或为空");
    }
    return { parsed, geometry, rings, points };
  }

  function drawGeometry(rings, points) {
    const xs = points.map((point) => Number(point[0]));
    const ys = points.map((point) => Number(point[1]));
    const minX = Math.min(...xs);
    const maxX = Math.max(...xs);
    const minY = Math.min(...ys);
    const maxY = Math.max(...ys);
    const width = Math.max(maxX - minX, 1e-9);
    const height = Math.max(maxY - minY, 1e-9);
    const path = rings
      .filter((ring) => Array.isArray(ring) && ring.length)
      .map((ring) =>
        ring
          .map((point, index) => {
            const x = 8 + ((Number(point[0]) - minX) / width) * 164;
            const y = 92 - ((Number(point[1]) - minY) / height) * 84;
            return `${index === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`;
          })
          .join(" ") + " Z",
      )
      .join(" ");
    dom.aoiPreviewPath.setAttribute("d", path);
    return { minX, minY, maxX, maxY };
  }

  function updateGeojsonPreview() {
    if (activeAoiMode() !== "geojson") return;
    if (!dom.geojsonText.value.trim()) {
      dom.aoiSummary.hidden = true;
      return;
    }
    try {
      const { geometry, rings, points } = parsedGeojson();
      const bounds = drawGeometry(rings, points);
      dom.aoiSummary.hidden = false;
      dom.aoiSummaryTitle.textContent = `${geometry.type} 已读取`;
      dom.aoiSummaryText.textContent = `顶点记录 ${points.length}；边界框 ${bounds.minX.toFixed(4)}, ${bounds.minY.toFixed(4)} 至 ${bounds.maxX.toFixed(4)}, ${bounds.maxY.toFixed(4)}`;
    } catch (error) {
      dom.aoiSummary.hidden = false;
      dom.aoiPreviewPath.setAttribute("d", "");
      dom.aoiSummaryTitle.textContent = "GeoJSON 尚未通过格式检查";
      dom.aoiSummaryText.textContent = error.message;
    }
  }

  function updateRegisteredAoiPreview() {
    if (activeAoiMode() !== "registered") return;
    const option = dom.registeredAoi.selectedOptions[0];
    if (!option || !option.value) {
      dom.aoiSummary.hidden = true;
      return;
    }
    dom.aoiSummary.hidden = false;
    dom.aoiPreviewPath.setAttribute("d", "M22,78 L42,22 L125,12 L160,45 L138,82 L22,78 Z");
    dom.aoiSummaryTitle.textContent = option.textContent;
    dom.aoiSummaryText.textContent = option.dataset.note || "真实几何将由服务端载入并校验。";
  }

  function buildAoiPayload() {
    if (activeAoiMode() === "registered") {
      if (!dom.registeredAoi.value) throw new Error("请选择一个具有几何的已登记研究区");
      return {
        aoi_id: dom.registeredAoi.value,
        aoi_name: dom.registeredAoi.selectedOptions[0]?.textContent || null,
      };
    }
    return { aoi_geojson: parsedGeojson().parsed };
  }

  function buildTaskPayload() {
    const query = dom.taskQuery.value.trim();
    if (!query) throw new Error("请输入影像准备任务");
    return {
      query,
      model_profile: dom.modelProfile.value,
      provider: dom.dataProvider.value,
      requested_data: ["optical"],
      user_timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Shanghai",
      ...buildAoiPayload(),
    };
  }

  async function createTask(event) {
    event.preventDefault();
    hideMessage();
    let payload;
    try {
      payload = buildTaskPayload();
    } catch (error) {
      showMessage(error.message, "error");
      return;
    }
    dom.createTaskButton.disabled = true;
    dom.createTaskButton.textContent = "正在创建…";
    try {
      const response = await apiRequest("/tasks", { method: "POST", body: payload });
      const taskId = firstDefined(response.task_id, objectValue(response.task).task_id, response.id);
      if (!taskId) throw new Error("服务端未返回 task_id");
      await activateTask(String(taskId), true);
      showMessage(`任务 ${taskId} 已创建；检索正在后台执行。`);
    } catch (error) {
      showMessage(`创建任务失败：${error.message}`, "error");
    } finally {
      dom.createTaskButton.disabled = false;
      dom.createTaskButton.textContent = "开始检索";
    }
  }

  async function updateRequest() {
    if (!state.taskId) return;
    let payload;
    try {
      payload = buildTaskPayload();
      payload.task_version = firstDefined(state.task?.task_version, state.task?.version);
      payload.continue_search = true;
    } catch (error) {
      showMessage(error.message, "error");
      return;
    }
    dom.updateRequestButton.disabled = true;
    try {
      await apiRequest(`/tasks/${encodeURIComponent(state.taskId)}/request`, {
        method: "PATCH",
        body: payload,
      });
      showMessage("条件已提交；服务端将按新版本继续解析和检索。候选与下载计划可能失效。");
      await refreshTaskData();
    } catch (error) {
      showMessage(`更新条件失败：${error.message}`, "error");
    } finally {
      dom.updateRequestButton.disabled = false;
    }
  }

  async function activateTask(taskId, updateUrl) {
    closeEventStream();
    state.taskId = taskId;
    state.task = null;
    state.candidates = null;
    state.plan = null;
    state.llmRecords = [];
    state.events = [];
    state.eventKeys = new Set();
    state.lastSequence = 0;
    state.fileProgress = new Map();
    state.planDirty = false;
    renderAll();
    if (updateUrl) {
      const url = new URL(window.location.href);
      url.searchParams.set("task_id", taskId);
      window.history.replaceState({}, "", url);
    }
    await refreshTaskData();
    connectEventStream();
  }

  async function refreshTaskData() {
    if (!state.taskId) return;
    dom.refreshButton.disabled = true;
    const id = encodeURIComponent(state.taskId);
    const [taskResult, candidatesResult, planResult] = await Promise.allSettled([
      apiRequest(`/tasks/${id}`),
      apiRequest(`/tasks/${id}/candidates`),
      apiRequest(`/tasks/${id}/plan`),
    ]);
    if (taskResult.status === "fulfilled") {
      state.task = objectValue(taskResult.value.task).task_id ? taskResult.value.task : taskResult.value;
      state.llmRecords = asArray(firstDefined(taskResult.value.llm_calls, state.task.llm_calls, []));
      mergeSnapshotEvents(firstDefined(taskResult.value.events, state.task.events, []));
      mergeFileProgress(firstDefined(state.task.download_files, state.task.files, []));
    } else {
      showMessage(`无法读取任务：${taskResult.reason.message}`, "error");
    }
    if (candidatesResult.status === "fulfilled") state.candidates = candidatesResult.value;
    else if (candidatesResult.reason.status !== 404 && candidatesResult.reason.status !== 409) {
      showMessage(`候选读取失败：${candidatesResult.reason.message}`, "error");
    }
    if (planResult.status === "fulfilled") {
      const returnedPlan = objectValue(planResult.value.plan).plan_hash ? planResult.value.plan : planResult.value;
      if (state.plan?.plan_hash !== returnedPlan.plan_hash) state.planDirty = false;
      state.plan = returnedPlan;
      mergeFileProgress(firstDefined(returnedPlan.download_files, returnedPlan.files, []));
    } else if (planResult.reason.status !== 404 && planResult.reason.status !== 409) {
      showMessage(`计划读取失败：${planResult.reason.message}`, "error");
    }
    renderAll();
    dom.refreshButton.disabled = false;
  }

  function mergeSnapshotEvents(events) {
    asArray(events).forEach((event) => addEvent(event, null, false));
  }

  function mergeFileProgress(files) {
    asArray(files).forEach((file) => {
      const item = objectValue(file);
      const id = firstDefined(item.file_id, item.id, item.relative_path, item.filename);
      if (!id) return;
      state.fileProgress.set(String(id), { ...state.fileProgress.get(String(id)), ...item });
    });
  }

  function scheduleRefresh(delay = 180) {
    if (state.refreshTimer) window.clearTimeout(state.refreshTimer);
    state.refreshTimer = window.setTimeout(() => {
      state.refreshTimer = null;
      refreshTaskData();
    }, delay);
  }

  function connectEventStream() {
    if (!state.taskId) return;
    closeEventStream();
    const id = encodeURIComponent(state.taskId);
    const url = `${API_ROOT}/tasks/${id}/events?after_seq=${encodeURIComponent(state.lastSequence)}`;
    const source = new EventSource(url, { withCredentials: true });
    state.eventSource = source;
    dom.connectionBadge.textContent = `连接中 · 从 #${state.lastSequence}`;
    dom.connectionBadge.className = "status-pill status-active";
    dom.reconnectButton.disabled = true;
    source.onopen = () => {
      dom.connectionBadge.textContent = "实时事件已连接";
      dom.connectionBadge.className = "status-pill status-success";
      dom.reconnectButton.disabled = false;
    };
    source.onmessage = (event) => receiveSseEvent(event, "message");
    SSE_EVENT_TYPES.forEach((type) => {
      source.addEventListener(type, (event) => receiveSseEvent(event, type));
    });
    source.onerror = () => {
      dom.connectionBadge.textContent = `连接中断 · 将从 #${state.lastSequence} 续读`;
      dom.connectionBadge.className = "status-pill status-waiting";
      dom.reconnectButton.disabled = false;
      closeEventStream(false);
      if (!TERMINAL_STATES.has(String(state.task?.status || "").toUpperCase())) {
        state.reconnectTimer = window.setTimeout(connectEventStream, 1600);
      }
    };
  }

  function closeEventStream(clearTimer = true) {
    if (state.eventSource) {
      state.eventSource.close();
      state.eventSource = null;
    }
    if (clearTimer && state.reconnectTimer) {
      window.clearTimeout(state.reconnectTimer);
      state.reconnectTimer = null;
    }
  }

  function receiveSseEvent(event, namedType) {
    if (!event.data || event.data === ": heartbeat") return;
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch (_error) {
      payload = { summary: event.data };
    }
    if (!payload.event_type && namedType !== "message") payload.event_type = namedType;
    const sequence = Number(firstDefined(payload.sequence, payload.seq, event.lastEventId));
    if (Number.isFinite(sequence)) {
      payload.sequence = sequence;
      state.lastSequence = Math.max(state.lastSequence, sequence);
    }
    addEvent(payload, namedType, true);
  }

  function eventIdentity(event) {
    const sequence = Number(firstDefined(event.sequence, event.seq));
    if (Number.isFinite(sequence)) return `sequence:${sequence}`;
    return [
      firstDefined(event.event_type, event.type, "event"),
      firstDefined(event.timestamp, event.created_at, ""),
      firstDefined(event.call_id, event.action_id, event.file_id, ""),
      firstDefined(event.summary, event.message, ""),
    ].join("|");
  }

  function addEvent(raw, namedType, live) {
    const event = objectValue(raw);
    if (!event.event_type) event.event_type = firstDefined(event.type, namedType, "event");
    const identity = eventIdentity(event);
    if (state.eventKeys.has(identity)) return;
    state.eventKeys.add(identity);
    state.events.push(event);
    state.events.sort((left, right) => {
      const a = Number(firstDefined(left.sequence, left.seq, Number.MAX_SAFE_INTEGER));
      const b = Number(firstDefined(right.sequence, right.seq, Number.MAX_SAFE_INTEGER));
      return a - b;
    });
    const sequence = Number(firstDefined(event.sequence, event.seq));
    if (Number.isFinite(sequence)) state.lastSequence = Math.max(state.lastSequence, sequence);
    updateProgressFromEvent(event);
    renderTimeline();
    renderLlmCalls();
    renderDownloads();
    if (live) reactToEvent(event);
  }

  function eventDetails(event) {
    return objectValue(firstDefined(event.details, event.payload, event.data));
  }

  function updateProgressFromEvent(event) {
    const type = String(firstDefined(event.event_type, event.type, ""));
    if (!type.startsWith("download.") && type !== "verification.finished") return;
    const details = eventDetails(event);
    const id = firstDefined(event.file_id, details.file_id, details.id, details.relative_path, details.filename);
    if (!id) return;
    const previous = state.fileProgress.get(String(id)) || {};
    const statusByEvent = {
      "download.preparing": "PREPARING_REMOTE",
      "download.started": "DOWNLOADING",
      "download.progress": "DOWNLOADING",
      "download.finished": "COMPLETED",
      "download.failed": "FAILED",
      "verification.finished": firstDefined(details.status, "VERIFYING"),
    };
    state.fileProgress.set(String(id), {
      ...previous,
      ...details,
      file_id: String(id),
      status: firstDefined(details.status, statusByEvent[type], previous.status),
      updated_at: firstDefined(event.timestamp, event.created_at, previous.updated_at),
    });
  }

  function reactToEvent(event) {
    const type = String(firstDefined(event.event_type, event.type, ""));
    if (
      type === "state.changed" ||
      type === "plan.ready" ||
      type === "approval.required" ||
      type === "approval.received" ||
      type.startsWith("task.") ||
      type === "tool.finished" ||
      type === "tool.failed" ||
      type === "llm.response" ||
      type === "llm.error"
    ) {
      scheduleRefresh();
    }
    if (type === "task.completed" || type === "task.failed" || type === "task.cancelled") {
      window.setTimeout(() => closeEventStream(), 400);
    }
  }

  function renderAll() {
    renderTask();
    renderCandidates();
    renderPlan();
    renderDownloads();
    renderTimeline();
    renderLlmCalls();
    updateModeBadge();
  }

  function addDefinition(list, label, value) {
    const dt = make("dt", null, label);
    const dd = make("dd", null, textValue(value));
    list.append(dt, dd);
  }

  function renderTask() {
    const task = objectValue(state.task);
    const hasTask = Boolean(state.taskId);
    dom.taskEmpty.hidden = hasTask;
    dom.taskCard.hidden = !hasTask;
    dom.taskIdLabel.textContent = state.taskId || "—";
    dom.refreshButton.disabled = !hasTask;
    dom.reconnectButton.disabled = !hasTask;
    if (!hasTask) return;
    const status = firstDefined(task.status, task.state, "CREATED");
    dom.taskStatusBadge.textContent = statusName(status);
    dom.taskStatusBadge.className = `status-pill ${statusClass(status)}`;
    dom.updateRequestButton.hidden = String(status).toUpperCase() !== "WAITING_INPUT";
    const parsed = objectValue(firstDefined(task.parsed_request, task.parsed, task.search_plan));
    const aoi = objectValue(firstDefined(task.aoi, parsed.aoi));
    const windows = firstDefined(parsed.periods, parsed.windows, parsed.time_windows, task.time_windows);
    clear(dom.taskFields);
    addDefinition(dom.taskFields, "用户原始任务", firstDefined(task.query, task.user_request, parsed.original_text, dom.taskQuery.value));
    addDefinition(dom.taskFields, "模型 profile", firstDefined(task.model_profile, parsed.model_profile, dom.modelProfile.value));
    addDefinition(dom.taskFields, "数据后端", firstDefined(task.provider, task.data_provider, parsed.provider, dom.dataProvider.value));
    addDefinition(dom.taskFields, "检索时期", windows);
    addDefinition(dom.taskFields, "用户时区 / 查询时区", {
      user: firstDefined(parsed.user_timezone, task.user_timezone),
      query: firstDefined(parsed.query_timezone, task.query_timezone, "UTC"),
    });
    addDefinition(dom.taskFields, "所需数据", firstDefined(parsed.requested_data, parsed.data_types, task.requested_data));
    addDefinition(dom.taskFields, "研究区", firstDefined(aoi.display_name, aoi.name, aoi.aoi_id, task.aoi_summary));
    addDefinition(dom.taskFields, "研究区面积", firstDefined(aoi.area_km2, task.aoi_area_km2) !== undefined
      ? `${firstDefined(aoi.area_km2, task.aoi_area_km2)} km²`
      : "等待程序计算");
    addDefinition(dom.taskFields, "任务版本", firstDefined(task.task_version, task.version, 1));
    clear(dom.taskWarnings);
    asArray(firstDefined(task.warnings, parsed.warnings, aoi.warnings, [])).forEach((warning) => {
      dom.taskWarnings.appendChild(make("div", "warning-item", textValue(warning)));
    });
    const taskModel = firstDefined(task.model_profile, parsed.model_profile);
    const taskProvider = firstDefined(task.provider, task.data_provider, parsed.provider);
    if (taskModel && Array.from(dom.modelProfile.options).some((option) => option.value === taskModel)) {
      dom.modelProfile.value = taskModel;
    }
    if (taskProvider && Array.from(dom.dataProvider.options).some((option) => option.value === taskProvider)) {
      dom.dataProvider.value = taskProvider;
    }
  }

  function candidateGroups() {
    const root = objectValue(state.candidates);
    const nested = objectValue(root.candidates);
    let optical = firstDefined(root.optical_candidates, root.optical, nested.optical, []);
    let sar = firstDefined(root.sar_candidates, root.sar, nested.sar, []);
    if (Array.isArray(root.candidates)) {
      optical = root.candidates.filter((item) => !String(firstDefined(item.kind, item.sensor, "")).toUpperCase().includes("S1"));
      sar = root.candidates.filter((item) => String(firstDefined(item.kind, item.sensor, "")).toUpperCase().includes("S1"));
    }
    const qualityItems = firstDefined(root.quality_summaries, root.quality, []);
    const qualityById = new Map();
    if (Array.isArray(qualityItems)) {
      qualityItems.forEach((item) => qualityById.set(String(item.candidate_id), item));
    } else if (qualityItems && typeof qualityItems === "object") {
      Object.entries(qualityItems).forEach(([id, item]) => qualityById.set(String(id), item));
    }
    const withQuality = (item) => {
      const candidate = objectValue(item);
      return candidate.quality
        ? candidate
        : { ...candidate, quality: qualityById.get(candidateId(candidate)) || null };
    };
    return {
      optical: asArray(optical).map(withQuality),
      sar: asArray(sar).map(withQuality),
      recommendation: objectValue(firstDefined(root.recommendation, root.scene_recommendation, root.selection)),
      subset: firstDefined(root.subset_note, root.pagination?.summary, root.is_subset),
    };
  }

  function candidateId(candidate) {
    return String(firstDefined(candidate.candidate_id, candidate.product_id, candidate.scene_id, candidate.system_index, candidate.id, ""));
  }

  function selectedCandidateIds() {
    const plan = objectValue(state.plan);
    const recommendation = candidateGroups().recommendation;
    const planIds = firstDefined(plan.selected_candidate_ids, plan.selected_scene_ids, plan.product_ids);
    if (Array.isArray(planIds)) return new Set(planIds.map(String));
    const recIds = firstDefined(
      recommendation.selected_candidate_ids,
      recommendation.selected_scene_ids,
      recommendation.selected_ids,
      recommendation.selected_optical_candidate_ids,
      recommendation.optical_candidate_ids,
    );
    const sarIds = asArray(recommendation.selected_sar_candidate_ids);
    return new Set([...asArray(recIds), ...sarIds].map(String));
  }

  function previewArtifactId(candidate, kind) {
    const previews = objectValue(candidate.previews);
    const raw =
      kind === "quality"
        ? firstDefined(
            candidate.quality_preview_artifact_id,
            candidate.quality_overlay_artifact_id,
            candidate.overlay_artifact_id,
            previews.quality_artifact_id,
            previews.quality,
          )
        : firstDefined(
            candidate.rgb_preview_artifact_id,
            candidate.preview_artifact_id,
            candidate.thumbnail_artifact_id,
            previews.rgb_artifact_id,
            previews.rgb,
          );
    if (raw && typeof raw === "object") return firstDefined(raw.artifact_id, raw.id);
    return raw;
  }

  function controlledArtifactUrl(artifactId) {
    if (!state.taskId || !artifactId) return null;
    return `${API_ROOT}/tasks/${encodeURIComponent(state.taskId)}/artifacts/${encodeURIComponent(String(artifactId))}`;
  }

  function previewCell(candidate, kind, label) {
    const cell = make("div", "candidate-preview");
    const artifactId = previewArtifactId(candidate, kind);
    if (artifactId) {
      const image = document.createElement("img");
      image.src = controlledArtifactUrl(artifactId);
      image.alt = `${candidateId(candidate)} ${label}`;
      image.loading = "lazy";
      image.addEventListener("error", () => {
        image.remove();
        cell.appendChild(make("span", null, "预览不可用"));
      }, { once: true });
      cell.appendChild(image);
    } else {
      cell.appendChild(make("span", null, firstDefined(candidate.preview_status, "预览尚未生成")));
    }
    cell.appendChild(make("span", "preview-label", label));
    return cell;
  }

  function addMetric(parent, label, value, formatter = formatFraction) {
    const row = make("div", "metric-row");
    row.appendChild(make("span", "metric-name", label));
    const formatted = formatter(value);
    row.appendChild(make("span", `metric-value${formatted === "未知" ? " unknown" : ""}`, formatted));
    parent.appendChild(row);
  }

  function renderCandidate(candidate, kind, selectedIds) {
    const id = candidateId(candidate);
    const card = make("article", `candidate-card${selectedIds.has(id) ? " selected" : ""}`);
    const previews = make("div", "candidate-preview-grid");
    previews.appendChild(previewCell(candidate, "rgb", kind === "sar" ? "SAR" : "原始 RGB"));
    previews.appendChild(previewCell(candidate, "quality", kind === "sar" ? "观测信息" : "质量叠加"));
    card.appendChild(previews);
    const body = make("div", "candidate-body");
    const titleRow = make("div", "candidate-title-row");
    const label = document.createElement("label");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "candidate-checkbox";
    checkbox.dataset.candidateId = id;
    checkbox.checked = selectedIds.has(id) || Boolean(candidate.selected);
    checkbox.addEventListener("change", () => {
      card.classList.toggle("selected", checkbox.checked);
      markPlanDirty("候选选择已改变，需更新计划并重新确认。 ");
    });
    label.append(checkbox, make("span", "candidate-id", id || "未提供候选 ID"));
    titleRow.appendChild(label);
    const sourceTag = make("span", "tag", kind === "sar" ? "Sentinel-1" : "Sentinel-2");
    titleRow.appendChild(sourceTag);
    body.appendChild(titleRow);
    const metaParts = [
      formatDate(firstDefined(candidate.acquired_at, candidate.acquisition_time, candidate.datetime, candidate.date)),
      firstDefined(candidate.tile_id, candidate.mgrs_tile, candidate.relative_orbit, candidate.orbit_pass),
      candidate.is_mock === true ? "模拟来源" : candidate.is_mock === false ? "真实目录元数据" : null,
    ].filter(Boolean);
    body.appendChild(make("p", "candidate-meta", metaParts.join(" · ")));
    const quality = objectValue(candidate.quality);
    if (kind === "sar") {
      addMetric(body, "AOI 覆盖", firstDefined(candidate.coverage_fraction, quality.valid_coverage_fraction));
      addMetric(body, "与目标光学时间差", firstDefined(candidate.delta_days, candidate.optical_delta_days), (value) =>
        value === null || value === undefined ? "未知" : `${value} 天`,
      );
      addMetric(body, "轨道", firstDefined(candidate.orbit_pass, candidate.orbit, "未知"), textValue);
    } else {
      addMetric(body, "有效覆盖 / AOI", firstDefined(quality.valid_coverage_fraction, candidate.valid_coverage_fraction));
      addMetric(body, "清晰观测 / AOI", firstDefined(quality.clear_aoi_fraction, candidate.clear_aoi_fraction));
      addMetric(body, "云影 / 有效覆盖", firstDefined(quality.cloud_shadow_fraction_on_valid, candidate.cloud_shadow_fraction_on_valid));
      addMetric(body, "已评估质量 / AOI", firstDefined(quality.quality_assessed_fraction, candidate.quality_assessed_fraction));
      const sceneCloud = firstDefined(candidate.scene_cloud_fraction, candidate.scene_cloud_percentage);
      if (sceneCloud !== undefined && sceneCloud !== null) {
        addMetric(body, "整景云量（仅粗筛）", sceneCloud, (value) => {
          const number = Number(value);
          if (!Number.isFinite(number)) return "未知";
          return `${number <= 1 ? (number * 100).toFixed(1) : number.toFixed(1)}%`;
        });
      }
    }
    const warnings = asArray(firstDefined(quality.warnings, candidate.warnings, []));
    warnings.forEach((warning) => body.appendChild(make("p", "candidate-warning", textValue(warning))));
    const status = firstDefined(quality.status, candidate.quality_status);
    if (status && String(status).toLowerCase() !== "available") {
      body.appendChild(make("span", "tag rejected", `质量：${status}`));
    }
    card.appendChild(body);
    return card;
  }

  function renderCandidates() {
    const groups = candidateGroups();
    const hasCandidates = groups.optical.length > 0 || groups.sar.length > 0;
    dom.candidateEmpty.hidden = hasCandidates;
    dom.candidateContent.hidden = !hasCandidates;
    if (!hasCandidates) return;
    const selected = selectedCandidateIds();
    clear(dom.opticalCandidates);
    groups.optical.forEach((candidate) => dom.opticalCandidates.appendChild(renderCandidate(objectValue(candidate), "optical", selected)));
    clear(dom.sarCandidates);
    groups.sar.forEach((candidate) => dom.sarCandidates.appendChild(renderCandidate(objectValue(candidate), "sar", selected)));
    dom.sarSection.hidden = groups.sar.length === 0;
    dom.opticalSubsetNote.textContent = groups.subset
      ? textValue(groups.subset)
      : "页面候选可能只是受限子集，不代表目录全部数据。";
    renderRecommendation(groups.recommendation);
  }

  function renderRecommendation(recommendation) {
    const sar = objectValue(firstDefined(recommendation.sar_recommendation, recommendation.sar));
    const sarStatus = firstDefined(sar.status, sar.recommendation, recommendation.sar_status);
    const labels = {
      not_needed: "当前不建议补充 SAR",
      recommended: "建议在授权窗口内补充 SAR",
      quality_unknown: "光学质量未知，暂不代替用户作决定",
      unavailable: "SAR 在当前条件下不可用",
    };
    dom.recommendationTitle.textContent = firstDefined(
      recommendation.title,
      labels[String(sarStatus || "").toLowerCase()],
      "已形成候选建议",
    );
    dom.recommendationReason.textContent = firstDefined(
      recommendation.reason,
      recommendation.rationale,
      sar.reason,
      "请核对模型选择与程序校验结果。",
    );
    clear(dom.recommendationGaps);
    const selected = asArray(firstDefined(
      recommendation.selected_candidate_ids,
      recommendation.selected_scene_ids,
      recommendation.selected_ids,
      recommendation.selected_optical_candidate_ids,
    ));
    selected.push(...asArray(recommendation.selected_sar_candidate_ids));
    selected.forEach((id) => dom.recommendationGaps.appendChild(make("span", "tag accepted", `建议：${id}`)));
    const rejected = firstDefined(recommendation.rejected_candidates, recommendation.rejected_suggestions, recommendation.rejected_ids, []);
    const rejectedItems = rejected && !Array.isArray(rejected) && typeof rejected === "object"
      ? Object.entries(rejected).map(([id, reason]) => `${id}：${reason}`)
      : asArray(rejected);
    rejectedItems.forEach((item) => {
      dom.recommendationGaps.appendChild(make("span", "tag rejected", `未选：${textValue(item)}`));
    });
    asArray(firstDefined(recommendation.missing_information, recommendation.information_gaps, recommendation.gaps, sar.information_gaps, [])).forEach((gap) => {
      dom.recommendationGaps.appendChild(make("span", "tag", `缺口：${textValue(gap)}`));
    });
  }

  function planFiles(plan) {
    return asArray(firstDefined(plan.files, plan.download_files, plan.file_manifest, []));
  }

  function planVersion(plan) {
    return firstDefined(plan.plan_version, plan.version);
  }

  function renderPlan() {
    const plan = objectValue(state.plan);
    const hasPlan = Boolean(firstDefined(plan.plan_hash, planVersion(plan), plan.files));
    dom.noPlanMessage.hidden = hasPlan;
    dom.planContent.hidden = !hasPlan;
    if (!hasPlan) {
      dom.planVersionBadge.textContent = "尚无计划";
      dom.planVersionBadge.className = "status-pill status-neutral";
      dom.cancelButton.disabled = !state.taskId;
      return;
    }
    const version = planVersion(plan);
    dom.planVersionBadge.textContent = `版本 ${textValue(version)} · ${String(plan.plan_hash || "无哈希").slice(0, 10)}`;
    dom.planVersionBadge.className = `status-pill ${statusClass(state.task?.status)}`;
    clear(dom.planSummary);
    addDefinition(dom.planSummary, "计划哈希", firstDefined(plan.plan_hash, "未提供"));
    addDefinition(dom.planSummary, "AOI / 网格版本", {
      aoi: firstDefined(plan.aoi_version, plan.aoi_hash),
      grid: firstDefined(plan.grid_version, plan.grid?.version),
    });
    addDefinition(dom.planSummary, "数据来源", firstDefined(plan.provider, state.task?.provider));
    addDefinition(dom.planSummary, "模拟标记", firstDefined(plan.is_mock, plan.simulated, state.task?.is_mock));
    addDefinition(dom.planSummary, "大小估计", firstDefined(plan.estimated_total_bytes, plan.estimated_size_bytes, plan.estimated_bytes) !== undefined
      ? `${formatBytes(firstDefined(plan.estimated_total_bytes, plan.estimated_size_bytes, plan.estimated_bytes))}（估计）`
      : "未知");
    const downloadRoot = firstDefined(plan.output_root, plan.requested_download_root, plan.download_root, plan.target_root);
    if (!state.planDirty && downloadRoot !== undefined) dom.downloadRoot.value = downloadRoot;
    dom.resolvedTaskPath.textContent = firstDefined(
      plan.resolved_task_directory,
      plan.resolved_task_dir,
      plan.task_directory,
      "等待服务端解析",
    );
    const grid = firstDefined(plan.grid_meters, plan.grid?.scale_meters, plan.grid?.resolution_meters, plan.grid?.scale);
    if (!state.planDirty && grid !== undefined) dom.gridMeters.value = grid;
    const bands = firstDefined(plan.optical_bands, plan.bands, plan.grid?.bands);
    if (!state.planDirty && Array.isArray(bands)) dom.bandsInput.value = bands.join(",");
    if (!state.planDirty && bands && typeof bands === "object" && !Array.isArray(bands)) {
      const commonBands = Object.values(bands).find((value) => Array.isArray(value));
      if (commonBands) dom.bandsInput.value = commonBands.join(",");
    }
    renderPlanFiles(planFiles(plan));
    const status = String(firstDefined(state.task?.status, "")).toUpperCase();
    const waiting = status === "WAITING_DOWNLOAD_APPROVAL";
    const terminal = TERMINAL_STATES.has(status);
    dom.approvalCheck.disabled = !waiting || state.planDirty;
    if (!waiting || state.planDirty) dom.approvalCheck.checked = false;
    dom.approveButton.disabled = !waiting || state.planDirty || !dom.approvalCheck.checked;
    dom.applyPlanButton.disabled = status === "DOWNLOADING" || status === "VERIFYING" || terminal;
    dom.cancelButton.disabled = terminal;
    dom.resumeButton.hidden = !["INTERRUPTED", "PARTIAL"].includes(status);
    dom.planDiff.hidden = !state.planDirty;
    if (state.planDirty && !dom.planDiff.textContent) {
      dom.planDiff.textContent = "本地编辑尚未提交。更新后会产生新计划版本，旧审批立即失效。";
    }
  }

  function renderPlanFiles(files) {
    clear(dom.planFileList);
    if (!files.length) {
      dom.planFileList.appendChild(make("p", "field-note", "当前计划未返回文件清单。"));
      return;
    }
    files.forEach((raw) => {
      const file = objectValue(raw);
      const id = String(firstDefined(file.file_id, file.id, file.relative_path, file.filename, ""));
      const row = make("label", "file-check-item");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.className = "plan-file-checkbox";
      checkbox.dataset.fileId = id;
      checkbox.checked = firstDefined(file.selected, file.included, true);
      const required = Boolean(firstDefined(file.required, file.is_required, false));
      checkbox.disabled = required;
      checkbox.addEventListener("change", () => markPlanDirty("正式文件清单已改变，需更新计划并重新确认。"));
      const description = make("span");
      description.appendChild(make("span", null, firstDefined(file.display_name, file.relative_path, file.filename, id)));
      description.appendChild(make("small", null, [
        firstDefined(file.kind, file.role, file.data_type),
        required ? "核心依赖，不可取消" : null,
        firstDefined(file.bands, file.band) ? `波段 ${textValue(firstDefined(file.bands, file.band))}` : null,
      ].filter(Boolean).join(" · ")));
      row.append(checkbox, description, make("span", "file-size", firstDefined(file.expected_size_bytes, file.estimated_size_bytes, file.estimated_bytes) !== undefined
        ? formatBytes(firstDefined(file.expected_size_bytes, file.estimated_size_bytes, file.estimated_bytes))
        : "大小未知"));
      dom.planFileList.appendChild(row);
    });
  }

  function markPlanDirty(message) {
    state.planDirty = true;
    dom.planDiff.hidden = false;
    dom.planDiff.textContent = message;
    dom.approvalCheck.checked = false;
    dom.approveButton.disabled = true;
  }

  function checkedValues(selector, attribute) {
    return Array.from(document.querySelectorAll(selector))
      .filter((input) => input.checked)
      .map((input) => input.dataset[attribute])
      .filter(Boolean);
  }

  async function applyPlanChanges() {
    if (!state.taskId || !state.plan) return;
    const bands = dom.bandsInput.value.split(",").map((band) => band.trim()).filter(Boolean);
    const payload = {
      plan_version: planVersion(state.plan),
      plan_hash: state.plan.plan_hash,
      selected_candidate_ids: checkedValues(".candidate-checkbox", "candidateId"),
      selected_file_ids: checkedValues(".plan-file-checkbox", "fileId"),
      download_root: dom.downloadRoot.value.trim(),
      grid_meters: Number(dom.gridMeters.value),
      optical_bands: bands,
    };
    dom.applyPlanButton.disabled = true;
    try {
      const response = await apiRequest(`/tasks/${encodeURIComponent(state.taskId)}/plan`, {
        method: "PATCH",
        body: payload,
      });
      state.plan = objectValue(response.plan).plan_hash ? response.plan : response;
      state.planDirty = false;
      dom.approvalCheck.checked = false;
      showMessage("计划已更新。请重新核对版本、哈希、清单、网格和解析后的绝对路径。 ");
      await refreshTaskData();
    } catch (error) {
      if (error.status === 409) scheduleRefresh(0);
      showMessage(`更新计划失败：${error.message}`, "error");
    } finally {
      dom.applyPlanButton.disabled = false;
    }
  }

  function idempotencyKey() {
    const planKey = `${state.taskId}:${state.plan?.plan_hash || planVersion(state.plan)}`;
    if (state.approvalKeys.has(planKey)) return state.approvalKeys.get(planKey);
    const key = window.crypto?.randomUUID
      ? window.crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    state.approvalKeys.set(planKey, key);
    return key;
  }

  async function approvePlan() {
    if (!state.taskId || !state.plan || !dom.approvalCheck.checked || state.planDirty) return;
    dom.approveButton.disabled = true;
    try {
      await apiRequest(`/tasks/${encodeURIComponent(state.taskId)}/approve`, {
        method: "POST",
        body: {
          plan_version: planVersion(state.plan),
          plan_hash: state.plan.plan_hash,
          idempotency_key: idempotencyKey(),
        },
      });
      showMessage("审批已记录。正式下载只会按已确认的计划执行；重复请求使用同一幂等键。 ");
      await refreshTaskData();
    } catch (error) {
      if (error.status === 409) scheduleRefresh(0);
      showMessage(`审批失败：${error.message}`, "error");
    }
  }

  async function taskAction(action, label) {
    if (!state.taskId) return;
    try {
      await apiRequest(`/tasks/${encodeURIComponent(state.taskId)}/${action}`, {
        method: "POST",
        body: {},
      });
      showMessage(label);
      await refreshTaskData();
      if (action === "resume") connectEventStream();
    } catch (error) {
      showMessage(`${label}失败：${error.message}`, "error");
    }
  }

  function renderDownloads() {
    clear(dom.downloadFiles);
    const files = Array.from(state.fileProgress.values());
    if (!files.length) {
      dom.downloadEmpty.hidden = false;
      dom.downloadSummary.textContent = "等待用户确认";
    } else {
      dom.downloadEmpty.hidden = true;
      const completed = files.filter((file) => String(file.status).toUpperCase() === "COMPLETED").length;
      const failed = files.filter((file) => String(file.status).toUpperCase() === "FAILED").length;
      dom.downloadSummary.textContent = `${completed}/${files.length} 文件完成${failed ? ` · ${failed} 失败` : ""}`;
      files.forEach((file) => dom.downloadFiles.appendChild(renderDownloadFile(file)));
    }
    renderResultArtifacts();
  }

  function renderDownloadFile(file) {
    const card = make("article", "download-file");
    const head = make("div", "download-file-head");
    head.appendChild(make("span", "download-file-name", firstDefined(file.relative_path, file.filename, file.file_id, file.id)));
    const status = firstDefined(file.status, file.stage, "QUEUED");
    head.appendChild(make("span", `status-pill ${statusClass(status)}`, statusName(status)));
    card.appendChild(head);
    const received = firstDefined(file.received_bytes, file.bytes_received, file.downloaded_bytes);
    const total = firstDefined(file.total_bytes, file.content_length);
    const meta = make("div", "download-file-meta");
    meta.appendChild(make("span", null, `已接收 ${formatBytes(received)}`));
    meta.appendChild(make("span", null, Number.isFinite(Number(total)) ? `总长度 ${formatBytes(total)}` : "总长度未知"));
    meta.appendChild(make("span", null, formatRate(firstDefined(file.rate_bytes_per_second, file.bytes_per_second, file.rate))));
    const attempt = firstDefined(file.attempt, file.attempt_number);
    if (attempt !== undefined) meta.appendChild(make("span", null, `尝试 ${attempt}`));
    card.appendChild(meta);
    if (Number.isFinite(Number(total)) && Number(total) > 0 && Number.isFinite(Number(received))) {
      const progress = document.createElement("progress");
      progress.max = Number(total);
      progress.value = Math.min(Number(received), Number(total));
      progress.setAttribute("aria-label", `${firstDefined(file.filename, file.file_id)} 下载进度`);
      card.appendChild(progress);
    } else if (String(status).toUpperCase() === "PREPARING_REMOTE") {
      card.appendChild(make("p", "verification-result", "平台正在生成文件；尚无可验证的总长度，不显示虚假百分比。"));
    }
    const verification = objectValue(firstDefined(file.verification, file.verification_result));
    if (Object.keys(verification).length || file.sha256 || file.checksum) {
      card.appendChild(make("p", "verification-result", `校验：${textValue(firstDefined(verification.status, file.verification_status, status))} · SHA-256 ${textValue(firstDefined(file.checksum_sha256, file.sha256, file.checksum, verification.sha256), "未提供")}`));
    }
    if (file.error || file.error_message) {
      card.appendChild(make("p", "candidate-warning", `失败原因：${firstDefined(file.error, file.error_message)}`));
    }
    return card;
  }

  function collectArtifacts() {
    const taskArtifacts = asArray(firstDefined(state.task?.artifacts, state.task?.result_artifacts, []));
    const fileArtifacts = Array.from(state.fileProgress.values()).flatMap((file) => {
      const values = [];
      if (file.thumbnail_artifact_id) values.push({ artifact_id: file.thumbnail_artifact_id, label: `${firstDefined(file.filename, file.file_id)} 本地缩略图` });
      if (file.manifest_artifact_id) values.push({ artifact_id: file.manifest_artifact_id, label: "下载清单" });
      return values;
    });
    const planArtifacts = [];
    if (state.plan?.manifest_artifact_id) planArtifacts.push({ artifact_id: state.plan.manifest_artifact_id, label: "下载清单" });
    const seen = new Set();
    return [...taskArtifacts, ...fileArtifacts, ...planArtifacts].filter((raw) => {
      const item = typeof raw === "string" ? { artifact_id: raw } : objectValue(raw);
      const id = firstDefined(item.artifact_id, item.id);
      if (!id || seen.has(String(id))) return false;
      seen.add(String(id));
      return true;
    });
  }

  function renderResultArtifacts() {
    clear(dom.resultArtifacts);
    collectArtifacts().forEach((raw) => {
      const item = typeof raw === "string" ? { artifact_id: raw } : objectValue(raw);
      const id = firstDefined(item.artifact_id, item.id);
      const link = make("a", "artifact-link", firstDefined(item.label, item.name, item.kind, id));
      link.href = controlledArtifactUrl(id);
      link.target = "_blank";
      link.rel = "noopener";
      dom.resultArtifacts.appendChild(link);
    });
  }

  function eventSeverity(type) {
    const lowered = String(type).toLowerCase();
    if (lowered.includes("failed") || lowered.includes("error") || lowered.includes("rejected")) return "error";
    if (lowered.includes("finished") || lowered.includes("completed") || lowered.includes("accepted")) return "success";
    return "";
  }

  function renderTimeline() {
    clear(dom.eventTimeline);
    state.events.forEach((event) => {
      const type = firstDefined(event.event_type, event.type, "event");
      const item = make("li", `event-item ${eventSeverity(type)}`.trim());
      const head = make("div", "event-head");
      head.appendChild(make("span", "event-type", `${Number.isFinite(Number(event.sequence)) ? `#${event.sequence} ` : ""}${type}`));
      head.appendChild(make("time", "event-time", formatDate(firstDefined(event.timestamp, event.created_at))));
      item.appendChild(head);
      item.appendChild(make("p", "event-summary", firstDefined(event.summary, event.message, event.stage, "事件已记录")));
      const details = make("details", "event-details");
      details.appendChild(make("summary", null, "查看原始事件"));
      details.appendChild(make("pre", null, prettyJson(event)));
      appendArtifactLoader(details, event, "载入完整详情 artifact");
      item.appendChild(details);
      dom.eventTimeline.appendChild(item);
    });
    if (dom.autoFollow.checked && !dom.eventTimeline.hidden) {
      dom.eventTimeline.scrollTop = dom.eventTimeline.scrollHeight;
    }
  }

  function artifactReference(value) {
    const details = objectValue(value);
    const raw = firstDefined(
      details.request_artifact_id,
      details.response_artifact_id,
      details.details_artifact_id,
      details.artifact_id,
      typeof details.details_ref === "string" ? details.details_ref : objectValue(details.details_ref).artifact_id,
      typeof details.artifact_ref === "string" ? details.artifact_ref : objectValue(details.artifact_ref).artifact_id,
    );
    return raw || null;
  }

  function appendArtifactLoader(parent, value, label) {
    const artifactId = artifactReference(value);
    if (!artifactId) return;
    const button = make("button", "button button-ghost button-small load-artifact-button", label);
    button.type = "button";
    button.addEventListener("click", async () => {
      button.disabled = true;
      button.textContent = "载入中…";
      try {
        const response = await fetch(controlledArtifactUrl(artifactId), { credentials: "same-origin" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const text = await response.text();
        const pre = make("pre", null, prettyJson(text));
        parent.appendChild(pre);
        button.remove();
      } catch (error) {
        button.disabled = false;
        button.textContent = `载入失败：${error.message}，点击重试`;
      }
    });
    parent.appendChild(button);
  }

  function llmCallGroups() {
    const groups = new Map();
    state.llmRecords.forEach((record, index) => {
      const callId = String(firstDefined(record.call_id, record.id, `record-${index}`));
      groups.set(callId, { callId, record, events: [] });
    });
    state.events.forEach((event) => {
      const type = String(firstDefined(event.event_type, event.type, ""));
      const callId = firstDefined(event.call_id, eventDetails(event).call_id);
      if (!callId || (!type.startsWith("llm.") && !type.startsWith("action.") && !type.startsWith("tool."))) return;
      const id = String(callId);
      if (!groups.has(id)) groups.set(id, { callId: id, record: null, events: [] });
      groups.get(id).events.push(event);
    });
    return Array.from(groups.values());
  }

  function eventOfType(group, type) {
    return group.events.filter((event) => String(firstDefined(event.event_type, event.type, "")) === type);
  }

  function logBlock(title, value) {
    const block = make("section", "log-block");
    block.appendChild(make("h4", null, title));
    block.appendChild(make("pre", null, prettyJson(value)));
    return block;
  }

  function persistedCallRequest(record) {
    const item = objectValue(record);
    if (item.request && typeof item.request === "object") return item.request;
    return {
      purpose: item.purpose,
      model_profile: item.model_profile,
      model_id: item.model_id,
      prompt_version: item.prompt_version,
      messages: item.messages,
      response_schema: item.response_schema,
      visible_context: item.visible_context,
      effective_parameters: item.effective_parameters,
      candidate_data: item.candidate_data,
      streaming: item.streaming,
      attempt: item.attempt,
      requested_at: item.requested_at,
      contains_mock: item.contains_mock,
    };
  }

  function persistedCallResponse(record) {
    const item = objectValue(record);
    return {
      status: item.status,
      actual_response: item.response,
      validation: item.validation,
      recommendation_accepted: item.accepted,
      proposed_action: item.action,
      created_at: item.created_at,
      updated_at: item.updated_at,
    };
  }

  function renderLlmCalls() {
    clear(dom.llmCalls);
    const groups = llmCallGroups();
    if (!groups.length) {
      dom.llmCalls.appendChild(make("div", "empty-state compact", "尚无模型调用。"));
      return;
    }
    groups.forEach((group) => {
      const requestEvents = eventOfType(group, "llm.request");
      const responseEvents = eventOfType(group, "llm.response");
      const deltaEvents = eventOfType(group, "llm.delta");
      const errorEvents = eventOfType(group, "llm.error");
      const actionEvents = group.events.filter((event) => String(event.event_type || event.type || "").startsWith("action."));
      const toolEvents = group.events.filter((event) => String(event.event_type || event.type || "").startsWith("tool."));
      const request = requestEvents.at(-1) || objectValue(group.record).request || group.record;
      const requestDetails = eventDetails(objectValue(request));
      const purpose = firstDefined(
        requestDetails.purpose,
        requestDetails.problem,
        objectValue(group.record).purpose,
        objectValue(group.record).stage,
        "结构化模型调用",
      );
      const provider = firstDefined(
        requestDetails.profile,
        requestDetails.provider,
        requestDetails.model,
        objectValue(objectValue(group.record).request).model_profile,
        objectValue(objectValue(objectValue(group.record).request).payload).model,
        objectValue(group.record).profile,
        objectValue(group.record).model,
        state.task?.model_profile,
        "未知 profile",
      );
      const status = errorEvents.length
        ? "失败"
        : responseEvents.length || objectValue(group.record).response
          ? "已返回"
          : deltaEvents.length
            ? "真实流式响应中"
            : "请求已发送，等待响应";
      const details = document.createElement("details");
      details.className = "llm-call";
      const summary = document.createElement("summary");
      summary.appendChild(make("span", "llm-call-title", purpose));
      summary.appendChild(make("span", "llm-call-subtitle", `${group.callId} · ${provider} · ${status}`));
      details.appendChild(summary);
      const body = make("div", "llm-call-body");
      body.appendChild(logBlock("调用目的", purpose));
      if (group.record) {
        body.appendChild(logBlock("实际发送的完整脱敏 messages、结构要求、候选数据与生效参数", persistedCallRequest(group.record)));
        requestEvents.forEach((item, index) => {
          const block = logBlock(`请求事件索引${requestEvents.length > 1 ? ` · 尝试 ${index + 1}` : ""}`, item);
          appendArtifactLoader(block, item, "载入持久化完整请求 artifact");
          body.appendChild(block);
        });
      } else if (requestEvents.length) {
        requestEvents.forEach((item, index) => {
          const block = logBlock(`实际发送的完整脱敏请求${requestEvents.length > 1 ? ` · 尝试 ${index + 1}` : ""}`, item);
          appendArtifactLoader(block, item, "载入完整请求 artifact");
          body.appendChild(block);
        });
      }
      if (deltaEvents.length) {
        const deltas = deltaEvents.map((item) => firstDefined(eventDetails(item).delta, eventDetails(item).content, item.delta, item.summary, "")).join("");
        body.appendChild(logBlock("供应商真实流式片段（完整 JSON 校验前不执行动作）", deltas));
      }
      if (group.record && (objectValue(group.record).response || objectValue(group.record).validation || objectValue(group.record).status)) {
        body.appendChild(logBlock("模型实际返回、解析与参数校验结果", persistedCallResponse(group.record)));
        responseEvents.forEach((item, index) => {
          const block = logBlock(`响应事件索引${responseEvents.length > 1 ? ` · 响应 ${index + 1}` : ""}`, item);
          appendArtifactLoader(block, item, "载入持久化完整响应 artifact");
          body.appendChild(block);
        });
      } else if (responseEvents.length) {
        responseEvents.forEach((item, index) => {
          const block = logBlock(`模型实际返回与解析校验${responseEvents.length > 1 ? ` · 响应 ${index + 1}` : ""}`, item);
          appendArtifactLoader(block, item, "载入完整响应 artifact");
          body.appendChild(block);
        });
      }
      errorEvents.forEach((item, index) => body.appendChild(logBlock(`模型错误 / 重试记录 ${index + 1}`, item)));
      if (actionEvents.length) body.appendChild(logBlock("模型建议是否被程序接受", actionEvents));
      if (objectValue(group.record).action) {
        body.appendChild(logBlock("程序校验后实际采用的动作与参数", {
          accepted: objectValue(group.record).accepted,
          action: objectValue(group.record).action,
        }));
      }
      if (toolEvents.length) body.appendChild(logBlock("程序实际执行的工具与参数", toolEvents));
      if (!responseEvents.length && !objectValue(group.record).response && !errorEvents.length) {
        body.appendChild(make("p", "field-note", "当前供应商尚未返回完整响应；页面不会用动画伪装流式输出，也不会提前执行模型动作。"));
      }
      details.appendChild(body);
      dom.llmCalls.appendChild(details);
    });
  }

  function switchLogTab(which) {
    const showLlm = which === "llm";
    dom.llmTab.classList.toggle("active", showLlm);
    dom.eventsTab.classList.toggle("active", !showLlm);
    dom.llmTab.setAttribute("aria-selected", String(showLlm));
    dom.eventsTab.setAttribute("aria-selected", String(!showLlm));
    dom.llmCalls.hidden = !showLlm;
    dom.eventTimeline.hidden = showLlm;
    if (!showLlm && dom.autoFollow.checked) dom.eventTimeline.scrollTop = dom.eventTimeline.scrollHeight;
  }

  function bindDom() {
    [
      "global-message",
      "connection-badge",
      "mode-badge",
      "task-form",
      "task-query",
      "model-profile",
      "data-provider",
      "geojson-input-group",
      "registered-input-group",
      "geojson-file",
      "geojson-text",
      "registered-aoi",
      "aoi-summary",
      "aoi-preview-path",
      "aoi-summary-title",
      "aoi-summary-text",
      "create-task-button",
      "update-request-button",
      "plan-version-badge",
      "no-plan-message",
      "plan-content",
      "plan-summary",
      "download-root",
      "apply-plan-button",
      "resolved-task-path",
      "grid-meters",
      "bands-input",
      "plan-file-list",
      "plan-diff",
      "approval-check",
      "approve-button",
      "cancel-button",
      "resume-button",
      "task-status-badge",
      "task-id-label",
      "task-empty",
      "task-card",
      "task-fields",
      "task-warnings",
      "refresh-button",
      "candidate-empty",
      "candidate-content",
      "optical-subset-note",
      "optical-candidates",
      "recommendation-title",
      "recommendation-reason",
      "recommendation-gaps",
      "sar-section",
      "sar-candidates",
      "download-summary",
      "download-empty",
      "download-files",
      "result-artifacts",
      "auto-follow",
      "reconnect-button",
      "llm-tab",
      "events-tab",
      "llm-calls",
      "event-timeline",
      "footer-config-note",
    ].forEach((id) => {
      const camel = id.replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
      dom[camel] = byId(id);
    });
  }

  function bindEvents() {
    dom.taskForm.addEventListener("submit", createTask);
    dom.updateRequestButton.addEventListener("click", updateRequest);
    dom.modelProfile.addEventListener("change", updateModeBadge);
    dom.dataProvider.addEventListener("change", updateModeBadge);
    document.querySelectorAll('input[name="aoi-mode"]').forEach((radio) => radio.addEventListener("change", switchAoiMode));
    dom.geojsonFile.addEventListener("change", async () => {
      const file = dom.geojsonFile.files?.[0];
      if (!file) return;
      try {
        dom.geojsonText.value = await file.text();
        updateGeojsonPreview();
      } catch (error) {
        showMessage(`无法读取 GeoJSON 文件：${error.message}`, "error");
      }
    });
    dom.geojsonText.addEventListener("input", updateGeojsonPreview);
    dom.registeredAoi.addEventListener("change", updateRegisteredAoiPreview);
    dom.refreshButton.addEventListener("click", refreshTaskData);
    dom.downloadRoot.addEventListener("input", () => markPlanDirty("保存目录已改变；提交后服务端会解析绝对路径并生成新计划。"));
    dom.gridMeters.addEventListener("input", () => markPlanDirty("共享网格已改变；提交后会生成新计划。"));
    dom.bandsInput.addEventListener("input", () => markPlanDirty("光学波段已改变；提交后会生成新计划。"));
    dom.applyPlanButton.addEventListener("click", applyPlanChanges);
    dom.approvalCheck.addEventListener("change", () => {
      const waiting = String(state.task?.status || "").toUpperCase() === "WAITING_DOWNLOAD_APPROVAL";
      dom.approveButton.disabled = !dom.approvalCheck.checked || !waiting || state.planDirty;
    });
    dom.approveButton.addEventListener("click", approvePlan);
    dom.cancelButton.addEventListener("click", () => taskAction("cancel", "取消请求已提交。已完成文件不会被隐式删除。"));
    dom.resumeButton.addEventListener("click", () => taskAction("resume", "恢复请求已提交；服务端会核对原审批和文件哈希。"));
    dom.reconnectButton.addEventListener("click", connectEventStream);
    dom.llmTab.addEventListener("click", () => switchLogTab("llm"));
    dom.eventsTab.addEventListener("click", () => switchLogTab("events"));
    window.addEventListener("beforeunload", () => closeEventStream());
  }

  async function initialize() {
    bindDom();
    bindEvents();
    renderAll();
    await loadConfig();
    const taskId = new URL(window.location.href).searchParams.get("task_id");
    if (taskId) {
      try {
        await activateTask(taskId, false);
      } catch (error) {
        showMessage(`恢复任务失败：${error.message}`, "error");
      }
    }
  }

  document.addEventListener("DOMContentLoaded", initialize);
})();
