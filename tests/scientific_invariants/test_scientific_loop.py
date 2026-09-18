from __future__ import annotations

import json
from time import perf_counter

import pytest

from eo_agent.llm.base import LLMResult
from eo_agent.llm.mock import MockLLMAdapter
from eo_agent.schemas import TaskRequest, TaskStatus, WorkflowMode
from eo_agent.scientific.environment import EpisodeEnvironment
from eo_agent.scientific.provenance import EvidenceGraph
from eo_agent.scientific.schemas import (
    BudgetLedger,
    EvidenceValidity,
    ExecutionStatus,
    ExperimentMetrics,
    ExperimentProposal,
    ExperimentResult,
    ExperimentSpec,
    ExperimentType,
    InvestigationSpec,
    ObservationRef,
    SourceNode,
)
from eo_agent.service import TaskService

QUERY = "对武汉演示区2025年10月与2024年9月进行变化检测"


def request(episode_id: str) -> TaskRequest:
    return TaskRequest(
        query=QUERY,
        aoi_id="wuhan_demo_100km2",
        workflow_mode=WorkflowMode.SCIENTIFIC,
        episode_id=episode_id,
    )


def run_report(tmp_path, episode_id: str, **service_kwargs):
    result = TaskService(tmp_path, **service_kwargs).run(request(episode_id))
    assert result.status == TaskStatus.COMPLETED, result.error
    return result, json.loads(
        result.report_json and open(result.report_json, encoding="utf-8").read()
    )


@pytest.mark.parametrize(
    ("episode_id", "expected"),
    [
        ("EP001", "transient_change"),
        ("EP002", "stable"),
        ("EP003", "persistent_change"),
        ("EP004", "abstain"),
    ],
)
def test_four_offline_episodes_complete(tmp_path, episode_id, expected):
    _, report = run_report(tmp_path / episode_id, episode_id)
    event = report["events"][0]
    assert event["decision"]["label"] == expected
    assert report["contains_mock"] is True
    assert all(item["contains_mock"] for item in event["experiments"])
    assert event["event"]["contains_mock"] is True
    for collection in (
        "hypotheses",
        "revisions",
        "precommitments",
        "evidence",
        "sources",
        "risk_history",
        "action_values",
    ):
        assert all(item["contains_mock"] for item in event[collection])
    assert event["decision"]["contains_mock"] is True
    assert event["claim"]["contains_mock"] is True
    assert event["risk_history"][-1]["class_probabilities"] is None
    assert all(item["expected_risk_reduction"] is None for item in event["action_values"])


def test_conflict_weakens_initial_hypothesis_and_keeps_dependency(tmp_path):
    _, report = run_report(tmp_path, "EP001")
    event = report["events"][0]
    assert event["evidence"][0]["relation"] == "supporting"
    assert event["evidence"][1]["relation"] == "conflicting"
    assert event["revisions"][0]["disposition"] == "weaken"
    assert event["evidence"][1]["dependent_with"] == [event["evidence"][0]["evidence_id"]]
    assert event["hypotheses"][0]["event_class"] == "persistent_change"
    assert "season" in event["hypotheses"][0]["interference_factors"]


def test_insufficient_information_abstains_instead_of_stable(tmp_path):
    _, report = run_report(tmp_path, "EP004")
    event = report["events"][0]
    assert event["decision"]["label"] == "abstain"
    assert event["decision"]["scientific_validity"] == "insufficient"
    assert event["decision"]["stop_reason"] == "insufficient_observation"
    assert all(item["evidence_validity"] == "insufficient" for item in event["experiments"])
    assert report["budget"]["used_experiments"] == 3
    assert report["budget"]["used_replans"] == 2
    assert report["budget"]["used_llm_calls"] == report["budget"]["max_llm_calls"]


