from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from eo_agent.audit import AuditLogger
from eo_agent.config import Settings
from eo_agent.llm.base import LLMClient
from eo_agent.reports.scientific_renderer import ScientificReportRenderer
from eo_agent.schemas import ArtifactRef, TaskRequest, TaskSpec, TaskStatus, TimeWindow
from eo_agent.scientific.environment import EpisodeEnvironment
from eo_agent.scientific.experiments import ExperimentExecutor, ExperimentRegistry
from eo_agent.scientific.provenance import EvidenceGraph
from eo_agent.scientific.risk import HeuristicRiskAdapter
from eo_agent.scientific.schemas import (
    BudgetLedger,
    InvestigationSpec,
    ScientificEventReport,
    ScientificReportFacts,
)
from eo_agent.storage.artifacts import ArtifactStore
from eo_agent.workflow.scientific_graph import build_scientific_graph
from eo_agent.workflow.scientific_state import ScientificWorkflowState


@dataclass
class ScientificRunOutput:
    status: TaskStatus
    steps: list[str]
    report_html: str
    report_json: str
    artifacts: list[ArtifactRef]
    error: str | None = None


class ScientificEngine:
    def __init__(
        self,
        settings: Settings,
        llm: LLMClient,
        *,
        environment: EpisodeEnvironment | None = None,
        registry: ExperimentRegistry | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.environment_override = environment
        self.registry = registry or ExperimentRegistry()
        self.audit_logger = audit_logger

    def run(
        self, task_id: str, request: TaskRequest, artifacts: ArtifactStore
    ) -> ScientificRunOutput:
        assert request.episode_id is not None
        environment = self.environment_override or EpisodeEnvironment.load(request.episode_id)
        task = self._task_contract(task_id, request)
        if self.audit_logger is not None:
            self.audit_logger.record(
                "task.decomposed",
                task_id=task_id,
                stage="scientific",
                message="科学调查任务契约与用户条件已解析",
                data={
                    "request": request.model_dump(mode="json"),
                    "task_contract": task.model_dump(mode="json"),
                },
            )
        budget = BudgetLedger(
            max_llm_calls=self.settings.scientific.max_llm_calls,
            max_experiments=self.settings.scientific.max_experiments,
            max_data_reads=self.settings.scientific.max_data_reads,
            max_replans=self.settings.scientific.max_replans,
        )
        investigation = InvestigationSpec(
            task=task,
            episode_id=request.episode_id,
            analysis_cutoff=environment.analysis_cutoff,
            allowed_reference_windows=environment.allowed_reference_windows,
            persistence_horizon_days=environment.persistence_horizon_days,
            spatial_support=environment.spatial_support,
            max_experiments=budget.max_experiments,
            max_replans=budget.max_replans,
            max_data_reads=budget.max_data_reads,
            max_llm_calls=budget.max_llm_calls,
        )
        refs: dict[str, ArtifactRef] = {}
        traces: list[dict[str, Any]] = []
        budget_history: list[dict[str, Any]] = []

        def save_json(path: str, value: object) -> None:
            refs[path] = artifacts.write_json(path, value, True)

        def save_jsonl(path: str, values: list[object]) -> None:
            lines = "".join(
                json.dumps(
                    item.model_dump(mode="json") if hasattr(item, "model_dump") else item,
                    ensure_ascii=False,
                    default=str,
                )
                + "\n"
                for item in values
            )
            refs[path] = artifacts.write_bytes(path, lines.encode(), "application/x-ndjson", True)

        save_json("task_contract.json", investigation.model_dump(mode="json"))
        save_json("events.json", [item.model_dump(mode="json") for item in environment.events()])
        reports: list[ScientificEventReport] = []
        for event in environment.events():
            event.investigation_status = "running"
            graph = EvidenceGraph(task_id, event.event_id)
            observations = environment.observations(investigation)
            for observation in observations:
                graph.add_root(observation)
            candidate = environment.candidate(event.event_id)
            initial = graph.add_initial_evidence(
                result_id=f"candidate-{event.event_id}",
                root_source_ids=[
                    item for item in candidate["candidate_root_source_ids"] if item in graph.sources
                ],
                measurement_value=candidate["candidate_effect"],
                coverage=candidate["candidate_coverage"],
                spatial_support=investigation.spatial_support,
                contains_mock=True,
            )
            event.evidence_ids.append(initial.evidence_id)
            risk_history = [HeuristicRiskAdapter().evaluate(event.event_id, graph)]
            prefix = f"events/{event.event_id}"

            def snapshot(
                stage: str,
                state: ScientificWorkflowState,
                _prefix: str = prefix,
                _graph: EvidenceGraph = graph,
            ) -> None:
                budget_history.append({"stage": stage, **state["budget"].model_dump(mode="json")})
                save_jsonl(f"{_prefix}/hypotheses.jsonl", state["hypotheses"])
                save_jsonl(f"{_prefix}/revisions.jsonl", state["revisions"])
                save_jsonl(f"{_prefix}/precommitments.jsonl", state["precommitments"])
                save_jsonl(f"{_prefix}/experiments.jsonl", state["results"])
                save_json(
                    f"{_prefix}/evidence_graph.json",
                    {
                        "sources": [
                            item.model_dump(mode="json") for item in _graph.sources.values()
                        ],
                        "evidence": [
                            item.model_dump(mode="json") for item in _graph.evidence.values()
                        ],
                    },
                )
                save_jsonl(f"{_prefix}/risk_history.jsonl", state["risk_history"])
                if state.get("decision") is not None:
                    save_json(
                        f"{_prefix}/decision.json",
                        state["decision"].model_dump(mode="json"),
                    )
                save_jsonl("budget_ledger.jsonl", budget_history)
                if self.audit_logger is not None:
                    self.audit_logger.record(
                        "scientific.snapshot",
                        task_id=task_id,
                        stage=stage,
                        message=f"科学调查状态快照：{stage}",
                        data={
                            "event": state["event"],
                            "hypotheses": state["hypotheses"],
                            "proposed_experiment": state["proposed_experiment"],
                            "precommitments": state["precommitments"],
                            "results": state["results"],
                            "revisions": state["revisions"],
                            "decision": state.get("decision"),
                            "budget": state["budget"],
                        },
                    )

            state: ScientificWorkflowState = {
                "event": event,
                "investigation": investigation,
                "environment": environment,
                "registry": self.registry,
                "executor": ExperimentExecutor(self.registry),
                "evidence_graph": graph,
                "budget": budget,
                "hypotheses": [],
                "revisions": [],
                "precommitments": [],
                "proposed_experiment": None,
                "results": [],
                "risk_history": risk_history,
                "action_values": [],
                "decision": None,
                "claim": None,
                "continue_investigation": False,
                "trace": [],
            }
            compiled = build_scientific_graph(self.llm, snapshot, self.audit_logger)
            state = compiled.invoke(
                state, config={"recursion_limit": self.settings.budgets.recursion_limit}
            )
            traces.extend(state["trace"])
            event.interference_factors = sorted(
                {
                    factor
                    for hypothesis in state["hypotheses"]
                    for factor in hypothesis.interference_factors
                },
                key=lambda item: item.value,
            )
            assert state["decision"] is not None and state["claim"] is not None
            snapshot("event_completed", state)
            reports.append(
                ScientificEventReport(
                    event=event,
                    hypotheses=state["hypotheses"],
                    revisions=state["revisions"],
                    precommitments=state["precommitments"],
                    experiments=state["results"],
                    evidence=list(graph.evidence.values()),
                    sources=list(graph.sources.values()),
                    risk_history=state["risk_history"],
                    action_values=state["action_values"],
                    decision=state["decision"],
                    claim=state["claim"],
                )
            )
        save_json("events.json", [item.event.model_dump(mode="json") for item in reports])
        save_json(
            "sources.json",
            [source.model_dump(mode="json") for report in reports for source in report.sources],
        )
        facts = ScientificReportFacts(
            task_id=task_id,
            episode_id=request.episode_id,
            contains_mock=True,
            notice="全部候选、观测、数值实验和结论均为离线模拟结果。",
            investigation=investigation,
            events=reports,
            budget=budget,
            limitations=[
                "P1 仅实现 E1 原始观测、E2 同季节对照、E3 持续性检验。",
                "风险与动作价值均为未训练启发式分数，不是概率；概率字段保持 null。",
                "未连接真实影像、真实遥感算法或付费模型接口。",
            ],
            artifacts=list(refs.values()),
        )
        renderer = ScientificReportRenderer()
        save_json("report_facts.json", facts.model_dump(mode="json"))
        refs["report.json"] = artifacts.write_bytes(
            "report.json", renderer.json(facts).encode(), "application/json", True
        )
        refs["report.md"] = artifacts.write_bytes(
            "report.md", renderer.markdown(facts).encode(), "text/markdown", True
        )
        refs["report.html"] = artifacts.write_bytes(
            "report.html", renderer.html(facts).encode(), "text/html", True
        )
        save_jsonl("execution_trace.jsonl", traces)
        save_json(
            "tool_results.json",
            [result.model_dump(mode="json") for report in reports for result in report.experiments],
        )
        save_json(
            "task.json",
            {
                "request": request.model_dump(mode="json"),
                "task": task.model_dump(mode="json"),
                "status": TaskStatus.COMPLETED.value,
                "contains_mock": True,
            },
        )
        save_json("effective_config.json", self.settings.effective_config())
        html_path = str(artifacts.resolve("report.html"))
        json_path = str(artifacts.resolve("report.json"))
        if self.audit_logger is not None:
            self.audit_logger.record(
                "scientific.report_ready",
                task_id=task_id,
                stage="report",
                message="事件级科学报告已生成",
                data={
                    "status": TaskStatus.COMPLETED,
                    "report_html": html_path,
                    "report_json": json_path,
                    "budget": budget,
                },
            )
        return ScientificRunOutput(
            status=TaskStatus.COMPLETED,
            steps=[
                "scientific: 保存任务契约、候选事件与初始证据",
                "scientific: LLM 基于白名单状态提出可检验解释",
                "scientific: 实验前承诺先持久化，再执行 LLM 提议的合法实验",
                "scientific: 更新来源图、证据、启发式风险并追加假设修订",
                "scientific: 程序执行停止/拒判规则并生成事件级模拟报告",
            ],
            report_html=html_path,
            report_json=json_path,
            artifacts=list(refs.values()),
        )

    def _task_contract(self, task_id: str, request: TaskRequest) -> TaskSpec:
        if request.aoi_id is None or request.aoi_id not in self.settings.aois:
            raise ValueError("scientific 模式必须使用已登记的 aoi_id")
        values = re.findall(r"(20\d{2})\s*年\s*(1[0-2]|0?[1-9])\s*月", request.query)
        if len(values) < 2:
            values = re.findall(r"(20\d{2})-(1[0-2]|0[1-9])", request.query)
        if len(values) < 2:
            raise ValueError("scientific 任务必须明确目标月和基准月")
        target = _month_window(*values[0])
        baseline = _month_window(*values[1])
        if baseline.start >= target.start:
            raise ValueError("基准期必须早于目标期")
        aoi = self.settings.aois[request.aoi_id]
        return TaskSpec(
            task_id=task_id,
            query=request.query,
            aoi_id=request.aoi_id,
            aoi_name=aoi["name"],
            area_km2=float(aoi["area_km2"]),
            geometry_ref=aoi["geometry_ref"],
            baseline=baseline,
            target=target,
            grid_meters=self.settings.app.grid_meters,
            seed=self.settings.app.seed,
            budgets=self.settings.scientific.model_dump(),
            schema_version=self.settings.app.schema_version,
            workflow_version="hera-p1.0",
            model_profile=request.model_profile,
        )


def _month_window(year_value: str, month_value: str) -> TimeWindow:
    year, month = int(year_value), int(month_value)
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return TimeWindow(start=start, end=end, label=f"{year}-{month:02d}")
