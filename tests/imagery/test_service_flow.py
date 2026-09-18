from __future__ import annotations

import time
from pathlib import Path

import rasterio

from eo_agent.imagery.config import ImageryConfig
from eo_agent.imagery.providers.mock import MockImageryProvider
from eo_agent.imagery.schemas import (
    DownloadApprovalRequest,
    DownloadPlanUpdateRequest,
    ImageryTaskRequest,
)
from eo_agent.imagery.service import ImageryConflict, ImageryTaskService


def wait_for(service: ImageryTaskService, task_id: str, states: set[str], timeout: float = 15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = service.get_task(task_id)
        if task["status"] in states:
            return task
        time.sleep(0.02)
    raise AssertionError(f"task did not reach {states}: {service.get_task(task_id)['status']}")


def make_service(tmp_path: Path) -> ImageryTaskService:
    config = ImageryConfig(default_download_root=tmp_path / "downloads")
    return ImageryTaskService(tmp_path / "control", config=config)


def create_cloudy_task(service: ImageryTaskService) -> tuple[str, dict]:
    response = service.create_task(
        ImageryTaskRequest(
            query="准备2023年7月与2024年7月研究区的Sentinel-2影像，质量不足时建议SAR",
            aoi_id="wuhan_sample_plot_wgs84",
        )
    )
    task = wait_for(service, response["task_id"], {"WAITING_DOWNLOAD_APPROVAL", "FAILED"})
    assert task["status"] == "WAITING_DOWNLOAD_APPROVAL", task["error"]
    return response["task_id"], task


def test_full_mock_flow_stops_for_approval_then_downloads_real_files(tmp_path) -> None:
    service = make_service(tmp_path)
    task_id, task = create_cloudy_task(service)
    plan = service.get_plan(task_id)
    target = Path(plan["resolved_task_dir"])
    assert not target.exists()
    assert not list((tmp_path / "downloads").glob("**/*.tif"))
    assert len(task["llm_calls"]) == 3
    assert all(call["request"]["payload"]["messages"] for call in task["llm_calls"])
    candidates = service.get_candidates(task_id)
    assert candidates["recommendation"]["selected_sar_candidate_ids"]
    assert all(item["preview_artifact_id"] for item in candidates["optical_candidates"])

    approval = DownloadApprovalRequest(
        plan_version=plan["version"],
        plan_hash=plan["plan_hash"],
        idempotency_key="approval-flow-0001",
    )
    first = service.approve(task_id, approval)
    second = service.approve(task_id, approval)
    assert first["created"] is True
    assert second["idempotent"] is True
    completed = wait_for(service, task_id, {"COMPLETED", "PARTIAL", "FAILED"})
    assert completed["status"] == "COMPLETED", completed["error"]
    files = completed["download_files"]
    assert files and all(item["status"] == "completed" for item in files)
    assert (target / "download_manifest.json").is_file()
    assert (target / "approval.json").is_file()
    for item in plan["files"]:
        path = target / item["relative_path"]
        assert path.is_file()
        with rasterio.open(path) as dataset:
            assert dataset.driver == "GTiff"
        assert (target / "thumbnails" / f"{item['file_id']}.png").is_file()
    assert any(event["event_type"] == "download.progress" for event in completed["events"])
    assert any(event["event_type"] == "verification.finished" for event in completed["events"])


def test_plan_change_rebinds_hash_and_stale_approval_is_rejected(tmp_path) -> None:
    service = make_service(tmp_path)
    task_id, _task = create_cloudy_task(service)
    old = service.get_plan(task_id)
    new_root = tmp_path / "alternate"
    update = DownloadPlanUpdateRequest(
        plan_version=old["version"],
        plan_hash=old["plan_hash"],
        selected_candidate_ids=old["selected_candidate_ids"],
        selected_file_ids=[item["file_id"] for item in old["files"]],
        download_root=str(new_root),
        grid_meters=20,
        optical_bands=["B2", "B3", "B4", "B8", "B11", "B12"],
    )
    new = service.update_plan(task_id, update)
    assert new.version == old["version"] + 1
    assert new.plan_hash != old["plan_hash"]
    assert Path(new.resolved_task_dir).parent == new_root.resolve()
    try:
        service.approve(
            task_id,
            DownloadApprovalRequest(
                plan_version=old["version"],
                plan_hash=old["plan_hash"],
                idempotency_key="stale-plan-0001",
            ),
        )
    except ImageryConflict:
        pass
    else:  # pragma: no cover
        raise AssertionError("stale plan approval must be rejected")
    assert not new_root.exists()


def test_cancel_before_approval_never_creates_download_directory(tmp_path) -> None:
    service = make_service(tmp_path)
    task_id, _task = create_cloudy_task(service)
    plan = service.get_plan(task_id)
    result = service.cancel(task_id)
    assert result["status"] == "CANCELLED"
    assert not Path(plan["resolved_task_dir"]).exists()


def test_unknown_online_quality_requires_second_approval_before_sar(tmp_path) -> None:
    class QualityUnavailableProvider(MockImageryProvider):
        def assess_optical_quality(self, **_kwargs):
            raise RuntimeError("quality reducer unavailable")

    class QualityUnavailableService(ImageryTaskService):
        def _provider(self, kind):
            if kind.value == "mock":
                return QualityUnavailableProvider()
            return super()._provider(kind)

    config = ImageryConfig(default_download_root=tmp_path / "downloads")
    service = QualityUnavailableService(tmp_path / "control", config=config)
    created = service.create_task(
        ImageryTaskRequest(
            query="准备2024年6月研究区的 Sentinel-2 影像",
            aoi_id="wuhan_sample_plot_wgs84",
        )
    )
    task_id = created["task_id"]
    first_wait = wait_for(service, task_id, {"WAITING_DOWNLOAD_APPROVAL", "FAILED"})
    assert first_wait["status"] == "WAITING_DOWNLOAD_APPROVAL"
    first = service.get_plan(task_id)
    assert all("MOCK-S1" not in item for item in first["selected_candidate_ids"])
    service.approve(
        task_id,
        DownloadApprovalRequest(
            plan_version=first["version"],
            plan_hash=first["plan_hash"],
            idempotency_key="local-quality-first-approval",
        ),
    )
    second_wait = wait_for(
        service,
        task_id,
        {"WAITING_DOWNLOAD_APPROVAL", "COMPLETED", "PARTIAL", "FAILED"},
    )
    assert second_wait["status"] == "WAITING_DOWNLOAD_APPROVAL", second_wait["error"]
    second = service.get_plan(task_id)
    assert second["version"] == first["version"] + 1
    assert any("MOCK-S1" in item for item in second["selected_candidate_ids"])
    assert Path(first["resolved_task_dir"]).is_dir()
    assert not any(
        Path(first["resolved_task_dir"]).joinpath(item["relative_path"]).is_file()
        for item in second["files"]
        if item["data_kind"] == "sar"
    )
    service.approve(
        task_id,
        DownloadApprovalRequest(
            plan_version=second["version"],
            plan_hash=second["plan_hash"],
            idempotency_key="local-quality-second-approval",
        ),
    )
    completed = wait_for(service, task_id, {"COMPLETED", "PARTIAL", "FAILED"})
    assert completed["status"] == "COMPLETED", completed["error"]