class ForcedExperimentLLM(MockLLMAdapter):
    def __init__(self, experiment_type: ExperimentType) -> None:
        self.experiment_type = experiment_type

    def generate_structured(self, purpose, visible_input, schema):
        if purpose != "propose_experiment":
            return super().generate_structured(purpose, visible_input, schema)
        started = perf_counter()
        kwargs = {
            "experiment_id": f"forced-{self.experiment_type.value[:2]}",
            "event_id": visible_input["event_id"],
            "target_hypothesis_id": visible_input["hypotheses"][0]["hypothesis_id"],
            "experiment_type": self.experiment_type,
            "window_id": "target",
            "subregion_id": visible_input["spatial_support"],
            "source_group_id": "raw_optical",
            "rationale": "测试 LLM 合法提案实际控制实验。",
        }
        if self.experiment_type == ExperimentType.E1_RAW_OBSERVATION:
            kwargs["parameter_profile"] = "raw_common_support"
        else:
            kwargs.update(
                parameter_profile="same_season",
                control_window_id="same_season",
            )
        proposal = ExperimentProposal(experiment=ExperimentSpec(**kwargs))
        return LLMResult(payload=proposal, metadata=self._meta(started))


def test_different_legal_llm_proposals_execute_different_experiments(tmp_path):
    _, e1 = run_report(
        tmp_path / "e1",
        "EP001",
        scientific_llm=ForcedExperimentLLM(ExperimentType.E1_RAW_OBSERVATION),
    )
    _, e2 = run_report(
        tmp_path / "e2",
        "EP001",
        scientific_llm=ForcedExperimentLLM(ExperimentType.E2_SEASONAL_CONTROL),
    )
    first = e1["events"][0]["precommitments"][0]["experiment"]
    second = e2["events"][0]["precommitments"][0]["experiment"]
    assert first["experiment_type"] == "E1_raw_observation_test"
    assert first["parameter_profile"] == "raw_common_support"
    assert second["experiment_type"] == "E2_seasonal_control_test"
    assert second["parameter_profile"] == "same_season"
    assert (
        e1["events"][0]["experiments"][0]["experiment_type"]
        != e2["events"][0]["experiments"][0]["experiment_type"]
    )


def test_episode_rename_does_not_change_measurement_or_decision(tmp_path):
    original_env = EpisodeEnvironment.load("EP003")
    _, original = run_report(tmp_path / "original", "EP003", scientific_environment=original_env)
    renamed_env = original_env.renamed("renamed_episode")
    _, renamed = run_report(
        tmp_path / "renamed", "renamed_episode", scientific_environment=renamed_env
    )
    left = original["events"][0]
    right = renamed["events"][0]
    assert left["decision"]["label"] == right["decision"]["label"]
    assert left["experiments"][0]["metrics"] == right["experiments"][0]["metrics"]
    assert (
        left["experiments"][0]["processing_signature"]
        == right["experiments"][0]["processing_signature"]
    )


def test_measurement_change_not_private_label_changes_decision(tmp_path):
    environment = EpisodeEnvironment.load("EP002")
    _, original = run_report(tmp_path / "original", "EP002", scientific_environment=environment)
    changed = environment.with_measurement("B_TARGET", 0.90)
    _, modified = run_report(tmp_path / "modified", "EP002", scientific_environment=changed)
    assert original["events"][0]["decision"]["label"] == "stable"
    assert modified["events"][0]["decision"]["label"] == "persistent_change"


def test_exact_duplicate_evidence_does_not_increase_counts():
    graph = EvidenceGraph("task", "event")
    for observation_id in ("O1", "O2"):
        graph.add_root(
            ObservationRef(
                observation_id=observation_id,
                sensor="mock",
                acquired_at="2025-01-01T00:00:00Z",
                available_at="2025-01-02T00:00:00Z",
                product_version="v1",
                spatial_support="cell",
                window_id="target",
                source_group_id="raw_optical",
                measurement_value=0.5,
                quality=0.9,
                coverage=0.9,
                unit="index",
            )
        )
    result = ExperimentResult(
        result_id="R1",
        event_id="event",
        experiment_id="X1",
        commitment_id="C1",
        experiment_type=ExperimentType.E1_RAW_OBSERVATION,
        execution_status=ExecutionStatus.SUCCEEDED,
        evidence_validity=EvidenceValidity.SUPPORTING,
        target_predicate_id="persistent_change",
        metrics=ExperimentMetrics(effect_size=0.5, coverage=0.9),
        root_source_ids=["O1", "O2"],
        spatial_support="cell",
        unit="index",
        processing_signature="a" * 64,
        message="mock",
    )
    first, duplicate_first = graph.ingest_result(result)
    second, duplicate_second = graph.ingest_result(result)
    assert duplicate_first is False
    assert duplicate_second is True
    assert first.evidence_id == second.evidence_id
    assert graph.unique_measurement_count() == 1
    assert graph.independent_root_count() == 2


