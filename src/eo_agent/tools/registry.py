from __future__ import annotations

from eo_agent.tools.base import DomainTool


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, DomainTool] = {}

    def register(self, tool: DomainTool, *, replace: bool = False) -> None:
        if tool.name in self._tools and not replace:
            raise ValueError(f"工具已注册: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> DomainTool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ValueError(f"未注册工具: {name}") from exc

    def implementation_ids(self) -> dict[str, str]:
        return {name: tool.implementation_id for name, tool in self._tools.items()}
