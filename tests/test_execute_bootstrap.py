"""Tests for EXECUTE message preload bootstrap + checklist + paste correction."""

import tempfile
from pathlib import Path

from orchestration.execute_bootstrap import (
    detect_repo_stacks,
    inject_repo_hints,
    suggest_minimal_files,
    build_paste_correction_suffix,
    extract_checklist_items,
    extract_requested_task_numbers,
    extract_review_findings,
    filter_task_sections,
    format_done_checklist,
    preload_cited_files,
    preload_for_review,
)

_SAMPLE_TASKS = """# Plan

## Tarea 1: Configurar env ⏳ PENDING

- [ ] Se agregan las nuevas variables de entorno (URL, API_KEY, timeout)
- [ ] Existe validación de configuración al iniciar la aplicación
- [ ] Se documentan las nuevas variables en README/ENV.md

---

## Tarea 2: Adapter HTTP ⏳ PENDING

- [ ] Cliente HTTP con timeout configurado (5s)
- [ ] Manejo de errores y mapeo de respuestas
- [ ] Logging estructurado para observabilidad
- [ ] Reutilizable desde distintos casos de uso

---

## Tarea 3: Retry ⏳ PENDING

- [ ] backoff

---

## Tarea 4: Integrate ⏳ PENDING

- [ ] use case
"""


class TestTaskFiltering:
    def test_extract_task_numbers(self):
        msg = "implementar la Tarea 1 y la Tarea 2 de /repo/tasks.md"
        assert extract_requested_task_numbers(msg) == [1, 2]

    def test_extract_tareas_plural(self):
        assert extract_requested_task_numbers("hacé las tareas 3 y 5") == [3, 5]

    def test_filter_keeps_only_requested(self):
        filtered = filter_task_sections(_SAMPLE_TASKS, [1, 2])
        assert "Configurar env" in filtered
        assert "Adapter HTTP" in filtered
        assert "backoff" not in filtered
        assert "use case" not in filtered
        assert "SOLO Tarea(s) 1, 2" in filtered

    def test_preload_filters_tasks_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks = Path(tmp) / "tasks.md"
            tasks.write_text(_SAMPLE_TASKS, encoding="utf-8")
            msg = f"implementar la Tarea 1 y la Tarea 2 de {tasks} completamente"
            out = preload_cited_files(msg)
            assert "Configurar env" in out
            assert "Adapter HTTP" in out
            assert "Retry" not in out
            assert "Integrate" not in out
            assert "ÚNICAMENTE Tarea 1, Tarea 2" in out
            assert "NO implementes otras tareas" in out

    def test_preload_skips_missing(self):
        msg = "implementar /nonexistent/path/tasks.md please"
        assert preload_cited_files(msg) == msg

    def test_search_code_missing_path_soft(self):
        from tools.search import search_code

        result = search_code.invoke({"path": "/nonexistent/dir", "pattern": "foo"})
        assert "does not exist" in result


