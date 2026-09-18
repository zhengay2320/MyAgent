from __future__ import annotations

from datetime import date
from typing import Any, TypeVar
from uuid import uuid4

from pydantic import BaseModel

from eo_agent.audit import AuditLogger
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
from eo_agent.llm.base import (
    LLMClient,
    LLMResult,
    build_structured_messages,
    redact_for_log,
)

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
        max_request_repairs: int = 1,
        max_selection_repairs: int = 1,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        if not 0 <= max_request_repairs <= 3:
            raise ValueError("任务拆解业务修复次数必须在 0 到 3 之间")
        if not 0 <= max_selection_repairs <= 3:
            raise ValueError("选图业务修复次数必须在 0 到 3 之间")
        self.llm = llm
        self.repository = repository
        self.artifacts = artifacts
        self.events = events
        self.model_profile = model_profile
        self.prompt_version = prompt_version
        self.sar_recommend_clear_fraction = sar_recommend_clear_fraction
        self.max_request_repairs = max_request_repairs
        self.max_selection_repairs = max_selection_repairs
        self.audit_logger = audit_logger

    def parse_request(
        self,
        task_id: str,
        request: ImageryTaskRequest,
        aoi_summary: dict[str, Any],
    ) -> ParsedRequest:
        authorized_requested_data = [item.value for item in request.requested_data]
        visible = {
            "query": request.query,
            "requested_data": authorized_requested_data,
            "authorized_requested_data": authorized_requested_data,
            "user_timezone": request.user_timezone,
            "aoi_provided": bool(aoi_summary.get("aoi_provided")),
            "aoi_id": aoi_summary.get("aoi_id"),
            "aoi_name": aoi_summary.get("aoi_name"),
            "constraints": {
                "do_not_expand_time": True,
                "do_not_invent_geometry": True,
                "query_interval": "left_closed_right_open",
                "requested_data_must_exactly_match_authorized_list": True,
                "quality_assessment_is_workflow_metadata_not_extra_authorization": True,
            },
            "decision_rules": [
                "requested_data 必须逐项复制 authorized_requested_data，不得新增或遗漏。",
                "在线光学质量检查由程序自动执行，不能因此擅自添加 quality。",
                "若用户只授权 optical，即使任务文字提到质量检查，也只能返回 optical。",
                "不得擅自添加 SAR；后续是否建议 SAR 由候选质量与独立决策步骤处理。",
            ],
        }
        return self._parse_request_with_business_repair(
            task_id=task_id,
            request=request,
            base_visible=visible,
        )

    def _parse_request_with_business_repair(
        self,
        *,
        task_id: str,
        request: ImageryTaskRequest,
        base_visible: dict[str, Any],
    ) -> ParsedRequest:
        previous: ParsedRequest | None = None
        previous_errors: list[str] = []
        for repair_round in range(self.max_request_repairs + 1):
            if repair_round == 0:
                purpose = "imagery_parse_request"
                purpose_label = "解析用户授权的区域、时间与所需数据"
                visible = base_visible
            else:
                purpose = "imagery_parse_request_repair"
                purpose_label = (
                    "根据程序校验错误修正任务拆解"
                    f"（修复 {repair_round}/{self.max_request_repairs}）"
                )
                visible = {
                    **base_visible,
                    "repair_round": repair_round,
                    "previous_parsed_request": (
                        previous.model_dump(mode="json") if previous is not None else None
                    ),
                    "validation_errors": previous_errors,
                    "repair_instruction": (
                        "只修正程序指出的字段；requested_data 必须与 "
                        "authorized_requested_data 完全一致。不得改变用户区域、"
                        "时区、原始请求或扩大时间范围。"
                    ),
                }
            result, call_id = self._call(
                task_id,
                purpose_label,
                purpose,
                visible,
                ParsedRequest,
            )
            value = result.payload
            errors = self._validate_parsed_request(value, request, base_visible)
            self._record_disposition(
                task_id,
                call_id,
                accepted=not errors,
                errors=errors,
                action={
                    "tool_name": "parse_imagery_request",
                    "parameters": {
                        "query": request.query,
                        "user_timezone": request.user_timezone,
                        "repair_round": repair_round,
                    },
                    "accepted": not errors,
                    "rejection_reason": "; ".join(errors) or None,
                    "result_summary": value.model_dump(mode="json") if not errors else None,
                },
            )
            if not errors:
                return value
            previous = value
            previous_errors = errors

        raise LLMProposalRejected(
            "任务拆解在 "
            f"{self.max_request_repairs + 1} 次模型提案后仍未通过硬校验；"
            f"已尝试业务修复 {self.max_request_repairs} 次；硬约束未放宽："
            f"{'；'.join(previous_errors)}"
        )

    @staticmethod
    def _validate_parsed_request(
        value: ParsedRequest,
        request: ImageryTaskRequest,
        visible: dict[str, Any],
    ) -> list[str]:
        errors: list[str] = []
        if value.original_query != request.query:
            errors.append("模型改写了用户原始请求")
        if value.original_timezone != request.user_timezone:
            errors.append("模型改写了用户时区")
        requested = set(request.requested_data)
        parsed = set(value.requested_data)
        if parsed - requested:
            added = ", ".join(sorted(item.value for item in parsed - requested))
            errors.append(f"模型增加了用户未授权的数据类型：{added}")
        if requested - parsed:
            omitted = ", ".join(sorted(item.value for item in requested - parsed))
            errors.append(f"模型遗漏了用户已授权的数据类型：{omitted}")
        if bool(value.needs_aoi) != (not visible["aoi_provided"]):
            errors.append("模型对 AOI 是否缺失的判断与程序输入不一致")
        return errors

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
        candidates_by_period: dict[str, list[dict[str, Any]]] = {
            period.period_id: [] for period in periods
        }
        for candidate in visible_candidates:
            candidates_by_period.setdefault(str(candidate["period_id"]), []).append(candidate)
        required_period_ids = [period.period_id for period in periods]
        selection_constraints = {
            "exactly_one_optical_per_period": True,
            "all_periods_must_be_covered": True,
            "only_candidate_ids_are_allowed": True,
            "do_not_expand_or_change_periods": True,
            "sar_does_not_replace_optical_selection": True,
        }
        visible = {
            "periods": [item.model_dump(mode="json") for item in periods],
            "required_period_ids": required_period_ids,
            "candidates": visible_candidates,
            "candidates_by_period": candidates_by_period,
            "selection_constraints": selection_constraints,
            "sar_recommend_clear_fraction": self.sar_recommend_clear_fraction,
            "decision_rules": [
                "必须为每一个 required_period_id 选择且只能选择一个光学候选。",
                "如果有 N 个时期，selected_optical_candidate_ids 必须包含 N 个光学候选。",
                "不得为同一个时期选择多个光学候选而遗漏另一个时期。",
                "只能从该时期对应的 candidates_by_period 中选择 candidate_id。",
                "不允许改变 period、扩大时间范围或编造候选。",
                "SAR 推荐与光学选择是不同问题；即使建议 SAR，每个时期仍必须先完成合法的光学选择。",
                "先比较同一授权窗口的替代光学观测；覆盖不足与质量未知不能解释为无云。",
            ],
        }
        return self._recommend_with_business_repair(
            task_id=task_id,
            periods=periods,
            candidates=candidates,
            base_visible=visible,
        )

    def _recommend_with_business_repair(
        self,
        *,
        task_id: str,
        periods: list[SearchPeriod],
        candidates: list[SceneCandidate],
        base_visible: dict[str, Any],
    ) -> SceneRecommendation:
        previous: SceneRecommendation | None = None
        previous_errors: list[str] = []
        for repair_round in range(self.max_selection_repairs + 1):
            if repair_round == 0:
                purpose = "imagery_recommend_scenes"
                purpose_label = "依据研究区内质量从真实候选池推荐光学影像并判断是否建议 SAR"
                visible = base_visible
            else:
                purpose = "imagery_recommend_scenes_repair"
                purpose_label = (
                    "根据程序选图校验错误修正光学候选选择"
                    f"（修复 {repair_round}/{self.max_selection_repairs}）"
                )
                visible = {
                    **base_visible,
                    "repair_round": repair_round,
                    "previous_recommendation": (
                        previous.model_dump(mode="json") if previous is not None else None
                    ),
                    "validation_errors": previous_errors,
                    "repair_instruction": (
                        "根据程序校验错误重新选择。只修正候选选择与相关说明；"
                        "不得改变或扩大用户时间范围，不得引用候选池外 ID，"
                        "不得用 SAR 代替任一时期的光学候选。"
                    ),
                }
            result, call_id = self._call(
                task_id,
                purpose_label,
                purpose,
                visible,
                SceneRecommendation,
            )
            errors = _validate_recommendation(
                result.payload,
                periods,
                candidates,
                require_optical=True,
            )
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
                        "selected_optical_candidate_ids": (
                            value.selected_optical_candidate_ids
                        ),
                        "sar_recommendation": value.sar_recommendation.status.value,
                        "repair_round": repair_round,
                    },
                    "accepted": not errors,
                    "rejection_reason": "; ".join(errors) or None,
                    "result_summary": {
                        "selected_count": len(value.selected_optical_candidate_ids),
                        "required_period_count": len(periods),
                        "recommended_period_ids": (
                            value.sar_recommendation.recommended_period_ids
                        ),
                    }
                    if not errors
                    else None,
                },
            )
            if not errors:
                return value
            previous = value
            previous_errors = errors

        raise LLMProposalRejected(
            "光学选图在 "
            f"{self.max_selection_repairs + 1} 次模型提案后仍未通过硬校验；"
            f"已尝试业务修复 {self.max_selection_repairs} 次；硬约束未放宽："
            f"{'；'.join(previous_errors)}"
        )

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
            if self.audit_logger is not None:
                self.audit_logger.record(
                    f"llm.{event}",
                    task_id=task_id,
                    stage=purpose,
                    message=purpose_label,
                    data=payload,
                )
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
                request = redact_for_log(
                    {
                        "purpose": purpose,
                        "attempt": 0,
                        "request_sent": False,
                        "message": "请求在发送前失败或调用预算已耗尽",
                        "error_type": type(exc).__name__,
                        "payload": {
                            "model_profile": self.model_profile,
                            "messages": build_structured_messages(
                                purpose, visible_input, schema
                            ),
                        },
                        "schema": schema.model_json_schema(),
                    }
                )
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
            if self.audit_logger is not None:
                self.audit_logger.record(
                    "llm.failure",
                    task_id=task_id,
                    stage=purpose,
                    message=purpose_label,
                    data={"error_type": type(exc).__name__, "message": str(exc)},
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
