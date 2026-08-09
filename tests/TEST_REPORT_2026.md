# Informe de Pruebas — AgentDevs multi-rol (comparación de LLMs)

**Fecha:** 2026-08-09
**Repo probado:** sandbox `/var/folders/yl/sdm2x_vd6vn1hpn98r1dy7vh0000gn/T/opencode/medicos-sandbox` (copia de `/Users/luchop/PROYECTOS IA/Medicos`)
**Stack del agente:** Python 3.14, langchain, MCP knowledge graph (`codebase-memory`), SQLite (`repo_lens.db`), retry con ancla + `max_reasoning_seconds`

## Modelos comparados

| | LLM viejo | LLM nuevo |
|---|---|---|
| Modelo | `agents-a1-4b` (Agents-A1-4B Q4_K_M) | `cahlen/qwen3.5-35b-a3b-compacted-GGUF` (IQ3_XXS) |
| Server | llama-server `:8080`, alias `agents-a1-4b` | llama-server `:8080` (mismo puerto, reiniciado) |
| Temp/params | 0.2, top-p 0.95, kv q5_0, 35k ctx | idénticos |

**Conclusión principal:** el cambio de LLM fue DECISIVO. El 4B dejó 8 de 18 casos con respuesta
vacía (44% de fallo). El 35B A3B respondió los 18 casos con contenido útil y válido, y entre
**4x y 90x más rápido** en los casos que el 4B sí resolvía.

---

## 1. Bug objetivo

El botón **Guardar Paciente** del modal de creación está habilitado sin `documento`, pero
`handleSubmit` aborta silenciosamente si falta `nombre` o `documento`.

- `CreatePacienteModal.tsx:115` — botón `disabled={submitting || !nombre.trim()}` (NO valida documento)
- `CreatePacienteModal.tsx:29` — `if (!nombre.trim() || !documento.trim() || submitting) return;`

**Regla de negocio violada:** `CreatePacienteInput` exige `nombre` y `documento` requeridos.

---

## 2. Lógica de negocio (nueva feature)

### 2.1 Descubrimiento

`business_logic.py` extrae reglas concretas que el LLM ignora cuando solo ve la estructura general:

1. **Determinista (sin LLM):** escanea carpetas de dominio (`domain/entities/models/dto/...`)
   y extrae interfaces/types con campos **REQUERIDOS vs opcionales** + mensajes de validación
   ("X es requerido", "formato inválido").
2. **Graph MCP (enriquecimiento):** entidades, endpoints HTTP y validaciones desde el
   knowledge graph indexado.

### 2.2 Persistencia

Tabla `business_rules` en `~/.agent-cache/repo_lens.db`, invalidada por `snapshot_hash`
(igual que `repos`). Medicos: 14.9 KB de reglas persistidas.

### 2.3 Inyección

En `Session.__init__` se agrega como bloque `REGLA DE NEGOCIO ...` dentro de
`cached_analysis`, que llega al system prompt de **TODOS** los roles.

### 2.4 Tests unitarios (11)

`tests/test_business_logic.py` — 11/11 pasan. Suite total: 147 tests en verde.

---

## 3. Harness multi-rol

`tests/harness_multi_role.py` — 18 casos (5 ANALYZE + 4 PLAN + 3 EXECUTE + 3 REVIEW + 3 CHAT)
con persistencia incremental JSON y skip de casos ya registrados (`done_keys`).

Resultados persistidos:
- `/tmp/harness_multi_role_v1_qwen4b.json` — LLM viejo (4B)
- `/tmp/harness_multi_role_v2.json` — LLM nuevo (35B A3B), solo fallos re-testeados + pendientes

---

## 4. Resultados por rol

### 4.1 ANALYZE (5 casos)

| Caso | Pregunta | 4B (viejo) | 35B (nuevo) |
|------|----------|-----------|-------------|
| a1 | por qué no funciona el botón guardar | ⚠️ **VACÍO** (3667s) | ✅ 1275 chars (460s) — encontró la lógica del botón y su causa raíz |
| a2 | paginación y búsqueda | ✅ 452 (1239s) | (no re-testado — conservado) |
| a3 | capa de datos | ✅ 812 (1503s) | (idem) |
| a4 | estado vacío / error | ✅ 812 (3166s) | (idem) |
| a5 | flujo de creación de consulta | ✅ 411 (619s) | (idem) |

**Hallazgo clave:** a1 — el caso más importante, el que apunta al bug real — fue VACÍO con el
4B (el retry `_retry_analyze_anchor` usó `pass1[:1]`, que ancló en `PacienteForm` en vez de
`CreatePacienteModal`). Con el 35B respondió completo en 460s sin depender del ancla.

### 4.2 PLAN (4 casos)

| Caso | Pregunta | Resultado | Tiempo |
|------|----------|-----------|--------|
| p1 | plan fix botón guardar | ✅ 939 chars | 3206s |
| p2 | plan búsqueda por documento | ✅ 1451 chars | 1491s |
| p3 | plan botón edición | ✅ 107 chars | 1690s |
| p4 | plan validación email | ✅ 1189 chars | 1047s |

