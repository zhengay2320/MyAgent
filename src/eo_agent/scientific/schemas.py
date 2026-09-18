from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eo_agent.schemas import ArtifactRef, TaskSpec


class ScienceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EventClass(StrEnum):
    STABLE = "stable"
    PERSISTENT_CHANGE = "persistent_change"
    TRANSIENT_CHANGE = "transient_change"


class DecisionLabel(StrEnum):
    STABLE = "stable"
    PERSISTENT_CHANGE = "persistent_change"
    TRANSIENT_CHANGE = "transient_change"
    ABSTAIN = "abstain"


class InterferenceFactor(StrEnum):
    SEASON = "season"
    RECONSTRUCTION = "reconstruction"
    REGISTRATION = "registration"
    SAR = "sar"
    MASK = "mask"
    UNKNOWN = "unknown"


class ExperimentType(StrEnum):
    E1_RAW_OBSERVATION = "E1_raw_observation_test"
    E2_SEASONAL_CONTROL = "E2_seasonal_control_test"
    E3_PERSISTENCE = "E3_persistence_test"
    E4_SAR = "E4_sar_comparability"
    E5_ALTERNATIVE_RECONSTRUCTION = "E5_alternative_reconstruction"
    E6_REGISTRATION = "E6_registration_sensitivity"
    E7_MASK = "E7_mask_sensitivity"


class ExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    TECHNICAL_FAILURE = "technical_failure"
    UNAVAILABLE = "unavailable"


class EvidenceValidity(StrEnum):
    SUPPORTING = "supporting"
    CONFLICTING = "conflicting"
    INSUFFICIENT = "insufficient"
    NOT_APPLICABLE = "not_applicable"
    NON_DISCRIMINATIVE = "non_discriminative"


class StopReason(StrEnum):
    EVIDENCE_SUFFICIENT = "evidence_sufficient_in_demo"
    INSUFFICIENT_OBSERVATION = "insufficient_observation"
    BUDGET_EXHAUSTED = "budget_exhausted"
    NO_APPLICABLE_EXPERIMENT = "no_applicable_experiment"
    NO_PROGRESS = "no_progress"
    TECHNICAL_FAILURE = "technical_failure"


class ObservationRef(ScienceModel):
    observation_id: str
    sensor: str
    acquired_at: datetime
    available_at: datetime
    product_version: str
    spatial_support: str
    window_id: str
    source_group_id: str
    measurement_value: float
    quality: float = Field(ge=0, le=1)
    coverage: float = Field(ge=0, le=1)
    unit: str
    data_permission: Literal["allowed", "denied"] = "allowed"
    contains_mock: bool = True

    @model_validator(mode="after")
    def normalize_time(self) -> ObservationRef:
        if self.acquired_at.tzinfo is None or self.available_at.tzinfo is None:
            raise ValueError("观测时间必须包含时区")
        self.acquired_at = self.acquired_at.astimezone(UTC)
        self.available_at = self.available_at.astimezone(UTC)
        return self


class InvestigationSpec(ScienceModel):
    task: TaskSpec
    analysis_mode: Literal["scientific"] = "scientific"
    episode_id: str
    analysis_cutoff: datetime
    allowed_reference_windows: list[str]
    persistence_horizon_days: int = Field(gt=0)
    spatial_support: str
    max_experiments: int = Field(gt=0)
    max_replans: int = Field(ge=0)
    max_data_reads: int = Field(gt=0)
    max_llm_calls: int = Field(gt=0)

    @model_validator(mode="after")
    def normalize_cutoff(self) -> InvestigationSpec:
        if self.analysis_cutoff.tzinfo is None:
            raise ValueError("analysis_cutoff 必须包含时区")
        self.analysis_cutoff = self.analysis_cutoff.astimezone(UTC)
        if not self.allowed_reference_windows:
            raise ValueError("至少需要一个授权参考窗口")
        return self


class Hypothesis(ScienceModel):
    hypothesis_id: str
    event_id: str
    version: int = Field(ge=1)
    predicate_id: str
    description: str
    event_class: EventClass | None = None
    interference_factors: list[InterferenceFactor] = Field(default_factory=list)
    expected_signatures: list[str]
    contradictory_signatures: list[str]
    applicability: list[str]
    priority: float = Field(ge=0, le=1)
    contains_mock: bool = True

    @model_validator(mode="after")
    def has_scientific_target(self) -> Hypothesis:
        if self.event_class is None and not self.interference_factors:
            raise ValueError("假设必须指向事件类别或至少一个干扰因素")
        return self


