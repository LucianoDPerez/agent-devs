"""Tests del cierre determinístico de turnos EXECUTE.

El 4B a veces no sabe cerrar el turno tras escribir + verificar: sigue
llamando tools (git_status/lint) hasta agotar el budget, y el retry
write-only re-escribía el mismo cambio pidiendo aprobación de nuevo.
Guarda: si el intento anterior YA escribió, se cierra el turno con un
resumen determinístico (git, no alucinado por el modelo).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestration.session import Session


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
    )


def _init_repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "README.md").write_text("# repo\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def _make_session(tmp_path: Path) -> Session:
    from config import LLM_BASE_URL, LLM_MODEL_NAME
    from llm_wrapper import LocalLLM

    llm = LocalLLM(
        base_url=LLM_BASE_URL, model_name=LLM_MODEL_NAME,
        temperature=0.2, max_tokens=1024, api_key="not-needed",
    )
    s = Session(llm, str(tmp_path), cached_analysis="Repo de prueba con README.")
    s.start()
    return s


def test_deterministic_close_con_cambios_y_verificacion(tmp_path):
    """Con cambios reales en disco + verify corrido: resumen con archivos y ✅."""
    repo = _init_repo(tmp_path)
    (repo / "README.md").write_text("# repo\n\nHOLA SOY AGENT DEVS\n")
    s = _make_session(repo)
    s._called_tools = {"edit_file", "run_lint", "run_tests"}
    close = s._deterministic_close()
    assert "Tarea realizada" in close
    assert "README.md" in close
    assert "Verificación" in close and "✅" in close


def test_deterministic_close_sin_cambios(tmp_path):
    """Sin cambios en disco: no se anuncia tarea realizada (no alucinar)."""
    repo = _init_repo(tmp_path)
    s = _make_session(repo)
    s._called_tools = set()
    close = s._deterministic_close()
    assert "Tarea realizada" not in close
    assert "sin cambios" in close


def test_deterministic_close_no_lee_al_modelo(tmp_path):
    """El resumen usa git, no _last_response del modelo (que puede alucinar)."""
    repo = _init_repo(tmp_path)
    (repo / "README.md").write_text("# repo\n\nEDITADO\n")
    s = _make_session(repo)
    # El modelo alucina que tocó un archivo que NO modificó:
    s._last_response = "Listo, creé helpers.py ✅"
    s._called_tools = {"write_file"}
    close = s._deterministic_close()
    assert "helpers.py" not in close  # git no lo ve → no se anuncia
    assert "README.md" in close       # git sí lo ve


def test_cambio_ya_escrito_no_dispara_retry_write_only(tmp_path):
    """Guarda: si _called_tools tiene write/edit/delete, no re-escribir."""
    repo = _init_repo(tmp_path)
    s = _make_session(repo)
    s._called_tools = {"edit_file", "run_lint"}
    # La guarda usa WRITE_TOOL_NAMES ∩ _called_tools:
    from orchestration.session import WRITE_TOOL_NAMES
    assert bool(s._called_tools & WRITE_TOOL_NAMES)


def test_close_marca_fallo_si_verify_fallo(tmp_path):
    """E2E real T3/12B: run_build en raíz rota contaba como 'build ✅'.
    Con resultado [FAILED] registrado, el cierre debe decir FALLÓ."""
    repo = _init_repo(tmp_path)
    (repo / "README.md").write_text("# repo\n\nEDITADO\n")
    s = _make_session(repo)
    s._called_tools = {"edit_file", "run_build"}
    s._verify_results = {"run_build": False}
    close = s._deterministic_close()
    assert "FALLÓ" in close
    assert "no commitear sin revisar" in close


def test_close_ok_solo_si_todo_paso(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "README.md").write_text("# repo\n\nEDITADO\n")
    s = _make_session(repo)
    s._called_tools = {"edit_file", "run_lint", "run_build"}
    s._verify_results = {"run_lint": True, "run_build": False}
    assert "FALLÓ" in s._deterministic_close()
    s._verify_results = {"run_lint": True, "run_build": True}
    close = s._deterministic_close()
    assert "Verificación: lint/tests/build ✅" in close


def test_close_sin_cambios_con_fallo_avisa(tmp_path):
    repo = _init_repo(tmp_path)
    s = _make_session(repo)
    s._called_tools = {"run_tests"}
    s._verify_results = {"run_tests": False}
    assert "FALLÓ" in s._deterministic_close()


def test_failed_close_con_commit_reciente_informa_completado(tmp_path):
    """E2E real: turno que commiteó y luego loo interesting narrando cerraba
    como 'fallido' aunque todo estaba hecho. Con árbol limpio + commit
    reciente, informa completado."""
    repo = _init_repo(tmp_path)
    (repo / "x.txt").write_text("v2", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "feat: x")
    s = _make_session(repo)
    msg = s._failed_turn_close()
    assert "ya commiteados" in msg
    assert "fallido" not in msg.lower()


def test_failed_close_con_cambios_mantiene_cautela(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "README.md").write_text("# repo\n\nPENDIENTE\n")
    s = _make_session(repo)
    msg = s._failed_turn_close()
    assert "fallido" in msg.lower()


def test_failed_close_codigo_roto_reporta_archivo(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "roto.py").write_text("def f(:\n", encoding="utf-8")
    s = _make_session(repo)
    msg = s._failed_turn_close()
    assert "ROTO" in msg
    assert "roto.py" in msg


def test_changed_files_filtra_clutter_de_otros_agentes(tmp_path):
    """E2E real 35B: el cierre anunció '7 archivo(s)' (untracked de cursor,
    devbase, evidence, e2e-worker) cuando el turno tocó 1 archivo."""
    from orchestration.session import Session

    repo = _init_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "a.ts").write_text("x", encoding="utf-8")
    for clutter in (".agents/n.md", ".cursor/rules", ".devbase/x",
                    ".agent/evidence/img.png", "scripts/e2e-worker.sh"):
        p = repo / clutter
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("clutter", encoding="utf-8")
    s = Session(llm=None, repo_path=str(repo))
    assert s._changed_files() == ["src/a.ts"]


def _old_commit(repo: Path) -> None:
    """Commit con fecha vieja (fuera de la ventana de 5 min de 'reciente')."""
    import os

    env = dict(os.environ, GIT_COMMITTER_DATE="2000-01-01T00:00:00")
    (repo / "old.txt").write_text("v1", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True,
                   capture_output=True, text=True)
    subprocess.run(["git", "commit", "-qm", "old", "--date=2000-01-01T00:00:00"],
                   cwd=str(repo), check=True, capture_output=True, text=True,
                   env=env)


def test_nothing_pending_commit_reciente_arbol_limpio(tmp_path):
    """E2E real: turno 'hacer commit' tras e7c8f3a — no hay nada que escribir,
    el retry no-write debe cerrarse, no inventar edits."""
    from orchestration.session import Session

    repo = _init_repo(tmp_path)  # commit init = reciente (ahora mismo)
    s = Session(llm=None, repo_path=str(repo))
    s._called_tools = {"changed_files"}
    assert s._nothing_pending_to_write() is True


def test_nothing_pending_verificado_sin_commit(tmp_path):
    """Tarea ya hecha + batería verde + árbol limpio: cerrar, no reescribir."""
    from orchestration.session import Session

    repo = _init_repo(tmp_path)
    _old_commit(repo)  # commit viejo → NO reciente
    assert Session(llm=None, repo_path=str(repo))._repo_has_recent_commit() is False
    s = Session(llm=None, repo_path=str(repo))
    s._called_tools = {"read_file", "run_lint", "run_tests", "run_build"}
    assert s._nothing_pending_to_write() is True


def test_nothing_pending_falso_con_cambios(tmp_path):
    """Con cambios sin commitear SÍ hay trabajo pendiente (aunque haya commit)."""
    from orchestration.session import Session

    repo = _init_repo(tmp_path)
    (repo / "README.md").write_text("# repo\n\nPENDIENTE\n")
    s = Session(llm=None, repo_path=str(repo))
    s._called_tools = {"changed_files"}
    assert s._nothing_pending_to_write() is False


def test_nothing_pending_falso_escape_vago(tmp_path):
    """Sin commit reciente, sin verify y sin cambios: escape vago, debe reintentar."""
    from orchestration.session import Session

    repo = _init_repo(tmp_path)
    _old_commit(repo)
    s = Session(llm=None, repo_path=str(repo))
    s._called_tools = {"read_file"}
    assert s._nothing_pending_to_write() is False


def test_readonly_evidence_turn_closes_without_retry(tmp_path):
    """E2E real T003: 'implementar' → 2 read_file → veredicto 'ya está' con
    archivo:línea. El no-write NO debe reintentar (5 no-ops después)."""
    from orchestration.session import Session

    repo = _init_repo(tmp_path)
    (repo / "infra").mkdir()
    (repo / "infra" / "iam.tf").write_text("Resource = [x]\n", encoding="utf-8")
    s = Session(llm=None, repo_path=str(repo))
    s._called_tools = {"read_file", "read_file_x", "git_status"}
    resp = (
        "T003 ya está implementada en infra/iam.tf línea 28:\n"
        'Resource = [aws_sqs_queue.pauta_desactivada.arn]\n'
        "Cumple el AC grep -q 'aws_sqs_queue.pauta_desactivada.arn' ✅"
    )
    assert s._readonly_evidence_turn(resp) is True


def test_readonly_evidence_turn_falso_sin_herramientas(tmp_path):
    """Solo texto sin tools: escape vago, SÍ debe reintentar."""
    from orchestration.session import Session

    repo = _init_repo(tmp_path)
    s = Session(llm=None, repo_path=str(repo))
    s._called_tools = set()
    assert s._readonly_evidence_turn("Ya está implementada, archivo:línea 28 ✅") is False


def test_readonly_evidence_turn_falso_con_writes(tmp_path):
    """Si escribió algo, el turno NO es solo-verificación."""
    from orchestration.session import Session

    repo = _init_repo(tmp_path)
    s = Session(llm=None, repo_path=str(repo))
    s._called_tools = {"read_file", "git_status", "edit_file"}
    assert s._readonly_evidence_turn("edité archivo:línea 28") is False


def test_readonly_evidence_turn_falso_sin_evidencia(tmp_path):
    """Leyó 2 archivos pero no cita archivo:línea ni verify: vago, reintentar."""
    from orchestration.session import Session

    repo = _init_repo(tmp_path)
    s = Session(llm=None, repo_path=str(repo))
    s._called_tools = {"read_file", "git_status"}
    assert s._readonly_evidence_turn("Creo que ya está, no vi nada raro.") is False


def test_readonly_evidence_turn_cuenta_llamadas_no_nombres(tmp_path):
    """T006: 2 read_file a archivos distintos + veredicto con path real =
    turno de verificación válido. Contar por NOMBRE daba 1 → retry forzado."""
    from orchestration.session import Session, _ToolCallLog

    repo = _init_repo(tmp_path)
    (repo / "infra").mkdir()
    (repo / "infra" / "iam.tf").write_text("x\n", encoding="utf-8")
    s = Session(llm=None, repo_path=str(repo))
    log = _ToolCallLog()
    log.add("read_file")
    log.add("read_file")
    s._called_tools = log
    resp = "T006 ya implementada en infra/iam.tf líneas 248-271 ✅"
    assert s._readonly_evidence_turn(resp) is True
    # Con UNA sola llamada sigue reintentando
    log1 = _ToolCallLog()
    log1.add("read_file")
    s._called_tools = log1
    assert s._readonly_evidence_turn(resp) is False


def test_tool_call_log_interfaz_set():
    """_ToolCallLog respeta la interfaz de set que usa el resto del código."""
    from orchestration.session import READISH_TOOL_NAMES, _ToolCallLog

    log = _ToolCallLog()
    log.add("read_file")
    log.add("read_file")
    log.add("git_status")
    assert "read_file" in log
    assert len(log) == 2
    assert sorted(log) == ["git_status", "read_file"]
    inter = log & READISH_TOOL_NAMES
    assert "read_file" in inter and "git_status" in inter
    log.discard("git_status")
    assert "git_status" not in log and log.counts == {"read_file": 2}
    log.clear()
    assert log.counts == {} and len(log) == 0


def test_verify_skipped_solo_da_none(tmp_path):
    """T001-docs: solo SKIPPED → verify_state None (N/A), no True ni False."""
    from orchestration.session import Session

    repo = _init_repo(tmp_path)
    s = Session(llm=None, repo_path=str(repo))
    s._called_tools = {"run_verify"}
    s._verify_results = {"run_verify": None}
    assert s._verify_all_passed() is None
    assert s._verify_all_skipped() is True


def test_verify_mixto_true_y_skipped_da_true(tmp_path):
    from orchestration.session import Session

    repo = _init_repo(tmp_path)
    s = Session(llm=None, repo_path=str(repo))
    s._called_tools = {"run_lint", "run_verify"}
    s._verify_results = {"run_lint": True, "run_verify": None}
    assert s._verify_all_passed() is True
    assert s._verify_all_skipped() is False


def test_verify_mixto_false_y_skipped_da_false(tmp_path):
    from orchestration.session import Session

    repo = _init_repo(tmp_path)
    s = Session(llm=None, repo_path=str(repo))
    s._called_tools = {"run_lint", "run_verify"}
    s._verify_results = {"run_lint": False, "run_verify": None}
    assert s._verify_all_passed() is False


def test_close_skipped_muestra_na(tmp_path):
    """Cierre docs-only: 'Verificación: N/A', sin alarma FALLÓ ni evidencia."""
    from orchestration.session import Session

    repo = _init_repo(tmp_path)
    (repo / "doc.md").write_text("# x\n", encoding="utf-8")
    s = Session(llm=None, repo_path=str(repo))
    s._turn_start_dirty = frozenset()
    s._called_tools = {"run_verify", "read_file"}
    s._verify_results = {"run_verify": None}
    s._last_response = "ADR-040 creado, ver doc.md:12 ✅"
    close = s._deterministic_close()
    assert "Verificación: N/A" in close
    assert "FALLÓ" not in close
    assert "Evidencia: ⚠️" not in close


def test_close_snapshot_none_sin_claim(tmp_path):
    """Snapshot fallido (None): se lista todo SIN 'en este turno'."""
    from orchestration.session import Session

    repo = _init_repo(tmp_path)
    (repo / "a.ts").write_text("x\n", encoding="utf-8")
    s = Session(llm=None, repo_path=str(repo))
    s._turn_start_dirty = None
    s._called_tools = {"edit_file"}
    close = s._deterministic_close()
    assert "en este turno" not in close
    assert "a.ts" in close


def test_failed_close_flip_done_sin_verify_avisa(tmp_path):
    """Regresión T004: tasks.json marcado done sin verify corrido → el
    cierre fallido lo señala explícito (antes quedaba done en silencio)."""
    repo = _init_repo(tmp_path)
    tasks_dir = repo / ".agent" / "tasks" / "ulab-1"
    tasks_dir.mkdir(parents=True)
    (tasks_dir / "tasks.json").write_text(
        '[{"id": "T004", "status": "done"}]', encoding="utf-8",
    )
    s = _make_session(repo)
    s._called_tools = {"apply_patch"}
    msg = s._failed_turn_close()
    assert "fallido" in msg.lower()
    assert "SIN verificación" in msg
    assert "tasks.json" in msg


def test_failed_close_flip_con_verify_no_avisa_extra(tmp_path):
    """Con verify corrido (aunque sea SKIPPED de docs), el aviso extra
    sobre el flip no aparece: el done tiene respaldo."""
    repo = _init_repo(tmp_path)
    tasks_dir = repo / ".agent" / "tasks" / "ulab-1"
    tasks_dir.mkdir(parents=True)
    (tasks_dir / "tasks.json").write_text(
        '[{"id": "T004", "status": "done"}]', encoding="utf-8",
    )
    s = _make_session(repo)
    s._called_tools = {"apply_patch", "run_verify"}
    s._verify_results = {"run_verify": None}  # SKIPPED docs/infra
    msg = s._failed_turn_close()
    assert "SIN verificación" not in msg


def test_closing_verdict_excerpt_trunca_y_vacio():
    """El veredicto impreso al cerrar sin escribir: vacío → '' y largo → corte."""
    from orchestration.session import _closing_verdict_excerpt

    assert _closing_verdict_excerpt(None) == ""
    assert _closing_verdict_excerpt("   ") == ""
    corto = "T005 ya implementada en prisma/schema.prisma:42."
    assert _closing_verdict_excerpt(corto) == corto
    largo = "x" * 2000
    out = _closing_verdict_excerpt(largo)
    assert len(out) < len(largo) and out.endswith("…")


def test_snapshot_preserva_verify_entre_reintentos(tmp_path):
    """Regresión T006: run_verify ✅ + clear del retry → el cierre recuerda
    que la verificación corrió (antes afirmaba 'SIN verificación')."""
    repo = _init_repo(tmp_path)
    s = _make_session(repo)
    s._called_tools = {"apply_patch", "run_verify"}
    s._verify_results = {"run_verify": True}
    s._snapshot_turn_verify()
    s._called_tools.clear()  # lo que hace _enter_budget_retry
    s._verify_results.clear()
    assert s._turn_verify_tools == {"run_verify"}
    assert s._verify_all_passed() is True


def test_snapshot_writes_despues_de_pass_no_bendicen(tmp_path):
    """Verify stale no vale: PASSED acumulado + escrituras posteriores sin
    verificar → el cierre NO anuncia ✅."""
    repo = _init_repo(tmp_path)
    s = _make_session(repo)
    s._called_tools = {"run_verify"}
    s._verify_results = {"run_verify": True}
    s._snapshot_turn_verify()
    s._called_tools = {"edit_file"}
    s._verify_results = {}
    s._snapshot_turn_verify()
    s._called_tools.clear()
    s._verify_results.clear()
    assert s._verify_all_passed() is None


def test_snapshot_failed_acumulado_contamina_cierre(tmp_path):
    """Un FAILED acumulado (aunque el intento actual esté limpio) → False."""
    repo = _init_repo(tmp_path)
    s = _make_session(repo)
    s._called_tools = {"run_tests"}
    s._verify_results = {"run_tests": False}
    s._snapshot_turn_verify()
    s._called_tools.clear()
    s._verify_results.clear()
    assert s._verify_all_passed() is False


def test_failed_close_con_verify_acumulado_no_dice_sin_verificacion(tmp_path):
    """Regresión T006: flip done + verify ✅ en intento previo + cola fallida
    → el cierre dice que la verificación CORRIÓ, sin aviso de flip."""
    repo = _init_repo(tmp_path)
    tasks_dir = repo / ".agent" / "tasks" / "ulab-1"
    tasks_dir.mkdir(parents=True)
    (tasks_dir / "tasks.json").write_text(
        '[{"id": "T006", "status": "done"}]', encoding="utf-8",
    )
    s = _make_session(repo)
    s._called_tools = {"apply_patch", "run_verify"}
    s._verify_results = {"run_verify": True}
    s._snapshot_turn_verify()
    s._called_tools.clear()  # retries posteriores
    s._verify_results.clear()
    msg = s._failed_turn_close()
    assert "CORRIÓ" in msg
    assert "SIN verificación" not in msg


def test_note_verify_result_guarda_rojos_y_limpia_en_verde(tmp_path, monkeypatch):
    """Los rojos de /verify persisten en la sesión; el verde los limpia."""
    import cache as cache_mod
    from orchestration.session import Session

    monkeypatch.setattr(cache_mod, "CACHE_DB", str(tmp_path / "t.db"))
    repo = _init_repo(tmp_path)
    s = Session(llm=None, repo_path=str(repo))
    assert s._last_verify is None
    s.note_verify_result(False, "  ❌ tests: [FAILED] exit=1")
    assert s._last_verify == {
        "passed": False,
        "report": "  ❌ tests: [FAILED] exit=1",
        "details": "",
    }
    s.note_verify_result(True, "todo verde")
    assert s._last_verify is None


def test_pop_failed_verify_consume_una_vez(tmp_path, monkeypatch):
    """El turno vago retoma los rojos una sola vez (no nag eterno)."""
    import cache as cache_mod
    from orchestration.session import Session

    monkeypatch.setattr(cache_mod, "CACHE_DB", str(tmp_path / "t.db"))
    s = Session(llm=None, repo_path=str(tmp_path))
    assert s._pop_failed_verify() == ""
    s.note_verify_result(False, "  ❌ tests: [FAILED] exit=1")
    assert s._pop_failed_verify() == "  ❌ tests: [FAILED] exit=1"
    assert s._pop_failed_verify() == ""
    assert s._last_verify is None


def test_failed_verify_suffix_ordena_no_repetir_bateria():
    """El sufijo inyectado trae el reporte y prohíbe repetir la batería."""
    from orchestration.session import _build_failed_verify_suffix

    out = _build_failed_verify_suffix("  ❌ tests: [FAILED] exit=1")
    assert "❌ tests" in out
    assert "batería completa" in out
    assert "done sin verde" in out
    assert "CONEXIÓN" in out


def test_note_verify_result_guarda_details(tmp_path, monkeypatch):
    """Los rojos se guardan con detalle (cola) para el turno siguiente."""
    import cache as cache_mod
    from orchestration.session import Session

    monkeypatch.setattr(cache_mod, "CACHE_DB", str(tmp_path / "t.db"))
    s = Session(llm=None, repo_path=str(tmp_path))
    s.note_verify_result(False, "rep", {"tests": "tail con sample_fail_test"})
    assert s._last_verify is not None
    assert s._last_verify["details"] == "--- tests (cola) ---\ntail con sample_fail_test"
    rec = s._pop_failed_verify_record()
    assert rec["report"] == "rep"
    assert "sample_fail_test" in rec["details"]
    assert s._last_verify is None


def test_suffix_con_details_va_directo_a_archivos():
    """Con detalle guardado, la orden es atacar archivos sin re-ejecutar."""
    from orchestration.session import _build_failed_verify_suffix

    out = _build_failed_verify_suffix(
        "  ❌ tests: [FAILED] exit=1",
        details="--- tests (cola) ---\nF sample_fail_test.py::test_rojo",
    )
    assert "DIRECTO" in out
    assert "sample_fail_test" in out
    assert "CONEXIÓN" in out


def test_session_hidrata_rojos_y_ancla_desde_disco(tmp_path, monkeypatch):
    """Restart/pull no evaporan: Session nueva levanta rojos + ancla."""
    import cache as cache_mod
    from orchestration.session import Session

    monkeypatch.setattr(cache_mod, "CACHE_DB", str(tmp_path / "t.db"))
    repo = _init_repo(tmp_path)
    tasks = repo / "tasks.json"
    tasks.write_text("[]", encoding="utf-8")
    snap = cache_mod.snapshot_hash(str(repo))
    cache_mod.save_repo_state(
        str(repo),
        last_verify={"passed": False, "report": "R", "details": "D"},
        last_tasks_file=str(tasks),
        snapshot=snap,
    )
    s = Session(llm=None, repo_path=str(repo))
    assert s._last_verify == {"passed": False, "report": "R", "details": "D"}
    assert s._last_tasks_file == str(tasks)


def test_has_verdict_markers():
    """Conclusión vs plan: participios SÍ, infinitivos/futuro NO."""
    from orchestration.session import _has_verdict_markers

    assert _has_verdict_markers("T003 ya está implementada, cumple el AC")
    assert _has_verdict_markers("build en verde, tests passed")
    assert not _has_verdict_markers("Plan: voy a verificar los tests")
    assert not _has_verdict_markers("leí tasks.json")
    assert not _has_verdict_markers(None)


def test_readonly_evidence_turn_rechaza_plan_en_futuro(tmp_path):
    """Regresión turno A: plan en futuro citando tasks.json REAL no es
    evidencia — debe reintentar, no cerrar hueco."""
    from orchestration.session import Session

    repo = _init_repo(tmp_path)
    (repo / ".agent").mkdir()
    (repo / ".agent" / "tasks.json").write_text("[]", encoding="utf-8")
    s = Session(llm=None, repo_path=str(repo))
    s._called_tools = {"read_file", "git_status"}
    plan = (
        "Plan: (1) leer .agent/tasks.json para confirmar qué subtareas ya "
        "quedaron implementadas; (2) después completo y verifico."
    )
    assert s._readonly_evidence_turn(plan) is False


def test_readonly_evidence_turn_acepta_veredicto_con_path_real(tmp_path):
    """Veredicto en pasado con path real en disco SÍ cierra (sin file:línea)."""
    from orchestration.session import Session

    repo = _init_repo(tmp_path)
    (repo / ".agent").mkdir()
    (repo / ".agent" / "tasks.json").write_text("[]", encoding="utf-8")
    s = Session(llm=None, repo_path=str(repo))
    s._called_tools = {"read_file", "git_status"}
    assert s._readonly_evidence_turn(
        "T005 ya está implementada en .agent/tasks.json, verificado."
    ) is True


def test_tasks_anchor_line(tmp_path):
    """Ancla contra paths adivinados: nombra el archivo real de la sesión."""
    from orchestration.session import _tasks_anchor_line

    assert _tasks_anchor_line(None) == ""
    out = _tasks_anchor_line("/r/.agent/tasks/x/tasks.json")
    assert ".agent/tasks/x/tasks.json" in out
    assert "NO adivines" in out


def test_close_pre_dirty_avisa_truncado(tmp_path):
    """6 previas listan 5 + '(+1 más)' en vez de callar el truncado."""
    repo = _init_repo(tmp_path)
    for i in range(6):
        (repo / f"f{i}.txt").write_text("x", encoding="utf-8")
    s = _make_session(repo)
    s._turn_start_dirty = frozenset(f"f{i}.txt" for i in range(6))
    s._called_tools = set()
    close = s._deterministic_close()
    assert "(+1 más)" in close
    assert "NO modificó archivos propios" in close


def test_close_con_rojos_muestra_atribucion(tmp_path):
    """Cierre con rojos + baseline: Heredados vs Nuevos con nombres."""
    repo = _init_repo(tmp_path)
    (repo / "README.md").write_text("# repo\n\nEDITADO\n")
    s = _make_session(repo)
    s._called_tools = {"edit_file", "run_tests"}
    s._verify_results = {"run_tests": False}
    s._dedupe._failure_tails = {
        ("run_tests", str(repo)): [
            "tests/integration/users-table.test.ts",
            "tests/new.test.ts",
        ]
    }
    s._baseline = {"failing": ["tests/integration/users-table.test.ts"]}
    close = s._deterministic_close()
    assert "FALLÓ" in close
    assert "Heredados: 1" in close
    assert "Nuevos: 1" in close
    assert "users-table.test.ts" in close


def test_failed_close_con_rojos_muestra_atribucion(tmp_path):
    """Cierre fallido con rojos observados: atribuye aunque el intento quedó limpio."""
    repo = _init_repo(tmp_path)
    (repo / "README.md").write_text("# repo\n\nPENDIENTE\n")
    s = _make_session(repo)
    s._called_tools = set()
    s._verify_results = {}
    s._dedupe._failure_tails = {
        ("run_tests", str(repo)): ["tests/integration/users-table.test.ts"]
    }
    s._baseline = {"failing": ["tests/integration/users-table.test.ts"]}
    msg = s._failed_turn_close()
    assert "Heredados: 1" in msg
    assert "Nuevos: 0" in msg


def test_claim_turn_rechaza_segunda_hebra(tmp_path):
    """Un turno a la vez: otra hebra es rechazada, la misma puede anidar
    (bulk-chain, retry de credencial). E2E T011/T013 mezclados."""
    import threading

    from orchestration.session import Session

    s = Session(llm=None, repo_path=str(tmp_path))
    assert s._try_claim_turn() is True
    assert s._try_claim_turn() is True  # anidado misma hebra: OK
    s._release_turn()
    assert s._turn_thread is not None  # depth 1: sigue reclamado
    errors = []

    def otra_hebra():
        if s._try_claim_turn() is not False:
            errors.append("debió rechazar")

    t = threading.Thread(target=otra_hebra)
    t.start()
    t.join()
    assert not errors
    s._release_turn()
    assert s._turn_thread is None
    ok = []

    def otra_hebra_2():
        ok.append(s._try_claim_turn())
        s._release_turn()

    t2 = threading.Thread(target=otra_hebra_2)
    t2.start()
    t2.join()
    assert ok == [True]
    assert s._turn_thread is None


def test_release_turn_ajena_no_libera(tmp_path):
    """Release desde otra hebra es no-op: no roba el reclamo."""
    import threading

    from orchestration.session import Session

    s = Session(llm=None, repo_path=str(tmp_path))
    assert s._try_claim_turn() is True
    done = threading.Event()

    def ajena():
        s._release_turn()
        done.set()

    t = threading.Thread(target=ajena)
    t.start()
    assert done.wait(timeout=5)
    t.join()
    assert s._turn_thread is not None
    s._release_turn()
    assert s._turn_thread is None


def test_cancel_turn_alcanza_a_todas_las_vivas(tmp_path):
    """ESC cancela TODAS las tasks vivas, no solo la última (E2E post-ESC
    escribiendo: el puntero único mataba al turno nuevo)."""
    from orchestration.session import Session

    cancelled = []

    class FakeLoop:
        def call_soon_threadsafe(self, cb):
            cb()

    class FakeTask:
        def cancel(self):
            cancelled.append(True)

    s = Session(llm=None, repo_path=str(tmp_path))
    loop = FakeLoop()
    t1, t2 = FakeTask(), FakeTask()
    s._track_turn_task(loop, t1)
    s._track_turn_task(loop, t2)
    s._cancel_turn()
    assert len(cancelled) == 2
    s._untrack_turn_task(t1)
    s._cancel_turn()
    assert len(cancelled) == 3


def test_run_turn_rechaza_si_hay_otro_en_curso(tmp_path, capsys):
    """Segundo run_turn concurrente vuelve al instante con aviso (sin LLM)."""
    import threading
    import time

    from orchestration.session import Session

    s = Session(llm=None, repo_path=str(tmp_path))
    assert s._try_claim_turn() is True  # simula turno en curso
    errors = []
    started = time.monotonic()

    def otro_turno():
        try:
            s.run_turn("implementar T999")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t = threading.Thread(target=otro_turno)
    t.start()
    t.join(timeout=30)
    elapsed = time.monotonic() - started
    assert not t.is_alive()
    assert not errors
    assert elapsed < 10
    assert "turno en curso" in capsys.readouterr().out
    s._release_turn()
    assert s._turn_thread is None


def test_readonly_evidence_turn_plan_inicial_no_tapa_veredicto(tmp_path):
    """Regresión T011: turno que EMPIEZA con Plan y TERMINA con veredicto
    con evidencia cierra (el plan inicial no contamina la conclusión)."""
    from orchestration.session import Session

    repo = _init_repo(tmp_path)
    (repo / ".agent").mkdir()
    (repo / ".agent" / "tasks.json").write_text("[]", encoding="utf-8")
    s = Session(llm=None, repo_path=str(repo))
    s._called_tools = {"read_file", "git_status"}
    plan = (
        "Plan: (1) leer .agent/tasks.json para confirmar el criterio; "
        "(2) revisar implementación y tests ya presentes; "
        "(3) voy a continuar después con la verificación. " * 6
    )
    verdict = (
        "T011 verificada: .agent/tasks.json ya existe y contiene la "
        "implementación completa. No hay edits que hacer."
    )
    text = plan + verdict
    assert len(plan) > 600  # el plan queda FUERA de la cola evaluada
    assert s._readonly_evidence_turn(text) is True
