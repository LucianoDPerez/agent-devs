"""Guard de project key: las cm__ tools corrigen slugs inventados
silenciosamente usando la key del repo actual (graph_project).

E2E real: 'demo-segmentacion' inventado quemó 3 llamadas del
presupuesto antes de auto-corregirse.
"""
import asyncio

from langchain_core.tools import StructuredTool

from orchestration.agent_builder import _with_fixed_project


def _fake_cm_tool(name: str) -> StructuredTool:
    async def coro(**kwargs):
        return f"OK project={kwargs.get('project')}"
    return StructuredTool(
        name=name,
        description="fake",
        args_schema=None,
        coroutine=coro,
    )


def test_project_inventado_se_corrige_silenciosamente():
    tool = _fake_cm_tool("cm__search_graph")
    wrapped = _with_fixed_project(tool, "Users-user-itti-demo-ads-platform")

    out = asyncio.run(wrapped.ainvoke({"project": "demo-segmentacion", "query": "x"}))
    assert "Users-user-itti-demo-ads-platform" in out


def test_project_correcto_no_se_toca():
    tool = _fake_cm_tool("cm__query_graph")
    wrapped = _with_fixed_project(tool, "Users-luchop-PROYECTOS-IA-Medicos")

    out = asyncio.run(wrapped.ainvoke({"project": "Users-luchop-PROYECTOS-IA-Medicos"}))
    assert "OK project=Users-luchop-PROYECTOS-IA-Medicos" in out


def test_sin_project_no_se_inyecta():
    """Si el modelo no pasa project, no lo agregamos (las tools con default
    lo resuelven solas, p.ej. trace_component)."""
    tool = _fake_cm_tool("cm__get_architecture")
    wrapped = _with_fixed_project(tool, "Users-luchop-PROYECTOS-IA-Medicos")

    out = asyncio.run(wrapped.ainvoke({"aspects": ["clusters"]}))
    assert "project=None" in out
