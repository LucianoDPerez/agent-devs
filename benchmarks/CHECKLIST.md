# Checklist de mejoras (priorizado, 2026-09-19)

- [x] **Fase 1** — Read-before-Edit + Write-guard + PLAN exploratorio. Verificado
  (694 tests, e2e Spark 4B). Ver `FASE1.md`.
- [x] **P0 — Polyglot-pilot (12)** — baseline 11/12 con Spark 4B.
  Runner: `polyglot-pilot/run_pilot.py`. Criterio objetivo (pytest), pins fijos.
- [x] **P0 — Fix cierre prematuro en crear-archivo** — `_commit_during_turn()`:
  el commit cuenta solo si es posterior al inicio del turno (pilot P01: el setup
  'init tests' engañaba a `_nothing_pending_to_write()`). Re-run P01 e2e en curso.
- [x] **Fase 2 (paquete "SLM torpe")** — output-parser (```tool, tags,
  JSON pelado, repair), thinking-budget por bloque (EXECUTE 12000/resto 6000
  chars, retry ya corría sin thinking), AGENTS.md (tope 4000, todos los
  roles). Verificado: suite 711, e2e AGENTS.md (hints=True), P04 PASS.
  Ver `FASE2.md`.
- [x] **P1 — Dynamic intervention + evidence journal** — hints ante fallo
  (escala al 2º sobre mismo path) + journal SQLite inyectado en retry/summary,
  cero tools nuevas. Verificado: suite 722, e2e journal 3/3 en SQLite.
  Ver `P1.md`.
- [x] **Foco primer fallo** — trampa del primer FAILED por tool canónica +
  bloque "arreglá SOLO esto" en el gate-retry. Suite 727. E2E PY02: el gate
  no disparó (el modelo verificaba por su cuenta), 3/8 tests (antes 0/8),
  sigue FAIL — foco sin efecto en vivo, queda unit-verificado.
- [x] **P2 — Per-model profiles** — `core/model_profiles.py` (spark/qwen/
  gemma/default): thinking por bloque + temp EXECUTE por request según el
  SLM detectado (/v1/models). Adaptación, no duplicación del server.
  Verificado: suite 738, profile spark en vivo.
- [ ] **P2 — Plan-mode con sub-coders** — solo si PLAN con tools directas
  no alcanza.
- [ ] **Polyglot-225 completo** — 200 restantes (con 35B, no con 4B).
- [ ] **Por release — Terminal-Bench 2.0** (leaderboard oficial).
- [ ] **Cuando 12B >40% Polyglot — SWE-bench Lite/Verified**.

Descartado: shell-write guard (sin tool bash genérica no hay bypass).
