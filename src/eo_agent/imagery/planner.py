from __future__ import annotations

from datetime import date
from typing import Any, TypeVar
from uuid import uuid4

from pydantic import BaseModel

from eo_agent.imagery.artifacts import ImageryArtifactStore
from eo_agent.imagery.events import EventBroker
from eo_agent.imagery.repository import ImageryRepository
from eo_agent.imagery.schemas import (
    DataKind,
    ImageryTaskRequest,
    ParsedRequest,
    QualitySummary,
    SceneCandidate,
    SceneRecommendation,
    SearchPeriod,
)
from eo_agent.llm.base import LLMClient, LLMResult

T = TypeVar("T", bound=BaseModel)


class LLMProposalRejected(ValueError):
    pass


class ImageryPlanner:
    """LLM-facing policy boundary with persisted exact I/O and program validation."""

    def __init__(
        self,
        llm: LLMClient,
        repository: ImageryRepository,
        artifacts: ImageryArtifactStore,
        events: EventBroker,
        *,
        model_profile: str,
        prompt_version: str = "imagery-v1",
        sar_recommend_clear_fraction: float = 0.55,
    ) -> None:
        self.llm = llm
        self.repository = repository
        self.artifacts = artifacts
        self.events = events
        self.model_profile = model_profile
        self.prompt_version = prompt_version
        self.sar_recommend_clear_fraction = sar_recommend_clear_fraction

    def parse_request(
        self,
        task_id: str,
        request: ImageryTaskRequest,
        aoi_summary: dict[str, Any],
    ) -> ParsedRequest:
        visible = {
            "query": request.query,
            "requested_data": [item.value for item in request.requested_data],
            "user_timezone": request.user_timezone,
            "aoi_provided": bool(aoi_summary.get("aoi_provided")),
            "aoi_id": aoi_summary.get("aoi_id"),
            "aoi_name": aoi_summary.get("aoi_name"),
            "constraints": {
                "do_not_expand_time": True,
                "do_not_invent_geometry": True,
                "query_interval": "left_closed_right_open",
            },
        }
        result, call_id = self._call(
            task_id,
            "解析用户授权的区域、时间与所需数据",
            "imagery_parse_request",
            visible,
            ParsedRequest,
        )
        value = result.payload
        errors: list[str] = []
        if value.original_query != request.query:
            errors.append("模型改写了用户原始请求")
        if value.original_timezone != request.user_timezone:
            errors.append("模型改写了用户时区")
        if any(item not in request.requested_data for item in value.requested_data):
            errors.append("模型增加了用户未授权的数据类型")
        if bool(value.needs_aoi) != (not visible["aoi_provided"]):
            errors.append("模型对 AOI 是否缺失的判断与程序输入不一致")
        self._record_disposition(
            task_id,
            call_id,
            accepted=not errors,
            errors=errors,
            action={
                "tool_name": "parse_imagery_request",
                "parameters": {"query": request.query, "user_timezone": request.user_timezone},
                "accepted": not errors,
                "rejection_reason": "; ".join(errors) or None,
                "result_summary": value.model_dump(mode="json") if not errors else None,
            },
        )
        if errors:
            raise LLMProposalRejected("；".join(errors))
        return value

    def recommend_optical(
        self,
        task_id: str,
        periods: list[SearchPeriod],
        candidates: list[SceneCandidate],
        quality_by_id: dict[str, QualitySummary],
    ) -> SceneRecommendation:
        visible_candidates = [
            {
                "candidate_id": item.candidate_id,
                "period_id": item.period_id,
                "dataset": item.dataset,
                "acquired_at": item.acquired_at.isoformat(),
                "coverage_fraction": item.coverage_fraction,
                "scene_cloud_fraction": item.scene_cloud_fraction,
                "quality": (
                    quality_by_id[item.candidate_id].model_dump(mode="json")
                    if item.candidate_id in quality_by_id
                    else None
                ),
                "is_mock": item.is_mock,
            }
            for item in candidates
        ]
        visible = {
            "periods": [item.model_dump(mode="json") for item in periods],
            "candidates": visible_candidates,
            "sar_recommend_clear_fraction": self.sar_recommend_clear_fraction,
            "decision_rules": [
                "先比较同一授权窗口的替代光学观测",
                "覆盖不足与质量未知不能解释为无云",
                "只能引用 candidates 中存在的 ID",
            ],
        }
        result, call_id = self._call(
            task_id,
            "依据研究区内质量从真实候选池推荐光学影像并判断是否建议 SAR",
            "imagery_recommend_scenes",
            visible,
            SceneRecommendation,
        )
        errors = _validate_recommendation(result.payload, periods, candidates, require_optical=True)
        value = result.payload.model_copy(
            update={"accepted_by_program": not errors, "validation_errors": errors}
        )
        self._record_disposition(
            task_id,
            call_id,
            accepted=not errors,
            errors=errors,
            action={
                "tool_name": "select_catalog_candidates",
                "parameters": {
                    "selected_optical_candidate_ids": value.selected_optical_candidate_ids,
                    "sar_recommendation": value.sar_recommendation.status.value,
                },
                "accepted": not errors,
                "rejection_reason": "; ".join(errors) or None,
                "result_summary": {
                    "selected_count": len(value.selected_optical_candidate_ids),
                    "recommended_period_ids": value.sar_recommendation.recommended_period_ids,
                }
                if not errors
                else None,
            },
        )
        if errors:
            raise LLMProposalRejected("；".join(errors))
        return value

    def finalize_sar(
        self,
        task_id: str,
        periods: list[SearchPeriod],
        prior_recommendation: SceneRecommendation,
        sar_candidates: list[SceneCandidate],
    ) -> SceneRecommendation:
        visible = {
            "periods": [item.model_dump(mode="json") for item in periods],
            "selected_optical_candidate_ids": prior_recommendation.selected_optical_candidate_ids,
            "recommended_period_ids": (
                prior_recommendation.sar_recommendation.recommended_period_ids
            ),
            "sar_candidates": [
                {
                    "candidate_id": item.candidate_id,
                    "period_id": item.period_id,
                    "dataset": item.dataset,
                    "acquired_at": item.acquired_at.isoformat(),
                    "coverage_fraction": item.coverage_fraction,
                    "delta_days": item.delta_days,
                    "orbit_direction": item.orbit_direction,
                    "relative_orbit": item.relative_orbit,
                    "polarizations": item.polarizations,
                    "matched_optical_candidate_id": item.matched_optical_candidate_id,
                    "is_mock": item.is_mock,
                }
                for item in sar_candidates
            ],
            "constraints": {
                "only_whitelisted_ids": True,
                "do_not_expand_periods": True,
                "required_mode": "IW",
                "required_polarizations": ["VV", "VH"],
            },
        }
        result, call_id = self._call(
            task_id,
            "从已检索 Sentinel-1 候选中整理最终补充下载建议",
            "imagery_finalize_sar",
            visible,
            SceneRecommendation,
        )
        all_candidates = sar_candidates + [
            _optical_stub(identifier, periods)
            for identifier in prior_recommendation.selected_optical_candidate_ids
        ]
        errors = _validate_recommendation(
            result.payload, periods, all_candidates, require_optical=False
        )
        if result.payload.selected_optical_candidate_ids != (
            prior_recommendation.selected_optical_candidate_ids
        ):
            errors.append("SAR 整理步骤不得更换已校验的光学候选")
        value = result.payload.model_copy(
            update={"accepted_by_program": not errors, "validation_errors": errors}
        )
        self._record_disposition(
            task_id,
            call_id,
            accepted=not errors,
            errors=errors,
            action={
                "tool_name": "select_sar_candidates",
                "parameters": {
                    "selected_sar_candidate_ids": value.selected_sar_candidate_ids,
                },
                "accepted": not errors,
                "rejection_reason": "; ".join(errors) or None,
                "result_summary": {"selected_count": len(value.selected_sar_candidate_ids)}
                if not errors
                else None,
            },
        )
        if errors:
            raise LLMProposalRejected("；".join(errors))
        return value

    def _call(
        self,
        task_id: str,
        purpose_label: str,
        purpose: str,
        visible_input: dict[str, Any],
        schema: type[T],
    ) -> tuple[LLMResult[T], str]:
        base_id = f"LLM-{uuid4().hex[:16]}"
        attempt_ids: dict[int, str] = {}
        responses: dict[int, dict[str, Any]] = {}
        last_call_id = base_id

        def observe(event: str, payload: dict[str, Any]) -> None:
            nonlocal last_call_id
            attempt = int(payload.get("attempt", 1))
            call_id = attempt_ids.get(attempt)
            if event == "request":
                call_id = f"{base_id}-A{attempt}"
                attempt_ids[attempt] = call_id
                last_call_id = call_id
                artifact_id = self.artifacts.write_json(
                    f"llm_calls/{base_id}/attempt-{attempt}-request.json",
                    payload,
                    contains_mock=self.model_profile == "mock",
                    artifact_id=f"{call_id}-request",
                )
                self.repository.start_llm_call(call_id, task_id, purpose_label, payload)
                self.events.emit(
                    task_id,
                    event_type="llm.request",
                    stage=self.repository.get_task(task_id)["status"],
                    summary=f"模型请求已发送：{purpose_label}（尝试 {attempt}）",
                    call_id=call_id,
                    details={
                        "purpose": purpose_label,
                        "attempt": attempt,
                        "request_artifact_id": artifact_id,
                        "streaming": False,
                    },
                )
                return
            call_id = call_id or f"{base_id}-A{attempt}"
            last_call_id = call_id
            if event == "response":
                responses[attempt] = payload
                artifact_id = self.artifacts.write_json(
                    f"llm_calls/{base_id}/attempt-{attempt}-response.json",
                    payload,
                    contains_mock=self.model_profile == "mock",
                    artifact_id=f"{call_id}-response",
                )
                self.events.emit(
                    task_id,
                    event_type="llm.response",
                    stage=self.repository.get_task(task_id)["status"],
                    summary=f"收到模型实际返回：{purpose_label}（尝试 {attempt}）",
                    call_id=call_id,
                    details={"attempt": attempt, "response_artifact_id": artifact_id},
                )
            elif event == "validation":
                valid = bool(payload.get("valid"))
                self.repository.finish_llm_call(
                    call_id,
                    status="succeeded" if valid else "validation_failed",
                    response=responses.get(attempt),
                    validation=payload,
                    accepted=None,
                    action=None,
                )
            elif event == "error":
                self.repository.finish_llm_call(
                    call_id,
                    status="failed",
                    response=payload,
                    validation={"valid": False},
                    accepted=False,
                    action=None,
                )
                self.events.emit(
                    task_id,
                    event_type="llm.error",
                    stage=self.repository.get_task(task_id)["status"],
                    summary=f"模型调用失败：{purpose_label}（尝试 {attempt}）",
                    call_id=call_id,
                    details=payload,
                )

        try:
            result = self.llm.generate_structured(
                purpose, visible_input, schema, observer=observe
            )
        except Exception as exc:
            if not attempt_ids:
                request = {
                    "purpose": purpose,
                    "attempt": 0,
                    "message": "请求在发送前失败或调用预算已耗尽",
                    "error_type": type(exc).__name__,
                }
                self.repository.start_llm_call(base_id, task_id, purpose_label, request)
                self.repository.finish_llm_call(
                    base_id,
                    status="failed",
                    response=None,
                    validation={"valid": False},
                    accepted=False,
                    action={"error": str(exc)},
                )
                last_call_id = base_id
            self.events.emit(
                task_id,
                event_type="llm.error",
                stage=self.repository.get_task(task_id)["status"],
                summary=f"模型步骤失败且未回退其他模型：{purpose_label}",
                call_id=last_call_id,
                details={"error_type": type(exc).__name__, "message": str(exc)},
            )
            raise
        return result, last_call_id

    def _record_disposition(
        self,
        task_id: str,
        call_id: str,
        *,
        accepted: bool,
        errors: list[str],
        action: dict[str, Any],
    ) -> None:
        existing = next(
            (
                item
                for item in self.repository.list_llm_calls(task_id)
                if item["call_id"] == call_id
            ),
            None,
        )
        response = existing.get("response") if existing else None
        validation = existing.get("validation") if existing else {"valid": True}
        validation = {**(validation or {}), "parameter_valid": accepted, "errors": errors}
        self.repository.finish_llm_call(
            call_id,
            status="succeeded" if accepted else "validation_failed",
            response=response,
            validation=validation,
            accepted=accepted,
            action=action,
        )
        self.events.emit(
            task_id,
            event_type="action.proposed",
            stage=self.repository.get_task(task_id)["status"],
            summary=f"模型提出动作：{action['tool_name']}",
            call_id=call_id,
            details={"parameters": action["parameters"]},
        )
        self.events.emit(
            task_id,
            event_type="action.accepted" if accepted else "action.rejected",
            stage=self.repository.get_task(task_id)["status"],
            summary=(
                f"程序接受模型动作：{action['tool_name']}"
                if accepted
                else f"程序拒绝模型动作：{'; '.join(errors)}"
            ),
            call_id=call_id,
            details={"accepted": accepted, "errors": errors, "action": action},
        )


