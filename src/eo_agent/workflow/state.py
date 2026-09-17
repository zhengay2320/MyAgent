from __future__ import annotations

from typing import Any, TypedDict

from eo_agent.schemas import (
    ReportFacts,
    TaskDraft,
    TaskRequest,
    TaskSpec,
    TaskStatus,
    ToolResult,
    TraceEvent,
)


class WorkflowState(TypedDict):
    request: TaskRequest
    task_id: str
    status: TaskStatus
    stage: str
    draft: TaskDraft | None
    task: TaskSpec | None
    current_resource_id: str | None
    resources: dict[str, str]
    tool_results: list[ToolResult]
    trace: list[TraceEvent]
    steps: list[str]
    scenario_state: dict[str, Any]
    evidence_count: int
    replan_count: int
    llm_call_count: int
    contains_mock: bool
    error: str | None
    report_facts: ReportFacts | None
    report_summary: str | None
