# Polyglot-225 — primeras 25 (10%)

Adapter del benchmark estándar Aider Polyglot para AgentDevs. Dataset:
Aider-AI/polyglot-benchmark (25 ejercicios estratificados 5×5 en
python/javascript/go/java/cpp; rust excluido sin toolchain).

## Pins (ablación post-P1/Fase 2)

- Modelo `spark2.5-4B` local, temperature 0.2, max_tokens 2048, 1 trial.
- Criterio: suite del ejercicio en verde (comando del stack). Sin judge LLM.
- Toolchain pins (macOS bench box): `pytest` del PATH (8.3.2), node 26 + jest
  por ejercicio (`npm install` en setup), go toolchain, java `./gradlew test`,
  Catch2 3.16 via brew para cpp. `python3 -m pytest` NO vale (el python3 del
  sistema no trae pytest; el primer intento PY01 falló por esto, se reinició).
- Setup commiteado antes de la sesión (el fix `_commit_during_turn` lo
  distingue del trabajo del turno).

## Uso

- `python benchmarks/polyglot-225/run_225.py --list`
- `python benchmarks/polyglot-225/run_225.py PY01`
- `nohup python benchmarks/polyglot-225/run_225.py --all > benchmarks/polyglot-225/results/run_all.log 2>&1 &`

## Resultados

- `results/<id>/record.json` + `verify.txt` + `response.txt`
- `results/summary.jsonl`, `REPORTE.md` al cerrar.
