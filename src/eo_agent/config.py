from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 自动加载项目根目录 .env
load_dotenv(PROJECT_ROOT / ".env")


class BudgetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_tool_calls: int = 20
    max_replans: int = 2
    max_tool_retries: int = 1
    max_schema_repairs: int = 1
    max_llm_calls: int = 8
    recursion_limit: int = 80


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grid_meters: int = 20
    area_soft_limit_km2: float = 400
    seed: int = 202503
    schema_version: str = "1.0"
    workflow_version: str = "0.1.0"


class ScientificConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_experiments: int = 3
    max_replans: int = 2
    max_data_reads: int = 3
    max_llm_calls: int = 8


class ModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    provider: str
    adapter: str
    model: str | None = None
    api_key: str | None = Field(default=None, repr=False)
    base_url: str | None = None
    prompt_version: str = "eo-v0.1"
    json_response_format: bool = False
    request_timeout_seconds: float = Field(default=60.0, ge=5.0, le=300.0)
    generation_params: dict[str, Any] = Field(default_factory=dict)

    def sanitized(self) -> dict[str, Any]:
        data = self.model_dump(exclude={"api_key"})
        data["api_key_configured"] = bool(self.api_key)
        return data


class Settings(BaseModel):
    app: AppConfig
    budgets: BudgetConfig
    scientific: ScientificConfig = Field(default_factory=ScientificConfig)
    profile: ModelProfile
    scenarios: dict[str, dict[str, Any]]
    aois: dict[str, dict[str, Any]]

    def effective_config(self) -> dict[str, Any]:
        return {
            "app": self.app.model_dump(),
            "budgets": self.budgets.model_dump(),
            "scientific": self.scientific.model_dump(),
            "model_profile": self.profile.sanitized(),
        }


def _yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def load_settings(profile_name: str = "mock", root: Path | None = None) -> Settings:
    root = root or PROJECT_ROOT
    app_data = _yaml(root / "configs" / "app.yaml")
    model_data = _yaml(root / "configs" / "models.yaml")["profiles"]
    if profile_name not in model_data:
        raise ValueError(f"未知模型 profile: {profile_name}")
    raw = dict(model_data[profile_name])
    raw["name"] = profile_name
    for target, env_key in (
        ("api_key", raw.pop("api_key_env", None)),
        ("base_url", raw.pop("base_url_env", None)),
        ("model", raw.pop("model_env", None)),
    ):
        if env_key:
            raw[target] = os.getenv(env_key)
    profile = ModelProfile.model_validate(raw)
    if profile.adapter != "mock":
        missing = [name for name in ("api_key", "base_url", "model") if not getattr(profile, name)]
        if missing:
            raise ValueError(
                f"profile {profile_name} 缺少配置: {', '.join(missing)}；不会回退到 Mock"
            )
    import json

    aois = json.loads((root / "fixtures" / "aois.json").read_text(encoding="utf-8"))
    scenarios = _yaml(root / "fixtures" / "scenarios" / "scenarios.yaml")["scenarios"]
    return Settings(
        app=AppConfig.model_validate(app_data["app"]),
        budgets=BudgetConfig.model_validate(app_data["budgets"]),
        scientific=ScientificConfig.model_validate(app_data.get("scientific", {})),
        profile=profile,
        scenarios=scenarios,
        aois=aois,
    )
