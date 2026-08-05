"""Tests for EXECUTE message preload bootstrap + checklist + paste correction."""

import tempfile
from pathlib import Path

from orchestration.execute_bootstrap import (
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
