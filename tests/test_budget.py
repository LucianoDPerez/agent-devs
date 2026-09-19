"""Unit tests for tool budget and dedupe logic."""
import pytest

from config import EXECUTE_BULK_MAX_ATTEMPTS
from orchestration.session import _CONTEXT_LIMIT, _bulk_budget
from orchestration.tool_dedupe import (
    ExploreBudget,
    ToolBudgetExceeded,
    VerifyRequired,
)


class TestBulkBudget:
    """Tareas bulk (14 archivos): budgets escalados para no cortar las lecturas."""

    def test_bulk_14_files_scales_budgets(self):
        bb = _bulk_budget(14)
        assert bb["max_reads_after_explore"] >= 18  # 14 + 4
        assert bb["max_tools_before_write"] >= 36  # 2*14 + 8
        assert bb["max_writes_before_verify"] >= 14
        assert bb["tool_calls_per_turn"] >= 46  # 4*14 + 8 (capped 55)

    def test_bulk_tool_cap_respects_ceiling(self):
        assert _bulk_budget(30)["tool_calls_per_turn"] <= 55
        assert _bulk_budget(30)["max_reads_after_explore"] <= 24

    def test_bulk_small_keeps_defaults(self):
        bb = _bulk_budget(1)
        assert bb["tool_calls_per_turn"] >= 30
        assert bb["max_reads_after_explore"] >= 5

    def test_context_limit_matches_62000_window(self):
        assert _CONTEXT_LIMIT == 55800  # 90% de n_ctx=62000

    def test_bulk_max_attempts_covers_full_flow(self):
        # Exploración → escritura → compuerta verify → completar faltantes
        assert EXECUTE_BULK_MAX_ATTEMPTS >= 6


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


class TestExploreBudgetAnalyze:
    """Modo ANALYZE/PLAN (write_pressure=False): capa la búsqueda MCP sin
    presionar a escribir. Al agotarse lanza ToolBudgetExceeded (no string)."""

    def setup_method(self):
        self.budget = ExploreBudget(
            max_calls=2,
            max_reads_after_explore=3,
            max_tools_before_write=0,
            write_pressure=False,
        )

    def test_mcp_search_graph_consumes_explore_budget(self):
        assert self.budget.consume("cm__search_graph", {"query": "a"}) is None
        assert self.budget.consume("cm__search_graph", {"query": "b"}) is None
        # Tercera búsqueda (query DISTINTA, no la atrapa el dedupe) → excepción
        with pytest.raises(ToolBudgetExceeded, match="Exploración agotada"):
            self.budget.consume("cm__search_graph", {"query": "c"})

    def test_get_code_snippet_is_read_not_explore(self):
        assert self.budget.consume("cm__get_code_snippet", {"qualified_name": "A"}) is None
        assert self.budget.used == 0  # no gastó exploración

    def test_reads_limited_after_explore_analyze(self):
        budget = ExploreBudget(
            max_calls=1,
            max_reads_after_explore=1,
            max_tools_before_write=0,
            write_pressure=False,
        )
        budget.consume("cm__search_graph", {})  # explora 1 → explore exhausted
        assert budget.consume("cm__get_code_snippet", {"qualified_name": "A"}) is None
        with pytest.raises(ToolBudgetExceeded, match="Demasiadas lecturas"):
            budget.consume("cm__get_code_snippet", {"qualified_name": "B"})

    def test_analyze_never_forced_to_write(self):
        budget = ExploreBudget(
            max_calls=10,
            max_reads_after_explore=50,
            max_tools_before_write=2,
            write_pressure=False,
        )
        for i in range(20):
            assert budget.consume("cm__get_code_snippet", {"qualified_name": f"F{i}"}) is None
        # write_pressure=False → nunca se exige write aunque haya muchas tools
        assert budget.consume("cm__search_graph", {"query": "z"}) is None


