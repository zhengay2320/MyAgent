from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, TypeVar

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

import eo_agent.imagery.service as service_module
from eo_agent.api import create_app
from eo_agent.config import ModelProfile
from eo_agent.imagery.artifacts import ImageryArtifactStore
from eo_agent.imagery.config import ImageryConfig
from eo_agent.imagery.events import EventBroker
from eo_agent.imagery.planner import ImageryPlanner
from eo_agent.imagery.repository import ImageryRepository
from eo_agent.imagery.schemas import (
    DataKind,
    DataProviderKind,
    ImageryTaskRequest,
    ParsedRequest,
    PeriodRole,
    QueryMode,
    SarRecommendation,
    SarRecommendationStatus,
    SceneCandidate,
    SceneRecommendation,
    SearchPeriod,
)
from eo_agent.imagery.service import ImageryTaskService
from eo_agent.llm.base import (
    LLMAttemptBudgetExceeded,
    LLMMetadata,
    LLMResult,
    StructuredObserver,
    build_structured_messages,
)
from eo_agent.llm.mock import MockLLMAdapter
from eo_agent.llm.openai_compatible import OpenAICompatibleAdapter

T = TypeVar("T", bound=BaseModel)

A1 = "MOCK-S2-202409-01"
A2 = "MOCK-S2-202409-02"
B1 = "MOCK-S2-202510-01"
B2 = "MOCK-S2-202510-02"


def _periods() -> list[SearchPeriod]:
    return [
        SearchPeriod(
            period_id="p2024_09",
            label="2024-09",
            start=date(2024, 9, 1),
            end=date(2024, 10, 1),
            role=PeriodRole.BASELINE,
            original_expression="2024年9月",
        ),
        SearchPeriod(
            period_id="p2025_10",
            label="2025-10",
            start=date(2025, 10, 1),
            end=date(2025, 11, 1),
            role=PeriodRole.TARGET,
            original_expression="2025年10月",
        ),
    ]


def _recommendation(
    optical_ids: list[str],
    *,
    sar_status: SarRecommendationStatus = SarRecommendationStatus.NOT_NEEDED,
) -> SceneRecommendation:
    recommended_periods = ["p2025_10"] if sar_status == SarRecommendationStatus.RECOMMENDED else []
    return SceneRecommendation(
        selected_optical_candidate_ids=optical_ids,
        selected_sar_candidate_ids=[],
        rejected_candidates={},
        missing_information=[],
        sar_recommendation=SarRecommendation(
            status=sar_status,
            rationale="测试中的结构化 SAR 建议。",
            considered_alternative_optical_ids=[A1, A2, B1, B2],
            unresolved_quality_candidate_ids=[],
            recommended_period_ids=recommended_periods,
            rule_summary="SAR 不能替代每期一个光学候选的硬约束。",
        ),
        rationale="测试中的结构化光学候选选择。",
    )


