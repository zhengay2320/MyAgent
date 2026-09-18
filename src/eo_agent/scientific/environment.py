from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from eo_agent.config import PROJECT_ROOT
from eo_agent.scientific.schemas import (
    EventState,
    EvidenceValidity,
    ExecutionStatus,
    ExperimentMetrics,
    ExperimentResult,
    ExperimentSpec,
    ExperimentType,
    InvestigationSpec,
    ObservationRef,
)


class EpisodeEnvironment:
    """Deterministic measurement world; private evaluation labels are never exposed."""

    def __init__(self, episode_id: str, definition: dict[str, Any]) -> None:
        self.episode_id = episode_id
        self._public = copy.deepcopy(definition["public"])
        self._private_evaluation = copy.deepcopy(definition.get("private_evaluation", {}))
        self._technical_failures = set(definition.get("technical_failures", []))

    @classmethod
    def load(cls, episode_id: str, root: Path | None = None) -> EpisodeEnvironment:
        root = root or PROJECT_ROOT
        raw = json.loads(
            (root / "fixtures" / "scientific" / "episodes.json").read_text(encoding="utf-8")
        )["episodes"]
        if episode_id not in raw:
            raise ValueError(f"未知 scientific episode: {episode_id}")
        return cls(episode_id, raw[episode_id])

    def renamed(self, episode_id: str) -> EpisodeEnvironment:
        definition = {
            "public": self._public,
            "private_evaluation": self._private_evaluation,
            "technical_failures": list(self._technical_failures),
        }
        return EpisodeEnvironment(episode_id, definition)

    def with_measurement(self, observation_id: str, value: float) -> EpisodeEnvironment:
        definition = {
            "public": copy.deepcopy(self._public),
            "private_evaluation": self._private_evaluation,
            "technical_failures": list(self._technical_failures),
        }
        for observation in definition["public"]["observations"]:
            if observation["observation_id"] == observation_id:
                observation["measurement_value"] = value
                return EpisodeEnvironment(self.episode_id, definition)
        raise KeyError(observation_id)

    @property
    def analysis_cutoff(self) -> datetime:
        return datetime.fromisoformat(self._public["analysis_cutoff"].replace("Z", "+00:00"))

    @property
    def allowed_reference_windows(self) -> list[str]:
        return list(self._public["allowed_reference_windows"])

    @property
    def persistence_horizon_days(self) -> int:
        return int(self._public["persistence_horizon_days"])

    @property
    def spatial_support(self) -> str:
        return str(self._public["spatial_support"])

    def events(self) -> list[EventState]:
        observation_ids = [item["observation_id"] for item in self._public["observations"]]
        return [
            EventState(
                event_id=item["event_id"],
                geometry_ref=item["geometry_ref"],
                area_km2=item["area_km2"],
                candidate_source=item["candidate_source"],
                context_summary=item["context_summary"],
                quality_flags=item["quality_flags"],
                observation_ids=observation_ids,
            )
            for item in self._public["events"]
        ]

    def candidate(self, event_id: str) -> dict[str, Any]:
        for item in self._public["events"]:
            if item["event_id"] == event_id:
                return copy.deepcopy(item)
        raise KeyError(event_id)

    def observations(self, investigation: InvestigationSpec) -> list[ObservationRef]:
        observations = [
            ObservationRef.model_validate(item) for item in self._public["observations"]
        ]
        return [
            item
            for item in observations
            if item.data_permission == "allowed"
            and item.acquired_at <= investigation.analysis_cutoff
            and item.available_at <= investigation.analysis_cutoff
        ]

    def execute(
        self, spec: ExperimentSpec, commitment_id: str, investigation: InvestigationSpec
    ) -> ExperimentResult:
        if spec.experiment_type.value in self._technical_failures:
            return self._result(
                spec,
                commitment_id,
                EvidenceValidity.NON_DISCRIMINATIVE,
                ExecutionStatus.TECHNICAL_FAILURE,
                ExperimentMetrics(),
                [],
                "模拟技术失败；不构成任何类别的反证",
            )
        observations = [
            item
            for item in self.observations(investigation)
            if item.source_group_id == spec.source_group_id
            and item.spatial_support == investigation.spatial_support
        ]
        if spec.experiment_type == ExperimentType.E1_RAW_OBSERVATION:
            return self._execute_e1(spec, commitment_id, observations)
        if spec.experiment_type == ExperimentType.E2_SEASONAL_CONTROL:
            return self._execute_e2(spec, commitment_id, observations, investigation)
        if spec.experiment_type == ExperimentType.E3_PERSISTENCE:
            return self._execute_e3(spec, commitment_id, observations, investigation)
        return self._result(
            spec,
            commitment_id,
            EvidenceValidity.NOT_APPLICABLE,
            ExecutionStatus.UNAVAILABLE,
            ExperimentMetrics(),
            [],
            "P1 未实现该实验，未注册为可用动作",
        )

    def _execute_e1(
        self, spec: ExperimentSpec, commitment_id: str, observations: list[ObservationRef]
    ) -> ExperimentResult:
        baseline = [item for item in observations if item.window_id == "baseline"]
        target = [item for item in observations if item.window_id == spec.window_id]
        used = baseline + target
        coverage = min((item.coverage for item in used), default=0.0)
        if not baseline or not target or coverage < 0.6:
            return self._result(
                spec,
                commitment_id,
                EvidenceValidity.INSUFFICIENT,
                ExecutionStatus.SUCCEEDED,
                ExperimentMetrics(
                    coverage=coverage,
                    distinct_observation_count=len({x.observation_id for x in used}),
                ),
                used,
                "原始观测共同覆盖不足，不能解释为稳定或无变化",
            )
        effect = _mean(target) - _mean(baseline)
        validity = _effect_relation(effect)
        return self._result(
            spec,
            commitment_id,
            validity,
            ExecutionStatus.SUCCEEDED,
            ExperimentMetrics(
                effect_size=round(effect, 6),
                coverage=coverage,
                comparable=True,
                distinct_observation_count=len({x.observation_id for x in used}),
            ),
            used,
            "原始观测差异基于共同支持范围计算",
        )

    def _execute_e2(
        self,
        spec: ExperimentSpec,
        commitment_id: str,
        observations: list[ObservationRef],
        investigation: InvestigationSpec,
    ) -> ExperimentResult:
        if spec.control_window_id not in investigation.allowed_reference_windows:
            return self._result(
                spec,
                commitment_id,
                EvidenceValidity.NOT_APPLICABLE,
                ExecutionStatus.SUCCEEDED,
                ExperimentMetrics(),
                [],
                "对照窗口不在任务授权参考域内",
            )
        baseline = [item for item in observations if item.window_id == "baseline"]
        target = [item for item in observations if item.window_id == spec.window_id]
        control = [item for item in observations if item.window_id == spec.control_window_id]
        used = baseline + target + control
        coverage = min((item.coverage for item in used), default=0.0)
        if not baseline or not target or not control or coverage < 0.6:
            return self._result(
                spec,
                commitment_id,
                EvidenceValidity.INSUFFICIENT,
                ExecutionStatus.SUCCEEDED,
                ExperimentMetrics(
                    coverage=coverage,
                    distinct_observation_count=len({x.observation_id for x in used}),
                ),
                used,
                "同季节对照覆盖不足，不作为稳定或反变化证据",
            )
        raw_effect = _mean(target) - _mean(baseline)
        adjusted = _mean(target) - _mean(control)
        if abs(raw_effect) >= 0.3 and abs(adjusted) < 0.15:
            validity = EvidenceValidity.CONFLICTING
        elif abs(adjusted) >= 0.3:
            validity = EvidenceValidity.SUPPORTING
        else:
            validity = EvidenceValidity.NON_DISCRIMINATIVE
        return self._result(
            spec,
            commitment_id,
            validity,
            ExecutionStatus.SUCCEEDED,
            ExperimentMetrics(
                effect_size=round(raw_effect, 6),
                adjusted_effect_size=round(adjusted, 6),
                coverage=coverage,
                comparable=True,
                distinct_observation_count=len({x.observation_id for x in used}),
            ),
            used,
            "同季节对照同时保留原差异和调整后差异",
        )

    def _execute_e3(
        self,
        spec: ExperimentSpec,
        commitment_id: str,
        observations: list[ObservationRef],
        investigation: InvestigationSpec,
    ) -> ExperimentResult:
        baseline = [item for item in observations if item.window_id == "baseline"]
        target = [item for item in observations if item.window_id == spec.window_id]
        followups = [item for item in observations if item.window_id == "followup"]
        used = baseline + target + followups
        coverage = min((item.coverage for item in used), default=0.0)
        distinct = len({item.observation_id for item in used})
        span_days = (
            (max(x.acquired_at for x in followups) - min(x.acquired_at for x in target)).days
            if target and followups
            else 0
        )
        horizon = spec.requested_horizon_days or investigation.persistence_horizon_days
        if not baseline or not target or not followups or coverage < 0.6 or span_days < horizon:
            return self._result(
                spec,
                commitment_id,
                EvidenceValidity.INSUFFICIENT,
                ExecutionStatus.SUCCEEDED,
                ExperimentMetrics(
                    coverage=coverage,
                    distinct_observation_count=distinct,
                    span_days=span_days,
                    censored=span_days < horizon,
                ),
                used,
                "持续期覆盖或后续跨度不足，结果被删失且不能支持稳定",
            )
        baseline_value = _mean(baseline)
        ratios = [abs(item.measurement_value - baseline_value) >= 0.3 for item in followups]
        persistence_ratio = sum(ratios) / len(ratios)
        validity = (
            EvidenceValidity.SUPPORTING
            if persistence_ratio >= 0.75
            else EvidenceValidity.CONFLICTING
            if persistence_ratio <= 0.25
            else EvidenceValidity.NON_DISCRIMINATIVE
        )
        return self._result(
            spec,
            commitment_id,
            validity,
            ExecutionStatus.SUCCEEDED,
            ExperimentMetrics(
                effect_size=round(_mean(target) - baseline_value, 6),
                persistence_ratio=round(persistence_ratio, 6),
                coverage=coverage,
                comparable=True,
                distinct_observation_count=distinct,
                span_days=span_days,
            ),
            used,
            "持续性仅由不同原始采集 ID 和日期计算",
        )

    def _result(
        self,
        spec: ExperimentSpec,
        commitment_id: str,
        validity: EvidenceValidity,
        execution_status: ExecutionStatus,
        metrics: ExperimentMetrics,
        observations: list[ObservationRef],
        message: str,
    ) -> ExperimentResult:
        root_ids = sorted({item.observation_id for item in observations})
        signature_payload = {
            "type": spec.experiment_type.value,
            "window": spec.window_id,
            "control": spec.control_window_id,
            "profile": spec.parameter_profile,
            "horizon": spec.requested_horizon_days,
            "roots": root_ids,
            "metrics": metrics.model_dump(mode="json"),
        }
        signature = hashlib.sha256(
            json.dumps(signature_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return ExperimentResult(
            result_id=f"R-{signature[:16]}",
            event_id=spec.event_id,
            experiment_id=spec.experiment_id,
            commitment_id=commitment_id,
            experiment_type=spec.experiment_type,
            execution_status=execution_status,
            evidence_validity=validity,
            target_predicate_id="persistent_change",
            metrics=metrics,
            root_source_ids=root_ids,
            acquired_at=max((item.acquired_at for item in observations), default=None),
            available_at=max((item.available_at for item in observations), default=None),
            spatial_support=self.spatial_support,
            unit="index",
            processing_signature=signature,
            contains_mock=True,
            message=message,
        )


def _mean(observations: list[ObservationRef]) -> float:
    return sum(item.measurement_value for item in observations) / len(observations)


def _effect_relation(effect: float) -> EvidenceValidity:
    if abs(effect) >= 0.3:
        return EvidenceValidity.SUPPORTING
    if abs(effect) < 0.15:
        return EvidenceValidity.CONFLICTING
    return EvidenceValidity.NON_DISCRIMINATIVE
