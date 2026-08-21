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
