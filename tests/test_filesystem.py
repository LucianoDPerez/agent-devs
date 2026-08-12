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
