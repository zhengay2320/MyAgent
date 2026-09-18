from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any, Generic, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from eo_agent.schemas import ActionSpec, ReportFacts, TaskDraft

T = TypeVar("T")
S = TypeVar("S", bound=BaseModel)
StructuredObserver = Callable[[str, dict[str, Any]], None]


def build_structured_messages(
    purpose: str, visible_input: dict[str, Any], schema: type[BaseModel]
) -> list[dict[str, str]]:
    prompt = (
        "完成指定的受约束应用步骤。只能使用 visible_input 中提供的数据；不得编造候选 ID、"
        "数值、时间、几何、路径或未来结果。仅输出符合 JSON Schema 的完整 JSON。"
        f"\npurpose={purpose}"
        f"\nSchema={json.dumps(schema.model_json_schema(), ensure_ascii=False)}"
        f"\nvisible_input={json.dumps(visible_input, ensure_ascii=False, default=str)}"
    )
    return [{"role": "user", "content": prompt}]


def redact_for_log(value: Any) -> Any:
    """Redact secrets and query strings without altering the request actually sent."""
    sensitive = re.compile(r"(authorization|api[_-]?key|cookie|token|signature)", re.I)
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if sensitive.search(str(key)) else redact_for_log(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_for_log(item) for item in value]
    if isinstance(value, str) and re.match(r"^https?://", value):
        return value.split("?", 1)[0] + ("?[REDACTED]" if "?" in value else "")
    return value


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
        self,
        purpose: str,
        visible_input: dict[str, Any],
        schema: type[S],
        observer: StructuredObserver | None = None,
    ) -> LLMResult[S]: ...
