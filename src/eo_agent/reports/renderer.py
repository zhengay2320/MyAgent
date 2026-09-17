from __future__ import annotations

import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from eo_agent.schemas import ReportFacts


class ReportRenderer:
    def __init__(self) -> None:
        template_dir = Path(__file__).parent / "templates"
        self.env = Environment(
            loader=FileSystemLoader(template_dir),
            autoescape=True,
        )

    def markdown(self, facts: ReportFacts, summary: str) -> str:
        change = (
            "无法判断"
            if facts.change_area_km2 is None
            else f"{facts.change_area_km2:.2f} km²（模拟）"
        )
        return "\n".join(
            [
                f"# EO-Agent 报告 {facts.task_id}",
                "",
                f"> **{facts.notice}**",
                "",
                f"- 状态：{facts.status.value}",
                f"- 区域：{facts.aoi_name or '待确认'} ({facts.aoi_id or '未指定'})",
                f"- 模型 profile：{facts.model_profile}",
                f"- 模拟标识：{facts.contains_mock}",
                f"- 变化面积：{change}",
                f"- 核验：{facts.verification_status}",
                f"- 工具调用：{facts.tool_call_count}",
                "",
                "## 结论",
                facts.conclusion,
                "",
                "## 短说明",
                summary,
                "",
                "## 执行步骤",
                *[f"1. {step}" for step in facts.steps],
                "",
                "## 限制",
                *[f"- {item}" for item in facts.limitations],
            ]
        )

    def html(self, facts: ReportFacts, summary: str) -> str:
        return self.env.get_template("report.html.j2").render(facts=facts, summary=summary)

    def json(self, facts: ReportFacts, summary: str) -> str:
        data = facts.model_dump(mode="json")
        data["mock_result"] = True
        data["summary"] = summary
        return json.dumps(data, ensure_ascii=False, indent=2)
