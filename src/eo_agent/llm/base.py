from __future__ import annotations

from typing import Any, Generic, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from eo_agent.schemas import ActionSpec, ReportFacts, TaskDraft

T = TypeVar("T")
S = TypeVar("S", bound=BaseModel)


class LLMMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str
    profile: str
    requested_model: str
    returned_model: str | None = None
    prompt_version: str
    output_protocol: str
    duration_ms: float
    attempts: int = Field(default=1, ge=1)
    retries: int = 0
    schema_repairs: int = 0
    effective_parameters: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, int | None] | None = None
    error: str | None = None


class LLMResult(BaseModel, Generic[T]):
    payload: T
    metadata: LLMMetadata


class LLMAttemptBudgetExceeded(RuntimeError):
    """Raised before an external attempt when no request budget remains."""


class LLMAttemptBudget:
    """Task-scoped reservation counter shared by all compatible-provider calls."""

    def __init__(self, max_calls: int | None) -> None:
        if max_calls is not None and max_calls < 0:
            raise ValueError("LLM 调用预算不能为负数")
        self.max_calls = max_calls
        self.used_calls = 0

    def reserve(self) -> int:
        if self.max_calls is not None and self.used_calls >= self.max_calls:
            raise LLMAttemptBudgetExceeded("LLM HTTP 尝试预算已耗尽；请求未发送")
        self.used_calls += 1
        return self.used_calls


class LLMClient(Protocol):
    def parse_task(self, query: str, aoi_id: str | None) -> LLMResult[TaskDraft]: ...

    def choose_action(
        self, visible_state: dict[str, Any], allowed_actions: list[str]
    ) -> LLMResult[ActionSpec]: ...

    def summarize(self, facts: ReportFacts) -> LLMResult[str]: ...

    def generate_structured(
        self, purpose: str, visible_input: dict[str, Any], schema: type[S]
    ) -> LLMResult[S]: ...
