from __future__ import annotations

import copy
import json

from eo_agent.scientific.environment import EpisodeEnvironment
from eo_agent.scientific.schemas import (
    BudgetLedger,
    ExperimentResult,
    ExperimentSpec,
    ExperimentType,
    InvestigationSpec,
)


class ExperimentRegistry:
    def __init__(self) -> None:
        self._available = {
            ExperimentType.E1_RAW_OBSERVATION,
            ExperimentType.E2_SEASONAL_CONTROL,
            ExperimentType.E3_PERSISTENCE,
        }

    def available(self) -> list[ExperimentType]:
        return sorted(self._available, key=lambda item: item.value)

    def validate(self, spec: ExperimentSpec, investigation: InvestigationSpec) -> None:
        if spec.experiment_type not in self._available:
            raise ValueError(f"实验未在 P1 注册为可用: {spec.experiment_type.value}")
        if spec.window_id != "target":
            raise ValueError("实验不得替换任务目标窗口")
        if (
            spec.experiment_type == ExperimentType.E2_SEASONAL_CONTROL
            and spec.control_window_id not in investigation.allowed_reference_windows
        ):
            raise ValueError("实验对照窗口不在授权参考域")
        if spec.subregion_id != investigation.spatial_support:
            raise ValueError("实验空间支持不得替换任务统计范围")
        if spec.source_group_id != "raw_optical":
            raise ValueError("P1 仅授权 raw_optical 来源组")


class ExperimentExecutor:
    def __init__(self, registry: ExperimentRegistry | None = None) -> None:
        self.registry = registry or ExperimentRegistry()
        self._cache: dict[str, ExperimentResult] = {}

    def execute(
        self,
        spec: ExperimentSpec,
        commitment_id: str,
        investigation: InvestigationSpec,
        environment: EpisodeEnvironment,
        budget: BudgetLedger,
    ) -> ExperimentResult:
        self.registry.validate(spec, investigation)
        budget.reserve("experiment")
        key = json.dumps(
            {
                "type": spec.experiment_type.value,
                "window": spec.window_id,
                "control": spec.control_window_id,
                "subregion": spec.subregion_id,
                "source_group": spec.source_group_id,
                "profile": spec.parameter_profile,
                "horizon": spec.requested_horizon_days,
            },
            sort_keys=True,
        )
        if key in self._cache:
            cached = self._cache[key].model_copy(deep=True)
            cached.experiment_id = spec.experiment_id
            cached.commitment_id = commitment_id
            cached.cache_hit = True
            return cached
        budget.reserve("data_read")
        result = environment.execute(spec, commitment_id, investigation)
        self._cache[key] = copy.deepcopy(result)
        return result
