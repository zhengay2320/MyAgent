from __future__ import annotations

from eo_agent.schemas import (
    ChangeData,
    ReportFacts,
    VerificationData,
)
from eo_agent.workflow.state import WorkflowState

MOCK_NOTICE = "本报告为系统流程演示，包含模拟数据或占位算法输出，不代表研究区域的真实地表变化。"


def build_report_facts(state: WorkflowState) -> ReportFacts:
    task = state["task"]
    change: ChangeData | None = None
    verification: VerificationData | None = None
    artifacts = []
    implementations = []
    for result in state["tool_results"]:
        implementations.append(f"{result.tool_name}:{result.implementation_id}")
        artifacts.extend(result.artifacts)
        if isinstance(result.data, ChangeData):
            change = result.data
        if isinstance(result.data, VerificationData):
            verification = result.data
    if change is None or not change.can_determine:
        conclusion = "无法判断：没有足够数据形成变化结论。"
    elif verification is None or not verification.sufficient:
        conclusion = "无法形成可靠结论：模拟核验证据不足。"
    else:
        conclusion = "模拟流程检测到变化事件；该结果仅用于验证系统执行链路。"
    quality_status = "无可用数据" if change is None else "已完成模拟质量检查"
    verification_status = verification.verdict if verification else "unavailable"
    return ReportFacts(
        task_id=state["task_id"],
        status=state["status"],
        query=state["request"].query,
        aoi_name=task.aoi_name if task else None,
        aoi_id=task.aoi_id if task else state["request"].aoi_id,
        baseline=task.baseline if task else None,
        target=task.target if task else None,
        model_profile=state["request"].model_profile,
        contains_mock=True,
        notice=MOCK_NOTICE,
        steps=list(state["steps"]),
        tool_implementations=implementations,
        change_area_km2=change.change_area_km2 if change else None,
        changed_fraction=change.changed_fraction if change else None,
        change_events=change.events if change else [],
        quality_status=quality_status,
        verification_status=verification_status,
        conclusion=conclusion,
        limitations=[
            "所有观测、去云、变化检测和核验结果均为确定性模拟数据。",
            "程序执行成功与科学有效性是两个独立维度；本结果未进行科学评估。",
            "无数据或证据不足时不得解释为没有变化。",
        ],
        tool_call_count=len(state["tool_results"]),
        llm_call_count=state["llm_call_count"],
        artifacts=artifacts,
    )
