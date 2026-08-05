"""Evita loops de tools idénticas y acota exploración en EXECUTE."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

# Tools que gastan presupuesto de exploración (solo EXECUTE)
EXPLORE_TOOL_NAMES = frozenset({"list_files", "search_code", "inspect_routes"})


class ToolCallDedupe:
    """Contador por (tool_name, args). Se resetea al inicio de cada turno."""

    def __init__(self, max_repeats: int = 2):
        self.max_repeats = max_repeats
        self._counts: dict[str, int] = {}

    def reset(self) -> None:
        self._counts.clear()

    def key(self, name: str, args: dict[str, Any]) -> str:
        try:
            payload = json.dumps(args, sort_keys=True, default=str)
        except TypeError:
            payload = str(args)
        return f"{name}::{payload}"

    def register(self, name: str, args: dict[str, Any]) -> int:
        k = self.key(name, args)
        self._counts[k] = self._counts.get(k, 0) + 1
        return self._counts[k]


class ExploreBudget:
    """Límite duro de exploraciones por turno (list_files/search_code/inspect_routes)."""

    def __init__(self, max_calls: int = 2):
        self.max_calls = max_calls
        self._count = 0

    def reset(self) -> None:
        self._count = 0

    @property
    def used(self) -> int:
        return self._count

    def consume(self, name: str) -> str | None:
        """Si la tool es de exploración y se agotó el budget, devuelve STOP message."""
        if name not in EXPLORE_TOOL_NAMES:
            return None
        self._count += 1
        if self._count > self.max_calls:
            return (
                f"STOP: exploration budget exhausted "
                f"({self.max_calls} list_files/search_code/inspect_routes). "
                "ESCRIBÍ YA con write_file/edit_file. No explores más. "
                "Si ya escribiste, verificá checklist + run_lint/run_tests/run_build."
            )
        return None


def wrap_tools_with_dedupe(
    tools: list,
    dedupe: ToolCallDedupe,
    explore_budget: ExploreBudget | None = None,
) -> list:
    """Envuelve tools: dedupe idéntico + (opcional) explore budget."""
    wrapped: list[BaseTool] = []
    for t in tools:
        wrapped.append(_wrap_one(t, dedupe, explore_budget))
    return wrapped


def _wrap_one(
    tool: BaseTool,
    dedupe: ToolCallDedupe,
    explore_budget: ExploreBudget | None,
) -> BaseTool:
    name = tool.name

    def _invoke(**kwargs):
        if explore_budget is not None:
            stop = explore_budget.consume(name)
            if stop:
                return stop
        n = dedupe.register(name, kwargs)
        if n > dedupe.max_repeats:
            return (
                f"STOP: already called {name} with the same arguments {n} times. "
                "Do NOT call it again. Use write_file/edit_file to implement changes, "
                "or finish with your findings."
            )
        return tool.invoke(kwargs)

    return StructuredTool.from_function(
        func=_invoke,
        name=name,
        description=tool.description,
        args_schema=getattr(tool, "args_schema", None),
    )
