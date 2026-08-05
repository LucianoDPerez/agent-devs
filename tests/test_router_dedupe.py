"""Tests for intent classifier priority, tool dedupe, and explore budget."""

from core.intents import Intent
from orchestration.router import classify_intent
from orchestration.tool_dedupe import (
    EXPLORE_TOOL_NAMES,
    ExploreBudget,
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


def test_plain_review_still_review():
    assert classify_intent(None, "revisá este PR por bugs") == Intent.REVIEW


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
    r3 = tool.invoke(args)
    assert "consola" in r1 or "log" in r1
    assert "STOP: already called" in r3
    assert "STOP" not in r2 or r2 == r1


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
    assert "STOP: exploration budget" not in r1
    assert "STOP: exploration budget" not in r2
    assert "STOP: exploration budget exhausted" in r3
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
    stop = by_name["list_files"].invoke({"path": str(tmp_path), "recursive": False})
    assert "STOP: exploration budget" in stop


def test_explore_budget_covers_search_code(tmp_path):
    f = tmp_path / "a.ts"
    f.write_text("timeout = 5\n", encoding="utf-8")
    dedupe = ToolCallDedupe(max_repeats=5)
    budget = ExploreBudget(max_calls=1, max_tools_before_write=50)
    tools = wrap_tools_with_dedupe([search_code], dedupe, budget)
    tool = tools[0]
    r1 = tool.invoke({"path": str(tmp_path), "pattern": "timeout"})
    r2 = tool.invoke({"path": str(tmp_path), "pattern": "other"})
    assert "STOP: exploration budget" not in r1
    assert "STOP: exploration budget exhausted" in r2
    assert "search_code" in EXPLORE_TOOL_NAMES


def test_explore_budget_reset():
    budget = ExploreBudget(max_calls=1, max_tools_before_write=50)
    assert budget.consume("list_files") is None
    assert budget.consume("list_files") is not None
    budget.reset()
    assert budget.used == 0
    assert budget.consume("list_files") is None


def test_recursive_list_forbidden(tmp_path):
    (tmp_path / "a.ts").write_text("x", encoding="utf-8")
    dedupe = ToolCallDedupe(max_repeats=5)
    budget = ExploreBudget(max_calls=5, max_tools_before_write=50)
    tools = wrap_tools_with_dedupe([list_files], dedupe, budget)
    out = tools[0].invoke({"path": str(tmp_path), "recursive": True})
    assert "recursive=true" in out.lower() or "PROHIBIDO" in out or "prohibido" in out


def test_force_write_after_too_many_tools(tmp_path):
    f = tmp_path / "a.ts"
    f.write_text("ok\n", encoding="utf-8")
    dedupe = ToolCallDedupe(max_repeats=20)
    budget = ExploreBudget(
        max_calls=10,
        max_reads_after_explore=20,
        max_tools_before_write=3,
    )
    tools = wrap_tools_with_dedupe([read_file], dedupe, budget)
    tool = tools[0]
    args = {"path": str(f)}
    assert "STOP" not in tool.invoke(args)
    assert "STOP" not in tool.invoke(args)
    assert "STOP" not in tool.invoke(args)
    # 4th non-write → STOP must write
    stop = tool.invoke(args)
    assert "STOP" in stop
    assert "write_file" in stop


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
    assert "STOP: exploration" not in by_name["list_files"].invoke(
        {"path": str(tmp_path), "recursive": False}
    )
    assert "ok" in by_name["read_file"].invoke({"path": str(f)})
    stop = by_name["read_file"].invoke({"path": str(f)})
    assert "STOP: demasiados read_file" in stop


def test_write_resets_pressure(tmp_path):
    """After write_file, further tools are allowed again (within explore budget)."""
    f = tmp_path / "a.ts"
    target = tmp_path / "out.ts"
    f.write_text("ok\n", encoding="utf-8")
    dedupe = ToolCallDedupe(max_repeats=20)
    budget = ExploreBudget(max_calls=5, max_tools_before_write=2)
    tools = wrap_tools_with_dedupe([read_file, write_file], dedupe, budget)
    by_name = {t.name: t for t in tools}
    by_name["read_file"].invoke({"path": str(f)})
    by_name["read_file"].invoke({"path": str(f)})
    # would stop on 3rd without write — write first
    w = by_name["write_file"].invoke({"path": str(target), "content": "x"})
    assert "STOP" not in w
    # now reads ok again because wrote=True
    assert "ok" in by_name["read_file"].invoke({"path": str(f)})