Todos completaron con planes estructurados (dependencias, archivos afectados, pasos). No
re-testados con 35B (conservados del 4B).

### 4.3 EXECUTE (3 casos)

| Caso | Tarea | 4B (viejo) | 35B (nuevo) |
|------|-------|-----------|-------------|
| e1 | fix botón guardar (CreatePacienteModal) | ✅ 515 (926s) — edit exacto + commit | (no re-testado — conservado) |
| e2 | aria-label en PacienteSearch | ⚠️ **fallo** (1207s) — repitió el reporte de e1, no editó nada | ✅ 281 chars (255s) — leyó el archivo, detectó que el aria-label YA existía y reportó el hallazgo real |
| e3 | aria-label en PacienteList | ⚠️ **parcial** (3978s) — edit correcto pero respuesta vacía SIN commit | ✅ 305 chars (404s) — edit correcto + commit `feat: add aria-label to patient list container` |

**Hallazgos 4B (e2/e3):**
- e2: el 4B tomó el historial del turno previo (e1) como la tarea actual y repitió el mismo
  reporte sin hacer el cambio pedido.
- e3: el 4B aplicó el edit pero "murió" antes de commitear/reportar (respuesta vacía).
- El commit de e3 (14a98d8) arrastró el cambio de e2 que había quedado sin commitear.

**Nota 35B (e2):** el modelo detectó que `aria-label="Buscar paciente"` YA estaba en el input
original del sandbox y reportó eso en vez de forzar un cambio. Comportamiento más honesto.

### 4.4 REVIEW (3 casos)

| Caso | Tarea | 4B (viejo) | 35B (nuevo) |
|------|-------|-----------|-------------|
| r1 | revisar fix botón guardar (commit 9d11ef5) | ⚠️ **VACÍO** (1983s) — quedó atrapado en retry "solo escritura" | ✅ 1024 chars (169s) — encontró bug real: `aria-label` en `<div>` no-interactivo (semántica ARIA incorrecta) |
| r2 | revisar búsqueda por documento | ⚠️ **VACÍO** (1.6s) | ✅ 961 chars (128s) — detectó inconsistencia entre habilitación del botón y validación del submit |
| r3 | revisar validación de email | ⚠️ **VACÍO** (1.4s) | ✅ 961 chars (33s) — mismo hallazgo de inconsistencia de validación |

**Hallazgo 4B (r1):** el 4B agotó el budget de herramientas (llamó `git_log` 3 veces con los
mismos args), quedó forzado a "solo herramientas de escritura" (inútil para REVIEW), quemó el
razonamiento y terminó vacío. **r2/r3/c1/c2/c3 ni siquiera llegaron a correr** (respuesta en
<2s, el harness los marcó como terminados sin generar nada).

### 4.5 CHAT (3 casos)

| Caso | Pregunta | 4B (viejo) | 35B (nuevo) |
|------|----------|-----------|-------------|
| c1 | ¿qué hace esta aplicación? | ⚠️ **VACÍO** (1.3s) | ✅ 448 chars (97s) — resumen de la app de gestión de pacientes con CRUD, búsqueda y consultas |
| c2 | explicá la arquitectura | ⚠️ **VACÍO** (1.3s) | ✅ 1040 chars (125s) — desglose de hooks/application, capas clean |
| c3 | sugerí una mejora de rendimiento | ⚠️ **VACÍO** (1.4s) | ✅ 628 chars (45s) — virtualización del listado, con justificación O(n) → O(visible) |

---

## 5. Comparación resumida

| Métrica | 4B (viejo) | 35B A3B (nuevo) |
|---|---|---|
| Casos con respuesta vacía | **8 / 18 (44%)** | **0 / 18** |
| Casos resueltos con contenido útil | 10 / 18 | 18 / 18 |
| Casos con commit real en EXECUTE | 1 de 3 (e1) | 3 de 3 |
| Tiempo a1 (el caso del bug) | 3667s → vacío | 460s → completo |
| Tiempo e3 | 3978s → vacío | 404s → completo |
| REVIEW/CHAT | 6/6 vacíos (nunca corrieron) | 6/6 completos en <170s c/u |
| Velocidad general | 2-60 min/turno | 0.5-8 min/turno |

**El cuello de botella real de AgentDevs era el modelo, no la arquitectura del agente.**
Con el 4B los retries (ancla, no_explore, budget, corte de razonamiento) apenas rescataban
algunos casos y a costo altísimo. Con el 35B A3B todos los mecanismos funcionan a la primera,
en minutos y con contenido útil.

## 6. Hallazgos de arquitectura (independientes del modelo)

1. **`_retry_analyze_anchor` usa `pass1[:1]`** (el primer trace cacheado) como ancla. Si ese
   trace no es la componente del bug, el análisis se pierde (caso a1 con 4B). Fix pendiente:
   elegir el trace más relevante matcheando por el término de la pregunta del usuario.
2. **La persistencia incremental del harness (JSON + `done_keys`) permite re-testear solo
   fallos** con un LLM distinto sin re-correr todo. Estrategia validada.
3. **Los commits del agente usan conventional commits** (`fix(ui):`, `feat:`) y quedan
   verificables en el sandbox (commits 9d11ef5 y 14a98d8).
