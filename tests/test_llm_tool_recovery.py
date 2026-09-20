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
