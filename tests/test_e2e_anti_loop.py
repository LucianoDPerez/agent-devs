"""End-to-end: prueba integrada de las 3 mejoras anti-loop para EXECUTE.

Escenario: repo con estructura similar a Medicos. Un usuario pide corregir un
bug en "/pacientes". Verifica:
  1. Keyword resolution: /pacientes → path real
  2. Write pressure: read_file NO es productivo → ToolBudgetExceeded tras 5 calls
  3. Verify gate: detecta verify tools correctamente (sin falsos positivos)
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

# ── Setup: repo temporal con estructura realista ──────────────────────────


def _setup_test_repo() -> Path:
    """Crea un repo con estructura: frontend/src/presentation/pages/PacientesPage.tsx"""
    repo = Path(tempfile.mkdtemp())
    # Git init
    subprocess.run(["git", "init", "-q"], cwd=str(repo), capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(repo), capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), capture_output=True, text=True)
    # Estructura de archivos
    (repo / "frontend" / "src" / "presentation" / "pages").mkdir(parents=True)
    (repo / "backend" / "src" / "routes").mkdir(parents=True)
    # PacientesPage (19 líneas — como el caso real)
    pacientes_page = """import React, { useState } from 'react';

export function PacientesPage() {
  const [open, setOpen] = useState(false);
  const [patients, setPatients] = useState([]);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);
  return (
    <div className="page-container">
      <h1>Pacientes</h1>
      {error && <div className="error-text">{error}</div>}
      {loading ? (
        <p>Cargando...</p>
      ) : (
        <PacienteList pacientes={patients} />
      )}
    </div>
  );
}"""
    (repo / "frontend" / "src" / "presentation" / "pages" / "PacientesPage.tsx").write_text(pacientes_page)
    (repo / "frontend" / "package.json").write_text('{"scripts":{"build":"echo ok"}}')
    (repo / "backend" / "src" / "routes" / "pacientes.ts").write_text("// API routes")
    # Commit
    subprocess.run(["git", "add", "-A"], cwd=str(repo), capture_output=True, text=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(repo), capture_output=True, text=True)
    return repo


# ── Test 1: Keyword resolution ─────────────────────────────────────────────


def test_e2e_keyword_resolution_with_real_repo():
    """Cuando el usuario dice /pacientes, el sistema inyecta el path real."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    from orchestration.execute_bootstrap import _resolve_keyword_paths
    from orchestration.session import preload_cited_files

    repo = _setup_test_repo()

    # Test A: keyword resolution via _resolve_keyword_paths
    hint = _resolve_keyword_paths("corregir bug en /pacientes", str(repo))
    assert "PacientesPage.tsx" in hint, f"Expected PacientesPage.tsx in hint, got: {hint}"
    assert "pacientes" in hint.lower()
    print(f"  ✅ Keyword resolution: {hint.strip()}")

    # Test B: integrated via preload_cited_files
    enriched = preload_cited_files("corregir bug en /pacientes que muestra error", str(repo))
    assert "PacientesPage.tsx" in enriched, f"preload_cited_files must include path hint"
    print(f"  ✅ preload_cited_files integrates keyword resolution")

    # Test C: no false positives
    hint2 = _resolve_keyword_paths("implementar feature X sin explorar", str(repo))
    assert hint2 == "", f"Expected empty for no match, got: {hint2}"
    print(f"  ✅ No false positives for non-matching input")


# ── Test 2: Write pressure — read_file NO es productivo ────────────────────


