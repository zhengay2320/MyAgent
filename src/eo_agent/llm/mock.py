from __future__ import annotations

import re
from time import perf_counter
from typing import Any, TypeVar

from pydantic import BaseModel

from eo_agent.llm.base import LLMMetadata, LLMResult
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
        self, purpose: str, visible_input: dict[str, Any], schema: type[T]
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
        event_id = str(visible_input.get("event_id", "event"))
        flags = set(visible_input.get("quality_flags", []))
        hypotheses = visible_input.get("hypotheses", [])
        target_id = hypotheses[0]["hypothesis_id"] if hypotheses else f"H-{event_id}-persistent"
        if purpose == "propose_hypotheses":
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
        return LLMResult(payload=value, metadata=self._meta(started))
