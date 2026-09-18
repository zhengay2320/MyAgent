from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import urlparse

import httpx

from eo_agent.imagery.config import ImageryConfig
from eo_agent.imagery.providers.base import BaseImageryProvider, ProviderDownloadRequest
from eo_agent.imagery.repository import ImageryRepository
from eo_agent.imagery.schemas import ApprovalRecord, DownloadFile, DownloadPlan, FileVerification

EventEmitter = Callable[..., dict[str, Any]]


class DownloadCancelled(RuntimeError):
    pass


class DownloadValidationError(RuntimeError):
    pass


class DownloadManager:
    def __init__(
        self,
        repository: ImageryRepository,
        emitter: EventEmitter,
        config: ImageryConfig,
        *,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.repository = repository
        self.emit = emitter
        self.config = config
        self.http_client = http_client or httpx.Client(timeout=60, follow_redirects=True)

    def run(
        self,
        *,
        task_id: str,
        plan: DownloadPlan,
        approval: ApprovalRecord,
        candidates: Mapping[str, Mapping[str, Any]],
        provider: BaseImageryProvider,
        cancel_event: threading.Event,
        control_dir: Path,
        aoi_geometry: Mapping[str, Any],
        resume: bool = False,
    ) -> dict[str, Any]:
        plan.assert_hash_matches()
        if approval.plan_hash != plan.plan_hash or approval.plan_version != plan.version:
            raise DownloadValidationError("审批与当前计划不匹配")
        task_dir = Path(plan.resolved_task_dir).resolve()
        expected = (Path(plan.output_root).resolve() / task_id).resolve()
        if task_dir != expected:
            raise DownloadValidationError("任务目录必须是已批准根目录下的独立 task_id 子目录")
        if resume:
            task_dir.mkdir(parents=True, exist_ok=True)
        else:
            task_dir.mkdir(parents=True, exist_ok=False)
        free = shutil.disk_usage(task_dir.parent).free
        if plan.estimated_total_bytes is not None and free < plan.estimated_total_bytes:
            raise DownloadValidationError("目标磁盘剩余空间低于计划估算")

        self._write_delivery_metadata(
            task_dir,
            plan,
            approval,
            control_dir,
            before=True,
            candidates=candidates,
        )
        completed = 0
        failed = 0
        for item in plan.files:
            if cancel_event.is_set() or self._cancel_requested(task_id):
                self._mark_remaining_cancelled(task_id, plan.files, item.file_id)
                raise DownloadCancelled("用户已取消；不会启动后续文件")
            scene = candidates.get(item.candidate_id)
            if scene is None:
                self._fail_file(task_id, item, "计划引用的候选不存在")
                failed += 1
                continue
            existing = self.repository.get_file(task_id, item.file_id)
            destination = (task_dir / item.relative_path).resolve()
            if resume and existing and existing["status"] == "completed" and destination.is_file():
                if existing.get("sha256") == _sha256(destination):
                    completed += 1
                    self.emit(
                        task_id,
                        event_type="download.finished",
                        stage="DOWNLOADING",
                        summary=f"已校验完成文件，恢复时跳过：{item.file_id}",
                        file_id=item.file_id,
                        plan_version=plan.version,
                        details={"resumed": True, "sha256": existing["sha256"]},
                    )
                    continue
            try:
                self._download_one(
                    task_id=task_id,
                    item=item,
                    plan=plan,
                    approval=approval,
                    scene=scene,
                    provider=provider,
                    task_dir=task_dir,
                    cancel_event=cancel_event,
                    aoi_geometry=aoi_geometry,
                    allow_existing_partial=resume,
                )
                completed += 1
            except DownloadCancelled:
                self._mark_remaining_cancelled(task_id, plan.files, item.file_id)
                raise
            except Exception as exc:
                self._fail_file(task_id, item, f"{type(exc).__name__}: {exc}")
                failed += 1
        manifest = {
            "task_id": task_id,
            "plan_version": plan.version,
            "plan_hash": plan.plan_hash,
            "provider": plan.provider.value,
            "contains_mock": plan.contains_mock,
            "completed_files": completed,
            "failed_files": failed,
            "files": self.repository.list_files(task_id),
        }
        _atomic_json(task_dir / "download_manifest.json", manifest)
        self._write_delivery_metadata(
            task_dir,
            plan,
            approval,
            control_dir,
            before=False,
            candidates=candidates,
        )
        return manifest

    def _download_one(
        self,
        *,
        task_id: str,
        item: DownloadFile,
        plan: DownloadPlan,
        approval: ApprovalRecord,
        scene: Mapping[str, Any],
        provider: BaseImageryProvider,
        task_dir: Path,
        cancel_event: threading.Event,
        aoi_geometry: Mapping[str, Any],
        allow_existing_partial: bool,
    ) -> None:
        destination = (task_dir / item.relative_path).resolve()
        if task_dir not in destination.parents:
            raise DownloadValidationError("文件路径越出已审批任务目录")
        if destination.exists():
            raise FileExistsError(f"拒绝覆盖已有文件: {destination.name}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        part = destination.with_name(destination.name + ".part")
        if part.exists() and allow_existing_partial:
            part.unlink()
        elif part.exists():
            part.unlink()
        self.repository.upsert_file(
            task_id,
            item.file_id,
            status="preparing_remote",
            relative_path=item.relative_path,
        )
        self.emit(
            task_id,
            event_type="download.preparing",
            stage="DOWNLOADING",
            summary=f"正在生成下载请求：{item.file_id}",
            file_id=item.file_id,
            plan_version=plan.version,
            details={"percent": None, "provider": plan.provider.value},
        )
        tile = {**item.model_dump(mode="json"), "region": dict(aoi_geometry)}
        request = provider.create_download_request(
            scene=scene,
            tile=tile,
            approval=approval.model_dump(mode="json"),
        )
        self.emit(
            task_id,
            event_type="download.started",
            stage="DOWNLOADING",
            summary=f"开始接收文件：{item.file_id}",
            file_id=item.file_id,
            plan_version=plan.version,
            details=request.to_log_dict(),
        )
        attempt = 0
        while True:
            attempt += 1
            try:
                total, chunks = self._stream(provider, request)
                received = self._write_stream(
                    task_id,
                    item,
                    chunks,
                    part,
                    total,
                    plan.version,
                    cancel_event,
                )
                break
            except DownloadCancelled:
                if part.exists():
                    part.unlink()
                raise
            except (httpx.HTTPError, OSError) as exc:
                if part.exists():
                    part.unlink()
                if attempt >= self.config.max_download_retries + 1:
                    raise DownloadValidationError(
                        f"文件传输在有限重试后失败: {type(exc).__name__}"
                    ) from exc
                self.emit(
                    task_id,
                    event_type="download.failed",
                    stage="DOWNLOADING",
                    summary=f"文件传输临时失败，准备有限重试：{item.file_id}",
                    file_id=item.file_id,
                    plan_version=plan.version,
                    details={"attempt": attempt, "error_type": type(exc).__name__},
                )
                request = provider.create_download_request(
                    scene=scene,
                    tile=tile,
                    approval=approval.model_dump(mode="json"),
                )
        self.repository.upsert_file(
            task_id,
            item.file_id,
            status="verifying",
            bytes_received=received,
            total_bytes=total,
        )
        try:
            validation = validate_geotiff(part, item)
        except Exception:
            if part.exists():
                part.unlink()
            raise
        checksum = validation["checksum_sha256"]
        os.replace(part, destination)
        thumbnail = task_dir / "thumbnails" / f"{item.file_id}.png"
        thumbnail_error = None
        try:
            create_local_thumbnail(destination, thumbnail, item)
        except Exception as exc:
            thumbnail_error = f"{type(exc).__name__}: {exc}"
            self.emit(
                task_id,
                event_type="tool.failed",
                stage="VERIFYING",
                summary=f"本地缩略图生成失败，但已校验 GeoTIFF 保持有效：{item.file_id}",
                file_id=item.file_id,
                plan_version=plan.version,
                details={"tool_name": "create_local_thumbnail", "error": thumbnail_error},
            )
        self.repository.upsert_file(
            task_id,
            item.file_id,
            status="completed",
            bytes_received=received,
            total_bytes=total,
            sha256=checksum,
            validation=validation,
            error=None,
        )
        self.emit(
            task_id,
            event_type="verification.finished",
            stage="VERIFYING",
            summary=f"GeoTIFF 校验通过并生成本地缩略图：{item.file_id}",
            file_id=item.file_id,
            plan_version=plan.version,
            details={
                "sha256": checksum,
                "validation": validation,
                "thumbnail": (
                    thumbnail.relative_to(task_dir).as_posix()
                    if thumbnail.is_file()
                    else None
                ),
                "thumbnail_error": thumbnail_error,
            },
        )
        self.emit(
            task_id,
            event_type="download.finished",
            stage="DOWNLOADING",
            summary=f"文件完成：{item.file_id}",
            file_id=item.file_id,
            plan_version=plan.version,
            details={"bytes_received": received, "sha256": checksum},
        )

    def _stream(
        self, provider: BaseImageryProvider, request: ProviderDownloadRequest
    ) -> tuple[int | None, Iterator[bytes]]:
        iterator = getattr(provider, "iter_download", None)
        if callable(iterator):
            return iterator(request, self.config.mock_download_chunk_bytes)
        if not request.url.startswith("https://"):
            raise DownloadValidationError("真实 provider 只能返回 HTTPS 下载地址")
        host = (urlparse(request.url).hostname or "").lower()
        trusted = (
            host == "googleapis.com"
            or host.endswith(".googleapis.com")
            or host == "googleusercontent.com"
            or host.endswith(".googleusercontent.com")
        )
        if not trusted:
            raise DownloadValidationError("下载地址不属于受信任的 Earth Engine HTTPS 域名")
        response_context = self.http_client.stream("GET", request.url)
        response = response_context.__enter__()
        try:
            response.raise_for_status()
            final_host = (response.url.host or "").lower()
            if not (
                final_host == "googleapis.com"
                or final_host.endswith(".googleapis.com")
                or final_host == "googleusercontent.com"
                or final_host.endswith(".googleusercontent.com")
            ):
                raise DownloadValidationError("下载重定向离开受信任 provider 域名")
            content_type = response.headers.get("content-type", "").lower()
            if "text/html" in content_type or "application/json" in content_type:
                raise DownloadValidationError("远端返回的不是 GeoTIFF，而是错误文档")
            length = response.headers.get("content-length")
            total = int(length) if length and length.isdigit() else None

            def chunks() -> Iterator[bytes]:
                try:
                    yield from response.iter_bytes(64 * 1024)
                finally:
                    response_context.__exit__(None, None, None)

            return total, chunks()
        except Exception:
            response_context.__exit__(None, None, None)
            raise

    def _write_stream(
        self,
        task_id: str,
        item: DownloadFile,
        chunks: Iterator[bytes],
        part: Path,
        total: int | None,
        plan_version: int,
        cancel_event: threading.Event,
    ) -> int:
        started = monotonic()
        last_emit = 0.0
        received = 0
        prefix = bytearray()
        with part.open("xb") as handle:
            for chunk in chunks:
                if cancel_event.is_set() or self._cancel_requested(task_id):
                    raise DownloadCancelled("用户取消下载")
                if not chunk:
                    continue
                if len(prefix) < 512:
                    prefix.extend(chunk[: 512 - len(prefix)])
                handle.write(chunk)
                received += len(chunk)
                now = monotonic()
                if now - last_emit >= 0.25 or (total is not None and received >= total):
                    elapsed = max(1e-6, now - started)
                    rate = received / elapsed
                    percent = received / total if total else None
                    self.repository.upsert_file(
                        task_id,
                        item.file_id,
                        status="downloading",
                        bytes_received=received,
                        total_bytes=total,
                        rate_bps=rate,
                    )
                    self.emit(
                        task_id,
                        event_type="download.progress",
                        stage="DOWNLOADING",
                        summary=f"正在接收：{item.file_id}",
                        file_id=item.file_id,
                        plan_version=plan_version,
                        details={
                            "bytes_received": received,
                            "total_bytes": total,
                            "rate_bps": rate,
                            "percent": percent,
                        },
                    )
                    last_emit = now
        if bytes(prefix).lstrip().lower().startswith((b"<!doctype html", b"<html", b"{")):
            raise DownloadValidationError("响应内容是 HTML/JSON 错误文档，不是 TIFF")
        if total is not None and received != total:
            raise DownloadValidationError("接收字节数与 Content-Length 不一致")
        return received

    def _fail_file(self, task_id: str, item: DownloadFile, error: str) -> None:
        self.repository.upsert_file(
            task_id,
            item.file_id,
            status="failed",
            relative_path=item.relative_path,
            error=error,
        )
        self.emit(
            task_id,
            event_type="download.failed",
            stage="DOWNLOADING",
            summary=f"文件失败：{item.file_id}",
            file_id=item.file_id,
            details={"error": error},
        )

    def _mark_remaining_cancelled(
        self, task_id: str, files: list[DownloadFile], current_file_id: str
    ) -> None:
        start = False
        for item in files:
            start = start or item.file_id == current_file_id
            if start:
                self.repository.upsert_file(
                    task_id,
                    item.file_id,
                    status="cancelled",
                    relative_path=item.relative_path,
                )

    def _cancel_requested(self, task_id: str) -> bool:
        task = self.repository.get_task(task_id)
        return bool(task and task["cancel_requested"])

    def _write_delivery_metadata(
        self,
        task_dir: Path,
        plan: DownloadPlan,
        approval: ApprovalRecord,
        control_dir: Path,
        *,
        before: bool,
        candidates: Mapping[str, Mapping[str, Any]],
    ) -> None:
        _atomic_json(task_dir / "selection_plan.json", plan.model_dump(mode="json"))
        _atomic_json(task_dir / "approval.json", approval.model_dump(mode="json"))
        _atomic_json(
            task_dir / "product_metadata.json",
            {
                "contains_mock": plan.contains_mock,
                "products": [
                    candidates[identifier]
                    for identifier in plan.selected_candidate_ids
                    if identifier in candidates
                ],
            },
        )
        task_snapshot = control_dir / "task.json"
        quality_snapshot = control_dir / "quality_summary.json"
        if task_snapshot.is_file():
            shutil.copyfile(task_snapshot, task_dir / "task.json")
        if quality_snapshot.is_file():
            shutil.copyfile(quality_snapshot, task_dir / "quality_summary.json")
        if not before:
            logs = task_dir / "logs"
            logs.mkdir(exist_ok=True)
            _atomic_json(logs / "llm_calls.json", self.repository.list_llm_calls(plan.task_id))
            _atomic_json(logs / "events.json", self.repository.events_after(plan.task_id, 0))
            for source in (control_dir / "llm_calls").glob("**/*.json"):
                relative = source.relative_to(control_dir / "llm_calls")
                destination = logs / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)


def validate_geotiff(path: Path, item: DownloadFile) -> dict[str, Any]:
    try:
        import numpy as np
        import rasterio
    except ImportError as exc:
        raise DownloadValidationError("GeoTIFF 校验需要 imagery extra") from exc
    try:
        with rasterio.open(path) as dataset:
            if dataset.driver != "GTiff":
                raise DownloadValidationError("文件驱动不是 GeoTIFF")
            if dataset.count != len(item.bands):
                raise DownloadValidationError("GeoTIFF 波段数与批准计划不一致")
            descriptions = list(dataset.descriptions)
            if descriptions != item.bands:
                raise DownloadValidationError("GeoTIFF 波段名称或顺序与批准计划不一致")
            if dataset.crs is None:
                raise DownloadValidationError("GeoTIFF 缺少 CRS")
            expected_dtype = "uint8" if item.data_kind.value == "quality" else "float32"
            if any(dtype != expected_dtype for dtype in dataset.dtypes):
                raise DownloadValidationError("GeoTIFF 数据类型与批准产品定义不一致")
            if item.grid is not None:
                if dataset.crs.to_string() != item.grid.crs:
                    raise DownloadValidationError("GeoTIFF CRS 与批准计划不一致")
                if item.grid.width and dataset.width != item.grid.width:
                    raise DownloadValidationError("GeoTIFF 宽度与批准计划不一致")
                if item.grid.height and dataset.height != item.grid.height:
                    raise DownloadValidationError("GeoTIFF 高度与批准计划不一致")
                if item.grid.transform:
                    actual = tuple(dataset.transform)[:6]
                    if any(
                        abs(a - b) > 1e-6
                        for a, b in zip(actual, item.grid.transform, strict=True)
                    ):
                        raise DownloadValidationError("GeoTIFF 仿射网格与批准计划不一致")
            data = dataset.read(masked=True)
            valid_count = int(np.ma.count(data))
            if valid_count == 0:
                raise DownloadValidationError("GeoTIFF 全部为 nodata")
            verification = FileVerification(
                passed=True,
                size_bytes=path.stat().st_size,
                checksum_sha256=_sha256(path),
                media_type_valid=True,
                geotiff_opened=True,
                extent_matches_plan=True,
                bands_match_plan=True,
                grid_matches_plan=True,
                all_nodata=False,
                warnings=[f"有效像元值数量：{valid_count}"],
            )
            return verification.model_dump(mode="json")
    except DownloadValidationError:
        raise
    except Exception as exc:
        raise DownloadValidationError(f"GeoTIFF 无法打开或已损坏: {type(exc).__name__}") from exc


def create_local_thumbnail(source: Path, destination: Path, item: DownloadFile) -> None:
    try:
        import numpy as np
        import rasterio
        from PIL import Image
    except ImportError as exc:
        raise DownloadValidationError("本地缩略图需要 imagery extra") from exc
    with rasterio.open(source) as dataset:
        data = dataset.read(masked=True)
    if item.data_kind.value == "quality":
        values = np.ma.filled(data[0], 0).astype(np.uint8)
        palette = np.array(
            [
                [0, 0, 0],
                [190, 190, 190],
                [60, 150, 70],
                [90, 130, 210],
                [220, 220, 230],
                [130, 100, 160],
                [245, 245, 255],
                [200, 80, 80],
                [235, 170, 75],
                [120, 90, 160],
                [110, 80, 150],
                [245, 245, 255],
            ],
            dtype=np.uint8,
        )
        rgb = palette[np.minimum(values, len(palette) - 1)]
    elif item.data_kind.value == "sar":
        value = np.ma.filled(data[0], -25.0).astype(np.float32)
        gray = np.clip((value + 25) / 20 * 255, 0, 255).astype(np.uint8)
        rgb = np.stack((gray, gray, gray), axis=-1)
    else:
        band_index = {name: index for index, name in enumerate(item.bands)}
        indices = [
            band_index.get(name, min(index, data.shape[0] - 1))
            for index, name in enumerate(("B4", "B3", "B2"))
        ]
        rgb_values = np.ma.filled(data[indices], 0.0).astype(np.float32)
        rgb = np.moveaxis(np.clip(rgb_values / 0.3 * 255, 0, 255).astype(np.uint8), 0, -1)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    Image.fromarray(rgb, "RGB").save(temporary, format="PNG")
    os.replace(temporary, destination)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    os.replace(temporary, path)
