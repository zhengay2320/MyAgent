from __future__ import annotations

import re
from time import perf_counter
from typing import Any

from eo_agent.llm.base import LLMMetadata, LLMResult
from eo_agent.schemas import ActionSpec, ReportFacts, TaskDraft


class MockLLMAdapter:
    """Deterministic rule adapter. It only sees visible fields, never scenario IDs."""

    profile = "mock"
    implementation_id = "mock-rule-parser-v1"

    def _meta(self, started: float) -> LLMMetadata:
        return LLMMetadata(
            provider="mock",
            profile=self.profile,
            requested_model=self.implementation_id,
            returned_model=self.implementation_id,
            prompt_version="eo-v0.1",
            output_protocol="local-structured",
            duration_ms=round((perf_counter() - started) * 1000, 3),
            usage=None,
        )

    def parse_task(self, query: str, aoi_id: str | None) -> LLMResult[TaskDraft]:
        started = perf_counter()
        months = re.findall(r"(20\d{2})\s*年\s*(1[0-2]|0?[1-9])\s*月", query)
        en_months = re.findall(r"(20\d{2})-(1[0-2]|0[1-9])", query)
        values = months or en_months
        task_type = (
            "continuous_monitoring"
            if re.search(r"连续|from.+to|时序", query, re.I)
            else "two_period_change"
        )
        aoi_text = None
        if "武汉演示区" in query:
            aoi_text = "武汉演示区"
        elif "武汉" in query:
            aoi_text = "武汉"
        elif "大型合成测试区" in query:
            aoi_text = "大型合成测试区"
        missing: list[str] = []
        if len(values) < 2:
            missing.append("baseline_month/target_month")
        if not aoi_text and not aoi_id:
            missing.append("aoi")
        draft = TaskDraft(
            aoi_text=aoi_text,
            baseline_month=f"{values[1][0]}-{int(values[1][1]):02d}" if len(values) >= 2 else None,
            target_month=f"{values[0][0]}-{int(values[0][1]):02d}" if len(values) >= 2 else None,
            task_type=task_type,
            missing_fields=missing,
        )
        return LLMResult(payload=draft, metadata=self._meta(started))

    def choose_action(
        self, visible_state: dict[str, Any], allowed_actions: list[str]
    ) -> LLMResult[ActionSpec]:
        started = perf_counter()
        if "reconstruct" in allowed_actions:
            recommended = bool(visible_state.get("reconstruction_recommended"))
            action = "reconstruct" if recommended else "skip_reconstruction"
            reason = "可见质量指标建议去云" if recommended else "可见质量指标满足检测要求"
        elif "fetch_additional_evidence" in allowed_actions:
            action = (
                "fetch_additional_evidence"
                if visible_state.get("verdict") == "needs_evidence"
                else "keep_partial"
            )
            reason = (
                "核验结果明确要求补充证据"
                if action.startswith("fetch")
                else "证据预算内无法继续改善"
            )
        else:
            raise ValueError("没有可选动作")
        if action not in allowed_actions:
            raise ValueError("Mock 决策不在允许动作内")
        return LLMResult(
            payload=ActionSpec(action=action, arguments={}, reason_summary=reason),
            metadata=self._meta(started),
        )

    def summarize(self, facts: ReportFacts) -> LLMResult[str]:
        started = perf_counter()
        text = (
            f"任务以 {facts.status.value} 结束；全部领域数据与算法输出均为模拟结果。"
            f"核验状态：{facts.verification_status}。"
        )
        return LLMResult(payload=text, metadata=self._meta(started))
