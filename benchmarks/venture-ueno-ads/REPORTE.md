# Reporte — banco venture-ueno-ads (ueno-ads)

Modelos evaluados: `qwen3.6-35b-a3b`

## Tabla comparativa

| tarea | rol | modelo | veredicto | tools | retries | tokens in/out | tiempo |
|---|---|---|---|---|---|---|---|
| SL1-endpoints | analyze | `qwen3.6-35b-a3b` | n/a | 1 | 0 | 7020/1261 | 77.2s |
| SL2-modelos | analyze | `qwen3.6-35b-a3b` | n/a | 9 | 2 | 12527/6814 | 499.3s |
| SL3-env | analyze | `qwen3.6-35b-a3b` | n/a | 2 | 0 | 9565/2837 | 196.5s |
| SL4-debug500 | analyze | `qwen3.6-35b-a3b` | n/a | 4 | 0 | 11946/1547 | 141.3s |
| SL5-plan-softdelete | plan | `qwen3.6-35b-a3b` | n/a | 31 | 1 | 6552/2871 | 370.0s |
| SL6-healthcheck | execute | `qwen3.6-35b-a3b` | ✅ | 31 | 5 | 9596/913 | 1943.0s |
| SL7-slugify | execute | `qwen3.6-35b-a3b` | ✅ | 11 | 0 | 15379/2316 | 2400.0s |

## Totales por modelo

> **Lectura honesta del éxito**: las tareas se separan por tipo. Análisis/Plan
> (SL1-SL5) no tienen criterio automático: se cuentan como completadas si el
> turno terminó con respuesta (exit 0, sin timeout/error). Ejecución (SL6-SL7)
> tiene criterio OBJETIVO (el archivo existe y valida). La columna 'completadas'
> suma ambas categorías.

| modelo | análisis | ejecución | completadas | tool calls Σ | retries Σ | tokens out Σ | tiempo Σ |
|---|---|---|---|---|---|---|---|
| `qwen3.6-35b-a3b` | 5/5 | 2/2 | 7/7 | 89 | 8 | 18,559 | 93.8 min |
