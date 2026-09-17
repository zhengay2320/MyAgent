from __future__ import annotations

from datetime import date
from time import perf_counter
from typing import Any

from eo_agent.config import Settings
from eo_agent.llm.base import LLMClient, LLMResult
from eo_agent.reports.facts import build_report_facts
from eo_agent.schemas import (
    ObservationData,
    ResourceArgs,
    SearchArgs,
    TaskSpec,
    TaskStatus,
    TimeWindow,
    ToolResult,
    TraceEvent,
    VerificationData,
    VerifyArgs,
)
from eo_agent.tools.base import ToolContext
from eo_agent.tools.executor import ToolExecutor
from eo_agent.workflow.state import WorkflowState


def _month_window(value: str) -> TimeWindow:
    try:
        year, month = (int(item) for item in value.split("-"))
        start = date(year, month, 1)
        end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"非法月份: {value}") from exc
    return TimeWindow(start=start, end=end, label=value)


class WorkflowNodes:
    def __init__(self, settings: Settings, llm: LLMClient, executor: ToolExecutor) -> None:
        self.settings = settings
        self.llm = llm
        self.executor = executor

    def _trace(
        self,
        state: WorkflowState,
        stage: str,
        event_type: str,
        duration_ms: float,
        implementation_id: str | None = None,
        input_summary: dict[str, Any] | None = None,
        output_summary: dict[str, Any] | None = None,
        error: str | None = None,
        usage: dict[str, int | None] | None = None,
    ) -> None:
        state["trace"].append(
            TraceEvent(
                sequence=len(state["trace"]) + 1,
                task_id=state["task_id"],
                stage=stage,
                event_type=event_type,
                implementation_id=implementation_id,
                model_profile=state["request"].model_profile,
                input_summary=input_summary or {},
                output_summary=output_summary or {},
                duration_ms=duration_ms,
                error=error,
                usage=usage,
            )
        )

    def _record_llm(self, state: WorkflowState, stage: str, result: LLMResult[Any]) -> None:
        state["llm_call_count"] += result.metadata.attempts
        if state["llm_call_count"] > self.settings.budgets.max_llm_calls:
            raise RuntimeError("LLM 调用预算已耗尽")
        self._trace(
            state,
            stage,
            "llm_call",
            result.metadata.duration_ms,
            implementation_id=result.metadata.returned_model or result.metadata.requested_model,
            output_summary={
                "provider": result.metadata.provider,
                "output_protocol": result.metadata.output_protocol,
                "retries": result.metadata.retries,
                "schema_repairs": result.metadata.schema_repairs,
            },
            error=result.metadata.error,
            usage=result.metadata.usage,
        )

    def _check_llm_budget(self, state: WorkflowState) -> None:
        if state["llm_call_count"] >= self.settings.budgets.max_llm_calls:
            raise RuntimeError("LLM 调用预算已耗尽；调用未开始")

    def parse(self, state: WorkflowState) -> WorkflowState:
        state["stage"] = "parse"
        self._check_llm_budget(state)
        result = self.llm.parse_task(state["request"].query, state["request"].aoi_id)
        self._record_llm(state, "parse", result)
        state["draft"] = result.payload
        model_id = result.metadata.returned_model or result.metadata.requested_model
        state["steps"].append(
            "parse: 将自然语言解析为候选任务"
            f"（provider={result.metadata.provider}, profile={result.metadata.profile}, "
            f"model={model_id}）"
        )
        return state

    def validate(self, state: WorkflowState) -> WorkflowState:
        started = perf_counter()
        state["stage"] = "validate"
        draft = state["draft"]
        request = state["request"]
        assert draft is not None
        error: str | None = None
        if draft.task_type != "two_period_change":
            state["status"] = TaskStatus.UNSUPPORTED_TASK_TYPE
            error = "仅支持两时期变化对比，不支持连续时序监测"
        elif draft.missing_fields:
            state["status"] = TaskStatus.WAITING_INPUT
            error = f"缺少待确认字段: {', '.join(draft.missing_fields)}"
        elif request.aoi_id is None and draft.aoi_text == "武汉":
            state["status"] = TaskStatus.WAITING_INPUT
            error = "“武汉”范围有歧义，请明确 aoi_id；不会自动缩为演示区"
        else:
            aoi_id = request.aoi_id
            if aoi_id is None:
                matches = [
                    key
                    for key, item in self.settings.aois.items()
                    if item["name"] == draft.aoi_text
                ]
                aoi_id = matches[0] if len(matches) == 1 else None
            if not aoi_id or aoi_id not in self.settings.aois:
                state["status"] = TaskStatus.WAITING_INPUT
                error = "未知区域，请提供已登记的 aoi_id"
            else:
                aoi = self.settings.aois[aoi_id]
                if draft.aoi_text not in {None, aoi["name"], "武汉"}:
                    state["status"] = TaskStatus.WAITING_INPUT
                    error = "显式 aoi_id 与文本区域矛盾，请确认"
                elif float(aoi["area_km2"]) > self.settings.app.area_soft_limit_km2:
                    state["status"] = TaskStatus.WAITING_INPUT
                    error = "区域超过 400 km² 软上限，请缩小区域；当前版本不自动裁剪"
                else:
                    try:
                        baseline = _month_window(str(draft.baseline_month))
                        target = _month_window(str(draft.target_month))
                        if baseline.start >= target.start:
                            raise ValueError("基准期必须早于目标期")
                    except ValueError as exc:
                        state["status"] = TaskStatus.WAITING_INPUT
                        error = str(exc)
                    else:
                        state["task"] = TaskSpec(
                            task_id=state["task_id"],
                            query=request.query,
                            aoi_id=aoi_id,
                            aoi_name=aoi["name"],
                            area_km2=float(aoi["area_km2"]),
                            geometry_ref=aoi["geometry_ref"],
                            baseline=baseline,
                            target=target,
                            grid_meters=self.settings.app.grid_meters,
                            seed=self.settings.app.seed,
                            budgets=self.settings.budgets.model_dump(exclude={"recursion_limit"}),
                            schema_version=self.settings.app.schema_version,
                            workflow_version=self.settings.app.workflow_version,
                            model_profile=request.model_profile,
                        )
                        state["status"] = TaskStatus.RUNNING
        state["error"] = error
        state["steps"].append("validate: 程序校验 AOI、月份、任务类型和预算")
        self._trace(
            state,
            "validate",
            "validation",
            round((perf_counter() - started) * 1000, 3),
            implementation_id="programmatic-validator-v1",
            output_summary={"status": state["status"].value},
            error=error,
        )
        return state

    def _context(self, state: WorkflowState) -> ToolContext:
        task = state["task"]
        if task is None:
            raise RuntimeError("工具上下文缺少已校验任务")
        return ToolContext(
            task_id=state["task_id"],
            seed=self.settings.app.seed,
            scenario_config=self.settings.scenarios[state["request"].scenario],
            scenario_state=state["scenario_state"],
            resources=state["resources"],
            area_denominator_km2=task.area_km2,
            area_denominator_kind="task_aoi_area",
        )

    def _tool(
        self,
        state: WorkflowState,
        name: str,
        stage: str,
        args: dict[str, Any],
        *,
        update_resource: bool = True,
    ) -> ToolResult:
        attempts = self.executor.execute(name, stage, args, self._context(state))
        for attempt in attempts:
            result = attempt.result
            state["tool_results"].append(result)
            self._trace(
                state,
                stage,
                "tool_call",
                attempt.duration_ms,
                implementation_id=result.implementation_id,
                input_summary={"tool": name, "resource_id": args.get("resource_id")},
                output_summary={
                    "status": result.status.value,
                    "data_kind": result.data.kind if result.data else None,
                },
                error=result.error,
            )
        final = attempts[-1].result
        if final.status.value != "succeeded" or final.data is None:
            raise RuntimeError(f"工具 {name} 失败: {final.error}")
        if update_resource:
            state["current_resource_id"] = final.data.resource_id
        state["steps"].append(f"{stage}: 调用 {name} [{final.implementation_id}]")
        return final

    def search(self, state: WorkflowState) -> WorkflowState:
        state["stage"] = "search"
        task = state["task"]
        assert task is not None
        result = self._tool(
            state,
            "search_observations",
            "search",
            SearchArgs(
                task_id=task.task_id,
                aoi_id=task.aoi_id,
                baseline=task.baseline,
                target=task.target,
            ).model_dump(mode="json"),
        )
        if isinstance(result.data, ObservationData) and not result.data.available:
            state["status"] = TaskStatus.PARTIAL
            state["error"] = "没有可用模拟观测，无法判断变化"
        return state

    def prepare(self, state: WorkflowState) -> WorkflowState:
        state["stage"] = "prepare"
        self._tool_resource(state, "prepare_data", "prepare")
        return state

    def quality(self, state: WorkflowState) -> WorkflowState:
        state["stage"] = "quality"
        self._tool_resource(state, "inspect_quality", "quality", update_resource=False)
        return state

    def choose_reconstruction(self, state: WorkflowState) -> WorkflowState:
        state["stage"] = "choose_reconstruction"
        quality = state["tool_results"][-1].data
        visible = {
            "usable": getattr(quality, "usable", False),
            "cloud_fraction": getattr(quality, "cloud_fraction", None),
            "reconstruction_recommended": getattr(quality, "reconstruction_recommended", False),
        }
        self._check_llm_budget(state)
        result = self.llm.choose_action(visible, ["reconstruct", "skip_reconstruction"])
        self._record_llm(state, "choose_reconstruction", result)
        state["scenario_state"]["chosen_reconstruction"] = int(
            result.payload.action == "reconstruct"
        )
        state["steps"].append(
            f"choose_reconstruction: {result.payload.action}（{result.payload.reason_summary}）"
        )
        return state

    def reconstruct(self, state: WorkflowState) -> WorkflowState:
        state["stage"] = "reconstruct"
        self._tool_resource(state, "reconstruct_optical", "reconstruct")
        return state

    def detect(self, state: WorkflowState) -> WorkflowState:
        state["stage"] = "detect"
        self._tool_resource(state, "detect_change", "detect")
        return state

    def verify(self, state: WorkflowState) -> WorkflowState:
        state["stage"] = "verify"
        current = self._current(state)
        result = self._tool(
            state,
            "verify_change",
            "verify",
            VerifyArgs(
                task_id=state["task_id"],
                resource_id=current,
                evidence_round=state["evidence_count"],
            ).model_dump(),
        )
        data = result.data
        assert isinstance(data, VerificationData)
        if data.sufficient:
            state["status"] = TaskStatus.COMPLETED
        return state

    def evidence(self, state: WorkflowState) -> WorkflowState:
        state["stage"] = "evidence"
        latest = state["tool_results"][-1].data
        assert isinstance(latest, VerificationData)
        self._check_llm_budget(state)
        decision = self.llm.choose_action(
            {"verdict": latest.verdict, "evidence_round": latest.evidence_round},
            ["fetch_additional_evidence", "keep_partial"],
        )
        self._record_llm(state, "evidence", decision)
        if decision.payload.action != "fetch_additional_evidence":
            state["status"] = TaskStatus.PARTIAL
            state["error"] = "核验证据不足，模型选择保留部分结果"
            return state
        state["evidence_count"] += 1
        state["replan_count"] += 1
        self._tool_resource(state, "fetch_additional_observation", "evidence")
        state["steps"].append("evidence: 新观测引用已生效，将重新执行预处理、检测和核验")
        return state

    def mark_partial(self, state: WorkflowState) -> WorkflowState:
        """Apply an insufficient-evidence terminal state outside conditional routing."""
        state["stage"] = "mark_partial"
        state["status"] = TaskStatus.PARTIAL
        state["error"] = "达到补证上限或证据仍不足，无法形成可靠判断"
        state["steps"].append("mark_partial: 证据不足或补证预算结束，保留部分结果")
        return state

    def report(self, state: WorkflowState) -> WorkflowState:
        state["stage"] = "report"
        if state["status"] == TaskStatus.RUNNING:
            state["status"] = TaskStatus.PARTIAL
        facts = build_report_facts(state)
        self._check_llm_budget(state)
        result = self.llm.summarize(facts)
        self._record_llm(state, "report", result)
        facts.llm_call_count = state["llm_call_count"]
        state["report_facts"] = facts
        state["report_summary"] = result.payload
        state["steps"].append("report: 从结构化事实生成 JSON、Markdown 和 HTML 报告")
        facts.steps = list(state["steps"])
        return state

    def _current(self, state: WorkflowState) -> str:
        value = state["current_resource_id"]
        if value is None:
            raise ValueError("缺少当前资源")
        return value

    def _tool_resource(
        self, state: WorkflowState, name: str, stage: str, *, update_resource: bool = True
    ) -> ToolResult:
        return self._tool(
            state,
            name,
            stage,
            ResourceArgs(task_id=state["task_id"], resource_id=self._current(state)).model_dump(),
            update_resource=update_resource,
        )
