from __future__ import annotations

import json

import httpx
import pytest

from eo_agent.config import ModelProfile, load_settings
from eo_agent.llm.base import LLMAttemptBudgetExceeded
from eo_agent.llm.openai_compatible import LLMProviderError, OpenAICompatibleAdapter
from eo_agent.scientific.schemas import ClaimDraft


def profile(name: str = "deepseek_dev", json_mode: bool = True) -> ModelProfile:
    return ModelProfile(
        name=name,
        provider=name,
        adapter="openai_compatible",
        api_key="secret-test-key",
        base_url=f"https://{name}.example/v1/",
        model=f"{name}-model",
        json_response_format=json_mode,
        generation_params={"temperature": 0},
    )


@pytest.mark.parametrize("name", ["deepseek_dev", "second_compatible"])
def test_profiles_build_correct_mocktransport_request(name: str) -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        seen["json"] = json.loads(request.content)
        body = {
            "model": "returned-model",
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "aoi_text": "武汉演示区",
                                "baseline_month": "2024-09",
                                "target_month": "2025-10",
                                "task_type": "two_period_change",
                                "missing_fields": [],
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        }
        return httpx.Response(200, json=body)

    adapter = OpenAICompatibleAdapter(
        profile(name, json_mode=name == "deepseek_dev"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = adapter.parse_task("query", None)
    assert seen["url"] == f"https://{name}.example/v1/chat/completions"
    assert seen["auth"] == "Bearer secret-test-key"
    assert seen["json"]["model"] == f"{name}-model"
    assert ("response_format" in seen["json"]) is (name == "deepseek_dev")
    assert result.metadata.returned_model == "returned-model"
    assert result.metadata.usage["total_tokens"] == 3


def test_invalid_json_gets_one_repair() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        content = (
            "not-json"
            if calls == 1
            else json.dumps(
                {"action": "skip_reconstruction", "arguments": {}, "reason_summary": "ok"}
            )
        )
        return httpx.Response(
            200, json={"model": "m", "choices": [{"message": {"content": content}}]}
        )

    adapter = OpenAICompatibleAdapter(
        profile(), client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    result = adapter.choose_action({}, ["skip_reconstruction"])
    assert calls == 2
    assert result.metadata.attempts == 2
    assert result.metadata.schema_repairs == 1


def test_auth_error_not_retried_and_no_fallback() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"error": "bad key"})

    adapter = OpenAICompatibleAdapter(
        profile(), client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(LLMProviderError, match="401"):
        adapter.parse_task("q", None)
    assert calls == 1


def test_timeout_retry_is_bounded() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("slow", request=request)

    adapter = OpenAICompatibleAdapter(
        profile(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_network_retries=1,
    )
    with pytest.raises(LLMProviderError, match="网络调用失败"):
        adapter.parse_task("q", None)
    assert calls == 2


def test_default_client_uses_profile_request_timeout() -> None:
    configured = profile().model_copy(update={"request_timeout_seconds": 73.0})
    adapter = OpenAICompatibleAdapter(configured)
    try:
        assert adapter.client.timeout.read == 73.0
        assert adapter.client.timeout.connect == 15.0
    finally:
        adapter.client.close()


def test_empty_response_repair_is_bounded() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": ""}}]})

    adapter = OpenAICompatibleAdapter(
        profile(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_schema_repairs=1,
    )
    with pytest.raises(LLMProviderError, match="结构化输出无效"):
        adapter.parse_task("q", None)
    assert calls == 2


def test_zero_attempt_budget_sends_no_http_request() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    adapter = OpenAICompatibleAdapter(
        profile(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_calls=0,
    )
    with pytest.raises(LLMAttemptBudgetExceeded, match="请求未发送"):
        adapter.parse_task("q", None)
    assert calls == 0


@pytest.mark.parametrize("failure", ["timeout", "schema"])
def test_retry_and_schema_repair_reserve_before_next_http_attempt(failure: str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if failure == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(
            200, json={"model": "m", "choices": [{"message": {"content": "not-json"}}]}
        )

    adapter = OpenAICompatibleAdapter(
        profile(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_calls=1,
        max_network_retries=1,
        max_schema_repairs=1,
    )
    with pytest.raises(LLMAttemptBudgetExceeded, match="请求未发送"):
        adapter.parse_task("q", None)
    assert calls == 1


def test_missing_real_profile_configuration_is_explicit(monkeypatch) -> None:
    for key in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValueError, match="不会回退到 Mock"):
        load_settings("deepseek_dev")


def test_generic_structured_entry_uses_only_caller_visible_input() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "returned",
                "choices": [{"message": {"content": json.dumps({"statement": "离线协议响应"})}}],
            },
        )

    adapter = OpenAICompatibleAdapter(
        profile(), client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    result = adapter.generate_structured(
        "compose_claim", {"event_id": "EV1", "visible_metric": 0.3}, ClaimDraft
    )
    prompt = captured["messages"][0]["content"]
    assert result.payload.statement == "离线协议响应"
    assert "visible_metric" in prompt
    assert "private_evaluation" not in prompt
