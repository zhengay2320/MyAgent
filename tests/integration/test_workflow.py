from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from eo_agent.schemas import TaskRequest, TaskStatus
from eo_agent.service import TaskService

QUERY = "对武汉演示区2025年10月与2024年9月进行变化检测"


def run_scenario(tmp_path: Path, scenario: str):
    service = TaskService(tmp_path / scenario)
    result = service.run(
        TaskRequest(
            query=QUERY,
            aoi_id="wuhan_demo_100km2",
            scenario=scenario,
            model_profile="mock",
        )
    )
    task_dir = Path(service.get_task(result.task_id)["task_dir"])
    tools = json.loads((task_dir / "tool_results.json").read_text(encoding="utf-8"))
    report = (
        json.loads((task_dir / "report.json").read_text(encoding="utf-8"))
        if (task_dir / "report.json").exists()
        else None
    )
    return service, result, task_dir, tools, report


def names(tools: list[dict]) -> list[str]:
    return [item["tool_name"] for item in tools]


def test_clear_skips_reconstruction_and_cloudy_calls_it(tmp_path: Path) -> None:
    _, clear, _, clear_tools, _ = run_scenario(tmp_path, "clear")
    _, cloudy, _, cloudy_tools, _ = run_scenario(tmp_path, "cloudy")
    assert clear.status == cloudy.status == TaskStatus.COMPLETED
    assert "reconstruct_optical" not in names(clear_tools)
    assert "reconstruct_optical" in names(cloudy_tools)
    assert "search_observations" in names(cloudy_tools)


def test_evidence_reprocesses_and_updates_resource(tmp_path: Path) -> None:
    _, result, _, tools, report = run_scenario(tmp_path, "needs_evidence")
    assert result.status == TaskStatus.COMPLETED
    assert names(tools).count("fetch_additional_observation") == 1
    assert names(tools).count("prepare_data") == 2
    assert names(tools).count("detect_change") == 2
    prepare_sources = [
        item["data"]["source_resource_id"] for item in tools if item["tool_name"] == "prepare_data"
    ]
    assert prepare_sources[0] != prepare_sources[1]
    assert report["verification_status"] == "supported"


def test_no_data_is_partial_and_never_zero_change(tmp_path: Path) -> None:
    _, result, task_dir, tools, report = run_scenario(tmp_path, "no_data")
    assert result.status == TaskStatus.PARTIAL
    assert names(tools) == ["search_observations"]
    assert report["change_area_km2"] is None
    assert "无法判断" in report["conclusion"]
    assert "未变化" not in (task_dir / "report.html").read_text(encoding="utf-8")


def test_retry_and_insufficient_are_bounded(tmp_path: Path) -> None:
    _, retried, _, retry_tools, _ = run_scenario(tmp_path, "temporary_failure")
    assert retried.status == TaskStatus.COMPLETED
    prepare = [item for item in retry_tools if item["tool_name"] == "prepare_data"]
    assert [item["status"] for item in prepare] == ["retryable_error", "succeeded"]
    _, insufficient, _, tools, report = run_scenario(tmp_path, "insufficient")
    assert insufficient.status == TaskStatus.PARTIAL
    assert names(tools).count("fetch_additional_observation") == 1
    assert len(tools) < 20
    assert report["verification_status"] == "insufficient"
    assert any(step.startswith("mark_partial:") for step in insufficient.steps)


def test_outputs_sqlite_trace_and_mock_propagation(tmp_path: Path) -> None:
    service, result, task_dir, tools, report = run_scenario(tmp_path, "cloudy")
    for name in (
        "task.json",
        "effective_config.json",
        "execution_trace.jsonl",
        "tool_results.json",
        "report_facts.json",
        "report.md",
        "report.html",
        "report.json",
    ):
        assert (task_dir / name).is_file()
    assert all(item["contains_mock"] for item in tools)
    assert report["contains_mock"] is True and report["mock_result"] is True
    html = (task_dir / "report.html").read_text(encoding="utf-8")
    assert "不代表研究区域的真实地表变化" in html
    trace = service.get_trace(result.task_id)
    assert sum(item["event_type"] == "tool_call" for item in trace) == len(tools)
    with sqlite3.connect(service.repository.db_path) as conn:
        row = conn.execute(
            "SELECT status,contains_mock FROM tasks WHERE task_id=?", (result.task_id,)
        ).fetchone()
    assert row == ("COMPLETED", 1)


def test_domain_results_repeat_and_task_retry_state_is_isolated(tmp_path: Path) -> None:
    _, first, _, _, first_report = run_scenario(tmp_path / "repeat-a", "clear")
    _, second, _, _, second_report = run_scenario(tmp_path / "repeat-b", "clear")
    assert first.task_id != second.task_id
    assert first_report["change_area_km2"] == second_report["change_area_km2"]
    assert first_report["change_events"] == second_report["change_events"]

    _, _, _, first_retry, _ = run_scenario(tmp_path / "isolation-a", "temporary_failure")
    _, _, _, second_retry, _ = run_scenario(tmp_path / "isolation-b", "temporary_failure")
    for tools in (first_retry, second_retry):
        statuses = [item["status"] for item in tools if item["tool_name"] == "prepare_data"]
        assert statuses == ["retryable_error", "succeeded"]


def test_change_fraction_uses_task_aoi_area_denominator(tmp_path: Path) -> None:
    service = TaskService(tmp_path / "denominator")
    result = service.run(
        TaskRequest(
            query="对面积分母测试区2025年10月与2024年9月进行变化检测",
            aoi_id="denominator_demo_200km2",
            scenario="clear",
            model_profile="mock",
        )
    )
    assert result.status == TaskStatus.COMPLETED
    report = json.loads(Path(result.report_json).read_text(encoding="utf-8"))
    assert report["change_area_denominator_km2"] == 200
    assert report["change_fraction_denominator"] == "task_aoi_area"
    assert report["changed_fraction"] == pytest.approx(report["change_area_km2"] / 200)
    assert report["changed_fraction"] != pytest.approx(report["change_area_km2"] / 100)


@pytest.mark.parametrize(
    ("query", "aoi_id"),
    [
        ("对武汉2025年10月与2024年9月进行变化检测", None),
        (QUERY, "missing"),
        ("对武汉演示区进行变化检测", "wuhan_demo_100km2"),
        ("对大型合成测试区2025年10月与2024年9月进行变化检测", "large_demo_500km2"),
    ],
)
def test_invalid_inputs_stop_before_tools(tmp_path: Path, query: str, aoi_id: str | None) -> None:
    service = TaskService(tmp_path / "invalid")
    result = service.run(TaskRequest(query=query, aoi_id=aoi_id, scenario="clear"))
    assert result.status == TaskStatus.WAITING_INPUT
    task = service.get_task(result.task_id)
    tools = json.loads((Path(task["task_dir"]) / "tool_results.json").read_text(encoding="utf-8"))
    assert tools == []


def test_html_escapes_user_input(tmp_path: Path) -> None:
    query = QUERY + " <script>alert(1)</script>"
    service = TaskService(tmp_path)
    result = service.run(TaskRequest(query=query, aoi_id="wuhan_demo_100km2", scenario="clear"))
    html = Path(result.report_html).read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
