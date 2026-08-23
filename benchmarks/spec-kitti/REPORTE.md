# Benchmark Spec-Kitti — Reporte Final

**Fecha**: 2026-08-17/18 · **Repositorio objetivo**: `~/demo/demo-spec-kitti` (Spec-Kitti CLI Python + binario Go de telemetría New Relic) · **Rama**: `benchmark/spec-kitti-telemetry-v1` (creada desde main, SIN push) · **LLM**: Qwen3.6-35B-A3B (config optimizada: t4, fa auto, q8_0, poll 0) · **Tareas**: tomadas TAL CUAL del RTF del usuario (`~/Desktop/Spec-kitti-TASKS.rtf`)

## Resultado: 4/9 cierres autónomos · 7/9 con verificación externa OK

> **Nota de recomputación (harness v2)**: los `summary.jsonl` y este reporte fueron
> recomputados con la semántica corregida del harness (`_same_failure`): los criterios
> de AUSENCIA ("no tests found", "no such file", "no go files") nunca se heredan —
> son el entregable de la tarea —, los fallos sin señales comparables (tails vacíos)
> no se heredan, y TIMEOUT del turno = el agente no cerró su loop (no cuenta como
> lograda aunque la verificación externa pase).

| Tarea | Nivel | Descripción | Resultado | Notas |
|---|---|---|---|---|
| T1 | junior | Estructura del proyecto Go telemetry/ | ✅ | go.mod sin deps externas + skeleton compila |
| T2 | semi-senior | New Relic Event API client | ⚠️ parcial | Verify externo OK, pero el turno explotó el contexto (TIMEOUT) |
| T3 | semi-senior | Resolver de usuario desde git config | ✅ | fallback chain completa + tests |
| T4 | semi-senior | CLI principal start/end | ✅ | 310 líneas, UUID crypto/rand, session file |
| T5 | junior | Conteo de tokens del template | ⚠️ parcial | Verify externo OK; el agente no escribió el test obligatorio (agregado en vivo), TIMEOUT |
| T6 | senior | Build system cross-compile + ldflags | ✅ | Makefile con validación de vars requeridas |
| T7 | senior | Integrar binario en spec-kitti init | ❌ | 2 turnos, 0 writes — entregable ausente (ver análisis) |
| T8 | junior | Hooks de telemetría en 14 templates | ❌ | 6/14 templates + interfaz INVENTADA — el harness ahora lo detecta (criterio sin entregar) |
| T9 | senior | Testing end-to-end | ⚠️ parcial | Suite go + smoke verde, pero turno en TIMEOUT |

## ¿Las tareas del RTF son suficientes como están?

**Respuesta corta: para un dev humano, sí. Para un LLM local de 33k context, NO — 2 de 9 fallaron y de las 7 que pasaron la verificación externa, 3 no cerraron el turno (TIMEOUT) y las 4 restantes necesitaron ~30% de corrección en vivo.**

**Lo que funciona del RTF:**
- Estructura clara por task con Resumen/Descripción/Acceptance Criteria/Notas técnicas — excelente formato.
- Los AC son verificables (compila, test con httptest, countTokens>0, etc.).
- La referencia al repo watcher (`itti-watch-skills/watcher`) ayudó al agente a copiar patrones.

**Los 3 problemas del RTF como input de LLM:**

1. **Falta la METAINSTRUCCIÓN de flujo.** El RTF no dice "analizá → desglosá → una subtarea → verify → siguiente". El agente en T7 intentó leer `__init__.py` completo (1400 líneas) en 3 reads → explotó el contexto (40498 tokens > 35072) → turno muerto. En T8 divagó inventando una interfaz nueva (`python3 -m spec_kitti_cli telemetry`) que no existe.

