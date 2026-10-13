"""Baseline de verificación: siembra, higiene, exención de infra y fix retry.

E2E real: pool frenado en T001 porque 2 tests de infra (sin PG/LocalStack)
contaron como "nuevos" — el baseline nunca se sembró (turnos sin verify
guardaban [] = "verde") y la atribución no distingue causa.
"""
from core.roles import Role
from orchestration.session import (
    _is_infra_failure_text,
    Session,
)


def _session(repo):
    sess = Session(llm=None, repo_path=str(repo))
    sess._rebuild_agent = lambda *a, **k: True  # sin LLM en tests
    return sess


class TestInfraClassifier:
    def test_infra_positives(self):
        assert _is_infra_failure_text(
            "connect ECONNREFUSED 127.0.0.1:5432")
        assert _is_infra_failure_text(
            "connection refused: Is the server running on host localhost?")
        assert _is_infra_failure_text(
            "could not connect to server: Connection timed out")
        assert _is_infra_failure_text(
            "LocalStack container not reachable on :4566")

    def test_infra_negatives(self):
        assert not _is_infra_failure_text(
            "AssertionError: expected 200 but got 500")
        assert not _is_infra_failure_text(
            "error TS2322: Type 'string' is not assignable")
        assert not _is_infra_failure_text("")
        assert not _is_infra_failure_text("   ")
        # Ambiguo a propósito: puede ser config del propio cambio → NO exime.
        assert not _is_infra_failure_text(
            "password authentication failed for user app")
        assert not _is_infra_failure_text(
            'database "app_test" does not exist')


class TestOnlyInfraReds:
    def test_todo_infra(self, tmp_path):
        s = _session(tmp_path)
        s._turn_failures = {"run_tests": "ECONNREFUSED 127.0.0.1:5432"}
        assert s._only_infra_reds() is True

    def test_mixto_no_exime(self, tmp_path):
        s = _session(tmp_path)
        s._turn_failures = {
            "run_tests": "ECONNREFUSED 127.0.0.1:5432",
            "run_lint": "error TS2322 en src/a.ts:10",
        }
        assert s._only_infra_reds() is False

    def test_sin_excerpts_no_exime(self, tmp_path):
        assert _session(tmp_path)._only_infra_reds() is False

    def test_rojo_real_no_exime(self, tmp_path):
        s = _session(tmp_path)
        s._turn_failures = {"run_tests": "FAIL src/a.test.ts: AssertionError"}
        assert s._only_infra_reds() is False


class TestStoreBaselineHygiene:
    def test_sin_verify_no_siembra_verde(self, tmp_path, monkeypatch):
        """Turno sin verificación no guarda [] (= 'verde' mentiroso)."""
        import cache

        saved = []
        monkeypatch.setattr(cache, "save_test_baseline",
                            lambda *a, **k: saved.append(1))
        s = _session(tmp_path)
        assert s._baseline is None
        s._store_baseline()
        assert s._baseline is None
        assert saved == []

    def test_rojos_observados_siempre_se_guardan(self, tmp_path, monkeypatch):
        import cache

        monkeypatch.setattr(cache, "save_test_baseline", lambda *a, **k: None)
        s = _session(tmp_path)
        s._explore_budget._failure_tails = {
            ("run_tests", "/x"): ["users-table.test.ts"]}
        s._store_baseline()
        assert s._baseline == {"failing": ["users-table.test.ts"]}

    def test_verde_real_guarda_vacio(self, tmp_path, monkeypatch):
        import cache

        monkeypatch.setattr(cache, "save_test_baseline", lambda *a, **k: None)
        s = _session(tmp_path)
        s._turn_verify_tools = {"run_lint", "run_tests", "run_build"}
        monkeypatch.setattr(s, "_verify_all_passed", lambda: True)
        s._store_baseline()
        assert s._baseline == {"failing": []}


