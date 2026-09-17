from __future__ import annotations

from fastapi.testclient import TestClient

from eo_agent.api import create_app


def test_sync_api_and_downloads(tmp_path) -> None:
    client = TestClient(create_app(tmp_path))
    assert client.get("/health").json() == {"status": "ok", "mode": "offline-first"}
    response = client.post(
        "/api/tasks/run",
        json={
            "query": "对武汉演示区2025年10月与2024年9月进行变化检测",
            "aoi_id": "wuhan_demo_100km2",
            "scenario": "cloudy",
            "model_profile": "mock",
        },
    )
    assert response.status_code == 200
    result = response.json()
    task_id = result["task_id"]
    assert result["status"] == "COMPLETED"
    assert client.get(f"/api/tasks/{task_id}").status_code == 200
    trace = client.get(f"/api/tasks/{task_id}/trace").json()
    assert any(item["event_type"] == "tool_call" for item in trace)
    report = client.get(f"/api/tasks/{task_id}/report?format=json")
    assert report.status_code == 200 and report.json()["mock_result"] is True
    html = client.get(f"/api/tasks/{task_id}/report?format=html")
    assert html.status_code == 200 and "模拟结果" in html.text
    assert client.get(f"/api/tasks/{task_id}/artifacts/not-a-real-id").status_code == 404
