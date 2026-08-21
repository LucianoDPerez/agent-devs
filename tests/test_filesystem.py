"""Tests para filesystem tools."""

import tempfile
from pathlib import Path

from tools.filesystem import delete_file, edit_file, list_files, read_file, write_file


def _create_repo(files: dict[str, str]) -> str:
    tmp = tempfile.mkdtemp()
    for name, content in files.items():
        p = Path(tmp) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return tmp


class TestListFiles:
    def test_list_root(self):
        repo = _create_repo({"a.txt": "a", "b.txt": "b"})
        result = list_files.invoke({"path": repo})
        assert "a.txt" in result
        assert "b.txt" in result

    def test_list_recursive(self):
        repo = _create_repo({"sub/c.txt": "c"})
        result = list_files.invoke({"path": repo, "recursive": True})
        assert "c.txt" in result


class TestReadFile:
    def test_read_content(self):
        repo = _create_repo({"test.py": "print('hello')"})
        result = read_file.invoke({"path": str(Path(repo) / "test.py")})
        assert "print('hello')" in result

    def test_read_line_range(self):
        repo = _create_repo({"test.py": "line1\nline2\nline3\n"})
        result = read_file.invoke({"path": str(Path(repo) / "test.py"), "start_line": 2, "end_line": 3})
        assert "line2" in result
        assert "line3" in result


class TestWriteFile:
    def test_write_creates_file(self):
        repo = tempfile.mkdtemp()
        path = str(Path(repo) / "new.txt")
        result = write_file.invoke({"path": path, "content": "hello world"})
        assert "Written" in result or "✅" in result
        assert (Path(repo) / "new.txt").read_text() == "hello world"

    def test_write_small_existing_file_allowed(self):
        """Sobrescribir un archivo existente CHICO (≤30 líneas) sigue permitido."""
        repo = tempfile.mkdtemp()
        p = Path(repo) / "small.txt"
        p.write_text("a\nb\n", encoding="utf-8")
        result = write_file.invoke({"path": str(p), "content": "new content"})
        assert "Written" in result or "✅" in result
        assert p.read_text(encoding="utf-8") == "new content"

    def test_write_blocks_overwrite_of_large_existing_file(self):
        """Guard anti-destrucción: write_file NO puede sobrescribir un archivo
        existente grande (el 4B reescribiéndolo de memoria pierde código)."""
        repo = tempfile.mkdtemp()
        p = Path(repo) / "big.txt"
        original = "\n".join(f"line {i}" for i in range(50)) + "\n"
        p.write_text(original, encoding="utf-8")
        result = write_file.invoke({"path": str(p), "content": "DESTRUYE TODO"})
        assert "BLOQUEADO" in result
        assert "edit_file" in result
        assert p.read_text(encoding="utf-8") == original


class TestEditFile:
    def test_edit_replaces_text(self):
        repo = _create_repo({"test.py": "old code"})
        result = edit_file.invoke({
            "path": str(Path(repo) / "test.py"),
            "old_str": "old code",
            "new_str": "new code",
        })
        assert "Replaced" in result or "✅" in result
        assert (Path(repo) / "test.py").read_text() == "new code"


