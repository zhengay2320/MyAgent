from __future__ import annotations

import hashlib
from typing import Any, ClassVar

from pydantic import BaseModel

from eo_agent.schemas import (
    ChangeData,
    ObservationData,
    PreparedData,
    QualityData,
    ReconstructedData,
    ResourceArgs,
    ScientificValidity,
    SearchArgs,
    ToolResult,
    ToolStatus,
    VerificationData,
    VerifyArgs,
)
from eo_agent.tools.base import RetryableToolError, ToolContext
from eo_agent.tools.registry import ToolRegistry


def _result(name: str, data: Any, warnings: list[str] | None = None) -> ToolResult:
    return ToolResult(
        status=ToolStatus.SUCCEEDED,
        tool_name=name,
        implementation_id=f"mock-{name}-v1",
        data=data,
        contains_mock=True,
        scientific_validity=ScientificValidity.NOT_EVALUATED,
        warnings=warnings or ["模拟数据/占位算法，不具备真实科学有效性"],
        provenance={"generator": "deterministic_mock", "schema": "eo-tool-v1"},
    )


class MockTool:
    implementation_id: ClassVar[str]

    def __init__(self, name: str, args_model: type[BaseModel]) -> None:
        self.name = name
        self.args_model = args_model
        self.implementation_id = f"mock-{name}-v1"

    def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        config = context.scenario_config
        fail_once = config.get("fail_once")
        attempt_key = f"attempt:{self.name}"
        attempt = context.scenario_state.get(attempt_key, 0)
        context.scenario_state[attempt_key] = attempt + 1
        if fail_once == self.name and attempt == 0:
            raise RetryableToolError("模拟临时失败")
        round_no = context.scenario_state.get("evidence_round", 0)
        suffix = f"{context.task_id}-r{round_no}"
        if self.name == "search_observations":
            available = bool(config["available"])
            data = ObservationData(
                resource_id=f"obs-{suffix}",
                available=available,
                observation_count=(4 + round_no * 2) if available else 0,
                cloud_fraction=config.get("cloud_fraction"),
                evidence_round=round_no,
            )
        elif self.name == "fetch_additional_observation":
            round_no += 1
            context.scenario_state["evidence_round"] = round_no
            data = ObservationData(
                resource_id=f"obs-{context.task_id}-r{round_no}",
                available=True,
                observation_count=6,
                cloud_fraction=max(0.04, float(config.get("cloud_fraction") or 0.2) - 0.08),
                evidence_round=round_no,
            )
        elif self.name == "prepare_data":
            source = _owned(arguments, context)
            cloud = float(
                context.scenario_state.get("last_cloud", config.get("cloud_fraction") or 0)
            )
            data = PreparedData(
                resource_id=f"prepared-{suffix}",
                source_resource_id=source,
                usable=True,
                cloud_fraction=cloud,
                evidence_round=round_no,
            )
        elif self.name == "inspect_quality":
            source = _owned(arguments, context)
            cloud = float(
                context.scenario_state.get("last_cloud", config.get("cloud_fraction") or 0)
            )
            data = QualityData(
                resource_id=f"quality-{suffix}",
                usable=True,
                cloud_fraction=cloud,
                reconstruction_recommended=cloud > 0.35,
            )
        elif self.name == "reconstruct_optical":
            source = _owned(arguments, context)
            residual = 0.08
            context.scenario_state["last_cloud"] = residual
            data = ReconstructedData(
                resource_id=f"reconstructed-{suffix}",
                source_resource_id=source,
                residual_cloud_fraction=residual,
                evidence_round=round_no,
            )
        elif self.name == "detect_change":
            source = _owned(arguments, context)
            denominator = context.area_denominator_km2
            if denominator is None or denominator <= 0:
                raise ValueError("变化比例缺少有效面积分母")
            denominator_kind = context.area_denominator_kind
            if denominator_kind not in {"task_aoi_area", "readable_area"}:
                raise ValueError("变化比例缺少有效分母类别")
            digest = hashlib.sha256(f"{context.seed}:change".encode()).hexdigest()
            area = round(2.5 + int(digest[:4], 16) % 300 / 100, 2)
            data = ChangeData(
                resource_id=f"change-{suffix}",
                source_resource_id=source,
                change_area_km2=area,
                changed_fraction=round(area / denominator, 6),
                area_denominator_km2=denominator,
                area_denominator_kind=denominator_kind,
                can_determine=True,
                events=[{"type": "mock_surface_change", "area_km2": area}],
                evidence_round=round_no,
            )
        elif self.name == "verify_change":
            source = _owned(arguments, context)
            mode = str(config["verification"])
            if mode == "needs_evidence" and round_no > 0:
                mode = "sufficient"
            if mode == "insufficient" and round_no == 0:
                mode = "needs_evidence"
            sufficient = mode == "sufficient"
            verdict = "supported" if sufficient else mode
            data = VerificationData(
                resource_id=f"verification-{suffix}",
                sufficient=sufficient,
                verdict=verdict,
                message=(
                    "模拟证据链满足流程核验" if sufficient else "模拟证据不足，无法形成可靠判断"
                ),
                evidence_round=round_no,
            )
        else:
            raise ValueError(f"未实现模拟工具: {self.name}")
        context.resources[data.resource_id] = self.name
        if isinstance(data, ObservationData):
            context.scenario_state["last_cloud"] = int((data.cloud_fraction or 0) * 10000) / 10000
        return _result(self.name, data)


def _owned(arguments: BaseModel, context: ToolContext) -> str:
    resource_id = arguments.resource_id
    if resource_id not in context.resources:
        raise ValueError("资源不存在或不属于当前任务")
    return resource_id


def create_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for name, args_model in (
        ("search_observations", SearchArgs),
        ("prepare_data", ResourceArgs),
        ("inspect_quality", ResourceArgs),
        ("reconstruct_optical", ResourceArgs),
        ("detect_change", ResourceArgs),
        ("verify_change", VerifyArgs),
        ("fetch_additional_observation", ResourceArgs),
    ):
        registry.register(MockTool(name, args_model))
    return registry
