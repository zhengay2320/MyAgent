from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from eo_agent.config import Settings
from eo_agent.llm.base import LLMClient
from eo_agent.schemas import TaskStatus, VerificationData
from eo_agent.tools.executor import ToolExecutor
from eo_agent.workflow.nodes import WorkflowNodes
from eo_agent.workflow.state import WorkflowState


def build_graph(settings: Settings, llm: LLMClient, executor: ToolExecutor):
    nodes = WorkflowNodes(settings, llm, executor)
    graph = StateGraph(WorkflowState)
    for name in (
        "parse",
        "validate",
        "search",
        "prepare",
        "quality",
        "choose_reconstruction",
        "reconstruct",
        "detect",
        "verify",
        "evidence",
        "report",
    ):
        graph.add_node(name, getattr(nodes, name))
    graph.add_edge(START, "parse")
    graph.add_edge("parse", "validate")
    graph.add_conditional_edges(
        "validate",
        lambda state: "search" if state["status"] == TaskStatus.RUNNING else "end",
        {"search": "search", "end": END},
    )
    graph.add_conditional_edges(
        "search",
        lambda state: "report" if state["status"] == TaskStatus.PARTIAL else "prepare",
        {"report": "report", "prepare": "prepare"},
    )
    graph.add_edge("prepare", "quality")
    graph.add_edge("quality", "choose_reconstruction")
    graph.add_conditional_edges(
        "choose_reconstruction",
        lambda state: (
            "reconstruct" if state["scenario_state"].get("chosen_reconstruction") else "detect"
        ),
        {"reconstruct": "reconstruct", "detect": "detect"},
    )
    graph.add_edge("reconstruct", "detect")
    graph.add_edge("detect", "verify")

    def after_verify(state: WorkflowState) -> str:
        data = state["tool_results"][-1].data
        assert isinstance(data, VerificationData)
        if data.sufficient:
            return "report"
        if (
            data.verdict == "needs_evidence"
            and state["replan_count"] < settings.budgets.max_replans
        ):
            return "evidence"
        state["status"] = TaskStatus.PARTIAL
        state["error"] = "达到补证上限或证据仍不足，无法形成可靠判断"
        return "report"

    graph.add_conditional_edges(
        "verify", after_verify, {"report": "report", "evidence": "evidence"}
    )
    graph.add_conditional_edges(
        "evidence",
        lambda state: "report" if state["status"] == TaskStatus.PARTIAL else "prepare",
        {"report": "report", "prepare": "prepare"},
    )
    graph.add_edge("report", END)
    return graph.compile()
