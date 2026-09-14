# eval-prod — Batería de producción sobre Medicos

Batería de 5 tests reales con `agents-a1-4b` (4B local, llama-server) sobre
`/Users/luchop/PROYECTOS IA/Medicos` (rama `eval/agent-battery`).
Regla: si un test falla por causa del harness, se corrige, se re-corre y el
ciclo queda registrado acá.

- Harness: agent-lucho `041b978` (+ fixes que surjan de la batería)
- Tareas y rúbricas: `tasks.json`
- Transcripts: `results/T1..T5/transcript.md` (+ `run.log` crudo)
- Backup DB (dev, 2 tablas): `baseline/db_dump.sql`

## T0 — Baseline (2026-09-13)

- `npm run build --prefix backend` (tsc): **PASS**
- `npm run build --prefix frontend` (tsc -b && vite build): **PASS**
- Scripts de raíz (`-w backend/frontend`): **ROTOS preexistente** — `package.json`
  sin campo `workspaces` (`No workspaces found`). El agente debe usar
  subproyectos (regla monorepo de `execute.md`).
- Tests/lint: **no existen** en el repo (sin runner, sin `.test.*`).
- Node del shell: roto (dylibs) → `brew reinstall llhttp simdutf` → v26.8.2.
- Docker: backend/frontend/db corriendo (db healthy).
- Dirty preexistente (usuario, no tocar): `M .gitignore`, `M AGENTS.md`.

## T1 — Comprensión (auditoría CRUD, sin cambios)

- Intento 1 (harness `041b978`): 674s, 7 tools, **0 read_file**. Quemó el budget
  en listados (`src` recursive+plano+subdirs) y el retry respondió sin leer.
  Veredicto honesto pero incompleto: sin archivo:línea, sin análisis.
  Transcript: `results/T1/transcript.md` (+ `run.log` crudo).
- Fix aplicado (`265bc13`): listados redundantes bloqueados SIN consumir budget
  + mensaje de `recursive=true` neutral + regla en `analyze.md`.
  Tests: `TestRedundantListing` (6) + ajuste de `test_explore_budget_allows_two_then_stops`.
- Intento 2 (harness `265bc13`): 652s. Mejor exploración (1 listado + grafo:
  snippet de PacienteController) pero el retry declaró "ningún archivo leído":
  `cm__get_code_snippet` no se cacheaba y los hechos filtraban claves `[..]`.
  Honesto (cero inventos, cero cambios) pero sin file:línea.
- Fix aplicado (`14fd12b`): snippets crudos al read_cache + hechos con todas
  las claves + label `SOURCE DEL GRAFO`. Tests: +2 en `test_analyze_anchor.py`.
- Intento 3 (harness `14fd12b`): 595s. Otra vez honesto sin file:línea
  (capas genéricas + "sin contenido del código fuente"). Mecanismo: el PASS1
  muere en breadth y el retry de 0 tools no puede convertir listados en
  lecturas.
- Fix aplicado (`23c5ccc`): retry de ANALYZE/PLAN de SOLO-LECTURA (conserva
  historial + agente solo con read_file + budget de reads acotado). Limpieza:
  flag `_no_explore_retry` muerto eliminado. Tests: +2 en
  `test_analyze_anchor.py` (retry conserva historial, solo read_file).
- Intento 4 (harness `23c5ccc`): 772s. El retry de solo-lectura funcionó
  mecánicamente (1 read_file, pero a un DIRECTORIO → error) y luego dos cortes
  de razonamiento de 180s dejaron la respuesta VACÍA: el cap pensado para
  EXECUTE mató la composición final del 4B.
- Fix aplicado (`4392019`): razonamiento paciente (`None`) en retry de
  solo-lectura (idle_timeout sigue cubriendo cuelgues) + aclaración
  archivos-vs-directorios en el mensaje.
- Intento 5 (harness `4392019`): 1543s. Leyó archivos reales (rutas) pero
  rumió 15k chars sin actuar hasta agotar output → respuesta VACÍA. El
  razonamiento paciente sin límite permite rumiar infinito.
- Fix aplicado (`e8dde6a`): retry en DOS etapas — 1ª solo-lectura, 2ª sin
  tools + ancla regenerada (calza en max_attempts=3). Tests: +1.
- Intento 6 (harness `e8dde6a`): 639s. Leyó 3 snippets + 4 reads fallidos
  (confundió la project-key del grafo con paths) y la 2ª vuelta declaró "sin
  acceso al código" TENIENDO el source en el ancla: historial gigante
  (lost-in-the-middle) + pregunta anidada "Reanalizá: Reanalizá: ...".
