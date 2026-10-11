"""Unit tests del transporte dual chat/responses (sin red)."""
from types import SimpleNamespace

import pytest

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from llm_config import (
    is_free_model,
    needs_messages_api,
    resolve_reasoning,
    transport_for,
)
from llm_wrapper import LocalLLM, is_protocol_error


@tool
def fake_time() -> str:
    """Devuelve la hora actual."""
    return "12:00"


class TestConfigLocation:
    def test_legacy_fallback(self, tmp_path, monkeypatch):
        import llm_config

        new = tmp_path / "new.json"
        old = tmp_path / "old.json"
        monkeypatch.setattr(llm_config, "CONFIG_FILE", new)
        monkeypatch.setattr(llm_config, "LEGACY_CONFIG_FILE", old)
        assert llm_config.active_config_file() == new  # ninguno existe → nueva
        old.write_text('{"provider": "opencode"}')
        assert llm_config.active_config_file() == old  # legacy manda si es la que existe
        new.write_text('{"provider": "local"}')
        assert llm_config.active_config_file() == new  # nueva gana si ambas existen

    def test_save_goes_to_new(self, tmp_path, monkeypatch):
        import llm_config

        new = tmp_path / "sub" / "config.json"
        monkeypatch.setattr(llm_config, "CONFIG_FILE", new)
        monkeypatch.setattr(llm_config, "LEGACY_CONFIG_FILE", tmp_path / "old.json")
        saved = llm_config.save_config({"provider": "local"})
        assert saved == new
        assert new.exists()


class TestTransportSeed:
    def test_responses_prefixes(self):
        assert transport_for("muse-spark-1.3") == "responses"
        assert transport_for("gpt-5.4-nano") == "responses"
        assert transport_for("grok-4.7") == "responses"

    def test_chat_default(self):
        assert transport_for("deepseek-v4.1-flash") == "chat"
        assert transport_for("qwen3.8-max") == "chat"
        assert transport_for("agents-a1-4b") == "chat"

    def test_messages_api(self):
        assert needs_messages_api("claude-sonnet-5") is True
        assert needs_messages_api("muse-spark-1.3") is False
        assert needs_messages_api("deepseek-v4.1-flash") is False

    def test_cache_overrides_seed(self, tmp_path, monkeypatch):
        import llm_config

        cache = tmp_path / "transport.json"
        monkeypatch.setattr(llm_config, "TRANSPORT_CACHE_FILE", cache)
        assert transport_for("some-model") == "chat"
        llm_config.remember_transport("some-model", "responses")
        assert transport_for("some-model") == "responses"


class TestReasoning:
    def test_default_medium(self, monkeypatch):
        monkeypatch.setattr("llm_config.load_config", lambda: {})
        assert resolve_reasoning() == "medium"

    def test_invalid_falls_back(self, monkeypatch):
        monkeypatch.setattr("llm_config.load_config", lambda: {"opencode_reasoning": "ultra"})
        assert resolve_reasoning() == "medium"

    def test_override_wins(self, monkeypatch):
        monkeypatch.setattr("llm_config.load_config", lambda: {"opencode_reasoning": "low"})
        assert resolve_reasoning("high") == "high"
        assert resolve_reasoning() == "low"


class TestProtocolError:
    def test_code_attr(self):
        assert is_protocol_error(SimpleNamespace(code="ModelProtocolUnsupported")) is True

    def test_message_variants(self):
        assert is_protocol_error(RuntimeError("Model does not support this protocol.")) is True
        assert is_protocol_error(RuntimeError("connection refused")) is False

    def test_chained(self):
        class ProtoErr(Exception):
            code = "ModelProtocolUnsupported"

        outer = RuntimeError("boom")
        outer.__cause__ = ProtoErr("inner")
        assert is_protocol_error(outer) is True


