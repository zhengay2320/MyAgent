from __future__ import annotations

import json
import math
import re
from datetime import UTC, date, datetime
from enum import StrEnum
from hashlib import sha256
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


class ImageryModel(BaseModel):
    """Base for persisted imagery records; silently ignored fields are unsafe here."""

    model_config = ConfigDict(extra="forbid")


class ImageryTaskStatus(StrEnum):
    CREATED = "CREATED"
    PARSING = "PARSING"
    WAITING_INPUT = "WAITING_INPUT"
    SEARCHING_OPTICAL = "SEARCHING_OPTICAL"
    PREVIEWING = "PREVIEWING"
    ASSESSING_QUALITY = "ASSESSING_QUALITY"
    PLANNING = "PLANNING"
    SEARCHING_SAR = "SEARCHING_SAR"
    WAITING_DOWNLOAD_APPROVAL = "WAITING_DOWNLOAD_APPROVAL"
    DOWNLOADING = "DOWNLOADING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"


class DataProviderKind(StrEnum):
    MOCK = "mock"
    EARTH_ENGINE = "gee"


class DataKind(StrEnum):
    OPTICAL = "optical"
    SAR = "sar"
    QUALITY = "quality"
    METADATA = "metadata"


class AOISource(StrEnum):
    UPLOAD = "upload"
    PASTED = "pasted"
    REGISTERED = "registered"
    SYNTHETIC_MOCK = "synthetic_mock"


class QueryMode(StrEnum):
    SINGLE_RANGE = "single_range"
    TWO_PERIOD_COMPARISON = "two_period_comparison"


class PeriodRole(StrEnum):
    SINGLE = "single"
    BASELINE = "baseline"
    TARGET = "target"
    REFERENCE = "reference"


class QualityStatus(StrEnum):
    VALID = "valid"
    INSUFFICIENT_COVERAGE = "insufficient_coverage"
    INSUFFICIENT_CLEAR = "insufficient_clear"
    UNKNOWN = "unknown"
    FAILED = "failed"


class SarRecommendationStatus(StrEnum):
    NOT_NEEDED = "not_needed"
    RECOMMENDED = "recommended"
    QUALITY_UNKNOWN = "quality_unknown"
    UNAVAILABLE = "unavailable"


class DownloadFileStatus(StrEnum):
    PLANNED = "planned"
    PREPARING = "preparing"
    DOWNLOADING = "downloading"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    SKIPPED = "skipped"


class ApprovalStatus(StrEnum):
    APPROVED = "approved"
    INVALIDATED = "invalidated"
    CANCELLED = "cancelled"


class LLMCallStatus(StrEnum):
    REQUESTED = "requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    VALIDATION_FAILED = "validation_failed"


class PolygonGeometry(ImageryModel):
    type: Literal["Polygon"] = "Polygon"
    coordinates: list[list[tuple[float, float]]]


class MultiPolygonGeometry(ImageryModel):
    type: Literal["MultiPolygon"] = "MultiPolygon"
    coordinates: list[list[list[tuple[float, float]]]]


WGS84Geometry = Annotated[
    PolygonGeometry | MultiPolygonGeometry,
    Field(discriminator="type"),
]


class SearchPeriod(ImageryModel):
    period_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    label: str = Field(min_length=1, max_length=120)
    start: date
    end: date
    role: PeriodRole
    original_expression: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def validate_left_closed_interval(self) -> SearchPeriod:
        if self.start >= self.end:
            raise ValueError("检索时间必须是非空的左闭右开区间")
        return self