def test_precommitment_trace_precedes_experiment_and_artifact_exists(tmp_path):
    result, _ = run_report(tmp_path, "EP001")
    task_dir = result.report_json and __import__("pathlib").Path(result.report_json).parent
    trace = [
        json.loads(line)
        for line in (task_dir / "execution_trace.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    kinds = [item["event_type"] for item in trace]
    assert kinds.index("precommitment_saved") < kinds.index("experiment_call")
    assert (task_dir / "events" / "EV001" / "precommitments.jsonl").is_file()


def test_legacy_remains_default():
    value = TaskRequest(query=QUERY, aoi_id="wuhan_demo_100km2")
    assert value.workflow_mode == WorkflowMode.LEGACY
    assert value.episode_id is None


class RecordingLLM(MockLLMAdapter):
    def __init__(self) -> None:
        self.inputs = []

    def generate_structured(self, purpose, visible_input, schema):
        self.inputs.append((purpose, visible_input))
        return super().generate_structured(purpose, visible_input, schema)


def test_llm_receives_whitelist_without_episode_truth_or_future_results(tmp_path):
    llm = RecordingLLM()
    run_report(tmp_path, "EP001", scientific_llm=llm)
    forbidden = {"private_evaluation", "event_class_truth", "correct_action", "scenario"}

    def keys(value):
        if isinstance(value, dict):
            return set(value).union(*(keys(item) for item in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(item) for item in value)) if value else set()
        return set()

    for purpose, visible in llm.inputs:
        assert not (keys(visible) & forbidden), purpose


def test_cutoff_requires_both_acquired_and_available(tmp_path):
    _, report = run_report(tmp_path, "EP001")
    investigation = InvestigationSpec.model_validate(report["investigation"])
    definition = {
        "public": {
            "analysis_cutoff": "2026-03-01T00:00:00Z",
            "allowed_reference_windows": ["same_season"],
            "persistence_horizon_days": 90,
            "spatial_support": "cell",
            "events": [],
            "observations": [
                {
                    "observation_id": "FUTURE_RELEASE",
                    "sensor": "mock",
                    "acquired_at": "2025-01-01T00:00:00Z",
                    "available_at": "2026-04-01T00:00:00Z",
                    "product_version": "v1",
                    "spatial_support": "cell",
                    "window_id": "target",
                    "source_group_id": "raw_optical",
                    "measurement_value": 0.8,
                    "quality": 0.9,
                    "coverage": 0.9,
                    "unit": "index",
                }
            ],
        }
    }
    environment = EpisodeEnvironment("cutoff", definition)
    assert environment.observations(investigation) == []


def test_scientific_budget_is_reserved_before_action():
    budget = BudgetLedger(max_llm_calls=0, max_experiments=0, max_data_reads=0, max_replans=0)
    for resource in ("llm", "experiment", "data_read", "replan"):
        with pytest.raises(RuntimeError, match="预算已耗尽"):
            budget.reserve(resource)
    assert budget.used_llm_calls == 0
    assert budget.used_experiments == 0
    assert budget.used_data_reads == 0
    assert budget.used_replans == 0


def test_source_dag_rejects_missing_parent_and_cross_event():
    graph = EvidenceGraph("task", "event")
    with pytest.raises(ValueError, match="缺失父源"):
        graph.add_source(
            SourceNode(
                source_id="derived",
                task_id="task",
                event_id="event",
                node_type="derived_measurement",
                parent_source_ids=["missing"],
                root_source_ids=["missing"],
                signature="s",
                contains_mock=True,
            )
        )
    with pytest.raises(ValueError, match="不属于当前任务或事件"):
        graph.add_source(
            SourceNode(
                source_id="foreign",
                task_id="task",
                event_id="other",
                node_type="root_observation",
                root_source_ids=["foreign"],
                signature="s2",
                contains_mock=True,
            )
        )