def _validate_recommendation(
    recommendation: SceneRecommendation,
    periods: list[SearchPeriod],
    candidates: list[SceneCandidate],
    *,
    require_optical: bool,
) -> list[str]:
    errors: list[str] = []
    by_id = {item.candidate_id: item for item in candidates}
    selected = (
        recommendation.selected_optical_candidate_ids
        + recommendation.selected_sar_candidate_ids
    )
    unknown = sorted(set(selected) - set(by_id))
    if unknown:
        errors.append(f"模型引用了候选池不存在的 ID: {', '.join(unknown)}")
    period_by_id = {item.period_id: item for item in periods}
    optical_by_period: dict[str, int] = {item.period_id: 0 for item in periods}
    for identifier in recommendation.selected_optical_candidate_ids:
        item = by_id.get(identifier)
        if item is None:
            continue
        if item.data_kind != DataKind.OPTICAL:
            errors.append(f"{identifier} 不是光学候选")
            continue
        optical_by_period[item.period_id] = optical_by_period.get(item.period_id, 0) + 1
        period = period_by_id.get(item.period_id)
        acquired = item.acquired_at.date()
        if period is None or not period.start <= acquired < period.end:
            errors.append(f"{identifier} 超出其授权时期")
    for identifier in recommendation.selected_sar_candidate_ids:
        item = by_id.get(identifier)
        if item is None:
            continue
        if item.data_kind != DataKind.SAR:
            errors.append(f"{identifier} 不是 SAR 候选")
        if not {"VV", "VH"}.issubset(item.polarizations):
            errors.append(f"{identifier} 不包含要求的 VV/VH 极化")
        period = period_by_id.get(item.period_id)
        acquired = item.acquired_at.date()
        if period is None or not period.start <= acquired < period.end:
            errors.append(f"{identifier} 超出其授权时期")
    if require_optical:
        for period_id, count in optical_by_period.items():
            if count != 1:
                errors.append(f"时期 {period_id} 必须且只能选择一个光学候选")
    valid_period_ids = set(period_by_id)
    invalid_periods = (
        set(recommendation.sar_recommendation.recommended_period_ids) - valid_period_ids
    )
    if invalid_periods:
        errors.append(f"SAR 建议引用了不存在的时期: {', '.join(sorted(invalid_periods))}")
    return list(dict.fromkeys(errors))


def _optical_stub(identifier: str, periods: list[SearchPeriod]) -> SceneCandidate:
    period = periods[0]
    return SceneCandidate(
        candidate_id=identifier,
        provider="mock",
        dataset="validated-optical-selection",
        product_id=identifier,
        system_index=identifier,
        acquired_at=_period_midpoint(period),
        period_id=period.period_id,
        data_kind=DataKind.OPTICAL,
        is_mock=True,
    )


def _period_midpoint(period: SearchPeriod):
    from datetime import UTC, datetime

    midpoint: date = period.start + (period.end - period.start) / 2
    return datetime(midpoint.year, midpoint.month, midpoint.day, tzinfo=UTC)
