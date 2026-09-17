from __future__ import annotations

from typing import Any, Generic, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from eo_agent.schemas import ActionSpec, ReportFacts, TaskDraft

T = TypeVar("T")


class LLMMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str
    profile: str
    requested_model: str
    returned_model: str | None = None
    prompt_version: str
    output_protocol: str
    duration_ms: float
    retries: int = 0
    schema_repairs: int = 0
    effective_parameters: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, int | None] | None = None
    error: str | None = None


class LLMResult(BaseModel, Generic[T]):
    payload: T
    metadata: LLMMetadata


class LLMClient(Protocol):
    def parse_task(self, query: str, aoi_id: str | None) -> LLMResult[TaskDraft]: ...

    def choose_action(
        self, visible_state: dict[str, Any], allowed_actions: list[str]
    ) -> LLMResult[ActionSpec]: ...

    def summarize(self, facts: ReportFacts) -> LLMResult[str]: ...
