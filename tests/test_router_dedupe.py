"""Tests for intent classifier priority, tool dedupe, and explore budget."""

from core.intents import Intent
from orchestration.router import classify_intent
from orchestration.tool_dedupe import (
    EXPLORE_TOOL_NAMES,
    ExploreBudget,
    ToolCallDedupe,
    wrap_tools_with_dedupe,
)
from tools.filesystem import list_files, read_file
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
    assert "STOP" not in r2 or r2 == r1  # second call still runs


def test_explore_budget_allows_two_then_stops(tmp_path):
    (tmp_path / "a.ts").write_text("x", encoding="utf-8")
    dedupe = ToolCallDedupe(max_repeats=5)
    budget = ExploreBudget(max_calls=2)
    tools = wrap_tools_with_dedupe([list_files], dedupe, budget)
    tool = tools[0]
    args = {"path": str(tmp_path), "recursive": False}
    r1 = tool.invoke(args)
    r2 = tool.invoke({**args})  # same path counts as 2nd explore
    # Change path so dedupe doesn't block — budget should
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
    budget = ExploreBudget(max_calls=0)  # already exhausted for explore tools
    tools = wrap_tools_with_dedupe([read_file, list_files], dedupe, budget)
    by_name = {t.name: t for t in tools}
    # read_file must still work
    content = by_name["read_file"].invoke({"path": str(f)})
    assert "ok" in content
    # list_files must STOP immediately (budget 0 → first explore call fails)
    stop = by_name["list_files"].invoke({"path": str(tmp_path), "recursive": False})
    assert "STOP: exploration budget" in stop


def test_explore_budget_covers_search_code(tmp_path):
    f = tmp_path / "a.ts"
    f.write_text("timeout = 5\n", encoding="utf-8")
    dedupe = ToolCallDedupe(max_repeats=5)
    budget = ExploreBudget(max_calls=1)
    tools = wrap_tools_with_dedupe([search_code], dedupe, budget)
    tool = tools[0]
    r1 = tool.invoke({"path": str(tmp_path), "pattern": "timeout"})
    r2 = tool.invoke({"path": str(tmp_path), "pattern": "other"})
    assert "STOP: exploration budget" not in r1
    assert "STOP: exploration budget exhausted" in r2
    assert "search_code" in EXPLORE_TOOL_NAMES


def test_explore_budget_reset():
    budget = ExploreBudget(max_calls=1)
    assert budget.consume("list_files") is None
    assert budget.consume("list_files") is not None
    budget.reset()
    assert budget.used == 0
    assert budget.consume("list_files") is None
