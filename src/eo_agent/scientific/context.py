from __future__ import annotations

from eo_agent.scientific.environment import EpisodeEnvironment
from eo_agent.scientific.experiments import ExperimentRegistry
from eo_agent.scientific.provenance import EvidenceGraph
from eo_agent.scientific.risk import HeuristicPriorityAdapter
from eo_agent.scientific.schemas import (
    AgentObservationView,
    BudgetLedger,
    EventState,
    EvidenceSummary,
    ExperimentResult,
    Hypothesis,
    InvestigationSpec,
)


def build_agent_view(
    *,
    event: EventState,
    investigation: InvestigationSpec,
    environment: EpisodeEnvironment,
    graph: EvidenceGraph,
    hypotheses: list[Hypothesis],
    results: list[ExperimentResult],
    budget: BudgetLedger,
    registry: ExperimentRegistry,
) -> AgentObservationView:
    """Build the only state object allowed to cross the scientific LLM boundary."""
    executed = [item.experiment_type for item in results]
    available = [item for item in registry.available() if item not in executed]
    action_values = HeuristicPriorityAdapter().rank(available, event.quality_flags, executed)
    observations = environment.observations(investigation)
    return AgentObservationView(
        event_id=event.event_id,
        context_summary=event.context_summary,
        spatial_support=investigation.spatial_support,
        quality_flags=event.quality_flags,
        available_asset_ids=[item.observation_id for item in observations],
        evidence=[
            EvidenceSummary(
                evidence_id=item.evidence_id,
                target_predicate_id=item.target_predicate_id,
                relation=item.relation,
                measurement_name=item.measurement_name,
                measurement_value=item.measurement_value,
                unit=item.unit,
                coverage=item.coverage,
                root_source_count=len(set(item.root_source_ids)),
                dependent=bool(item.dependent_with),
            )
            for item in graph.evidence.values()
        ],
        hypotheses=hypotheses,
        available_experiments=available,
        allowed_reference_windows=investigation.allowed_reference_windows,
        persistence_horizon_days=investigation.persistence_horizon_days,
        executed_experiments=executed,
        action_values=action_values,
        remaining_experiment_budget=max(0, budget.max_experiments - budget.used_experiments),
        analysis_cutoff=investigation.analysis_cutoff,
    )
