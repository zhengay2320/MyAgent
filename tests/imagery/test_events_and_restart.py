from __future__ import annotations

from eo_agent.imagery.events import EventBroker
from eo_agent.imagery.repository import ImageryRepository
from eo_agent.imagery.schemas import ImageryTaskStatus
from eo_agent.imagery.service import ImageryTaskService


def create_row(repository: ImageryRepository, tmp_path, task_id: str, status: str) -> None:
    control = tmp_path / task_id
    control.mkdir()
    repository.create_task(
        task_id=task_id,
        status=status,
        query="query",
        model_profile="mock",
        data_backend="mock",
        request={"query": "query"},
        control_dir=control,
    )


def test_event_sequences_are_replayable_and_isolated(tmp_path) -> None:
    repository = ImageryRepository(tmp_path / "events.sqlite3")
    create_row(repository, tmp_path, "task-a", "CREATED")
    create_row(repository, tmp_path, "task-b", "CREATED")
    broker = EventBroker(repository)
    first = broker.emit(
        "task-a",
        event_type="tool.started",
        stage="SEARCHING_OPTICAL",
        summary="start",
    )
    second = broker.emit(
        "task-a",
        event_type="tool.finished",
        stage="SEARCHING_OPTICAL",
        summary="done",
    )
    broker.emit(
        "task-b",
        event_type="task.failed",
        stage="FAILED",
        summary="other task",
    )
    assert first["sequence"] == 1
    assert second["sequence"] == 2
    assert [item["summary"] for item in repository.events_after("task-a", 1)] == ["done"]
    assert all(item["task_id"] == "task-a" for item in repository.events_after("task-a"))


def test_service_restart_marks_active_task_interrupted_without_resuming(tmp_path) -> None:
    output = tmp_path / "control"
    repository = ImageryRepository(output / "imagery.sqlite3")
    create_row(repository, tmp_path, "task-running", ImageryTaskStatus.DOWNLOADING.value)
    service = ImageryTaskService(output)
    task = service.get_task("task-running")
    assert service.interrupted_on_startup == 1
    assert task["status"] == ImageryTaskStatus.INTERRUPTED.value
    assert task["download_started"] is False