class TestRedundantListing:
    """Listados redundantes: bloqueados SIN consumir budget (E2E real T1).

    El 4B listaba src recursive + src plano + subdirs (ya incluidos) y quemaba
    el budget de exploración con 0 reads. El redundante devuelve STOP y no
    cuenta en _count/_total."""

    def _budget(self):
        return ExploreBudget(
            max_calls=4,
            max_reads_after_explore=8,
            max_tools_before_write=12,
            write_pressure=False,
        )

    def test_repeat_flat_same_path_blocked_free(self, tmp_path):
        b = self._budget()
        assert b.consume("list_files", {"path": str(tmp_path), "recursive": False}) is None
        assert b.used == 1
        stop = b.consume("list_files", {"path": str(tmp_path), "recursive": False})
        assert stop is not None and "Ya listaste" in stop
        assert b.used == 1  # no consumió budget
        assert b._total == 1

    def test_recursive_never_tracked_as_listed(self, tmp_path):
        # recursive=true lo bloquea la regla de abajo (no ejecuta): no debe
        # envenenar el tracking y bloquear un flat posterior que SÍ aporta.
        b = self._budget()
        blocked = b.consume("list_files", {"path": str(tmp_path), "recursive": True})
        assert blocked is not None and "recursive=true" in blocked
        assert b.consume("list_files", {"path": str(tmp_path), "recursive": False}) is None
        assert b.used == 1

    def test_different_dirs_not_blocked(self, tmp_path):
        b = self._budget()
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        assert b.consume("list_files", {"path": str(tmp_path / "a")}) is None
        assert b.consume("list_files", {"path": str(tmp_path / "b")}) is None
        assert b.used == 2

    def test_nonexistent_path_not_tracked(self, tmp_path):
        b = self._budget()
        missing = str(tmp_path / "nope")
        assert b.consume("list_files", {"path": missing}) is None
        assert b.consume("list_files", {"path": missing}) is None
        assert b.used == 2  # al no existir, no se marca como listado

    def test_reset_clears_listing_memory(self, tmp_path):
        b = self._budget()
        assert b.consume("list_files", {"path": str(tmp_path), "recursive": False}) is None
        assert b.consume("list_files", {"path": str(tmp_path), "recursive": False}) is not None
        b.reset()
        assert b.consume("list_files", {"path": str(tmp_path), "recursive": False}) is None
        assert b.used == 1


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


class TestEditsPerFile:
    """Tope de edit_file al MISMO archivo sin verify en el medio (anti-loop)."""

    def setup_method(self):
        self.budget = ExploreBudget(
            max_calls=5,
            max_reads_after_explore=10,
            max_tools_before_write=30,
            max_edits_per_file=3,
        )

    def test_edits_below_cap_allowed(self):
        for i in range(3):
            result = self.budget.consume(
                "edit_file", {"path": "/repo/a.ts", "old_str": f"x{i}", "new_str": "y"}
            )
            assert result is None

    def test_edits_past_cap_raise(self):
        for i in range(3):
            self.budget.consume("edit_file", {"path": "/repo/a.ts", "old_str": f"x{i}", "new_str": "y"})
        with pytest.raises(ToolBudgetExceeded, match="edit_file"):
            self.budget.consume("edit_file", {"path": "/repo/a.ts", "old_str": "x", "new_str": "y"})

    def test_edits_to_other_file_not_blocked(self):
        for i in range(3):
            self.budget.consume("edit_file", {"path": "/repo/a.ts", "old_str": f"x{i}", "new_str": "y"})
        # Otro archivo: cuenta aparte
        assert self.budget.consume("edit_file", {"path": "/repo/b.ts", "old_str": "z", "new_str": "w"}) is None

    def test_verify_resets_edit_counter(self):
        for i in range(3):
            self.budget.consume("edit_file", {"path": "/repo/a.ts", "old_str": f"x{i}", "new_str": "y"})
        self.budget.consume("run_build", {"path": "/repo"})
        # Tras verify, editar de nuevo es legítimo
        assert self.budget.consume("edit_file", {"path": "/repo/a.ts", "old_str": "x", "new_str": "y"}) is None


