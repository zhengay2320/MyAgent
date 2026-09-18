from __future__ import annotations

import math

from pydantic import Field, model_validator

from eo_agent.imagery.schemas import ImageryModel, QualityStatus, QualitySummary


class QualityAreaInputs(ImageryModel):
    """Area totals returned by a quality reducer, all in square metres.

    Optional values are genuinely unknown. Callers must not replace absent quality
    masks, failed reducers, or zero-denominator ratios with numeric zero.
    """

    aoi_area_m2: float = Field(gt=0)
    valid_area_m2: float | None = Field(default=None, ge=0)
    clear_area_m2: float | None = Field(default=None, ge=0)
    quality_assessed_area_m2: float | None = Field(default=None, ge=0)
    cloud_area_m2: float | None = Field(default=None, ge=0)
    shadow_area_m2: float | None = Field(default=None, ge=0)
    cloud_shadow_area_m2: float | None = Field(default=None, ge=0)
    snow_area_m2: float | None = Field(default=None, ge=0)
    nodata_area_m2: float | None = Field(default=None, ge=0)
    saturated_area_m2: float | None = Field(default=None, ge=0)
    method: str = Field(min_length=1, max_length=300)
    cloud_score_threshold: float | None = Field(default=None, ge=0, le=1)
    scale_meters: int = Field(ge=1)
    cloud_score_match_count: int = Field(default=0, ge=0)
    failure_reason: str | None = Field(default=None, max_length=1000)
    warnings: list[str] = Field(default_factory=list)
    contains_mock: bool

    @model_validator(mode="after")
    def validate_area_relationships(self) -> QualityAreaInputs:
        area_fields = {
            "valid_area_m2": self.valid_area_m2,
            "clear_area_m2": self.clear_area_m2,
            "quality_assessed_area_m2": self.quality_assessed_area_m2,
            "cloud_area_m2": self.cloud_area_m2,
            "shadow_area_m2": self.shadow_area_m2,
            "cloud_shadow_area_m2": self.cloud_shadow_area_m2,
            "snow_area_m2": self.snow_area_m2,
            "nodata_area_m2": self.nodata_area_m2,
            "saturated_area_m2": self.saturated_area_m2,
        }
        tolerance = max(1e-6, self.aoi_area_m2 * 1e-9)
        for field_name, value in area_fields.items():
            if value is not None and not math.isfinite(value):
                raise ValueError(f"{field_name} 必须是有限数值")
            if value is not None and value > self.aoi_area_m2 + tolerance:
                raise ValueError(f"{field_name} 不能超过 AOI 面积")
        if self.valid_area_m2 is not None:
            valid_limited = (
                "clear_area_m2",
                "cloud_area_m2",
                "shadow_area_m2",
                "cloud_shadow_area_m2",
                "snow_area_m2",
                "saturated_area_m2",
            )
            for field_name in valid_limited:
                value = getattr(self, field_name)
                if value is not None and value > self.valid_area_m2 + tolerance:
                    raise ValueError(f"{field_name} 不能超过有效覆盖面积")
        if self.cloud_shadow_area_m2 is not None:
            if self.cloud_area_m2 is not None and self.shadow_area_m2 is not None:
                separated = self.cloud_area_m2 + self.shadow_area_m2
                if not math.isclose(
                    self.cloud_shadow_area_m2,
                    separated,
                    rel_tol=1e-8,
                    abs_tol=tolerance,
                ):
                    raise ValueError("cloud_shadow_area_m2 必须等于云和阴影分项之和")
        return self


def safe_fraction(numerator: float | None, denominator: float | None) -> float | None:
    """Compute a fraction without converting missing or zero-denominator data to 0."""

    if numerator is None or denominator is None:
        return None
    if not math.isfinite(numerator) or not math.isfinite(denominator):
        raise ValueError("比例输入必须是有限数值")
    if numerator < 0 or denominator < 0:
        raise ValueError("比例输入不能为负")
    if denominator == 0:
        return None
    ratio = numerator / denominator
    if ratio < -1e-12 or ratio > 1 + 1e-9:
        raise ValueError("分子不能超过分母")
    return min(1.0, max(0.0, ratio))