class SequenceSelectionLLM:
    """Controlled LLM double: it proposes; production validation decides."""

    def __init__(self, recommendations: list[SceneRecommendation]) -> None:
        self.recommendations = list(recommendations)
        self.delegate = MockLLMAdapter()
        self.visible_calls: list[tuple[str, dict[str, Any]]] = []

    def generate_structured(
        self,
        purpose: str,
        visible_input: dict[str, Any],
        schema: type[T],
        observer: StructuredObserver | None = None,
    ) -> LLMResult[T]:
        self.visible_calls.append((purpose, visible_input))
        if purpose == "imagery_parse_request":
            value: BaseModel = ParsedRequest(
                task_summary="测试两时期影像准备。",
                query_mode=QueryMode.TWO_PERIOD_COMPARISON,
                periods=_periods(),
                requested_data=[DataKind.OPTICAL],
                original_query=str(visible_input["query"]),
                original_timezone=str(visible_input["user_timezone"]),
                role_interpretation="测试显式保留两个时期。",
                needs_aoi=False,
                missing_fields=[],
                extension_suggestions=[],
            )
        elif purpose in {
            "imagery_recommend_scenes",
            "imagery_recommend_scenes_repair",
        }:
            if not self.recommendations:
                raise AssertionError("planner 发起了超过测试序列上限的选图调用")
            value = self.recommendations.pop(0)
        else:
            return self.delegate.generate_structured(
                purpose, visible_input, schema, observer=observer
            )

        typed = schema.model_validate(value.model_dump(mode="json"))
        if observer:
            observer(
                "request",
                {
                    "purpose": purpose,
                    "attempt": 1,
                    "endpoint": "local://selection-sequence",
                    "payload": {
                        "model": "selection-sequence-v1",
                        "messages": build_structured_messages(
                            purpose, visible_input, schema
                        ),
                    },
                    "schema": schema.model_json_schema(),
                },
            )
            observer(
                "response",
                {
                    "purpose": purpose,
                    "attempt": 1,
                    "content": typed.model_dump_json(),
                    "finish_reason": "stop",
                    "returned_model": "selection-sequence-v1",
                    "usage": None,
                },
            )
            observer(
                "validation",
                {
                    "purpose": purpose,
                    "attempt": 1,
                    "valid": True,
                    "parsed": typed.model_dump(mode="json"),
                },
            )
        return LLMResult(
            payload=typed,
            metadata=LLMMetadata(
                provider="test",
                profile="selection-sequence",
                requested_model="selection-sequence-v1",
                returned_model="selection-sequence-v1",
                prompt_version="imagery-v1-test",
                output_protocol="json_object",
                duration_ms=0,
            ),
        )


def _wait_for_terminal_or_approval(
    service: ImageryTaskService, task_id: str, timeout: float = 15
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = service.get_task(task_id)
        if task["status"] in {"WAITING_DOWNLOAD_APPROVAL", "FAILED"}:
            return task
        time.sleep(0.02)
    raise AssertionError(f"任务未按时结束选图阶段: {service.get_task(task_id)['status']}")


def _run_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recommendations: list[SceneRecommendation],
    *,
    max_selection_repairs: int = 1,
) -> tuple[ImageryTaskService, SequenceSelectionLLM, dict[str, Any]]:
    fake = SequenceSelectionLLM(recommendations)
    monkeypatch.setattr(service_module, "create_llm", lambda _settings: fake)
    service = ImageryTaskService(
        tmp_path / "control",
        config=ImageryConfig(
            default_download_root=tmp_path / "downloads",
            max_selection_repairs=max_selection_repairs,
        ),
    )
    created = service.create_task(
        ImageryTaskRequest(
            query="准备2024年9月与2025年10月研究区的 Sentinel-2 影像",
            aoi_id="wuhan_sample_plot_wgs84",
        )
    )
    task = _wait_for_terminal_or_approval(service, created["task_id"])
    return service, fake, task


def _selection_calls(task: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in task["llm_calls"]
        if item["request"].get("purpose", "").startswith("imagery_recommend_scenes")
    ]


def test_valid_selection_succeeds_without_repair_and_builds_plan(
    tmp_path, monkeypatch
) -> None:
    service, fake, task = _run_service(
        tmp_path, monkeypatch, [_recommendation([A1, B1])]
    )
    try:
        assert task["status"] == "WAITING_DOWNLOAD_APPROVAL", task["error"]
        assert [purpose for purpose, _ in fake.visible_calls].count(
            "imagery_recommend_scenes_repair"
        ) == 0
        plan = service.get_plan(task["task_id"])
        assert set(plan["selected_candidate_ids"]) == {A1, B1}
        assert service.get_candidates(task["task_id"])["recommendation"][
            "accepted_by_program"
        ] is True
        selection_input = next(
            value
            for purpose, value in fake.visible_calls
            if purpose == "imagery_recommend_scenes"
        )
        assert selection_input["required_period_ids"] == ["p2024_09", "p2025_10"]
        assert set(selection_input["candidates_by_period"]) == {
            "p2024_09",
            "p2025_10",
        }
        assert selection_input["selection_constraints"][
            "exactly_one_optical_per_period"
        ] is True
    finally:
        service.close()


