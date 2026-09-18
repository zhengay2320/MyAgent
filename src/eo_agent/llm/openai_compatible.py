from __future__ import annotations

import json
from time import perf_counter
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from eo_agent.config import ModelProfile
from eo_agent.llm.base import (
    LLMAttemptBudget,
    LLMMetadata,
    LLMResult,
    StructuredObserver,
    build_structured_messages,
    redact_for_log,
)
from eo_agent.schemas import ActionSpec, ReportFacts, TaskDraft

T = TypeVar("T", bound=BaseModel)


class LLMProviderError(RuntimeError):
    pass


class OpenAICompatibleAdapter:
    def __init__(
        self,
        profile: ModelProfile,
        client: httpx.Client | None = None,
        max_schema_repairs: int = 1,
        max_network_retries: int = 1,
        max_calls: int | None = None,
    ) -> None:
        if not profile.api_key or not profile.base_url or not profile.model:
            raise ValueError(f"profile {profile.name} 配置不完整；不会回退到 Mock")
        self.profile = profile
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(
                profile.request_timeout_seconds,
                connect=min(15.0, profile.request_timeout_seconds),
            )
        )
        self.max_schema_repairs = max_schema_repairs
        self.max_network_retries = max_network_retries
        self.attempt_budget = LLMAttemptBudget(max_calls)

    @property
    def endpoint(self) -> str:
        base = self.profile.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def _invoke(
        self,
        messages: list[dict[str, str]],
        schema: type[T],
        *,
        observer: StructuredObserver | None = None,
        purpose: str = "legacy",
    ) -> LLMResult[T]:
        started = perf_counter()
        repairs = 0
        retries = 0
        attempts = 0
        error: str | None = None
        while True:
            payload: dict[str, Any] = {
                "model": self.profile.model,
                "messages": messages,
                **self.profile.generation_params,
            }
            if self.profile.json_response_format:
                payload["response_format"] = {"type": "json_object"}
            self.attempt_budget.reserve()
            attempts += 1
            if observer:
                observer(
                    "request",
                    redact_for_log(
                        {
                            "purpose": purpose,
                            "attempt": attempts,
                            "endpoint": self.endpoint,
                            "payload": payload,
                            "schema": schema.model_json_schema(),
                        }
                    ),
                )
            try:
                response = self.client.post(
                    self.endpoint,
                    headers={"Authorization": f"Bearer {self.profile.api_key}"},
                    json=payload,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if observer:
                    observer(
                        "error",
                        {
                            "purpose": purpose,
                            "attempt": attempts,
                            "error_type": type(exc).__name__,
                            "message": "网络调用失败",
                        },
                    )
                if retries >= self.max_network_retries:
                    raise LLMProviderError(f"兼容模型网络调用失败: {type(exc).__name__}") from exc
                retries += 1
                continue
            if response.status_code in {401, 403}:
                if observer:
                    observer(
                        "error",
                        {
                            "purpose": purpose,
                            "attempt": attempts,
                            "http_status": response.status_code,
                            "message": "鉴权或权限失败",
                        },
                    )
                raise LLMProviderError(f"兼容模型鉴权/权限失败: HTTP {response.status_code}")
            if response.status_code == 429 or response.status_code >= 500:
                if observer:
                    observer(
                        "error",
                        {
                            "purpose": purpose,
                            "attempt": attempts,
                            "http_status": response.status_code,
                            "message": (
                                "限流或临时服务错误，将在预算内重试"
                                if retries < self.max_network_retries
                                else "限流或临时服务错误，重试上限已用尽"
                            ),
                        },
                    )
                if retries < self.max_network_retries:
                    retries += 1
                    continue
                raise LLMProviderError(
                    f"兼容模型临时服务错误: HTTP {response.status_code}"
                )
            if response.status_code >= 400:
                if observer:
                    observer(
                        "error",
                        {
                            "purpose": purpose,
                            "attempt": attempts,
                            "http_status": response.status_code,
                            "message": "兼容模型请求被拒绝",
                        },
                    )
                raise LLMProviderError(f"兼容模型请求失败: HTTP {response.status_code}")
            try:
                body = response.json()
                content = body["choices"][0]["message"]["content"]
                if observer:
                    observer(
                        "response",
                        redact_for_log(
                            {
                                "purpose": purpose,
                                "attempt": attempts,
                                "http_status": response.status_code,
                                "content": content,
                                "finish_reason": body["choices"][0].get("finish_reason"),
                                "returned_model": body.get("model"),
                                "usage": body.get("usage"),
                            }
                        ),
                    )
                if not content or not str(content).strip():
                    raise ValueError("空响应")
                value = schema.model_validate(json.loads(content))
            except (KeyError, IndexError, json.JSONDecodeError, ValidationError, ValueError) as exc:
                error = str(exc)
                if observer:
                    observer(
                        "validation",
                        {
                            "purpose": purpose,
                            "attempt": attempts,
                            "valid": False,
                            "error": error,
                        },
                    )
                if repairs >= self.max_schema_repairs:
                    raise LLMProviderError(f"兼容模型结构化输出无效: {error}") from exc
                repairs += 1
                messages = [
                    *messages,
                    {"role": "assistant", "content": str(content) if "content" in locals() else ""},
                    {
                        "role": "user",
                        "content": f"输出校验失败：{error}。仅返回符合 JSON Schema 的 JSON。",
                    },
                ]
                continue
            if observer:
                observer(
                    "validation",
                    {
                        "purpose": purpose,
                        "attempt": attempts,
                        "valid": True,
                        "parsed": value.model_dump(mode="json"),
                    },
                )
            usage_raw = body.get("usage")
            usage = None
            if isinstance(usage_raw, dict):
                usage = {
                    "prompt_tokens": usage_raw.get("prompt_tokens"),
                    "completion_tokens": usage_raw.get("completion_tokens"),
                    "total_tokens": usage_raw.get("total_tokens"),
                }
            meta = LLMMetadata(
                provider=self.profile.provider,
                profile=self.profile.name,
                requested_model=self.profile.model,
                returned_model=body.get("model"),
                prompt_version=self.profile.prompt_version,
                output_protocol="json_object"
                if self.profile.json_response_format
                else "prompted-json",
                duration_ms=round((perf_counter() - started) * 1000, 3),
                attempts=attempts,
                retries=retries,
                schema_repairs=repairs,
                effective_parameters=dict(self.profile.generation_params),
                usage=usage,
                error=error if repairs else None,
            )
            return LLMResult(payload=value, metadata=meta)

    def parse_task(self, query: str, aoi_id: str | None) -> LLMResult[TaskDraft]:
        prompt = (
            "解析两时期变化任务。仅输出 JSON，字段符合给定 schema。不得改写区域或月份。"
            f"\nSchema: {json.dumps(TaskDraft.model_json_schema(), ensure_ascii=False)}"
            f"\nquery={query}\naoi_id={aoi_id}"
        )
        return self._invoke([{"role": "user", "content": prompt}], TaskDraft)

    def generate_structured(
        self,
        purpose: str,
        visible_input: dict[str, Any],
        schema: type[T],
        observer: StructuredObserver | None = None,
    ) -> LLMResult[T]:
        """Generic structured entry point; callers must pass a pre-built safe view."""
        return self._invoke(
            build_structured_messages(purpose, visible_input, schema),
            schema,
            observer=observer,
            purpose=purpose,
        )

    def choose_action(
        self, visible_state: dict[str, Any], allowed_actions: list[str]
    ) -> LLMResult[ActionSpec]:
        prompt = (
            "从 allowed_actions 中选一个动作，只输出 JSON，不得提供代码或路径。"
            f"\nSchema: {json.dumps(ActionSpec.model_json_schema(), ensure_ascii=False)}"
            f"\nvisible_state={json.dumps(visible_state, ensure_ascii=False)}"
            f"\nallowed_actions={json.dumps(allowed_actions, ensure_ascii=False)}"
        )
        result = self._invoke([{"role": "user", "content": prompt}], ActionSpec)
        if result.payload.action not in allowed_actions:
            raise LLMProviderError("模型选择了未允许的动作")
        return result

    def summarize(self, facts: ReportFacts) -> LLMResult[str]:
        class Summary(BaseModel):
            text: str

        prompt = (
            '根据给定事实生成不超过120字的中文说明，不得增加数字或事实。只输出 {"text": ...}。'
            f"\nfacts={facts.model_dump_json(exclude={'artifacts'})}"
        )
        result = self._invoke([{"role": "user", "content": prompt}], Summary)
        return LLMResult(payload=result.payload.text, metadata=result.metadata)