class ImageryTaskRequest(ImageryModel):
    query: str = Field(min_length=1, max_length=4000)
    model_profile: str = Field(default="mock", min_length=1, max_length=100)
    provider: DataProviderKind = DataProviderKind.MOCK
    aoi_geojson: dict[str, Any] | None = None
    aoi_id: str | None = Field(default=None, min_length=1, max_length=128)
    aoi_name: str | None = Field(default=None, max_length=200)
    requested_data: list[DataKind] = Field(default_factory=lambda: [DataKind.OPTICAL])
    user_timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=80)

    @model_validator(mode="after")
    def validate_aoi_choice_and_data(self) -> ImageryTaskRequest:
        if self.aoi_geojson is not None and self.aoi_id is not None:
            raise ValueError("aoi_geojson 与 aoi_id 只能选择一种")
        if not self.requested_data:
            raise ValueError("至少需要一种数据类型")
        if DataKind.OPTICAL not in self.requested_data:
            raise ValueError("V1 必须先准备 optical 数据")
        return self


class ParsedRequest(ImageryModel):
    task_summary: str = Field(min_length=1, max_length=1000)
    query_mode: QueryMode
    periods: list[SearchPeriod] = Field(default_factory=list, max_length=4)
    requested_data: list[DataKind]
    original_query: str = Field(min_length=1, max_length=4000)
    original_timezone: str = Field(min_length=1, max_length=80)
    query_timezone: Literal["UTC"] = "UTC"
    role_interpretation: str = Field(min_length=1, max_length=1000)
    needs_aoi: bool
    missing_fields: list[str] = Field(default_factory=list)
    extension_suggestions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_period_shape(self) -> ParsedRequest:
        ids = [period.period_id for period in self.periods]
        if len(ids) != len(set(ids)):
            raise ValueError("period_id 必须唯一")
        if not self.periods:
            if "time_range" not in self.missing_fields:
                raise ValueError("没有解析到时期时必须明确缺少 time_range")
            return self
        if self.query_mode == QueryMode.SINGLE_RANGE and len(self.periods) != 1:
            raise ValueError("single_range 必须且只能包含一个时期")
        if self.query_mode == QueryMode.TWO_PERIOD_COMPARISON:
            if len(self.periods) != 2:
                raise ValueError("two_period_comparison 必须包含两个时期")
            roles = {period.role for period in self.periods}
            if roles != {PeriodRole.BASELINE, PeriodRole.TARGET}:
                raise ValueError("两时期对比必须明确 baseline 和 target")
        if DataKind.OPTICAL not in self.requested_data:
            raise ValueError("解析结果必须先请求 optical 数据")
        return self


class AOIRef(ImageryModel):
    aoi_id: str = Field(min_length=1, max_length=128)
    version: int = Field(default=1, ge=1)
    name: str = Field(min_length=1, max_length=200)
    geometry: WGS84Geometry
    crs: Literal["EPSG:4326"] = "EPSG:4326"
    bbox: tuple[float, float, float, float]
    area_km2: float = Field(gt=0)
    vertex_count: int = Field(ge=4)
    geometry_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: AOISource
    is_synthetic: bool = False
    within_recommended_area: bool
    requires_area_adjustment: bool = False
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_bounds_and_provenance(self) -> AOIRef:
        min_lon, min_lat, max_lon, max_lat = self.bbox
        if not (-180 <= min_lon <= max_lon <= 180):
            raise ValueError("AOI bbox 经度超出 WGS84 范围")
        if not (-90 <= min_lat <= max_lat <= 90):
            raise ValueError("AOI bbox 纬度超出 WGS84 范围")
        if self.source == AOISource.SYNTHETIC_MOCK and not self.is_synthetic:
            raise ValueError("synthetic_mock AOI 必须明确 is_synthetic=true")
        if self.is_synthetic and self.source != AOISource.SYNTHETIC_MOCK:
            raise ValueError("合成 AOI 必须使用 synthetic_mock 来源")
        return self