class TestEditFileFuzzy:
    """Matching tolerante estilo aider SEARCH/REPLACE: el modelo copia el
    bloque con contexto pero puede errar espacios/indentación."""

    CONTENT = (
        "import React from 'react'\n"
        "\n"
        "export function PacientesPage() {\n"
        "  const [pacientes, setPacientes] = useState([])\n"
        "  const [error, setError] = useState(null)\n"
        "\n"
        "  useEffect(() => {\n"
        "    loadPacientes()\n"
        "  }, [])\n"
        "}\n"
    )

    def _apply(self, old_str: str, new_str: str) -> str:
        repo = _create_repo({"PacientesPage.tsx": self.CONTENT})
        result = edit_file.invoke({
            "path": str(Path(repo) / "PacientesPage.tsx"),
            "old_str": old_str,
            "new_str": new_str,
        })
        assert "Replaced" in result or "✅" in result, result
        return (Path(repo) / "PacientesPage.tsx").read_text()

    def test_exact_match(self):
        out = self._apply(
            "const [error, setError] = useState(null)",
            "const [error, setError] = useState<string | null>(null)",
        )
        assert "useState<string | null>(null)" in out

    def test_trailing_whitespace_difference(self):
        # El modelo copió el bloque con espacios de más al final de línea
        out = self._apply(
            "const [pacientes, setPacientes] = useState([])   ",
            "const [pacientes, setPacientes] = useState<Paciente[]>([])",
        )
        assert "useState<Paciente[]>([])" in out

    def test_indentation_difference(self):
        # El modelo copió la línea sin indentación
        out = self._apply(
            "useEffect(() => {\n    loadPacientes()\n  }, [])",
            "useEffect(() => {\n    loadPacientes().catch(console.error)\n  }, [])",
        )
        assert "loadPacientes().catch(console.error)" in out

    def test_anchor_match_multiline(self):
        # Bloque con contexto: falla el match exacto de líneas, matchea por anclas
        out = self._apply(
            "import React from 'react'\n\n\n\nexport function PacientesPage() {\n"
            "  const [pacientes, setPacientes] = useState([])",
            "import React, { useState, useEffect } from 'react'\n\n"
            "export function PacientesPage() {\n"
            "  const [pacientes, setPacientes] = useState([])",
        )
        assert "useState, useEffect" in out

    def test_fuzzy_rejects_prefix_match_on_longer_line(self):
        """Regresión E2E real: old_str 'from pathlib import Path' matcheaba por
        PREFIJO la línea real 'from pathlib import Path, PureWindowsPath' y le
        borraba ', PureWindowsPath' (corrupción silenciosa). Cada línea del
        bloque debe anclarse al FINAL: el prefijo ya NO debe matchear."""
        repo = _create_repo({"app.py": "import sys\nfrom pathlib import Path, PureWindowsPath\n\nx = 1\n"})
        result = edit_file.invoke({
            "path": str(Path(repo) / "app.py"),
            "old_str": "import sys\nfrom pathlib import Path",
            "new_str": "import sys\nfrom pathlib import Path as P",
        })
        assert "old_str not found" in result, result
        assert "Path, PureWindowsPath" in (Path(repo) / "app.py").read_text()

    def test_not_found_returns_context_hint(self):
        repo = _create_repo({"PacientesPage.tsx": self.CONTENT})
        # La primera línea (ancla) existe, pero la segunda línea inventada NO →
        # falla el matching y el error muestra el contexto real del archivo
        result = edit_file.invoke({
            "path": str(Path(repo) / "PacientesPage.tsx"),
            "old_str": "export function PacientesPage() {\n  const [pacientes] = useState([])",
            "new_str": "export function PacientesPage() {\n  const [pacientes] = useState<Paciente[]>([])",
        })
        assert "old_str not found" in result
        assert "Contexto real del archivo" in result
        assert "PacientesPage" in result
        assert (Path(repo) / "PacientesPage.tsx").read_text() == self.CONTENT

    def test_blocks_whole_file_rewrite(self):
        """Guard anti-reescritura: edit_file con el archivo ENTERO como bloque
        debe ser bloqueado (el modelo chico reescribe de memoria y rompe
        interfaces/duplicados — bug real: useConsultasList/useMigracion)."""
        content = "\n".join(f"const line_{i} = {i};" for i in range(60)) + "\n"
        repo = _create_repo({"big.ts": content})
        result = edit_file.invoke({
            "path": str(Path(repo) / "big.ts"),
            "old_str": content,          # archivo entero
            "new_str": content + "// reescritura\n",
        })
        assert "QUIRÚRGICAS" in result
        assert (Path(repo) / "big.ts").read_text() == content

    def test_surgical_edit_still_allowed(self):
        content = "\n".join(f"const line_{i} = {i};" for i in range(60)) + "\n"
        repo = _create_repo({"big.ts": content})
        result = edit_file.invoke({
            "path": str(Path(repo) / "big.ts"),
            "old_str": "const line_10 = 10;",
            "new_str": "const line_10 = 100;",
        })
        assert "Replaced" in result or "✅" in result
        assert "const line_10 = 100;" in (Path(repo) / "big.ts").read_text()

    def test_ambiguous_match_needs_more_context(self):
        repo = _create_repo({"a.ts": "const x = 1\nconst x = 1\n"})
        result = edit_file.invoke({
            "path": str(Path(repo) / "a.ts"),
            "old_str": "const x = 1",
            "new_str": "const y = 2",
        })
        assert "possible matches" in result.lower() or "disambiguate" in result.lower()
        assert (Path(repo) / "a.ts").read_text() == "const x = 1\nconst x = 1\n"


