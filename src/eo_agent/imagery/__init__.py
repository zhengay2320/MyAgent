"""Interactive imagery preparation contracts and pure-Python helpers."""

from eo_agent.imagery.aoi import AOIValidationError, normalize_geojson
from eo_agent.imagery.quality import QualityAreaInputs, compute_quality_summary, safe_fraction
from eo_agent.imagery.schemas import (
    AOIRef,
    ApprovalRecord,
    DownloadFile,
    DownloadPlan,
    DownloadPlanUpdateRequest,
    ImageryTaskRequest,
    ImageryTaskStatus,
    InteractionEvent,
    LLMCallRecord,
    ParsedRequest,
    QualitySummary,
    SarRecommendation,
    SceneCandidate,
    SceneRecommendation,
    SearchPlan,
)

__all__ = [
    "AOIRef",
    "AOIValidationError",
    "ApprovalRecord",
    "DownloadFile",
    "DownloadPlan",
    "DownloadPlanUpdateRequest",
    "ImageryTaskRequest",
    "ImageryTaskStatus",
    "InteractionEvent",
    "LLMCallRecord",
    "ParsedRequest",
    "QualityAreaInputs",
    "QualitySummary",
    "SarRecommendation",
    "SceneCandidate",
    "SceneRecommendation",
    "SearchPlan",
    "compute_quality_summary",
    "normalize_geojson",
    "safe_fraction",
]