def test_invalid_first_selection_is_rejected_then_repaired(
    tmp_path, monkeypatch
) -> None:
    service, fake, task = _run_service(
        tmp_path,
        monkeypatch,
        [_recommendation([A1, A2]), _recommendation([A1, B1])],
    )
    try:
        assert task["status"] == "WAITING_DOWNLOAD_APPROVAL", task["error"]
        calls = _selection_calls(task)
        assert len(calls) == 2
        assert [item["accepted"] for item in calls] == [False, True]
        assert calls[0]["request"]["purpose"] == "imagery_recommend_scenes"
        assert calls[1]["request"]["purpose"] == "imagery_recommend_scenes_repair"
        repair_input = next(
            value
            for purpose, value in fake.visible_calls
            if purpose == "imagery_recommend_scenes_repair"
        )
        assert repair_input["previous_recommendation"][
            "selected_optical_candidate_ids"
        ] == [A1, A2]
        assert "时期 p2025_10 必须且只能选择一个光学候选" in repair_input[
            "validation_errors"
        ]
        events = [
            item["event_type"]
            for item in task["events"]
            if item["event_type"] in {"action.rejected", "action.accepted"}
            and item["details"].get("action", {}).get("tool_name")
            == "select_catalog_candidates"
        ]
        assert events == ["action.rejected", "action.accepted"]
        assert set(service.get_plan(task["task_id"])["selected_candidate_ids"]) == {
            A1,
            B1,
        }
    finally:
        service.close()


def test_second_invalid_selection_fails_after_configured_limit(
    tmp_path, monkeypatch
) -> None:
    service, _fake, task = _run_service(
        tmp_path,
        monkeypatch,
        [_recommendation([A1, A2]), _recommendation([A1, A2])],
    )
    try:
        assert task["status"] == "FAILED"
        assert task["plan"] is None
        assert len(_selection_calls(task)) == 2
        assert "时期 p2024_09 必须且只能选择一个光学候选" in task["error"]
        assert "时期 p2025_10 必须且只能选择一个光学候选" in task["error"]
        assert "已尝试业务修复 1 次" in task["error"]
        assert "硬约束未放宽" in task["error"]
    finally:
        service.close()


def test_unknown_candidate_is_rejected_and_only_llm_can_repair(
    tmp_path, monkeypatch
) -> None:
    service, _fake, task = _run_service(
        tmp_path,
        monkeypatch,
        [_recommendation(["FAKE-ID", B1]), _recommendation([A1, B1])],
    )
    try:
        assert task["status"] == "WAITING_DOWNLOAD_APPROVAL", task["error"]
        calls = _selection_calls(task)
        assert calls[0]["accepted"] is False
        assert "模型引用了候选池不存在的 ID: FAKE-ID" in calls[0]["validation"][
            "errors"
        ]
        plan = service.get_plan(task["task_id"])
        assert "FAKE-ID" not in plan["selected_candidate_ids"]
        assert set(plan["selected_candidate_ids"]) == {A1, B1}
    finally:
        service.close()


def test_sar_recommendation_cannot_replace_missing_optical_period(
    tmp_path, monkeypatch
) -> None:
    invalid = _recommendation(
        [A1], sar_status=SarRecommendationStatus.RECOMMENDED
    )
    service, _fake, task = _run_service(tmp_path, monkeypatch, [invalid, invalid])
    try:
        assert task["status"] == "FAILED"
        assert task["plan"] is None
        assert "时期 p2025_10 必须且只能选择一个光学候选" in task["error"]
        assert len(_selection_calls(task)) == 2
    finally:
        service.close()


