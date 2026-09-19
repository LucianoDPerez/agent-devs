# Checklist de mejoras (priorizado, 2026-09-19)

- [x] **Fase 1** — Read-before-Edit + Write-guard + PLAN exploratorio. Verificado
  (694 tests, e2e Spark 4B). Ver `FASE1.md`.
- [x] **P0 — Polyglot-pilot (12)** — baseline 11/12 con Spark 4B.
  Runner: `polyglot-pilot/run_pilot.py`. Criterio objetivo (pytest), pins fijos.
- [x] **P0 — Fix cierre prematuro en crear-archivo** — `_commit_during_turn()`:
  el commit cuenta solo si es posterior al inicio del turno (pilot P01: el setup
  'init tests' engañaba a `_nothing_pending_to_write()`). Re-run P01 e2e en curso.
- [ ] **P1 — Output-parser por modelo** — ampliar `test_llm_tool_recovery.py`:
  ```tool / `<tool_call>` / JSON pelado / trailing commas / single quotes.
- [ ] **P1 — Thinking-budget con retry-off** — cap + reintento sin thinking
  (referencia: `thinking-budget/index.ts`, 4096).
- [ ] **P1 — Dynamic intervention** — inyectar hint ante error repetido
  (path inválido → sugerir `search_code`; re-lectura → exigir edit o verify).
  Equivalente nativo a skill-inject, sin otra LLM.
- [ ] **P2 — Evidence store** — `evidence_add/get/list` (snippets ≤1KB) que
  sobreviva compact/retry/role-switch. Hoy solo summary al 90%.
- [ ] **P2 — Per-model profiles** — contexto/thinking/temperatura por modelo
  (hoy tiers RAM estáticos).
- [ ] **P2 — Plan-mode con sub-coders** — solo si el pilot muestra que PLAN con
  tools directas (`trace_component`, `inspect_*`) no alcanza.
- [ ] **Por release — Terminal-Bench 2.0** (leaderboard oficial).
- [ ] **Cuando 12B >40% Polyglot — SWE-bench Lite/Verified**.

Descartado: shell-write guard (sin tool bash genérica no hay bypass).
