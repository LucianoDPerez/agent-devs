"""Tests for intent classifier priority, tool dedupe, and explore budget."""

import pytest

from core.intents import Intent
from orchestration.router import _extract_command_prefix, classify_intent
from orchestration.tool_dedupe import (
    EXPLORE_TOOL_NAMES,
    ExploreBudget,
    ToolBudgetExceeded,
    ToolCallDedupe,
    wrap_tools_with_dedupe,
)
from tools.filesystem import list_files, read_file, write_file
from tools.search import search_code


def test_implementar_correcciones_with_review_paste_is_execute():
    msg = (
        "implementar estas correcciones detectadas "
        "### PROBLEMAS CRITICOS DETECTADOS: "
        "## REVISION DE IMPLEMENTACION - Branch ulab-1368 "
        "Falta timeout CRITICAL"
    )
    assert classify_intent(None, msg) == Intent.EXECUTE


def test_implementar_observaciones_del_review_is_execute():
    """'implementar las ... del review' NO debe ir a REVIEW — es corrección post-review."""
    assert classify_intent(None, "implementar las observaciones del review") == Intent.EXECUTE
    assert classify_intent(None, "implementar las sugerencias del review") == Intent.EXECUTE
    assert classify_intent(None, "aplicar los hallazgos del review") == Intent.EXECUTE
    assert classify_intent(None, "aplicar los cambios del review") == Intent.EXECUTE


def test_plain_review_still_review():
    assert classify_intent(None, "revisá este PR por bugs") == Intent.REVIEW
    assert classify_intent(None, "hacer code review de la Tarea 3") == Intent.REVIEW
    assert classify_intent(None, "revisá los cambios") == Intent.REVIEW


def test_implementar_tasks_still_execute():
    assert classify_intent(
        None,
        "implementar la Tarea 1 y la Tarea 2 de /repo/lucho-plans/tasks.md",
    ) == Intent.EXECUTE


def test_dedupe_blocks_third_identical_call(tmp_path):
    f = tmp_path / "a.ts"
    f.write_text("consola.log(1)\n", encoding="utf-8")
    dedupe = ToolCallDedupe(max_repeats=2)
    tools = wrap_tools_with_dedupe([read_file], dedupe)
    tool = tools[0]
    args = {"path": str(f)}
    r1 = tool.invoke(args)
    r2 = tool.invoke(args)
    assert "consola" in r1 or "log" in r1
    assert "⛔" not in r2 or r2 == r1
    # read_file repetido ahora devuelve STRING (no exception): no debe disparar
    # un retry completo por releer un archivo
    r3 = tool.invoke(args)
    assert "⛔" in r3 or "Ya leíste" in r3


def test_dedupe_still_raises_write_dupes(tmp_path):
    """write_file repetido demasiadas veces SÍ levanta ToolBudgetExceeded."""
    from tools.filesystem import write_file
    import tempfile
    dedupe = ToolCallDedupe(max_repeats=1)
    tools = wrap_tools_with_dedupe([write_file], dedupe)
    tool = tools[0]
    target = str(tmp_path / "out.ts")
    # 1era y 2da con repeats=1+2 margen → strings; 4ta levanta
    r1 = tool.invoke({"path": target, "content": "a"})
    r2 = tool.invoke({"path": target, "content": "a"})
    r3 = tool.invoke({"path": target, "content": "a"})
    assert "✅" in r1 or "ya se ejecutó" in r1 or "⛔" in r1
    with pytest.raises(ToolBudgetExceeded, match="same args"):
        tool.invoke({"path": target, "content": "a"})


def test_explore_budget_allows_two_then_stops(tmp_path):
    (tmp_path / "a.ts").write_text("x", encoding="utf-8")
    dedupe = ToolCallDedupe(max_repeats=5)
    budget = ExploreBudget(max_calls=2, max_tools_before_write=50)
    tools = wrap_tools_with_dedupe([list_files], dedupe, budget)
    tool = tools[0]
    args = {"path": str(tmp_path), "recursive": False}
    r1 = tool.invoke(args)
    r2 = tool.invoke({**args})
    sub = tmp_path / "sub"
    sub.mkdir()
    r3 = tool.invoke({"path": str(sub), "recursive": False})
    assert "agotada" in r3
    assert budget.used == 3


def test_explore_budget_does_not_block_read_file(tmp_path):
    f = tmp_path / "a.ts"
    f.write_text("ok\n", encoding="utf-8")
    dedupe = ToolCallDedupe(max_repeats=5)
    budget = ExploreBudget(max_calls=0, max_tools_before_write=50)
    tools = wrap_tools_with_dedupe([read_file, list_files], dedupe, budget)
    by_name = {t.name: t for t in tools}
    content = by_name["read_file"].invoke({"path": str(f)})
    assert "ok" in content
    # When max_calls=0, explore tools raise ToolBudgetExceeded (not string)
    with pytest.raises(ToolBudgetExceeded, match="prohibida"):
        by_name["list_files"].invoke({"path": str(tmp_path), "recursive": False})


def test_explore_budget_covers_search_code(tmp_path):
    f = tmp_path / "a.ts"
    f.write_text("timeout = 5\n", encoding="utf-8")
    dedupe = ToolCallDedupe(max_repeats=5)
    budget = ExploreBudget(max_calls=1, max_tools_before_write=50)
    tools = wrap_tools_with_dedupe([search_code], dedupe, budget)
    tool = tools[0]
    r1 = tool.invoke({"path": str(tmp_path), "pattern": "timeout"})
    assert "STOP: exploration budget" not in r1
    r2 = tool.invoke({"path": str(tmp_path), "pattern": "other"})
    assert "agotada" in r2
    assert "search_code" in EXPLORE_TOOL_NAMES


