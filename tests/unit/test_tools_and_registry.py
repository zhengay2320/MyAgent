from __future__ import annotations

import json
from pathlib import Path

import pytest

from eo_agent.schemas import (
    ChangeData,
    ResourceArgs,
    ScientificValidity,
    TaskRequest,
    ToolResult,
    ToolStatus,
)
from eo_agent.service import TaskService
from eo_agent.storage.artifacts import ArtifactStore
from eo_agent.tools.base import ToolContext
from eo_agent.tools.executor import ToolExecutor
from eo_agent.tools.mock_tools import create_default_registry


class ReplacementDetector:
    name = "detect_change"
    implementation_id = "test-replacement-v1"
    args_model = ResourceArgs

    def execute(self, arguments, context):
        assert arguments.resource_id in context.resources
        data = ChangeData(
            resource_id=f"replacement-{context.task_id}",
            source_resource_id=arguments.resource_id,
            change_area_km2=9.99,
            changed_fraction=0.0999,
            can_determine=True,
            events=[{"type": "replacement", "area_km2": 9.99}],
            evidence_round=0,
        )
        context.resources[data.resource_id] = self.name
        return ToolResult(
            status=ToolStatus.SUCCEEDED,
            tool_name=self.name,
            implementation_id=self.implementation_id,
            data=data,
            contains_mock=True,
            scientific_validity=ScientificValidity.NOT_EVALUATED,
        )


def test_registry_replacement_changes_result_without_workflow_edit(tmp_path: Path) -> None:
    registry = create_default_registry()
    registry.register(ReplacementDetector(), replace=True)
    service = TaskService(tmp_path, registry=registry)
    result = service.run(
        TaskRequest(
            query="对武汉演示区2025年10月与2024年9月进行变化检测",
            aoi_id="wuhan_demo_100km2",
            scenario="clear",
        )
    )
    report = json.loads(Path(result.report_json).read_text(encoding="utf-8"))
    assert report["change_area_km2"] == 9.99
    assert any("test-replacement-v1" in item for item in report["tool_implementations"])


def test_executor_rejects_unknown_stage_params_and_foreign_resource(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path, "task")
    executor = ToolExecutor(create_default_registry(), store, max_retries=1, max_calls=20)
    context = ToolContext("task", 1, {"cloud_fraction": 0.1}, {}, {})
    with pytest.raises(ValueError, match="不允许"):
        executor.execute(
            "detect_change", "search", {"task_id": "task", "resource_id": "x"}, context
        )
    with pytest.raises(ValueError, match="参数无效"):
        executor.execute(
            "detect_change", "detect", {"task_id": "task", "resource_id": "x", "bad": 1}, context
        )
    with pytest.raises(ValueError, match="不属于"):
        executor.execute(
            "detect_change", "detect", {"task_id": "task", "resource_id": "foreign"}, context
        )
    with pytest.raises(ValueError, match="未注册"):
        executor.registry.get("shell")
