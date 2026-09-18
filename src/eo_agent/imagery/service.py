from __future__ import annotations

import math
import os
import shutil
import threading
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from eo_agent.config import PROJECT_ROOT, load_settings
from eo_agent.imagery.aoi import AOIValidationError, geometry_as_geojson, normalize_geojson
from eo_agent.imagery.artifacts import ImageryArtifactStore
from eo_agent.imagery.config import ImageryConfig, load_imagery_config
from eo_agent.imagery.download import DownloadCancelled, DownloadManager
from eo_agent.imagery.events import EventBroker
from eo_agent.imagery.planner import ImageryPlanner
from eo_agent.imagery.planning import DownloadPlanBuilder
from eo_agent.imagery.providers import EarthEngineProvider, MockImageryProvider
from eo_agent.imagery.providers.base import ImageryProviderError
from eo_agent.imagery.quality import QualityAreaInputs, compute_quality_summary
from eo_agent.imagery.repository import (
    ApprovalStateError,
    ImageryRepository,
    StalePlanError,
)
from eo_agent.imagery.schemas import (
    AOIRef,
    AOISource,
    ApprovalRecord,
    DataKind,
    DataProviderKind,
    DownloadApprovalRequest,
    DownloadPlan,
    DownloadPlanUpdateRequest,
    ImageryTaskRequest,
    ImageryTaskStatus,
    ParsedRequest,
    QualityStatus,
    QualitySummary,
    SarRecommendationStatus,
    SceneCandidate,
    SceneRecommendation,
    SearchPlan,
)
from eo_agent.llm.factory import create_llm


class ImageryTaskNotFound(KeyError):
    pass


class ImageryConflict(RuntimeError):
    pass