class TestWritesBeforeVerify:
    """Tope de escrituras TOTALES (cualquier archivo) sin verify en el medio.

    Anti-spree multi-archivo (iteración de Medicos: 15 writes ciegos a
    package.json x3 + 4 archivos de scaffolding, 0 verificación).
    """

    def setup_method(self):
        self.budget = ExploreBudget(
            max_calls=3,
            max_reads_after_explore=10,
            max_tools_before_write=30,
            max_writes_before_verify=3,
        )

    def test_writes_below_cap_allowed(self):
        for i in range(3):
            result = self.budget.consume(
                "write_file", {"path": f"/repo/f{i}.ts", "content": "x"}
            )
            assert result is None

    def test_writes_across_files_past_cap_raise(self):
        """El spree multi-archivo SÍ se atrapa (a diferencia de max_edits_per_file)."""
        for i in range(3):
            self.budget.consume("write_file", {"path": f"/repo/f{i}.ts", "content": "x"})
        with pytest.raises(VerifyRequired, match="verify"):
            self.budget.consume("write_file", {"path": "/repo/f3.ts", "content": "x"})

    def test_verify_required_is_tool_budget_exceeded(self):
        """Subclase: cualquier handler genérico de ToolBudgetExceeded la atrapa."""
        assert issubclass(VerifyRequired, ToolBudgetExceeded)

    def test_verify_resets_write_counter(self):
        for i in range(3):
            self.budget.consume("write_file", {"path": f"/repo/f{i}.ts", "content": "x"})
        self.budget.consume("run_tests", {"path": "/repo"})
        # Tras verify, escribir de nuevo es legítimo
        assert self.budget.consume("write_file", {"path": "/repo/f3.ts", "content": "x"}) is None

    def test_git_write_counts_toward_cap(self):
        """Commit sin verificar también queda bloqueado."""
        for i in range(3):
            self.budget.consume("write_file", {"path": f"/repo/f{i}.ts", "content": "x"})
        with pytest.raises(VerifyRequired):
            self.budget.consume("stage_files", {"path": "/repo"})

    def test_reset_clears_write_counter(self):
        for i in range(3):
            self.budget.consume("write_file", {"path": f"/repo/f{i}.ts", "content": "x"})
        self.budget.reset()
        assert self.budget.consume("write_file", {"path": "/repo/f3.ts", "content": "x"}) is None

    def test_disabled_when_zero(self):
        """Default 0 → comportamiento histórico sin tope."""
        budget = ExploreBudget(
            max_calls=3,
            max_reads_after_explore=10,
            max_tools_before_write=30,
        )
        for i in range(15):
            assert budget.consume("write_file", {"path": f"/repo/f{i}.ts", "content": "x"}) is None

    def test_analyze_never_raises_verify_required(self):
        """write_pressure=False (ANALYZE/PLAN) → el tope no aplica."""
        budget = ExploreBudget(
            max_calls=3,
            max_reads_after_explore=10,
            max_tools_before_write=0,
            write_pressure=False,
            max_writes_before_verify=2,
        )
        for i in range(5):
            assert budget.consume("write_file", {"path": f"/repo/f{i}.ts", "content": "x"}) is None


class TestLimitReadsNow:
    """limit_reads_now(): tope de lecturas activo DESDE EL INICIO (retry write-only)."""

    def test_reads_capped_immediately(self):
        budget = ExploreBudget(
            max_calls=3,
            max_reads_after_explore=2,
            max_tools_before_write=30,
        )
        budget.limit_reads_now()
        assert budget.consume("read_file", {"path": "/repo/a.ts"}) is None
        assert budget.consume("read_file", {"path": "/repo/b.ts"}) is None
        # Tercera lectura → bloqueada aunque nunca haya explorado
        result = budget.consume("read_file", {"path": "/repo/c.ts"})
        assert result is not None
        assert "read_file" in result

    def test_explore_still_allowed_once_after_limit(self):
        """max_calls=1 + limit_reads_now: UNA búsqueda sigue permitida."""
        budget = ExploreBudget(
            max_calls=1,
            max_reads_after_explore=2,
            max_tools_before_write=30,
        )
        budget.limit_reads_now()
        assert budget.consume("search_code", {"pattern": "foo"}) is None
        result = budget.consume("search_code", {"pattern": "bar"})
        assert result is not None


class TestVerifyStreak:
    """Tope de verify calls SEGUIDAS sin escribir (anti-loop run_lint)."""

    def setup_method(self):
        self.budget = ExploreBudget(
            max_calls=3,
            max_reads_after_explore=10,
            max_tools_before_write=30,
            max_writes_before_verify=6,
            max_verify_before_write=3,
        )

    def test_verify_below_cap_allowed(self):
        for _ in range(3):
            assert self.budget.consume("run_lint", {"path": "/repo"}) is None

    def test_verify_loop_raises(self):
        for _ in range(3):
            self.budget.consume("run_lint", {"path": "/repo"})
        with pytest.raises(ToolBudgetExceeded, match="verifies seguidos"):
            self.budget.consume("run_lint", {"path": "/repo"})

    def test_write_resets_verify_streak(self):
        for _ in range(3):
            self.budget.consume("run_lint", {"path": "/repo"})
        self.budget.consume("edit_file", {"path": "/repo/a.ts", "old_str": "x", "new_str": "y"})
        assert self.budget.consume("run_lint", {"path": "/repo"}) is None

    def test_disabled_when_no_write_cap(self):
        """REVIEW (max_writes_before_verify=0) → verify sin límite de streak."""
        budget = ExploreBudget(
            max_calls=3,
            max_reads_after_explore=15,
            max_tools_before_write=30,
        )
        for _ in range(10):
            assert budget.consume("run_lint", {"path": "/repo"}) is None