- Fix aplicado (`d7a1a30`): 2ª vuelta con contexto MÍNIMO (summaries +
  pregunta original desde `_turn_question` + ancla) y prohibición explícita
  de declarar falta de acceso.
- Intento 7 (harness `d7a1a30`): 1213s. PASS1 leyó `PacienteController.ts`
  (verificado: `read_file` devuelve 2496 chars reales) + 3 snippets. La 2ª
  vuelta declaró "no se pudo identificar el archivo fuente" y CONFABULÓ un
  `trace_component` negativo que nunca ocurrió en el turno. Falla de SÍNTESIS
  con contenido presente (no de lectura): el 4B no sintetiza arquitectura
  amplia ni distingue su propio historial.
- Veredicto: **FAIL** (7 intentos, 0 file:línea). Evolución del modo de falla:
  inventar paths (queja original, 35B) → negativa honesta (intentos 2-6) →
  negativa con justificación confabulada (intento 7). Cero cambios en disco
  en todos los intentos (bien). La pregunta amplia excede la capacidad del 4B.
  Los fixes quedan (todos con tests verdes) porque cada uno corrigió un
  mecanismo real y verificado.
- Pendiente T1b: validar E2E el caso ORIGINAL (tareas citadas en archivo) con
  pregunta acotada — ejerce preload_for_analyze + ruta REVIEW + evidence guard.

## T2 — Bug fixing duplicados por concurrencia

- Intento 1 (harness `d7a1a30`): 684s, rol PLAN. Leyó 1 hook de frontend vía
  grafo, nunca el backend (teniendo `read_file` + paths en el prompt), y cerró
  con un *plan para leer* ("intentar leer src/controllers/...") en vez de
  leer. Sin cambios en disco (bien).
- Fix aplicado (`38f76d2`): el retry exige próxima acción = tool call real,
  prohibido responder con planes futuros.
- Intento 2 (harness `38f76d2`): 1218s, rol PLAN. Leyó EXACTO lo necesario
  (`PacienteController.ts` 2531 chars, `CreatePaciente.ts` 745 chars,
  `PrismaPacienteRepository.ts` 2047 chars — verificados legibles) pero el
  veredicto declara "Archivo no verificado: PrismaPacienteRepository.ts" y
  pide leerlo. Falla de SÍNTESIS con evidencia presente (misma clase que
  T1-intento-7). La clase de diagnóstico es correcta (race sin unique
  constraint: el schema no tiene `unique` y `CreatePaciente` no es
  idempotente) pero sin raíz verificada ni fix. Sin cambios en disco.
- Veredicto: **FAIL** (intento 2). Patrón 4B confirmado: lee bien, sintetiza
  mal en preguntas amplias. Se sigue con T3 (EXECUTE con preload/scaffolding,
  donde el harness más ayuda) en vez de más tweaks de mensajes.

## T2 — Bug fixing (9B, intento 2)

- 666s, rol PLAN. Leyó schema + CreatePaciente + rutas + repositorio (todos
  legibles, 0 errores) pero declaró "no fueron leídos con contenido".
  Hipótesis: el encuadre "intento anterior" invalida las lecturas en la
  cabeza del modelo.
- Fix aplicado (`b87a83e` + este commit de test): ancla en PRESENTE
  ("EVIDENCIA VERIFICADA DE ESTE TURNO", "CUENTA como lectura válida").
- Intento 3 (9B): EN CURSO (`results/T2/run3.log`)
- Veredicto: —
- Evidencia: —

## T2 — Bug fixing (9B, intento 4, dos turnos)

- El prompt rutea PLAN ("Proponé...") y la rúbrica exige implementar: se corre
  en 2 turnos encadenados (investigar → implementar), como uso real.
- Fix previo (`validador backticks/rangos`): el intento 3 citó
  `` `CreatePaciente.ts:15` `` en archivo de 14 líneas sin que nada lo frene.
- EN CURSO (`results/T2/run4.log`)
- Veredicto: —
- Evidencia: —

## T2 — Bug fixing (9B, intento 4, dos turnos PLAN→EXECUTE)

- 1533s. Turno 1 investiga bien (reads exactos). Turno 2 implementa MECANISMO
  ERRÓNEO: `uuid` generado por request (no evita duplicados, cada request
  genera uno distinto) + doble `@id` en schema (Prisma inválido) + dep `uuid`
  sin instalar + entidad inconsistente. Build backend en ROJO (errores
  TS2322/TS2741, verificado a mano). Nunca corrió la verificación pedida.
  Revertido todo; build restaurado en verde.