2. **Tareas grandes sin punto de anclaje.** T7 dice "Modificar __init__.py en la función init" — la función init es enorme. Falta: la LÍNEA exacta o la sección, o "buscá con search_code 'def init(' y leé 40 líneas alrededor". T8 dice "modificar 14 archivos" sin dar el formato EXACTO de la línea a insertar (el agente inventó su propio formato).

3. **T8 es repetitiva pero difusa.** 14 archivos con la misma edición es ideal para un script, terrible para un LLM: el agente perdió la cuenta (6/14) y degeneró el formato. Debería decir "edité el PRIMERO, verificá, y replicá el MISMO diff en los otros 13".

**Mejoras mínimas al RTF (sin cambiar el contenido técnico):**
- Agregar a cada task: "FLUJO: analizá → desglosá en subtareas → UNA a la vez con `go vet` + `go test` (o pytest) después de CADA una."
- T7: "CRÍTICO: __init__.py es GRANDE. Buscá `def init(` con search_code y leé SOLO 40 líneas alrededor. NUNCA leas el archivo completo."
- T8: "La edición es IDÉNTICA en los 14 archivos: agregá al inicio `Run: \`.spec-kitti/bin/spec-kitti-telemetry start <command-name> --template .cursor/commands/<command-name>.md\`` y al final `Run: \`.spec-kitti/bin/spec-kitti-telemetry end <command-name>\``. Hacé el PRIMERO, verificá el formato, y replicá EXACTAMENTE ese diff en los otros 13."

## Correcciones en vivo necesarias (por tarea)

| Tarea | Corrección |
|---|---|
| T3 | Falta import `regexp` → luego el agente lo cambió a `strings.Index`; su test esperaba helper `extractUsernameFromEmail` que el resolver no exponía (agregado) + import sin usar |
| T5 | El agente no escribió el test unitario obligatorio (agregado main_test.go) |
| T7 | 0 writes en 2 turnos — fallo total por contexto (ver arriba) |
| T8 | Interfaz inventada + solo 6/14 archivos |

## Hallazgos del harness (nuevos en este benchmark)

1. **Criterios de verify por "existencia de cambios" son débiles** (T7: "tests pasan" dio ok sin implementación). Los criterios deben verificar el ARTEFACTO real (bin/ existe, grep del código, etc.), no solo que los tests sigan verdes.
2. **`_same_failure` es demasiado amplia** para criterios no-build: marcó "heredado" un fallo real del agente (T8) porque baseline y verify tenían el mismo exit=1 sin 'build failed'. Refinar.
3. **El flujo baseline + prompt de subtareas FUNCIONÓ**: T2-T6 convergieron mejor que el benchmark anterior; el patrón "UNA subtarea + verify" mantiene el contexto chico.

## Recomendaciones para la próxima iteración del RTF

1. **Un task = una decisión técnica.** T7 mezcla "detectar SO/arch + copiar binario + chmod + warning" — dividir en: 7a (helper detect_platform + tests), 7b (función copy_telemetry_binary + tests), 7c (integrar la llamada en init con sección citada).
2. **Dar el anclaje de código**: `def init(` está en la línea ~1066 de `__init__.py` — citarlo en el task.
3. **T8 como 2 subtareas**: 8a (formato exacto en 1 archivo + verify), 8b (replicar en los 13 restantes).
4. **El criterio de éxito en el prompt**: "el resultado de go test debe ser ok, no solo que compile".
5. **Mantener la referencia a watcher** — fue de lo más útil del RTF.

## Estado del repo tras el benchmark

Rama `benchmark/spec-kitti-telemetry-v1` (sin push):
- `telemetry/`: módulo Go completo (go.mod, cmd/spec-kitti-telemetry/main.go 310 líneas, internal/nr con client+test, internal/gitconfig con resolver+test, Makefile cross-compile con ldflags)
- Suite Go completa verde + smoke start/end OK
- 6/14 templates de commands con hooks (formato incorrecto — el agente inventó interfaz)
- Sin cambios en src/spec_kitti_cli/__init__.py (T7 falló)
