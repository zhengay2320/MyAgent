from __future__ import annotations

from datetime import UTC, datetime

import pytest
import rasterio

from eo_agent.imagery.aoi import AOIValidationError, normalize_geojson
from eo_agent.imagery.download import validate_geotiff
from eo_agent.imagery.providers import EarthEngineProvider, MockImageryProvider
from eo_agent.imagery.quality import QualityAreaInputs, compute_quality_summary
from eo_agent.imagery.schemas import (
    AOISource,
    DataKind,
    DataProviderKind,
    DownloadFile,
    GridSpec,
    ImageryTaskRequest,
    ParsedRequest,
    SearchPlan,
)
from eo_agent.llm.mock import MockLLMAdapter

AOI = {
    "type": "Polygon",
    "coordinates": [
        [[114.28, 30.50], [114.36, 30.50], [114.36, 30.57], [114.28, 30.57], [114.28, 30.50]]
    ],
}


def test_aoi_validation_and_missing_time_are_explicit() -> None:
    aoi = normalize_geojson(AOI, source=AOISource.PASTED)
    assert aoi.geometry_hash
    assert 40 < aoi.area_km2 < 100
    with pytest.raises(AOIValidationError):
        normalize_geojson(
            {"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [0, 1], [1, 0], [0, 0]]]}
        )

    result = MockLLMAdapter().generate_structured(
        "imagery_parse_request",
        {
            "query": "准备武汉研究区影像",
            "requested_data": ["optical"],
            "user_timezone": "Asia/Shanghai",
            "aoi_provided": True,
        },
        ParsedRequest,
    ).payload
    assert result.periods == []
    assert "time_range" in result.missing_fields


def test_explicit_period_roles_are_not_reordered() -> None:
    result = MockLLMAdapter().generate_structured(
        "imagery_parse_request",
        {
            "query": "目标期2023年7月，基准期2024年7月，准备光学影像",
            "requested_data": ["optical"],
            "user_timezone": "Asia/Shanghai",
            "aoi_provided": True,
        },
        ParsedRequest,
    ).payload
    assert result.periods[0].start.isoformat() == "2024-07-01"
    assert result.periods[1].start.isoformat() == "2023-07-01"
    assert "明确" in result.role_interpretation


def test_quality_uses_aoi_and_valid_area_denominators() -> None:
    summary = compute_quality_summary(
        candidate_id="scene",
        values=QualityAreaInputs(
            aoi_area_m2=100,
            valid_area_m2=50,
            quality_assessed_area_m2=50,
            clear_area_m2=40,
            cloud_area_m2=5,
            shadow_area_m2=5,
            cloud_shadow_area_m2=10,
            method="unit-area",
            scale_meters=20,
            cloud_score_threshold=0.6,
            cloud_score_match_count=1,
            contains_mock=True,
        ),
    )
    assert summary.valid_coverage_fraction == 0.5
    assert summary.clear_aoi_fraction == 0.4
    assert summary.cloud_shadow_fraction_on_valid == 0.2


def test_mock_provider_generates_openable_aligned_geotiff(tmp_path) -> None:
    provider = MockImageryProvider()
    scene = provider.search_optical(
        aoi=AOI,
        start="2024-06-01",
        end="2024-07-01",
        limit=1,
        window_id="single",
    )["items"][0]
    grid = GridSpec(
        crs="EPSG:32650",
        scale_meters=20,
        width=32,
        height=24,
        transform=(20, 0, 238920, 0, -20, 3385100),
        nodata=-9999,
    )
    item = DownloadFile(
        file_id="mock-file",
        candidate_id=scene["candidate_id"],
        data_kind=DataKind.OPTICAL,
        period_id="single",
        relative_path="optical/mock-file.tif",
        bands=["B2", "B3", "B4"],
        grid=grid,
        media_type="image/tiff",
        contains_mock=True,
    )
    request = provider.create_download_request(
        scene=scene,
        tile={**item.model_dump(mode="json"), "region": AOI},
        approval={"status": "approved", "plan_hash": "a" * 64},
    )
    _total, chunks = provider.iter_download(request, 97)
    destination = tmp_path / "mock.tif"
    destination.write_bytes(b"".join(chunks))
    result = validate_geotiff(destination, item)
    assert result["passed"] is True
    assert result["bands_match_plan"] is True

    with rasterio.open(destination, "r+") as dataset:
        dataset.descriptions = tuple(None for _ in range(dataset.count))
    without_descriptions = validate_geotiff(destination, item)
    assert without_descriptions["passed"] is True
    assert any("未嵌入波段描述" in value for value in without_descriptions["warnings"])


def test_synthetic_aoi_is_rejected_for_real_search_plan() -> None:
    aoi = normalize_geojson(AOI, source=AOISource.SYNTHETIC_MOCK)
    parsed = MockLLMAdapter().generate_structured(
        "imagery_parse_request",
        {
            "query": "准备2024年6月光学影像",
            "requested_data": ["optical"],
            "user_timezone": "Asia/Shanghai",
            "aoi_provided": True,
        },
        ParsedRequest,
    ).payload
    with pytest.raises(ValueError, match="合成测试 AOI"):
        SearchPlan(
            task_id="task",
            request_version=1,
            aoi=aoi,
            parsed_request=parsed,
            provider=DataProviderKind.EARTH_ENGINE,
            created_at=datetime.now(UTC),
            contains_mock=False,
        )


def test_gee_doctor_does_not_initialize_without_remote_check() -> None:
    class FailingEE:
        def Initialize(self, **_kwargs):  # pragma: no cover - must never be called
            raise AssertionError("doctor default must not initialize")

    provider = EarthEngineProvider(project_id="test-project", ee_module=FailingEE())
    result = provider.doctor(check_remote=False)
    assert result["status"] == "local_ready"
    assert result["remote_checked"] is False


def test_gee_download_request_uses_approved_exact_grid_and_never_logs_url() -> None:
    captured: dict = {}

    class Image:
        def select(self, _bands):
            return self

        def resample(self, _method):
            return self

        def multiply(self, _value):
            return self

        def toFloat(self):
            return self

        def clip(self, geometry):
            captured["clip"] = geometry
            return self

        def getDownloadURL(self, parameters):
            captured["parameters"] = parameters
            return "https://earthengine.googleapis.com/download?id=signed-secret"

    class EE:
        @staticmethod
        def Image(_asset):
            return Image()

        @staticmethod
        def Geometry(value):
            return value

    provider = EarthEngineProvider(project_id="test-project", ee_module=EE())
    provider._initialized = True
    request = provider.create_download_request(
        scene={
            "dataset": "COPERNICUS/S2_SR_HARMONIZED",
            "system_index": "20240601T000000_TEST",
        },
        tile={
            "file_id": "tile-1",
            "data_kind": "optical",
            "bands": ["B2", "B3", "B4"],
            "grid": {
                "crs": "EPSG:32650",
                "width": 32,
                "height": 24,
                "transform": [20, 0, 238920, 0, -20, 3385100],
            },
            "region": AOI,
            "plan_hash": "a" * 64,
            "plan_version": 2,
        },
        approval={
            "status": "approved",
            "plan_hash": "a" * 64,
            "plan_version": 2,
        },
    )
    assert captured["parameters"]["dimensions"] == [32, 24]
    assert captured["parameters"]["crs_transform"] == [20, 0, 238920, 0, -20, 3385100]
    assert "region" not in captured["parameters"]
    assert captured["clip"] == AOI
    assert captured["parameters"]["format"] == "GEO_TIFF"
    assert "signed-secret" not in repr(request)
    assert "signed-secret" not in str(request.to_log_dict())


def test_request_requires_optical_first() -> None:
    with pytest.raises(ValueError, match="optical"):
        ImageryTaskRequest(query="SAR only", requested_data=[DataKind.SAR])