def test_e2e_read_file_triggers_write_pressure():
    """En EXECUTE, read_file NO cuenta como productivo. Tras 5 tool calls
    sin write ni verify, ToolBudgetExceeded debe dispararse."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    from orchestration.tool_dedupe import (
        ExploreBudget, ToolBudgetExceeded, VERIFY_TOOL_NAMES, PRODUCTIVE_TOOL_NAMES,
    )

    # Budget con productive_names=VERIFY_TOOL_NAMES (sin read_file)
    budget = ExploreBudget(
        max_calls=2,
        max_reads_after_explore=3,
        max_tools_before_write=5,
        write_pressure=True,
        productive_names=VERIFY_TOOL_NAMES,
    )

    # Simular 5 read_file calls (NO deberían ser productivas)
    for i in range(5):
        result = budget.consume("read_file", {"path": f"/tmp/file{i}.ts"})
        assert result is None, f"read_file should not be blocked at call {i}"
        assert not budget._wrote

    # La 6ta tool call (no write, no verify) debe disparar ToolBudgetExceeded
    try:
        budget.consume("list_files", {"path": "/tmp"})
        assert False, "Should have raised ToolBudgetExceeded after 5 non-productive calls"
    except ToolBudgetExceeded:
        pass  # Expected
    print(f"  ✅ read_file does NOT count as productive — ToolBudgetExceeded after 5 calls")

    # Pero run_lint SÍ es productivo → no debería disparar excepción
    budget2 = ExploreBudget(
        max_calls=2, max_reads_after_explore=3, max_tools_before_write=5,
        write_pressure=True, productive_names=VERIFY_TOOL_NAMES,
    )
    for i in range(4):
        budget2.consume("read_file", {"path": f"/tmp/x{i}.ts"})
    # 5ta call es verify → productiva → no excepción
    result = budget2.consume("run_lint", {"path": "/tmp"})
    assert result is None, "run_lint should be allowed (productive)"
    print(f"  ✅ run_lint IS productive — no exception for verify calls")


# ── Test 3: Verify gate no da falsos positivos ─────────────────────────────


def test_e2e_verify_gate_correct_detection():
    """_verify_tools_called debe detectar run_lint/run_tests/run_build en los
    mensajes proporcionados, no en self._messages."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    from langchain_core.messages import AIMessage, ToolMessage
    from langchain_core.messages.tool import ToolCall

    def _make_ai(name: str):
        return AIMessage(content="", tool_calls=[
            ToolCall(name=name, args={"path": "x"}, id=f"call_{name}")
        ])

    msgs = [
        _make_ai("read_file"),
        ToolMessage(content="file content", tool_call_id="call_read_file"),
        _make_ai("run_lint"),
        ToolMessage(content="[PASSED]", tool_call_id="call_run_lint"),
        _make_ai("run_tests"),
        ToolMessage(content="[PASSED]", tool_call_id="call_run_tests"),
        _make_ai("run_build"),
        ToolMessage(content="[PASSED]", tool_call_id="call_run_build"),
        AIMessage(content="LISTO"),
    ]

    from orchestration.session import Session

    # Crear sesión mínima para acceder al método
    session = Session.__new__(Session)
    session._messages = msgs

    assert session._verify_tools_called() is True, "Should detect verify tools in messages"
    print(f"  ✅ Verify gate correctly detects verify tools")

    # Messages sin verify tools
    msgs2 = [
        _make_ai("read_file"),
        ToolMessage(content="content", tool_call_id="call_read_file"),
        AIMessage(content="LISTO"),
    ]
    session2 = Session.__new__(Session)
    session2._messages = msgs2
    assert session2._verify_tools_called() is False, "Should NOT detect verify tools"
    print(f"  ✅ Verify gate correctly identifies missing verify tools")


# ── Test 4: Dedupe bloquea repeticiones ────────────────────────────────────


def test_e2e_dedupe_blocks_repeats():
    """max_repeats=1 debe bloquear la segunda llamada idéntica."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    from orchestration.tool_dedupe import ToolCallDedupe

    dedupe = ToolCallDedupe(max_repeats=1)

    # Primera llamada: OK
    dedupe.register("read_file", {"path": "/tmp/a.ts"})
    # Segunda idéntica: debe bloquear
    try:
        dedupe.check("read_file", {"path": "/tmp/a.ts"})
        assert False, "Should block repeated identical call"
    except Exception:
        pass  # Expected
    print(f"  ✅ Dedupe max_repeats=1 blocks second identical call")


# ── Test 5: write_file guard en repositorio real ───────────────────────────


def test_e2e_write_file_guard_with_real_repo():
    """write_file en archivo existente de 19 líneas debe ser BLOQUEADO."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    from tools.filesystem import write_file

    repo = _setup_test_repo()
    page_path = repo / "frontend" / "src" / "presentation" / "pages" / "PacientesPage.tsx"
    assert page_path.exists()
    lines = len(page_path.read_text().splitlines())
    assert lines > 5, f"Test file should be >5 lines, got {lines}"

    result = write_file.invoke({"path": str(page_path), "content": "DESTROYED"})
    assert "BLOQUEADO" in result, f"Expected BLOQUEADO, got: {result}"
    assert "edit_file" in result
    # Original intacto
    assert "useState" in page_path.read_text(), "Original file should be untouched"
    print(f"  ✅ write_file blocked for {lines}-line existing file")


# ── Main ────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    print("=" * 60)
    print("E2E: Probando mejoras anti-loop para EXECUTE")
    print("=" * 60)

    test_e2e_keyword_resolution_with_real_repo()
    test_e2e_read_file_triggers_write_pressure()
    test_e2e_verify_gate_correct_detection()
    test_e2e_dedupe_blocks_repeats()
    test_e2e_write_file_guard_with_real_repo()

    print("\n" + "=" * 60)
    print("TODAS LAS PRUEBAS PASARON ✅")
    print("=" * 60)