- Veredicto: **FAIL** (fix incorrecto + red build + sin verificación).
  Transcript: `results/T2/transcript.md` (2 turnos) + `run4.log`.

## T3 — Feature desactivación múltiple (9B) — PASS (primer PASS de la batería)

- Turno 1 (ANALYZE): investiga los 5 casos de uso + repo + rutas con citas
  reales. Turno 2 (EXECUTE): implementa `DeactivatePacientes` (caso de uso con
  patrón existente) + `DeactivatePacientesDTO` + `IPacienteRepository.deactivate`
  + impl Prisma (`updateMany`, atómico) + método en controller + ruta
  `POST /deactivate` con middleware validate + campo `activo` en schema
  (válido por `prisma validate`) + extensión mínima de `validate.ts` para
  reglas array. Total ~29 min.
- El agente corrió `db:generate` (solo regenera cliente, NO migra) para dejar
  `tsc` en verde (exit 0 verificado a mano). DB intacta (sin columna `activo`,
  migración pendiente como se le pidió). Notable: NO tocó la DB pese a tener
  el servidor a mano — respetó la restricción.
- Imperfecciones registradas: una iteración intermedia degradó `updateMany` a
  loop N+1 (quedó el loop: funciona pero menos eficiente); tocó `validate.ts`
  compartido (aditivo, build verde); el codemod PATH FIX metió ruido en
  `api.ts` (revertido, es determinístico y ajeno a T3).
- Evidencia: `results/T3/transcript.md` + `transcript2.md` + `run2/run3.log`.
  Cambios vivos en la rama (feature realista pendiente de migración).


- Nota de ruteo: el prompt arranca con "Necesito agregar..." + "Analizá
  primero" → el router da ANALYZE (el verbo de análisis subordinado gana al
  de acción, por diseño). Correcto para el flujo encadenado del harness
  (analizar → "implementá"), así que T3 corre en DOS turnos como uso real.
- Intento 1 (1 turno, matado a los ~5min al detectar el ruteo): sin cambios.
- Intento 2 (2 turnos): EN CURSO (`results/T3/run1.log`)
- Veredicto: —
- Evidencia: —

## T4 — Seguridad auth (9B) — PASS

- 1051s, rol REVIEW, ~15 reads (middlewares, rutas, 3 controllers). Hallazgo
  central CORRECTO y de alto impacto: no existe authN/authZ (solo helmet/cors/
  rateLimit en `server.ts`), todo expuesto. Citas reales con líneas
  plausibles; detectó hasta el método `deactivate` agregado por T3 (leyó el
  working tree, no el caché). Sin FPs inventados (nada de "SQL injection"
  genérico). Warnings honestamente condicionales. Cero modificaciones.
- Detalle menor: sección Verificados usada para un "NO verificado" (debió
  quedar vacía o listar cumplimientos).
- Evidencia: `results/T4/transcript.md` + `run1.log`.

## T5 — Mejora autónoma (9B) — PASS

- 852s, rol EXECUTE. Eligió con evidencia el roto preexistente de T0 (scripts
  raíz `-w` sin `workspaces`), lo explicó ANTES de actuar y aplicó un diff
  mínimo (4 líneas en `package.json` raíz). Se autocorrigió un `console.log`
  propio en `server.ts` (diff neto cero ahí). Verificación real: `npm run
  build` en raíz ahora corre backend+frontend en verde (re-verificado a mano;
  antes fallaba con `No workspaces found`). Cero cambio funcional.
- Evidencia: `results/T5/transcript.md` + `run1.log`.

## Tabla de veredictos (9B qwen35-9b)

| Test | Veredicto | Tiempo | Nota |
|------|-----------|--------|------|
| T1b (verificación citada) | PARCIAL | ~9 min | 100% grounded, 2.5/4 veredictos |
| EXEC angosto | PASS | ~3 min | edit exacto + build verde |
| T2 (bug concurrencia) | FAIL | ~26 min | mecanismo erróneo + build rojo (revertido) |
| T3 (feature bulk) | PASS | ~50 min | feature completa + build verde, migración pendiente |
| T4 (seguridad) | PASS | ~18 min | hallazgo real con evidencia, 0 cambios |
| T5 (autónoma) | PASS | ~14 min | workspaces + builds raíz en verde |

## Tabla de veredictos (4B agents-a1-4b, referencia)

| Test | Veredicto | Nota |
|------|-----------|------|
| T1 (comprensión amplia) | FAIL (7 intentos) | no sintetiza; negativa honesta → confabulada |
| T2 (bug concurrencia) | FAIL (2 intentos) | lee bien, declara no-leído / no implementa |

## T1b — Verificación de tareas citadas (9B, caso original de la queja)