class TestSoftErrors:
    def test_read_file_missing(self):
        result = read_file.invoke({"path": "/nonexistent/path/file.txt"})
        assert "does not exist" in result

    def test_read_file_missing_suggests_similar(self):
        """'Did you mean': si el path no existe pero hay un archivo con nombre
        similar en el repo, sugerirlo (el modelo inventa paths)."""
        repo = _create_repo({
            "package.json": "{}\n",
            "frontend/src/application/services/api.ts": "export const api = {};\n",
        })
        result = read_file.invoke({
            "path": str(Path(repo) / "frontend/src/services/api/pacientesApi.ts"),
        })
        assert "does not exist" in result
        assert "Archivos similares" in result
        assert "application/services/api.ts" in result

    def test_read_file_on_directory(self):
        repo = tempfile.mkdtemp()
        result = read_file.invoke({"path": repo})
        assert "directory" in result.lower()
        assert "list_files" in result

    def test_list_files_missing(self):
        result = list_files.invoke({"path": "/nonexistent/path"})
        assert "does not exist" in result

    def test_edit_file_missing(self):
        result = edit_file.invoke({
            "path": "/nonexistent/path/file.txt",
            "old_str": "a",
            "new_str": "b",
        })
        assert "does not exist" in result

    def test_edit_file_on_directory(self):
        repo = tempfile.mkdtemp()
        result = edit_file.invoke({
            "path": repo,
            "old_str": "a",
            "new_str": "b",
        })
        assert "directory" in result.lower()

    def test_write_file_to_directory(self):
        repo = tempfile.mkdtemp()
        result = write_file.invoke({"path": repo, "content": "hello"})
        assert "directory" in result.lower()
        assert "⛔" in result

    def test_write_protected_tasks_md(self):
        """El 4B no debe poder reescribir tasks.md (planificación protegida)."""
        repo = tempfile.mkdtemp()
        result = write_file.invoke({
            "path": str(Path(repo) / ".agent-devs" / "tasks.md"),
            "content": "# plan corrupto",
        })
        assert "PLANIFICACIÓN" in result
        assert "PROHIBIDO" in result
        assert not (Path(repo) / ".agent-devs" / "tasks.md").exists()

    def test_edit_protected_prd(self):
        repo = tempfile.mkdtemp()
        p = Path(repo) / "PRD.md"
        p.write_text("original", encoding="utf-8")
        result = edit_file.invoke({
            "path": str(p), "old_str": "original", "new_str": "corrupto",
        })
        assert "PROHIBIDO" in result
        assert p.read_text(encoding="utf-8") == "original"

    def test_delete_protected_plan(self):
        repo = tempfile.mkdtemp()
        p = Path(repo) / "plans" / "plan.md"
        p.parent.mkdir(parents=True)
        p.write_text("x", encoding="utf-8")
        result = delete_file.invoke({"path": str(p)})
        assert "PROHIBIDO" in result
        assert p.exists()


