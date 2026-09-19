# Fase 1 — Write/Edit invariante + PLAN exploratorio (2026-09-19)

Estado: implementado, suite verde, e2e real verificado. Sin commitear.

## Qué cambió

1. **Read-before-Edit** (`tools/filesystem.py`):
   - `READ_SEEN_PATHS` + `clear_read_tracker()` + `_mark_read()` / `_was_read()`.
   - `read_file` y `write_file` (creador conoce el contenido) marcan; `edit_file` y
     `apply_patch` rechazan si el path no fue leído en la sesión, con mensaje que
     redirige a `read_file` primero.
   - `Session.start()` y `Session.reset()` limpian el tracker (por sesión, no por turno:
     leer en turno N habilita editar en N+1).
2. **PLAN exploratorio** (`prompts/plan.md`): regla 2 obliga ≥2-3 tool calls de lectura
   (`list_files` / `read_file` / `search_code` / `trace_component` / `inspect_*`) antes
   de entregar el plan. Las tools ya existían (`PLANNER_TOOLS`, 13); el modelo no las usaba.
3. **Excepción documentada (intencional, no bug):** `write_file` sigue permitiendo
   sobrescribir archivos de ≤5 líneas (`WRITE_FILE_OVERWRITE_MAX_LINES = 5`,
   `config.py:259`) para configs triviales. little-coder bloquea todo existente;
   nosotros mantenemos la excepción y la declaramos.

## Evidencia

- Suite: **694 passed** (690 previos + 4 nuevos `TestReadBeforeEdit`), ruff limpio.
- E2E real llama.cpp `spark2.5-4B` (n_ctx 26624):
  - PLAN “agregar saludar() en app.py”: `plan_explored=True` (antes 0 tools).
  - EXECUTE iteración 1 (path largo macOS): leyó primero pero se enredó con el path
    y no editó (`edit_ok=False`) — hallazgo: paths largos confunden al 4B.
  - EXECUTE iteración 2 (`/tmp/e2e-fase1`, path corto): `tools=[edit_file, read_file,
    run_lint, search_code]`, `edit_ok=True` en ~8 min.

## Archivos tocados

- `tools/filesystem.py` (+tracker y guards)
- `prompts/plan.md` (regla exploración)
- `orchestration/session.py` (clear en `start`/`reset`)
- `tests/test_filesystem.py` (+`TestReadBeforeEdit`, seed de lectura en setup)
- `tests/test_budget.py` (seed de lectura)
