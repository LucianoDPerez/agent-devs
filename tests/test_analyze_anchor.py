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
