from __future__ import annotations

from typing import Any

from eo_agent.config import load_settings
from eo_agent.llm.base import LLMMetadata, LLMResult
from eo_agent.schemas import ActionSpec, ReportFacts, TaskDraft, TaskRequest, TaskStatus
from eo_agent.workflow.nodes import WorkflowNodes
from eo_agent.workflow.state import WorkflowState


class ProviderFakeLLM:
    def parse_task(self, query: str, aoi_id: str | None) -> LLMResult[TaskDraft]:
        return LLMResult(
            payload=TaskDraft(
                aoi_text="武汉演示区",
                baseline_month="2024-09",
                target_month="2025-10",
                task_type="two_period_change",
            ),
            metadata=LLMMetadata(
                provider="deepseek",
                profile="deepseek_dev",
                requested_model="requested-model",
                returned_model="returned-model",
                prompt_version="test-v1",
                output_protocol="json_object",
                duration_ms=1,
            ),
        )

    def choose_action(
        self, visible_state: dict[str, Any], allowed_actions: list[str]
    ) -> LLMResult[ActionSpec]:
        raise AssertionError("not used")

    def summarize(self, facts: ReportFacts) -> LLMResult[str]:
        raise AssertionError("not used")


def test_parse_step_uses_actual_provider_metadata() -> None:
    state: WorkflowState = {
        "request": TaskRequest(
            query="对武汉演示区2025年10月与2024年9月进行变化检测",
            aoi_id="wuhan_demo_100km2",
            model_profile="deepseek_dev",
        ),
        "task_id": "test-task",
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
        "contains_mock": False,
        "error": None,
        "report_facts": None,
        "report_summary": None,
    }
    nodes = WorkflowNodes(load_settings("mock"), ProviderFakeLLM(), executor=object())
    result = nodes.parse(state)
    assert "provider=deepseek" in result["steps"][0]
    assert "profile=deepseek_dev" in result["steps"][0]
    assert "model=returned-model" in result["steps"][0]
    assert "MockLLM" not in result["steps"][0]
