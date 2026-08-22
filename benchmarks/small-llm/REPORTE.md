# Reporte — banco small-llm (Medicos)

Modelos evaluados: `agents-a1-4b`, `qwen3.5-9b`

## Tabla comparativa

| tarea | rol | modelo | veredicto | tools | retries | tokens in/out | tiempo |
|---|---|---|---|---|---|---|---|
| SL1-endpoints | analyze | `agents-a1-4b` | n/a | 1 | 0 | 7545/1691 | 363.1s |
| SL1-endpoints | analyze | `qwen3.5-9b` | n/a | 1 | 0 | 7486/365 | 85.7s |
| SL2-modelos | analyze | `agents-a1-4b` | n/a | 2 | 0 | 7796/1067 | 262.0s |
| SL2-modelos | analyze | `qwen3.5-9b` | n/a | 2 | 0 | 7729/610 | 141.3s |
| SL3-env | analyze | `agents-a1-4b` | n/a | 1 | 0 | 7262/587 | 156.2s |
| SL3-env | analyze | `qwen3.5-9b` | n/a | 7 | 0 | 9203/976 | 253.4s |
| SL4-debug500 | analyze | `agents-a1-4b` | n/a | 6 | 2 | 10657/9255 | 1504.7s |
| SL4-debug500 | analyze | `qwen3.5-9b` | n/a | 5 | 1 | 15804/1723 | 668.7s |
| SL5-plan-softdelete | plan | `agents-a1-4b` | n/a | 0 | 2 | 4435/3881 | 763.5s |
| SL5-plan-softdelete | plan | `qwen3.5-9b` | n/a | 0 | 0 | 16402/2312 | 983.5s |
| SL6-healthcheck | execute | `agents-a1-4b` | ✅ | 5 | 0 | 7527/1009 | 393.4s |
| SL6-healthcheck | execute | `qwen3.5-9b` | ✅ | 12 | 1 | —/— | 2400.0s |
| SL7-slugify | execute | `agents-a1-4b` | ✅ | 4 | 0 | 7929/1150 | 581.4s |
| SL7-slugify | execute | `qwen3.5-9b` | ✅ | 3 | 0 | 8057/4865 | 2400.0s |

## Totales por modelo

| modelo | casos | éxito verify | tool calls Σ | retries Σ | tokens out Σ | tiempo Σ |
|---|---|---|---|---|---|---|
| `agents-a1-4b` | 7 | 2/7 | 19 | 4 | 18,640 | 67.1 min |
| `qwen3.5-9b` | 7 | 2/7 | 30 | 2 | 10,851 | 115.5 min |

## Hallazgos

1. **EXECUTE es donde la arquitectura brilla con modelos chicos.** El 4B completó
   ambas tareas de escritura con verificación ✅ en 393s y 581s. El 9B también
   produjo los artefactos correctos (verify ✅ en ambos) pero excedió el cap de
   2400s: su lentitud por-token en hardware local convierte "correcto" en
   "fuera de tiempo".

2. **Análisis simple (SL1-SL3): ambos resuelven, perfiles opuestos.** Qwen3.5-9B
   es conciso (365 out tokens vs 1691 del 4B en SL1) y más rápido en SL1/SL2;
   el 4B es verboso. En SL3-env el 9B usó 7 tools vs 1 del 4B.

3. **Debug senior (SL4): ninguno converge limpio sin ayuda.** 4B: 2 retries y
   9255 tokens-out de espirales; 9B: 1 retry. La causa raíz (route GET / sin
   pasar query param al service que YA soporta búsqueda) fue alcanzada por
   ambos eventualmente, pero con costos desproporcionados.

4. **PLAN: cero tool calls en AMBOS modelos.** Planearon exclusivamente desde
   el análisis cacheado. Posible optimización pendiente: el prompt de PLAN
   debería empujar exploración dirigida cuando el plan toca archivos no
   cubiertos por el análisis cacheado.

5. **Tokens: el 4B gastó MÁS output total** (18.6k vs 10.8k) pese a ser 4×
   más chico — la verbosidad + retries se comen la ventaja teórica. El input
   por caso es comparable (~7-16k).

6. **Tiempo total**: 4B 67 min vs 9B 115 min para el mismo banco (~2x), consistente
   con el throughput esperado por tamaño en el mismo hardware.

## Caveats metodológicos

- n=7 casos; diferencias <15% no son concluyentes.
- Análisis canónico compartido (600 chars — el generado por Qwen3.5-9B tras el
  fix de timeout). Ambos modelos corrieron con EL MISMO contexto inicial.
- SL4 y SL5 corrieron sobre código de routing distinto entre pasadas (fixes de
  router aterrizados entre corridas) — las rutas finales coinciden.
- Los EXECUTE del 9B quedaron registrados como TIMEOUT(2400s) aunque sus
  criterios verificaron ✅ después del corte: el trabajo estaba hecho, el turno
  no cerró a tiempo.
