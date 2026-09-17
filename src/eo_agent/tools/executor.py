from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Any

from pydantic import ValidationError

from eo_agent.schemas import ScientificValidity, ToolResult, ToolStatus
from eo_agent.storage.artifacts import ArtifactStore
from eo_agent.tools.base import RetryableToolError, ToolContext
from eo_agent.tools.registry import ToolRegistry

ALLOWED_STAGES = {
    "search": {"search_observations"},
    "prepare": {"prepare_data"},
    "quality": {"inspect_quality"},
    "reconstruct": {"reconstruct_optical"},
    "detect": {"detect_change"},
    "verify": {"verify_change"},
    "evidence": {"fetch_additional_observation"},
}


@dataclass
class ExecutionAttempt:
    result: ToolResult
    duration_ms: float


class ToolExecutor:
    def __init__(
        self, registry: ToolRegistry, artifacts: ArtifactStore, max_retries: int, max_calls: int
    ) -> None:
        self.registry = registry
        self.artifacts = artifacts
        self.max_retries = max_retries
        self.max_calls = max_calls
        self.call_count = 0

    def execute(
        self, name: str, stage: str, arguments: dict[str, Any], context: ToolContext
    ) -> list[ExecutionAttempt]:
        if name not in ALLOWED_STAGES.get(stage, set()):
            raise ValueError(f"工具 {name} 不允许在阶段 {stage} 执行")
        tool = self.registry.get(name)
        try:
            validated = tool.args_model.model_validate(arguments)
        except ValidationError as exc:
            raise ValueError(f"工具参数无效: {exc}") from exc
        attempts: list[ExecutionAttempt] = []
        for retry in range(self.max_retries + 1):
            if self.call_count >= self.max_calls:
                raise RuntimeError("工具调用预算已耗尽")
            self.call_count += 1
            started = perf_counter()
            try:
                result = tool.execute(validated, context)
            except RetryableToolError as exc:
                result = ToolResult(
                    status=ToolStatus.RETRYABLE_ERROR,
                    tool_name=name,
                    implementation_id=tool.implementation_id,
                    contains_mock=True,
                    scientific_validity=ScientificValidity.NOT_EVALUATED,
                    error=str(exc),
                    provenance={"retry": retry},
                )
                attempts.append(
                    ExecutionAttempt(result, round((perf_counter() - started) * 1000, 3))
                )
                if retry < self.max_retries:
                    continue
                return attempts
            if result.status == ToolStatus.SUCCEEDED and result.data is not None:
                artifact = self.artifacts.write_json(
                    f"artifacts/{self.call_count:02d}_{name}.mock.json",
                    {
                        "mock_notice": "此文件为模拟结果，不代表真实地表变化",
                        "tool_result": result.model_dump(mode="json"),
                    },
                    contains_mock=True,
                )
                result.artifacts.append(artifact)
            attempts.append(ExecutionAttempt(result, round((perf_counter() - started) * 1000, 3)))
            return attempts
        return attempts
