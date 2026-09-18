from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from eo_agent.config import PROJECT_ROOT


class ImageryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = "mock"
    model_profile: str = "mock"
    default_download_root: Path = Path("data/downloads")
    grid_meters: int = Field(default=20, ge=10, le=1000)
    preview_max_dimension: int = Field(default=768, ge=64, le=2048)
    quality_scale_meters: int = Field(default=20, ge=10, le=1000)
    cloud_score_band: str = "cs_cdf"
    cloud_score_clear_threshold: float = Field(default=0.60, ge=0, le=1)
    sar_recommend_clear_fraction: float = Field(default=0.55, ge=0, le=1)
    candidate_display_limit_per_window: int = Field(default=5, ge=1, le=20)
    catalog_limit_per_window: int = Field(default=100, ge=1, le=500)
    initial_quality_limit_per_window: int = Field(default=10, ge=1, le=50)
    max_download_concurrency: int = Field(default=2, ge=1, le=8)
    max_download_retries: int = Field(default=2, ge=0, le=5)
    max_llm_schema_repairs: int = Field(default=1, ge=0, le=3)
    max_llm_attempts: int = Field(default=12, ge=1, le=50)
    max_request_uncompressed_mib: int = Field(default=24, ge=1, le=31)
    mock_download_chunk_bytes: int = Field(default=16384, ge=1024)
    single_process: bool = True

    @model_validator(mode="after")
    def limits_are_consistent(self) -> ImageryConfig:
        if self.initial_quality_limit_per_window > self.catalog_limit_per_window:
            raise ValueError("在线质检上限不能大于目录查询上限")
        if self.candidate_display_limit_per_window > self.catalog_limit_per_window:
            raise ValueError("候选展示上限不能大于目录查询上限")
        return self

    def resolved_download_root(self, project_root: Path = PROJECT_ROOT) -> Path:
        override = os.getenv("EO_IMAGERY_DOWNLOAD_ROOT")
        path = Path(override) if override else self.default_download_root
        if not path.is_absolute():
            path = project_root / path
        return path.resolve()


def load_imagery_config(root: Path | None = None) -> ImageryConfig:
    root = root or PROJECT_ROOT
    with (root / "configs" / "imagery.yaml").open("r", encoding="utf-8") as handle:
        value = yaml.safe_load(handle) or {}
    return ImageryConfig.model_validate(value)
