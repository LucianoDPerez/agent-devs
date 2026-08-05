"""Unit tests for tool budget and dedupe logic."""
import pytest
from orchestration.tool_dedupe import ExploreBudget, ToolBudgetExceeded


class TestExploreBudgetReview:
    """Review role should allow many reads and tool calls without forcing writes."""

    def setup_method(self):
        self.budget = ExploreBudget(
            max_calls=1,
            max_reads_after_explore=15,
            max_tools_before_write=30,
        )

    def test_parallel_read_git_status_changed_files(self):
        """Reviewer can do parallel read_file + git_status + changed_files."""
        # These 3 tool calls happen in parallel at the start of review
        assert self.budget.consume("git_status", {"path": "/repo"}) is None
        assert self.budget.consume("changed_files", {"path": "/repo"}) is None
        assert self.budget.consume("read_file", {"path": "/repo/file.ts"}) is None

    def test_reviewer_can_read_many_files(self):
        """Reviewer can read all modified files without hitting budget."""
        # Simulate reading 10 files (common for a meaningful review)
        for i in range(10):
            result = self.budget.consume("read_file", {"path": f"/repo/file{i}.ts"})
            assert result is None, f"read_file #{i+1} was blocked: {result}"

    def test_reviewer_can_run_verify_tools(self):
        """Reviewer can run lint/tests/build after reading files."""
        for i in range(5):
            self.budget.consume("read_file", {"path": f"/repo/file{i}.ts"})
        assert self.budget.consume("run_lint", {"path": "/repo"}) is None
        assert self.budget.consume("run_tests", {"path": "/repo"}) is None
        assert self.budget.consume("run_build", {"path": "/repo"}) is None

    def test_reviewer_not_forced_to_write(self):
        """Reviewer should never be forced to write code."""
        # 25 tool calls (reads + verifies) — well within budget
        for i in range(20):
            self.budget.consume("read_file", {"path": f"/repo/file{i}.ts"})
        for tool in ["run_lint", "run_tests", "run_build", "git_log"]:
            self.budget.consume(tool, {"path": "/repo"})
        # Should NOT raise — reviewer produces a report, not code
        assert self.budget.consume("read_file", {"path": "/repo/last.ts"}) is None


class TestExploreBudgetExecute:
    """Execute role should allow exploring then writing."""

    def setup_method(self):
        self.budget = ExploreBudget(
            max_calls=1,
            max_reads_after_explore=5,
            max_tools_before_write=8,
        )

    def test_can_explore_once_then_write(self):
        """Executor can list files once, then write."""
        assert self.budget.consume("list_files", {"path": "/repo/src", "recursive": False}) is None
        assert self.budget.consume("read_file", {"path": "/repo/src/file.ts"}) is None
        assert self.budget.consume("edit_file", {"path": "/repo/src/file.ts", "old_str": "x", "new_str": "y"}) is None

    def test_can_read_multiple_then_write(self):
        """After explore exhausted, can read files then write."""
        self.budget.consume("list_files", {"path": "/repo/src", "recursive": False})
        for i in range(5):
            self.budget.consume("read_file", {"path": f"/repo/src/file{i}.ts"})
        assert self.budget.consume("edit_file", {"path": "/repo/src/file.ts", "old_str": "x", "new_str": "y"}) is None

    def test_cannot_explore_twice(self):
        """After max_calls, explore is blocked."""
        self.budget.consume("list_files", {"path": "/repo/src", "recursive": False})
        result = self.budget.consume("list_files", {"path": "/repo/other", "recursive": False})
        assert result is not None
        assert "agotada" in result.lower() or "prohibida" in result.lower()


class TestDedupe:
    """Tool dedupe should allow N repeats then block."""

    def test_allows_repeats_then_blocks(self):
        from orchestration.tool_dedupe import ToolCallDedupe
        dedupe = ToolCallDedupe(max_repeats=2)
        assert dedupe.register("read_file", {"path": "/a"}) == 1
        assert dedupe.register("read_file", {"path": "/a"}) == 2
        # Third call with same args should be blocked
        n = dedupe.register("read_file", {"path": "/a"})
        assert n > dedupe.max_repeats

    def test_different_args_not_deduped(self):
        from orchestration.tool_dedupe import ToolCallDedupe
        dedupe = ToolCallDedupe(max_repeats=2)
        assert dedupe.register("read_file", {"path": "/a"}) == 1
        assert dedupe.register("read_file", {"path": "/b"}) == 1  # Different path = new key


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