def compute_quality_summary(
    candidate_id: str,
    values: QualityAreaInputs,
    *,
    min_valid_coverage_fraction: float = 0.80,
    min_clear_aoi_fraction: float = 0.60,
    min_quality_assessed_fraction: float = 0.80,
) -> QualitySummary:
    """Turn provider reducer areas into safe, AOI-relative quality metrics.

    Coverage and clear-observation sufficiency are separate decisions. Scene-level
    cloud metadata is deliberately not accepted by this function because it is not
    an AOI quality measurement.
    """

    for name, threshold in {
        "min_valid_coverage_fraction": min_valid_coverage_fraction,
        "min_clear_aoi_fraction": min_clear_aoi_fraction,
        "min_quality_assessed_fraction": min_quality_assessed_fraction,
    }.items():
        if not 0 <= threshold <= 1:
            raise ValueError(f"{name} 必须在 [0, 1] 范围内")

    warnings = list(values.warnings)
    if values.failure_reason:
        return _summary(
            candidate_id,
            values,
            status=QualityStatus.FAILED,
            reason=f"在线质量计算失败：{values.failure_reason}",
            warnings=warnings,
            valid_coverage_fraction=None,
            clear_aoi_fraction=None,
            cloud_shadow_fraction_on_valid=None,
            quality_assessed_fraction=None,
            cloud_fraction_on_valid=None,
            shadow_fraction_on_valid=None,
            snow_fraction_on_valid=None,
            nodata_fraction_of_aoi=None,
            saturated_fraction_on_valid=None,
        )

    valid_fraction = safe_fraction(values.valid_area_m2, values.aoi_area_m2)
    nodata_fraction = safe_fraction(values.nodata_area_m2, values.aoi_area_m2)
    if values.valid_area_m2 is None:
        return _summary(
            candidate_id,
            values,
            status=QualityStatus.UNKNOWN,
            reason="有效覆盖面积未知，不能计算研究区质量",
            warnings=warnings,
            valid_coverage_fraction=None,
            clear_aoi_fraction=None,
            cloud_shadow_fraction_on_valid=None,
            quality_assessed_fraction=None,
            cloud_fraction_on_valid=None,
            shadow_fraction_on_valid=None,
            snow_fraction_on_valid=None,
            nodata_fraction_of_aoi=nodata_fraction,
            saturated_fraction_on_valid=None,
        )

    if values.valid_area_m2 == 0:
        warnings.append("研究区内没有有效光谱覆盖；这不是 0% 云，也不是清晰观测")
        return _summary(
            candidate_id,
            values,
            status=QualityStatus.INSUFFICIENT_COVERAGE,
            reason="研究区内有效覆盖为零，云影比例分母为零",
            warnings=warnings,
            valid_coverage_fraction=0.0,
            clear_aoi_fraction=None,
            cloud_shadow_fraction_on_valid=None,
            quality_assessed_fraction=None,
            cloud_fraction_on_valid=None,
            shadow_fraction_on_valid=None,
            snow_fraction_on_valid=None,
            nodata_fraction_of_aoi=nodata_fraction,
            saturated_fraction_on_valid=None,
        )

    assessed_fraction = safe_fraction(values.quality_assessed_area_m2, values.aoi_area_m2)
    clear_fraction = safe_fraction(values.clear_area_m2, values.aoi_area_m2)
    cloud_shadow_area = values.cloud_shadow_area_m2
    if cloud_shadow_area is None:
        if values.cloud_area_m2 is not None and values.shadow_area_m2 is not None:
            cloud_shadow_area = values.cloud_area_m2 + values.shadow_area_m2
    cloud_shadow_fraction = safe_fraction(cloud_shadow_area, values.valid_area_m2)
    cloud_fraction = safe_fraction(values.cloud_area_m2, values.valid_area_m2)
    shadow_fraction = safe_fraction(values.shadow_area_m2, values.valid_area_m2)
    snow_fraction = safe_fraction(values.snow_area_m2, values.valid_area_m2)
    saturated_fraction = safe_fraction(values.saturated_area_m2, values.valid_area_m2)

    if values.quality_assessed_area_m2 is None or assessed_fraction == 0:
        return _summary(
            candidate_id,
            values,
            status=QualityStatus.UNKNOWN,
            reason="研究区没有可用质量信息，不能推断云量或清晰程度",
            warnings=warnings,
            valid_coverage_fraction=valid_fraction,
            clear_aoi_fraction=None,
            cloud_shadow_fraction_on_valid=None,
            quality_assessed_fraction=assessed_fraction,
            cloud_fraction_on_valid=None,
            shadow_fraction_on_valid=None,
            snow_fraction_on_valid=None,
            nodata_fraction_of_aoi=nodata_fraction,
            saturated_fraction_on_valid=None,
        )

    if clear_fraction is None or cloud_shadow_fraction is None:
        return _summary(
            candidate_id,
            values,
            status=QualityStatus.UNKNOWN,
            reason="质量掩膜不完整，无法同时计算清晰覆盖与云影比例",
            warnings=warnings,
            valid_coverage_fraction=valid_fraction,
            clear_aoi_fraction=clear_fraction,
            cloud_shadow_fraction_on_valid=cloud_shadow_fraction,
            quality_assessed_fraction=assessed_fraction,
            cloud_fraction_on_valid=cloud_fraction,
            shadow_fraction_on_valid=shadow_fraction,
            snow_fraction_on_valid=snow_fraction,
            nodata_fraction_of_aoi=nodata_fraction,
            saturated_fraction_on_valid=saturated_fraction,
        )

    if valid_fraction is None or valid_fraction < min_valid_coverage_fraction:
        status = QualityStatus.INSUFFICIENT_COVERAGE
        reason = (
            f"有效覆盖 {valid_fraction:.3f} 低于阈值 "
            f"{min_valid_coverage_fraction:.3f}；不能解释为无变化或单纯多云"
        )
    elif assessed_fraction is None or assessed_fraction < min_quality_assessed_fraction:
        status = QualityStatus.UNKNOWN
        reason = (
            f"质量评估覆盖 {assessed_fraction:.3f} 低于阈值 "
            f"{min_quality_assessed_fraction:.3f}"
        )
    elif clear_fraction < min_clear_aoi_fraction:
        status = QualityStatus.INSUFFICIENT_CLEAR
        reason = (
            f"清晰观测覆盖 {clear_fraction:.3f} 低于阈值 "
            f"{min_clear_aoi_fraction:.3f}"
        )
    else:
        status = QualityStatus.VALID
        reason = None

    return _summary(
        candidate_id,
        values,
        status=status,
        reason=reason,
        warnings=warnings,
        valid_coverage_fraction=valid_fraction,
        clear_aoi_fraction=clear_fraction,
        cloud_shadow_fraction_on_valid=cloud_shadow_fraction,
        quality_assessed_fraction=assessed_fraction,
        cloud_fraction_on_valid=cloud_fraction,
        shadow_fraction_on_valid=shadow_fraction,
        snow_fraction_on_valid=snow_fraction,
        nodata_fraction_of_aoi=nodata_fraction,
        saturated_fraction_on_valid=saturated_fraction,
    )


