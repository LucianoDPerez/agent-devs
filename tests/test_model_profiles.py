"""Tests de per-model profiles (adaptación al SLM cargado)."""

from core.model_profiles import DEFAULT_PROFILE, match_profile


def test_spark():
    p = match_profile("spark2.5-4B")
    assert p["family"] == "spark"
    assert p["thinking_chars_exec"] < 12000  # presupuesto corto


def test_qwen():
    p = match_profile("Qwen3.6-35B-A3B-UD-Q4_K_M")
    assert p["family"] == "qwen"


def test_gemma():
    assert match_profile("gemma-4-12b-it-qat")["family"] == "gemma"


def test_desconocido_y_none_fallback():
    assert match_profile("mistral-raro-7b")["family"] == "default"
    assert match_profile(None)["family"] == "default"
    assert match_profile("") == DEFAULT_PROFILE


def test_copia_no_mutable():
    p = match_profile("spark-x")
    p["family"] = "roto"
    assert match_profile("spark-x")["family"] == "spark"


class TestSizeParse:
    def test_moe_toma_el_total(self):
        from core.model_profiles import _parse_size_b

        assert _parse_size_b("qwen3.6-35b-a3b") == 35

    def test_chico(self):
        from core.model_profiles import _parse_size_b

        assert _parse_size_b("agents-a1-4b") == 4

    def test_sin_b_no_matchea_version(self):
        from core.model_profiles import _parse_size_b

        assert _parse_size_b("muse-spark-1.3") is None
        assert _parse_size_b(None) is None
        assert _parse_size_b("mimodelo") is None


class TestBudgetTiers:
    def _llm(self, model_name):
        from types import SimpleNamespace

        return SimpleNamespace(base_url="http://localhost:9999/v1",
                               model_name=model_name)

    def _no_live(self, monkeypatch):
        import llm_wrapper

        monkeypatch.setattr(llm_wrapper, "detect_server_model",
                            lambda *a, **k: None)

    def test_chico_siempre_ajustado(self, monkeypatch):
        from core.model_profiles import analyze_budget_for, plan_budget_for

        self._no_live(monkeypatch)
        for ctx in (None, 20_000, 35_000, 65_000):
            assert analyze_budget_for(self._llm("agents-a1-4b"), ctx) == (4, 8)
            assert plan_budget_for(self._llm("agents-a1-4b"), ctx) == (4, 8)

    def test_grande_escala_con_contexto(self, monkeypatch):
        from core.model_profiles import analyze_budget_for, plan_budget_for

        self._no_live(monkeypatch)
        llm = self._llm("qwen3.6-35b-a3b")
        assert analyze_budget_for(llm, 65_000) == (10, 12)
        assert analyze_budget_for(llm, 35_000) == (7, 10)
        assert analyze_budget_for(llm, 20_000) == (4, 8)
        assert analyze_budget_for(llm, None) == (4, 8)
        assert plan_budget_for(llm, 35_000) == (7, 10)

    def test_sin_dato_fail_open(self, monkeypatch):
        from core.model_profiles import analyze_budget_for

        self._no_live(monkeypatch)
        assert analyze_budget_for(self._llm("mimodelo"), 65_000) == (4, 8)


def test_session_aplica_profile(tmp_path):
    from core.roles import Role
    from orchestration.session import Session

    s = Session(llm=None, repo_path=str(tmp_path))
    assert s._profile_thinking(Role.EXECUTE) == 12000  # fallback config
    s._model_profile = match_profile("spark2.5-4B")
    assert s._profile_thinking(Role.EXECUTE) == 8000
    assert s._profile_temps() == {Role.EXECUTE: 0.2}