class TestVerifyCache:
    """Repetir verify sin writes intermedios devuelve caché (E2E real 35B:
    3 rondas lint/tests/build = 9 ejecuciones para 2 edits de un JSON)."""

    def _budget(self):
        return ExploreBudget(
            max_calls=10,
            max_reads_after_explore=8,
            max_tools_before_write=50,
            write_pressure=True,
        )

    def test_segunda_ronda_igual_path_devuelve_cache(self):
        b = self._budget()
        kw = {"path": "/repo"}
        assert b.consume("run_lint", kw) is None
        b.note_verify("run_lint", kw, "[PASSED] exit=0\n$ tsc")
        out = b.consume("run_lint", kw)
        assert out is not None and "ya verificado" in out
        # Sin costo: ni total ni streak avanzan
        assert b._total == 1
        assert b._verify_streak == 0

    def test_write_invalida_cache(self):
        b = self._budget()
        kw = {"path": "/repo"}
        b.consume("run_lint", kw)
        b.note_verify("run_lint", kw, "[PASSED] exit=0")
        assert b.consume("run_lint", kw) is not None  # cache hit
        b.consume("edit_file", {"path": "/repo/a.ts", "old_str": "x", "new_str": "y"})
        assert b.consume("run_lint", kw) is None  # re-ejecuta

    def test_fallo_no_cachea(self):
        b = self._budget()
        kw = {"path": "/repo"}
        b.consume("run_tests", kw)
        b.note_verify("run_tests", kw, "[FAILED] exit=1\n$ pytest\nF")
        assert b.consume("run_tests", kw) is None  # reintenta, no cachea fallos

    def test_path_distinto_es_otra_entrada(self):
        b = self._budget()
        b.consume("run_build", {"path": "/repo/a"})
        b.note_verify("run_build", {"path": "/repo/a"}, "[PASSED] exit=0")
        assert b.consume("run_build", {"path": "/repo/b"}) is None

    def test_reset_limpia_cache(self):
        b = self._budget()
        kw = {"path": "/repo"}
        b.consume("run_lint", kw)
        b.note_verify("run_lint", kw, "[PASSED] exit=0")
        b.reset()
        assert b.consume("run_lint", kw) is None


class TestVerifyCacheHitStreak:
    """E2E real T004: 12 run_verify repetidos tras PASSED (el gate retry con
    force_tool_calls no deja cerrar con texto). El cache-hit devolvía string
    ignorable sin topear NADA: el 2º cache-hit seguido debe levantar excepción."""

    def _budget(self):
        return ExploreBudget(
            max_calls=10,
            max_reads_after_explore=8,
            max_tools_before_write=50,
            write_pressure=True,
            max_writes_before_verify=4,
            max_verify_before_write=5,
        )

    def test_segundo_cache_hit_lanza(self):
        from orchestration.tool_dedupe import ToolBudgetExceeded

        b = self._budget()
        kw = {"path": "/repo"}
        assert b.consume("run_verify", kw) is None
        b.note_verify("run_verify", kw, "[PASSED] batería verde")
        out = b.consume("run_verify", kw)
        assert "ya verificado" in out  # 1er cache-hit: aviso
        with pytest.raises(ToolBudgetExceeded, match="ritual"):
            b.consume("run_verify", kw)  # 2do cache-hit: excepción

    def test_write_resetea_streak(self):
        b = self._budget()
        kw = {"path": "/repo"}
        b.consume("run_verify", kw)
        b.note_verify("run_verify", kw, "[PASSED] verde")
        assert b.consume("run_verify", kw) is not None  # cache hit 1
        b.consume("edit_file", {"path": "/r/a.ts", "old_str": "a", "new_str": "b"})
        # Write invalida el cache → la próxima verify es real (no cache-hit)
        assert b.consume("run_verify", kw) is None
        assert b._verify_cache_hit_streak == 0

    def test_verify_real_resetea_streak(self):
        b = self._budget()
        kw = {"path": "/repo"}
        b.consume("run_lint", kw)
        b.note_verify("run_lint", kw, "[PASSED] x")
        assert b.consume("run_lint", kw) is not None  # cache hit 1
        b._verify_cache_hit_streak = 1
        # Verify de OTRA key ejecuta de verdad → streak a 0
        assert b.consume("run_tests", kw) is None
        assert b._verify_cache_hit_streak == 0

    def test_reset_limpia_streak(self):
        b = self._budget()
        b._verify_cache_hit_streak = 5
        b.reset()
        assert b._verify_cache_hit_streak == 0


