# Fase 2 — paquete "SLM torpe" (2026-09-19)

Estado: implementado, suite 711 verde. E2E Spark en curso.

## Qué cambió

1. **Output-parser** (`llm_wrapper.py` + 7 tests): tras XML e inline-JSON
   (existentes, sin cambios), ahora recupera ```tool/```json fenceados,
   `<tool_call>…</tool_call>` (JSON o pythonico `Read(path='…')`), `[TOOL]
   name(...)`, JSON pelado `{"name","args"|"parameters"|"arguments"}` y
   reparación tolerante (trailing commas, single quotes). Solo args de tool
   calls, nunca archivos del repo.
2. **Thinking-budget** (`display/console.py`, `config.py`, `session.py` + 3
   tests): `max_reasoning_chars` por bloque (EXECUTE 12000 ≈ 3000 tokens,
   resto 6000; el 4B razona 1500-2500 antes de actuar). El corte por segundos
   no alcanza cuando el server emite thinking rápido y largo. El retry ya
   corría con thinking off (`force_tool_calls`); ahora el corte llega antes.
3. **AGENTS.md** (`orchestration/framework_rules.py::load_agents_md` +
   `agent_builder.py` + 5 tests): `<repo>/AGENTS.md` (tope 4000 chars,
   fail-open) inyectado al contexto de todos los roles.

## Evidencia

- Suite 711 (696 + 15 nuevos), ruff limpio.
- E2E AGENTS.md con Spark 4B (`/tmp/e2e-agentsmd`, 2026-09-19): repo con
  convención (type hints + docstring). El modelo leyó AGENTS.md primero
  (“Leeré el AGENTS.md…”) y entregó `def duplicar(n: int) -> int` con
  docstring: fn=True, hints=True, tools=[edit_file, read_file, run_verify].
- Re-run P04 pilot (regresión con guards nuevos): ✅ PASS en 868.6s
  (read → write → run_tests/run_verify). Pilot sigue 12/12.
