"""Tests para la clasificación de fallos del harness de benchmark
(_same_failure / _failure_kind en benchmarks/run.py)."""

from benchmarks.run import _failing_tests, _failure_kind, _same_failure


def _crit(exit_code, tail):
    return {"exit": exit_code, "tail": tail}


class TestFailureKind:
    def test_absence_markers(self):
        assert _failure_kind("ls: src/x: No such file or directory") == "absence"
        assert _failure_kind("No tests found for given includes: [*X*]") == "absence"
        assert _failure_kind("no Go files in /repo/internal/nr") == "absence"
        assert _failure_kind("stat /repo/telemetry/cmd/x: directory not found") == "absence"

    def test_build_markers(self):
        assert _failure_kind("FAILURE: Build failed with an exception.") == "build"
        assert _failure_kind("error: cannot find symbol\n  symbol: method getList") == "build"

    def test_test_markers(self):
        assert _failure_kind("BaseControllerTest > login FAILED") == "test"
        assert _failure_kind("=== 2 failed, 10 passed ===") == "test"

    def test_other(self):
        assert _failure_kind("") == "other"
        assert _failure_kind("0\n") == "other"

    def test_build_failed_takes_precedence_over_absence(self):
        # "cannot find symbol" es un error de compilación, NO una ausencia
        assert _failure_kind("error: cannot find symbol") == "build"


class TestFailingTests:
    def test_gradle(self):
        tail = "    BaseControllerTest > loginReturns200 FAILED\n    UserControllerTest > list FAILED"
        assert _failing_tests(tail) == frozenset(
            {"basecontrollertest > loginreturns200 failed", "usercontrollertest > list failed"}
        )

    def test_go(self):
        assert _failing_tests("--- FAIL: TestResolveUsername (0.01s)") == frozenset({"testresolveusername"})

    def test_pytest(self):
        assert _failing_tests("FAILED tests/test_x.py::test_foo - AssertionError") == frozenset(
            {"tests/test_x.py::test_foo"}
        )


class TestSameFailure:
    def test_absence_never_inherited(self):
        base = _crit(1, "No tests found for given includes: [*RepositoryController*]")
        now = _crit(1, "No tests found for given includes: [*RepositoryController*]")
        assert not _same_failure(base, now)

    def test_absence_to_build_is_agent_error(self):
        base = _crit(1, "No tests found for given includes: [*X*]")
        now = _crit(1, "FAILURE: Build failed with an exception.\n> error: cannot find symbol")
        assert not _same_failure(base, now)

    def test_identical_build_failure_is_inherited(self):
        base = _crit(1, "FAILURE: Build failed with an exception.\n> Execution failed for task ':compileJava'.")
        now = _crit(1, "FAILURE: Build failed with an exception.\n> Execution failed for task ':compileJava'.")
        assert _same_failure(base, now)

    def test_different_build_errors_are_not_inherited(self):
        base = _crit(1, "FAILURE: Build failed.\n> error: cannot find symbol: class Foo")
        now = _crit(1, "FAILURE: Build failed.\n> error: cannot find symbol: class Bar")
        assert not _same_failure(base, now)

    def test_same_failing_tests_inherited(self):
        base = _crit(1, "    BaseControllerTest > login FAILED")
        now = _crit(1, "    BaseControllerTest > login FAILED")
        assert _same_failure(base, now)

    def test_new_failing_test_is_agent_error(self):
        base = _crit(1, "    BaseControllerTest > login FAILED")
        now = _crit(1, "    BaseControllerTest > login FAILED\n    UserControllerTest > list FAILED")
        assert not _same_failure(base, now)

    def test_other_with_empty_tails_never_inherited(self):
        base = _crit(1, "")
        now = _crit(1, "")
        assert not _same_failure(base, now)

    def test_exit_mismatch_not_inherited(self):
        base = _crit(1, "FAILURE: Build failed.")
        now = _crit(2, "FAILURE: Build failed.")
        assert not _same_failure(base, now)

    def test_kind_mismatch_not_inherited(self):
        base = _crit(1, "FAILURE: Build failed.")
        now = _crit(1, "BaseControllerTest > login FAILED")
        assert not _same_failure(base, now)