- Intento 1 (harness `d7a1a30` + 9B): 509s. Ruteo REVIEW correcto + buena
  investigación inicial (3 searches acertados). Pero REVIEW preloaded tenía
  `max_calls=1` → budget muerto al 2º search → cayó en `_enter_budget_retry`
  (write-only de EXECUTE): mutó el rol a EXECUTE y metió 5 `edit_file` a
  `package.json` raíz con texto narrativo ("Voy a explorar..."). Ninguno
  persistió (git limpio) pero es violación CRÍTICA de solo-lectura + UX
  (5 approvals en TUI). Sin veredictos por ítem.
- Fix aplicado (`496e0ca`, CRÍTICO/seguridad): REVIEW va al retry de
  solo-lectura (budget de explore, mensaje de informe); preloaded 1→4
  explores; `_enter_budget_retry` fail-closed fuera de EXECUTE. Tests: +2.
- Intento 2 (harness `496e0ca` + 9B): 546s. Rol contenido en REVIEW, 10+
  reads, cero edits (el fix de seguridad funciona). Pero respuesta VACÍA:
  `require_write` aplicaba a REVIEW en retries y castigó dos veces un informe
  correcto ("foco en escritura") hasta cerrar vacío.
- Fix aplicado (`25925ba`): `require_write` SOLO EXECUTE; REVIEW exige
  `require_text` (informe) en vez de escritura.
- Intento 3 (harness `25925ba` + 9B): 285s. Flujo REVIEW correcto
  (git_status → changed_files → searches → 5 reads), rol contenido, cero
  writes. Informe real con file:línea: DeletePaciente hard-delete y ausencia
  de JWT correctos; cumplidos sin veredicto (el formato no tenía sección).
- Fix aplicado (`620709d` + test): sección `Verificados` en formato e
  instrucciones de review.
- Intento 4 (harness `620709d` + 9B): 365s. REGRESIÓN: sin searches, con
  listados, citó `src/lib/patient.service.ts:0` y otros paths Next.js
  inexistentes (repo NestJS) + `:0` como línea. Mismo síntoma de la queja
  original, ahora determinísticamente detectable.
- Fix aplicado (`fff0479`): `orchestration/evidence.py` valida cada
  `[archivo:línea]` contra disco (existe + rango; `:0` inválida) y
  REVIEW/ANALYZE reintentan 1 vez si algo no verifica. Negativa honesta sin
  citas sigue permitida. Tests: `test_evidence.py` (6).
- Intento 5 (harness `fff0479` + 9B): 534s. Primer resultado defendible:
  flujo completo (changed_files → inspects → searches → 5 reads), todas las
  citas a archivos REALES (el validador no disparó), polaridad correcta en
  ítems 2 (hard-delete) y 3 (sin JWT), cero cambios. Gaps: ítem 1 hedged
  ("potencialmente", era TRUE) e ítem 4 mal (dice que no existe
  GET /api/health, existe en `server.ts:29`) — recall incompleto, no
  invención.
- Veredicto: **PARCIAL** (evidencia 100% grounded, 2.5/4 veredictos).
  Trayectoria 9B: mutación de rol → vacío → 2/4 → citas fantasma → grounded
  parcial. Cada fix movió la aguja con evidencia.

## EXEC angosto (9B, discriminador 2/2)

- Prompt: comentario de 1 línea en `CreatePaciente.ts` + build del backend.
- Resultado (171s): **PASS** — edit quirúrgico exacto sin tocar lógica,
  `run_build` en `backend/` ejecutado, `tsc` verde (re-verificado a mano),
  cierre determinístico correcto. Revertido después para dejar la rama limpia.
- Nota: el codemod PATH FIX re-aplicó `api.ts` (determinístico, correcto solo
  por el shim del apiClient). Sigue pendiente hacerlo respetar confirmaciones.

## Fixes al harness surgidos de la batería

1. `265bc13` — listados redundantes no consumen exploración (T1/4B).
2. `14fd12b` — snippets del grafo al read_cache + hechos completos (T1/4B).
3. `23c5ccc` — retry de solo-lectura en vez de 0 tools (T1/4B).
4. `4392019` — razonamiento paciente en retry de lectura (T1/4B).
5. `e8dde6a` — retry en 2 etapas, 2ª sin tools con ancla (T1/4B).
6. `d7a1a30` — 2ª vuelta con contexto mínimo + pregunta original (T1/4B).
7. `38f76d2` — retry exige tool call real, no plan de lectura (T2/4B).
8. `496e0ca` — REVIEW nunca gana escritura en retries, fail-closed (T1b/9B).
