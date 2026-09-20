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


def test_session_aplica_profile(tmp_path):
    from core.roles import Role
    from orchestration.session import Session

    s = Session(llm=None, repo_path=str(tmp_path))
    assert s._profile_thinking(Role.EXECUTE) == 12000  # fallback config
    s._model_profile = match_profile("spark2.5-4B")
    assert s._profile_thinking(Role.EXECUTE) == 8000
    assert s._profile_temps() == {Role.EXECUTE: 0.2}
