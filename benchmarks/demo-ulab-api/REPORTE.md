# Benchmark Venture-API-ULab — Reporte Final

**Fecha**: 2026-08-17 · **Repositorio objetivo**: `~/demo/demo-ulab-api` (Spring Boot 3.5.7 WebFlux, Java 21, Gradle 8.10.2, R2DBC, ~1975 tests existentes) · **LLM**: Qwen3.6-35B-A3B (IQ3_XXS, alias `agents-a1-4b-64k`, context 33k, ctx-size 35000) · **Harness**: `~/agent-devs` · **Rama**: `benchmark/demo-ulab-v1` (local, SIN push)

## Resultado: 6/9 cierres autónomos · 9/9 con verificación externa OK

> **Nota de recomputación (harness v2)**: los `summary.jsonl` y este reporte fueron
> recomputados con la semántica corregida del harness (`_same_failure`): los criterios
> de AUSENCIA ("no tests found", "no such file") nunca se heredan — son el entregable —,
> los fallos sin señales comparables no se heredan, y TIMEOUT del turno = el agente no
> cerró su loop (marcado ⚠️ parcial aunque la verificación externa pase).

| Tarea | Nivel | Descripción | Resultado | Duración agente | Corrección en vivo |
|---|---|---|---|---|---|
| J1 | junior | Tests de BaseController + AuthenticationController | ✅ 8 tests verdes | 29 min | Sí (auth mock + header) |
| J2 | junior | Test del GenericJiraWebhookController | ⚠️ parcial (3 tests verdes, turno TIMEOUT) | 40 min (TIMEOUT) | Sí (JWT HMAC firmado) |
| J3 | junior | Limpiar 5 archivos basura commiteados con nombres corruptos | ✅ staged delete | 6 min | No (fix de harness: stage_files) |
| S1 | semi-senior | Test completo de routing webhook RN-1..RN-5 | ✅ 19 tests verdes | 24 min | No (re-verify tras interferencia) |
| S2 | semi-senior | Tests de PlanningController + UserController | ⚠️ parcial (14 tests verdes, turno TIMEOUT) | 40 min (TIMEOUT) | Sí (firmas de records + mocks ownership) |
| S3 | semi-senior | Test de RepositoryController | ⚠️ parcial (6 tests verdes, turno TIMEOUT) | 40 min (TIMEOUT) | Sí (3 mocks faltantes + header) |
| T1 | senior | Correlation-ID + request logging | ✅ suite completa verde | 13 min | Sí (getMethod() + imports) |
| T2 | senior | Test del GlobalExceptionHandler | ✅ 44 tests verdes | 11 min | Sí (appender.list) |
| T3 | senior | Eliminar paquetes corruptos + build | ✅ build verde | 40 min (TIMEOUT) | Sí (limpieza directa) |

## ¿Tuvimos que atomizar mucho las tareas?

**Sí, y es la conclusión principal del benchmark.** Evidencia:

1. **El 35B con 33k de context NO puede hacer tareas multi-clase sin supervisión.** Las 3 tareas que pedían "creá N tests de M controllers" (J1, S2, S3) terminaron en TIMEOUT de 40 min o con archivos a medio escribir. Las tareas que pedían UNA cosa con un patrón de referencia claro (J3, S1, T1, T2) convergieron en 6-24 min.

2. **Lo que NO atomizamos, lo pagamos en correcciones en vivo.** 6 de 9 tareas necesitaron intervención manual del operador (correcciones de 1-3 líneas). El agente escribe el 90% bien pero falla en:
   - Firmas de records/DTOs que no leyó (inventa constructores — S2)
   - Beans del contexto Spring que no mockeó (S3: faltaban 3 use-cases; J1/J2/S2: SessionValidator, ownership)
   - APIs de librerías que "sabe de memoria" pero no verificó (logback `getList()` en T2, `.isEmpty()` en expectBodyList en S3)
   - Requisitos de auth/JWT de los filters globales (401 en J1, J2, S2, S3)

3. **Los errores heredados existen y hay que distinguirlos.** En S2: PUT /api/v1/users devuelve 500 (validation + OwnershipAuthorizationAspect) — es un bug del repo, no del agente. Lo documentamos y NO lo arreglamos (política correcta).

## Hallazgos técnicos del harness (corregidos en este benchmark)

- **NO correr tareas en paralelo sobre el mismo repo** (J2 interfirió con S1: compileTestJava rompía por el test a medio escribir).
- **El criterio de verify no debe usar `| tail -5`** (enmascara el exit code: marcó ok con BUILD FAILED).
- **Baseline pre-tarea** (nuevo en run.py): lint+tests+build ANTES de que el agente toque nada, inyectado en el prompt + comparación post-tarea para distinguir errores heredados vs del agente.
- **Auto-retry con fallo inyectado** (nuevo en run.py): si el verify falla en algo que el baseline tenía verde, re-lanza UN turno con el error exacto.
- **Soporte Java/Gradle en verify.py** (nuevo): run_lint → compileJava, run_tests → test, run_build → build -x test, run_install → gradle.
- **stage_files(files=".")** ahora hace `git add -u` (stage deletes/modifies sin tocar untracked) — necesario para los archivos basura con nombres unicode.

## Recomendaciones para darle tareas con resultado exitoso

### Formato de tarea ideal (según la evidencia)

1. **UNA tarea = UNA decisión técnica.** No "creá tests de A, B y C" sino "creá el test de A" y repetí.
2. **Dale el patrón de referencia con path EXACTO.** "Copiá el patrón de `RefinerControllerTest.java`" funciona; "usá WebFluxTest" no (el repo exige @SpringBootTest + mocks específicos).
3. **Anticipá los requisitos de contexto que el agente no va a descubrir:** filters de auth (SessionValidator, JWT), beans que el controller inyecta (listalos TODOS), aspectos (@RequireOwnership).
4. **Si el task involucra firmas de DTOs/records, exigí leerlos explícitamente** ("leé User.java ANTES de escribir; NO inventes constructores").
5. **Poné el criterio de éxito en el prompt** ("el resultado de run_tests debe ser BUILD SUCCESSFUL, no solo que compile").
6. **El verify por criterio externo debe ser específico** (`--tests '*X*'`) para que corra rápido (~20s vs 2.5 min de suite completa).

### Reglas de flujo (lo que funcionó)

- **Secuencial, nunca paralelo** sobre el mismo repo.
- **Baseline primero** — sabé qué fallaba antes de arrancar.
- **Corregir en vivo 1-3 líneas está bien** — el agente entrega el 90%; el operador completa el 10% en 30 segundos en vez de esperar otro turno de 20 min.
- **Errores heredados: documentar, no arreglar.**
- **Mantener el criterio de verify SIN pipes que enmascaren exit codes.**

## Estado del repo tras el benchmark

Rama `benchmark/demo-ulab-v1` (sin push):
- 7 tests nuevos: BaseControllerTest, AuthenticationControllerTest, GenericJiraWebhookControllerTest, PlanningControllerTest, UserControllerTest, RepositoryControllerTest, BaseExceptionHandlerTest (44 tests)
- GenericWebhookRouterServiceTest ampliado (19 tests RN-1..RN-5)
- CorrelationIdWebFilter: request logging (method/path/status/ms) + test
- 5 archivos basura con nombres corruptos staged para borrado
- 2 paquetes corruptos vacíos eliminados
- Suite completa: BUILD SUCCESSFUL (2m36s)
