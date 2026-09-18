from __future__ import annotations

from typing import Any, TypedDict

from eo_agent.scientific.environment import EpisodeEnvironment
from eo_agent.scientific.experiments import ExperimentExecutor, ExperimentRegistry
from eo_agent.scientific.provenance import EvidenceGraph
from eo_agent.scientific.schemas import (
    ActionValueEstimate,
    BudgetLedger,
    Decision,
    EventState,
    ExperimentResult,
    ExperimentSpec,
    Hypothesis,
    HypothesisRevision,
    InvestigationSpec,
    Precommitment,
    RiskEstimate,
    ScientificClaim,
)


class ScientificWorkflowState(TypedDict):
    event: EventState
    investigation: InvestigationSpec
    environment: EpisodeEnvironment
    registry: ExperimentRegistry
    executor: ExperimentExecutor
    evidence_graph: EvidenceGraph
    budget: BudgetLedger
    hypotheses: list[Hypothesis]
    revisions: list[HypothesisRevision]
    precommitments: list[Precommitment]
    proposed_experiment: ExperimentSpec | None
    results: list[ExperimentResult]
    risk_history: list[RiskEstimate]
    action_values: list[ActionValueEstimate]
    decision: Decision | None
    claim: ScientificClaim | None
    continue_investigation: bool
    trace: list[dict[str, Any]]