class TestShouldFixRetry:
    def _red_turn(self, tmp_path, monkeypatch):
        s = _session(tmp_path)
        s.current_role = Role.EXECUTE
        s._turn_wrote = True
        s._turn_failures = {"run_tests": "FAIL src/a.test.ts: expected X"}
        monkeypatch.setattr(s, "_turn_verify_is_red", lambda: True)
        return s

    def test_caso_feliz(self, tmp_path, monkeypatch):
        assert self._red_turn(tmp_path, monkeypatch)._should_fix_retry() is True

    def test_un_solo_intento(self, tmp_path, monkeypatch):
        s = self._red_turn(tmp_path, monkeypatch)
        s._fix_retried = True
        assert s._should_fix_retry() is False

    def test_sin_writes_nada_que_arreglar(self, tmp_path, monkeypatch):
        s = self._red_turn(tmp_path, monkeypatch)
        s._turn_wrote = False
        assert s._should_fix_retry() is False

    def test_solo_infra_se_exime_no_se_arregla(self, tmp_path, monkeypatch):
        s = self._red_turn(tmp_path, monkeypatch)
        s._turn_failures = {"run_tests": "ECONNREFUSED 127.0.0.1:5432"}
        assert s._should_fix_retry() is False

    def test_rol_no_execute(self, tmp_path, monkeypatch):
        s = self._red_turn(tmp_path, monkeypatch)
        s.current_role = Role.ANALYZE
        assert s._should_fix_retry() is False

    def test_sin_excerpts(self, tmp_path, monkeypatch):
        s = self._red_turn(tmp_path, monkeypatch)
        s._turn_failures = {}
        assert s._should_fix_retry() is False


class TestSeedPoolBaseline:
    def test_con_baseline_no_paga_bateria(self, tmp_path, monkeypatch):
        import orchestration.session as mod

        called = []
        monkeypatch.setattr(mod, "run_commit_verification",
                            lambda *a, **k: called.append(1) or (True, "ok"))
        s = _session(tmp_path)
        s._baseline = {"failing": []}
        s._seed_pool_baseline()
        assert called == []

    def test_sin_baseline_siembra(self, tmp_path, monkeypatch):
        import orchestration.session as mod

        import cache

        def fake_verify(repo_path, capture=None, **kwargs):
            if capture is not None:
                capture.setdefault("details", {})["tests"] = (
                    "FAIL  tests/integration/users-table.test.ts > suite > caso\n"
                    "connect ECONNREFUSED 127.0.0.1:5432")
            return False, "report"

        monkeypatch.setattr(mod, "run_commit_verification", fake_verify)
        monkeypatch.setattr(cache, "save_test_baseline", lambda *a, **k: None)
        monkeypatch.setattr(cache, "snapshot_hash", lambda *a, **k: "snap")
        s = _session(tmp_path)
        assert s._ensure_baseline() is None
        s._seed_pool_baseline()
        assert s._baseline is not None
        assert any("users-table" in f for f in s._baseline["failing"])


class TestRedsAttributionInfra:
    def test_infra_se_informa_heredado(self, tmp_path):
        s = _session(tmp_path)
        s._explore_budget._failure_tails = {
            ("run_tests", "/x"): ["users-table.test.ts"]}
        s._baseline = {"failing": []}
        s._turn_failures = {"run_tests": "ECONNREFUSED 127.0.0.1:5432"}
        line = s._reds_attribution_line()
        assert "infra" in line
        assert "Nuevos: 0" in line


class TestVerifyRedClose:
    """El veredicto ante rojo: falla solo si hay rojos NUEVOS reales."""

    def _red_session(self, tmp_path, tails, baseline, failures):
        s = _session(tmp_path)
        s._explore_budget._failure_tails = tails
        s._baseline = baseline
        s._turn_failures = failures
        return s

    def test_solo_heredados_cierra_ok(self, tmp_path):
        s = self._red_session(
            tmp_path,
            {("run_tests", "/x"): ["users-table.test.ts"]},
            {"failing": ["users-table.test.ts"]},
            {"run_tests": "FAIL users-table.test.ts: assert falló"},
        )
        ok, why = s._verify_red_close()
        assert (ok, why) == (True, "solo-heredados")

    def test_solo_infra_cierra_ok_sin_baseline(self, tmp_path):
        s = self._red_session(
            tmp_path,
            {("run_tests", "/x"): ["users-table.test.ts"]},
            None,
            {"run_tests": "ECONNREFUSED 127.0.0.1:5432"},
        )
        ok, why = s._verify_red_close()
        assert (ok, why) == (True, "solo-infra")

    def test_con_nuevos_falla(self, tmp_path):
        s = self._red_session(
            tmp_path,
            {("run_tests", "/x"): ["users-table.test.ts", "nuevo.test.ts"]},
            {"failing": ["users-table.test.ts"]},
            {"run_tests": "FAIL nuevo.test.ts: assert falló"},
        )
        ok, why = s._verify_red_close()
        assert (ok, why) == (False, "con-nuevos")

    def test_sin_baseline_falla_seguro(self, tmp_path):
        s = self._red_session(
            tmp_path,
            {("run_tests", "/x"): ["nuevo.test.ts"]},
            None,
            {"run_tests": "FAIL nuevo.test.ts: assert falló"},
        )
        ok, why = s._verify_red_close()
        assert (ok, why) == (False, "sin-baseline")

    def test_mixto_infra_mas_real_falla(self, tmp_path):
        s = self._red_session(
            tmp_path,
            {("run_tests", "/x"): ["users-table.test.ts", "nuevo.test.ts"]},
            {"failing": []},
            {
                "run_tests": "FAIL nuevo.test.ts: assert falló",
                "run_build": "ECONNREFUSED 127.0.0.1:5432",
            },
        )
        # No todo es infra (hay un rojo real) → no exime...
        assert s._only_infra_reds() is False
        # ...y como el baseline no lo tiene como heredado → falla.
        ok, _ = s._verify_red_close()
        assert ok is False