class TestNpmScriptVerifyEquivalence:
    """run_npm_script lint*/test*/build* es verificación (E2E real: el modelo
    corrió `lint:check` + `build` x2 por run_npm_script y el harness no lo
    contó: ni reseteó edits, ni alimentó resultados, ni tocó el caché)."""

    def test_mapeo_scripts(self):
        from orchestration.tool_dedupe import _canonical_verify_for_script as m

        assert m("lint") == "run_lint"
        assert m("lint:check") == "run_lint"
        assert m("test") == "run_tests"
        assert m("test:unit") == "run_tests"
        assert m("build") == "run_build"
        assert m("db:generate") is None
        assert m("dev") is None
        assert m("install") is None
        assert m("") is None

    def test_verify_like_resetea_edits(self):
        from orchestration.tool_dedupe import ExploreBudget

        b = ExploreBudget(
            max_calls=10,
            max_reads_after_explore=8,
            max_tools_before_write=50,
            write_pressure=True,
            max_edits_per_file=2,
        )
        b.consume("edit_file", {"path": "/r/a.ts", "old_str": "a", "new_str": "b"})
        b.consume("edit_file", {"path": "/r/a.ts", "old_str": "c", "new_str": "d"})
        assert b.consume("run_npm_script", {"path": "/r", "script": "lint:check"}) is None
        assert b.consume("edit_file", {"path": "/r/a.ts", "old_str": "e", "new_str": "f"}) is None

    def test_verify_like_no_bloquea_por_dedupe(self):
        from orchestration.tool_dedupe import ToolCallDedupe, wrap_tools_with_dedupe
        from tools.verify import run_npm_script

        dedupe = ToolCallDedupe(max_repeats=1)
        wrapped = wrap_tools_with_dedupe([run_npm_script], dedupe)
        npm = wrapped[0]
        # Misma llamada 3 veces: al ser verify-equivalente nunca se bloquea
        # por dedupe (idempotente como run_lint/run_tests/run_build).
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "package.json").write_text('{"scripts": {"lint:check": "exit 1"}}')
            for _ in range(3):
                out = npm.invoke({"path": tmp, "script": "lint:check"})
                assert isinstance(out, str)

    def test_resultado_se_registra_canonico(self):
        from orchestration.tool_dedupe import _record_verify_result

        res: dict = {}
        _record_verify_result(
            "run_npm_script", "[PASSED] exit=0", res,
            {"path": "/r", "script": "build"},
        )
        assert res == {"run_build": True}
        res2: dict = {}
        _record_verify_result(
            "run_npm_script", "db ok", res2, {"path": "/r", "script": "db:generate"}
        )
        assert res2 == {}


class TestStatusFlipPassthrough:
    """Pasillo del flujo del usuario: status pending→DONE en tasks.json no se
    intercepta en el wrapper; filesystem valida el JSON completo."""

    def test_wrapper_permite_flip_y_bloquea_otros(self, tmp_path):
        import json

        from orchestration.tool_dedupe import (
            ExploreBudget,
            ToolCallDedupe,
            wrap_tools_with_dedupe,
        )
        from tools.filesystem import edit_file, read_file

        p = tmp_path / ".agent" / "tasks.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(
            {"tasks": [{"id": "T006", "status": "pending", "file": "a.ts"}]},
            indent=2,
        ))
        read_file.invoke({"path": str(p)})
        dd = ToolCallDedupe(max_repeats=9)
        w = wrap_tools_with_dedupe([edit_file], dd, ExploreBudget(),
                                   repo_path=str(tmp_path))[0]
        r1 = w.invoke({"path": str(p), "old_str": '"status": "pending"',
                       "new_str": '"status": "DONE"'})
        assert "Replaced" in r1
        assert json.loads(p.read_text())["tasks"][0]["status"] == "DONE"

        r2 = w.invoke({"path": str(p), "old_str": '"file": "a.ts"',
                       "new_str": '"file": "b.ts"'})
        assert "PROTEGIDO" in r2
        assert "ÚNICA edición permitida" in r2

    def test_flip_candidate_heuristica(self):
        from orchestration.tool_dedupe import _status_flip_candidate as f

        assert f("edit_file", {"old_str": '"status": "pending"', "new_str": '"status": "DONE"'}) is True
        assert f("edit_file", {"old_str": '"file": "a.ts"', "new_str": '"file": "b.ts"'}) is False
        # un-done: old ya tiene done → NO es candidate (el wrapper lo frena)
        assert f("edit_file", {"old_str": '"status": "DONE"', "new_str": '"status": "pending"'}) is False
        assert f("write_file", {"content": '{"status": "DONE"}'}) is True


