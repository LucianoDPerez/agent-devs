# Reporte — banco small-llm (Medicos)

Modelos evaluados: `agents-a1-4b`, `gemma-4-12b`, `qwen3.5-9b`

## Tabla comparativa

| tarea | rol | modelo | veredicto | tools | retries | tokens in/out | tiempo |
|---|---|---|---|---|---|---|---|
| SL1-endpoints | analyze | `agents-a1-4b` | n/a | 1 | 0 | 7545/1691 | 363.1s |
| SL1-endpoints | analyze | `gemma-4-12b` | n/a | 1 | 0 | 7055/367 | 365.3s |
| SL1-endpoints | analyze | `qwen3.5-9b` | n/a | 1 | 0 | 7486/365 | 85.7s |
| SL2-modelos | analyze | `agents-a1-4b` | n/a | 2 | 0 | 7796/1067 | 262.0s |
| SL2-modelos | analyze | `gemma-4-12b` | n/a | 1 | 0 | 6670/384 | 450.7s |
| SL2-modelos | analyze | `qwen3.5-9b` | n/a | 2 | 0 | 7729/610 | 141.3s |
| SL3-env | analyze | `agents-a1-4b` | n/a | 1 | 0 | 7262/587 | 156.2s |
| SL3-env | analyze | `gemma-4-12b` | n/a | 1 | 0 | 6699/372 | 431.3s |
| SL3-env | analyze | `qwen3.5-9b` | n/a | 7 | 0 | 9203/976 | 253.4s |
| SL4-debug500 | analyze | `agents-a1-4b` | n/a | 6 | 2 | 10657/9255 | 1504.7s |
| SL4-debug500 | analyze | `gemma-4-12b` | n/a | 6 | 0 | 14763/1046 | 1686.9s |
| SL4-debug500 | analyze | `qwen3.5-9b` | n/a | 5 | 1 | 15804/1723 | 668.7s |
| SL5-plan-softdelete | plan | `agents-a1-4b` | n/a | 0 | 2 | 4435/3881 | 763.5s |
| SL5-plan-softdelete | plan | `gemma-4-12b` | n/a | 0 | 2 | 3785/1089 | 1259.1s |
| SL5-plan-softdelete | plan | `qwen3.5-9b` | n/a | 0 | 0 | 16402/2312 | 983.5s |
| SL6-healthcheck | execute | `agents-a1-4b` | ✅ | 0 | 0 | —/— | 1500.0s |
| SL6-healthcheck | execute | `gemma-4-12b` | ✅ | 3 | 2 | 6559/627 | 1500.0s |
| SL6-healthcheck | execute | `qwen3.5-9b` | ✅ | 6 | 0 | —/— | 1500.0s |
| SL7-slugify | execute | `agents-a1-4b` | ❌ | 5 | 2 | —/— | 1500.0s |
| SL7-slugify | execute | `gemma-4-12b` | ✅ | 15 | 1 | —/— | 1500.0s |
| SL7-slugify | execute | `qwen3.5-9b` | ❌ | 8 | 2 | 7217/966 | 1500.0s |

## Totales por modelo

| modelo | casos | éxito verify | tool calls Σ | retries Σ | tokens out Σ | tiempo Σ |
|---|---|---|---|---|---|---|
| `agents-a1-4b` | 7 | 1/7 | 15 | 6 | 16,481 | 100.8 min |
| `gemma-4-12b` | 7 | 2/7 | 27 | 5 | 3,885 | 119.9 min |
| `qwen3.5-9b` | 7 | 1/7 | 29 | 3 | 6,952 | 85.5 min |

## Lectura de los EXECUTE (evidencia honesta tras fixes)

Recuperación con runner blindado (ensure_ascii, limpieza de artefactos entre
modelos, logs por modelo). Con el cap -turn-timeout 1500s:

| modelo | SL6 healthcheck.ts | SL7 slugify.ts |
|---|---|---|
| agents-a1-4b | ✅ verify (creó archivo, TIMEOUT) | ❌ no creado |
| qwen3.5-9b | ✅ verify (creó, TIMEOUT) | ❌ no creado |
| gemma-4-12b | ✅ verify (creó, TIMEOUT) | ✅ verify (12 reads + write) |

Todos superaron los 1500s: el cuello es el TIEMPO en hardware local, no la
capacidad. Solo gemma resolvió ambas; 4B y 9B crearon SL6 pero no SL7 dentro
del cap.
