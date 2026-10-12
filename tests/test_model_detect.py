"""pretty_model_id + fallback de detect_server_model sin server."""
from llm_wrapper import pretty_model_id


def test_alias_limpio_intacto():
    assert pretty_model_id("qwen3.6-35b-a3b") == "qwen3.6-35b-a3b"


def test_path_con_gguf():
    assert pretty_model_id("/models/qwen3.6-35b-a3b-Q4_K_M.gguf") == "qwen3.6-35b-a3b-Q4_K_M"


def test_vacio():
    assert pretty_model_id("") == ""
    assert pretty_model_id(None) == ""


def test_detect_sin_server_devuelve_none(monkeypatch):
    import urllib.request

    from llm_wrapper import detect_server_model

    def boom(*a, **k):
        raise ConnectionError("no server")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert detect_server_model("http://localhost:9999/v1") is None


def test_switch_model_muestra_id_real_del_server(tmp_path, monkeypatch):
    """E2E: /model local decía el default del config (qwen3.6-35b-a3b) con
    otro modelo cargado. Debe mostrar el id de /v1/models."""
    import llm_wrapper
    from orchestration.session import Session

    monkeypatch.setattr(llm_wrapper, "detect_server_model", lambda *a, **k: "spark2.5-4B")
    import llm_config

    monkeypatch.setattr(llm_config, "save_config", lambda cfg: "mocked")
    s = Session(llm=None, repo_path=str(tmp_path))
    ok, msg = s.switch_model(provider="local")
    assert ok is True
    assert "spark2.5-4B" in msg
    assert "qwen3.6-35b-a3b" not in msg
    assert s.get_status()["model"] == "spark2.5-4B"


def test_switch_model_fallback_al_config_sin_server(tmp_path, monkeypatch):
    import llm_wrapper
    from orchestration.session import Session

    monkeypatch.setattr(llm_wrapper, "detect_server_model", lambda *a, **k: None)
    import llm_config

    monkeypatch.setattr(llm_config, "save_config", lambda cfg: "mocked")
    s = Session(llm=None, repo_path=str(tmp_path))
    ok, msg = s.switch_model(provider="local")
    assert ok is True
    assert "qwen3.6-35b-a3b" in msg
    assert s.get_status()["model"] == "qwen3.6-35b-a3b"
