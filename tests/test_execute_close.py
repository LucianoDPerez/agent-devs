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
