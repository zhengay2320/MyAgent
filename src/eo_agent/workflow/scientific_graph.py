from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from eo_agent.audit import AuditLogger
from eo_agent.llm.base import LLMClient, LLMResult, build_structured_messages
from eo_agent.scientific.context import build_agent_view
from eo_agent.scientific.risk import HeuristicRiskAdapter
from eo_agent.scientific.schemas import (
    ClaimDraft,
    Decision,
    DecisionLabel,
    EvidenceValidity,
    ExecutionStatus,
    ExperimentProposal,
    ExperimentType,
    HypothesisProposal,
    HypothesisRevision,
    Precommitment,
    Reflection,
    ScientificClaim,
    StopReason,
)
from eo_agent.workflow.scientific_state import ScientificWorkflowState

SnapshotCallback = Callable[[str, ScientificWorkflowState], None]


class ScientificWorkflowNodes:
    def __init__(
        self,
        llm: LLMClient,
        snapshot: SnapshotCallback,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        self.llm = llm
        self.snapshot = snapshot
        self.audit_logger = audit_logger

    def _view(self, state: ScientificWorkflowState) -> dict[str, Any]:
        view = build_agent_view(
            event=state["event"],
            investigation=state["investigation"],
            environment=state["environment"],
            graph=state["evidence_graph"],
            hypotheses=state["hypotheses"],
            results=state["results"],
            budget=state["budget"],
            registry=state["registry"],
        )
        return view.model_dump(mode="json")

    def _llm(
        self,
        state: ScientificWorkflowState,
        purpose: str,
        visible: dict[str, Any],
        schema: type[Any],
    ) -> LLMResult[Any]:
        state["budget"].reserve("llm")
        task_id = state["investigation"].task.task_id
        if self.audit_logger is not None:
            self.audit_logger.record(
                "llm.input",
                task_id=task_id,
                stage=purpose,
                message="科学调查模型调用",
                data={
                    "purpose": purpose,
                    "visible_input": visible,
                    "schema": schema.model_json_schema(),
                    "messages": build_structured_messages(purpose, visible, schema),
                },
            )
        try:
            result = self.llm.generate_structured(purpose, visible, schema)
        except Exception as exc:
            if self.audit_logger is not None:
                self.audit_logger.record(
                    "llm.failure",
                    task_id=task_id,
                    stage=purpose,
                    message="科学调查模型调用失败",
                    data={"error_type": type(exc).__name__, "error": str(exc)},
                )
            raise
        if self.audit_logger is not None:
            self.audit_logger.record(
                "llm.output",
                task_id=task_id,
                stage=purpose,
                message="科学调查模型返回并完成结构校验",
                data={"payload": result.payload, "metadata": result.metadata},
            )
        state["trace"].append(
            {
                "sequence": len(state["trace"]) + 1,
                "event_id": state["event"].event_id,
                "stage": purpose,
                "event_type": "llm_call",
                "provider": result.metadata.provider,
                "profile": result.metadata.profile,
                "implementation_id": result.metadata.returned_model
                or result.metadata.requested_model,
                "attempts": result.metadata.attempts,
                "visible_fields": sorted(visible),
            }
        )
        return result

    def propose_hypotheses(self, state: ScientificWorkflowState) -> dict[str, Any]:
        result = self._llm(state, "propose_hypotheses", self._view(state), HypothesisProposal)
        hypotheses = result.payload.hypotheses
        if not hypotheses or any(item.event_id != state["event"].event_id for item in hypotheses):
            raise ValueError("LLM 假设必须属于当前事件且不能为空")
        state["event"].hypothesis_ids.extend(item.hypothesis_id for item in hypotheses)
        return {"hypotheses": hypotheses, "event": state["event"], "budget": state["budget"]}

    def propose_experiment(self, state: ScientificWorkflowState) -> dict[str, Any]:
        visible = self._view(state)
        state["action_values"].extend(
            build_agent_view(
                event=state["event"],
                investigation=state["investigation"],
                environment=state["environment"],
                graph=state["evidence_graph"],
                hypotheses=state["hypotheses"],
                results=state["results"],
                budget=state["budget"],
                registry=state["registry"],
            ).action_values
        )
        result = self._llm(state, "propose_experiment", visible, ExperimentProposal)
        experiment = result.payload.experiment
        if experiment.event_id != state["event"].event_id:
            raise ValueError("LLM 实验提案不属于当前事件")
        state["registry"].validate(experiment, state["investigation"])
        available = set(visible["available_experiments"])
        if experiment.experiment_type.value not in available:
            raise ValueError("LLM 提议了已执行或不可用实验")
        return {
            "proposed_experiment": experiment,
            "action_values": state["action_values"],
            "budget": state["budget"],
        }

    def precommit(self, state: ScientificWorkflowState) -> dict[str, Any]:
        experiment = state["proposed_experiment"]
        assert experiment is not None
        version = 1 + len(state["revisions"])
        conditions = {
            ExperimentType.E1_RAW_OBSERVATION: (
                "共同覆盖>=0.6 且 abs(effect_size)>=0.3",
                "共同覆盖>=0.6 且 abs(effect_size)<0.15",
            ),
            ExperimentType.E2_SEASONAL_CONTROL: (
                "共同覆盖>=0.6 且 abs(adjusted_effect_size)>=0.3",
                "共同覆盖>=0.6、abs(effect_size)>=0.3 且 abs(adjusted_effect_size)<0.15",
            ),
            ExperimentType.E3_PERSISTENCE: (
                "共同覆盖>=0.6、跨度达到登记期限且 persistence_ratio>=0.75",
                "共同覆盖>=0.6、跨度达到登记期限且 persistence_ratio<=0.25",
            ),
        }
        support_condition, conflict_condition = conditions[experiment.experiment_type]
        payload = {
            "event_id": state["event"].event_id,
            "hypothesis_version": version,
            "experiment": experiment.model_dump(mode="json"),
            "support_condition": support_condition,
            "conflict_condition": conflict_condition,
            "insufficient_data_handling": "abstain_or_continue",
        }
        content_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        commitment = Precommitment(
            commitment_id=f"C-{content_hash[:16]}",
            created_at=datetime.now(UTC),
            content_hash=content_hash,
            **payload,
        )
        state["precommitments"].append(commitment)
        state["event"].commitment_ids.append(commitment.commitment_id)
        self.snapshot("precommitment_saved", state)
        state["trace"].append(
            {
                "sequence": len(state["trace"]) + 1,
                "event_id": state["event"].event_id,
                "stage": "precommit",
                "event_type": "precommitment_saved",
                "commitment_id": commitment.commitment_id,
            }
        )
        return {
            "precommitments": state["precommitments"],
            "event": state["event"],
        }

    def execute(self, state: ScientificWorkflowState) -> dict[str, Any]:
        experiment = state["proposed_experiment"]
        assert experiment is not None
        commitment = state["precommitments"][-1]
        result = state["executor"].execute(
            experiment,
            commitment.commitment_id,
            state["investigation"],
            state["environment"],
            state["budget"],
        )
        state["results"].append(result)
        state["event"].result_ids.append(result.result_id)
        state["trace"].append(
            {
                "sequence": len(state["trace"]) + 1,
                "event_id": state["event"].event_id,
                "stage": "execute",
                "event_type": "experiment_call",
                "experiment_type": result.experiment_type.value,
                "parameter_profile": experiment.parameter_profile,
                "result_id": result.result_id,
                "execution_status": result.execution_status.value,
            }
        )
        return {
            "results": state["results"],
            "event": state["event"],
            "budget": state["budget"],
        }

    def ingest(self, state: ScientificWorkflowState) -> dict[str, Any]:
        evidence, duplicate = state["evidence_graph"].ingest_result(state["results"][-1])
        if not duplicate:
            state["event"].evidence_ids.append(evidence.evidence_id)
        state["risk_history"].append(
            HeuristicRiskAdapter().evaluate(state["event"].event_id, state["evidence_graph"])
        )
        self.snapshot("experiment_ingested", state)
        return {
            "risk_history": state["risk_history"],
            "event": state["event"],
        }

    def reflect(self, state: ScientificWorkflowState) -> dict[str, Any]:
        visible = self._view(state)
        visible["latest_relation"] = state["results"][-1].evidence_validity.value
        result = self._llm(state, "reflect_on_result", visible, Reflection)
        reflection = result.payload
        from_version = 1 + len(state["revisions"])
        revision = HypothesisRevision(
            revision_id=f"HR-{uuid4().hex[:12]}",
            event_id=state["event"].event_id,
            hypothesis_id=state["hypotheses"][0].hypothesis_id,
            from_version=from_version,
            to_version=from_version + 1,
            disposition=reflection.disposition,
            reason_summary=reflection.reason_summary,
            unresolved_question=reflection.unresolved_question,
            created_at=datetime.now(UTC),
        )
        state["revisions"].append(revision)
        can_continue = (
            reflection.continue_investigation
            and state["budget"].used_experiments < state["budget"].max_experiments
            and bool(visible["available_experiments"])
            and state["budget"].used_replans < state["budget"].max_replans
        )
        if can_continue:
            state["budget"].reserve("replan")
        return {
            "revisions": state["revisions"],
            "continue_investigation": can_continue,
            "budget": state["budget"],
        }

    def decide(self, state: ScientificWorkflowState) -> dict[str, Any]:
        results = state["results"]
        valid = [
            item
            for item in results
            if item.execution_status == ExecutionStatus.SUCCEEDED
            and item.evidence_validity
            in {EvidenceValidity.SUPPORTING, EvidenceValidity.CONFLICTING}
        ]
        if not valid:
            label = DecisionLabel.ABSTAIN
            stop = (
                StopReason.INSUFFICIENT_OBSERVATION
                if any(item.evidence_validity == EvidenceValidity.INSUFFICIENT for item in results)
                else StopReason.TECHNICAL_FAILURE
            )
        else:
            latest = valid[-1]
            if latest.evidence_validity == EvidenceValidity.SUPPORTING:
                label = DecisionLabel.PERSISTENT_CHANGE
            elif latest.experiment_type == ExperimentType.E2_SEASONAL_CONTROL:
                label = DecisionLabel.TRANSIENT_CHANGE
            elif latest.experiment_type == ExperimentType.E1_RAW_OBSERVATION:
                label = DecisionLabel.STABLE
            else:
                label = DecisionLabel.TRANSIENT_CHANGE
            stop = StopReason.EVIDENCE_SUFFICIENT
        evidence = list(state["evidence_graph"].evidence.values())
        decision = Decision(
            event_id=state["event"].event_id,
            label=label,
            technical_status="partial" if label == DecisionLabel.ABSTAIN else "completed",
            scientific_validity="insufficient"
            if label == DecisionLabel.ABSTAIN
            else "not_evaluated",
            stop_reason=stop,
            supporting_evidence_ids=[
                item.evidence_id
                for item in evidence
                if item.relation == EvidenceValidity.SUPPORTING
            ],
            conflicting_evidence_ids=[
                item.evidence_id
                for item in evidence
                if item.relation == EvidenceValidity.CONFLICTING
            ],
            unresolved_questions=(
                ["缺少达到覆盖与时间跨度要求的独立观测"] if label == DecisionLabel.ABSTAIN else []
            ),
            contains_mock=True,
        )
        state["event"].stop_reason = stop
        state["event"].investigation_status = (
            "partial" if label == DecisionLabel.ABSTAIN else "completed"
        )
        return {"decision": decision, "event": state["event"]}

    def compose_claim(self, state: ScientificWorkflowState) -> dict[str, Any]:
        decision = state["decision"]
        assert decision is not None
        visible = {
            "event_id": decision.event_id,
            "decision_label": decision.label.value,
            "stop_reason": decision.stop_reason.value,
            "supporting_evidence_ids": decision.supporting_evidence_ids,
            "conflicting_evidence_ids": decision.conflicting_evidence_ids,
            "unresolved_questions": decision.unresolved_questions,
        }
        draft = self._llm(state, "compose_claim", visible, ClaimDraft).payload
        claim = ScientificClaim(
            event_id=decision.event_id,
            decision=decision.label,
            statement=draft.statement,
            observation_facts=[
                f"{item.measurement_name}={item.measurement_value} {item.unit}; "
                f"relation={item.relation.value}"
                for item in state["evidence_graph"].evidence.values()
            ],
            model_inferences=[item.reason_summary for item in state["revisions"]],
            supporting_evidence_ids=decision.supporting_evidence_ids,
            conflicting_evidence_ids=decision.conflicting_evidence_ids,
            missing_evidence=decision.unresolved_questions,
            stop_reason=decision.stop_reason,
            contains_mock=True,
        )
        self.snapshot("decision_saved", state)
        return {"claim": claim, "budget": state["budget"]}


def build_scientific_graph(
    llm: LLMClient,
    snapshot: SnapshotCallback,
    audit_logger: AuditLogger | None = None,
):
    nodes = ScientificWorkflowNodes(llm, snapshot, audit_logger)
    graph = StateGraph(ScientificWorkflowState)
    graph.add_node("propose_hypotheses", nodes.propose_hypotheses)
    graph.add_node("propose_experiment", nodes.propose_experiment)
    graph.add_node("precommit", nodes.precommit)
    graph.add_node("execute", nodes.execute)
    graph.add_node("ingest", nodes.ingest)
    graph.add_node("reflect", nodes.reflect)
    graph.add_node("decide", nodes.decide)
    graph.add_node("compose_claim", nodes.compose_claim)
    graph.add_edge(START, "propose_hypotheses")
    graph.add_edge("propose_hypotheses", "propose_experiment")
    graph.add_edge("propose_experiment", "precommit")
    graph.add_edge("precommit", "execute")
    graph.add_edge("execute", "ingest")
    graph.add_edge("ingest", "reflect")
    graph.add_conditional_edges(
        "reflect",
        lambda state: "continue" if state["continue_investigation"] else "decide",
        {"continue": "propose_experiment", "decide": "decide"},
    )
    graph.add_edge("decide", "compose_claim")
    graph.add_edge("compose_claim", END)
    return graph.compile()