def _summary(
    candidate_id: str,
    values: QualityAreaInputs,
    *,
    status: QualityStatus,
    reason: str | None,
    warnings: list[str],
    valid_coverage_fraction: float | None,
    clear_aoi_fraction: float | None,
    cloud_shadow_fraction_on_valid: float | None,
    quality_assessed_fraction: float | None,
    cloud_fraction_on_valid: float | None,
    shadow_fraction_on_valid: float | None,
    snow_fraction_on_valid: float | None,
    nodata_fraction_of_aoi: float | None,
    saturated_fraction_on_valid: float | None,
) -> QualitySummary:
    return QualitySummary(
        candidate_id=candidate_id,
        status=status,
        valid_coverage_fraction=valid_coverage_fraction,
        clear_aoi_fraction=clear_aoi_fraction,
        cloud_shadow_fraction_on_valid=cloud_shadow_fraction_on_valid,
        quality_assessed_fraction=quality_assessed_fraction,
        cloud_fraction_on_valid=cloud_fraction_on_valid,
        shadow_fraction_on_valid=shadow_fraction_on_valid,
        snow_fraction_on_valid=snow_fraction_on_valid,
        nodata_fraction_of_aoi=nodata_fraction_of_aoi,
        saturated_fraction_on_valid=saturated_fraction_on_valid,
        aoi_area_m2=values.aoi_area_m2,
        areas_m2={
            "valid_area_m2": values.valid_area_m2,
            "clear_area_m2": values.clear_area_m2,
            "quality_assessed_area_m2": values.quality_assessed_area_m2,
            "cloud_area_m2": values.cloud_area_m2,
            "shadow_area_m2": values.shadow_area_m2,
            "cloud_shadow_area_m2": values.cloud_shadow_area_m2,
            "snow_area_m2": values.snow_area_m2,
            "nodata_area_m2": values.nodata_area_m2,
            "saturated_area_m2": values.saturated_area_m2,
        },
        method=values.method,
        threshold=values.cloud_score_threshold,
        scale_meters=values.scale_meters,
        cloud_score_match_count=values.cloud_score_match_count,
        reason=reason,
        warnings=list(dict.fromkeys(warnings)),
        contains_mock=values.contains_mock,
    )