class SearchPlan(ImageryModel):
    task_id: str = Field(min_length=1, max_length=128)
    request_version: int = Field(ge=1)
    aoi: AOIRef
    parsed_request: ParsedRequest
    provider: DataProviderKind
    optical_dataset: Literal["COPERNICUS/S2_SR_HARMONIZED"] = (
        "COPERNICUS/S2_SR_HARMONIZED"
    )
    quality_dataset: Literal["GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED"] = (
        "GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED"
    )
    sar_dataset: Literal["COPERNICUS/S1_GRD"] = "COPERNICUS/S1_GRD"
    cloud_score_band: Literal["cs_cdf"] = "cs_cdf"
    cloud_score_clear_threshold: float = Field(default=0.60, ge=0, le=1)
    quality_scale_meters: int = Field(default=20, ge=10, le=1000)
    candidate_display_limit_per_period: int = Field(default=5, ge=1, le=100)
    catalog_limit_per_period: int = Field(default=100, ge=1, le=1000)
    initial_quality_limit_per_period: int = Field(default=10, ge=1, le=100)
    created_at: datetime
    contains_mock: bool

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        return _as_utc(value, "created_at")

    @model_validator(mode="after")
    def validate_limits_and_provider(self) -> SearchPlan:
        if not self.parsed_request.periods or "time_range" in self.parsed_request.missing_fields:
            raise ValueError("缺少授权时间范围时不能创建检索计划")
        if self.parsed_request.needs_aoi:
            raise ValueError("解析结果仍缺 AOI 时不能创建检索计划")
        if self.aoi.requires_area_adjustment:
            raise ValueError("AOI 超过普通模式面积上限，必须由用户调整后再检索")
        if self.candidate_display_limit_per_period > self.catalog_limit_per_period:
            raise ValueError("展示数量不能超过目录检索上限")
        if self.initial_quality_limit_per_period > self.catalog_limit_per_period:
            raise ValueError("初始质检数量不能超过目录检索上限")
        if self.provider == DataProviderKind.MOCK and not self.contains_mock:
            raise ValueError("mock provider 的检索计划必须保留模拟标记")
        if self.provider == DataProviderKind.EARTH_ENGINE and self.aoi.is_synthetic:
            raise ValueError("合成测试 AOI 不得用于真实 Earth Engine 检索")
        return self


class SceneCandidate(ImageryModel):
    candidate_id: str = Field(min_length=1, max_length=256)
    provider: DataProviderKind
    dataset: str = Field(min_length=1, max_length=200)
    product_id: str = Field(min_length=1, max_length=512)
    system_index: str = Field(min_length=1, max_length=256)
    acquired_at: datetime
    period_id: str = Field(
        min_length=1,
        max_length=64,
        validation_alias=AliasChoices("period_id", "window_id"),
    )
    data_kind: Literal[DataKind.OPTICAL, DataKind.SAR]
    coverage_fraction: float | None = Field(default=None, ge=0, le=1)
    scene_cloud_fraction: float | None = Field(default=None, ge=0, le=1)
    mgrs_tile: str | None = Field(default=None, max_length=32)
    orbit_direction: Literal["ASCENDING", "DESCENDING"] | None = None
    relative_orbit: int | None = Field(default=None, ge=1)
    polarizations: list[Literal["VV", "VH", "HH", "HV"]] = Field(default_factory=list)
    matched_optical_candidate_id: str | None = Field(default=None, max_length=256)
    delta_days: float | None = Field(default=None, ge=0)
    preview_artifact_id: str | None = Field(default=None, max_length=256)
    quality_overlay_artifact_id: str | None = Field(default=None, max_length=256)
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    is_mock: bool

    @model_validator(mode="after")
    def validate_sensor_specific_fields(self) -> SceneCandidate:
        if self.acquired_at.tzinfo is None:
            raise ValueError("采集时间必须包含时区")
        self.acquired_at = self.acquired_at.astimezone(UTC)
        if self.provider == DataProviderKind.MOCK and not self.is_mock:
            raise ValueError("mock 候选必须明确 is_mock=true")
        if self.data_kind == DataKind.SAR:
            if not self.polarizations:
                raise ValueError("SAR 候选必须声明极化方式")
            if self.scene_cloud_fraction is not None:
                raise ValueError("SAR 候选不使用整景云量")
        elif any(value is not None for value in (self.orbit_direction, self.relative_orbit)):
            raise ValueError("光学候选不应包含 SAR 轨道字段")
        return self

    @property
    def window_id(self) -> str:
        """Compatibility name used by catalog providers."""

        return self.period_id


