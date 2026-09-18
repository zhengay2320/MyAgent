from __future__ import annotations

import hashlib
import json

from eo_agent.scientific.schemas import (
    EvidenceItem,
    EvidenceValidity,
    ExperimentResult,
    ObservationRef,
    SourceNode,
)


class EvidenceGraph:
    """Task/event-owned source DAG plus separately indexed semantic evidence."""

    def __init__(self, task_id: str, event_id: str) -> None:
        self.task_id = task_id
        self.event_id = event_id
        self.sources: dict[str, SourceNode] = {}
        self.evidence: dict[str, EvidenceItem] = {}
        self._signature_index: dict[str, str] = {}

    def add_root(self, observation: ObservationRef) -> SourceNode:
        node = SourceNode(
            source_id=observation.observation_id,
            task_id=self.task_id,
            event_id=self.event_id,
            node_type="root_observation",
            root_source_ids=[observation.observation_id],
            signature=_hash(
                {
                    "observation_id": observation.observation_id,
                    "acquired_at": observation.acquired_at.isoformat(),
                    "available_at": observation.available_at.isoformat(),
                    "support": observation.spatial_support,
                    "version": observation.product_version,
                }
            ),
            contains_mock=observation.contains_mock,
        )
        return self.add_source(node)

    def add_source(self, node: SourceNode) -> SourceNode:
        if node.task_id != self.task_id or node.event_id != self.event_id:
            raise ValueError("来源不属于当前任务或事件")
        if node.source_id in node.parent_source_ids:
            raise ValueError("来源 DAG 不允许自环")
        missing = [parent for parent in node.parent_source_ids if parent not in self.sources]
        if missing:
            raise ValueError(f"来源 DAG 缺失父源: {', '.join(missing)}")
        if any(self._has_ancestor(parent, node.source_id) for parent in node.parent_source_ids):
            raise ValueError("来源 DAG 不允许环")
        existing = self.sources.get(node.source_id)
        if existing is not None:
            if existing == node:
                return existing
            raise ValueError("来源 ID 已存在且定义不同")
        self.sources[node.source_id] = node
        return node

    def ingest_result(self, result: ExperimentResult) -> tuple[EvidenceItem, bool]:
        if result.event_id != self.event_id:
            raise ValueError("实验结果不属于当前事件")
        for root_id in result.root_source_ids:
            if root_id not in self.sources:
                raise ValueError(f"证据引用未知根来源: {root_id}")
        derived_id = f"SRC-{result.processing_signature[:16]}"
        self.add_source(
            SourceNode(
                source_id=derived_id,
                task_id=self.task_id,
                event_id=self.event_id,
                node_type="derived_measurement",
                parent_source_ids=list(result.root_source_ids),
                root_source_ids=list(result.root_source_ids),
                signature=result.processing_signature,
                contains_mock=result.contains_mock,
            )
        )
        measurement_name, measurement_value = _primary_measurement(result)
        signature = _hash(
            {
                "roots": sorted(result.root_source_ids),
                "support": result.spatial_support,
                "processing": result.processing_signature,
                "measurement": measurement_name,
                "value": measurement_value,
                "relation": result.evidence_validity.value,
            }
        )
        if signature in self._signature_index:
            return self.evidence[self._signature_index[signature]], True
        dependencies = sorted(
            evidence.evidence_id
            for evidence in self.evidence.values()
            if set(evidence.root_source_ids) & set(result.root_source_ids)
        )
        evidence = EvidenceItem(
            evidence_id=f"E-{signature[:16]}",
            task_id=self.task_id,
            event_id=self.event_id,
            result_id=result.result_id,
            target_predicate_id=result.target_predicate_id,
            relation=result.evidence_validity,
            measurement_name=measurement_name,
            measurement_value=measurement_value,
            unit=result.unit,
            coverage=result.metrics.coverage,
            root_source_ids=list(result.root_source_ids),
            parent_evidence_ids=[],
            dependent_with=dependencies,
            spatial_support=result.spatial_support,
            acquired_at=result.acquired_at,
            available_at=result.available_at,
            processing_signature=result.processing_signature,
            evidence_signature=signature,
            contains_mock=result.contains_mock,
        )
        self.evidence[evidence.evidence_id] = evidence
        self._signature_index[signature] = evidence.evidence_id
        return evidence, False

    def add_initial_evidence(
        self,
        *,
        result_id: str,
        root_source_ids: list[str],
        measurement_value: float | None,
        coverage: float,
        spatial_support: str,
        contains_mock: bool,
    ) -> EvidenceItem:
        relation = (
            EvidenceValidity.INSUFFICIENT
            if measurement_value is None or coverage < 0.6
            else EvidenceValidity.SUPPORTING
            if abs(measurement_value) >= 0.3
            else EvidenceValidity.NON_DISCRIMINATIVE
        )
        signature = _hash(
            {
                "roots": sorted(root_source_ids),
                "support": spatial_support,
                "processing": "initial_candidate_v1",
                "measurement": "candidate_effect",
                "value": measurement_value,
                "relation": relation.value,
            }
        )
        evidence = EvidenceItem(
            evidence_id=f"E-{signature[:16]}",
            task_id=self.task_id,
            event_id=self.event_id,
            result_id=result_id,
            target_predicate_id="persistent_change",
            relation=relation,
            measurement_name="candidate_effect",
            measurement_value=measurement_value,
            unit="index",
            coverage=coverage,
            root_source_ids=root_source_ids,
            spatial_support=spatial_support,
            processing_signature="initial_candidate_v1",
            evidence_signature=signature,
            contains_mock=contains_mock,
        )
        self.evidence[evidence.evidence_id] = evidence
        self._signature_index[signature] = evidence.evidence_id
        return evidence

    def unique_measurement_count(self) -> int:
        return len(self.evidence)

    def independent_root_count(self) -> int:
        return len({root for item in self.evidence.values() for root in item.root_source_ids})

    def _has_ancestor(self, source_id: str, candidate_ancestor: str) -> bool:
        if source_id == candidate_ancestor:
            return True
        source = self.sources.get(source_id)
        if source is None:
            return False
        return any(
            self._has_ancestor(parent, candidate_ancestor) for parent in source.parent_source_ids
        )


def _primary_measurement(result: ExperimentResult) -> tuple[str, float | None]:
    if result.experiment_type.value.startswith("E2"):
        return "adjusted_effect_size", result.metrics.adjusted_effect_size
    if result.experiment_type.value.startswith("E3"):
        return "persistence_ratio", result.metrics.persistence_ratio
    return "effect_size", result.metrics.effect_size


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
    ).hexdigest()
