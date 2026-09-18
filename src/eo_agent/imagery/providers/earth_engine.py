"""Google Earth Engine imagery provider.

The module has no import-time Earth Engine side effects.  Selecting ``gee``
and invoking a remote operation performs the lazy import and ``Initialize``;
it never calls ``ee.Authenticate`` and never falls back to mock data.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import math
import os
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, TypeVar
from urllib.parse import urlsplit

from eo_agent.imagery.providers.base import (
    CatalogPage,
    ImageryProviderError,
    PreviewURLs,
    ProviderDownloadRequest,
    ProviderFailureKind,
)

S2_DATASET = "COPERNICUS/S2_SR_HARMONIZED"
CLOUD_SCORE_DATASET = "GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED"
S1_DATASET = "COPERNICUS/S1_GRD"

S2_REFLECTANCE_BANDS = frozenset({"B2", "B3", "B4", "B8", "B11", "B12"})
S2_DEFAULT_BANDS = ("B2", "B3", "B4", "B8", "B11", "B12")
S1_BANDS = frozenset({"VV", "VH"})
_TRUSTED_DOWNLOAD_HOST_SUFFIXES = (".googleapis.com", ".googleusercontent.com")
_T = TypeVar("_T")


class EarthEngineProvider:
    """Read Sentinel catalogues and build approved Earth Engine downloads."""

    name = "gee"
    is_mock = False

    def __init__(
        self,
        *,
        project_id: str | None = None,
        ee_module: Any | None = None,
        credentials: Any | None = None,
        max_catalog_limit: int = 100,
        max_request_uncompressed_mib: float = 24,
        max_pixels: int = 50_000_000,
    ) -> None:
        self.project_id = (project_id or os.getenv("EE_PROJECT_ID", "")).strip() or None
        self._ee = ee_module
        self._credentials = credentials
        self._initialized = False
        self._max_catalog_limit = max_catalog_limit
        self._max_request_bytes = int(max_request_uncompressed_mib * 1024 * 1024)
        self._max_pixels = max_pixels
        if max_catalog_limit < 1:
            raise ValueError("max_catalog_limit must be positive")
        if max_request_uncompressed_mib <= 0 or max_request_uncompressed_mib >= 32:
            raise ValueError("max_request_uncompressed_mib must be greater than 0 and below 32")

    def doctor(self, *, check_remote: bool = False) -> Mapping[str, Any]:
        """Check local readiness, and only when requested make a tiny remote read."""

        dependency_available = self._ee is not None or importlib.util.find_spec("ee") is not None
        configured = bool(self.project_id)
        base: dict[str, Any] = {
            "provider": self.name,
            "is_mock": False,
            "dependency": "earthengine-api",
            "dependency_available": dependency_available,
            "project_configured": configured,
            "remote_checked": check_remote,
            "remote_available": None,
            "datasets": [S2_DATASET, CLOUD_SCORE_DATASET, S1_DATASET],
            "authentication_behavior": "uses existing credentials; never calls ee.Authenticate",
        }
        if not dependency_available:
            base.update(
                status="dependency_missing",
                message="未安装 earthengine-api；未尝试连接 Earth Engine。",
            )
            return base
        if not configured:
            base.update(
                status="configuration_missing",
                message="未设置 EE_PROJECT_ID；未尝试连接 Earth Engine。",
            )
            return base
        if not check_remote:
            base.update(
                status="local_ready",
                message="依赖和项目配置存在；真实连接尚未验证。",
            )
            return base
        try:
            self.initialize()
            value = self._remote_call(lambda: self._ee.Number(1).getInfo(), "doctor")
            if value != 1:
                raise ImageryProviderError(
                    ProviderFailureKind.REMOTE,
                    "Earth Engine 只读检查返回了意外结果。",
                )
        except ImageryProviderError as exc:
            base.update(
                status=exc.kind.value,
                message=str(exc),
                retryable=exc.retryable,
                remote_available=False,
            )
            return base
        base.update(
            status="remote_ready",
            message="Earth Engine 初始化和小型只读检查成功。",
            remote_available=True,
        )
        return base

    def initialize(self) -> None:
        """Initialize with existing credentials; never launch an auth flow."""

        if self._initialized:
            return
        if not self.project_id:
            raise ImageryProviderError(
                ProviderFailureKind.CONFIGURATION_MISSING,
                "未设置 EE_PROJECT_ID，无法初始化 Earth Engine。",
            )
        if self._ee is None:
            try:
                self._ee = importlib.import_module("ee")
            except (ImportError, ModuleNotFoundError) as exc:
                raise ImageryProviderError(
                    ProviderFailureKind.DEPENDENCY_MISSING,
                    "未安装 earthengine-api，无法使用 gee 数据后端。",
                    cause=exc,
                ) from exc
        try:
            if self._credentials is None:
                self._ee.Initialize(project=self.project_id)
            else:
                self._ee.Initialize(credentials=self._credentials, project=self.project_id)
        except Exception as exc:  # vendor SDK exposes several optional exception types
            raise self._classify_exception(exc, "初始化 Earth Engine") from exc
        self._initialized = True

    def search_optical(
        self,
        *,
        aoi: Mapping[str, Any] | Any,
        start: str,
        end: str,
        limit: int = 5,
        offset: int = 0,
        window_id: str | None = None,
    ) -> Mapping[str, Any]:
        """Search a bounded page of Sentinel-2 L2A candidates."""

        self.initialize()
        start_iso, end_iso = self._validate_interval(start, end)
        limit, offset = self._validate_page(limit, offset)
        geometry_json = self._normalise_geometry(aoi)
        geometry = self._ee.Geometry(geometry_json)

        def run() -> dict[str, Any]:
            collection = (
                self._ee.ImageCollection(S2_DATASET)
                .filterBounds(geometry)
                .filterDate(start_iso, end_iso)
                .map(self._with_stable_sort_key)
                .sort("_eo_sort_key")
            )
            page = self._read_catalog_page(
                collection=collection,
                geometry=geometry,
                limit=limit,
                offset=offset,
                feature_builder=self._s2_feature,
                item_builder=lambda properties: self._s2_item(properties, window_id),
            )
            result = page.to_dict()
            result.update(
                provider=self.name,
                dataset=S2_DATASET,
                start=start_iso,
                end=end_iso,
                interval_semantics="left_closed_right_open",
                filters={"bounds": "user_aoi", "date": [start_iso, end_iso]},
                is_mock=False,
            )
            return result

        return self._remote_call(run, "检索 Sentinel-2")

    def create_preview_urls(
        self,
        *,
        scene: Mapping[str, Any],
        aoi: Mapping[str, Any] | Any,
        max_dimension: int = 768,
    ) -> PreviewURLs:
        """Create ephemeral raw-RGB and SCL overlay thumbnail URLs."""

        self.initialize()
        if not 64 <= max_dimension <= 2048:
            raise self._invalid("缩略图最大边长必须在 64 到 2048 像素之间。")
        dataset = str(scene.get("dataset") or "")
        if dataset == S2_DATASET:
            image, system_index = self._scene_image(scene, expected_dataset=S2_DATASET)
        elif dataset == S1_DATASET:
            image, system_index = self._scene_image(scene, expected_dataset=S1_DATASET)
        else:
            raise self._invalid("预览候选必须来自已验证的 Sentinel-2 或 Sentinel-1 数据集。")
        geometry_json = self._normalise_geometry(aoi)
        geometry = self._ee.Geometry(geometry_json)

        def run() -> PreviewURLs:
            if dataset == S2_DATASET:
                rgb = image.select(["B4", "B3", "B2"]).visualize(
                    bands=["B4", "B3", "B2"],
                    min=0,
                    max=3000,
                    gamma=1.1,
                )
                scl = image.select("SCL")
                cloud_shadow = (
                    scl.eq(3).multiply(1)
                    .where(scl.eq(8).Or(scl.eq(9)).Or(scl.eq(10)), 2)
                    .where(scl.eq(11), 3)
                    .where(scl.eq(0).Or(scl.eq(1)), 4)
                )
                overlay = cloud_shadow.updateMask(cloud_shadow.gt(0)).visualize(
                    min=1,
                    max=4,
                    palette=["4c1d95", "ef4444", "67e8f9", "111827"],
                    opacity=0.60,
                )
                quality_preview = rgb.blend(overlay)
                warnings = (
                    "预览保留原始云；叠加层来自 SCL，不是正式分析数据。",
                    "短时访问 URL 不得落盘或发送给模型。",
                )
            else:
                vv = image.select("VV")
                vh = image.select("VH")
                rgb = vv.visualize(min=-25, max=0, palette=["111827", "f8fafc"])
                quality_preview = (
                    image.select(["VV", "VH"])
                    .addBands(vv.subtract(vh).rename("VV_minus_VH"))
                    .visualize(
                        bands=["VV", "VH", "VV_minus_VH"],
                        min=[-25, -32, 0],
                        max=[0, -5, 15],
                    )
                )
                warnings = (
                    "SAR 预览为 VV 灰度与 VV/VH 观测组合，不是真彩色。",
                    "短时访问 URL 不得落盘或发送给模型。",
                )
            common = {
                "region": geometry,
                "dimensions": max_dimension,
                "format": "png",
            }
            rgb_url = rgb.getThumbURL(common)
            quality_url = quality_preview.getThumbURL(common)
            self._validate_provider_url(rgb_url)
            self._validate_provider_url(quality_url)
            return PreviewURLs(
                rgb_url=rgb_url,
                quality_url=quality_url,
                provider=self.name,
                candidate_id=self._candidate_id(
                    "s2" if dataset == S2_DATASET else "s1", system_index
                ),
                warnings=warnings,
            )

        return self._remote_call(run, "生成 Sentinel-2 缩略图")

    def assess_optical_quality(
        self,
        *,
        scene: Mapping[str, Any],
        aoi: Mapping[str, Any] | Any,
        cloud_score_threshold: float = 0.60,
        scale_meters: int = 20,
    ) -> Mapping[str, Any]:
        """Calculate area-weighted SCL and Cloud Score+ quality inside the AOI."""

        self.initialize()
        if not 0 <= cloud_score_threshold <= 1:
            raise self._invalid("Cloud Score+ 清晰阈值必须在 0 到 1 之间。")
        if not 10 <= scale_meters <= 1000:
            raise self._invalid("质量计算尺度必须在 10 到 1000 米之间。")
        image, system_index = self._scene_image(scene, expected_dataset=S2_DATASET)
        geometry_json = self._normalise_geometry(aoi)
        geometry = self._ee.Geometry(geometry_json)

        def run() -> dict[str, Any]:
            score_matches = self._ee.ImageCollection(CLOUD_SCORE_DATASET).filter(
                self._ee.Filter.eq("system:index", system_index)
            )
            score_match_count = int(score_matches.size().getInfo())
            scl = image.select("SCL")
            spectral_mask = (
                image.select(list(S2_DEFAULT_BANDS)).mask().reduce(self._ee.Reducer.min())
            )
            scl_mask = scl.mask()
            valid = spectral_mask.And(scl_mask).And(scl.neq(0)).And(scl.neq(1))
            shadow = scl.eq(3)
            cloud = scl.eq(8).Or(scl.eq(9)).Or(scl.eq(10))
            cloud_shadow = shadow.Or(cloud)
            snow = scl.eq(11)
            scl_clear = valid.And(cloud_shadow.Not()).And(snow.Not())
            warnings: list[str] = []
            if score_match_count:
                score = self._ee.Image(score_matches.first()).select("cs_cdf")
                quality_assessed = valid.And(score.mask())
                clear = scl_clear.And(score.gte(cloud_score_threshold))
                method = "SCL + Cloud Score+ cs_cdf (matched by system:index)"
                if score_match_count > 1:
                    warnings.append(
                        "Cloud Score+ 出现多个同 system:index 结果；使用稳定集合中的第一项。"
                    )
            else:
                quality_assessed = valid
                clear = scl_clear
                method = "SCL-only (Cloud Score+ system:index match unavailable)"
                warnings.append("未匹配到 Cloud Score+；质量已明确降级为 SCL-only。")

            pixel_area = self._ee.Image.pixelArea()
            area_stack = self._ee.Image.cat(
                pixel_area.updateMask(valid).rename("valid_area_m2"),
                pixel_area.updateMask(clear).rename("clear_area_m2"),
                pixel_area.updateMask(valid.And(cloud_shadow)).rename(
                    "cloud_shadow_area_m2"
                ),
                pixel_area.updateMask(valid.And(cloud)).rename("cloud_area_m2"),
                pixel_area.updateMask(valid.And(shadow)).rename("shadow_area_m2"),
                pixel_area.updateMask(quality_assessed).rename("quality_area_m2"),
                pixel_area.updateMask(valid.And(snow)).rename("snow_area_m2"),
                pixel_area.updateMask(scl_mask.And(scl.eq(1))).rename(
                    "saturated_area_m2"
                ),
            )
            reduced = area_stack.reduceRegion(
                reducer=self._ee.Reducer.sum(),
                geometry=geometry,
                scale=scale_meters,
                bestEffort=False,
                maxPixels=self._max_pixels,
            ).getInfo()
            aoi_area = float(geometry.area(maxError=1).getInfo())
            if not math.isfinite(aoi_area) or aoi_area <= 0:
                raise self._invalid("研究区面积为空或无效，无法计算质量比例。")
            areas = {
                key: self._optional_nonnegative_float(reduced.get(key))
                for key in (
                    "valid_area_m2",
                    "clear_area_m2",
                    "cloud_shadow_area_m2",
                    "cloud_area_m2",
                    "shadow_area_m2",
                    "quality_area_m2",
                    "snow_area_m2",
                    "saturated_area_m2",
                )
            }
            valid_area = areas["valid_area_m2"]
            if valid_area is None:
                return self._unknown_quality(
                    scene=scene,
                    method=method,
                    threshold=cloud_score_threshold,
                    scale_meters=scale_meters,
                    score_match_count=score_match_count,
                    reason="Earth Engine 面积归约没有返回有效覆盖面积。",
                    warnings=warnings,
                )
            if valid_area <= 0:
                warnings.append("研究区内没有有效光谱覆盖；不能推断云量或清晰率。")
                return {
                    "candidate_id": self._candidate_id("s2", system_index),
                    "status": "insufficient_coverage",
                    "valid_coverage_fraction": 0.0,
                    "clear_aoi_fraction": None,
                    "cloud_shadow_fraction_on_valid": None,
                    "quality_assessed_fraction": None,
                    "cloud_fraction_on_valid": None,
                    "shadow_fraction_on_valid": None,
                    "snow_fraction_on_valid": None,
                    "nodata_fraction_of_aoi": None,
                    "saturated_fraction_on_valid": None,
                    "method": method,
                    "cloud_score_band": "cs_cdf" if score_match_count else None,
                    "threshold": cloud_score_threshold if score_match_count else None,
                    "scale_meters": scale_meters,
                    "cloud_score_match_count": score_match_count,
                    "aoi_area_m2": aoi_area,
                    "areas_m2": areas,
                    "denominators": self._quality_denominators(),
                    "warnings": warnings,
                    "reason": "no_valid_coverage",
                    "contains_mock": False,
                }
            clear_area = areas["clear_area_m2"]
            cloud_shadow_area = areas["cloud_shadow_area_m2"]
            cloud_area = areas["cloud_area_m2"]
            shadow_area = areas["shadow_area_m2"]
            quality_area = areas["quality_area_m2"]
            snow_area = areas["snow_area_m2"]
            saturated_area = areas["saturated_area_m2"]
            result = {
                "candidate_id": self._candidate_id("s2", system_index),
                "status": "valid",
                "valid_coverage_fraction": self._fraction(valid_area, aoi_area),
                "clear_aoi_fraction": self._fraction(clear_area, aoi_area),
                "cloud_shadow_fraction_on_valid": self._fraction(
                    cloud_shadow_area, valid_area
                ),
                "quality_assessed_fraction": self._fraction(quality_area, aoi_area),
                "cloud_fraction_on_valid": self._fraction(cloud_area, valid_area),
                "shadow_fraction_on_valid": self._fraction(shadow_area, valid_area),
                "snow_fraction_on_valid": self._fraction(snow_area, valid_area),
                "nodata_fraction_of_aoi": max(
                    0.0,
                    min(
                        1.0,
                        1
                        - valid_area / aoi_area
                        - (saturated_area or 0.0) / aoi_area,
                    ),
                ),
                "saturated_fraction_on_valid": self._fraction(saturated_area, valid_area),
                "method": method,
                "threshold": cloud_score_threshold if score_match_count else None,
                "scale_meters": scale_meters,
                "cloud_score_match_count": score_match_count,
                "aoi_area_m2": aoi_area,
                "areas_m2": areas,
                "denominators": self._quality_denominators(),
                "warnings": warnings,
                "reason": None,
                "contains_mock": False,
            }
            if clear_area is None or quality_area is None or cloud_shadow_area is None:
                result["status"] = "unknown"
                result["reason"] = "one_or_more_area_reductions_missing"
                result["warnings"].append(
                    "部分面积统计缺失；缺失字段保持 null，不解释为 0%。"
                )
            return result

        return self._remote_call(run, "计算研究区 Sentinel-2 质量")

    def search_sar(
        self,
        *,
        aoi: Mapping[str, Any] | Any,
        start: str,
        end: str,
        limit: int = 5,
        offset: int = 0,
        window_id: str | None = None,
        target_optical: tuple[Mapping[str, Any], ...] = (),
    ) -> Mapping[str, Any]:
        """Search Sentinel-1 GRD IW scenes containing both VV and VH."""

        self.initialize()
        start_iso, end_iso = self._validate_interval(start, end)
        limit, offset = self._validate_page(limit, offset)
        geometry_json = self._normalise_geometry(aoi)
        geometry = self._ee.Geometry(geometry_json)

        def run() -> dict[str, Any]:
            collection = (
                self._ee.ImageCollection(S1_DATASET)
                .filterBounds(geometry)
                .filterDate(start_iso, end_iso)
                .filter(self._ee.Filter.eq("instrumentMode", "IW"))
                .filter(
                    self._ee.Filter.listContains("transmitterReceiverPolarisation", "VV")
                )
                .filter(
                    self._ee.Filter.listContains("transmitterReceiverPolarisation", "VH")
                )
                .map(self._with_stable_sort_key)
                .sort("_eo_sort_key")
            )
            page = self._read_catalog_page(
                collection=collection,
                geometry=geometry,
                limit=limit,
                offset=offset,
                feature_builder=self._s1_feature,
                item_builder=lambda properties: self._s1_item(
                    properties, window_id, target_optical
                ),
            )
            result = page.to_dict()
            result.update(
                provider=self.name,
                dataset=S1_DATASET,
                start=start_iso,
                end=end_iso,
                interval_semantics="left_closed_right_open",
                filters={
                    "bounds": "user_aoi",
                    "date": [start_iso, end_iso],
                    "instrument_mode": "IW",
                    "required_polarizations": ["VV", "VH"],
                },
                ordering=(
                    "stable acquisition time/system:index; callers may group same relative orbit "
                    "before comparing dates"
                ),
                is_mock=False,
            )
            return result

        return self._remote_call(run, "检索 Sentinel-1")

    def create_download_request(
        self,
        *,
        scene: Mapping[str, Any],
        tile: Mapping[str, Any],
        approval: Mapping[str, Any],
    ) -> ProviderDownloadRequest:
        """Generate one ephemeral GeoTIFF URL for an approved, aligned tile."""

        self.initialize()
        plan_hash, plan_version = self._validate_approval(tile, approval)
        role = str(
            tile.get("data_kind")
            or tile.get("role")
            or tile.get("product_kind")
            or ""
        ).lower()
        tile_id = str(tile.get("tile_id") or tile.get("file_id") or "").strip()
        if not tile_id or not all(char.isalnum() or char in "-_" for char in tile_id):
            raise self._invalid("下载 tile_id 为空或含不安全字符。")
        grid = tile.get("grid")
        if hasattr(grid, "model_dump"):
            grid = grid.model_dump(mode="json")
        if not isinstance(grid, Mapping):
            grid = {}
        dimensions = tile.get("dimensions")
        if not dimensions:
            dimensions = [
                tile.get("width") or grid.get("width"),
                tile.get("height") or grid.get("height"),
            ]
        if not isinstance(dimensions, Sequence) or len(dimensions) != 2:
            raise self._invalid("下载分块必须提供 [width, height] dimensions。")
        width, height = (self._positive_int(value, "dimensions") for value in dimensions)
        if width > 10_000 or height > 10_000:
            raise self._invalid("Earth Engine 单请求网格维度不能超过 10000。")
        transform = (
            tile.get("crs_transform")
            or tile.get("transform")
            or grid.get("crs_transform")
            or grid.get("transform")
        )
        if not isinstance(transform, Sequence) or len(transform) != 6:
            raise self._invalid("下载分块必须提供六元素 crs_transform。")
        transform_values = [self._finite_float(value, "crs_transform") for value in transform]
        crs = str(tile.get("crs") or grid.get("crs") or "").strip()
        if not crs or len(crs) > 128:
            raise self._invalid("下载分块必须提供有效 CRS。")
        region_json = self._normalise_geometry(
            tile.get("region") or tile.get("geometry") or grid.get("region")
        )
        region = self._ee.Geometry(region_json)
        image, system_index, bands, dtype, unit, resampling = self._download_image(
            scene=scene,
            role=role,
            requested_bands=tile.get("bands"),
        )
        requested_dtype = tile.get("dtype")
        if requested_dtype and str(requested_dtype).lower() != dtype:
            raise self._invalid(
                f"计划 dtype={requested_dtype!s} 与 {role} 的固定导出类型 {dtype} 不一致。"
            )
        bytes_per_sample = {"uint8": 1, "uint16": 2, "float32": 4}[dtype]
        estimated_bytes = width * height * len(bands) * bytes_per_sample
        if estimated_bytes > self._max_request_bytes:
            raise self._invalid(
                "分块未压缩估计超过配置上限；必须按相同批准网格进一步切块，不能降分辨率。"
            )
        clipped = image.clip(region)
        params = {
            "bands": list(bands),
            "crs": crs,
            "crs_transform": transform_values,
            "dimensions": [width, height],
            # Official API ignores region when crs + transform are supplied;
            # clip above enforces the user/tile geometry nevertheless.
            "region": region,
            "format": "GEO_TIFF",
            "filePerBand": False,
        }
        content_definition = {
            "provider": self.name,
            "dataset": scene.get("dataset"),
            "system_index": system_index,
            "role": role,
            "bands": list(bands),
            "dtype": dtype,
            "unit": unit,
            "resampling": resampling,
            "crs": crs,
            "crs_transform": transform_values,
            "dimensions": [width, height],
            "region": region_json,
            "plan_hash": plan_hash,
            "plan_version": plan_version,
        }
        definition_hash = hashlib.sha256(
            json.dumps(
                content_definition,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()

        def run() -> ProviderDownloadRequest:
            url = clipped.getDownloadURL(params)
            self._validate_provider_url(url)
            return ProviderDownloadRequest(
                url=url,
                provider=self.name,
                tile_id=tile_id,
                content_definition_hash=definition_hash,
                metadata={
                    "dataset": scene.get("dataset"),
                    "system_index": system_index,
                    "bands": list(bands),
                    "dtype": dtype,
                    "unit": unit,
                    "resampling": resampling,
                    "scale_factor_applied": 0.0001 if role in {"optical", "reflectance"} else None,
                    "estimated_uncompressed_bytes": estimated_bytes,
                    "dimensions": [width, height],
                    "crs": crs,
                    "crs_transform": transform_values,
                    "plan_hash": plan_hash,
                    "plan_version": plan_version,
                    "short_lived_url": True,
                    "url_must_not_be_logged": True,
                },
            )

        return self._remote_call(run, "生成已审批分块的 Earth Engine 下载请求")

    def _download_image(
        self,
        *,
        scene: Mapping[str, Any],
        role: str,
        requested_bands: Any,
    ) -> tuple[Any, str, tuple[str, ...], str, str, str]:
        if role in {"optical", "reflectance", "s2_reflectance"}:
            image, system_index = self._scene_image(scene, expected_dataset=S2_DATASET)
            bands = self._validate_bands(
                requested_bands or S2_DEFAULT_BANDS, S2_REFLECTANCE_BANDS
            )
            prepared = image.select(list(bands)).resample("bilinear").multiply(0.0001).toFloat()
            return prepared, system_index, bands, "float32", "surface_reflectance", "bilinear"
        if role in {"quality"}:
            requested = tuple(str(value) for value in (requested_bands or ("SCL",)))
            role = "optical_quality" if set(requested).issubset({"cs", "cs_cdf"}) else "scl"
            requested_bands = requested
        if role in {"scl", "optical_scl"}:
            image, system_index = self._scene_image(scene, expected_dataset=S2_DATASET)
            bands = self._validate_bands(requested_bands or ("SCL",), frozenset({"SCL"}))
            return (
                image.select(list(bands)).toUint8(),
                system_index,
                bands,
                "uint8",
                "class",
                "nearest",
            )
        if role in {"cloud_score", "cs_cdf", "optical_quality"}:
            _, system_index = self._scene_image(scene, expected_dataset=S2_DATASET)
            bands = self._validate_bands(
                requested_bands or ("cs_cdf",), frozenset({"cs", "cs_cdf"})
            )
            matches = self._ee.ImageCollection(CLOUD_SCORE_DATASET).filter(
                self._ee.Filter.eq("system:index", system_index)
            )
            count = self._remote_call(
                lambda: int(matches.size().getInfo()), "检查 Cloud Score+ 下载源"
            )
            if count < 1:
                raise ImageryProviderError(
                    ProviderFailureKind.NO_DATA,
                    "所选 Sentinel-2 影像没有同 system:index 的 Cloud Score+ 产品。",
                )
            prepared = self._ee.Image(matches.first()).select(list(bands)).toFloat()
            return prepared, system_index, bands, "float32", "unitless_score", "bilinear"
        if role in {"sar", "s1", "sar_backscatter"}:
            image, system_index = self._scene_image(scene, expected_dataset=S1_DATASET)
            bands = self._validate_bands(requested_bands or ("VV", "VH"), S1_BANDS)
            prepared = image.select(list(bands)).resample("bilinear").toFloat()
            return prepared, system_index, bands, "float32", "dB", "bilinear"
        raise self._invalid(
            "不支持的下载数据类型；仅允许 reflectance、SCL、Cloud Score+ 或 SAR。"
        )

    def _read_catalog_page(
        self,
        *,
        collection: Any,
        geometry: Any,
        limit: int,
        offset: int,
        feature_builder: Callable[[Any, Any], Any],
        item_builder: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    ) -> CatalogPage:
        total = int(collection.size().getInfo())
        bounded_total = min(total, self._max_catalog_limit)
        if offset >= bounded_total:
            items: tuple[Mapping[str, Any], ...] = ()
        else:
            take = min(limit, bounded_total - offset)
            bounded = collection.limit(self._max_catalog_limit, "_eo_sort_key", True)
            image_list = bounded.toList(take, offset)
            feature_list = image_list.map(lambda value: feature_builder(value, geometry))
            info = self._ee.FeatureCollection(feature_list).getInfo()
            features = info.get("features", []) if isinstance(info, Mapping) else []
            items = tuple(
                item_builder(feature.get("properties", {}))
                for feature in features
                if isinstance(feature, Mapping)
            )
        return CatalogPage(
            items=items,
            total_matches=total,
            offset=offset,
            limit=limit,
            has_more=offset + len(items) < bounded_total,
            catalogue_truncated=total > self._max_catalog_limit,
        )

    def _with_stable_sort_key(self, value: Any) -> Any:
        image = self._ee.Image(value)
        stamp = self._ee.Date(image.get("system:time_start")).format("YYYYMMddHHmmssSSS")
        key = stamp.cat("_").cat(self._ee.String(image.get("system:index")))
        return image.set("_eo_sort_key", key)

    def _s2_feature(self, value: Any, geometry: Any) -> Any:
        image = self._ee.Image(value)
        return self._ee.Feature(
            None,
            {
                "system_index": image.get("system:index"),
                "acquired_millis": image.get("system:time_start"),
                "product_id": image.get("PRODUCT_ID"),
                "mgrs_tile": image.get("MGRS_TILE"),
                "scene_cloud_percent": image.get("CLOUDY_PIXEL_PERCENTAGE"),
                "processing_baseline": image.get("PROCESSING_BASELINE"),
                "generation_time": image.get("GENERATION_TIME"),
                "datastrip_id": image.get("DATASTRIP_ID"),
                "coverage_fraction": self._coverage_fraction(image, geometry),
            },
        )

    def _s1_feature(self, value: Any, geometry: Any) -> Any:
        image = self._ee.Image(value)
        return self._ee.Feature(
            None,
            {
                "system_index": image.get("system:index"),
                "acquired_millis": image.get("system:time_start"),
                "instrument_mode": image.get("instrumentMode"),
                "polarizations": image.get("transmitterReceiverPolarisation"),
                "orbit_direction": image.get("orbitProperties_pass"),
                "relative_orbit": image.get("relativeOrbitNumber_start"),
                "platform_number": image.get("platform_number"),
                "resolution_meters": image.get("resolution_meters"),
                "product_type": image.get("productType"),
                "slice_number": image.get("sliceNumber"),
                "total_slices": image.get("totalSlices"),
                "coverage_fraction": self._coverage_fraction(image, geometry),
            },
        )

    def _coverage_fraction(self, image: Any, geometry: Any) -> Any:
        aoi_area = geometry.area(maxError=1)
        intersection = image.geometry().intersection(geometry, maxError=1).area(maxError=1)
        return self._ee.Number(intersection).divide(aoi_area).max(0).min(1)

    def _s2_item(
        self, properties: Mapping[str, Any], window_id: str | None
    ) -> Mapping[str, Any]:
        system_index = self._required_index(properties)
        scene_cloud_percent = self._optional_nonnegative_float(
            properties.get("scene_cloud_percent")
        )
        return {
            "candidate_id": self._candidate_id("s2", system_index),
            "provider": self.name,
            "dataset": S2_DATASET,
            "product_id": properties.get("product_id") or system_index,
            "system_index": system_index,
            "data_kind": "optical",
            "acquired_at": self._iso_from_millis(properties.get("acquired_millis")),
            "period_id": window_id or "unassigned",
            "mgrs_tile": properties.get("mgrs_tile"),
            "coverage_fraction": self._bounded_optional_fraction(
                properties.get("coverage_fraction")
            ),
            "scene_cloud_fraction": (
                min(scene_cloud_percent / 100, 1.0)
                if scene_cloud_percent is not None
                else None
            ),
            "metadata": {
                "data_level": "Level-2A surface reflectance",
                "scene_cloud_denominator": "whole_scene_metadata",
                "processing_baseline": properties.get("processing_baseline"),
                "generation_time": properties.get("generation_time"),
                "datastrip_id": properties.get("datastrip_id"),
                "native_scale_factor": 0.0001,
            },
            "is_mock": False,
        }

    def _s1_item(
        self,
        properties: Mapping[str, Any],
        window_id: str | None,
        target_optical: tuple[Mapping[str, Any], ...],
    ) -> Mapping[str, Any]:
        system_index = self._required_index(properties)
        acquired_at = self._iso_from_millis(properties.get("acquired_millis"))
        target_id, delta_days = self._nearest_optical(acquired_at, target_optical)
        polarizations = properties.get("polarizations")
        if not isinstance(polarizations, list):
            polarizations = ["VV", "VH"]
        return {
            "candidate_id": self._candidate_id("s1", system_index),
            "provider": self.name,
            "dataset": S1_DATASET,
            "product_id": system_index,
            "system_index": system_index,
            "data_kind": "sar",
            "acquired_at": acquired_at,
            "period_id": window_id or "unassigned",
            "coverage_fraction": self._bounded_optional_fraction(
                properties.get("coverage_fraction")
            ),
            "polarizations": polarizations,
            "orbit_direction": properties.get("orbit_direction"),
            "relative_orbit": properties.get("relative_orbit"),
            "matched_optical_candidate_id": target_id,
            "delta_days": delta_days,
            "metadata": {
                "instrument_mode": properties.get("instrument_mode"),
                "platform_number": properties.get("platform_number"),
                "resolution_meters": properties.get("resolution_meters"),
                "product_type": properties.get("product_type"),
                "slice_number": properties.get("slice_number"),
                "total_slices": properties.get("total_slices"),
                "unit": "dB",
                "earth_engine_preprocessing": (
                    "GRD border/thermal noise removal, radiometric calibration, terrain "
                    "correction; no extra radiometric terrain flattening claimed"
                ),
            },
            "is_mock": False,
        }

    def _scene_image(self, scene: Mapping[str, Any], *, expected_dataset: str) -> tuple[Any, str]:
        dataset = str(scene.get("dataset") or "")
        if dataset != expected_dataset:
            raise self._invalid(f"候选数据集必须是 {expected_dataset}。")
        system_index = str(scene.get("system_index") or "").strip()
        if not system_index or "/" in system_index or "\\" in system_index:
            raise self._invalid("候选缺少合法 system:index。")
        expected_asset = f"{expected_dataset}/{system_index}"
        asset_id = str(scene.get("asset_id") or expected_asset)
        if asset_id != expected_asset:
            raise self._invalid("候选 asset_id 与已验证 dataset/system:index 不一致。")
        return self._ee.Image(expected_asset), system_index

    def _normalise_geometry(self, value: Mapping[str, Any] | Any) -> dict[str, Any]:
        if value is None:
            raise self._invalid("缺少研究区几何。")
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        if not isinstance(value, Mapping):
            raise self._invalid("研究区必须是 GeoJSON Polygon 或 MultiPolygon。")
        if self._is_synthetic(value):
            raise self._invalid("合成或演示研究区不能发送到真实 Earth Engine provider。")
        geometry: Any = value
        if value.get("type") == "Feature":
            geometry = value.get("geometry")
        elif value.get("type") == "FeatureCollection":
            features = value.get("features")
            if not isinstance(features, list) or len(features) != 1:
                raise self._invalid("真实检索只接受含一个研究区的 FeatureCollection。")
            geometry = features[0].get("geometry") if isinstance(features[0], Mapping) else None
        elif "geometry" in value and value.get("type") not in {"Polygon", "MultiPolygon"}:
            geometry = value.get("geometry")
        if hasattr(geometry, "model_dump"):
            geometry = geometry.model_dump(mode="json")
        if not isinstance(geometry, Mapping) or geometry.get("type") not in {
            "Polygon",
            "MultiPolygon",
        }:
            raise self._invalid("真实检索只接受 Polygon 或 MultiPolygon。")
        coordinates = geometry.get("coordinates")
        coordinate_count = self._validate_coordinates(coordinates)
        if coordinate_count < 4:
            raise self._invalid("研究区几何点数不足。")
        return {"type": str(geometry["type"]), "coordinates": coordinates}

    def _validate_coordinates(self, value: Any) -> int:
        if not isinstance(value, (list, tuple)) or not value:
            raise self._invalid("GeoJSON coordinates 为空或格式无效。")
        if len(value) >= 2 and all(isinstance(item, (int, float)) for item in value[:2]):
            longitude = self._finite_float(value[0], "longitude")
            latitude = self._finite_float(value[1], "latitude")
            if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
                raise self._invalid("研究区经纬度超出 WGS84 范围。")
            return 1
        return sum(self._validate_coordinates(item) for item in value)

    @staticmethod
    def _is_synthetic(value: Mapping[str, Any]) -> bool:
        candidates: list[Mapping[str, Any]] = [value]
        properties = value.get("properties")
        if isinstance(properties, Mapping):
            candidates.append(properties)
        for candidate in candidates:
            if candidate.get("is_synthetic") is True or candidate.get("synthetic") is True:
                return True
            if candidate.get("real_geometry") is False:
                return True
            source = str(
                candidate.get("geometry_source") or candidate.get("source") or ""
            ).lower()
            if source in {"mock", "synthetic", "demo", "metadata_only"}:
                return True
        return False

    def _validate_interval(self, start: str, end: str) -> tuple[str, str]:
        start_dt = self._parse_datetime(start, "start")
        end_dt = self._parse_datetime(end, "end")
        if start_dt >= end_dt:
            raise self._invalid("检索结束时间必须晚于开始时间。")
        return self._format_datetime(start_dt), self._format_datetime(end_dt)

    def _validate_page(self, limit: int, offset: int) -> tuple[int, int]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise self._invalid("单页候选数量必须在 1 到 50 之间。")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise self._invalid("候选分页 offset 必须是非负整数。")
        if offset >= self._max_catalog_limit:
            raise self._invalid("分页 offset 已超过本任务配置的目录读取上限。")
        return limit, offset

    @staticmethod
    def _parse_datetime(value: str, field: str) -> datetime:
        if not isinstance(value, str) or not value.strip():
            raise ImageryProviderError(
                ProviderFailureKind.INVALID_REQUEST,
                f"{field} 必须是 ISO 8601 日期或时间。",
            )
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ImageryProviderError(
                ProviderFailureKind.INVALID_REQUEST,
                f"{field} 不是有效 ISO 8601 日期或时间。",
                cause=exc,
            ) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    @staticmethod
    def _format_datetime(value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")

    @staticmethod
    def _iso_from_millis(value: Any) -> str:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ImageryProviderError(
                ProviderFailureKind.REMOTE,
                "Earth Engine 候选缺少有效采集时间。",
            )
        return datetime.fromtimestamp(float(value) / 1000, tz=UTC).isoformat().replace(
            "+00:00", "Z"
        )

    @staticmethod
    def _nearest_optical(
        acquired_at: str, targets: tuple[Mapping[str, Any], ...]
    ) -> tuple[str | None, float | None]:
        acquired = datetime.fromisoformat(acquired_at.replace("Z", "+00:00"))
        best: tuple[float, str] | None = None
        for target in targets:
            raw_time = target.get("acquired_at")
            target_id = target.get("candidate_id")
            if not isinstance(raw_time, str) or not isinstance(target_id, str):
                continue
            try:
                target_time = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            except ValueError:
                continue
            delta = abs((acquired - target_time).total_seconds()) / 86_400
            candidate = (delta, target_id)
            if best is None or candidate < best:
                best = candidate
        return (best[1], round(best[0], 6)) if best else (None, None)

    def _validate_approval(
        self, tile: Mapping[str, Any], approval: Mapping[str, Any]
    ) -> tuple[str, int]:
        status = str(approval.get("status") or "").lower()
        if approval.get("approved") is not True and status not in {"approved", "received"}:
            raise self._invalid("正式下载 URL 只能在用户批准当前计划后生成。")
        plan_hash = str(approval.get("plan_hash") or "").strip()
        if len(plan_hash) < 16:
            raise self._invalid("审批缺少有效 plan_hash。")
        raw_version = approval.get("plan_version", approval.get("version"))
        plan_version = self._positive_int(raw_version, "plan_version")
        tile_hash = tile.get("plan_hash")
        tile_version = tile.get("plan_version")
        if tile_hash is not None and str(tile_hash) != plan_hash:
            raise self._invalid("下载分块与审批 plan_hash 不一致，必须重新确认。")
        if tile_version is not None and int(tile_version) != plan_version:
            raise self._invalid("下载分块与审批 plan_version 不一致，必须重新确认。")
        return plan_hash, plan_version

    @staticmethod
    def _validate_bands(value: Any, allowed: frozenset[str]) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)) or not value:
            raise ImageryProviderError(
                ProviderFailureKind.INVALID_REQUEST,
                "下载波段必须是非空白名单列表。",
            )
        bands = tuple(str(item) for item in value)
        if len(set(bands)) != len(bands) or not set(bands).issubset(allowed):
            raise ImageryProviderError(
                ProviderFailureKind.INVALID_REQUEST,
                f"下载波段超出白名单：{sorted(allowed)}。",
            )
        return bands

    @staticmethod
    def _validate_provider_url(url: Any) -> None:
        if not isinstance(url, str):
            raise ImageryProviderError(
                ProviderFailureKind.REMOTE,
                "Earth Engine 没有返回有效 HTTPS 地址。",
            )
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").lower()
        trusted = hostname == "earthengine.googleapis.com" or any(
            hostname.endswith(suffix) for suffix in _TRUSTED_DOWNLOAD_HOST_SUFFIXES
        )
        if parsed.scheme != "https" or not trusted or parsed.username or parsed.password:
            raise ImageryProviderError(
                ProviderFailureKind.REMOTE,
                "Earth Engine 返回了不受信任的下载端点，已拒绝访问。",
            )

    def _remote_call(self, function: Callable[[], _T], operation: str) -> _T:
        try:
            return function()
        except ImageryProviderError:
            raise
        except Exception as exc:  # vendor SDK exception hierarchy is optional at install time
            raise self._classify_exception(exc, operation) from exc

    @staticmethod
    def _classify_exception(exc: BaseException, operation: str) -> ImageryProviderError:
        text = f"{type(exc).__name__}: {exc}".lower()
        status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        if callable(status):
            try:
                status = status()
            except Exception:
                status = None
        if status in {401} or any(token in text for token in ("unauthenticated", "credential")):
            return ImageryProviderError(
                ProviderFailureKind.AUTHENTICATION,
                f"{operation}失败：Earth Engine 凭据缺失或无效；应用不会自动认证。",
                cause=exc,
            )
        if status in {403} or any(
            token in text for token in ("permission denied", "not registered", "forbidden")
        ):
            return ImageryProviderError(
                ProviderFailureKind.PERMISSION,
                f"{operation}失败：项目或账户没有所需 Earth Engine 权限。",
                cause=exc,
            )
        if status in {429} or any(token in text for token in ("quota", "rate limit")):
            return ImageryProviderError(
                ProviderFailureKind.QUOTA,
                f"{operation}失败：Earth Engine 配额或速率限制。",
                retryable=True,
                cause=exc,
            )
        if status in {500, 502, 503, 504}:
            return ImageryProviderError(
                ProviderFailureKind.REMOTE,
                f"{operation}失败：Earth Engine 暂时不可用。",
                retryable=True,
                cause=exc,
            )
        if any(
            token in text
            for token in (
                "connection",
                "dns",
                "network",
                "name resolution",
                "timed out",
                "timeout",
            )
        ):
            return ImageryProviderError(
                ProviderFailureKind.NETWORK,
                f"{operation}失败：无法连接 Earth Engine。",
                retryable=True,
                cause=exc,
            )
        return ImageryProviderError(
            ProviderFailureKind.REMOTE,
            f"{operation}失败：Earth Engine 返回未分类错误。",
            cause=exc,
        )

    @staticmethod
    def _required_index(properties: Mapping[str, Any]) -> str:
        value = properties.get("system_index")
        if not isinstance(value, str) or not value:
            raise ImageryProviderError(
                ProviderFailureKind.REMOTE,
                "Earth Engine 候选缺少 system:index。",
            )
        return value

    @staticmethod
    def _candidate_id(prefix: str, system_index: str) -> str:
        return f"gee:{prefix}:{system_index}"

    @staticmethod
    def _optional_nonnegative_float(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        converted = float(value)
        return converted if math.isfinite(converted) and converted >= 0 else None

    def _bounded_optional_fraction(self, value: Any) -> float | None:
        converted = self._optional_nonnegative_float(value)
        return min(converted, 1.0) if converted is not None else None

    @staticmethod
    def _fraction(numerator: float | None, denominator: float) -> float | None:
        if numerator is None or denominator <= 0:
            return None
        return min(max(numerator / denominator, 0.0), 1.0)

    @staticmethod
    def _finite_float(value: Any, field: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ImageryProviderError(
                ProviderFailureKind.INVALID_REQUEST,
                f"{field} 必须是有限数值。",
            )
        converted = float(value)
        if not math.isfinite(converted):
            raise ImageryProviderError(
                ProviderFailureKind.INVALID_REQUEST,
                f"{field} 必须是有限数值。",
            )
        return converted

    @staticmethod
    def _positive_int(value: Any, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ImageryProviderError(
                ProviderFailureKind.INVALID_REQUEST,
                f"{field} 必须是正整数。",
            )
        return value

    @staticmethod
    def _quality_denominators() -> dict[str, str]:
        return {
            "valid_coverage_fraction": "valid spectral pixel area / user AOI area",
            "clear_aoi_fraction": "clear quality-assessed pixel area / user AOI area",
            "cloud_shadow_fraction_on_valid": "cloud-or-shadow area / valid spectral area",
            "quality_assessed_fraction": "quality-assessed area / user AOI area",
        }

    def _unknown_quality(
        self,
        *,
        scene: Mapping[str, Any],
        method: str,
        threshold: float,
        scale_meters: int,
        score_match_count: int,
        reason: str,
        warnings: list[str],
    ) -> dict[str, Any]:
        return {
            "candidate_id": scene.get("candidate_id"),
            "status": "unknown",
            "valid_coverage_fraction": None,
            "clear_aoi_fraction": None,
            "cloud_shadow_fraction_on_valid": None,
            "quality_assessed_fraction": None,
            "cloud_fraction_on_valid": None,
            "shadow_fraction_on_valid": None,
            "snow_fraction_on_valid": None,
            "nodata_fraction_of_aoi": None,
            "saturated_fraction_on_valid": None,
            "method": method,
            "threshold": threshold if score_match_count else None,
            "scale_meters": scale_meters,
            "cloud_score_match_count": score_match_count,
            "warnings": [*warnings, reason],
            "reason": reason,
            "contains_mock": False,
        }

    @staticmethod
    def _invalid(message: str) -> ImageryProviderError:
        return ImageryProviderError(ProviderFailureKind.INVALID_REQUEST, message)
