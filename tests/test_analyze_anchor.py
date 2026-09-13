"""Ancla del retry ANALYZE: todo lo leído + hechos + fallo honesto.

E2E real (35B): el PASS1 leyó 2 archivos pero el ancla inyectaba solo el
primero (`pass1[:1]`) con texto hardcodeado del botón Guardar. El modelo
respondió "sin ejecución de tools" y recomendó archivos de src/modules/*
nunca leídos citando "líneas inferidas".
"""
from orchestration.session import Session


def _session(repo):
    sess = Session(llm=None, repo_path=str(repo))
    sess._rebuild_agent = lambda role: True  # sin LLM en tests
    return sess


def test_anchor_incluye_todos_los_leidos_no_solo_el_primero(tmp_path):
    sess = _session(tmp_path)
    sess._read_cache = {
        "src/a.ts": "contenido A " * 100,
        "src/b.ts": "contenido B " * 100,
        "src/c.ts": "contenido C " * 100,
    }
    anchor = sess._retry_analyze_anchor()
    assert "CONTENIDO REAL DE src/a.ts" in anchor
    assert "CONTENIDO REAL DE src/b.ts" in anchor
    assert "CONTENIDO REAL DE src/c.ts" in anchor
    assert "contenido B" in anchor


def test_anchor_sin_texto_hardcodeado_del_boton(tmp_path):
    sess = _session(tmp_path)
    sess._read_cache = {"src/a.ts": "x = 1\n" * 100}
    anchor = sess._retry_analyze_anchor()
    assert "botón Guardar" not in anchor
    assert "submit" not in anchor.lower()


def test_anchor_inyecta_hechos_del_turno(tmp_path):
    sess = _session(tmp_path)
    sess._read_cache = {"src/a.ts": "x = 1\n" * 100}
    sess._called_tools = {"read_file", "list_files"}
    anchor = sess._retry_analyze_anchor()
    assert "HECHOS DE TU INTENTO ANTERIOR" in anchor
    assert "read_file" in anchor and "list_files" in anchor
    assert "src/a.ts" in anchor
    assert "NO verificado" in anchor


def test_anchor_exige_fallo_honesto(tmp_path):
    sess = _session(tmp_path)
    sess._read_cache = {"src/a.ts": "x = 1\n" * 100}
    anchor = sess._retry_analyze_anchor()
    assert "NO completes con el análisis cacheado" in anchor
    assert "QUÉ te falta" in anchor


def test_anchor_excedente_va_a_indice_sin_inventar(tmp_path):
    sess = _session(tmp_path)
    sess._read_cache = {f"src/f{i}.ts": f"contenido {i} " * 500 for i in range(6)}
    anchor = sess._retry_analyze_anchor(max_blocks=2, max_chars=4000)
    assert "src/f0.ts" in anchor and "src/f1.ts" in anchor
    # El resto figura como índice (paths conocidos, sin líneas citables)
    assert "SIN CONTENIDO POR PRESUPUESTO" in anchor
    assert "src/f5.ts" in anchor


def test_anchor_cache_vacio(tmp_path):
    sess = _session(tmp_path)
    assert sess._retry_analyze_anchor() == ""


def test_readonly_retry_conserva_historial_y_solo_read_file(tmp_path, monkeypatch):
    """E2E real T1: el retry de 0 tools no podía convertir listados en
    lecturas (3 intentos, 0 read_file). El retry de solo-lectura conserva el
    historial (listados visibles) y reconstruye el agente solo con read_file.
    """
    from langchain_core.messages import HumanMessage

    from core.roles import Role

    sess = _session(tmp_path)
    sess._read_cache = {"[trace: Foo]": "class Foo {} " * 100}
    sess._messages = [HumanMessage("listado:\n- a.ts\n- b.ts")]
    seen = {}

    def _fake_rebuild(role, no_explore=False, tools_override=None):
        seen["role"] = role
        seen["tools"] = [getattr(t, "name", t) for t in (tools_override or [])]
        return True

    sess._rebuild_agent = _fake_rebuild
    sess._retry_analyze_no_explore(Role.ANALYZE, "budget agotado")
    # Historial conservado (sin trim): el listado sigue visible para elegir
    assert any("a.ts" in str(m.content) for m in sess._messages)
    # Agente reconstruido SOLO con read_file
    assert seen["tools"] == ["read_file"]
    # Búsqueda bloqueada, lecturas acotadas
    assert sess._analyze_budget.max_calls == 0
    # El mensaje exige leer + citar (no "respondé con lo ya leído")
    body = str(sess._messages[-1].content)
    assert "SOLO tenés read_file" in body
    assert "archivo:línea" in body
    assert sess._readonly_retry is True