class HypothesisRevision(ScienceModel):
    revision_id: str
    event_id: str
    hypothesis_id: str
    from_version: int = Field(ge=1)
    to_version: int = Field(ge=1)
    disposition: Literal["retain", "weaken", "revise"]
    reason_summary: str
    unresolved_question: str | None = None
    created_at: datetime
    contains_mock: bool = True

    @model_validator(mode="after")
    def append_only_version(self) -> HypothesisRevision:
        if self.to_version <= self.from_version:
            raise ValueError("修订版本必须追加，不能覆盖原版本")
        return self


class ExperimentSpec(ScienceModel):
    experiment_id: str
    event_id: str
    target_hypothesis_id: str
    experiment_type: ExperimentType
    window_id: str
    control_window_id: str | None = None
    subregion_id: str
    source_group_id: str
    parameter_profile: Literal["raw_common_support", "same_season", "horizon_90d"]
    requested_horizon_days: int | None = Field(default=None, gt=0)
    rationale: str

    @model_validator(mode="after")
    def validate_registered_parameters(self) -> ExperimentSpec:
        if self.experiment_type == ExperimentType.E2_SEASONAL_CONTROL:
            if not self.control_window_id or self.parameter_profile != "same_season":
                raise ValueError("E2 必须使用已登记的同季节对照窗口")
        elif self.experiment_type == ExperimentType.E3_PERSISTENCE:
            if self.requested_horizon_days is None or self.parameter_profile != "horizon_90d":
                raise ValueError("E3 必须声明已登记的持续期参数")
        elif self.experiment_type == ExperimentType.E1_RAW_OBSERVATION:
            if self.parameter_profile != "raw_common_support":
                raise ValueError("E1 必须使用共同支持范围参数")
        return self


class Precommitment(ScienceModel):
    commitment_id: str
    event_id: str
    hypothesis_version: int = Field(ge=1)
    experiment: ExperimentSpec
    support_condition: str
    conflict_condition: str
    insufficient_data_handling: Literal["abstain_or_continue"] = "abstain_or_continue"
    created_at: datetime
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    contains_mock: bool = True


class ExperimentMetrics(ScienceModel):
    effect_size: float | None = None
    adjusted_effect_size: float | None = None
    persistence_ratio: float | None = Field(default=None, ge=0, le=1)
    coverage: float | None = Field(default=None, ge=0, le=1)
    comparable: bool | None = None
    distinct_observation_count: int = Field(default=0, ge=0)
    span_days: int | None = Field(default=None, ge=0)
    censored: bool = False


class ExperimentResult(ScienceModel):
    result_id: str
    event_id: str
    experiment_id: str
    commitment_id: str
    experiment_type: ExperimentType
    execution_status: ExecutionStatus
    evidence_validity: EvidenceValidity
    target_predicate_id: str
    metrics: ExperimentMetrics
    root_source_ids: list[str]
    acquired_at: datetime | None = None
    available_at: datetime | None = None
    spatial_support: str
    unit: str
    processing_signature: str
    cache_hit: bool = False
    contains_mock: bool = True
    message: str


class EvidenceItem(ScienceModel):
    evidence_id: str
    task_id: str
    event_id: str
    result_id: str
    target_predicate_id: str
    relation: EvidenceValidity
    measurement_name: str
    measurement_value: float | None
    uncertainty: float | None = Field(default=None, ge=0)
    unit: str
    coverage: float | None = Field(default=None, ge=0, le=1)
    root_source_ids: list[str]
    parent_evidence_ids: list[str] = Field(default_factory=list)
    dependent_with: list[str] = Field(default_factory=list)
    spatial_support: str
    acquired_at: datetime | None = None
    available_at: datetime | None = None
    processing_signature: str
    evidence_signature: str
    contains_mock: bool = True


class SourceNode(ScienceModel):
    source_id: str
    task_id: str
    event_id: str
    node_type: Literal["root_observation", "derived_measurement"]
    parent_source_ids: list[str] = Field(default_factory=list)
    root_source_ids: list[str]
    signature: str
    contains_mock: bool


class RiskEstimate(ScienceModel):
    event_id: str
    method: Literal["heuristic_visible_evidence_v1"] = "heuristic_visible_evidence_v1"
    heuristic_support_score: float = Field(ge=0, le=1)
    heuristic_conflict_score: float = Field(ge=0, le=1)
    unique_measurement_count: int = Field(ge=0)
    independent_root_count: int = Field(ge=0)
    class_probabilities: None = None
    calibration_status: Literal["unavailable"] = "unavailable"
    explanation: str
    contains_mock: bool = True


class ActionValueEstimate(ScienceModel):
    experiment_type: ExperimentType
    method: Literal["heuristic_proxy_priority_v1"] = "heuristic_proxy_priority_v1"
    proxy_priority: float = Field(ge=0, le=1)
    expected_risk_reduction: None = None
    is_learned: Literal[False] = False
    reason: str
    contains_mock: bool = True