class TestChecklist:
    def test_extract_checkbox_items(self):
        items = extract_checklist_items(_SAMPLE_TASKS)
        assert "Cliente HTTP con timeout configurado (5s)" in items
        assert "backoff" in items
        assert len(items) >= 7

    def test_preload_injects_antes_de_terminar(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks = Path(tmp) / "tasks.md"
            tasks.write_text(_SAMPLE_TASKS, encoding="utf-8")
            msg = f"implementar la Tarea 1 y la Tarea 2 de {tasks}"
            out = preload_cited_files(msg)
            assert "ANTES DE TERMINAR" in out
            assert "timeout configurado (5s)" in out
            assert "ENV.md" in out or "README/ENV.md" in out
            assert "backoff" not in out  # Tarea 3 filtrada
            assert "checklist verde" in out or "run_lint" in out

    def test_format_done_checklist_empty(self):
        assert format_done_checklist([]) == ""

    def test_format_review_mode(self):
        block = format_done_checklist(["timeout 5s"], mode="review")
        assert "CRITERIOS DE ACEPTACIÓN" in block
        assert "No inventes" in block


class TestRelativePreload:
    def test_preload_relative_path_with_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plans = root / "lucho-plans"
            plans.mkdir()
            tasks = plans / "tasks.md"
            tasks.write_text(_SAMPLE_TASKS, encoding="utf-8")
            msg = "implementar Tarea 1 de lucho-plans/tasks.md"
            out = preload_cited_files(msg, repo_path=str(root))
            assert "ANTES DE TERMINAR" in out
            assert "variables de entorno" in out


class TestReviewPreload:
    def test_preload_for_review_ac_aware(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks = Path(tmp) / "tasks.md"
            tasks.write_text(_SAMPLE_TASKS, encoding="utf-8")
            msg = f"review de Tarea 1 y Tarea 2 contra {tasks}"
            out = preload_for_review(msg)
            assert "REVIEW AC-AWARE" in out
            assert "CRITICAL = checkbox" in out
            assert "timeout configurado (5s)" in out
            assert "backoff" not in out

    def test_preload_for_review_clean_tree_shows_commits(self):
        """When tree is clean (code committed), review gets diff against main."""
        import subprocess
        import shutil
        tmp = tempfile.mkdtemp()
        try:
            subprocess.run(["git", "init"], cwd=tmp, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=tmp, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.name", "A"], cwd=tmp, capture_output=True, check=True)
            # Create initial commit on main
            (Path(tmp) / "a.txt").write_text("main content", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=tmp, capture_output=True, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=tmp, capture_output=True, check=True)
            # Create feature branch with a committed change
            subprocess.run(["git", "checkout", "-b", "feat"], cwd=tmp, capture_output=True, check=True)
            (Path(tmp) / "new-file.ts").write_text("new code", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=tmp, capture_output=True, check=True)
            subprocess.run(["git", "commit", "-m", "feat: new file"], cwd=tmp, capture_output=True, check=True)
            # Now tree is clean, but branch has new file
            out = preload_for_review("review", tmp)
            # Should show committed diff info (branch vs main)
            assert "main...feat" in out
            assert "new-file.ts" in out
            assert "ESTADO DE GIT" in out
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestPasteCorrection:
    def test_extract_findings(self):
        paste = """
### PROBLEMAS CRÍTICOS DETECTADOS:
#### 1. Falta timeout configurado en el adaptador
El adaptador usa fetch sin timeout.
#### 2. Falta documentación en ENV.md
#### 3. El adaptador no es reutilizable
WARNING: algo menor
"""
        findings = extract_review_findings(paste)
        assert any("timeout" in f.lower() for f in findings)
        assert any("env.md" in f.lower() for f in findings)

    def test_paste_suffix_no_nestjs_hardcode(self):
        paste = "x" * 50 + "\nCRITICAL: Falta timeout\nFalta ENV.md\n"
        suffix = build_paste_correction_suffix(paste)
        assert "app.module.ts" not in suffix
        assert "Hallazgos a resolver" in suffix
        assert "edit_file" in suffix
        assert "timeout" in suffix.lower()
        assert "ENV.md" in suffix or "env.md" in suffix.lower()


class TestRepoHints:
    def test_detect_node(self, tmp_path):
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        assert detect_repo_stacks(tmp_path) == ["node"]

    def test_detect_python(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        assert detect_repo_stacks(tmp_path) == ["python"]

    def test_detect_go(self, tmp_path):
        (tmp_path / "go.mod").write_text("module example.com/x\n\ngo 1.22\n", encoding="utf-8")
        assert detect_repo_stacks(tmp_path) == ["go"]

    def test_detect_java(self, tmp_path):
        (tmp_path / "pom.xml").write_text("<project/>\n", encoding="utf-8")
        assert detect_repo_stacks(tmp_path) == ["java"]

    def test_detect_multi_stack(self, tmp_path):
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
        assert detect_repo_stacks(tmp_path) == ["node", "go"]

    def test_inject_node_nestjs_layout(self, tmp_path):
        (tmp_path / "package.json").write_text('{"name":"api"}', encoding="utf-8")
        src = tmp_path / "apps" / "api" / "src"
        src.mkdir(parents=True)
        (src / "main.ts").write_text("bootstrap();\n", encoding="utf-8")
        (tmp_path / ".env.example").write_text("FOO=1\n", encoding="utf-8")
        hints = inject_repo_hints(str(tmp_path))
        assert "stacks detectados" in hints
        assert "node" in hints
        assert "main.ts" in hints
        assert "FOO=1" in hints

    def test_inject_python(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
        app = tmp_path / "app"
        app.mkdir()
        (app / "main.py").write_text("def main():\n    pass\n", encoding="utf-8")
        hints = inject_repo_hints(str(tmp_path))
        assert "python" in hints
        assert "main.py" in hints
        assert "def main" in hints

    def test_inject_go_cmd(self, tmp_path):
        (tmp_path / "go.mod").write_text("module example.com/svc\n\ngo 1.22\n", encoding="utf-8")
        cmd = tmp_path / "cmd" / "server"
        cmd.mkdir(parents=True)
        (cmd / "main.go").write_text("package main\n\nfunc main() {}\n", encoding="utf-8")
        (tmp_path / "internal").mkdir()
        (tmp_path / "internal" / "svc.go").write_text("package internal\n", encoding="utf-8")
        hints = inject_repo_hints(str(tmp_path))
        assert "go" in hints
        assert "cmd/server/main.go" in hints or "listing cmd" in hints
        assert "func main" in hints

    def test_inject_java_spring(self, tmp_path):
        (tmp_path / "pom.xml").write_text("<project><modelVersion>4.0.0</modelVersion></project>\n", encoding="utf-8")
        res = tmp_path / "src" / "main" / "resources"
        res.mkdir(parents=True)
        (res / "application.yml").write_text("server:\n  port: 8080\n", encoding="utf-8")
        java = tmp_path / "src" / "main" / "java" / "com" / "demo"
        java.mkdir(parents=True)
        (java / "DemoApplication.java").write_text(
            "package com.demo;\npublic class DemoApplication {}\n",
            encoding="utf-8",
        )
        hints = inject_repo_hints(str(tmp_path))
        assert "java" in hints
        assert "application.yml" in hints or "pom.xml" in hints
        assert "DemoApplication" in hints

    def test_preload_includes_repo_hints(self, tmp_path):
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        src = tmp_path / "apps" / "api" / "src"
        src.mkdir(parents=True)
        (src / "main.ts").write_text("x", encoding="utf-8")
        tasks = tmp_path / "tasks.md"
        tasks.write_text(_SAMPLE_TASKS, encoding="utf-8")
        out = preload_cited_files(
            f"implementar Tarea 1 de {tasks}",
            repo_path=str(tmp_path),
        )
        assert "CONTEXTO DE REPO PRECARGADO" in out or "stack-aware" in out
        assert "PRIMERA ACCIÓN" in out
        assert "node" in out

    def test_preload_bans_explore_at_top(self, tmp_path):
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        src = tmp_path / "apps" / "api" / "src"
        src.mkdir(parents=True)
        (src / "main.ts").write_text("x", encoding="utf-8")
        tasks = tmp_path / "tasks.md"
        tasks.write_text(_SAMPLE_TASKS, encoding="utf-8")
        out = preload_cited_files(
            f"implementar Tarea 1 de {tasks}",
            repo_path=str(tmp_path),
        )
        assert out.startswith("⛔ PROHIBIDO")
        assert "CONTEXTO DE REPO PRECARGADO" in out



class TestMinimalPlan:
    def test_suggest_env_and_adapter_node(self):
        items = [
            "Se agregan las nuevas variables de entorno (URL, API_KEY, timeout)",
            "Existe validación de configuración al iniciar la aplicación",
            "Se documentan las nuevas variables en README/ENV.md",
            "Cliente HTTP con timeout configurado (5s)",
            "Reutilizable desde distintos casos de uso",
        ]
        plan = suggest_minimal_files(items, ["node"])
        assert "ENV.md en la RAÍZ" in plan
        assert "SIN CRUD de dominio" in plan
        assert "app.module" in plan
        assert ".env.example" in plan

    def test_preload_includes_minimal_plan(self, tmp_path):
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        src = tmp_path / "apps" / "api" / "src"
        src.mkdir(parents=True)
        (src / "main.ts").write_text("x", encoding="utf-8")
        tasks = tmp_path / "tasks.md"
        tasks.write_text(_SAMPLE_TASKS, encoding="utf-8")
        out = preload_cited_files(
            f"implementar Tarea 1 y Tarea 2 de {tasks}",
            repo_path=str(tmp_path),
        )
        assert "PLAN DE ARCHIVOS MÍNIMOS" in out
        assert "ENV.md en la RAÍZ" in out
        assert "SIN CRUD" in out