def test_readonly_retry_segunda_vuelta_responde_sin_tools(tmp_path):
    """Si la etapa de lectura también se agota, 2ª vuelta SIN tools + ancla
    (evita rumiar hasta agotar output — E2E real T1: respuesta vacía)."""
    from langchain_core.messages import HumanMessage

    from core.roles import Role

    sess = _session(tmp_path)
    sess._read_cache = {"src/a.ts": "contenido A " * 100}
    sess._messages = [HumanMessage("pregunta")]
    seen = {}

    def _fake_rebuild(role, no_explore=False, tools_override=None):
        seen["no_explore"] = no_explore
        seen["tools"] = tools_override
        return True

    sess._rebuild_agent = _fake_rebuild
    sess._retry_analyze_no_explore(Role.ANALYZE, "1")
    assert seen["tools"] is not None  # 1ª: solo-lectura
    sess._retry_analyze_no_explore(Role.ANALYZE, "2")
    assert seen["no_explore"] is True  # 2ª: sin tools
    assert seen["tools"] is None
    body = str(sess._messages[-1].content)
    assert "RESPONDÉ AHORA" in body
    assert "CONTENIDO REAL DE src/a.ts" in body  # ancla regenerada


def test_segunda_vuelta_contexto_minimo_y_pregunta_original(tmp_path):
    """E2E real T1: con todo el historial encima el 4B declaró 'sin acceso
    al código' teniendo snippets. La 2ª vuelta deja solo summaries +
    pregunta original + ancla (sin anidar reintentos)."""
    from langchain_core.messages import HumanMessage, SystemMessage

    from core.roles import Role

    sess = _session(tmp_path)
    sess._read_cache = {"src/a.ts": "contenido A " * 100}
    sess._messages = [
        SystemMessage("resumen previo"),
        HumanMessage("pregunta original del usuario"),
        HumanMessage("Reanalizá la pregunta: ... (retry 1ª)"),
        HumanMessage("basura larga " * 500),
    ]
    sess._turn_question = "pregunta original del usuario"
    sess._rebuild_agent = lambda *a, **k: True
    sess._retry_analyze_no_explore(Role.ANALYZE, "1")  # 1ª: solo-lectura
    sess._retry_analyze_no_explore(Role.ANALYZE, "2")  # 2ª: mínima
    assert len(sess._messages) == 2
    assert isinstance(sess._messages[0], SystemMessage)
    body = str(sess._messages[1].content)
    assert "Pregunta original: \"pregunta original del usuario\"" in body
    assert "Reanalizá: Reanalizá" not in body
    assert "PROHIBIDO decir que no tenés acceso" in body


def test_readonly_retry_preserva_traces_sin_system_trace(tmp_path, monkeypatch):
    """Con traces del PASS1 no se dispara _system_trace_for (evita duplicar
    contenido que distrae al 4B)."""
    from core.roles import Role

    sess = _session(tmp_path)
    sess._read_cache = {"[trace: Foo]": "class Foo {} " * 100}
    sess._rebuild_agent = lambda *a, **k: True
    called = []
    monkeypatch.setattr(
        sess, "_system_trace_for", lambda *a, **k: called.append(1) or "")
    sess._retry_analyze_no_explore(Role.ANALYZE, "x")
    assert called == []


def test_anchor_incluye_snippets_en_hechos(tmp_path):
    # E2E real T1: el modelo leyó source vía cm__get_code_snippet pero los
    # hechos decían "ninguno" (se filtraban las claves [..]) y el modelo
    # descartó su propia lectura. Ahora toda clave del caché cuenta.
    sess = _session(tmp_path)
    sess._read_cache = {"[snippet:backend.Foo]": "class Foo {} " * 100}
    sess._called_tools = {"cm__get_code_snippet", "cm__search_graph"}
    anchor = sess._retry_analyze_anchor()
    assert "SOURCE DEL GRAFO ([snippet:backend.Foo])" in anchor
    assert "[snippet:backend.Foo]" in anchor.split("Leíste estos archivos:")[1]
    assert "ninguno" not in anchor


def test_snippet_crudo_se_cachea_para_el_retry(tmp_path):
    from langchain_core.tools import tool as _tool

    from orchestration.tool_dedupe import ExploreBudget, wrap_tools_with_dedupe

    @_tool
    def cm__get_code_snippet(qualified_name: str) -> str:
        """fake"""
        return "class Foo {}"

    cache: dict = {}
    budget = ExploreBudget(max_calls=5, max_reads_after_explore=8,
                           max_tools_before_write=0, write_pressure=False)
    from orchestration.tool_dedupe import ToolCallDedupe

    wrapped = wrap_tools_with_dedupe(
        [cm__get_code_snippet], ToolCallDedupe(), budget, cache)
    out = wrapped[0].invoke({"qualified_name": "backend.Foo"})
    assert "class Foo" in out
    assert "[snippet:backend.Foo]" in cache
