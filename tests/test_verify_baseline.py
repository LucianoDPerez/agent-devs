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