class ImageryTaskService:
    """Bounded local orchestrator for interactive imagery preparation."""

    def __init__(
        self,
        output_dir: str | Path,
        *,
        project_root: Path = PROJECT_ROOT,
        config: ImageryConfig | None = None,
        max_workers: int = 2,
    ) -> None:
        self.output_dir = Path(output_dir).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.project_root = project_root.resolve()
        self.control_root = self.output_dir / "imagery"
        self.control_root.mkdir(parents=True, exist_ok=True)
        self.config = config or load_imagery_config(self.project_root)
        self.repository = ImageryRepository(self.output_dir / "imagery.sqlite3")
        self.events = EventBroker(self.repository)
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="imagery")
        self._futures: dict[str, Future[Any]] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._planners: dict[str, ImageryPlanner] = {}
        self._lock = threading.Lock()
        interrupted = self.repository.mark_running_interrupted()
        self.interrupted_on_startup = interrupted

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=False)

    def create_task(self, request: ImageryTaskRequest) -> dict[str, Any]:
        # Validate profiles before accepting a background job. This never contacts a provider.
        load_settings(request.model_profile, self.project_root)
        task_id = f"IMG-{uuid4().hex[:20]}"
        control_dir = self.control_root / task_id
        control_dir.mkdir(parents=True, exist_ok=False)
        self.repository.create_task(
            task_id=task_id,
            status=ImageryTaskStatus.CREATED.value,
            query=request.query,
            model_profile=request.model_profile,
            data_backend=request.provider.value,
            request=request.model_dump(mode="json"),
            control_dir=control_dir,
        )
        self.events.emit(
            task_id,
            event_type="task.created",
            stage=ImageryTaskStatus.CREATED.value,
            summary="影像准备任务已创建，后台开始解析；尚未请求正式下载。",
            details={
                "model_profile": request.model_profile,
                "provider": request.provider.value,
                "contains_mock": request.model_profile == "mock"
                or request.provider == DataProviderKind.MOCK,
            },
        )
        self._write_task_snapshot(task_id)
        self._submit(task_id, self._prepare, task_id, 1)
        return {"task_id": task_id, "status": ImageryTaskStatus.CREATED.value}

    def get_task(self, task_id: str) -> dict[str, Any]:
        task = self._task(task_id)
        return {
            **task,
            "parsed_request": task.get("parsed"),
            "provider": task["data_backend"],
            "aoi": task.get("aoi"),
            "download_files": self.repository.list_files(task_id),
            "llm_calls": self.repository.list_llm_calls(task_id),
            "events": self.repository.events_after(task_id, 0),
            "artifacts": self.repository.list_artifacts(task_id),
        }

    def get_candidates(self, task_id: str) -> dict[str, Any]:
        task = self._task(task_id)
        value = task.get("candidates")
        if value is None:
            raise FileNotFoundError("候选尚未生成")
        return value

    def get_plan(self, task_id: str) -> dict[str, Any]:
        task = self._task(task_id)
        if task.get("plan") is None:
            raise FileNotFoundError("下载计划尚未生成")
        value = dict(task["plan"])
        value["download_files"] = self.repository.list_files(task_id)
        return value

    def stream_events(self, task_id: str, after_sequence: int = 0):
        self._task(task_id)
        return self.events.stream(task_id, after_sequence)

    def artifact_path(self, task_id: str, artifact_id: str) -> tuple[Path, str]:
        self._task(task_id)
        return self._artifacts(task_id).resolve_artifact(artifact_id)

    def update_request(
        self, task_id: str, request: ImageryTaskRequest, task_version: int
    ) -> dict[str, Any]:
        task = self._task(task_id)
        if task_version != task["task_version"]:
            raise ImageryConflict("任务版本已变化，请刷新后重试")
        if task["status"] not in {
            ImageryTaskStatus.WAITING_INPUT.value,
            ImageryTaskStatus.WAITING_DOWNLOAD_APPROVAL.value,
            ImageryTaskStatus.FAILED.value,
        }:
            raise ImageryConflict(f"当前状态不允许修改请求: {task['status']}")
        if task["download_started"]:
            raise ImageryConflict("正式下载已开始，不能直接改写原请求")
        load_settings(request.model_profile, self.project_root)
        next_version = task_version + 1
        self.repository.update_task(
            task_id,
            status=ImageryTaskStatus.CREATED.value,
            task_version=next_version,
            plan_version=0,
            plan_hash=None,
            request_json=request.model_dump(mode="json"),
            query=request.query,
            model_profile=request.model_profile,
            data_backend=request.provider.value,
            parsed_json=None,
            aoi_json=None,
            candidates_json=None,
            plan_json=None,
            error=None,
            cancel_requested=0,
        )
        self.events.emit(
            task_id,
            event_type="state.changed",
            stage=ImageryTaskStatus.CREATED.value,
            summary=f"用户条件已更新到任务版本 {next_version}；旧计划不再有效。",
            details={"task_version": next_version},
        )
        self._submit(task_id, self._prepare, task_id, next_version)
        return self.get_task(task_id)

    def update_plan(
        self, task_id: str, update: DownloadPlanUpdateRequest
    ) -> DownloadPlan:
        task = self._task(task_id)
        if task["status"] != ImageryTaskStatus.WAITING_DOWNLOAD_APPROVAL.value:
            raise ImageryConflict(f"当前状态不允许修改计划: {task['status']}")
        previous = DownloadPlan.model_validate(task["plan"])
        if previous.version != update.plan_version or previous.plan_hash != update.plan_hash:
            raise ImageryConflict("计划版本或哈希已过期，请刷新后重试")
        unknown_files = set(update.selected_file_ids) - {item.file_id for item in previous.files}
        if unknown_files:
            raise ValueError("文件选择包含上一版本计划不存在的 ID")
        candidates_value = task.get("candidates") or {}
        candidates = [
            SceneCandidate.model_validate(item)
            for item in (
                candidates_value.get("optical_candidates", [])
                + candidates_value.get("sar_candidates", [])
            )
        ]
        known = {item.candidate_id for item in candidates}
        unknown = set(update.selected_candidate_ids) - known
        if unknown:
            raise ValueError(f"计划包含不存在的候选 ID: {', '.join(sorted(unknown))}")
        selected_models = [
            item for item in candidates if item.candidate_id in update.selected_candidate_ids
        ]
        if not any(item.data_kind == DataKind.OPTICAL for item in selected_models):
            raise ValueError("下载计划至少保留一个光学候选")
        recommendation = SceneRecommendation.model_validate(candidates_value["recommendation"])
        recommendation = recommendation.model_copy(
            update={
                "selected_optical_candidate_ids": [
                    item.candidate_id
                    for item in selected_models
                    if item.data_kind == DataKind.OPTICAL
                ],
                "selected_sar_candidate_ids": [
                    item.candidate_id for item in selected_models if item.data_kind == DataKind.SAR
                ],
                "accepted_by_program": True,
                "validation_errors": [],
            }
        )
        parsed = ParsedRequest.model_validate(task["parsed"])
        aoi = AOIRef.model_validate(task["aoi"])
        builder = self._plan_builder()
        plan = builder.build(
            task_id=task_id,
            version=previous.version + 1,
            aoi=aoi,
            periods=parsed.periods,
            candidates=candidates,
            recommendation=recommendation,
            provider=DataProviderKind(task["data_backend"]),
            download_root=update.download_root,
            selected_file_ids=update.selected_file_ids,
            optical_bands=update.optical_bands,
            grid_meters=update.grid_meters,
        )
        self._persist_plan(task_id, plan)
        self.events.emit(
            task_id,
            event_type="plan.ready",
            stage=ImageryTaskStatus.WAITING_DOWNLOAD_APPROVAL.value,
            summary=f"用户修改已生成计划版本 {plan.version}；必须重新确认。",
            plan_version=plan.version,
            details={"plan_hash": plan.plan_hash, "resolved_task_dir": plan.resolved_task_dir},
        )
        return plan

    def approve(self, task_id: str, request: DownloadApprovalRequest) -> dict[str, Any]:
        task = self._task(task_id)
        plan = DownloadPlan.model_validate(task["plan"])
        plan.assert_hash_matches()
        approval = ApprovalRecord(
            approval_id=f"APR-{uuid4().hex[:20]}",
            task_id=task_id,
            plan_version=plan.version,
            plan_hash=plan.plan_hash,
            idempotency_key=request.idempotency_key,
            selected_candidate_ids=plan.selected_candidate_ids,
            selected_file_ids=[item.file_id for item in plan.files],
            bands=plan.bands,
            grid=plan.grid,
            output_root=plan.output_root,
            resolved_task_dir=plan.resolved_task_dir,
            approved_at=datetime.now(UTC),
        )
        try:
            stored, created = self.repository.create_approval_once(
                approval_id=approval.approval_id,
                task_id=task_id,
                plan_version=request.plan_version,
                plan_hash=request.plan_hash,
                idempotency_key=request.idempotency_key,
                approval=approval.model_dump(mode="json"),
            )
        except (StalePlanError, ApprovalStateError) as exc:
            raise ImageryConflict(str(exc)) from exc
        if not created:
            return {"approval": stored["approval"], "created": False, "idempotent": True}
        self._artifacts(task_id).write_json(
            f"approvals/{approval.approval_id}.json",
            approval.model_dump(mode="json"),
            contains_mock=plan.contains_mock,
        )
        for item in plan.files:
            existing = self.repository.get_file(task_id, item.file_id)
            if not (
                existing
                and existing["status"] == "completed"
                and existing["relative_path"] == item.relative_path
                and existing.get("sha256")
            ):
                self.repository.upsert_file(
                    task_id,
                    item.file_id,
                    status=item.status.value,
                    relative_path=item.relative_path,
                )
        self.events.emit(
            task_id,
            event_type="approval.received",
            stage=ImageryTaskStatus.DOWNLOADING.value,
            summary=f"用户已确认计划版本 {plan.version}；正式下载现已获准。",
            plan_version=plan.version,
            details={"approval_id": approval.approval_id, "plan_hash": plan.plan_hash},
        )
        resume_existing = bool(self.repository.list_files(task_id)) and Path(
            plan.resolved_task_dir
        ).is_dir()
        self._submit(task_id, self._download, task_id, approval, resume_existing)
        return {"approval": approval.model_dump(mode="json"), "created": True}

    def cancel(self, task_id: str) -> dict[str, Any]:
        task = self._task(task_id)
        if task["status"] in {
            ImageryTaskStatus.COMPLETED.value,
            ImageryTaskStatus.FAILED.value,
            ImageryTaskStatus.CANCELLED.value,
        }:
            return {"task_id": task_id, "status": task["status"], "already_terminal": True}
        self.repository.update_task(task_id, cancel_requested=1)
        with self._lock:
            self._cancel_events.setdefault(task_id, threading.Event()).set()
        if task["status"] in {
            ImageryTaskStatus.WAITING_INPUT.value,
            ImageryTaskStatus.WAITING_DOWNLOAD_APPROVAL.value,
            ImageryTaskStatus.CREATED.value,
            ImageryTaskStatus.PARTIAL.value,
            ImageryTaskStatus.INTERRUPTED.value,
        }:
            self.repository.update_task(task_id, status=ImageryTaskStatus.CANCELLED.value)
            self.events.emit(
                task_id,
                event_type="task.cancelled",
                stage=ImageryTaskStatus.CANCELLED.value,
                summary="用户取消任务；未开始新的正式下载。",
                details={},
            )
        return {"task_id": task_id, "status": self._task(task_id)["status"]}

    def resume(self, task_id: str) -> dict[str, Any]:
        task = self._task(task_id)
        if task["status"] not in {
            ImageryTaskStatus.INTERRUPTED.value,
            ImageryTaskStatus.PARTIAL.value,
        }:
            raise ImageryConflict(f"当前状态不允许恢复: {task['status']}")
        stored = self.repository.latest_approval(task_id)
        if stored is None:
            raise ImageryConflict("没有可复用的原审批记录")
        approval = ApprovalRecord.model_validate(stored["approval"])
        plan = DownloadPlan.model_validate(task["plan"])
        if approval.plan_hash != plan.plan_hash or approval.plan_version != plan.version:
            raise ImageryConflict("原审批与当前计划不匹配，必须重新确认")
        self.repository.update_task(
            task_id,
            status=ImageryTaskStatus.DOWNLOADING.value,
            cancel_requested=0,
            error=None,
        )
        self.events.emit(
            task_id,
            event_type="state.changed",
            stage=ImageryTaskStatus.DOWNLOADING.value,
            summary="用户显式恢复；已完成文件将按哈希核对后跳过。",
            plan_version=plan.version,
            details={"resume": True},
        )
        self._submit(task_id, self._download, task_id, approval, True)
        return {"task_id": task_id, "status": ImageryTaskStatus.DOWNLOADING.value}

    def doctor(self, provider: DataProviderKind, *, check_remote: bool = False) -> dict[str, Any]:
        return dict(self._provider(provider).doctor(check_remote=check_remote))

    def _submit(self, task_id: str, function, *args) -> None:
        with self._lock:
            current = self._futures.get(task_id)
            if current is not None and not current.done():
                raise ImageryConflict("该任务已有后台动作正在执行")
            event = self._cancel_events.setdefault(task_id, threading.Event())
            event.clear()
            future = self.executor.submit(function, *args)
            self._futures[task_id] = future

    def _prepare(self, task_id: str, task_version: int) -> None:
        try:
            task = self._task(task_id)
            if task["task_version"] != task_version or self._cancelled(task_id):
                return
            request = ImageryTaskRequest.model_validate(task["request"])
            settings = load_settings(request.model_profile, self.project_root)
            planner = ImageryPlanner(
                create_llm(settings),
                self.repository,
                self._artifacts(task_id),
                self.events,
                model_profile=request.model_profile,
                prompt_version=settings.profile.prompt_version,
                sar_recommend_clear_fraction=self.config.sar_recommend_clear_fraction,
            )
            with self._lock:
                self._planners[task_id] = planner
            self._transition(task_id, ImageryTaskStatus.PARSING, "模型开始拆解用户任务。")
            aoi_summary = {
                "aoi_provided": bool(request.aoi_geojson or request.aoi_id),
                "aoi_id": request.aoi_id,
                "aoi_name": request.aoi_name,
            }
            parsed = planner.parse_request(task_id, request, aoi_summary)
            self.repository.update_task(task_id, parsed_json=parsed.model_dump(mode="json"))
            self._artifacts(task_id).write_json(
                f"versions/{task_version}/parsed_request.json",
                parsed.model_dump(mode="json"),
                contains_mock=request.model_profile == "mock",
            )
            if parsed.missing_fields or parsed.needs_aoi:
                self._transition(
                    task_id,
                    ImageryTaskStatus.WAITING_INPUT,
                    "任务条件不完整，等待用户补充；程序未擅自补造范围或时间。",
                    details={"missing_fields": parsed.missing_fields},
                )
                self._write_task_snapshot(task_id)
                return
            try:
                aoi = self._resolve_aoi(request)
            except AOIValidationError as exc:
                self.repository.update_task(task_id, error=str(exc))
                self._transition(
                    task_id,
                    ImageryTaskStatus.WAITING_INPUT,
                    "研究区未通过校验，等待用户修正。",
                    details={"error": str(exc)},
                )
                self._write_task_snapshot(task_id)
                return
            self.repository.update_task(task_id, aoi_json=aoi.model_dump(mode="json"))
            if aoi.requires_area_adjustment:
                self._transition(
                    task_id,
                    ImageryTaskStatus.WAITING_INPUT,
                    "研究区超过普通模式建议上限，必须由用户调整，不会自动缩小。",
                    details={"area_km2": aoi.area_km2, "warnings": aoi.warnings},
                )
                self._write_task_snapshot(task_id)
                return
            provider = self._provider(request.provider)
            search_plan = SearchPlan(
                task_id=task_id,
                request_version=task_version,
                aoi=aoi,
                parsed_request=parsed,
                provider=request.provider,
                cloud_score_clear_threshold=self.config.cloud_score_clear_threshold,
                quality_scale_meters=self.config.quality_scale_meters,
                candidate_display_limit_per_period=self.config.candidate_display_limit_per_window,
                catalog_limit_per_period=self.config.catalog_limit_per_window,
                initial_quality_limit_per_period=self.config.initial_quality_limit_per_window,
                created_at=datetime.now(UTC),
                contains_mock=provider.is_mock,
            )
            self._artifacts(task_id).write_json(
                f"versions/{task_version}/search_plan.json",
                search_plan.model_dump(mode="json"),
                contains_mock=provider.is_mock,
            )
            candidates, pages = self._search_optical(task_id, search_plan, provider)
            candidates = self._create_previews(task_id, candidates, aoi, provider)
            quality = self._assess_quality(task_id, candidates, aoi, provider)
            recommendation = planner.recommend_optical(
                task_id, parsed.periods, candidates, quality
            )
            sar_candidates: list[SceneCandidate] = []
            should_search_sar = (
                recommendation.sar_recommendation.status
                == SarRecommendationStatus.RECOMMENDED
                or DataKind.SAR in parsed.requested_data
            )
            if should_search_sar:
                sar_candidates = self._search_sar(
                    task_id,
                    parsed,
                    aoi,
                    provider,
                    candidates,
                    recommendation,
                )
                sar_candidates = self._create_previews(
                    task_id, sar_candidates, aoi, provider, allow_quality_overlay=False
                )
                recommendation = planner.finalize_sar(
                    task_id, parsed.periods, recommendation, sar_candidates
                )
            candidate_state = {
                "optical_candidates": [item.model_dump(mode="json") for item in candidates],
                "sar_candidates": [item.model_dump(mode="json") for item in sar_candidates],
                "quality_summaries": [item.model_dump(mode="json") for item in quality.values()],
                "recommendation": recommendation.model_dump(mode="json"),
                "catalog_pages": pages,
                "subset_note": "展示的是有上限的候选子集，不代表目录全部数据。",
                "contains_mock": provider.is_mock,
            }
            self.repository.update_task(task_id, candidates_json=candidate_state)
            self._artifacts(task_id).write_json(
                f"versions/{task_version}/quality_summary.json",
                candidate_state,
                contains_mock=provider.is_mock,
                artifact_id=f"quality-v{task_version}",
            )
            self._artifacts(task_id).write_json(
                "quality_summary.json",
                candidate_state,
                contains_mock=provider.is_mock,
                artifact_id="quality-current",
            )
            self._transition(task_id, ImageryTaskStatus.PLANNING, "程序构造下载文件与共享网格。")
            all_candidates = candidates + sar_candidates
            plan = self._plan_builder().build(
                task_id=task_id,
                version=max(1, int(task.get("plan_version") or 0) + 1),
                aoi=aoi,
                periods=parsed.periods,
                candidates=all_candidates,
                recommendation=recommendation,
                provider=request.provider,
                download_root=str(self.config.resolved_download_root(self.project_root)),
            )
            self._persist_plan(task_id, plan)
            self.events.emit(
                task_id,
                event_type="plan.ready",
                stage=ImageryTaskStatus.WAITING_DOWNLOAD_APPROVAL.value,
                summary=f"下载计划版本 {plan.version} 已生成；尚未创建正式影像文件。",
                plan_version=plan.version,
                details={
                    "plan_hash": plan.plan_hash,
                    "resolved_task_dir": plan.resolved_task_dir,
                    "estimated_total_bytes": plan.estimated_total_bytes,
                },
            )
            self.events.emit(
                task_id,
                event_type="approval.required",
                stage=ImageryTaskStatus.WAITING_DOWNLOAD_APPROVAL.value,
                summary="等待用户确认影像、波段、共享网格和后端机器保存目录。",
                plan_version=plan.version,
                details={"directory_notice": "目录位于运行 Python 后端的机器"},
            )
            self._write_task_snapshot(task_id)
        except Exception as exc:
            self._fail_task(task_id, exc)

    def _search_optical(self, task_id: str, plan: SearchPlan, provider):
        self._transition(task_id, ImageryTaskStatus.SEARCHING_OPTICAL, "开始检索 Sentinel-2。")
        candidates: list[SceneCandidate] = []
        pages: list[dict[str, Any]] = []
        geometry = geometry_as_geojson(plan.aoi)
        for period in plan.parsed_request.periods:
            self._ensure_not_cancelled(task_id)
            action_id = f"ACT-{uuid4().hex[:12]}"
            parameters = {
                "dataset": plan.optical_dataset,
                "period_id": period.period_id,
                "start": period.start.isoformat(),
                "end": period.end.isoformat(),
                "limit": plan.candidate_display_limit_per_period,
                "aoi_id": plan.aoi.aoi_id,
            }
            self._tool_started(task_id, action_id, "search_optical", parameters)
            try:
                page = dict(
                    provider.search_optical(
                        aoi=geometry,
                        start=period.start.isoformat(),
                        end=period.end.isoformat(),
                        limit=plan.candidate_display_limit_per_period,
                        offset=0,
                        window_id=period.period_id,
                    )
                )
                items = [SceneCandidate.model_validate(item) for item in page.get("items", [])]
                candidates.extend(items)
                safe_page = {key: value for key, value in page.items() if key != "items"}
                safe_page["period_id"] = period.period_id
                pages.append(safe_page)
                self._tool_finished(
                    task_id,
                    action_id,
                    "search_optical",
                    parameters,
                    {"returned": len(items), "total_matches": page.get("total_matches")},
                )
            except Exception as exc:
                self._tool_failed(task_id, action_id, "search_optical", parameters, exc)
                raise
        if not candidates:
            raise RuntimeError("授权时间和研究区内没有返回 Sentinel-2 候选；未解释为没有变化")
        return candidates, pages

    def _create_previews(
        self,
        task_id: str,
        candidates: list[SceneCandidate],
        aoi: AOIRef,
        provider,
        *,
        allow_quality_overlay: bool = True,
    ) -> list[SceneCandidate]:
        self._transition(task_id, ImageryTaskStatus.PREVIEWING, "开始生成并缓存小型候选预览。")
        result: list[SceneCandidate] = []
        geometry = geometry_as_geojson(aoi)
        for candidate in candidates:
            self._ensure_not_cancelled(task_id)
            action_id = f"ACT-{uuid4().hex[:12]}"
            params = {
                "candidate_id": candidate.candidate_id,
                "aoi_id": aoi.aoi_id,
                "max_dimension": self.config.preview_max_dimension,
            }
            self._tool_started(task_id, action_id, "create_preview", params)
            try:
                if provider.is_mock:
                    rgb = provider.preview_bytes(candidate.model_dump(mode="json"), quality=False)
                    quality = provider.preview_bytes(
                        candidate.model_dump(mode="json"), quality=allow_quality_overlay
                    )
                else:
                    urls = provider.create_preview_urls(
                        scene=candidate.model_dump(mode="json"),
                        aoi=geometry,
                        max_dimension=self.config.preview_max_dimension,
                    )
                    rgb = self._fetch_preview(urls.rgb_url)
                    quality = self._fetch_preview(urls.quality_url)
                store = self._artifacts(task_id)
                rgb_id = store.write_bytes(
                    f"previews/{candidate.candidate_id}-rgb.png",
                    rgb,
                    "image/png",
                    provider.is_mock,
                )
                quality_id = store.write_bytes(
                    f"previews/{candidate.candidate_id}-quality.png",
                    quality,
                    "image/png",
                    provider.is_mock,
                )
                candidate = candidate.model_copy(
                    update={
                        "preview_artifact_id": rgb_id,
                        "quality_overlay_artifact_id": quality_id,
                    }
                )
                self._tool_finished(
                    task_id,
                    action_id,
                    "create_preview",
                    params,
                    {"rgb_artifact_id": rgb_id, "quality_artifact_id": quality_id},
                )
            except Exception as exc:
                self._tool_failed(task_id, action_id, "create_preview", params, exc)
                # Preview failure is visible but does not falsify catalogue/quality results.
            result.append(candidate)
        return result

    def _assess_quality(self, task_id: str, candidates, aoi, provider):
        self._transition(
            task_id, ImageryTaskStatus.ASSESSING_QUALITY, "按用户研究区计算光学质量。"
        )
        result: dict[str, QualitySummary] = {}
        geometry = geometry_as_geojson(aoi)
        for candidate in candidates[: self.config.initial_quality_limit_per_window * 4]:
            self._ensure_not_cancelled(task_id)
            action_id = f"ACT-{uuid4().hex[:12]}"
            params = {
                "candidate_id": candidate.candidate_id,
                "aoi_id": aoi.aoi_id,
                "cloud_score_threshold": self.config.cloud_score_clear_threshold,
                "scale_meters": self.config.quality_scale_meters,
            }
            self._tool_started(task_id, action_id, "assess_optical_quality", params)
            try:
                value = provider.assess_optical_quality(
                    scene=candidate.model_dump(mode="json"),
                    aoi=geometry,
                    cloud_score_threshold=self.config.cloud_score_clear_threshold,
                    scale_meters=self.config.quality_scale_meters,
                )
                summary = QualitySummary.model_validate(value)
                result[candidate.candidate_id] = summary
                self._tool_finished(
                    task_id,
                    action_id,
                    "assess_optical_quality",
                    params,
                    summary.model_dump(mode="json"),
                )
            except Exception as exc:
                summary = QualitySummary(
                    candidate_id=candidate.candidate_id,
                    status=QualityStatus.UNKNOWN,
                    method="provider_quality_unavailable",
                    scale_meters=self.config.quality_scale_meters,
                    reason=f"在线质量检查不可用: {type(exc).__name__}",
                    warnings=["质量未知不能解释为无云或没有变化"],
                    contains_mock=provider.is_mock,
                )
                result[candidate.candidate_id] = summary
                self._tool_failed(task_id, action_id, "assess_optical_quality", params, exc)
        return result

    def _search_sar(
        self, task_id, parsed, aoi, provider, optical_candidates, recommendation
    ) -> list[SceneCandidate]:
        self._transition(
            task_id,
            ImageryTaskStatus.SEARCHING_SAR,
            "在原授权窗口内检索 Sentinel-1。",
        )
        geometry = geometry_as_geojson(aoi)
        result: list[SceneCandidate] = []
        recommended = set(recommendation.sar_recommendation.recommended_period_ids)
        if not recommended:
            recommended = {period.period_id for period in parsed.periods}
        for period in parsed.periods:
            self._ensure_not_cancelled(task_id)
            if period.period_id not in recommended:
                continue
            targets = tuple(
                item.model_dump(mode="json")
                for item in optical_candidates
                if item.candidate_id in recommendation.selected_optical_candidate_ids
                and item.period_id == period.period_id
            )
            action_id = f"ACT-{uuid4().hex[:12]}"
            params = {
                "dataset": "COPERNICUS/S1_GRD",
                "period_id": period.period_id,
                "start": period.start.isoformat(),
                "end": period.end.isoformat(),
                "instrument_mode": "IW",
                "polarizations": ["VV", "VH"],
            }
            self._tool_started(task_id, action_id, "search_sar", params)
            try:
                page = provider.search_sar(
                    aoi=geometry,
                    start=period.start.isoformat(),
                    end=period.end.isoformat(),
                    limit=self.config.candidate_display_limit_per_window,
                    offset=0,
                    window_id=period.period_id,
                    target_optical=targets,
                )
                items = [SceneCandidate.model_validate(item) for item in page.get("items", [])]
                result.extend(items)
                self._tool_finished(
                    task_id,
                    action_id,
                    "search_sar",
                    params,
                    {"returned": len(items), "total_matches": page.get("total_matches")},
                )
            except Exception as exc:
                self._tool_failed(task_id, action_id, "search_sar", params, exc)
                raise
        return result

    def _download(self, task_id: str, approval: ApprovalRecord, resume: bool) -> None:
        try:
            task = self._task(task_id)
            plan = DownloadPlan.model_validate(task["plan"])
            aoi = AOIRef.model_validate(task["aoi"])
            candidate_state = task["candidates"]
            candidates = {
                item["candidate_id"]: item
                for item in (
                    candidate_state.get("optical_candidates", [])
                    + candidate_state.get("sar_candidates", [])
                )
            }
            manager = DownloadManager(self.repository, self.events.emit, self.config)
            manifest = manager.run(
                task_id=task_id,
                plan=plan,
                approval=approval,
                candidates=candidates,
                provider=self._provider(DataProviderKind(task["data_backend"])),
                cancel_event=self._cancel_events[task_id],
                control_dir=Path(task["control_dir"]),
                aoi_geometry=geometry_as_geojson(aoi),
                resume=resume,
            )
            store = self._artifacts(task_id)
            delivery_dir = Path(plan.resolved_task_dir)
            manifest_id = store.write_bytes(
                "delivery/download_manifest.json",
                (delivery_dir / "download_manifest.json").read_bytes(),
                "application/json",
                plan.contains_mock,
                artifact_id=f"manifest-v{plan.version}",
            )
            for item in plan.files:
                thumbnail = delivery_dir / "thumbnails" / f"{item.file_id}.png"
                if thumbnail.is_file():
                    store.write_bytes(
                        f"delivery/thumbnails/{item.file_id}.png",
                        thumbnail.read_bytes(),
                        "image/png",
                        plan.contains_mock,
                        artifact_id=f"local-thumbnail-{item.file_id}",
                    )
            if self._plan_local_quality_followup(
                task_id,
                plan,
                candidate_state,
                delivery_dir,
            ):
                self._write_task_snapshot(task_id)
                shutil.copyfile(
                    Path(self._task(task_id)["control_dir"]) / "task.json",
                    delivery_dir / "task.json",
                )
                return
            final = (
                ImageryTaskStatus.COMPLETED
                if manifest["failed_files"] == 0
                else ImageryTaskStatus.PARTIAL
            )
            self.repository.update_task(task_id, status=final.value, error=None)
            self.events.emit(
                task_id,
                event_type="task.completed",
                stage=final.value,
                summary=(
                    "全部批准文件已下载并通过校验。"
                    if final == ImageryTaskStatus.COMPLETED
                    else "部分文件失败；任务未被报告为全部成功。"
                ),
                plan_version=plan.version,
                details={**manifest, "manifest_artifact_id": manifest_id},
            )
            self._write_task_snapshot(task_id)
            shutil.copyfile(
                Path(self._task(task_id)["control_dir"]) / "task.json",
                delivery_dir / "task.json",
            )
        except DownloadCancelled as exc:
            self.repository.update_task(
                task_id, status=ImageryTaskStatus.CANCELLED.value, error=str(exc)
            )
            self.events.emit(
                task_id,
                event_type="task.cancelled",
                stage=ImageryTaskStatus.CANCELLED.value,
                summary=str(exc),
                details={},
            )
        except Exception as exc:
            self._fail_task(task_id, exc)

    def _resolve_aoi(self, request: ImageryTaskRequest) -> AOIRef:
        if request.aoi_geojson is not None:
            return normalize_geojson(
                request.aoi_geojson,
                name=request.aoi_name or "用户上传研究区",
                source=AOISource.PASTED,
            )
        if request.aoi_id is None:
            raise AOIValidationError("缺少研究区几何")
        settings = load_settings(request.model_profile, self.project_root)
        record = settings.aois.get(request.aoi_id)
        if record is None:
            raise AOIValidationError(f"未知已登记研究区: {request.aoi_id}")
        geometry = record.get("geometry")
        if geometry is None:
            raise AOIValidationError("该登记项没有真实几何，不能用于影像检索")
        source = (
            AOISource.SYNTHETIC_MOCK if record.get("is_synthetic") else AOISource.REGISTERED
        )
        return normalize_geojson(
            geometry,
            aoi_id=request.aoi_id,
            name=request.aoi_name or record.get("name") or request.aoi_id,
            source=source,
        )

    def _plan_local_quality_followup(
        self,
        task_id: str,
        previous_plan: DownloadPlan,
        candidate_state: dict[str, Any],
        delivery_dir: Path,
    ) -> bool:
        quality_values = {
            item["candidate_id"]: item for item in candidate_state.get("quality_summaries", [])
        }
        unresolved = {
            identifier
            for identifier, value in quality_values.items()
            if value.get("status") in {QualityStatus.UNKNOWN.value, QualityStatus.FAILED.value}
            and identifier in previous_plan.selected_candidate_ids
        }
        if not unresolved or candidate_state.get("sar_candidates"):
            return False
        local_quality: dict[str, QualitySummary] = {}
        for identifier in sorted(unresolved):
            quality_file = next(
                (
                    item
                    for item in previous_plan.files
                    if item.candidate_id == identifier and item.data_kind == DataKind.QUALITY
                ),
                None,
            )
            if quality_file is None:
                continue
            path = delivery_dir / quality_file.relative_path
            try:
                local_quality[identifier] = self._local_scl_quality(
                    task_id, identifier, path
                )
            except Exception as exc:
                self.events.emit(
                    task_id,
                    event_type="tool.failed",
                    stage=ImageryTaskStatus.VERIFYING.value,
                    summary=f"本地 SCL 质量复核失败：{identifier}",
                    file_id=quality_file.file_id,
                    details={"error_type": type(exc).__name__, "message": str(exc)},
                )
        if not local_quality:
            return False
        quality_values.update(
            {key: value.model_dump(mode="json") for key, value in local_quality.items()}
        )
        candidate_state["quality_summaries"] = list(quality_values.values())
        self._artifacts(task_id).write_json(
            "quality_summary.json",
            candidate_state,
            contains_mock=previous_plan.contains_mock,
            artifact_id="quality-current",
        )
        optical_by_id = {
            item["candidate_id"]: SceneCandidate.model_validate(item)
            for item in candidate_state.get("optical_candidates", [])
        }
        downloaded_optical = [
            optical_by_id[item]
            for item in previous_plan.selected_candidate_ids
            if item in optical_by_id
        ]
        parsed = ParsedRequest.model_validate(self._task(task_id)["parsed"])
        with self._lock:
            planner = self._planners.get(task_id)
        if planner is None:
            request = ImageryTaskRequest.model_validate(self._task(task_id)["request"])
            settings = load_settings(request.model_profile, self.project_root)
            planner = ImageryPlanner(
                create_llm(settings),
                self.repository,
                self._artifacts(task_id),
                self.events,
                model_profile=request.model_profile,
                prompt_version=settings.profile.prompt_version,
                sar_recommend_clear_fraction=self.config.sar_recommend_clear_fraction,
            )
        recommendation = planner.recommend_optical(
            task_id, parsed.periods, downloaded_optical, local_quality
        )
        if (
            recommendation.sar_recommendation.status
            != SarRecommendationStatus.RECOMMENDED
        ):
            candidate_state["recommendation"] = recommendation.model_dump(mode="json")
            self.repository.update_task(task_id, candidates_json=candidate_state)
            return False
        task = self._task(task_id)
        aoi = AOIRef.model_validate(task["aoi"])
        provider = self._provider(DataProviderKind(task["data_backend"]))
        sar_candidates = self._search_sar(
            task_id,
            parsed,
            aoi,
            provider,
            downloaded_optical,
            recommendation,
        )
        sar_candidates = self._create_previews(
            task_id,
            sar_candidates,
            aoi,
            provider,
            allow_quality_overlay=False,
        )
        recommendation = planner.finalize_sar(
            task_id, parsed.periods, recommendation, sar_candidates
        )
        candidate_state["sar_candidates"] = [
            item.model_dump(mode="json") for item in sar_candidates
        ]
        candidate_state["recommendation"] = recommendation.model_dump(mode="json")
        self.repository.update_task(task_id, candidates_json=candidate_state)
        all_candidates = [
            SceneCandidate.model_validate(item)
            for item in candidate_state["optical_candidates"]
        ] + sar_candidates
        followup = self._plan_builder().build(
            task_id=task_id,
            version=previous_plan.version + 1,
            aoi=aoi,
            periods=parsed.periods,
            candidates=all_candidates,
            recommendation=recommendation,
            provider=previous_plan.provider,
            download_root=previous_plan.output_root,
            optical_bands=next(
                (
                    bands
                    for candidate_id, bands in previous_plan.bands.items()
                    if candidate_id in recommendation.selected_optical_candidate_ids
                ),
                None,
            ),
            grid_meters=previous_plan.grid.scale_meters,
        )
        self._persist_plan(task_id, followup)
        self.events.emit(
            task_id,
            event_type="plan.ready",
            stage=ImageryTaskStatus.WAITING_DOWNLOAD_APPROVAL.value,
            summary=(
                "本地 SCL 复核后建议补充 SAR，已生成新计划；既有文件保留，新增内容必须二次确认。"
            ),
            plan_version=followup.version,
            details={
                "plan_hash": followup.plan_hash,
                "requires_second_approval": True,
                "local_quality_method": "SCL-only",
            },
        )
        self.events.emit(
            task_id,
            event_type="approval.required",
            stage=ImageryTaskStatus.WAITING_DOWNLOAD_APPROVAL.value,
            summary="新增 SAR 未获原审批授权，等待用户第二次确认。",
            plan_version=followup.version,
            details={"preserved_existing_files": True},
        )
        return True

    def _local_scl_quality(
        self, task_id: str, candidate_id: str, path: Path
    ) -> QualitySummary:
        try:
            import numpy as np
            import rasterio
        except ImportError as exc:
            raise RuntimeError("本地 SCL 质量复核需要 imagery extra") from exc
        with rasterio.open(path) as dataset:
            values = dataset.read(1, masked=True)
            pixel_area = abs(float(dataset.transform.a * dataset.transform.e))
        raw = np.ma.filled(values, 0)
        total = raw.size * pixel_area
        valid_mask = (~np.ma.getmaskarray(values)) & (~np.isin(raw, [0, 1]))
        clear_mask = valid_mask & np.isin(raw, [2, 4, 5, 6, 7])
        shadow_mask = valid_mask & (raw == 3)
        cloud_mask = valid_mask & np.isin(raw, [8, 9, 10])
        snow_mask = valid_mask & (raw == 11)
        valid = int(valid_mask.sum()) * pixel_area
        return compute_quality_summary(
            candidate_id,
            QualityAreaInputs(
                aoi_area_m2=total,
                valid_area_m2=valid,
                clear_area_m2=int(clear_mask.sum()) * pixel_area,
                quality_assessed_area_m2=valid,
                cloud_area_m2=int(cloud_mask.sum()) * pixel_area,
                shadow_area_m2=int(shadow_mask.sum()) * pixel_area,
                cloud_shadow_area_m2=(
                    int(cloud_mask.sum()) + int(shadow_mask.sum())
                )
                * pixel_area,
                snow_area_m2=int(snow_mask.sum()) * pixel_area,
                nodata_area_m2=total - valid,
                saturated_area_m2=0,
                method="local_scl_area_after_approved_download",
                scale_meters=int(round(math.sqrt(pixel_area))),
                cloud_score_match_count=0,
                warnings=["本地复核仅使用已下载 SCL；未离线运行 Cloud Score+ 模型"],
                contains_mock=self._task(task_id)["data_backend"] == "mock",
            ),
            min_clear_aoi_fraction=self.config.sar_recommend_clear_fraction,
        )

    def _provider(self, kind: DataProviderKind):
        if kind == DataProviderKind.MOCK:
            return MockImageryProvider()
        if kind == DataProviderKind.EARTH_ENGINE:
            return EarthEngineProvider(
                project_id=os.getenv("EE_PROJECT_ID"),
                max_catalog_limit=self.config.catalog_limit_per_window,
                max_request_uncompressed_mib=self.config.max_request_uncompressed_mib,
            )
        raise ValueError(f"不支持的数据后端: {kind}")

    def _plan_builder(self) -> DownloadPlanBuilder:
        return DownloadPlanBuilder(
            project_root=self.project_root,
            max_request_uncompressed_mib=self.config.max_request_uncompressed_mib,
            default_grid_meters=self.config.grid_meters,
        )

    def _persist_plan(self, task_id: str, plan: DownloadPlan) -> None:
        plan.assert_hash_matches()
        self.repository.update_task(
            task_id,
            status=ImageryTaskStatus.WAITING_DOWNLOAD_APPROVAL.value,
            plan_version=plan.version,
            plan_hash=plan.plan_hash,
            plan_json=plan.model_dump(mode="json"),
            download_started=0,
        )
        self._artifacts(task_id).write_json(
            f"plans/plan-v{plan.version}.json",
            plan.model_dump(mode="json"),
            plan.contains_mock,
            artifact_id=f"plan-v{plan.version}",
        )

    def _transition(
        self,
        task_id: str,
        status: ImageryTaskStatus,
        summary: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.repository.update_task(task_id, status=status.value)
        self.events.emit(
            task_id,
            event_type="state.changed",
            stage=status.value,
            summary=summary,
            details=details or {},
        )

    def _tool_started(self, task_id, action_id, name, parameters) -> None:
        self.events.emit(
            task_id,
            event_type="tool.started",
            stage=self._task(task_id)["status"],
            summary=f"工具开始：{name}",
            action_id=action_id,
            details={"tool_name": name, "parameters": parameters},
        )

    def _tool_finished(self, task_id, action_id, name, parameters, result) -> None:
        self.events.emit(
            task_id,
            event_type="tool.finished",
            stage=self._task(task_id)["status"],
            summary=f"工具完成：{name}",
            action_id=action_id,
            details={"tool_name": name, "parameters": parameters, "result": result},
        )

    def _tool_failed(self, task_id, action_id, name, parameters, exc) -> None:
        detail = exc.to_dict() if isinstance(exc, ImageryProviderError) else {
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
        self.events.emit(
            task_id,
            event_type="tool.failed",
            stage=self._task(task_id)["status"],
            summary=f"工具失败：{name}",
            action_id=action_id,
            details={"tool_name": name, "parameters": parameters, "error": detail},
        )

    def _fetch_preview(self, url: str) -> bytes:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not (
            host == "googleapis.com" or host.endswith(".googleapis.com")
            or host == "googleusercontent.com"
            or host.endswith(".googleusercontent.com")
        ):
            raise ValueError("预览地址不是受信任的 provider HTTPS 域名")
        try:
            with httpx.Client(timeout=30, follow_redirects=True) as client:
                response = client.get(url)
                response.raise_for_status()
                final_host = (response.url.host or "").lower()
                if not (
                    final_host == "googleapis.com"
                    or final_host.endswith(".googleapis.com")
                    or final_host == "googleusercontent.com"
                    or final_host.endswith(".googleusercontent.com")
                ):
                    raise ValueError("预览重定向离开受信任 provider 域名")
                content_type = response.headers.get("content-type", "").lower()
                if not content_type.startswith("image/"):
                    raise ValueError("预览端点未返回图像")
                if len(response.content) > 10 * 1024 * 1024:
                    raise ValueError("预览超过 10 MiB 安全上限")
                return response.content
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"预览请求失败: HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"预览网络失败: {type(exc).__name__}") from exc

    def _artifacts(self, task_id: str) -> ImageryArtifactStore:
        return ImageryArtifactStore(self.control_root, task_id, self.repository)

    def _task(self, task_id: str) -> dict[str, Any]:
        task = self.repository.get_task(task_id)
        if task is None:
            raise ImageryTaskNotFound(task_id)
        return task

    def _cancelled(self, task_id: str) -> bool:
        task = self.repository.get_task(task_id)
        with self._lock:
            event = self._cancel_events.get(task_id)
        return bool(task and task["cancel_requested"]) or bool(event and event.is_set())

    def _ensure_not_cancelled(self, task_id: str) -> None:
        if self._cancelled(task_id):
            raise DownloadCancelled("用户已取消；不会启动后续检索或质量请求")

    def _fail_task(self, task_id: str, exc: Exception) -> None:
        if self._cancelled(task_id):
            self.repository.update_task(
                task_id, status=ImageryTaskStatus.CANCELLED.value, error=str(exc)
            )
            event_type = "task.cancelled"
            summary = "任务按用户请求停止。"
            stage = ImageryTaskStatus.CANCELLED.value
        else:
            safe = (
                exc.to_dict()["message"] if isinstance(exc, ImageryProviderError) else str(exc)
            )
            self.repository.update_task(
                task_id, status=ImageryTaskStatus.FAILED.value, error=safe
            )
            event_type = "task.failed"
            summary = f"影像准备失败：{safe}"
            stage = ImageryTaskStatus.FAILED.value
        self.events.emit(
            task_id,
            event_type=event_type,
            stage=stage,
            summary=summary,
            details={"error_type": type(exc).__name__},
        )
        self._write_task_snapshot(task_id)

    def _write_task_snapshot(self, task_id: str) -> None:
        task = self.repository.get_task(task_id)
        if task is None:
            return
        contains_mock = task["model_profile"] == "mock" or task["data_backend"] == "mock"
        self._artifacts(task_id).write_json(
            "task.json", task, contains_mock, artifact_id="task-snapshot"
        )


def task_request_from_patch(value: Mapping[str, Any]) -> tuple[ImageryTaskRequest, int]:
    payload = dict(value)
    try:
        version = int(payload.pop("task_version"))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("更新请求必须包含 task_version") from exc
    payload.pop("continue_search", None)
    return ImageryTaskRequest.model_validate(payload), version
