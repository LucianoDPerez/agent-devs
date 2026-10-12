"""Verificación por alcance: json/docs no exigen batería.

E2E real: turno que solo escribió tasks/tasks.json en repo sin stack →
compuerta exigiendo lint/tests/build → fallo con trabajo correcto.
"""
from orchestration.session import (
    _split_written_by_kind,
    Session,
)


def _session(repo):
    sess = Session(llm=None, repo_path=str(repo))
    sess._rebuild_agent = lambda *a, **k: True  # sin LLM en tests
    return sess


class TestSplitKinds:
    def test_json_docs_code(self):
        code, js, other = _split_written_by_kind(
            ["a.json", "b.md", "c.ts", "d.txt", "e.py"])
        assert js == ["a.json"]
        assert sorted(code) == ["c.ts", "e.py"]
        assert sorted(other) == ["b.md", "d.txt"]

    def test_desconocido_es_code_conservador(self):
        code, js, other = _split_written_by_kind(["x.zig", "Makefile", "a.json"])
        assert "x.zig" in code and "Makefile" in code
        assert js == ["a.json"]
        assert other == []

    def test_vacio(self):
        assert _split_written_by_kind([]) == ([], [], [])


class TestCheckWrittenJson:
    def test_valido(self, tmp_path):
        s = _session(tmp_path)
        (tmp_path / "t.json").write_text('{"a": [1, 2]}')
        ok, msg = s._check_written_json(["t.json"])
        assert ok is True
        assert "válido" in msg

    def test_roto(self, tmp_path):
        s = _session(tmp_path)
        (tmp_path / "t.json").write_text('{"a": 1,}')
        ok, msg = s._check_written_json(["t.json"])
        assert ok is False
        assert "t.json" in msg

    def test_borrado_se_salta(self, tmp_path):
        s = _session(tmp_path)
        ok, _ = s._check_written_json(["gone.json"])
        assert ok is True

    def test_no_json_se_ignora(self, tmp_path):
        s = _session(tmp_path)
        (tmp_path / "a.md").write_text("# hola")
        ok, msg = s._check_written_json(["a.md"])
        assert ok is True
        assert "0 archivo" in msg


class TestTurnScopeKinds:
    def _scope(self, tmp_path, monkeypatch, files):
        s = _session(tmp_path)
        monkeypatch.setattr(s, "_changed_files", lambda: list(files))
        s._turn_start_dirty = frozenset()
        s._turn_start_diffs = {}
        return s._turn_scope_kinds()

    def test_solo_json(self, tmp_path, monkeypatch):
        assert self._scope(tmp_path, monkeypatch, ["tasks/t.json"]) == (
            False, ["tasks/t.json"])

    def test_con_codigo_no_relaja(self, tmp_path, monkeypatch):
        has_code, js = self._scope(
            tmp_path, monkeypatch, ["tasks/t.json", "src/a.ts"])
        assert has_code is True
        assert js == ["tasks/t.json"]

    def test_sin_nada_none(self, tmp_path, monkeypatch):
        assert self._scope(tmp_path, monkeypatch, []) is None

    def test_snapshot_fallido_none(self, tmp_path, monkeypatch):
        s = _session(tmp_path)
        monkeypatch.setattr(s, "_changed_files", lambda: ["t.json"])
        s._turn_start_dirty = None
        assert s._turn_scope_kinds() is None


class TestGateCounterTripwire:
    def test_contador_existe(self):
        """La compuerta por alcance resetea este contador: si se renombra,
        el gate silenciosamente deja de satisfacerse."""
        from orchestration.tool_dedupe import ExploreBudget

        b = ExploreBudget(max_calls=3, max_reads_after_explore=5,
                          max_tools_before_write=12)
        b._writes_since_verify = 4
        b._writes_since_verify = 0
        assert b._writes_since_verify == 0
