from __future__ import annotations

import math
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from eo_agent.config import PROJECT_ROOT
from eo_agent.imagery.schemas import (
    AOIRef,
    DataKind,
    DataProviderKind,
    DownloadFile,
    DownloadPlan,
    GridSpec,
    SceneCandidate,
    SceneRecommendation,
    SearchPeriod,
)

OPTICAL_BAND_WHITELIST = {"B2", "B3", "B4", "B8", "B11", "B12"}
SAR_BAND_WHITELIST = {"VV", "VH"}
QUALITY_BANDS = ["SCL"]


class DownloadPlanBuilder:
    """Construct approval-bound files; it never creates the user target directory."""

    def __init__(
        self,
        *,
        project_root: Path = PROJECT_ROOT,
        max_request_uncompressed_mib: int = 24,
        default_grid_meters: int = 20,
        tile_pixels: int = 512,
    ) -> None:
        self.project_root = project_root.resolve()
        self.max_request_bytes = max_request_uncompressed_mib * 1024 * 1024
        self.default_grid_meters = default_grid_meters
        self.tile_pixels = min(tile_pixels, 10_000)

    def build(
        self,
        *,
        task_id: str,
        version: int,
        aoi: AOIRef,
        periods: list[SearchPeriod],
        candidates: list[SceneCandidate],
        recommendation: SceneRecommendation,
        provider: DataProviderKind,
        download_root: str,
        selected_file_ids: list[str] | None = None,
        optical_bands: list[str] | None = None,
        sar_bands: list[str] | None = None,
        grid_meters: float | None = None,
    ) -> DownloadPlan:
        if not recommendation.accepted_by_program:
            raise ValueError("只有经过程序候选校验的模型建议才能生成下载计划")
        candidate_by_id = {item.candidate_id: item for item in candidates}
        selected_ids = (
            recommendation.selected_optical_candidate_ids
            + recommendation.selected_sar_candidate_ids
        )
        if not selected_ids:
            raise ValueError("下载计划至少需要一个已校验候选")
        unknown = set(selected_ids) - set(candidate_by_id)
        if unknown:
            raise ValueError(f"下载计划引用不存在的候选: {', '.join(sorted(unknown))}")
        optical = list(optical_bands or ["B2", "B3", "B4", "B8", "B11", "B12"])
        sar = list(sar_bands or ["VV", "VH"])
        _validate_bands(optical, OPTICAL_BAND_WHITELIST, "光学")
        _validate_bands(sar, SAR_BAND_WHITELIST, "SAR")
        scale = float(grid_meters or self.default_grid_meters)
        if not 10 <= scale <= 1000:
            raise ValueError("共享网格分辨率必须在 10 到 1000 米之间")
        grid = _shared_grid(aoi, scale)
        files: list[DownloadFile] = []
        band_map: dict[str, list[str]] = {}
        for candidate_id in selected_ids:
            candidate = candidate_by_id[candidate_id]
            if candidate.data_kind == DataKind.OPTICAL:
                band_map[candidate_id] = optical
                files.extend(
                    self._candidate_files(candidate, DataKind.OPTICAL, optical, grid, "float32")
                )
                files.extend(
                    self._candidate_files(
                        candidate, DataKind.QUALITY, QUALITY_BANDS, grid, "uint8"
                    )
                )
            elif candidate.data_kind == DataKind.SAR:
                band_map[candidate_id] = sar
                files.extend(
                    self._candidate_files(candidate, DataKind.SAR, sar, grid, "float32")
                )
        if selected_file_ids is not None:
            # All files are content dependencies of selected products in V1. Candidate
            # checkboxes add/remove products; file IDs cannot smuggle arbitrary paths.
            # The service validates submitted IDs against the previous plan. IDs for
            # a product the user just deselected are harmless and are dropped here.
            del selected_file_ids
        if not files:
            raise ValueError("下载计划没有可交付文件")
        root = _resolve_backend_root(download_root, self.project_root)
        task_dir = (root / task_id).resolve()
        if root not in task_dir.parents:
            raise ValueError("任务下载目录必须是用户根目录下的独立 task_id 子目录")
        estimated = sum(item.expected_size_bytes or 0 for item in files)
        plan = DownloadPlan(
            task_id=task_id,
            version=version,
            plan_hash="0" * 64,
            aoi_version=aoi.version,
            periods=periods,
            selected_candidate_ids=selected_ids,
            bands=band_map,
            grid=grid,
            output_root=str(root),
            resolved_task_dir=str(task_dir),
            provider=provider,
            contains_mock=provider == DataProviderKind.MOCK
            or any(candidate_by_id[item].is_mock for item in selected_ids),
            files=files,
            estimated_total_bytes=estimated,
            estimate_method="未压缩像元数×波段数×数据类型字节数；不是 HTTP 精确长度",
            created_at=datetime.now(UTC),
        )
        plan = plan.model_copy(update={"plan_hash": plan.computed_hash()})
        plan.assert_hash_matches()
        return plan

    def rebuild(
        self,
        previous: DownloadPlan,
        *,
        aoi: AOIRef,
        candidates: list[SceneCandidate],
        recommendation: SceneRecommendation,
        selected_file_ids: list[str] | None = None,
        download_root: str | None = None,
        optical_bands: list[str] | None = None,
        sar_bands: list[str] | None = None,
        grid_meters: float | None = None,
    ) -> DownloadPlan:
        return self.build(
            task_id=previous.task_id,
            version=previous.version + 1,
            aoi=aoi,
            periods=previous.periods,
            candidates=candidates,
            recommendation=recommendation,
            provider=previous.provider,
            download_root=download_root or previous.output_root,
            selected_file_ids=selected_file_ids,
            optical_bands=optical_bands,
            sar_bands=sar_bands,
            grid_meters=grid_meters or previous.grid.scale_meters,
        )

    def _candidate_files(
        self,
        candidate: SceneCandidate,
        kind: DataKind,
        bands: list[str],
        full_grid: GridSpec,
        dtype: str,
    ) -> list[DownloadFile]:
        if full_grid.width is None or full_grid.height is None or full_grid.transform is None:
            raise ValueError("共享网格缺少明确尺寸或仿射变换")
        bytes_per_sample = {"uint8": 1, "uint16": 2, "float32": 4}[dtype]
        max_pixels_by_bytes = max(1, self.max_request_bytes // (len(bands) * bytes_per_sample))
        max_side = min(10_000, self.tile_pixels, int(math.sqrt(max_pixels_by_bytes)))
        files: list[DownloadFile] = []
        safe_candidate = _safe_id(candidate.candidate_id)
        for row_offset in range(0, full_grid.height, max_side):
            height = min(max_side, full_grid.height - row_offset)
            for column_offset in range(0, full_grid.width, max_side):
                width = min(max_side, full_grid.width - column_offset)
                a, b, c, d, e, f = full_grid.transform
                transform = (
                    a,
                    b,
                    c + column_offset * a + row_offset * b,
                    d,
                    e,
                    f + column_offset * d + row_offset * e,
                )
                file_id = (
                    f"{safe_candidate}-{kind.value}-r{row_offset:05d}-c{column_offset:05d}"
                )
                relative = f"{kind.value}/{file_id}.tif"
                estimate = width * height * len(bands) * bytes_per_sample
                if estimate > self.max_request_bytes:
                    raise ValueError("程序分块估计仍超过单请求上限")
                files.append(
                    DownloadFile(
                        file_id=file_id,
                        candidate_id=candidate.candidate_id,
                        data_kind=kind,
                        period_id=candidate.period_id,
                        relative_path=relative,
                        bands=list(bands),
                        grid=GridSpec(
                            crs=full_grid.crs,
                            scale_meters=full_grid.scale_meters,
                            width=width,
                            height=height,
                            transform=transform,
                            nodata=255 if dtype == "uint8" else -9999.0,
                        ),
                        media_type="image/tiff",
                        expected_size_bytes=estimate,
                        expected_size_method=(
                            f"{width}×{height}×{len(bands)}×{bytes_per_sample} 字节（未压缩估计）"
                        ),
                        required=True,
                        contains_mock=candidate.is_mock,
                    )
                )
        return files


def _shared_grid(aoi: AOIRef, scale: float) -> GridSpec:
    try:
        from pyproj import Transformer
    except ImportError as exc:
        raise RuntimeError("共享投影网格需要 imagery extra（pyproj）") from exc
    min_lon, min_lat, max_lon, max_lat = aoi.bbox
    center_lon = (min_lon + max_lon) / 2
    center_lat = (min_lat + max_lat) / 2
    zone = max(1, min(60, int((center_lon + 180) // 6) + 1))
    epsg = (32600 if center_lat >= 0 else 32700) + zone
    crs = f"EPSG:{epsg}"
    transformer = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    points = [
        transformer.transform(lon, lat)
        for lon in (min_lon, max_lon)
        for lat in (min_lat, max_lat)
    ]
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    left = math.floor(min(xs) / scale) * scale
    right = math.ceil(max(xs) / scale) * scale
    bottom = math.floor(min(ys) / scale) * scale
    top = math.ceil(max(ys) / scale) * scale
    width = max(1, math.ceil((right - left) / scale))
    height = max(1, math.ceil((top - bottom) / scale))
    return GridSpec(
        crs=crs,
        scale_meters=scale,
        width=width,
        height=height,
        transform=(scale, 0.0, left, 0.0, -scale, top),
        nodata=-9999.0,
    )


def _resolve_backend_root(value: str, project_root: Path) -> Path:
    raw = value.strip()
    if not raw:
        raise ValueError("下载根目录不能为空")
    if os.name != "nt" and re.match(r"^[A-Za-z]:[\\/]", raw):
        raise ValueError("当前后端不是 Windows，不能把 Windows 盘符路径当作相对目录")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = project_root / path
    return path.resolve()


def _validate_bands(values: list[str], allowed: set[str], label: str) -> None:
    if not values or len(values) != len(set(values)):
        raise ValueError(f"{label}波段不能为空或重复")
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"{label}波段不在白名单: {', '.join(sorted(unknown))}")


def _safe_id(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-")
    if not safe:
        raise ValueError("候选 ID 无法生成安全文件 ID")
    return safe[:80]
