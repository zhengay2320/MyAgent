from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel

from eo_agent.schemas import ToolResult


class RetryableToolError(RuntimeError):
    pass


@dataclass
class ToolContext:
    task_id: str
    seed: int
    scenario_config: dict[str, Any]
    scenario_state: dict[str, Any]
    resources: dict[str, str]


class DomainTool(Protocol):
    name: str
    implementation_id: str
    args_model: type[BaseModel]

    def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult: ...
