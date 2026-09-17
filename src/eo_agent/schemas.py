from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskStatus(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    WAITING_INPUT = "WAITING_INPUT"
    REPORTING = "REPORTING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    UNSUPPORTED_TASK_TYPE = "UNSUPPORTED_TASK_TYPE"


class ToolStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRYABLE_ERROR = "retryable_error"


class ScientificValidity(StrEnum):
    NOT_EVALUATED = "not_evaluated"
    INSUFFICIENT = "insufficient"
    UNAVAILABLE = "unavailable"


class TimeWindow(StrictModel):
    start: date
    end: date
    label: str

    @model_validator(mode="after")
    def ordered(self) -> TimeWindow:
        if self.start >= self.end:
            raise ValueError("时间窗口必须为非空左闭右开区间")
        return self


class TaskRequest(StrictModel):
    query: str = Field(min_length=1, max_length=2000)
    aoi_id: str | None = None
    scenario: str = "cloudy"
    model_profile: str = "mock"


class TaskDraft(StrictModel):
    aoi_text: str | None = None
    baseline_month: str | None = None
    target_month: str | None = None
    task_type: str | None = None
    missing_fields: list[str] = Field(default_factory=list)


class TaskSpec(StrictModel):
    task_id: str
    query: str
    aoi_id: str
    aoi_name: str
    area_km2: float
    geometry_ref: str
    baseline: TimeWindow
    target: TimeWindow
    grid_meters: int
    seed: int
    budgets: dict[str, int]
    schema_version: str
    workflow_version: str
    model_profile: str


class SearchArgs(StrictModel):
    task_id: str
    aoi_id: str
    baseline: TimeWindow
    target: TimeWindow


class ResourceArgs(StrictModel):
    task_id: str
    resource_id: str


class VerifyArgs(ResourceArgs):
    evidence_round: int = Field(ge=0)


class ActionSpec(StrictModel):
    action: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason_summary: str = Field(max_length=300)


class ArtifactRef(StrictModel):
    artifact_id: str
    relative_path: str
    media_type: str
    checksum_sha256: str
    contains_mock: bool


class ObservationData(StrictModel):
    kind: Literal["observations"] = "observations"
    resource_id: str
    available: bool
    observation_count: int
    cloud_fraction: float | None
    evidence_round: int = 0


class PreparedData(StrictModel):
    kind: Literal["prepared"] = "prepared"
    resource_id: str
    source_resource_id: str
    usable: bool
    cloud_fraction: float | None
    evidence_round: int


class QualityData(StrictModel):
    kind: Literal["quality"] = "quality"
    resource_id: str
    usable: bool
    cloud_fraction: float | None
    reconstruction_recommended: bool


class ReconstructedData(StrictModel):
    kind: Literal["reconstructed"] = "reconstructed"
    resource_id: str
    source_resource_id: str
    method: Literal["mock_temporal_fill"] = "mock_temporal_fill"
    residual_cloud_fraction: float
    evidence_round: int


class ChangeData(StrictModel):
    kind: Literal["change"] = "change"
    resource_id: str
    source_resource_id: str
    change_area_km2: float | None
    changed_fraction: float | None
    can_determine: bool
    events: list[dict[str, str | float]]
    evidence_round: int


class VerificationData(StrictModel):
    kind: Literal["verification"] = "verification"
    resource_id: str
    sufficient: bool
    verdict: Literal["supported", "needs_evidence", "insufficient", "unavailable"]
    message: str
    evidence_round: int


ToolData = Annotated[
    ObservationData
    | PreparedData
    | QualityData
    | ReconstructedData
    | ChangeData
    | VerificationData,
    Field(discriminator="kind"),
]


class ToolResult(StrictModel):
    status: ToolStatus
    tool_name: str
    implementation_id: str
    schema_version: str = "1.0"
    data: ToolData | None = None
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    contains_mock: bool
    scientific_validity: ScientificValidity
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    provenance: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class TraceEvent(StrictModel):
    sequence: int
    task_id: str
    stage: str
    event_type: str
    implementation_id: str | None = None
    model_profile: str | None = None
    input_summary: dict[str, Any] = Field(default_factory=dict)
    output_summary: dict[str, Any] = Field(default_factory=dict)
    duration_ms: float
    error: str | None = None
    usage: dict[str, int | None] | None = None


class ReportFacts(StrictModel):
    task_id: str
    status: TaskStatus
    query: str
    aoi_name: str | None
    aoi_id: str | None
    baseline: TimeWindow | None
    target: TimeWindow | None
    model_profile: str
    contains_mock: bool
    notice: str
    steps: list[str]
    tool_implementations: list[str]
    change_area_km2: float | None
    changed_fraction: float | None
    change_events: list[dict[str, str | float]]
    quality_status: str
    verification_status: str
    conclusion: str
    limitations: list[str]
    tool_call_count: int
    llm_call_count: int
    artifacts: list[ArtifactRef]


class TaskRunResult(StrictModel):
    task_id: str
    status: TaskStatus
    contains_mock: bool
    steps: list[str]
    report_html: str | None
    report_json: str | None
    error: str | None = None