class TestRedsUnrelated:
    """T04 real: turno toca ExportDailyCsvButton.tsx, fallan
    users-table + video-link-firmado (deuda ajena, baseline ausente).
    Antes → 'Turno fallido'; ahora → cierra ok informando."""

    def _unrelated_session(self, tmp_path, baseline):
        from orchestration.session import Session

        s = Session(llm=None, repo_path=str(tmp_path))
        s._rebuild_agent = lambda *a, **k: True
        s._explore_budget._failure_tails = {
            ("run_tests", "/x"): [
                "tests/integration/users-table.test.ts",
                "tests/integration/video-link-firmado.test.ts",
            ]
        }
        s._baseline = baseline
        s._turn_failures = {
            "run_tests": "FAIL users-table.test.ts: assert falló"}
        # Turno tocó solo el botón de export.
        s._turn_start_dirty = frozenset()
        s._changed_files = lambda: [
            "src/components/ops/metricas/ExportDailyCsvButton.tsx"]
        s._turn_own_files = lambda files, pre: list(files)
        s._post_write_gate = lambda: (True, "")
        return s

    def test_no_relacionados_con_baseline_vacio_cierra_ok(self, tmp_path):
        s = self._unrelated_session(tmp_path, {"failing": []})
        ok, why = s._verify_red_close()
        assert (ok, why) == (True, "no-relacionados")

    def test_no_relacionados_sin_baseline_cierra_ok(self, tmp_path):
        s = self._unrelated_session(tmp_path, None)
        # Sin baseline en memoria ni disco → _ensure_baseline None.
        s._baseline = None
        import cache
        orig = cache.load_test_baseline
        cache.load_test_baseline = lambda *a, **k: {}
        try:
            ok, why = s._verify_red_close()
        finally:
            cache.load_test_baseline = orig
        assert (ok, why) == (True, "sin-baseline-no-relacionados")

    def test_relacionado_sigue_fallando(self, tmp_path):
        from orchestration.session import Session

        s = Session(llm=None, repo_path=str(tmp_path))
        s._rebuild_agent = lambda *a, **k: True
        s._explore_budget._failure_tails = {
            ("run_tests", "/x"): ["tests/unit/bar.test.ts"]}
        s._baseline = {"failing": []}
        s._turn_failures = {"run_tests": "FAIL bar.test.ts: assert"}
        s._turn_start_dirty = frozenset()
        s._changed_files = lambda: ["src/bar.ts"]
        s._turn_own_files = lambda files, pre: list(files)
        s._post_write_gate = lambda: (True, "")
        ok, why = s._verify_red_close()
        assert (ok, why) == (False, "con-nuevos")

    def test_stems(self):
        from orchestration.session import (
            _reds_unrelated_to_own,
            _red_stem,
        )

        assert _red_stem("tests/integration/users-table.test.ts") == "users-table"
        assert _red_stem("src/bar.ts") == "bar"
        assert _red_stem("TestNombre") == ""
        assert _reds_unrelated_to_own(
            ["tests/integration/users-table.test.ts"],
            ["src/components/ops/metricas/ExportDailyCsvButton.tsx"],
        ) is True
        assert _reds_unrelated_to_own(
            ["tests/unit/bar.test.ts"], ["src/bar.ts"]) is False
        # Identificador Go sin path: no atribuible → relacionado (falla seguro).
        assert _reds_unrelated_to_own(
            ["TestNombre"], ["src/bar.ts"]) is False
