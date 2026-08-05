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