class TestResponsesConversion:
    def test_input_messages(self):
        llm = LocalLLM(base_url="https://opencode.ai/zen/v1", model_name="muse-spark-1.3",
                       api_key="x", transport="responses")
        items = llm._convert_to_responses_input([
            SystemMessage("sos un dev"),
            HumanMessage("hola"),
            AIMessage(content="llamo", tool_calls=[{"id": "c1", "name": "fake_time", "args": {}, "type": "tool_call"}]),
            ToolMessage(content="12:00", tool_call_id="c1"),
        ])
        kinds = [(i.get("role") or i.get("type")) for i in items]
        assert kinds == ["system", "user", "assistant", "function_call", "function_call_output"]
        assert items[3]["call_id"] == "c1"
        assert items[4]["call_id"] == "c1"

    def test_tools_shape(self):
        llm = LocalLLM(base_url="https://opencode.ai/zen/v1", model_name="muse-spark-1.3",
                       api_key="x", transport="responses").bind_tools([fake_time])
        tools = llm._responses_tools()
        assert tools[0]["type"] == "function"
        assert tools[0]["name"] == "fake_time"
        assert "function" not in tools[0]  # plano, no anidado estilo chat
        assert tools[0]["parameters"]["type"] == "object"

    def test_output_parsing(self):
        out = [
            SimpleNamespace(type="message", content=[
                SimpleNamespace(type="output_text", text="listo"),
            ]),
            SimpleNamespace(type="function_call", call_id="c9", name="fake_time", arguments="{}"),
            SimpleNamespace(type="reasoning", id="r1"),
        ]
        assert LocalLLM._responses_text(out) == "listo"
        calls = LocalLLM._responses_tool_calls(out)
        assert calls == [{"id": "c9", "name": "fake_time", "args": {}, "type": "tool_call"}]

    def test_bad_args_json(self):
        out = [SimpleNamespace(type="function_call", call_id="c1", name="t", arguments="{roto")]
        assert LocalLLM._responses_tool_calls(out)[0]["args"] == {}


class TestLlamaParamsGating:
    """Los params llama-only no viajan a la nube (daban 400)."""

    def test_local_keeps_extras(self):
        llm = LocalLLM(base_url="http://localhost:8080/v1", model_name="m",
                       max_reasoning_tokens=512,
                       chat_template_kwargs={"enable_thinking": False})
        params = llm._extract_kwargs()
        assert params["extra_body"]["max_reasoning_tokens"] == 512
        assert params["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}

    def test_cloud_strips_extras(self):
        llm = LocalLLM(base_url="https://opencode.ai/zen/v1", model_name="muse-spark-1.3",
                       api_key="x", max_reasoning_tokens=1024,
                       chat_template_kwargs={"enable_thinking": False})
        assert "extra_body" not in llm._extract_kwargs()

    def test_responses_kwargs(self):
        llm = LocalLLM(base_url="https://opencode.ai/zen/v1", model_name="muse-spark-1.3",
                       api_key="x", transport="responses", reasoning_effort="low",
                       force_tool_calls=True).bind_tools([fake_time])
        params = llm._responses_kwargs()
        assert params["reasoning"] == {"effort": "low"}
        assert params["tool_choice"] == "required"
        assert params["tools"][0]["name"] == "fake_time"

    def test_free_still_detected(self):
        assert is_free_model({"id": "muse-spark-1.3-contributor-free"}) is True
        assert is_free_model({"id": "muse-spark-1.3"}) is False


class TestKnownTextToolCalls:
    """El recovery filtra calls fantasma de la prosa (if/casero/matches)."""

    _PROSE = (
        "Validador manual con cadena if (rule.type === \"string\") y luego minLength. "
        "TODO en cm__search_code: 0 matches (total_results=0). "
        "FIXME en cm__search_code: 0 matches (total_results=0)."
    )

    def test_prose_yields_nothing(self):
        llm = LocalLLM(base_url="https://opencode.ai/zen/v1", model_name="m",
                       api_key="x").bind_tools([fake_time])
        assert llm._known_text_tool_calls(self._PROSE) == []

    def test_real_call_passes(self):
        llm = LocalLLM(base_url="https://opencode.ai/zen/v1", model_name="m",
                       api_key="x").bind_tools([fake_time])
        calls = llm._known_text_tool_calls('Uso la tool fake_time{"x": 2} ahora.')
        assert [(c["name"], c["args"]) for c in calls] == [("fake_time", {"x": 2})]

    def test_no_tools_bound_recovers_nothing(self):
        llm = LocalLLM(base_url="http://localhost:8080/v1", model_name="m")
        assert llm._known_text_tool_calls('read_file{"path": "/x"}') == []

    def test_raw_parser_still_finds_ghosts(self):
        # El parser crudo SIGUE detectándolos (lo usa el 4B); el filtro es
        # quien los descarta. Si esto falla, el test anterior no prueba nada.
        from llm_wrapper import parse_text_tool_calls

        names = {c["name"] for c in parse_text_tool_calls(self._PROSE)}
        assert "if" in names and "matches" in names
