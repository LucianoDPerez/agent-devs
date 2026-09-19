# REPORTE — Polyglot-pilot 12 (baseline Fase 1)

Fecha: 2026-09-19. Harness: Fase 1 (read-before-edit + PLAN exploratorio).
Modelo: `spark2.5-4B` local (llama.cpp :8080, n_ctx 26624), temperature 0.2,
max_tokens 2048, 1 trial/tarea, timeout 900s. Criterio: `pytest tests/` exit 0.

## Resultado: 11/12 (91.7%) → 12/12 tras fix P01

Baseline (harness Fase 1 sin fix): 11/12. El único FAIL (P01) era bug del
harness (`_nothing_pending_to_write()` confundía el setup-commit con trabajo
hecho). Tras el fix (`_commit_during_turn()`), re-run P01: ✅ PASS en 335.8s
(tools: read_file → write_file → run_lint/run_tests). Estado actual: **12/12**.

| id | tarea | veredicto | secs | tools |
|---|---|---|---|---|
| P01 | slugify | ❌ FAIL | 240 | read_file |
| P02 | fizzbuzz | ✅ | 517 | git_status, read_file, run_lint, run_npm_script, run_tests, run_verify, search_code, write_file |
| P03 | wordcount | ✅ | 711 | inspect_routes, read_file, run_build, run_lint, run_npm_script, run_tests, search_code, write_file |
| P04 | bsearch | ✅ | 294 | read_file, run_npm_script, run_tests, write_file |
| P05 | dedup | ✅ | 350 | (ver record.json) |
| P06 | flatten | ✅ | 428 | (ver record.json) |
| P07 | groupby | ✅ | 390 | (ver record.json) |
| P08 | csvsum | ✅ | 658 | (ver record.json) |
| P09 | counter | ✅ | 409 | (ver record.json) |
| P10 | chunks | ✅ | 511 | (ver record.json) |
| P11 | topwords | ✅ | 493 | (ver record.json) |
| P12 | merge_sorted | ✅ | 555 | read_file, run_tests, run_verify, search_code, write_file |

Tiempo total: ~92 min. Detalle por tarea en `results/<id>/` (record.json, verify.txt,
response.txt, diffstat.txt); agregado en `results/summary.jsonl`.

## Lectura honesta

- El invariante read-before-edit **no bloqueó ningún edit legítimo**: los 11 pass
  leyeron → escribieron (archivo nuevo) → verificaron.
- P02/P03 exploran de más (`run_npm_script`, `inspect_routes` en tareas stdlib):
  convergencia con sobrecosto, posible P1 (tool-gating / dynamic intervention).
- El único FAIL (P01) es bug del harness, no del modelo: tras corte por
  `EXECUTE_MAX_REASONING_SECONDS` (150s pensando sin output), el retry cerró con
  “Nada pendiente por escribir” (`session.py:3951`, `_nothing_pending_to_write()`
  True en tarea de crear-archivo). Entra como P0 con esta evidencia.

## Alcance

Baseline propio (12 ejercicios fáciles, 1 trial, juez = pytest). No comparable con
Polyglot-225. Siguiente: Polyglot-225 completo (dataset exercism) y ablaciones.
