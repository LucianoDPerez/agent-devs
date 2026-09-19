# AgentDevs vs little-coder vs Aider — comparación verificada con código (2026-09-19)

Fuentes: `/tmp/little-coder` (clon 2026-09-19, 35 extensiones en `.pi/extensions/`),
`/tmp/aider` (clon 2026-09-19), y este repo leído entero (`tools/filesystem.py`,
`config.py`, `orchestration/agent_builder.py`, `prompts/plan.md`, suite 694).

## Verificado en código ajeno

- `write-guard/index.ts`: Write rechaza existente (dispara en ~57% de Polyglot según
  su comentario) + cubre bypass por shell (`cat > file`, issue #70).
- `read-guard-edit/index.ts`: Edit exige Read previo en la sesión; `write`/`edit`
  exitosos marcan como conocido; clear en `session_start`. (Nuestra Fase 1 replica
  esta semántica.)
- `thinking-budget/index.ts`: cap 4096 + abort y retry con thinking off.
- `skill-inject/index.ts`: selección en 3 prioridades (error > recencia > intención),
  inyectada como tail message para no invalidar KV cache.
- `quality-monitor/`: assess en `turn_end` + corrección steer, máx 2.
- `plan-mode/`: sub-coders read-only concurrentes + 1-3 preguntas + plan escrito.
- Aider: núcleo = formatos de edición (`coders/search_replace.py`, `udiff*`,
  `editblock*`) + `repomap.py`; sin guards SLM (apuesta a git + diff review).

## Correcciones a la crítica original

1. Write-guard: la crítica decía que AgentDevs permite sobrescribir libremente.
   **Incorrecto**: guard existe (`config.py:259`, `filesystem.py:528`, >5 líneas).
   Gap restante: excepción ≤5 líneas + (no aplica) bypass shell.
2. Thinking-cap: la frase “no tenés thinking-cap” era **incorrecta**. Tenemos
   `EXECUTE_MAX_REASONING_TOKENS=1024`, `MAX_REASONING_SECONDS=180`,
   `EXECUTE_MAX_REASONING_SECONDS=150` y detección de reasoning-only. Falta el
   cap-con-retry-off y el parser ancho, no el control de reasoning.
3. Shell-bypass: **no aplica**. Verificado: `tools/` no tiene tool bash genérica
   (solo `run_*`, git, probe). Sin shell no hay bypass por shell.
4. “4 tools”: hoy little-coder tiene 35 extensiones, no 4. El minimalismo describe
   su núcleo pi, no el producto.

## Gaps reales confirmados

- Output-parser ancho por modelo (el nuestro en `test_llm_tool_recovery.py` es parcial).
- Thinking-budget con retry-off.
- Skill/knowledge inject dinámico por turno.
- Evidence store durable al compact (nuestro summary al 90% pierde detalle).
- Per-model profiles (tenemos tiers RAM estáticos).
- Plan-mode con sub-coders + preguntas (nuestro PLAN ahora explora, sin sub-coders).
- Benchmark externo reproducible (el mayor déficit de credibilidad, no de ingeniería).

## Lo que AgentDevs tiene y ellos no (verificado en este repo)

- `ExploreBudget` + `ToolCallDedupe` + retries write-only + `verify_gate.py`.
- Knowledge graph MCP (`trace_component` en 1 llamada) + extracción determinista
  de business logic + roles con budgets + judge LLM + memoria SQLite cross-sesión
  + batches bulk + TUI fullscreen + installer/doctor con tiers RAM.
