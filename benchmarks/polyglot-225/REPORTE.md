# REPORTE — Polyglot-225 primeras 25 (baseline Spark 4B)

Fecha: 2026-09-20. Harness: post-P1 (`b68e006` + Fase 2 `a0dc2ba`).
Modelo: `spark2.5-4B` local, temperature 0.2, 1 trial. Criterio: suite del
ejercicio en verde. Dataset: Aider-AI/polyglot-benchmark, 5×5
(python/javascript/go/java/cpp).

## Resultado: 2/25 (8%)

| lang | pass | detalle |
|---|---|---|
| python | 0/5 | PY01 affine (lógica agrupado/dígitos mal), PY02 beer-song (bordes), PY03 book-store (19 fail, falta optimización descuentos), PY04/PY05 fail |
| javascript | 1/5 | JS05 book-store PASS con **1 test activo y 16 en skip** (xtest) — pass débil, se declara |
| go | 0/5 | GO03 45 min: alucina helper `recursiveMax` inexistente, no se corrige |
| java | 0/5 | todos FAIL (lógica) |
| cpp | 1/5 | CP02 allergies PASS (flujo edit+build+verify sano) |

Tiempo medio ~15 min/tarea (rango 5-45 min). Total ~7 hs. Detalle en
`results/<id>/` (record.json, verify.txt, response.txt).

## Lectura

1. **Los fallos son de capacidad del modelo, no bloqueos del harness.**
   Guards se comportaron (write completo bloqueado → edit quirúrgico que
   pasaba; verify corrió en todos). Ningún FAIL por read-before-edit falso,
   cierre prematuro o crash: 25/25 turnos cerraron limpio.
2. **El 4B no sostiene este nivel.** Cripto modular, agrupamiento óptimo y
   strings exactos multi-caso exceden su razonamiento; alucina helpers y
   quema 20-45 min por tarea. Proyección 225 completa: 30-60 hs.
3. **Comparabilidad:** little-coder reporta 45.56% en Polyglot-225 con
   Qwen3.5-9B (y Aider vainilla 19% mismo modelo). Nuestro 8% es con un 4B
   en un subset de 25 — no es apples-to-apples. La pregunta producto
   ("¿el scaffold rinde?") exige correr ESTAS 25 con un 9B/12B.

## Decisión propuesta

Congelar este 8% como baseline Spark-4B y repetir las mismas 25 con
Qwen3.5-9B o Gemma-12B (mismo runner, mismos pins). Ahí sí comparamos
contra 45.56%/19%.
