"""Validador determinístico de citas archivo:línea (E2E real T1b/9B).

El informe citaba `src/lib/patient.service.ts:0` y otros paths Next.js
inexistentes en un repo NestJS. La evidencia pasa a ser exigible: lo que no
verifica en disco dispara UN retry de corrección.
"""
from orchestration.evidence import find_unverifiable_cites


def _repo(tmp_path):
    src = tmp_path / "backend" / "src"
    src.mkdir(parents=True)
    f = src / "a.ts"
    f.write_text("l1\nl2\nl3\n", encoding="utf-8")
    return tmp_path


def test_cita_valida_pasa(tmp_path):
    repo = _repo(tmp_path)
    assert find_unverifiable_cites(
        "verificado en **[backend/src/a.ts:2]** ok", str(repo)) == []


def test_archivo_inexistente_no_pasa(tmp_path):
    repo = _repo(tmp_path)
    bad = find_unverifiable_cites(
        "falla en **[src/lib/patient.service.ts:0]**", str(repo))
    assert bad == ["src/lib/patient.service.ts:0"]


def test_linea_cero_y_fuera_de_rango_no_pasan(tmp_path):
    repo = _repo(tmp_path)
    bad = find_unverifiable_cites(
        "a **[backend/src/a.ts:0]** y b **[backend/src/a.ts:99]**", str(repo))
    assert bad == ["backend/src/a.ts:0", "backend/src/a.ts:99"]


def test_sin_citas_pasa_negativa_honesta(tmp_path):
    repo = _repo(tmp_path)
    assert find_unverifiable_cites(
        "No se pudo verificar: falta el path del módulo.", str(repo)) == []
    assert find_unverifiable_cites("", str(repo)) == []
    assert find_unverifiable_cites(None, str(repo)) == []


def test_no_falsos_positivos_comunes(tmp_path):
    repo = _repo(tmp_path)
    # timestamps, índices de array, URLs y fences no son citas
    text = ("a las [12:30] el arr[0:2] falló, ver https://x.io/a.ts "
            "y el bloque ```md ... ```")
    assert find_unverifiable_cites(text, str(repo)) == []


def test_backticks_y_rangos(tmp_path):
    # E2E real T2/9B: `CreatePaciente.ts:15` en archivo de 14 líneas.
    repo = _repo(tmp_path)
    (repo / "backend" / "src" / "x.ts").write_text("a\n", encoding="utf-8")
    bad = find_unverifiable_cites(
        "ver `backend/src/a.ts:99` y `backend/src/x.ts:5-27`", str(repo))
    assert bad == ["backend/src/a.ts:99", "backend/src/x.ts:5"]
    ok = find_unverifiable_cites(
        "ver `backend/src/a.ts:2` y `backend/src/x.ts:1`", str(repo))
    assert ok == []


def test_backticks_sin_extension_no_matchean(tmp_path):
    repo = _repo(tmp_path)
    assert find_unverifiable_cites("el `arr[0:2]` falló a las `12:30`",
                                   str(repo)) == []


def test_paths_absolutos_y_relativos(tmp_path):
    repo = _repo(tmp_path)
    abs_cite = f"**[{repo / 'backend' / 'src' / 'a.ts'}:3]**"
    assert find_unverifiable_cites(f"ok {abs_cite}", str(repo)) == []
    bad = find_unverifiable_cites("mal **[otro/b.ts:1]**", str(repo))
    assert bad == ["otro/b.ts:1"]
