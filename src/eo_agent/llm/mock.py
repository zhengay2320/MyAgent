from __future__ import annotations

import re
from datetime import date, timedelta
from time import perf_counter
from typing import Any, TypeVar

from pydantic import BaseModel

from eo_agent.llm.base import (
    LLMMetadata,
    LLMResult,
    StructuredObserver,
    build_structured_messages,
)
from eo_agent.schemas import ActionSpec, ReportFacts, TaskDraft

T = TypeVar("T", bound=BaseModel)


class MockLLMAdapter:
    """Deterministic rule adapter. It only sees visible fields, never scenario IDs."""

    profile = "mock"
    implementation_id = "mock-rule-parser-v1"

    def _meta(self, started: float) -> LLMMetadata:
        return LLMMetadata(
            provider="mock",
            profile=self.profile,
            requested_model=self.implementation_id,
            returned_model=self.implementation_id,
            prompt_version="eo-v0.1",
            output_protocol="local-structured",
            duration_ms=round((perf_counter() - started) * 1000, 3),
            usage=None,
        )

    def parse_task(self, query: str, aoi_id: str | None) -> LLMResult[TaskDraft]:
        started = perf_counter()
        months = re.findall(r"(20\d{2})\s*年\s*(1[0-2]|0?[1-9])\s*月", query)
        en_months = re.findall(r"(20\d{2})-(1[0-2]|0[1-9])", query)
        values = months or en_months
        task_type = (
            "continuous_monitoring"
            if re.search(r"连续|from.+to|时序", query, re.I)
            else "two_period_change"
        )
        aoi_text = None
        if "武汉演示区" in query:
            aoi_text = "武汉演示区"
        elif "武汉" in query:
            aoi_text = "武汉"
        elif "大型合成测试区" in query:
            aoi_text = "大型合成测试区"
        missing: list[str] = []
        if len(values) < 2:
            missing.append("baseline_month/target_month")
        if not aoi_text and not aoi_id:
            missing.append("aoi")
        draft = TaskDraft(
            aoi_text=aoi_text,
            baseline_month=f"{values[1][0]}-{int(values[1][1]):02d}" if len(values) >= 2 else None,
            target_month=f"{values[0][0]}-{int(values[0][1]):02d}" if len(values) >= 2 else None,
            task_type=task_type,
            missing_fields=missing,
        )
        return LLMResult(payload=draft, metadata=self._meta(started))

    def choose_action(
        self, visible_state: dict[str, Any], allowed_actions: list[str]
    ) -> LLMResult[ActionSpec]:
        started = perf_counter()
        if "reconstruct" in allowed_actions:
            recommended = bool(visible_state.get("reconstruction_recommended"))
            action = "reconstruct" if recommended else "skip_reconstruction"
            reason = "可见质量指标建议去云" if recommended else "可见质量指标满足检测要求"
        elif "fetch_additional_evidence" in allowed_actions:
            action = (
                "fetch_additional_evidence"
                if visible_state.get("verdict") == "needs_evidence"
                else "keep_partial"
            )
            reason = (
                "核验结果明确要求补充证据"
                if action.startswith("fetch")
                else "证据预算内无法继续改善"
            )
        else:
            raise ValueError("没有可选动作")
        if action not in allowed_actions:
            raise ValueError("Mock 决策不在允许动作内")
        return LLMResult(
            payload=ActionSpec(action=action, arguments={}, reason_summary=reason),
            metadata=self._meta(started),
        )

    def summarize(self, facts: ReportFacts) -> LLMResult[str]:
        started = perf_counter()
        text = (
            f"任务以 {facts.status.value} 结束；全部领域数据与算法输出均为模拟结果。"
            f"核验状态：{facts.verification_status}。"
        )
        return LLMResult(payload=text, metadata=self._meta(started))

    def generate_structured(
        self,
        purpose: str,
        visible_input: dict[str, Any],
        schema: type[T],
        observer: StructuredObserver | None = None,
    ) -> LLMResult[T]:
        """Offline scientific policy operating only on the supplied whitelist view."""
        from eo_agent.scientific.schemas import (
            ClaimDraft,
            EventClass,
            ExperimentProposal,
            ExperimentSpec,
            ExperimentType,
            Hypothesis,
            HypothesisProposal,
            InterferenceFactor,
            Reflection,
        )

        started = perf_counter()
        if observer:
            observer(
                "request",
                {
                    "purpose": purpose,
                    "attempt": 1,
                    "endpoint": "local://mock-structured",
                    "payload": {
                        "model": self.implementation_id,
                        "messages": build_structured_messages(purpose, visible_input, schema),
                    },
                    "schema": schema.model_json_schema(),
                },
            )
        event_id = str(visible_input.get("event_id", "event"))
        flags = set(visible_input.get("quality_flags", []))
        hypotheses = visible_input.get("hypotheses", [])
        target_id = hypotheses[0]["hypothesis_id"] if hypotheses else f"H-{event_id}-persistent"
        if purpose == "imagery_parse_request":
            from eo_agent.imagery.schemas import (
                DataKind,
                ParsedRequest,
                PeriodRole,
                QueryMode,
                SearchPeriod,
            )

            query = str(visible_input["query"])
            periods: list[SearchPeriod] = []
            date_values = re.findall(r"(20\d{2}-\d{2}-\d{2})", query)
            month_values = re.findall(r"(20\d{2})\s*年\s*(1[0-2]|0?[1-9])\s*月", query)
            if len(date_values) >= 2:
                start = date.fromisoformat(date_values[0])
                inclusive_end = date.fromisoformat(date_values[1])
                periods.append(
                    SearchPeriod(
                        period_id="single",
                        label=f"{start.isoformat()} 至 {inclusive_end.isoformat()}",
                        start=start,
                        end=inclusive_end + timedelta(days=1),
                        role=PeriodRole.SINGLE,
                        original_expression=" 至 ".join(date_values[:2]),
                    )
                )
                query_mode = QueryMode.SINGLE_RANGE
                interpretation = "显式日期范围按用户书写顺序解释，结束日转换为右开边界。"
            elif month_values:
                month_dates = [date(int(year), int(month), 1) for year, month in month_values]
                month_dates = list(dict.fromkeys(month_dates))
                if len(month_dates) >= 2:
                    baseline_match = re.search(
                        r"(?:基准期|基线期|对照期)[^\d]{0,12}(20\d{2})\s*年\s*(1[0-2]|0?[1-9])\s*月",
                        query,
                    )
                    target_match = re.search(
                        r"(?:目标期|当前期|后期)[^\d]{0,12}(20\d{2})\s*年\s*(1[0-2]|0?[1-9])\s*月",
                        query,
                    )
                    if baseline_match and target_match:
                        chosen = [
                            date(int(baseline_match.group(1)), int(baseline_match.group(2)), 1),
                            date(int(target_match.group(1)), int(target_match.group(2)), 1),
                        ]
                        interpretation = (
                            "按用户明确给出的基准期和目标期角色解释，未按出现顺序改写。"
                        )
                    else:
                        ordered = sorted(month_dates)
                        chosen = [ordered[0], ordered[-1]]
                        interpretation = "用户未明确角色，月份按时间排序为基准期和目标期。"
                    roles = [PeriodRole.BASELINE, PeriodRole.TARGET]
                    ids = ["baseline", "target"]
                    query_mode = QueryMode.TWO_PERIOD_COMPARISON
                else:
                    chosen = month_dates[:1]
                    roles = [PeriodRole.SINGLE]
                    ids = ["single"]
                    query_mode = QueryMode.SINGLE_RANGE
                    interpretation = "用户只指定一个月份，未强造第二时期。"
                periods = [
                    SearchPeriod(
                        period_id=period_id,
                        label=f"{item:%Y-%m}",
                        start=item,
                        end=(
                            date(item.year + 1, 1, 1)
                            if item.month == 12
                            else date(item.year, item.month + 1, 1)
                        ),
                        role=role,
                        original_expression=f"{item.year}年{item.month}月",
                    )
                    for item, role, period_id in zip(chosen, roles, ids, strict=True)
                ]
            else:
                query_mode = QueryMode.SINGLE_RANGE
                interpretation = "未识别到合法日期或月份，等待用户补充。"
            missing = [] if periods else ["time_range"]
            if not visible_input.get("aoi_provided"):
                missing.append("aoi_geometry")
            value = ParsedRequest(
                task_summary="准备用户授权范围内的 Sentinel-2 候选、质量与可选 SAR 数据。",
                query_mode=query_mode,
                periods=periods,
                requested_data=[DataKind(item) for item in visible_input["requested_data"]],
                original_query=query,
                original_timezone=visible_input["user_timezone"],
                role_interpretation=interpretation,
                needs_aoi=not visible_input.get("aoi_provided"),
                missing_fields=missing,
                extension_suggestions=[],
            )
        elif purpose in {
            "imagery_recommend_scenes",
            "imagery_recommend_scenes_repair",
        }:
            from eo_agent.imagery.schemas import (
                SarRecommendation,
                SarRecommendationStatus,
                SceneRecommendation,
            )

            candidates = list(visible_input["candidates"])
            selected: list[str] = []
            rejected: dict[str, str] = {}
            unknown: list[str] = []
            recommended_periods: list[str] = []
            threshold = float(visible_input["sar_recommend_clear_fraction"])
            for period_id in sorted({str(item["period_id"]) for item in candidates}):
                period_candidates = [item for item in candidates if item["period_id"] == period_id]

                def rank(item: dict[str, Any]) -> tuple[float, float]:
                    quality = item.get("quality") or {}
                    clear = quality.get("clear_aoi_fraction")
                    return (
                        -1.0 if clear is None else float(clear),
                        float(item.get("coverage_fraction") or 0),
                    )

                best = max(period_candidates, key=rank)
                selected.append(str(best["candidate_id"]))
                quality = best.get("quality") or {}
                status = quality.get("status")
                clear = quality.get("clear_aoi_fraction")
                if status in {"unknown", "failed", "insufficient_coverage"} or clear is None:
                    unknown.append(str(best["candidate_id"]))
                elif float(clear) < threshold:
                    recommended_periods.append(period_id)
                for item in period_candidates:
                    if item is not best:
                        rejected[str(item["candidate_id"])] = "同一授权窗口内质量排序低于已选候选"
            if unknown:
                sar_status = SarRecommendationStatus.QUALITY_UNKNOWN
                rationale = "部分已选光学候选质量或有效覆盖未知；不把未知直接解释为需要 SAR。"
            elif recommended_periods:
                sar_status = SarRecommendationStatus.RECOMMENDED
                rationale = "同一授权窗口内的最佳光学候选仍低于清晰覆盖启发式阈值。"
            else:
                sar_status = SarRecommendationStatus.NOT_NEEDED
                rationale = "每个时期均有达到演示阈值的替代光学候选，当前不建议补充 SAR。"
            value = SceneRecommendation(
                selected_optical_candidate_ids=selected,
                selected_sar_candidate_ids=[],
                rejected_candidates=rejected,
                missing_information=(
                    ["需要本地或在线质量复核"] if unknown else []
                ),
                sar_recommendation=SarRecommendation(
                    status=sar_status,
                    rationale=rationale,
                    considered_alternative_optical_ids=[
                        str(item["candidate_id"]) for item in candidates
                    ],
                    unresolved_quality_candidate_ids=unknown,
                    recommended_period_ids=recommended_periods,
                    rule_summary=(
                        "先比较同一授权窗口内替代光学；只有最佳清晰AOI覆盖低于阈值时建议SAR。"
                    ),
                ),
                rationale=rationale,
                accepted_by_program=False,
            )
        elif purpose == "imagery_finalize_sar":
            from eo_agent.imagery.schemas import (
                SarRecommendation,
                SarRecommendationStatus,
                SceneRecommendation,
            )

            optical_ids = [str(item) for item in visible_input["selected_optical_candidate_ids"]]
            candidates = list(visible_input["sar_candidates"])
            period_ids = set(visible_input.get("recommended_period_ids", []))
            selected_sar: list[str] = []
            rejected_sar: dict[str, str] = {}
            for period_id in sorted(period_ids):
                available = [item for item in candidates if item["period_id"] == period_id]
                if not available:
                    continue
                best = min(
                    available,
                    key=lambda item: (
                        float(item.get("delta_days") or 10_000),
                        -float(item.get("coverage_fraction") or 0),
                        str(item["candidate_id"]),
                    ),
                )
                selected_sar.append(str(best["candidate_id"]))
                for item in available:
                    if item is not best:
                        rejected_sar[str(item["candidate_id"])] = (
                            "同一授权窗口内与目标光学时间差或覆盖排序较低"
                        )
            status = (
                SarRecommendationStatus.RECOMMENDED
                if selected_sar
                else SarRecommendationStatus.UNAVAILABLE
            )
            rationale = (
                "已在用户授权窗口内选择与目标光学日期最接近且覆盖较好的 SAR 候选。"
                if selected_sar
                else "当前授权窗口未找到可用 SAR 候选；未擅自扩大时间范围。"
            )
            value = SceneRecommendation(
                selected_optical_candidate_ids=optical_ids,
                selected_sar_candidate_ids=selected_sar,
                rejected_candidates=rejected_sar,
                missing_information=[] if selected_sar else ["授权窗口内缺少可用 SAR"],
                sar_recommendation=SarRecommendation(
                    status=status,
                    rationale=rationale,
                    considered_alternative_optical_ids=optical_ids,
                    unresolved_quality_candidate_ids=[],
                    recommended_period_ids=sorted(period_ids),
                    rule_summary="只从已检索的真实候选 ID 中按日期差、覆盖与轨道元数据整理建议。",
                ),
                rationale=rationale,
                accepted_by_program=False,
            )
        elif purpose == "propose_hypotheses":
            factors: list[InterferenceFactor] = []
            if "cross_month_comparison" in flags:
                factors.append(InterferenceFactor.SEASON)
            if "reconstruction_derived" in flags:
                factors.append(InterferenceFactor.RECONSTRUCTION)
            value: BaseModel = HypothesisProposal(
                hypotheses=[
                    Hypothesis(
                        hypothesis_id=target_id,
                        event_id=event_id,
                        version=1,
                        predicate_id="persistent_change",
                        description="候选差异代表持续地表变化；可见干扰因素需通过实验排除。",
                        event_class=EventClass.PERSISTENT_CHANGE,
                        interference_factors=factors,
                        expected_signatures=["原始观测差异显著或后续观测保持差异"],
                        contradictory_signatures=["同季节对照消除差异或原始观测差异很小"],
                        applicability=["仅适用于当前登记空间支持与时间窗口"],
                        priority=0.8,
                    )
                ]
            )
        elif purpose == "propose_experiment":
            available = [ExperimentType(item) for item in visible_input["available_experiments"]]
            priorities = [
                ExperimentType(item["experiment_type"])
                for item in visible_input.get("action_values", [])
            ]
            chosen = next((item for item in priorities if item in available), available[0])
            kwargs: dict[str, Any] = {
                "experiment_id": f"X-{event_id}-{chosen.value[:2]}",
                "event_id": event_id,
                "target_hypothesis_id": target_id,
                "experiment_type": chosen,
                "window_id": "target",
                "subregion_id": visible_input["spatial_support"],
                "source_group_id": "raw_optical",
                "rationale": "检验可见证据下优先级最高的合法假设区分实验。",
            }
            if chosen == ExperimentType.E1_RAW_OBSERVATION:
                kwargs["parameter_profile"] = "raw_common_support"
            elif chosen == ExperimentType.E2_SEASONAL_CONTROL:
                kwargs.update(
                    parameter_profile="same_season",
                    control_window_id=visible_input["allowed_reference_windows"][0],
                )
            else:
                kwargs.update(
                    parameter_profile="horizon_90d",
                    requested_horizon_days=visible_input["persistence_horizon_days"],
                )
            value = ExperimentProposal(experiment=ExperimentSpec(**kwargs))
        elif purpose == "reflect_on_result":
            relation = visible_input["latest_relation"]
            remaining = bool(visible_input.get("available_experiments"))
            value = Reflection(
                disposition="weaken" if relation in {"conflicting", "insufficient"} else "retain",
                reason_summary=f"最新登记证据关系为 {relation}。",
                unresolved_question=("仍缺少可判定观测" if relation == "insufficient" else None),
                continue_investigation=relation == "insufficient" and remaining,
            )
        elif purpose == "compose_claim":
            label = visible_input["decision_label"]
            statements = {
                "persistent_change": "模拟证据支持该候选在登记时间范围内具有持续变化特征。",
                "transient_change": "模拟对照证据削弱持续变化解释，更符合瞬态差异。",
                "stable": "模拟原始观测未支持候选变化，但这不是普遍无变化声明。",
                "abstain": "现有模拟观测信息不足，无法判断地表真实类别。",
            }
            value = ClaimDraft(statement=statements[label])
        else:
            raise ValueError(f"未知结构化用途: {purpose}")
        if not isinstance(value, schema):
            value = schema.model_validate(value.model_dump(mode="json"))
        if observer:
            observer(
                "response",
                {
                    "purpose": purpose,
                    "attempt": 1,
                    "content": value.model_dump_json(),
                    "finish_reason": "stop",
                    "returned_model": self.implementation_id,
                    "usage": None,
                },
            )
            observer(
                "validation",
                {
                    "purpose": purpose,
                    "attempt": 1,
                    "valid": True,
                    "parsed": value.model_dump(mode="json"),
                },
            )
        return LLMResult(payload=value, metadata=self._meta(started))