class QualitySummary(ImageryModel):
    candidate_id: str = Field(min_length=1, max_length=256)
    status: QualityStatus
    valid_coverage_fraction: float | None = Field(default=None, ge=0, le=1)
    clear_aoi_fraction: float | None = Field(default=None, ge=0, le=1)
    cloud_shadow_fraction_on_valid: float | None = Field(default=None, ge=0, le=1)
    quality_assessed_fraction: float | None = Field(default=None, ge=0, le=1)
    cloud_fraction_on_valid: float | None = Field(default=None, ge=0, le=1)
    shadow_fraction_on_valid: float | None = Field(default=None, ge=0, le=1)
    snow_fraction_on_valid: float | None = Field(default=None, ge=0, le=1)
    nodata_fraction_of_aoi: float | None = Field(default=None, ge=0, le=1)
    saturated_fraction_on_valid: float | None = Field(default=None, ge=0, le=1)
    aoi_area_m2: float | None = Field(default=None, gt=0)
    areas_m2: dict[str, float | None] = Field(default_factory=dict)
    denominators: dict[str, str] = Field(
        default_factory=lambda: {
            "valid_coverage_fraction": "aoi_area_m2",
            "clear_aoi_fraction": "aoi_area_m2",
            "cloud_shadow_fraction_on_valid": "valid_area_m2",
            "quality_assessed_fraction": "aoi_area_m2",
        }
    )
    method: str = Field(min_length=1, max_length=300)
    threshold: float | None = Field(default=None, ge=0, le=1)
    scale_meters: int = Field(ge=1)
    cloud_score_match_count: int = Field(default=0, ge=0)
    reason: str | None = Field(default=None, max_length=1000)
    warnings: list[str] = Field(default_factory=list)
    contains_mock: bool

    @field_validator("areas_m2")
    @classmethod
    def validate_area_totals(cls, values: dict[str, float | None]) -> dict[str, float | None]:
        for name, value in values.items():
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError(f"areas_m2.{name} 必须是非负有限数值或 null")
        return values

    @model_validator(mode="after")
    def validate_metric_meaning(self) -> QualitySummary:
        if self.status != QualityStatus.VALID and not self.reason:
            raise ValueError("非 valid 质量状态必须说明原因")
        required = (
            self.valid_coverage_fraction,
            self.clear_aoi_fraction,
            self.cloud_shadow_fraction_on_valid,
            self.quality_assessed_fraction,
        )
        if self.status == QualityStatus.VALID and any(value is None for value in required):
            raise ValueError("valid 质量结果必须包含四项核心指标")
        if self.valid_coverage_fraction == 0 and self.cloud_shadow_fraction_on_valid is not None:
            raise ValueError("无有效覆盖时云影比例的分母为零，必须为 null")
        if self.cloud_fraction_on_valid is not None and self.shadow_fraction_on_valid is not None:
            combined = self.cloud_fraction_on_valid + self.shadow_fraction_on_valid
            if self.cloud_shadow_fraction_on_valid is not None and not math.isclose(
                combined,
                self.cloud_shadow_fraction_on_valid,
                abs_tol=1e-6,
            ):
                raise ValueError("云与阴影分项必须和合并比例一致")
        return self


class SarRecommendation(ImageryModel):
    status: SarRecommendationStatus
    rationale: str = Field(min_length=1, max_length=1200)
    considered_alternative_optical_ids: list[str] = Field(default_factory=list)
    unresolved_quality_candidate_ids: list[str] = Field(default_factory=list)
    recommended_period_ids: list[str] = Field(default_factory=list)
    rule_summary: str = Field(min_length=1, max_length=500)