def test_explore_budget_reset():
    budget = ExploreBudget(max_calls=1, max_tools_before_write=50)
    assert budget.consume("list_files") is None
    stop = budget.consume("list_files")
    assert stop is not None
    assert "agotada" in stop
    budget.reset()
    assert budget.used == 0
    assert budget.consume("list_files") is None


def test_recursive_list_forbidden(tmp_path):
    (tmp_path / "a.ts").write_text("x", encoding="utf-8")
    dedupe = ToolCallDedupe(max_repeats=5)
    budget = ExploreBudget(max_calls=5, max_tools_before_write=50)
    tools = wrap_tools_with_dedupe([list_files], dedupe, budget)
    out = tools[0].invoke({"path": str(tmp_path), "recursive": True})
    assert "recursive=true" in out.lower() or "prohibido" in out.lower()


def test_force_write_blocks_non_productive_tools(tmp_path):
    """max_tools_before_write blocks NON-productive tools (not read_file/git_status/etc).
    read_file is in PRODUCTIVE_TOOL_NAMES so it bypasses force-write (needed for REVIEW)."""
    from orchestration.tool_dedupe import PRODUCTIVE_TOOL_NAMES
    assert "read_file" in PRODUCTIVE_TOOL_NAMES
    budget = ExploreBudget(
        max_calls=10,
        max_reads_after_explore=20,
        max_tools_before_write=2,
    )
    # read_file is productive → never triggers force-write
    budget.consume("read_file", {"path": "/a"})
    budget.consume("read_file", {"path": "/b"})
    budget.consume("read_file", {"path": "/c"})  # 3rd read_file still OK (productive)
    budget.consume("run_lint", {"path": "/repo"})  # verify tool (productive) still OK
    # A hypothetical non-productive tool WOULD raise exception:
    with pytest.raises(ToolBudgetExceeded, match="write_file"):
        budget.consume("some_random_tool", {})


def test_reads_limited_after_explore(tmp_path):
    f = tmp_path / "a.ts"
    f.write_text("ok\n", encoding="utf-8")
    dedupe = ToolCallDedupe(max_repeats=20)
    budget = ExploreBudget(
        max_calls=1,
        max_reads_after_explore=1,
        max_tools_before_write=50,
    )
    tools = wrap_tools_with_dedupe([list_files, read_file], dedupe, budget)
    by_name = {t.name: t for t in tools}
    assert "⛔" not in by_name["list_files"].invoke(
        {"path": str(tmp_path), "recursive": False}
    )
    assert "ok" in by_name["read_file"].invoke({"path": str(f)})
    stop = by_name["read_file"].invoke({"path": str(f)})
    assert "Demasiados read_file" in stop


def test_write_resets_pressure(tmp_path):
    """After write_file, _wrote=True so force-write check is satisfied."""
    f = tmp_path / "a.ts"
    target = tmp_path / "out.ts"
    f.write_text("ok\n", encoding="utf-8")
    dedupe = ToolCallDedupe(max_repeats=20)
    budget = ExploreBudget(max_calls=5, max_tools_before_write=2)
    tools = wrap_tools_with_dedupe([read_file, write_file], dedupe, budget)
    by_name = {t.name: t for t in tools}
    by_name["read_file"].invoke({"path": str(f)})
    w = by_name["write_file"].invoke({"path": str(target), "content": "x"})
    assert "STOP" not in w
    assert budget._wrote is True
    # Now productive tools keep working without force-write pressure
    assert "ok" in by_name["read_file"].invoke({"path": str(f)})


def test_max_calls_zero_exhausts_immediately(tmp_path):
    """Bug fix: max_calls=0 (hints_on path) must set _explore_exhausted=True
    so that read_file is limited immediately, preventing infinite loops."""
    f = tmp_path / "a.ts"
    f.write_text("ok\n", encoding="utf-8")
    dedupe = ToolCallDedupe(max_repeats=20)
    budget = ExploreBudget(
        max_calls=0,
        max_reads_after_explore=1,
        max_tools_before_write=50,
    )
    tools = wrap_tools_with_dedupe([read_file, list_files], dedupe, budget)
    by_name = {t.name: t for t in tools}
    # First read_file allowed (max_reads_after_explore=1)
    assert "ok" in by_name["read_file"].invoke({"path": str(f)})
    # Second read_file raises (max_calls=0 → exception, not string)
    with pytest.raises(ToolBudgetExceeded, match="Demasiados read_file"):
        by_name["read_file"].invoke({"path": str(f)})


def test_hacer_review_with_pasted_checklist_is_review():
    """Leading 'hacer review' overrides 'Implementación' in pasted checklist."""
    msg = (
        'hacer review "✅ Tarea 3 completada. Checklist verificado:\n'
        "| Criterio | Estado | Implementación |\n"
        "|---|---|---|\n"
        "| Retry | ✅ | requestWithRetry() |"
    )
    assert classify_intent(None, msg) == Intent.REVIEW


def test_hacer_code_review_de_la_tarea_is_review():
    msg = "hacer code review de la Tarea 3"
    assert classify_intent(None, msg) == Intent.REVIEW


def test_command_prefix_stops_at_paste_markers():
    msg = 'hacer review "✅ Tarea 3 completada. Implementación correcta.'
    prefix = _extract_command_prefix(msg)
    assert "implementación" not in prefix.lower()
    assert "review" in prefix


def test_command_prefix_plain_message():
    msg = "implementá la Tarea 3 de tasks.md"
    prefix = _extract_command_prefix(msg)
    assert "implementá" in prefix
    assert classify_intent(None, msg) == Intent.EXECUTE


def test_command_prefix_review_only():
    msg = "hacer code review"
    prefix = _extract_command_prefix(msg)
    assert "review" in prefix
    assert classify_intent(None, msg) == Intent.REVIEW