def _candidate(identifier: str, period_id: str, acquired_at: datetime) -> SceneCandidate:
    return SceneCandidate(
        candidate_id=identifier,
        provider=DataProviderKind.MOCK,
        dataset="COPERNICUS/S2_SR_HARMONIZED",
        product_id=identifier,
        system_index=identifier,
        acquired_at=acquired_at,
        period_id=period_id,
        data_kind=DataKind.OPTICAL,
        coverage_fraction=0.95,
        scene_cloud_fraction=0.1,
        is_mock=True,
    )


def test_selection_repair_consumes_shared_http_attempt_budget(tmp_path) -> None:
    invalid = _recommendation([A1, A2])
    received: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "budget-test-model",
                "choices": [
                    {
                        "message": {"content": invalid.model_dump_json()},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    repository = ImageryRepository(tmp_path / "budget.sqlite3")
    task_id = "selection-budget-task"
    control_dir = tmp_path / "control" / task_id
    control_dir.mkdir(parents=True)
    repository.create_task(
        task_id=task_id,
        status="ASSESSING_QUALITY",
        query="query",
        model_profile="budget-test",
        data_backend="mock",
        request={"query": "query"},
        control_dir=control_dir,
    )
    adapter = OpenAICompatibleAdapter(
        ModelProfile(
            name="budget-test",
            provider="compatible",
            adapter="openai_compatible",
            model="budget-test-model",
            api_key="not-logged",
            base_url="https://example.test/v1",
            json_response_format=True,
            generation_params={"temperature": 0},
        ),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_network_retries=0,
        max_schema_repairs=0,
        max_calls=1,
    )
    planner = ImageryPlanner(
        adapter,
        repository,
        ImageryArtifactStore(tmp_path / "artifacts", task_id, repository),
        EventBroker(repository),
        model_profile="budget-test",
        max_selection_repairs=1,
    )
    candidates = [
        _candidate(A1, "p2024_09", datetime(2024, 9, 5, tzinfo=UTC)),
        _candidate(A2, "p2024_09", datetime(2024, 9, 15, tzinfo=UTC)),
        _candidate(B1, "p2025_10", datetime(2025, 10, 5, tzinfo=UTC)),
        _candidate(B2, "p2025_10", datetime(2025, 10, 15, tzinfo=UTC)),
    ]

    with pytest.raises(LLMAttemptBudgetExceeded, match="预算已耗尽"):
        planner.recommend_optical(task_id, _periods(), candidates, {})

    assert len(received) == 1
    calls = repository.list_llm_calls(task_id)
    assert len(calls) == 2
    assert calls[0]["accepted"] is False
    assert calls[1]["status"] == "failed"
    assert calls[1]["request"]["purpose"] == "imagery_recommend_scenes_repair"
    assert calls[1]["request"]["request_sent"] is False
    assert "not-logged" not in json.dumps(calls, ensure_ascii=False)


def test_chinese_validation_error_survives_sqlite_and_api_json(tmp_path) -> None:
    message = "时期 p2025_10 必须且只能选择一个光学候选"
    app = create_app(tmp_path / "api")
    with TestClient(app) as client:
        service: ImageryTaskService = app.state.imagery_service
        task_id = "utf8-selection-error"
        control_dir = service.control_root / task_id
        control_dir.mkdir(parents=True)
        service.repository.create_task(
            task_id=task_id,
            status="FAILED",
            query="中文校验测试",
            model_profile="mock",
            data_backend="mock",
            request={"query": "中文校验测试"},
            control_dir=control_dir,
        )
        service.events.emit(
            task_id,
            event_type="action.rejected",
            stage="FAILED",
            summary=f"程序拒绝模型动作：{message}",
            details={"accepted": False, "errors": [message]},
        )

        with sqlite3.connect(service.repository.path) as connection:
            stored_summary, stored_details = connection.execute(
                "SELECT summary, details_json FROM imagery_events WHERE task_id=?",
                (task_id,),
            ).fetchone()
        assert message in stored_summary
        assert json.loads(stored_details)["errors"] == [message]

        response = client.get(f"/api/imagery/tasks/{task_id}")
        assert response.status_code == 200
        assert response.json()["events"][0]["details"]["errors"] == [message]
        assert message in response.text