class SceneRecommendation(ImageryModel):
    selected_optical_candidate_ids: list[str] = Field(default_factory=list)
    selected_sar_candidate_ids: list[str] = Field(default_factory=list)
    rejected_candidates: dict[str, str] = Field(default_factory=dict)
    missing_information: list[str] = Field(default_factory=list)
    sar_recommendation: SarRecommendation
    rationale: str = Field(min_length=1, max_length=2000)
    accepted_by_program: bool = False
    validation_errors: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_selections(self) -> SceneRecommendation:
        selected = self.selected_optical_candidate_ids + self.selected_sar_candidate_ids
        if len(selected) != len(set(selected)):
            raise ValueError("同一候选不能在推荐清单中重复")
        if self.accepted_by_program and self.validation_errors:
            raise ValueError("包含校验错误的模型建议不能标为已接受")
        return self


class GridSpec(ImageryModel):
    crs: str = Field(default="EPSG:32650", min_length=1, max_length=80)
    scale_meters: float = Field(default=20, gt=0)
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    transform: tuple[float, float, float, float, float, float] | None = None
    nodata: float | int | None = None


class FileVerification(ImageryModel):
    passed: bool
    size_bytes: int = Field(ge=0)
    checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type_valid: bool
    geotiff_opened: bool | None = None
    extent_matches_plan: bool | None = None
    bands_match_plan: bool | None = None
    grid_matches_plan: bool | None = None
    all_nodata: bool | None = None
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_outcome(self) -> FileVerification:
        if self.passed and self.errors:
            raise ValueError("包含校验错误的文件不能标为 passed")
        if not self.passed and not self.errors:
            raise ValueError("未通过的文件必须记录校验错误")
        if self.all_nodata is True and self.passed:
            raise ValueError("全 nodata 文件不能通过完成校验")
        return self


class DownloadFile(ImageryModel):
    file_id: str = Field(min_length=1, max_length=128)
    candidate_id: str = Field(min_length=1, max_length=256)
    data_kind: DataKind
    period_id: str = Field(min_length=1, max_length=64)
    relative_path: str = Field(min_length=1, max_length=500)
    bands: list[str] = Field(default_factory=list)
    grid: GridSpec | None = None
    media_type: str = Field(min_length=1, max_length=100)
    expected_size_bytes: int | None = Field(default=None, ge=0)
    expected_size_method: str | None = Field(default=None, max_length=300)
    required: bool = True
    status: DownloadFileStatus = DownloadFileStatus.PLANNED
    received_bytes: int = Field(default=0, ge=0)
    response_total_bytes: int | None = Field(default=None, ge=0)
    transfer_rate_bytes_per_second: float | None = Field(default=None, ge=0)
    checksum_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    verification: FileVerification | None = None
    local_thumbnail_relative_path: str | None = Field(default=None, max_length=500)
    error: str | None = Field(default=None, max_length=2000)
    contains_mock: bool

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        path = PurePosixPath(normalized)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError("下载文件必须使用安全的相对路径")
        if re.match(r"^[A-Za-z]:", normalized):
            raise ValueError("下载文件不能包含盘符")
        return str(path)