class TestWriteOverride:
    """Escalamiento de estrategia: write_file habilitado para un path tras
    fallar la cirugía fina de edit_file (WRITE_OVERRIDE_PATHS)."""

    def setup_method(self):
        from tools.filesystem import WRITE_OVERRIDE_PATHS, clear_write_overrides
        clear_write_overrides()
        self._set = WRITE_OVERRIDE_PATHS

    def teardown_method(self):
        from tools.filesystem import clear_write_overrides
        clear_write_overrides()

    def test_override_allows_full_overwrite(self):
        content = "\n".join(f"const line_{i} = {i};" for i in range(60)) + "\n"
        repo = _create_repo({"big.ts": content})
        path = str(Path(repo) / "big.ts")
        # Sin override: bloqueado
        assert "BLOQUEADO" in write_file.invoke({"path": path, "content": "x"})
        # Con override: se escribe
        self._set.add(path)
        result = write_file.invoke({"path": path, "content": "// full rewrite\n"})
        assert "Written" in result
        assert (Path(repo) / "big.ts").read_text() == "// full rewrite\n"

    def test_override_still_blocks_protected_paths(self):
        from tools.filesystem import clear_write_overrides
        repo = _create_repo({"tasks.md": "plan"})
        path = str(Path(repo) / "tasks.md")
        self._set.add(path)
        result = write_file.invoke({"path": path, "content": "hack"})
        assert "PLANIFICACIÓN" in result or "PROHIBIDO" in result


class TestIntegrityAndSyntaxCheck:
    """Validación post-write de integridad y sintaxis (Corrección 1).

    Detectar archivos que el LLM escribe TRUNCADOS (agota su output durante el
    tool call) y que quedaban rotos sin que nadie lo notara (E2E real:
    e2e_verify.sh terminó en `python3 -c "` sin cerrar)."""

    def test_balanced_content_ok(self):
        from tools.filesystem import _integrity_check
        assert _integrity_check('echo "hola"\nif [ -n "$X" ]; then\n  echo ok\nfi\n') is None

    def test_truncated_unclosed_quote_detected(self):
        from tools.filesystem import _integrity_check
        # Termina a mitad de una cadena abierta (caso real del e2e)
        result = _integrity_check('TOKENS_OK=$(echo "$NR_RESPONSE" | python3 -c "')
        assert result is not None
        assert "TRUNCADO" in result

    def test_truncated_unclosed_bracket_detected(self):
        from tools.filesystem import _integrity_check
        result = _integrity_check('def foo():\n    x = [1, 2, 3')
        assert result is not None
        assert "TRUNCADO" in result

    def test_syntax_check_bash_ok(self):
        from tools.filesystem import _syntax_check
        assert _syntax_check("/tmp/test.sh", '#!/usr/bin/env bash\necho "ok"\n') is None

    def test_syntax_check_bash_broken(self):
        from tools.filesystem import _syntax_check
        result = _syntax_check("/tmp/test.sh", 'echo "unclosed')
        assert result is not None
        assert "SINTAXIS" in result

    def test_syntax_check_python_broken(self):
        from tools.filesystem import _syntax_check
        result = _syntax_check("/tmp/test.py", "def foo(:\n    pass")
        assert result is not None
        assert "SINTAXIS" in result

    def test_syntax_check_unknown_ext_skips(self):
        from tools.filesystem import _syntax_check
        # Extensión sin checker → no verifica (fail-open)
        assert _syntax_check("/tmp/test.xyz", "anything !!!") is None

    def test_write_file_reports_truncation_warning(self):
        repo = tempfile.mkdtemp()
        path = str(Path(repo) / "script.sh")
        result = write_file.invoke({"path": path, "content": 'echo "unclosed'})
        # El write persiste (no bloqueado) pero el resultado advierte
        assert "Written" in result
        assert "INTEGRIDAD" in result or "SINTAXIS" in result

    def test_write_file_clean_content_no_warning(self):
        repo = tempfile.mkdtemp()
        path = str(Path(repo) / "clean.sh")
        result = write_file.invoke({"path": path, "content": '#!/usr/bin/env bash\necho "ok"\n'})
        assert "Written" in result
        assert "INTEGRIDAD" not in result
        assert "SINTAXIS" not in result


