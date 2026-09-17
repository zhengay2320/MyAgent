from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from eo_agent.config import load_settings
from eo_agent.llm.factory import create_llm
from eo_agent.reports.renderer import ReportRenderer
from eo_agent.schemas import ArtifactRef, TaskRequest, TaskRunResult, TaskStatus
from eo_agent.storage.artifacts import ArtifactStore
from eo_agent.storage.repository import TaskRepository
from eo_agent.tools.executor import ToolExecutor
from eo_agent.tools.mock_tools import create_default_registry
from eo_agent.tools.registry import ToolRegistry
from eo_agent.workflow.graph import build_graph
from eo_agent.workflow.state import WorkflowState


class TaskService:
    def __init__(
        self,
        output_root: str | Path,
        *,
        registry: ToolRegistry | None = None,
    ) -> None:
        self.output_root = Path(output_root).resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.repository = TaskRepository(self.output_root / "tasks.sqlite3")
        self.registry = registry or create_default_registry()

    def run(self, request: TaskRequest) -> TaskRunResult:
        settings = load_settings(request.model_profile)
        if request.scenario not in settings.scenarios:
            raise ValueError(f"未知场景: {request.scenario}")
        task_id = uuid4().hex
        artifacts = ArtifactStore(self.output_root, task_id)
        self.repository.create_task(
            task_id, request.query, request.scenario, request.model_profile, artifacts.task_dir
        )
        state: WorkflowState = {
            "request": request,
            "task_id": task_id,
            "status": TaskStatus.CREATED,
            "stage": "created",
            "draft": None,
            "task": None,
            "current_resource_id": None,
            "resources": {},
            "tool_results": [],
            "trace": [],
            "steps": [],
            "scenario_state": {},
            "evidence_count": 0,
            "replan_count": 0,
            "llm_call_count": 0,
            "contains_mock": True,
            "error": None,
            "report_facts": None,
            "report_summary": None,
        }
        executor = ToolExecutor(
            self.registry,
            artifacts,
            max_retries=settings.budgets.max_tool_retries,
            max_calls=settings.budgets.max_tool_calls,
        )
        all_artifacts: list[ArtifactRef] = []
        report_html_path: str | None = None
        report_json_path: str | None = None
        try:
            graph = build_graph(settings, create_llm(settings), executor)
            state = graph.invoke(
                state, config={"recursion_limit": settings.budgets.recursion_limit}
            )
        except Exception as exc:  # terminal persistence is intentional
            state["status"] = TaskStatus.FAILED
            state["error"] = f"{type(exc).__name__}: {exc}"

        all_artifacts.extend(item for result in state["tool_results"] for item in result.artifacts)
        task_ref = artifacts.write_json(
            "task.json",
            {
                "request": request.model_dump(mode="json"),
                "draft": state["draft"].model_dump(mode="json") if state["draft"] else None,
                "task": state["task"].model_dump(mode="json") if state["task"] else None,
                "status": state["status"].value,
                "error": state["error"],
                "contains_mock": True,
            },
            True,
        )
        config_ref = artifacts.write_json(
            "effective_config.json", settings.effective_config(), True
        )
        trace_content = "".join(event.model_dump_json() + "\n" for event in state["trace"]).encode(
            "utf-8"
        )
        trace_ref = artifacts.write_bytes(
            "execution_trace.jsonl", trace_content, "application/x-ndjson", True
        )
        tool_ref = artifacts.write_json(
            "tool_results.json",
            [result.model_dump(mode="json") for result in state["tool_results"]],
            True,
        )
        all_artifacts.extend([task_ref, config_ref, trace_ref, tool_ref])
        if state["report_facts"] is not None:
            renderer = ReportRenderer()
            facts = state["report_facts"]
            summary = state["report_summary"] or "报告文字生成失败；仅展示结构化事实。"
            facts_ref = artifacts.write_json(
                "report_facts.json", facts.model_dump(mode="json"), True
            )
            md_ref = artifacts.write_bytes(
                "report.md", renderer.markdown(facts, summary).encode(), "text/markdown", True
            )
            json_ref = artifacts.write_bytes(
                "report.json",
                renderer.json(facts, summary).encode(),
                "application/json",
                True,
            )
            html_ref = artifacts.write_bytes(
                "report.html", renderer.html(facts, summary).encode(), "text/html", True
            )
            all_artifacts.extend([facts_ref, md_ref, json_ref, html_ref])
            report_html_path = str(artifacts.resolve(html_ref.relative_path))
            report_json_path = str(artifacts.resolve(json_ref.relative_path))
        result = TaskRunResult(
            task_id=task_id,
            status=state["status"],
            contains_mock=True,
            steps=state["steps"],
            report_html=report_html_path,
            report_json=report_json_path,
            error=state["error"],
        )
        self.repository.finish_task(
            task_id,
            state["status"],
            True,
            result.model_dump(mode="json"),
            report_html_path,
            report_json_path,
            state["error"],
            all_artifacts,
        )
        return result

    def get_task(self, task_id: str) -> dict | None:
        return self.repository.get_task(task_id)

    def get_trace(self, task_id: str) -> list[dict]:
        task = self._required_task(task_id)
        path = Path(task["task_dir"]) / "execution_trace.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def report_path(self, task_id: str, format_name: str) -> Path:
        task = self._required_task(task_id)
        key = "report_html" if format_name == "html" else "report_json"
        value = task.get(key)
        if not value:
            raise FileNotFoundError("该任务没有报告")
        path = Path(value).resolve()
        task_dir = Path(task["task_dir"]).resolve()
        if task_dir not in path.parents or not path.is_file():
            raise FileNotFoundError("报告路径无效")
        return path

    def artifact_path(self, task_id: str, artifact_id: str) -> tuple[Path, str]:
        task = self._required_task(task_id)
        row = self.repository.get_artifact(task_id, artifact_id)
        if row is None:
            raise FileNotFoundError("产物不存在")
        path = (Path(task["task_dir"]) / row["relative_path"]).resolve()
        task_dir = Path(task["task_dir"]).resolve()
        if task_dir not in path.parents or not path.is_file():
            raise FileNotFoundError("产物路径无效")
        return path, row["media_type"]

    def _required_task(self, task_id: str) -> dict:
        task = self.repository.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        return task
