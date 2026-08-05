"""Evita loops de tools idénticas y fuerza escritura en EXECUTE."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool


class ToolBudgetExceeded(Exception):
    """Raised when a tool call is blocked by dedupe or explore budget.
    The 4B model ignores STOP strings; this exception forcibly halts the agent turn."""
    pass


# Tools que gastan presupuesto de exploración (solo EXECUTE)
EXPLORE_TOOL_NAMES = frozenset({"list_files", "search_code", "inspect_routes"})
WRITE_TOOL_NAMES = frozenset({
    "write_file", "edit_file", "delete_file", "stage_files", "create_commit", "push", "create_pr",
})
# Tools de lectura (no son "write" pero son acción productiva)
READISH_TOOL_NAMES = frozenset(
    {"read_file", "changed_files", "git_status", "git_log", "current_branch", "read_pr", "list_prs"}
)

# Tools de verificación (lint/tests/build) — acción productiva
VERIFY_TOOL_NAMES = frozenset({"run_lint", "run_tests", "run_build", "run_install"})

# Tools que cuentan como "producto final" para evitar max_tools_before_write
# (REVIEW nunca escribe; estos son sus outputs válidos)
PRODUCTIVE_TOOL_NAMES = READISH_TOOL_NAMES | VERIFY_TOOL_NAMES


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
    """Límite duro de exploraciones + presión a escribir (EXECUTE).

    El 4B ignora mensajes STOP y sigue con read_file hasta recursion_limit.
    Por eso también limitamos reads post-explore y tool calls totales sin write.
    """

    def __init__(
        self,
        max_calls: int = 2,
        max_reads_after_explore: int = 2,
        max_tools_before_write: int = 5,
    ):
        self.max_calls = max_calls
        self.max_reads_after_explore = max_reads_after_explore
        self.max_tools_before_write = max_tools_before_write
        self._count = 0
        self._reads_after = 0
        self._total = 0
        self._wrote = False
        self._explore_exhausted = False

    def reset(self) -> None:
        self._count = 0
        self._reads_after = 0
        self._total = 0
        self._wrote = False
        self._explore_exhausted = self.max_calls <= 0

    @property
    def used(self) -> int:
        return self._count

    def consume(self, name: str, kwargs: dict[str, Any] | None = None) -> str | None:
        """Devuelve mensaje STOP si la llamada no debe ejecutarse."""
        kwargs = kwargs or {}
        self._total += 1

        # max_calls puede cambiar post-reset (hints_on → 0 en session.py)
        if self.max_calls <= 0:
            self._explore_exhausted = True

        if name in WRITE_TOOL_NAMES:
            self._wrote = True
            return None

        # VERIFY / git RO siempre permitidos una vez que ya escribió
        # (y también antes, con tope de tools sin write)

        # recursive=true prohibido en EXECUTE
        if name == "list_files" and kwargs.get("recursive"):
            return (
                "⛔ list_files(recursive=true) prohibido en EXECUTE. "
                "Usá recursive=false o ESCRIBÍ YA con write_file/edit_file."
            )

        if name in EXPLORE_TOOL_NAMES:
            self._count += 1
            if self._count >= self.max_calls:
                self._explore_exhausted = True
            if self._count > self.max_calls:
                # Cuando el modelo ignora strings STOP (4B), usar exception
                if self.max_calls <= 0:
                    raise ToolBudgetExceeded(
                        "Exploración prohibida (max_calls=0). "
                        "TU ÚNICA ACCIÓN: write_file, edit_file o delete_file AHORA."
                    )
                return (
                    "⛔ Exploración agotada. NO uses list_files/search_code/inspect_routes. "
                    "TU ÚNICA ACCIÓN: write_file, edit_file o delete_file AHORA."
                )
            return None

        # Tras agotar explore, limitar read_file / git_status loops
        if self._explore_exhausted:
            if name in READISH_TOOL_NAMES or name == "read_file":
                self._reads_after += 1
                if self._reads_after > self.max_reads_after_explore:
                    if self.max_calls <= 0:
                        raise ToolBudgetExceeded(
                            "Demasiados read_file. NO leas más. "
                            "TU ÚNICA ACCIÓN: write_file, edit_file o delete_file AHORA."
                        )
                    return (
                        "⛔ Demasiados read_file. NO leas más archivos. "
                        "TU ÚNICA ACCIÓN: write_file, edit_file o delete_file AHORA."
                    )

        # Sin ninguna escritura tras N tools → forzar write (EXCEPTION — halts turn)
        # Pero PRODUCTIVE tools (read/git/verify) sí cuentan como acción productiva
        if not self._wrote and self._total > self.max_tools_before_write:
            if name not in WRITE_TOOL_NAMES and name not in PRODUCTIVE_TOOL_NAMES:
                raise ToolBudgetExceeded(
                    f"{self._total} tool calls sin escribir código. "
                    "NO explores más. TU ÚNICA ACCIÓN: write_file o edit_file AHORA."
                )

        return None


def wrap_tools_with_dedupe(
    tools: list,
    dedupe: ToolCallDedupe,
    explore_budget: ExploreBudget | None = None,
) -> list:
    """Envuelve tools: dedupe idéntico + (opcional) explore/write guard."""
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
            stop = explore_budget.consume(name, kwargs)
            if stop:
                return stop
        n = dedupe.register(name, kwargs)
        if n > dedupe.max_repeats:
            # RAISE instead of return string — the 4B model ignores text responses
            # and keeps calling the same tool, burning recursion limit.
            raise ToolBudgetExceeded(
                f"⛔ Called {name} with same args {n} times. "
                "STOP: Do NOT call it again. If this is write_file/edit_file, "
                "the path may be wrong or a directory exists at that path. "
                "Choose a different file path."
            )
        return tool.invoke(kwargs)

    return StructuredTool.from_function(
        func=_invoke,
        name=name,
        description=tool.description,
        args_schema=getattr(tool, "args_schema", None),
    )
