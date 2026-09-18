from __future__ import annotations

from eo_agent.config import Settings
from eo_agent.llm.base import LLMClient
from eo_agent.llm.mock import MockLLMAdapter
from eo_agent.llm.openai_compatible import OpenAICompatibleAdapter


def create_llm(settings: Settings, *, max_calls: int | None = None) -> LLMClient:
    if settings.profile.adapter == "mock":
        return MockLLMAdapter()
    if settings.profile.adapter == "openai_compatible":
        return OpenAICompatibleAdapter(
            settings.profile,
            max_schema_repairs=settings.budgets.max_schema_repairs,
            max_calls=settings.budgets.max_llm_calls if max_calls is None else max_calls,
        )
    raise ValueError(f"不支持的 LLM adapter: {settings.profile.adapter}")
