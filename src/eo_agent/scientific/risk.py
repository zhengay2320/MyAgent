from __future__ import annotations

from eo_agent.scientific.provenance import EvidenceGraph
from eo_agent.scientific.schemas import (
    ActionValueEstimate,
    EvidenceValidity,
    ExperimentType,
    RiskEstimate,
)


class HeuristicRiskAdapter:
    """Transparent debugging score; it is neither probability nor calibrated risk."""

    def evaluate(self, event_id: str, graph: EvidenceGraph) -> RiskEstimate:
        supporting = sum(
            item.relation == EvidenceValidity.SUPPORTING for item in graph.evidence.values()
        )
        conflicting = sum(
            item.relation == EvidenceValidity.CONFLICTING for item in graph.evidence.values()
        )
        denominator = max(1, supporting + conflicting)
        return RiskEstimate(
            event_id=event_id,
            heuristic_support_score=round(supporting / denominator, 6),
            heuristic_conflict_score=round(conflicting / denominator, 6),
            unique_measurement_count=graph.unique_measurement_count(),
            independent_root_count=graph.independent_root_count(),
            class_probabilities=None,
            calibration_status="unavailable",
            explanation="未训练的可见证据计数规则；分数不是概率，也未校准。",
        )


class HeuristicPriorityAdapter:
    """Ranks allowed experiments with named observable rules only."""

    def rank(
        self,
        available: list[ExperimentType],
        quality_flags: list[str],
        executed: list[ExperimentType],
    ) -> list[ActionValueEstimate]:
        values: list[ActionValueEstimate] = []
        for experiment_type in available:
            if experiment_type in executed:
                continue
            priority = 0.4
            reason = "尚未执行的注册实验"
            if (
                experiment_type == ExperimentType.E2_SEASONAL_CONTROL
                and "cross_month_comparison" in quality_flags
            ):
                priority, reason = 1.0, "可见质量标记显示跨月份可比性风险"
            elif (
                experiment_type == ExperimentType.E1_RAW_OBSERVATION
                and "reconstruction_derived" in quality_flags
            ):
                priority, reason = 1.0, "可见质量标记显示结果来自恢复路径"
            elif (
                experiment_type == ExperimentType.E3_PERSISTENCE
                and "multi_temporal_available" in quality_flags
            ):
                priority, reason = 1.0, "可见资产允许持续性检查"
            values.append(
                ActionValueEstimate(
                    experiment_type=experiment_type,
                    proxy_priority=priority,
                    expected_risk_reduction=None,
                    is_learned=False,
                    reason=reason,
                )
            )
        return sorted(values, key=lambda item: (-item.proxy_priority, item.experiment_type.value))
