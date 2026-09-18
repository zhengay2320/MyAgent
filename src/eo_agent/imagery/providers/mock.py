from __future__ import annotations

import hashlib
import io
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from eo_agent.imagery.providers.base import (
    ImageryProviderError,
    PreviewURLs,
    ProviderDownloadRequest,
    ProviderFailureKind,
)


class MockImageryProvider:
    """Deterministic offline provider using the same catalogue/download boundary as GEE."""

    name = "mock"
    is_mock = True

    def doctor(self, *, check_remote: bool = False) -> Mapping[str, Any]:
        dependencies = _dependency_status()
        return {
            "provider": self.name,
            "is_mock": True,
            "remote_checked": False,
            "ready": all(dependencies.values()),
            "dependencies": dependencies,
            "message": "离线 mock 不访问网络；影像依赖仅在生成/校验时使用。",
        }

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
        del aoi
        start_dt = datetime.fromisoformat(start).replace(tzinfo=UTC)
        end_dt = datetime.fromisoformat(end).replace(tzinfo=UTC)
        count = 8
        items = []
        for index in range(count):
            acquired = start_dt + (end_dt - start_dt) * ((index + 1) / (count + 1))
            candidate_id = f"MOCK-S2-{start_dt:%Y%m}-{index + 1:02d}"
            coverage = max(0.42, 0.97 - index * 0.055)
            scene_cloud = min(0.95, max(0.02, 1 - _clear_value(start_dt.month, index) + 0.05))
            items.append(
                {
                    "candidate_id": candidate_id,
                    "provider": "mock",
                    "dataset": "COPERNICUS/S2_SR_HARMONIZED",
                    "product_id": f"MOCK_PRODUCT_{start_dt:%Y%m}_{index + 1:02d}",
                    "system_index": candidate_id,
                    "acquired_at": acquired.isoformat(),
                    "period_id": window_id or f"period-{start_dt:%Y%m}",
                    "data_kind": "optical",
                    "coverage_fraction": round(coverage, 4),
                    "scene_cloud_fraction": round(scene_cloud, 4),
                    "mgrs_tile": "49RGP",
                    "metadata": {
                        "processing_baseline": "MOCK-05.00",
                        "data_level": "L2A-simulated",
                        "catalogue_subset": True,
                    },
                    "is_mock": True,
                }
            )
        selected = items[offset : offset + limit]
        return {
            "items": selected,
            "total_matches": count,
            "offset": offset,
            "limit": limit,
            "returned": len(selected),
            "has_more": offset + limit < count,
            "is_candidate_subset": offset + limit < count,
            "catalogue_truncated": False,
        }

    def create_preview_urls(
        self,
        *,
        scene: Mapping[str, Any],
        aoi: Mapping[str, Any] | Any,
        max_dimension: int = 768,
    ) -> PreviewURLs:
        del aoi, max_dimension
        candidate_id = str(scene["candidate_id"])
        return PreviewURLs(
            rgb_url=f"mock://preview/{candidate_id}/rgb",
            quality_url=f"mock://preview/{candidate_id}/quality",
            provider="mock",
            candidate_id=candidate_id,
        )

    def preview_bytes(
        self, scene: Mapping[str, Any], *, quality: bool = False, size: int = 256
    ) -> bytes:
        try:
            import numpy as np
            from PIL import Image
        except ImportError as exc:
            raise ImageryProviderError(
                ProviderFailureKind.DEPENDENCY_MISSING,
                "生成 mock 缩略图需要 imagery extra（numpy、Pillow）",
                cause=exc,
            ) from exc
        seed = int(hashlib.sha256(str(scene["candidate_id"]).encode()).hexdigest()[:8], 16)
        y, x = np.mgrid[0:size, 0:size]
        if quality:
            clear = float(scene.get("mock_clear_fraction", 0.5))
            threshold = int(clear * size)
            rgb = np.zeros((size, size, 3), dtype=np.uint8)
            rgb[:, :threshold] = (55, 170, 80)
            rgb[:, threshold:] = (210, 210, 220)
            rgb[(x + y + seed) % 29 < 3] = (90, 70, 130)
        else:
            rgb = np.stack(
                (
                    (x + seed % 97) % 256,
                    (y * 2 + seed % 67) % 256,
                    ((x + y) // 2 + seed % 131) % 256,
                ),
                axis=-1,
            ).astype(np.uint8)
            cloud_mask = (x * 3 + y * 5 + seed) % 31 < 5
            rgb[cloud_mask] = np.minimum(255, rgb[cloud_mask] // 3 + 180)
        output = io.BytesIO()
        Image.fromarray(rgb, "RGB").save(output, format="PNG")
        return output.getvalue()

    def assess_optical_quality(
        self,
        *,
        scene: Mapping[str, Any],
        aoi: Mapping[str, Any] | Any,
        cloud_score_threshold: float = 0.60,
        scale_meters: int = 20,
    ) -> Mapping[str, Any]:
        del aoi
        acquired = datetime.fromisoformat(str(scene["acquired_at"]).replace("Z", "+00:00"))
        index = int(str(scene["candidate_id"]).rsplit("-", 1)[-1]) - 1
        coverage = float(scene.get("coverage_fraction") or 0)
        clear_on_covered = _clear_value(acquired.month, index)
        if coverage < 0.55:
            return {
                "candidate_id": scene["candidate_id"],
                "status": "insufficient_coverage",
                "valid_coverage_fraction": coverage,
                "clear_aoi_fraction": None,
                "cloud_shadow_fraction_on_valid": None,
                "quality_assessed_fraction": coverage,
                "method": "mock_scl_plus_cs_cdf_area",
                "threshold": cloud_score_threshold,
                "scale_meters": scale_meters,
                "cloud_score_match_count": 1,
                "reason": "有效覆盖不足，不能解释为清晰或无云",
                "warnings": ["模拟面积统计"],
                "contains_mock": True,
            }
        clear_aoi = coverage * clear_on_covered
        cloud = max(0.0, 1 - clear_on_covered) * 0.76
        shadow = max(0.0, 1 - clear_on_covered) * 0.24
        return {
            "candidate_id": scene["candidate_id"],
            "status": "valid",
            "valid_coverage_fraction": round(coverage, 6),
            "clear_aoi_fraction": round(clear_aoi, 6),
            "cloud_shadow_fraction_on_valid": round(cloud + shadow, 6),
            "quality_assessed_fraction": round(coverage, 6),
            "cloud_fraction_on_valid": round(cloud, 6),
            "shadow_fraction_on_valid": round(shadow, 6),
            "snow_fraction_on_valid": 0.0,
            "nodata_fraction_of_aoi": round(1 - coverage, 6),
            "saturated_fraction_on_valid": 0.0,
            "method": "mock_scl_plus_cs_cdf_area",
            "threshold": cloud_score_threshold,
            "scale_meters": scale_meters,
            "cloud_score_match_count": 1,
            "reason": None,
            "warnings": ["确定性模拟面积统计，不代表真实 Sentinel-2 质量"],
            "contains_mock": True,
        }

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
        del aoi
        start_dt = datetime.fromisoformat(start).replace(tzinfo=UTC)
        targets = [
            datetime.fromisoformat(str(item["acquired_at"]).replace("Z", "+00:00"))
            for item in target_optical
        ]
        items = []
        for index in range(6):
            acquired = start_dt + timedelta(days=2 + index * 4)
            delta = min(
                (abs((acquired - item).total_seconds()) / 86400 for item in targets),
                default=0,
            )
            candidate_id = f"MOCK-S1-{start_dt:%Y%m}-{index + 1:02d}"
            items.append(
                {
                    "candidate_id": candidate_id,
                    "provider": "mock",
                    "dataset": "COPERNICUS/S1_GRD",
                    "product_id": f"MOCK_S1_GRD_{start_dt:%Y%m}_{index + 1:02d}",
                    "system_index": candidate_id,
                    "acquired_at": acquired.isoformat(),
                    "period_id": window_id or f"period-{start_dt:%Y%m}",
                    "data_kind": "sar",
                    "coverage_fraction": round(0.96 - index * 0.03, 4),
                    "scene_cloud_fraction": None,
                    "orbit_direction": "ASCENDING" if index % 2 == 0 else "DESCENDING",
                    "relative_orbit": 18 if index % 2 == 0 else 91,
                    "polarizations": ["VV", "VH"],
                    "matched_optical_candidate_id": (
                        str(target_optical[0]["candidate_id"]) if target_optical else None
                    ),
                    "delta_days": round(delta, 4),
                    "metadata": {
                        "instrument_mode": "IW",
                        "unit": "dB",
                        "data_level": "GRD-simulated",
                    },
                    "is_mock": True,
                }
            )
        selected = items[offset : offset + limit]
        return {
            "items": selected,
            "total_matches": len(items),
            "offset": offset,
            "limit": limit,
            "returned": len(selected),
            "has_more": offset + limit < len(items),
            "is_candidate_subset": offset + limit < len(items),
            "catalogue_truncated": False,
        }

    def create_download_request(
        self,
        *,
        scene: Mapping[str, Any],
        tile: Mapping[str, Any],
        approval: Mapping[str, Any],
    ) -> ProviderDownloadRequest:
        if approval.get("status") != "approved" or not approval.get("plan_hash"):
            raise ImageryProviderError(
                ProviderFailureKind.INVALID_REQUEST,
                "正式下载请求必须绑定已批准计划",
            )
        definition = {
            "scene": scene["candidate_id"],
            "file_id": tile["file_id"],
            "bands": tile.get("bands", []),
            "grid": tile.get("grid"),
            "plan_hash": approval["plan_hash"],
        }
        digest = hashlib.sha256(repr(sorted(definition.items())).encode()).hexdigest()
        return ProviderDownloadRequest(
            url=f"mock://download/{tile['file_id']}",
            provider="mock",
            tile_id=str(tile["file_id"]),
            content_definition_hash=digest,
            metadata={"scene": dict(scene), "tile": dict(tile)},
        )

    def iter_download(
        self, request: ProviderDownloadRequest, chunk_size: int = 16384
    ) -> tuple[int | None, Iterator[bytes]]:
        content = self._geotiff_bytes(request.metadata["scene"], request.metadata["tile"])

        def chunks() -> Iterator[bytes]:
            for offset in range(0, len(content), chunk_size):
                yield content[offset : offset + chunk_size]

        known = not bool(request.metadata["tile"].get("unknown_content_length"))
        return (len(content) if known else None), chunks()

    def _geotiff_bytes(self, scene: Mapping[str, Any], tile: Mapping[str, Any]) -> bytes:
        try:
            import numpy as np
            from affine import Affine
            from rasterio.io import MemoryFile
            from rasterio.transform import from_origin
        except ImportError as exc:
            raise ImageryProviderError(
                ProviderFailureKind.DEPENDENCY_MISSING,
                "生成 mock GeoTIFF 需要 imagery extra（numpy、rasterio）",
                cause=exc,
            ) from exc
        bands = list(tile.get("bands") or ["B2", "B3", "B4", "B8", "B11", "B12"])
        width = int((tile.get("grid") or {}).get("width") or 64)
        height = int((tile.get("grid") or {}).get("height") or 64)
        data_kind = tile.get("data_kind", "optical")
        seed = int(hashlib.sha256(str(scene["candidate_id"]).encode()).hexdigest()[:8], 16)
        dtype = "uint8" if data_kind == "quality" else "float32"
        arrays = []
        y, x = np.mgrid[0:height, 0:width]
        for index, _band in enumerate(bands):
            if dtype == "uint8":
                array = ((x + y + seed + index) % 12).astype(np.uint8)
            elif data_kind == "sar":
                array = (-18 + ((x + y + seed + index * 5) % 120) / 10).astype(np.float32)
            else:
                array = (((x * 3 + y * 5 + seed + index * 101) % 10000) / 10000).astype(
                    np.float32
                )
            arrays.append(array)
        data = np.stack(arrays)
        grid = tile.get("grid") or {}
        scale = float(grid.get("scale_meters", 20))
        transform_values = grid.get("transform")
        transform = (
            Affine(*[float(value) for value in transform_values])
            if transform_values
            else from_origin(500000, 3380000, scale, scale)
        )
        with MemoryFile() as memory:
            with memory.open(
                driver="GTiff",
                width=width,
                height=height,
                count=len(bands),
                dtype=dtype,
                crs=grid.get("crs", "EPSG:32650"),
                transform=transform,
                nodata=255 if dtype == "uint8" else -9999.0,
            ) as dataset:
                dataset.write(data)
                dataset.descriptions = tuple(bands)
                dataset.update_tags(
                    contains_mock="true",
                    source_candidate=str(scene["candidate_id"]),
                    data_kind=str(data_kind),
                )
            return memory.read()


def _clear_value(month: int, index: int) -> float:
    patterns = {
        0: (0.86, 0.72, 0.48, 0.40, 0.32, 0.28, 0.22, 0.18),
        1: (0.42, 0.35, 0.22, 0.20, 0.18, 0.15, 0.12, 0.10),
        2: (0.68, 0.51, 0.31, 0.28, 0.24, 0.20, 0.16, 0.12),
    }
    return patterns[month % 3][index % 8]


def _dependency_status() -> dict[str, bool]:
    import importlib.util

    return {
        "numpy": bool(importlib.util.find_spec("numpy")),
        "Pillow": bool(importlib.util.find_spec("PIL")),
        "rasterio": bool(importlib.util.find_spec("rasterio")),
    }
