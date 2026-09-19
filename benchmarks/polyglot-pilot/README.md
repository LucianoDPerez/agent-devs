# Polyglot-pilot (12 ejercicios Python)

Baseline apples-to-apples de AgentDevs con SLM local. Inspiración: Aider Polyglot
(225 ejercicios); aquí 12 propios, acotados y 100% objetivos, para medir en horas
en vez de días con un 4B.

## Pins de determinismo (ablación Fase 1)

- Modelo: `spark2.5-4B` (llama.cpp `:8080`, n_ctx server 26624)
- temperature 0.2, max_tokens 2048, 1 trial por tarea, timeout 900s/tarea
- Harness: estado Fase 1 (read-before-edit + PLAN exploratorio, sin más cambios)

## Criterio

`python -m pytest tests/ -q` en el repo de la tarea → exit 0 = PASS. Sin judge LLM.
El runner pre-crea y commitea los tests; el agente implementa el módulo. Runner solo
lee y verifica.

## Uso

- `python benchmarks/polyglot-pilot/run_pilot.py --list`
- `python benchmarks/polyglot-pilot/run_pilot.py P01`
- `nohup python benchmarks/polyglot-pilot/run_pilot.py --all > benchmarks/polyglot-pilot/results/run_all.log 2>&1 &`

## Resultados

- `results/<id>/record.json` (pass, secs, tools), `verify.txt`, `response.txt`, `diffstat.txt`
- `results/summary.jsonl` (1 línea por tarea)
- `REPORTE.md` (se escribe al cerrar el pilot)
