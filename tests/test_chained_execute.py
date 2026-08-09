"""Tests for the "analiza → implementa" chaining helpers (Bug 3)."""

import tempfile
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage

from orchestration.session import (
    _build_chained_execute_suffix,
    _derive_task_from_history,
    _extract_target_files,
    _is_ambiguous_execute,
)


def _make_repo() -> str:
    d = tempfile.mkdtemp()
    Path(d, "frontend/src/presentation/components").mkdir(parents=True)
    Path(d, "frontend/src/presentation/components/CreatePacienteModal.tsx").write_text("// x")
    return d


def test_is_ambiguous_execute_detects_bare_verb():
    assert _is_ambiguous_execute("implementa") is True
    assert _is_ambiguous_execute("arreglá el bug") is True
    assert _is_ambiguous_execute("hacelo") is True


def test_is_ambiguous_execute_false_for_concrete_tasks():
    repo = _make_repo()
    # path real citado → no ambiguo
    assert _is_ambiguous_execute(
        "implementa el fix del botón guardar en CreatePacienteModal.tsx", repo
    ) is False
    # tarea explícita
    assert _is_ambiguous_execute("implementá la Tarea 1") is False
    # review correction (lo captura otro flujo, pero helper también lo rechaza)
    assert _is_ambiguous_execute("aplicá las correcciones del review") is False
    # analiza no es un verbo de ejecución
    assert _is_ambiguous_execute("analizá por qué no funciona") is False


def test_is_ambiguous_execute_false_when_long_description():
    assert _is_ambiguous_execute("implementá el endpoint /health con validación") is False


def test_derive_task_from_history_picks_last_ai_message():
    msgs = [
        HumanMessage("analizá el botón guardar"),
        AIMessage("Hallazgo: el bug está en frontend/src/.../CreatePacienteModal.tsx:115"),
    ]
    task = _derive_task_from_history(msgs)
    assert task is not None
    assert "CreatePacienteModal.tsx:115" in task


def test_derive_task_from_history_returns_none_when_empty_ai():
    msgs = [
        HumanMessage("analizá X"),
        AIMessage(""),
    ]
    assert _derive_task_from_history(msgs) is None


def test_derive_task_from_history_returns_none_without_ai():
    msgs = [HumanMessage("implementa")]
    assert _derive_task_from_history(msgs) is None


def test_build_chained_execute_suffix_includes_markers():
    suffix = _build_chained_execute_suffix(
        "bug en frontend/src/components/modal.tsx:115",
        target_files=["frontend/src/components/modal.tsx"],
    )
    assert "INSTRUCCIÓN (RETOMANDO ANÁLISIS PREVIO)" in suffix
    assert "modal.tsx" in suffix


def test_build_chained_execute_suffix_lists_target_files():
    targets = ["frontend/src/components/modal.tsx", "backend/src/x.go"]
    suffix = _build_chained_execute_suffix("analysis text", target_files=targets)
    assert "frontend/src/components/modal.tsx" in suffix
    assert "backend/src/x.go" in suffix
    assert "NO explor" in suffix  # prohibición de explore (explora/explores)


def test_extract_target_files_pulls_paths_from_analysis():
    analysis = "bug en frontend/src/components/modal.tsx:115 y tambien en backend/src/x.go"
    targets = _extract_target_files(analysis)
    assert "frontend/src/components/modal.tsx" in targets
    assert "backend/src/x.go" in targets


def test_build_chained_execute_suffix_truncates_long_task():
    long_task = "x" * 5000
    suffix = _build_chained_execute_suffix(long_task)
    assert "truncado" in suffix
