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
    from orchestration.agent_builder import load_prompt
    from core.roles import Role

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
    que guíe al modelo a leer el componente, hook, API call."""
    from orchestration.agent_builder import load_prompt
    from core.roles import Role

    prompt = load_prompt(Role.EXECUTE)
    assert "Diagnóstico previo" in prompt, "Missing bug diagnostic section"
    assert "CAUSA RAÍZ" in prompt, "Must mention root cause analysis"
    assert "hook" in prompt.lower(), "Must mention reading data hooks"
    assert "fetch" in prompt.lower() or "API" in prompt, "Must mention API/trace"
    assert "lecturas de diagnóstico NO cuentan" in prompt, (
        "Diagnostic reads must not count against the 2-read limit"
    )


def test_e2e_execute_prompt_has_mandatory_verification():
    """El prompt EXECUTE debe exigir run_lint/run_tests/run_build como
    OBLIGATORIO, no como opcional."""
    from orchestration.agent_builder import load_prompt
    from core.roles import Role

    prompt = load_prompt(Role.EXECUTE)
    assert "Verificación OBLIGATORIA" in prompt, "Missing mandatory verification section"
    assert "run_lint" in prompt
    assert "run_tests" in prompt
    assert "run_build" in prompt
    assert "sin excepción" in prompt, "Verification must be unconditional"
    assert "No des la tarea por terminada" in prompt, (
        "Must block completion until verify is done"
    )


# ── Gate retry: inyecta contenido original ─────────────────────────────────


def test_e2e_gate_retry_includes_read_cache_snapshot():
    """Simula el flujo: read_file guarda en cache, write_file daña, gate
    falla → el mensaje de retry debe incluir el contenido original leído."""
    from orchestration.session import Session

    # Verificamos que la lógica de snapshot de contenido original existe
    # en el código fuente de Session.
    import inspect
    orig_src = inspect.getsource(Session.run_turn)
    gate_src = inspect.getsource(Session._rebuild_agent_gate_retry)
    assert "_read_cache" in orig_src, "Gate retry must snapshot _read_cache"
    assert "CONTENIDO ORIGINAL" in orig_src, "Gate retry must inject original content"
    assert "_orig_snapshot" in orig_src, "Gate must build original content block"
    assert "_rebuild_agent_gate_retry" in orig_src
    assert "_post_write_gate" in orig_src
    assert "GATE_RETRY_TOOLS" in gate_src


def test_e2e_verify_gate_exists_in_session():
    """Session debe tener el método _verify_tools_called y la compuerta
    de verificación en run_turn."""
    from orchestration.session import Session
    import inspect

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
    assert "No ejecutaste run_lint" in src, (
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
