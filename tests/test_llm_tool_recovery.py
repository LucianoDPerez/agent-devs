"""Tests for text/XML tool-call recovery and coercion."""

from llm_wrapper import parse_text_tool_calls


class TestParseTextToolCalls:
    def test_xml_function_with_params(self):
        text = """
<tool_call>
<function=list_files>
<parameter=path>
/Users/me/repo/apps/api/src
</parameter>
<parameter=recursive>
True
</parameter>
</function>
</tool_call>
"""
        calls = parse_text_tool_calls(text)
        assert len(calls) == 1
        assert calls[0]["name"] == "list_files"
        assert calls[0]["args"]["path"] == "/Users/me/repo/apps/api/src"
        assert calls[0]["args"]["recursive"] is True

    def test_inline_json(self):
        text = '🔧 read_file{"path":"/tmp/tasks.md"}'
        calls = parse_text_tool_calls(text)
        assert len(calls) == 1
        assert calls[0]["name"] == "read_file"
        assert calls[0]["args"]["path"] == "/tmp/tasks.md"

    def test_empty(self):
        assert parse_text_tool_calls("") == []
        assert parse_text_tool_calls("solo texto sin tools") == []

    def test_multiple_xml(self):
        text = """
<function=read_file>
<parameter=path>/a.md</parameter>
</function>
<function=write_file>
<parameter=path>/b.md</parameter>
<parameter=content>hola</parameter>
</function>
"""
        calls = parse_text_tool_calls(text)
        assert len(calls) == 2
        assert calls[0]["name"] == "read_file"
        assert calls[1]["name"] == "write_file"
        assert calls[1]["args"]["content"] == "hola"


class TestParseNewFormats:
    """Fase 2: formatos que emiten los SLM chicos en vez de function-calling."""

    def test_fenced_tool_block(self):
        text = 'Voy a leer:\n```tool\n{"name": "read_file", "args": {"path": "/a.md"}}\n```'
        calls = parse_text_tool_calls(text)
        assert len(calls) == 1
        assert calls[0]["name"] == "read_file"
        assert calls[0]["args"]["path"] == "/a.md"

    def test_tool_tag_json(self):
        text = "<tool_call>{\"name\": \"list_files\", \"args\": {\"path\": \"/r\"}}</tool_call>"
        calls = parse_text_tool_calls(text)
        assert len(calls) == 1
        assert calls[0]["name"] == "list_files"
        assert calls[0]["args"]["path"] == "/r"

    def test_tool_tag_pythonic(self):
        text = "<tool_call_start>[Read(path='/x.md')]<tool_call_end>"
        calls = parse_text_tool_calls(text)
        assert len(calls) == 1
        assert calls[0]["name"] == "Read"
        assert calls[0]["args"]["path"] == "/x.md"

    def test_bare_json_parameters(self):
        text = 'hago esto: {"name": "search_code", "parameters": {"pattern": "foo"}} listo'
        calls = parse_text_tool_calls(text)
        assert len(calls) == 1
        assert calls[0]["name"] == "search_code"
        assert calls[0]["args"]["pattern"] == "foo"

    def test_trailing_comma_and_single_quotes(self):
        text = "read_file{'path': '/a.md',}"
        calls = parse_text_tool_calls(text)
        assert len(calls) == 1
        assert calls[0]["name"] == "read_file"
        assert calls[0]["args"]["path"] == "/a.md"

    def test_bracket_tool(self):
        text = "[TOOL] edit_file(old_str='a', new_str='b')"
        calls = parse_text_tool_calls(text)
        assert len(calls) == 1
        assert calls[0]["name"] == "edit_file"
        assert calls[0]["args"]["old_str"] == "a"

    def test_existing_xml_still_first(self):
        text = "<function=read_file>\n<parameter=path>/a.md</parameter>\n</function>"
        calls = parse_text_tool_calls(text)
        assert len(calls) == 1
        assert calls[0]["name"] == "read_file"


class TestParseDSML:
    """DSML fullwidth de DeepSeek (E2E: write_file completo evaporado tras
    agotar exploración — el contenido venía bien formado)."""

    _DSML_WRITE = (
        '<｜DSML｜ invoke name="write_file"> '
        '<｜DSML｜ parameter name="path" string="true">/tmp/tasks.json</｜DSML｜ parameter> '
        '<｜DSML｜ parameter name="content" string="true">{"a": 1}</｜DSML｜ parameter>'
    )

    def test_dsml_fullwidth(self):
        calls = parse_text_tool_calls("Voy a escribir: " + self._DSML_WRITE)
        assert len(calls) == 1
        assert calls[0]["name"] == "write_file"
        assert calls[0]["args"]["path"] == "/tmp/tasks.json"
        assert calls[0]["args"]["content"] == '{"a": 1}'

    def test_dsml_ascii_variant(self):
        text = self._DSML_WRITE.replace("｜", "|")
        calls = parse_text_tool_calls(text)
        assert len(calls) == 1
        assert calls[0]["name"] == "write_file"
        assert calls[0]["args"]["path"] == "/tmp/tasks.json"

    def test_dsml_multiple_invokes(self):
        text = (
            '<｜DSML｜ invoke name="read_file">'
            '<｜DSML｜ parameter name="path">/a.md</｜DSML｜ parameter> '
            '<｜DSML｜ invoke name="read_file">'
            '<｜DSML｜ parameter name="path">/b.md</｜DSML｜ parameter>'
        )
        calls = parse_text_tool_calls(text)
        assert len(calls) == 2
        assert [c["args"]["path"] for c in calls] == ["/a.md", "/b.md"]

    def test_dsml_prosa_con_pipes_no_dispara(self):
        assert parse_text_tool_calls("uso pipes | así | en texto") == []


class TestClientImportRaceHeals:
    """Carrera de imports openai 3.x: el primer AsyncOpenAI() del proceso
    puede fallar con ImportError 'materialize_input_tools' y envenenar todos
    los turnos (E2E pool: 0/4 en 0.1s). _get_client reintenta una vez tras
    reimportar explícito."""

    def test_get_client_reintenta_tras_importerror(self, monkeypatch):
        import llm_wrapper
        from llm_wrapper import LocalLLM

        calls = []

        class FakeClient:
            pass

        def flaky(**kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise ImportError(
                    "cannot import name 'materialize_input_tools' from "
                    "'openai.lib._parsing'"
                )
            return FakeClient()

        monkeypatch.setattr(llm_wrapper, "AsyncOpenAI", flaky)
        llm = LocalLLM(base_url="http://127.0.0.1:8080/v1",
                       model_name="m", temperature=0.2, max_tokens=16,
                       api_key="x")
        assert isinstance(llm._get_client(), FakeClient)
        assert len(calls) == 2
        # Segunda vez: cacheado, sin reintentar.
        assert isinstance(llm._get_client(), FakeClient)
        assert len(calls) == 2

    def test_get_client_falla_de_verdad_si_persiste(self, monkeypatch):
        import llm_wrapper
        from llm_wrapper import LocalLLM

        def broken(**kwargs):
            raise ImportError("roto de verdad")

        monkeypatch.setattr(llm_wrapper, "AsyncOpenAI", broken)
        llm = LocalLLM(base_url="http://127.0.0.1:8080/v1",
                       model_name="m", temperature=0.2, max_tokens=16,
                       api_key="x")
        try:
            llm._get_client()
            assert False, "debió propagar"
        except ImportError:
            pass