def test_verify_validacion_no_registra_como_falso():
    """T005: run_lint sobre un ARCHIVO → 'is not a directory' (mensaje de
    validación). NO debe registrar False que envenene el cierre."""
    from orchestration.tool_dedupe import _record_verify_result

    res: dict = {}
    _record_verify_result(
        "run_lint", "'/repo/x.ts' is not a directory.", res, {"path": "/repo/x.ts"}
    )
    assert res == {}  # no es veredicto, es mal uso
    _record_verify_result(
        "run_lint", "[FAILED] exit=1\nF test", res, {"path": "/repo"}
    )
    assert res == {"run_lint": False}


class TestApplyPatchStatusFlip:
    """T013: el modelo marca DONE con apply_patch atómico — sin pasillo el
    bloqueo le rompía el cierre y alucinaba éxito."""

    def test_flip_por_patch_permite(self, tmp_path):
        import json

        from tools.filesystem import apply_patch, read_file

        p = tmp_path / ".agent" / "tasks.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(
            {"tasks": [{"id": "T013", "title": "x", "status": "pending", "file": "a.ts"}]},
            indent=2,
        ))
        read_file.invoke({"path": str(p)})
        edits = json.dumps([{
            "old_string": '"status": "pending",\n      "file": "a.ts"',
            "new_string": '"status": "DONE",\n      "file": "a.ts"',
        }])
        r = apply_patch.invoke({"path": str(p), "edits": edits})
        assert r.startswith("✅")
        assert json.loads(p.read_text())["tasks"][0]["status"] == "DONE"

    def test_patch_con_otros_cambios_bloquea(self, tmp_path):
        import json

        from tools.filesystem import apply_patch

        p = tmp_path / ".agent" / "tasks.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(
            {"tasks": [{"id": "T013", "status": "pending", "file": "a.ts"}]},
            indent=2,
        ))
        edits = json.dumps([{"old_string": '"file": "a.ts"', "new_string": '"file": "b.ts"'}])
        r = apply_patch.invoke({"path": str(p), "edits": edits})
        assert "PROHIBIDO" in r
        assert json.loads(p.read_text())["tasks"][0]["file"] == "a.ts"

    def test_candidate_patch_heuristica(self):
        import json

        from orchestration.tool_dedupe import _status_flip_candidate as f

        flip = json.dumps([{
            "old_string": '"status": "pending"', "new_string": '"status": "DONE"',
        }])
        mixto = json.dumps([
            {"old_string": '"status": "pending"', "new_string": '"status": "DONE"'},
            {"old_string": '"file": "a.ts"', "new_string": '"file": "b.ts"'},
        ])
        assert f("apply_patch", {"edits": flip}) is True
        assert f("apply_patch", {"edits": mixto}) is False


def test_write_bloqueado_no_cuenta_como_escrito(tmp_path):
    """T014: un apply_patch/edit bloqueado quedaba en _called_tools como
    "wrote" y el cierre honesto de 'ya está implementado' no disparaba →
    turno fallido. El write fallido se descarta del logger."""
    from orchestration.session import _ToolCallLog
    from orchestration.tool_dedupe import (
        ToolCallDedupe,
        wrap_tools_with_dedupe,
    )
    from tools.filesystem import edit_file, read_file

    p = tmp_path / "a.ts"
    p.write_text("x = 1\n", encoding="utf-8")
    read_file.invoke({"path": str(p)})
    dd = ToolCallDedupe(max_repeats=9)
    logged = _ToolCallLog()
    w = wrap_tools_with_dedupe([edit_file], dd, None, repo_path=str(tmp_path),
                               tool_call_logger=logged)[0]
    r1 = w.invoke({"path": str(p), "old_str": "ZZZ-NO-EXISTE", "new_str": "y"})
    assert "old_str not found" in r1
    assert logged.names == set()  # no escribió → no cuenta
    r2 = w.invoke({"path": str(p), "old_str": "x = 1", "new_str": "x = 2"})
    assert r2.startswith("✅")
    assert logged.names == {"edit_file"}  # éxito sí cuenta
