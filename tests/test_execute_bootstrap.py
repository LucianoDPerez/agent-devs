"""Tests for EXECUTE message preload bootstrap + checklist + paste correction."""

import tempfile
from pathlib import Path

from orchestration.execute_bootstrap import (
    build_paste_correction_suffix,
    detect_bulk_file_count,
    detect_repo_stacks,
    extract_checklist_items,
    extract_requested_task_numbers,
    extract_review_findings,
    extract_scope_files,
    extract_task_range,
    filter_json_task_sections,
    filter_task_sections,
    format_done_checklist,
    inject_repo_hints,
    pinned_task_header,
    pinned_task_scope_files,
    preload_cited_files,
    preload_for_review,
    suggest_minimal_files,
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

    def test_extract_task_id_t002(self):
        """IDs estilo T002 (convención .agent/tasks): pinnaban en vacío."""
        assert extract_requested_task_numbers(
            "implementar T002 desde /repo/.agent/tasks/ulab-1/tasks.json") == [2]
        assert extract_requested_task_numbers("implementar T002 y T003 dale") == [2, 3]

    def test_extract_task_range(self):
        assert extract_task_range("/tasks-pool T001-T010 plans/t.md") == (1, 10)
        assert extract_task_range("pool T1-T3") == (1, 3)
        assert extract_task_range("tareas 4 al 6") == (4, 6)
        assert extract_task_range("T007 hasta T009 dale") == (7, 9)
        assert extract_task_range("T010-T001") == (1, 10)  # invertido se ordena

    def test_extract_task_range_rechaza_ruido(self):
        assert extract_task_range("del 2024 al 2026") is None
        assert extract_task_range("hola como estas") is None
        assert extract_task_range("T001-T999") is None  # cap 50

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
        import shutil
        import subprocess
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

    def test_inject_lists_asset_bin_dir(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='pkg'\n", encoding="utf-8")
        src = tmp_path / "src" / "spec_kitti_cli"
        src.mkdir(parents=True)
        (src / "__init__.py").write_text("VERSION = '1.0'\n", encoding="utf-8")
        bin_dir = src / "bin"
        bin_dir.mkdir()
        (bin_dir / "spec-kitti-telemetry").write_bytes(b"\x7fELF not really")
        (bin_dir / "spec-kitti-agent").write_bytes(b"\x7fELF not really")
        hints = inject_repo_hints(str(tmp_path))
        assert "listing assets" in hints
        assert "spec-kitti-telemetry" in hints
        assert "spec-kitti-agent" in hints

    def test_inject_no_asset_dir_yields_no_error(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='pkg'\n", encoding="utf-8")
        src = tmp_path / "src"
        src.mkdir(parents=True)
        (src / "module.py").write_text("X = 1\n", encoding="utf-8")
        hints = inject_repo_hints(str(tmp_path))
        assert "listing assets" not in hints
        assert "src" in hints

    def test_inject_lists_nested_asset_subdir_files(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='pkg'\n", encoding="utf-8")
        src = tmp_path / "src" / "spec_kitti_cli"
        (src / "templates" / "commands").mkdir(parents=True)
        (src / "templates" / "vscode-settings.json").write_text("{}", encoding="utf-8")
        for name in ("spec-kitti.clarify.md", "spec-kitti.implement.md", "spec-kitti.plan.md"):
            (src / "templates" / "commands" / name).write_text("# cmd\n", encoding="utf-8")
        (src / "__init__.py").write_text("VERSION = '1.0'\n", encoding="utf-8")
        hints = inject_repo_hints(str(tmp_path))
        assert "listing assets src/spec_kitti_cli/templates" in hints
        assert "commands/" in hints
        assert "commands/spec-kitti.clarify.md" in hints
        assert "commands/spec-kitti.implement.md" in hints
        assert "commands/spec-kitti.plan.md" in hints

    def test_detect_bulk_file_count_from_task8_prompt(self):
        task8 = (
            "implementar Task 8: Agregar hooks de telemetría en los 14 templates de commands\n"
            "Resumen: Modificar cada archivo .md de command para que incluya las llamadas start y end\n"
            "Notas técnicas:\n"
            "- Lista completa de commands: implement, clarify, tasks, constitution, swagger, "
            "changelog, specify, refactor, discovery, sync-status, tasks.shape, sync, amend, plan\n"
        )
        assert detect_bulk_file_count(task8) == 14

    def test_detect_bulk_file_count_small_task_yields_zero(self):
        assert detect_bulk_file_count("modifica src/spec_kitti_cli/__init__.py") == 0
        assert detect_bulk_file_count("implementa la Tarea 1 de tasks.md") == 0

    def test_detect_bulk_file_count_explicit_count(self):
        assert detect_bulk_file_count("hay que modificar los 8 archivos de plantillas") == 8

    def test_missing_resources_notice(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='pkg'\n", encoding="utf-8")
        src = tmp_path / "src" / "spec_kitti_cli"
        src.mkdir(parents=True)
        (src / "__init__.py").write_text("def init():\n    pass\n", encoding="utf-8")
        tasks = tmp_path / "tasks.md"
        tasks.write_text(
            "## Tarea 1\n"
            "- [ ] copia desde src/spec_kitti_cli/bin/\n"
            "- [ ] si no hay binario no rompe el init\n",
            encoding="utf-8",
        )
        out = preload_cited_files(
            f"implementar Tarea 1 de {tasks}",
            repo_path=str(tmp_path),
        )
        assert "RECURSOS CITADOS QUE NO EXISTEN" in out
        assert "src/spec_kitti_cli/bin/" in out
        assert "no rompe el init" in out

    def test_missing_resources_no_false_positive_for_existing(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='pkg'\n", encoding="utf-8")
        src = tmp_path / "src" / "spec_kitti_cli"
        src.mkdir(parents=True)
        (src / "__init__.py").write_text("def init():\n    pass\n", encoding="utf-8")
        tasks = tmp_path / "tasks.md"
        tasks.write_text(
            "## Tarea 1\n- [ ] modifica src/spec_kitti_cli/__init__.py\n",
            encoding="utf-8",
        )
        out = preload_cited_files(
            f"implementar Tarea 1 de {tasks}",
            repo_path=str(tmp_path),
        )
        assert "RECURSOS CITADOS QUE NO EXISTEN" not in out

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
        assert "leé los archivos de la tarea con read_file" in out
        assert "node" in out

    def test_preload_permits_reads_then_write(self, tmp_path):
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
        assert out.startswith("PLAN: leé los archivos de la tarea con read_file")
        assert "write_file o edit_file" in out
        assert "sin list_files" in out
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

    def test_preload_for_analyze_inyecta_checklist_y_orden(self, tmp_path):
        """E2E real (35B): 'verificá si están hechas estas tareas file.md'
        respondió desde el caché sin leer. El preload debe inyectar contenido
        + checklist + orden de verificar contra código."""
        from orchestration.execute_bootstrap import preload_for_analyze

        tasks = tmp_path / "tareas.md"
        tasks.write_text(_SAMPLE_TASKS, encoding="utf-8")
        out = preload_for_analyze(
            f"verificá si están realizadas estas tareas {tasks}",
            repo_path=str(tmp_path),
        )
        assert out != f"verificá si están realizadas estas tareas {tasks}"
        assert "VERIFICACIÓN CON EVIDENCIA" in out
        assert "CHECKLIST A VERIFICAR" in out
        assert "Se agregan las nuevas variables de entorno" in out
        assert "NO escribas código" in out

    def test_preload_for_analyze_sin_citados_no_cambia_nada(self, tmp_path):
        from orchestration.execute_bootstrap import preload_for_analyze

        msg = "analizá si el login anda"
        assert preload_for_analyze(msg, repo_path=str(tmp_path)) == msg

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


class TestReviewVerificados:
    """T1b/9B: el informe solo listaba fallas y los criterios cumplidos
    quedaban sin veredicto. El formato exige sección Verificados."""

    def test_prompt_review_tiene_seccion_verificados(self):
        from core.roles import Role, load_prompt

        assert "Verificados" in load_prompt(Role.REVIEW)

    def test_preload_review_pide_verificados(self, tmp_path):
        from orchestration.execute_bootstrap import preload_for_review

        tasks = tmp_path / "tareas.md"
        tasks.write_text(_SAMPLE_TASKS, encoding="utf-8")
        out = preload_for_review(f"revisá estas tareas {tasks}",
                                 repo_path=str(tmp_path))
        assert "Verificados" in out


class TestReviewChecklistBeatsCleanTree:
    """E2E real T1b/12B: con working tree limpio el modelo declaró 'nada que
    verificar' ignorando el checklist citado. La instrucción debe ordenar
    verificar igual contra el código."""

    def test_preload_review_ordena_verificar_con_arbol_limpio(self, tmp_path):
        from orchestration.execute_bootstrap import preload_for_review

        tasks = tmp_path / "tareas.md"
        tasks.write_text(_SAMPLE_TASKS, encoding="utf-8")
        out = preload_for_review(f"verificá estas tareas {tasks}",
                                 repo_path=str(tmp_path))
        assert "aunque el working tree esté limpio" in out
        assert "NUNCA es un veredicto válido" in out


class TestJsonTaskPinning:
    """E2E real: pidió T002 de un tasks.json y el preload inyectó TODAS las
    tareas (T003 con infra/iam.tf incluida) → el modelo implementó T003 por
    iniciativa. El pin debe filtrar también en JSON."""

    def _tasks_json(self, tmp_path):
        import json

        p = tmp_path / "tasks.json"
        p.write_text(json.dumps({"tasks": [
            {"id": "T002", "diff": "outputs en infra/sqs.tf"},
            {"id": "T003", "diff": "permisos en infra/iam.tf"},
        ]}), encoding="utf-8")
        return p

    def test_filter_json_deja_solo_pinnada(self):
        import json

        raw = json.dumps({"tasks": [
            {"id": "T002", "diff": "outputs en infra/sqs.tf"},
            {"id": "T003", "diff": "permisos en infra/iam.tf"},
        ]})
        out = json.loads(filter_json_task_sections(raw, [2]))
        assert [e["id"] for e in out["tasks"]] == ["T002"]
        assert "iam" not in filter_json_task_sections(raw, [2])

    def test_filter_json_fail_open(self):
        assert filter_json_task_sections("no-json{{{", [2]) == "no-json{{{"
        import json

        raw = json.dumps({"tasks": [{"id": "T009", "diff": "x"}]})
        assert filter_json_task_sections(raw, [2]) == raw

    def test_preload_json_no_inyecta_otras_tareas(self, tmp_path):
        p = self._tasks_json(tmp_path)
        out = preload_cited_files(f"implementar T002 desde {p}",
                                  repo_path=str(tmp_path))
        assert "sqs.tf" in out
        # El bloque citado (no el aviso de recursos faltantes) trae solo T002.
        cited = out.split("CONTENIDO YA CARGADO")[1].split("FIN tasks.json")[0]
        assert "iam.tf" not in cited
        assert "solo Tarea(s) 2" in out

    def test_scope_files_de_pinnada(self, tmp_path):
        p = self._tasks_json(tmp_path)
        scope = pinned_task_scope_files(f"implementar T002 desde {p}",
                                        repo_path=str(tmp_path))
        assert "infra/sqs.tf" in scope
        assert "infra/iam.tf" not in scope

    def test_extract_scope_files(self):
        assert extract_scope_files("outputs en infra/sqs.tf y schema.prisma") == {
            "infra/sqs.tf", "schema.prisma",
        }
        assert extract_scope_files("sin paths acá") == set()


class TestScopeBreaker:
    """Breaker de scope creep: 1-3 writes fuera del alcance pasan (auxiliares),
    el 4º levanta excepción con evidencia."""

    def _wrapped(self, tmp_path):
        from orchestration.tool_dedupe import (
            ExploreBudget,
            ToolCallDedupe,
            wrap_tools_with_dedupe,
        )
        from tools.filesystem import edit_file

        for rel in ("infra/sqs.tf", "infra/iam.tf", "a.ts", "b.ts", "c.ts"):
            p = tmp_path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x\n", encoding="utf-8")
        dd = ToolCallDedupe(max_repeats=9)
        dd.scope_files = frozenset({"infra/sqs.tf"})
        w = wrap_tools_with_dedupe([edit_file], dd, ExploreBudget(),
                                   repo_path=str(tmp_path))[0]
        return w

    def test_in_scope_pasa(self, tmp_path):
        w = self._wrapped(tmp_path)
        out = w.invoke({"path": str(tmp_path / "infra" / "sqs.tf"),
                        "old_str": "zzz", "new_str": "y"})
        assert "SCOPE CREEP" not in out

    def test_tres_fuera_pasan_cuarto_frena(self, tmp_path):
        import pytest

        from orchestration.tool_dedupe import ToolBudgetExceeded

        w = self._wrapped(tmp_path)
        for rel in ("infra/iam.tf", "a.ts", "b.ts"):
            out = w.invoke({"path": str(tmp_path / rel),
                            "old_str": "zzz", "new_str": "y"})
            assert "SCOPE CREEP" not in out
        with pytest.raises(ToolBudgetExceeded, match="SCOPE CREEP"):
            w.invoke({"path": str(tmp_path / "c.ts"),
                      "old_str": "zzz", "new_str": "y"})

    def test_sin_pin_no_hay_enforcement(self, tmp_path):
        from orchestration.tool_dedupe import (
            ExploreBudget,
            ToolCallDedupe,
            wrap_tools_with_dedupe,
        )
        from tools.filesystem import edit_file

        p = tmp_path / "a.ts"
        p.write_text("x\n", encoding="utf-8")
        dd = ToolCallDedupe(max_repeats=9)
        w = wrap_tools_with_dedupe([edit_file], dd, ExploreBudget(),
                                   repo_path=str(tmp_path))[0]
        for i in range(3):
            out = w.invoke({"path": str(p), "old_str": f"zzz-{i}",
                            "new_str": "y"})
            assert "SCOPE CREEP" not in out

    def test_spec_hereda_alcance(self, tmp_path):
        from orchestration.tool_dedupe import _in_scope

        assert _in_scope("/r/x/foo.spec.ts", frozenset({"x/foo.ts"})) is True
        assert _in_scope("/r/infra/iam.tf", frozenset({"infra/sqs.tf"})) is False


class TestJsonChecklist:
    """El banner decía '+ checklist AC' también para JSON, pero no había
    checklist (solo .md lo generaba). Ahora el pin JSON trae su resumen."""

    def _tasks_json(self, tmp_path):
        import json

        p = tmp_path / "tasks.json"
        p.write_text(json.dumps({"tasks": [
            {"id": "T012", "title": "Resolver SQS desactivada URL en boot",
             "status": "pending", "file": "src/main.ts",
             "verify": "grep -q DESACTIVADA src/main.ts",
             "depends_on": ["T011"]},
            {"id": "T013", "title": "Otra cosa", "status": "pending",
             "file": "src/x.ts"},
        ]}), encoding="utf-8")
        return p

    def test_checklist_resume_pinnada(self):
        import json

        from orchestration.execute_bootstrap import format_json_checklist

        raw = json.dumps({"tasks": [
            {"id": "T012", "title": "Resolver URL", "status": "pending",
             "file": "src/main.ts", "verify": "grep -q X src/main.ts"},
        ]})
        out = format_json_checklist(raw)
        assert "CHECKLIST DE TAREAS" in out
        assert "T012" in out and "Resolver URL" in out
        assert "src/main.ts" in out and "grep -q X" in out
        assert len(out) < 1500

    def test_checklist_fail_open(self):
        from orchestration.execute_bootstrap import format_json_checklist

        assert format_json_checklist("no-json{{{") == ""
        assert format_json_checklist('{"otro": 1}') == ""

    def test_preload_json_trae_checklist(self, tmp_path):
        p = self._tasks_json(tmp_path)
        out = preload_cited_files(f"implementar la tarea 012 de {p}",
                                  repo_path=str(tmp_path))
        assert "CHECKLIST DE TAREAS" in out
        assert "T012" in out and "src/main.ts" in out
        # T013 filtrada del pin: ni en el JSON ni en el checklist
        cited = out.split("CONTENIDO YA CARGADO")[1]
        assert "T013" not in cited


class TestPinnedTaskHeader:
    """Encabezado QUÉ hace la tarea (título + criterio) impreso ANTES de
    actuar. E2E T005: cierre honesto sin writes sin que quedara claro qué
    pedía la tarea."""

    def _tasks_json(self, tmp_path):
        import json

        p = tmp_path / "tasks.json"
        p.write_text(json.dumps({"tasks": [
            {"id": "T005", "title": "Relación desactivadaCommands en Prisma",
             "status": "pending",
             "acceptance": "prisma validate en verde y relación visible",
             "depends_on": ["T004"]},
            {"id": "T006", "title": "Otra cosa", "status": "pending"},
        ]}), encoding="utf-8")
        return p

    def test_header_muestra_titulo_y_criterio(self, tmp_path):
        p = self._tasks_json(tmp_path)
        out = pinned_task_header(f"implementar T005 de {p}",
                                 repo_path=str(tmp_path))
        assert "T005" in out
        assert "Relación desactivadaCommands" in out
        assert "Criterio:" in out and "prisma validate" in out
        assert "T006" not in out

    def test_header_id_numerico_y_lista(self, tmp_path):
        import json

        p = tmp_path / "tasks.json"
        p.write_text(json.dumps([
            {"id": 5, "title": "Cinco", "status": "pending"},
        ]), encoding="utf-8")
        out = pinned_task_header(f"implementar la tarea 5 de {p}",
                                 repo_path=str(tmp_path))
        assert "Cinco" in out

    def test_header_fail_open(self, tmp_path):
        assert pinned_task_header("implementar algo", repo_path=str(tmp_path)) == ""
        p = tmp_path / "tasks.json"
        p.write_text("no-json{{{", encoding="utf-8")
        assert pinned_task_header(f"implementar T005 de {p}",
                                  repo_path=str(tmp_path)) == ""
        p.write_text('{"otro": 1}', encoding="utf-8")
        assert pinned_task_header(f"implementar T005 de {p}",
                                  repo_path=str(tmp_path)) == ""
