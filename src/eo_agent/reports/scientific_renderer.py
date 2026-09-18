from __future__ import annotations

import html
import json

from eo_agent.scientific.schemas import ScientificReportFacts


class ScientificReportRenderer:
    def json(self, facts: ScientificReportFacts) -> str:
        return facts.model_dump_json(indent=2)

    def markdown(self, facts: ScientificReportFacts) -> str:
        lines = [
            "# HERA-Change P1 事件级科学调查报告",
            "",
            "> **模拟结果**：本报告的候选、观测、实验和结论均来自离线模拟，"
            "不得解释为真实遥感结论。",
            "",
            f"- 任务：`{facts.task_id}`",
            f"- Episode：`{facts.episode_id}`",
            f"- LLM/实验预算：{facts.budget.used_llm_calls}/{facts.budget.max_llm_calls}；"
            f"{facts.budget.used_experiments}/{facts.budget.max_experiments}",
        ]
        for report in facts.events:
            lines.extend(
                [
                    "",
                    f"## 事件 {report.event.event_id}",
                    "",
                    f"- 决策：**{report.decision.label.value}**",
                    f"- 停止原因：`{report.decision.stop_reason.value}`",
                    f"- 声明：{report.claim.statement}",
                    f"- 支持证据：{', '.join(report.decision.supporting_evidence_ids) or '无'}",
                    f"- 冲突证据：{', '.join(report.decision.conflicting_evidence_ids) or '无'}",
                    f"- 缺失证据：{'; '.join(report.claim.missing_evidence) or '无'}",
                    "",
                    "### 实验前承诺与结果",
                    "",
                ]
            )
            for commitment, result in zip(report.precommitments, report.experiments, strict=False):
                lines.append(
                    f"- `{commitment.experiment.experiment_type.value}` / "
                    f"`{commitment.experiment.parameter_profile}` → "
                    f"{result.evidence_validity.value}；commit=`{commitment.content_hash[:12]}`"
                )
            lines.extend(["", "### 假设修订", ""])
            for revision in report.revisions:
                lines.append(
                    f"- v{revision.from_version}→v{revision.to_version} "
                    f"{revision.disposition}：{revision.reason_summary}"
                )
            if not report.revisions:
                lines.append("- 无")
            lines.extend(["", "### 来源与依赖", ""])
            for evidence in report.evidence:
                dependency = ", ".join(evidence.dependent_with) or "独立/首项"
                lines.append(
                    f"- `{evidence.evidence_id}` roots={','.join(evidence.root_source_ids)}；"
                    f"依赖={dependency}；relation={evidence.relation.value}"
                )
        lines.extend(["", "## 限制", ""])
        lines.extend(f"- {item}" for item in facts.limitations)
        return "\n".join(lines) + "\n"

    def html(self, facts: ScientificReportFacts) -> str:
        body: list[str] = []
        for report in facts.events:
            supporting = html.escape(", ".join(report.decision.supporting_evidence_ids) or "无")
            conflicting = html.escape(", ".join(report.decision.conflicting_evidence_ids) or "无")
            missing = html.escape("; ".join(report.claim.missing_evidence) or "无")
            experiments = "".join(
                "<li><code>"
                + html.escape(result.experiment_type.value)
                + "</code>："
                + html.escape(result.evidence_validity.value)
                + "，参数 <code>"
                + html.escape(commitment.experiment.parameter_profile)
                + "</code></li>"
                for commitment, result in zip(
                    report.precommitments, report.experiments, strict=False
                )
            )
            revisions = (
                "".join(
                    f"<li>v{item.from_version}→v{item.to_version} "
                    f"{html.escape(item.disposition)}：{html.escape(item.reason_summary)}</li>"
                    for item in report.revisions
                )
                or "<li>无</li>"
            )
            evidence = "".join(
                "<li><code>"
                + html.escape(item.evidence_id)
                + "</code> "
                + html.escape(item.relation.value)
                + "；roots="
                + html.escape(",".join(item.root_source_ids))
                + "；依赖="
                + html.escape(",".join(item.dependent_with) or "独立/首项")
                + "</li>"
                for item in report.evidence
            )
            body.append(
                f"<section><h2>事件 {html.escape(report.event.event_id)}</h2>"
                f"<p><b>决策：</b>{html.escape(report.decision.label.value)}；"
                f"<b>停止原因：</b>{html.escape(report.decision.stop_reason.value)}</p>"
                f"<p>{html.escape(report.claim.statement)}</p>"
                f"<h3>支持</h3><p>{supporting}</p>"
                f"<h3>冲突</h3><p>{conflicting}</p>"
                f"<h3>缺失</h3><p>{missing}</p>"
                f"<h3>实验前承诺与结果</h3><ul>{experiments}</ul>"
                f"<h3>假设修订</h3><ul>{revisions}</ul>"
                f"<h3>证据来源与依赖</h3><ul>{evidence}</ul></section>"
            )
        limitations = "".join(f"<li>{html.escape(item)}</li>" for item in facts.limitations)
        embedded = html.escape(json.dumps(facts.model_dump(mode="json"), ensure_ascii=False))
        return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>HERA-Change P1 模拟科学报告</title><style>
body{{font-family:system-ui,sans-serif;max-width:980px;margin:auto;padding:28px;line-height:1.55}}
.mock{{background:#fff3cd;border:2px solid #d99b00;padding:18px;font-size:1.15rem}}
section{{border:1px solid #ddd;border-radius:10px;padding:18px;margin:20px 0}}
code{{background:#eee;padding:2px 5px}}
</style></head><body><div class="mock"><b>模拟结果</b><br>候选、观测、实验和结论均为离线模拟，
不得解释为真实遥感结论。</div><h1>HERA-Change P1 事件级科学调查报告</h1>
<p>任务 <code>{html.escape(facts.task_id)}</code>；
Episode <code>{html.escape(facts.episode_id)}</code></p>
{"".join(body)}<h2>限制</h2><ul>{limitations}</ul>
<details><summary>结构化报告事实</summary><pre>{embedded}</pre></details></body></html>"""
