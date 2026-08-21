"""End-to-end: verifica las 3 protecciones anti-destrucción en el flujo real.

Escenario: el modelo lee un archivo existente con funcionalidad valiosa (botones,
estado, SVG), intenta reescribirlo con write_file y la compuerta lo frena.

1. Guard: write_file bloqueado para archivos >5 líneas
2. Prompt: execute.md ya no sugiere write_file para archivos existentes
3. Gate: el retry inyecta el contenido original del archivo leído
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
    )


def _init_repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "package.json").write_text("{}")
    return tmp_path


# ── Guard: write_file bloqueado para archivos >5 líneas ────────────────────


def test_e2e_write_file_blocked_for_meaningful_file(tmp_path, monkeypatch):
    """Un archivo de 10 líneas con botones + estado: write_file debe ser
    rechazado. Solo archivos NUEVOS o ≤5 líneas son permitidos."""
    from tools.filesystem import write_file

    repo = _init_repo(tmp_path)
    # Simula un PacientesPage.tsx de ~10 líneas: tiene estado, botones, SVG
    page = repo / "src" / "pages" / "PacientesPage.tsx"
    page.parent.mkdir(parents=True, exist_ok=True)
    original = """import React, { useState } from 'react';

export function PacientesPage() {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <h1>Pacientes</h1>
      <button onClick={() => setOpen(true)}>
        <svg width="16" height="16"><line x1="5" y1="5" x2="19" y2="19"/></svg>
      </button>
    </div>
  );
}"""
    page.write_text(original)
    assert len(page.read_text().splitlines()) > 5  # guard target

    result = write_file.invoke({"path": str(page), "content": "rewritten"})
    assert "BLOQUEADO" in result, f"Expected BLOQUEADO, got: {result}"
    assert "edit_file" in result
    assert page.read_text() == original  # no se tocó nada


def test_e2e_write_file_allowed_for_new_file(tmp_path):
    """Archivo NUEVO: write_file debe funcionar sin restricción."""
    from tools.filesystem import write_file

    repo = _init_repo(tmp_path)
    new_path = repo / "src" / "pages" / "NewPage.tsx"
    new_path.parent.mkdir(parents=True, exist_ok=True)

    result = write_file.invoke({"path": str(new_path), "content": "hello"})
    assert "Written" in result or "✅" in result
    assert new_path.read_text() == "hello"


def test_e2e_write_file_allowed_for_tiny_config(tmp_path):
    """Archivo existente de ≤5 líneas: write_file sí permite sobrescribir
    (configs triviales como .env.example)."""
    from tools.filesystem import write_file

    repo = _init_repo(tmp_path)
    cfg = repo / "config.py"
    cfg.write_text("X = 1\n")
    assert len(cfg.read_text().splitlines()) <= 5

    result = write_file.invoke({"path": str(cfg), "content": "X = 2\n"})
    assert "Written" in result or "✅" in result
    assert cfg.read_text() == "X = 2\n"


# ── Prompt: execute.md ya no sugiere write_file para existentes ────────────


def test_e2e_execute_prompt_steers_to_edit_file():
    """El prompt EXECUTE debe distinguir NUEVO vs EXISTENTE y forzar
    edit_file para archivos que ya existen."""
    from core.roles import Role
    from orchestration.agent_builder import load_prompt

    prompt = load_prompt(Role.EXECUTE)
    assert "NUEVOS" in prompt, f"prompt must mention NUEVOS\n{prompt}"
    assert "EXISTENTES" in prompt, f"prompt must mention EXISTENTES\n{prompt}"
    assert "edit_file" in prompt
    assert "write_file" in prompt
    # write_file solo debe mencionarse en contexto de archivos NUEVOS
    write_idx = prompt.index("write_file")
    # Verificar que "NUEVOS" aparece en la misma línea/sentencia que write_file
    # (está antes de write_file, no después — la frase es
    #  "Para archivos NUEVOS usá write_file")
    assert prompt.rfind("NUEVOS", 0, write_idx) > write_idx - 150, (
        f"'NUEVOS' should appear near write_file (it's before it in the sentence)\n"
        f"Full prompt:\n{prompt}"
    )
    # "EXISTENTES" debe estar cerca de "edit_file"
    edit_idx = prompt.index("edit_file")
    assert prompt.rfind("EXISTENTES", 0, edit_idx) > edit_idx - 150, (
        f"'EXISTENTES' should appear near edit_file\n"
        f"Full prompt:\n{prompt}"
    )


def test_e2e_execute_prompt_has_bug_diagnostic_step():
    """El prompt EXECUTE debe tener un paso de diagnóstico para bugs
    que guíe al modelo a analizar antes de escribir."""
    from core.roles import Role
    from orchestration.agent_builder import load_prompt

    prompt = load_prompt(Role.EXECUTE)
    assert "ANALIZAR" in prompt, "Missing bug diagnostic section"
    assert "CAUSA RAÍZ" in prompt, "Must mention root cause analysis"
    assert "DESGLOSAR" in prompt, "Must break the task into subtasks"
    assert "leé el código que vas a tocar" in prompt or "leelo" in prompt.lower(), (
        "Must read code before writing"
    )


def test_e2e_execute_prompt_has_mandatory_verification():
    """El prompt EXECUTE debe exigir run_lint/run_tests/run_build como
    OBLIGATORIO, no como opcional."""
    from core.roles import Role
    from orchestration.agent_builder import load_prompt

    prompt = load_prompt(Role.EXECUTE)
    assert "VERIFICAR" in prompt, "Missing mandatory verification section"
    assert "run_lint" in prompt
    assert "run_tests" in prompt
    assert "run_build" in prompt
    assert "Después de escribir CADA subtarea" in prompt, "Verify must run per-subtask"
    assert "respondé LISTO" in prompt, (
        "Must block completion until verify is done"
    )


# ── Gate retry: inyecta contenido original ─────────────────────────────────


def test_e2e_gate_retry_includes_read_cache_snapshot():
    """Simula el flujo: read_file guarda en cache, write_file daña, gate
    falla → el mensaje de retry debe incluir el contenido original leído."""
    # Verificamos que la lógica de snapshot de contenido original existe
    # en el código fuente de Session.
    import inspect

    from orchestration.session import Session
    orig_src = inspect.getsource(Session.run_turn)
    gate_src = inspect.getsource(Session._rebuild_agent_gate_retry)
    assert "_read_cache" in orig_src, "Gate retry must snapshot _read_cache"
    assert "CONTENIDO ORIGINAL" in orig_src, "Gate retry must inject original content"
    assert "_orig_snapshot" in orig_src, "Gate must build original content block"
    assert "_rebuild_agent_gate_retry" in orig_src
    assert "_post_write_gate" in orig_src
    assert "GATE_RETRY_TOOLS" in gate_src


def test_e2e_budget_retry_tools_exclude_delete():
    """Regresión E2E real: en el retry de budget el 35B borró __init__.py de
    1851 líneas con delete_file y reescribió un stub de 40 (borrar+recrear
    bypassa el guard anti-sobrescritura). BUDGET_RETRY_TOOLS debe tener
    read_file (acotado, sin lecturas alucina) pero NO delete_file."""
    from tools import BUDGET_RETRY_TOOLS

    names = [t.name for t in BUDGET_RETRY_TOOLS]
    assert "read_file" in names, "Retry sin read_file: el 35B alucina/destruye"
    assert "delete_file" not in names, "delete_file en el retry bypassa el guard"
    assert "edit_file" in names
    assert "write_file" in names


def test_e2e_verify_gate_exists_in_session():
    """Session debe tener el método _verify_tools_called y la compuerta
    de verificación en run_turn."""
    import inspect

    from orchestration.session import Session

    assert hasattr(Session, "_verify_tools_called"), (
        "Missing _verify_tools_called method"
    )
    src = inspect.getsource(Session.run_turn)
    assert "_verify_tools_called" in src, (
        "run_turn must call _verify_tools_called in verify gate"
    )
    assert "Compuerta de verificación" in src, (
        "Verify gate comment must be present"
    )
    gate_src = inspect.getsource(Session._inject_verify_gate)
    assert "No ejecutaste run_lint" in gate_src, (
        "Verify gate message must mention run_lint/run_tests/run_build"
    )


def test_e2e_paste_correction_suffix_no_longer_suggests_write_for_existing():
    """El sufijo de corrección ya no debe sugerir write_file como opción
    genérica para archivos existentes."""
    from orchestration.execute_bootstrap import build_paste_correction_suffix

    msg = "corregir este error de build en Componente.tsx: syntax error at line 5"
    suffix = build_paste_correction_suffix(msg)

    # Debe mencionar edit_file
    assert "edit_file" in suffix
    # write_file debe ser calificado como "solo archivos NUEVOS"
    assert "NUEVOS" in suffix or "nuevos" in suffix.lower()


# ── Keyword path resolution ────────────────────────────────────────────────


def test_e2e_keyword_resolution_finds_matching_files(tmp_path):
    """_resolve_keyword_paths debe encontrar archivos de código que contengan
    la keyword del usuario."""
    from orchestration.execute_bootstrap import _resolve_keyword_paths

    repo = tmp_path
    (repo / "frontend" / "src" / "pages").mkdir(parents=True)
    (repo / "frontend" / "src" / "pages" / "PacientesPage.tsx").write_text("x")
    (repo / "frontend" / "src" / "pages" / "TurnosPage.tsx").write_text("x")
    (repo / "README.md").write_text("# doc")

    # Keyword match
    result = _resolve_keyword_paths("corregir bug en /pacientes", str(repo))
    assert "pacientes" in result.lower()
    assert "PacientesPage.tsx" in result
    assert "TurnosPage" not in result  # partial match should not include unrelated files
    # .md files should be excluded
    assert "README" not in result


def test_e2e_keyword_resolution_returns_empty_for_no_match(tmp_path):
    """Si no hay archivos que contengan la keyword, devuelve string vacío."""
    from orchestration.execute_bootstrap import _resolve_keyword_paths

    result = _resolve_keyword_paths("implementar feature X", str(tmp_path))
    assert result == ""


def test_e2e_keyword_resolution_excludes_noise_keywords(tmp_path):
    """Palabras comunes como 'error', 'que' no deben generar búsqueda."""
    from orchestration.execute_bootstrap import _resolve_keyword_paths

    result = _resolve_keyword_paths("corregir el error que hay en /pacientes", str(tmp_path))
    assert "error" not in result.lower() or "ARCHIVOS DETECTADOS" not in result


# ── Productive names (read_file no es productivo en EXECUTE) ────────────────


def test_e2e_read_file_not_productive_in_execute():
    """El ExploreBudget de EXECUTE debe usar VERIFY_TOOL_NAMES como productivas,
    no PRODUCTIVE_TOOL_NAMES (que incluye read_file). read_file NO debe
    considerarse productivo — el modelo se escondía en lecturas infinitas."""
    import inspect

    from orchestration.session import Session
    from orchestration.tool_dedupe import (
        PRODUCTIVE_TOOL_NAMES,
        READISH_TOOL_NAMES,
        VERIFY_TOOL_NAMES,
        ExploreBudget,
    )

    # Check that ExploreBudget.__init__ accepts productive_names
    src = inspect.getsource(ExploreBudget.__init__)
    assert "productive_names" in src, "ExploreBudget must accept productive_names"

    # Default: PRODUCTIVE_TOOL_NAMES includes READISH_TOOL_NAMES (read_file, etc.)
    assert "read_file" in READISH_TOOL_NAMES
    assert READISH_TOOL_NAMES.issubset(PRODUCTIVE_TOOL_NAMES)

    # EXECUTE: VERIFY_TOOL_NAMES does NOT include read_file
    assert "read_file" not in VERIFY_TOOL_NAMES
    assert "run_lint" in VERIFY_TOOL_NAMES
    assert "run_tests" in VERIFY_TOOL_NAMES
    assert "run_build" in VERIFY_TOOL_NAMES

    # Verify Session uses VERIFY_TOOL_NAMES for the EXECUTE budget
    src = inspect.getsource(Session.__init__)
    assert "productive_names=VERIFY_TOOL_NAMES" in src, (
        "EXECUTE ExploreBudget must use productive_names=VERIFY_TOOL_NAMES"
    )


# ── Recursion limit: alineado con el tope de tool calls + retry ─────────────


def test_e2e_recursion_limit_aligned_with_tool_call_cap():
    """Cada tool call consume ~2 pasos de recursion en langgraph (nodo agente +
    nodo tools). EXECUTE_RECURSION_LIMIT debe ser > 2×MAX_TOOL_CALLS_PER_TURN
    para que el tope de tool calls de stream corte ANTES que el recursion
    (E2E real: con recursion=30 el 35B murió en la tool #15 = run_tests, justo
    antes de ver el resultado)."""
    import config

    assert config.MAX_TOOL_CALLS_PER_TURN >= 30, (
        "30 tool calls son necesarias para el flujo completo del 35B "
        "(lecturas por rangos + edits + lint/tests/build + re-corregir + commit)"
    )
    assert config.EXECUTE_RECURSION_LIMIT > 2 * config.MAX_TOOL_CALLS_PER_TURN, (
        "EXECUTE_RECURSION_LIMIT debe estar por encima del tope de tool calls "
        "×2+1 para que el recursion nunca corte antes que stream"
    )


def test_e2e_recursion_retries_instead_of_failing():
    """Al cortar por recursion, EXECUTE debe reintentar con el agente de budget
    (read acotado + edit + write) en vez de fallar el turno entero — el modelo
    suele quedar a 1-2 pasos de terminar (E2E real: murió en run_tests)."""
    import inspect

    from orchestration.session import Session

    src = inspect.getsource(Session.run_turn)
    assert "_enter_budget_retry" in src, "Recursion retry must reuse _enter_budget_retry"
    # El retry de recursion solo aplica a EXECUTE (los cambios quedan en disco;
    # el ancla inyecta el contenido leído para que el retry continúe).
    marker = 'or "recursion" in err_text'
    assert marker in src
    recursion_block = src[src.index(marker):]
    assert "new_role == Role.EXECUTE" in recursion_block
    assert "_enter_budget_retry(" in recursion_block


def test_e2e_main_warns_on_dirty_repo():
    """main.py debe advertir en consola si el repo target arranca con cambios
    sin commitear (daño pre-existente contamina la verificación y el modelo
    gasta pasos arreglando lo ajeno)."""
    import main

    assert hasattr(main, "_warn_dirty_repo")
    assert "_warn_dirty_repo(repo_path)" in open(main.__file__).read()
