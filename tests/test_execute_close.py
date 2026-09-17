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