class TestMdIntegrityGuard:
    """Guard anti-corrupción .md: rechazar edits que rompan frontmatter/fences."""

    def _template_md(self) -> str:
        return (
            "---\n"
            "description: Template de command\n"
            "scripts:\n"
            "  sh: scripts/bash/check.sh --json\n"
            "---\n"
            "\n"
            "## User Input\n"
            "\n"
            "```text\n"
            "$ARGUMENTS\n"
            "```\n"
            "\n"
            "## Outline\n"
            "\n"
            "Pasos del comando.\n"
        )

    def test_edit_rejected_when_frontmatter_duplicated(self):
        repo = tempfile.mkdtemp()
        path = str(Path(repo) / "cmd.md")
        Path(path).write_text(self._template_md(), encoding="utf-8")
        # old_str fuzzy-incorrecto (como el E2E real): el modelo pasa un
        # frontmatter DISTINTO al real; el matcher ancla en líneas parecidas
        # y el new_str inserta un segundo bloque ---
        bad_old = (
            "---\n"
            "description: OTRA description que no existe en el archivo\n"
            "---\n"
            "\n"
            "## User Input\n"
        )
        bad_new = (
            "---\n"
            "description: OTRA description que no existe en el archivo\n"
            "scripts:\n"
            "  sh: otra/cosa.sh\n"
            "---\n"
            "\n"
            "Run: telemetry start\n"
            "\n"
            "## User Input\n"
        )
        result = edit_file.invoke({"path": path, "old_str": bad_old, "new_str": bad_new})
        assert "RECHAZADO" in result
        assert "frontmatter" in result
        # El archivo NO fue modificado
        assert Path(path).read_text(encoding="utf-8") == self._template_md()

    def test_edit_rejected_when_fence_lost(self):
        repo = tempfile.mkdtemp()
        path = str(Path(repo) / "cmd.md")
        Path(path).write_text(self._template_md(), encoding="utf-8")
        # Fusionar ```text + $ARGUMENTS (pierde el fence de apertura)
        result = edit_file.invoke({
            "path": path,
            "old_str": "```text\n$ARGUMENTS",
            "new_str": "$ARGUMENTS```",
        })
        assert "RECHAZADO" in result
        assert "```" in result or "desbalanceada" in result
        assert Path(path).read_text(encoding="utf-8") == self._template_md()

    def test_legit_edit_allowed(self):
        repo = tempfile.mkdtemp()
        path = str(Path(repo) / "cmd.md")
        original = self._template_md()
        Path(path).write_text(original, encoding="utf-8")
        hook = "\nRun: `.spec-kitti/bin/spec-kitti-telemetry end cmd`\n"
        result = edit_file.invoke({
            "path": path,
            "old_str": "Pasos del comando.\n",
            "new_str": "Pasos del comando." + hook,
        })
        assert "✅" in result
        assert "spec-kitti-telemetry" in Path(path).read_text(encoding="utf-8")

    def test_already_corrupted_file_not_blocked(self):
        repo = tempfile.mkdtemp()
        path = str(Path(repo) / "broken.md")
        broken = (
            "---\n"
            "description: A\n"
            "---\n"
            "description: B\n"
            "---\n"
            "\n"
            "contenido\n"
        )
        Path(path).write_text(broken, encoding="utf-8")
        # Editar un archivo YA corrupto no debe bloquearse por este guard
        result = edit_file.invoke({
            "path": path,
            "old_str": "contenido\n",
            "new_str": "contenido corregido\n",
        })
        assert "✅" in result

    def test_non_md_files_unaffected(self):
        repo = tempfile.mkdtemp()
        path = str(Path(repo) / "code.py")
        Path(path).write_text("x = 1\n", encoding="utf-8")
        result = edit_file.invoke({"path": path, "old_str": "x = 1", "new_str": "x = 2"})
        assert "✅" in result


class TestNoopEditGuard:
    """edit_file con old_str == new_str debe rechazarse (loop de no-ops E2E)."""

    def test_noop_edit_rejected(self):
        repo = tempfile.mkdtemp()
        path = str(Path(repo) / "x.md")
        Path(path).write_text("línea\n", encoding="utf-8")
        result = edit_file.invoke({
            "path": path, "old_str": "línea", "new_str": "línea",
        })
        assert "NO-OP" in result
        assert Path(path).read_text(encoding="utf-8") == "línea\n"

    def test_noop_edit_whitespace_only_diff_also_rejected(self):
        repo = tempfile.mkdtemp()
        path = str(Path(repo) / "y.md")
        Path(path).write_text("hola mundo\n", encoding="utf-8")
        result = edit_file.invoke({
            "path": path,
            "old_str": "hola mundo",
            "new_str": "hola mundo  ",
        })
        assert "NO-OP" in result
