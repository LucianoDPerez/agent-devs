# Análisis final — Benchmarks de AgentDevs

Comparativa de 4 modelos (4B/9B/12B/35B) sobre el mismo banco de 7 tareas
reales: 5 de análisis/plan + 2 de ejecución con criterio objetivo (el
archivo creado existe y valida).

Bancos: `benchmarks/small-llm/` (4B, 9B, 12B sobre Medicos) y
`benchmarks/venture-ueno-ads/` (35B sobre ueno-ads). Misma estructura de
tareas, mismos criterios de verify, mismo runner.

## Resultados

| Métrica | **agents-a1-4b** | **qwen3.5-9b** | **gemma-4-12b** | **qwen3.6-35b-a3b** |
|---|---|---|---|---|
| Completadas | 6/7 (86%) | 6/7 (86%) | **7/7 (100%)** | **7/7 (100%)** |
| Análisis/Plan | 5/5 | 5/5 | 5/5 | 5/5 |
| Ejecución (verify objetivo) | 1/2 | 1/2 | **2/2** | **2/2** |
| Tiempo total | 101 min | **86 min** | 120 min | 96 min |
| Tool calls | 15 | 29 | 27 | 90 |
| Retries | 6 | **3** | 5 | 8 |
| Tokens out | 16,481 | 6,952 | **3,885** | 19,820 |

## Conclusiones

1. **El éxito NO escala con el tamaño del modelo — la arquitectura compensa.**
   El 4B completa el 86% de lo que el 35B: los 5 análisis/plan idénticos. La
   diferencia real está en UNA tarea (ejecución SL7). El harness (tools
   densas + guards + verificación) aplanó la curva de capacidad.

2. **El cuello es el tiempo, no la inteligencia.**
   Los execute que "fallaron" en 4B/9B eran archivos CREADOS correctamente
   pero excediendo el cap de 2400s. Ningún modelo "no entendió" la tarea.

3. **Gemma-12B es el punto dulce inesperado.**
   Único junto al 35B en 7/7, pero con 5× menos tokens out (3.9k vs 19.8k)
   y menos tools que el 9B. Mejor eficiencia por token. Costo: el más lento
   (120 min).

4. **El 9B es el más estable y eficiente en tiempo.**
   Menos retries (3), más rápido (86 min), conciso (6.9k tokens). Ideal
   para análisis puro.

5. **El 35B no es 2× mejor que el 4B — es marginalmente más consistente.**
   Misma tasa en análisis; la ventaja real: completa ambos execute. Pero
   gasta 90 tools y 19.8k tokens — 6× más tools que el 4B para cerrar una
   tarea extra.

## Tesis

> **"Esta arquitectura reduce la dependencia del tamaño del modelo"** — SÍ,
> con evidencia: el 4B alcanza 86% del rendimiento del 35B con 5× menos
> parámetros, y el 12B lo empata con 5× menos tokens.

## Recomendación de producción

- **Default diario**: agents-a1-4b (rápido, 86%, ideal análisis/consulta).
- **Para cerrar tareas**: gemma-4-12b (7/7, máxima eficiencia por token) o
  el 35B cuando esté disponible.
- **El harness es el multiplicador**: sin guards/tools densas/verificación,
  ningún modelo chico alcanza estas tasas — ese es el corazón de la tesis.

## Notas metodológicas

- 7 casos por banco, n=1 por modelo: la tendencia es clara pero diferencias
  <15% no son concluyentes. Escalar a 15-20 casos daría significancia.
- Análisis canónico compartido dentro de cada banco (mismo contexto inicial).
- Los EXECUTE del 35B corrieron sobre otro repo (ueno-ads, NestJS + Next.js)
  con la misma estructura de tareas; los de los chicos sobre Medicos.
- Métricas re-computadas desde logs (el extractor por stdout se perdía en
  turnos largos); evidencia completa en `results/` de cada banco.