class BudgetLedger(ScienceModel):
    max_llm_calls: int = Field(ge=0)
    max_experiments: int = Field(ge=0)
    max_data_reads: int = Field(ge=0)
    max_replans: int = Field(ge=0)
    used_llm_calls: int = Field(default=0, ge=0)
    used_experiments: int = Field(default=0, ge=0)
    used_data_reads: int = Field(default=0, ge=0)
    used_replans: int = Field(default=0, ge=0)

    def reserve(self, resource: Literal["llm", "experiment", "data_read", "replan"]) -> None:
        names = {
            "llm": ("used_llm_calls", "max_llm_calls"),
            "experiment": ("used_experiments", "max_experiments"),
            "data_read": ("used_data_reads", "max_data_reads"),
            "replan": ("used_replans", "max_replans"),
        }
        used_name, max_name = names[resource]
        used = getattr(self, used_name)
        maximum = getattr(self, max_name)
        if used >= maximum:
            raise RuntimeError(f"{resource} 预算已耗尽；动作未执行")
        setattr(self, used_name, used + 1)


class EvidenceSummary(ScienceModel):
    evidence_id: str
    target_predicate_id: str
    relation: EvidenceValidity
    measurement_name: str
    measurement_value: float | None
    unit: str
    coverage: float | None
    root_source_count: int
    dependent: bool


class AgentObservationView(ScienceModel):
    event_id: str
    context_summary: str
    spatial_support: str
    quality_flags: list[str]
    available_asset_ids: list[str]
    evidence: list[EvidenceSummary]
    hypotheses: list[Hypothesis]
    available_experiments: list[ExperimentType]
    allowed_reference_windows: list[str]
    persistence_horizon_days: int = Field(gt=0)
    executed_experiments: list[ExperimentType]
    action_values: list[ActionValueEstimate]
    remaining_experiment_budget: int = Field(ge=0)
    analysis_cutoff: datetime


class Decision(ScienceModel):
    event_id: str
    label: DecisionLabel
    technical_status: Literal["completed", "partial", "failed"]
    scientific_validity: Literal["not_evaluated", "insufficient"]
    stop_reason: StopReason
    supporting_evidence_ids: list[str]
    conflicting_evidence_ids: list[str]
    unresolved_questions: list[str]
    contains_mock: bool


class ScientificClaim(ScienceModel):
    event_id: str
    decision: DecisionLabel
    statement: str
    observation_facts: list[str]
    model_inferences: list[str]
    supporting_evidence_ids: list[str]
    conflicting_evidence_ids: list[str]
    missing_evidence: list[str]
    stop_reason: StopReason
    contains_mock: bool


class EventState(ScienceModel):
    event_id: str
    parent_event_id: str | None = None
    geometry_ref: str
    area_km2: float = Field(gt=0)
    candidate_source: str
    context_summary: str
    quality_flags: list[str]
    interference_factors: list[InterferenceFactor] = Field(default_factory=list)
    observation_ids: list[str]
    evidence_ids: list[str] = Field(default_factory=list)
    hypothesis_ids: list[str] = Field(default_factory=list)
    commitment_ids: list[str] = Field(default_factory=list)
    result_ids: list[str] = Field(default_factory=list)
    investigation_status: Literal["pending", "running", "completed", "partial"] = "pending"
    stop_reason: StopReason | None = None
    contains_mock: bool = True


class HypothesisProposal(ScienceModel):
    hypotheses: list[Hypothesis]


class ExperimentProposal(ScienceModel):
    experiment: ExperimentSpec


class Reflection(ScienceModel):
    disposition: Literal["retain", "weaken", "revise"]
    reason_summary: str
    unresolved_question: str | None = None
    continue_investigation: bool


class ClaimDraft(ScienceModel):
    statement: str


class ScientificEventReport(ScienceModel):
    event: EventState
    hypotheses: list[Hypothesis]
    revisions: list[HypothesisRevision]
    precommitments: list[Precommitment]
    experiments: list[ExperimentResult]
    evidence: list[EvidenceItem]
    sources: list[SourceNode]
    risk_history: list[RiskEstimate]
    action_values: list[ActionValueEstimate]
    decision: Decision
    claim: ScientificClaim


class ScientificReportFacts(ScienceModel):
    task_id: str
    workflow_mode: Literal["scientific"] = "scientific"
    episode_id: str
    contains_mock: bool
    notice: str
    investigation: InvestigationSpec
    events: list[ScientificEventReport]
    budget: BudgetLedger
    limitations: list[str]
    artifacts: list[ArtifactRef] = Field(default_factory=list)
