from __future__ import annotations

import json

import httpx
from fastapi.testclient import TestClient
from pydantic import BaseModel

from eo_agent.api import create_app
from eo_agent.config import ModelProfile
from eo_agent.llm.openai_compatible import OpenAICompatibleAdapter


class StructuredAnswer(BaseModel):
    choice: str


def test_page_static_and_csrf_protected_task_creation(tmp_path) -> None:
    app = create_app(tmp_path / "api")
    with TestClient(app) as client:
        page = client.get("/imagery")
        assert page.status_code == 200
        assert "交互式影像准备" in page.text
        script = client.get("/imagery/static/imagery.js")
        assert script.status_code == 200
        assert "实际发送的完整脱敏 messages、结构要求、候选数据与生效参数" in script.text
        assert "actual_response: item.response" in script.text
        assert "程序校验后实际采用的动作与参数" in script.text
        config = client.get("/api/imagery/config").json()
        assert config["default_model_profile"] == "mock"
        payload = {
            "query": "准备2024年6月研究区的 Sentinel-2 影像",
            "model_profile": "mock",
            "provider": "mock",
            "aoi_id": "wuhan_sample_plot_wgs84",
            "requested_data": ["optical"],
            "user_timezone": "Asia/Shanghai",
        }
        assert client.post("/api/imagery/tasks", json=payload).status_code == 403
        created = client.post(
            "/api/imagery/tasks",
            json=payload,
            headers={"X-CSRF-Token": config["csrf_token"]},
        )
        assert created.status_code == 202
        task_id = created.json()["task_id"]
        assert client.get(f"/api/imagery/tasks/{task_id}").status_code == 200


def test_openai_compatible_logs_every_429_retry_and_exact_messages() -> None:
    received: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(json.loads(request.content))
        if len(received) == 1:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(
            200,
            json={
                "model": "compatible-test",
                "choices": [
                    {
                        "message": {"content": '{"choice":"candidate-a"}'},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    profile = ModelProfile(
        name="second-test",
        provider="compatible",
        adapter="openai_compatible",
        model="compatible-test",
        api_key="secret-key",
        base_url="https://example.test/v1",
        json_response_format=True,
        generation_params={"temperature": 0},
    )
    events: list[tuple[str, dict]] = []
    adapter = OpenAICompatibleAdapter(
        profile,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_network_retries=1,
        max_calls=2,
    )
    result = adapter.generate_structured(
        "choose",
        {"candidate_ids": ["candidate-a"]},
        StructuredAnswer,
        observer=lambda event, value: events.append((event, value)),
    )
    assert result.payload.choice == "candidate-a"
    requests = [value for event, value in events if event == "request"]
    assert len(requests) == 2
    assert requests[0]["payload"] == received[0]
    assert requests[1]["payload"] == received[1]
    assert "secret-key" not in json.dumps(events)
    assert [value["http_status"] for event, value in events if event == "error"] == [429]