class DownloadPlan(ImageryModel):
    task_id: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1)
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    aoi_version: int = Field(ge=1)
    periods: list[SearchPeriod] = Field(min_length=1)
    selected_candidate_ids: list[str] = Field(min_length=1)
    bands: dict[str, list[str]]
    grid: GridSpec
    output_root: str = Field(min_length=1, max_length=1000)
    resolved_task_dir: str = Field(min_length=1, max_length=1200)
    provider: DataProviderKind
    contains_mock: bool
    files: list[DownloadFile] = Field(min_length=1)
    estimated_total_bytes: int | None = Field(default=None, ge=0)
    estimate_method: str | None = Field(default=None, max_length=300)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        return _as_utc(value, "created_at")

    @model_validator(mode="after")
    def validate_bound_content(self) -> DownloadPlan:
        selected = set(self.selected_candidate_ids)
        if len(selected) != len(self.selected_candidate_ids):
            raise ValueError("selected_candidate_ids 不得重复")
        file_ids = [item.file_id for item in self.files]
        if len(file_ids) != len(set(file_ids)):
            raise ValueError("DownloadFile.file_id 必须唯一")
        if any(item.candidate_id not in selected for item in self.files):
            raise ValueError("下载文件只能引用已选择候选")
        if set(self.bands) != selected:
            raise ValueError("bands 必须逐一绑定全部所选候选")
        if self.provider == DataProviderKind.MOCK and not self.contains_mock:
            raise ValueError("mock 下载计划必须保留模拟标记")
        if not _looks_absolute_backend_path(self.resolved_task_dir):
            raise ValueError("resolved_task_dir 必须是后端机器上的绝对路径")
        return self

    def content_for_hash(self) -> dict[str, Any]:
        """Return the approval-bound content, excluding mutable transfer state."""

        payload = self.model_dump(mode="json", exclude={"plan_hash", "created_at"})
        for item in payload["files"]:
            item.pop("status", None)
            item.pop("received_bytes", None)
            item.pop("response_total_bytes", None)
            item.pop("transfer_rate_bytes_per_second", None)
            item.pop("checksum_sha256", None)
            item.pop("verification", None)
            item.pop("local_thumbnail_relative_path", None)
            item.pop("error", None)
        return payload

    def computed_hash(self) -> str:
        canonical = json.dumps(
            self.content_for_hash(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return sha256(canonical.encode("utf-8")).hexdigest()

    def assert_hash_matches(self) -> None:
        if self.plan_hash != self.computed_hash():
            raise ValueError("plan_hash 与审批绑定内容不一致")


class DownloadApprovalRequest(ImageryModel):
    plan_version: int = Field(ge=1)
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=8, max_length=200)


class DownloadPlanUpdateRequest(ImageryModel):
    plan_version: int = Field(ge=1)
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_candidate_ids: list[str] = Field(min_length=1)
    selected_file_ids: list[str] = Field(min_length=1)
    download_root: str = Field(min_length=1, max_length=1000)
    grid_meters: float = Field(gt=0)
    optical_bands: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_selections(self) -> DownloadPlanUpdateRequest:
        if len(self.selected_candidate_ids) != len(set(self.selected_candidate_ids)):
            raise ValueError("selected_candidate_ids 不得重复")
        if len(self.selected_file_ids) != len(set(self.selected_file_ids)):
            raise ValueError("selected_file_ids 不得重复")
        if len(self.optical_bands) != len(set(self.optical_bands)):
            raise ValueError("optical_bands 不得重复")
        return self


class ApprovalRecord(ImageryModel):
    approval_id: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=128)
    plan_version: int = Field(ge=1)
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=8, max_length=200)
    selected_candidate_ids: list[str] = Field(min_length=1)
    selected_file_ids: list[str] = Field(min_length=1)
    bands: dict[str, list[str]]
    grid: GridSpec
    output_root: str = Field(min_length=1, max_length=1000)
    resolved_task_dir: str = Field(min_length=1, max_length=1200)
    status: ApprovalStatus = ApprovalStatus.APPROVED
    approved_at: datetime

    @field_validator("approved_at")
    @classmethod
    def normalize_approved_at(cls, value: datetime) -> datetime:
        return _as_utc(value, "approved_at")

    @model_validator(mode="after")
    def validate_approval_path(self) -> ApprovalRecord:
        if not _looks_absolute_backend_path(self.resolved_task_dir):
            raise ValueError("审批记录必须保存解析后的后端绝对路径")
        return self


class InteractionEvent(ImageryModel):
    task_id: str = Field(min_length=1, max_length=128)
    sequence: int = Field(ge=1)
    timestamp: datetime
    event_type: str = Field(min_length=1, max_length=100)
    stage: ImageryTaskStatus
    summary: str = Field(min_length=1, max_length=500)
    call_id: str | None = Field(default=None, max_length=128)
    action_id: str | None = Field(default=None, max_length=128)
    file_id: str | None = Field(default=None, max_length=128)
    plan_version: int | None = Field(default=None, ge=1)
    details_artifact_id: str | None = Field(default=None, max_length=256)
    details: dict[str, Any] | None = None

    @model_validator(mode="after")
    def normalize_timestamp(self) -> InteractionEvent:
        if self.timestamp.tzinfo is None:
            raise ValueError("事件时间必须包含时区")
        self.timestamp = self.timestamp.astimezone(UTC)
        return self


class LLMMessage(ImageryModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = Field(max_length=200_000)
    name: str | None = Field(default=None, max_length=100)


class LLMValidationResult(ImageryModel):
    parsed: bool
    schema_valid: bool
    parameter_valid: bool
    errors: list[str] = Field(default_factory=list)


class ExecutedToolAction(ImageryModel):
    tool_name: str = Field(min_length=1, max_length=150)
    parameters: dict[str, Any]
    accepted: bool
    rejection_reason: str | None = Field(default=None, max_length=1000)
    result_summary: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_action_disposition(self) -> ExecutedToolAction:
        if not self.accepted and not self.rejection_reason:
            raise ValueError("被拒绝的动作必须记录原因")
        return self


class LLMCallRecord(ImageryModel):
    task_id: str = Field(min_length=1, max_length=128)
    call_id: str = Field(min_length=1, max_length=128)
    attempt: int = Field(ge=1)
    purpose: str = Field(min_length=1, max_length=500)
    model_profile: str = Field(min_length=1, max_length=100)
    model_id: str = Field(min_length=1, max_length=200)
    prompt_version: str = Field(min_length=1, max_length=100)
    messages: list[LLMMessage] = Field(min_length=1)
    response_schema: dict[str, Any]
    visible_context: dict[str, Any]
    effective_parameters: dict[str, str | int | float | bool | None]
    candidate_data: list[dict[str, Any]] = Field(default_factory=list)
    streaming: bool
    status: LLMCallStatus
    raw_response: str | None = Field(default=None, max_length=500_000)
    parsed_output: dict[str, Any] | None = None
    finish_reason: str | None = Field(default=None, max_length=100)
    returned_model: str | None = Field(default=None, max_length=200)
    duration_ms: float | None = Field(default=None, ge=0)
    usage: dict[str, int | None] | None = None
    validation: LLMValidationResult | None = None
    recommendation_accepted: bool | None = None
    recommendation_rejection_reasons: list[str] = Field(default_factory=list)
    executed_actions: list[ExecutedToolAction] = Field(default_factory=list)
    requested_at: datetime
    completed_at: datetime | None = None
    error: str | None = Field(default=None, max_length=4000)
    contains_mock: bool

    @model_validator(mode="after")
    def validate_call_lifecycle(self) -> LLMCallRecord:
        if self.requested_at.tzinfo is None:
            raise ValueError("LLM 请求时间必须包含时区")
        self.requested_at = self.requested_at.astimezone(UTC)
        if self.completed_at is not None:
            if self.completed_at.tzinfo is None:
                raise ValueError("LLM 完成时间必须包含时区")
            self.completed_at = self.completed_at.astimezone(UTC)
        if self.status == LLMCallStatus.SUCCEEDED:
            if self.raw_response is None or self.validation is None:
                raise ValueError("成功调用必须保存实际响应和校验结果")
            if not self.validation.schema_valid or not self.validation.parameter_valid:
                raise ValueError("校验未通过的调用不能标为 succeeded")
        if self.status in {LLMCallStatus.FAILED, LLMCallStatus.VALIDATION_FAILED}:
            if not self.error and not self.recommendation_rejection_reasons:
                raise ValueError("失败调用必须记录错误")
        if self.recommendation_accepted and self.recommendation_rejection_reasons:
            raise ValueError("已接受建议不能同时存在拒绝原因")
        return self


def _looks_absolute_backend_path(value: str) -> bool:
    normalized = value.replace("\\", "/")
    return normalized.startswith("/") or bool(re.match(r"^[A-Za-z]:/", normalized))


def _as_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field_name} 必须包含时区")
    return value.astimezone(UTC)


# Compatibility aliases used by provider- and UI-facing modules.
ImageryCandidate = SceneCandidate
QualityMetrics = QualitySummary
ImageryEvent = InteractionEvent
