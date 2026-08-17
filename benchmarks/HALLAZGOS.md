# Benchmark Medicos — Hallazgos, Correcciones y Evidencia

**Fecha**: 2026-08-16/17 · **Repositorio objetivo**: `/Users/luchop/PROYECTOS IA/Medicos` (no se modifica desde acá: todo cambio lo ejecuta el agente AgentDevs) · **Harness**: `/Users/luchop/agent-lucho`

## Estado del benchmark (15 tareas: J1-J5 junior, S1-S5 semi-senior, T1-T5 senior)

| Tarea | Estado | Evidencia |
|---|---|---|
| J1 — tests backend funcionales | ✅ LOGRADA (esencia) | `npm run test -w backend -- --forceExit` → **2 passed, 2 total**; `npm run build -w backend` → exit 0 |
| J1.1 — forceExit en jest.config | ⚠️ pendiente | sin el flag, `npm run test -w backend` a secas queda colgado por open handles de PrismaClient |
| J2–J5, S1–S5, T1–T5 | 🔲 sin ejecutar | prompts listos en `benchmarks/tasks.json` |

## Cronología de J1 (8 iteraciones de tarea + micro-pasos)

1. **J1**: agente creó `backend/jest.config.js` + exportó `app` en server.ts. Verificación falló: el root no tenía `workspaces` → `npm run -w` roto.
2. **J1b**: agregó workspaces (lo DUPLICÓ en una edición). jest seguía sin instalarse.
3. **J1c**: el agente NUNCA llamó run_install (grep=0) y loop de 15 run_lint.
4. **J1d**: arregló setup.ts y tsconfig (excluir src/tests del build → build ✅).
5. **J1e**: se quitó `@types/vitest` (E404 real: **no existe en npm** — era el veneno del scaffolding original que rompía TODO npm install de workspaces).
6. **J1f**: jest corría tests COMPILADOS de dist/ → fix roots/ignorePatterns.
7. **J1g/J1h**: el agente **inventó la versión `@types/supertest@6.3.10`** (E404 — segundo invento de versión). La correcta: ^6.0.2.
8. **Micro-pasos finales**: prisma generate (`@prisma/client did not initialize` — los install-scripts de npm están bloqueados, por eso prisma nunca se autogenera), el test raíz `GET /` → 404 (la ruta no existe), y el agente (4B) reescribió el archivo de tests dejándolo limpio: 2 tests reales (`/api/health`, `/api/pacientes`) → **2 passed**.

## Hallazgos clave del modelo pequeño (4B/9B) — todos con evidencia en `benchmarks/results/`

- **Invención de dependencias/versiones inexistentes** (2 casos: `@types/vitest`, `@types/supertest@6.3.10`). El harness no puede validar versiones; la red de seguridad es `run_install` fallando + iteración.
- **Nunca corren `run_install`** incluso con el hint explícito (4 turnos con el 9B, 0 llamadas). Fix del harness: `AUTO_INSTALL_ON_VERIFY_FAIL` — si un verify falla y faltan deps declaradas, el harness ejecuta npm install solo y re-corre (funcionó: instaló @types/supertest sin ayuda del modelo).
- **Loops de verify sin escribir** (15 run_lint seguidos). Fix: `max_verify_before_write` → ToolBudgetExceeded.
- **Spree multi-archivo sin verificar** (15 writes ciegos). Fix: `max_writes_before_verify` → `VerifyRequired` → inyección de compuerta de verificación (se activó en vivo: "El modelo escribió sin verificar. Inyectando compuerta").
- **Edits de memoria sin leer** (11 edit_file fallidos al mismo path). El 4B escribe archivos COMPLETOS mejor que edits quirúrgicos.
- **Trabajo no solicitado** (9B): creó `run_install.sh`, `scripts/`, `.env.example` con JWT, tocó repos Prisma, `domain/paciente/` en singular. Fix del harness: tool `git_restore` (el agente pudo revertir su propia basura: lo hizo bien en el paso de limpieza).
- **Desobediencia a "UNA sola tool"** (ambos modelos). Con razonamiento off el 9B divaga más; el 4B responde mejor a tareas de UNA acción con dictado exacto.

## Fixes del harness (agent-lucho) — todos con tests verdes (104+ unit tests)

| Fix | Dónde | Evidencia |
|---|---|---|
| `VerifyRequired` + `max_writes_before_verify` | orchestration/tool_dedupe.py | tests/test_budget.py (TestWritesBeforeVerify) |
| `max_verify_before_write` (anti-loop verify) | idem | TestVerifyStreak |
| `limit_reads_now()` — retry write-only con lectura acotada REAL | idem | TestLimitReadsNow |
| Retry con `max_calls=1` + `_called_tools.clear()` | orchestration/session.py | suite completa |
| **Bug latente: la compuerta de verificación nunca llegaba al modelo** (`messages_for_agent` stale) | session.py:1305 | fix + comentario |
| `AUTO_INSTALL_ON_VERIFY_FAIL` + detección de deps faltantes/workspaces stale por NOMBRE de paquete | tools/verify.py | tests/test_verify.py (6 tests nuevos) |
| `run_npm_script` (scripts declarados, sin comandos arbitrarios) | tools/verify.py | tests/test_verify.py |
| `git_restore` (revert de daño lateral) | tools/git.py | tests/test_git.py |
| `MCP_CONNECT_TIMEOUT` — **el harness se colgaba 40 min en init_mcp** con el MCP muerto | orchestration/agent_builder.py | corrida real: "⚠️ MCP no respondió — se continúa sin tools" |
| `_validate_cwd` con path vacío | tools/verify.py | tests/test_verify.py |

## Métricas de velocidad (M1 Air 16GB — benchmarks/speed_test.py)

- **Decode: 4.3 tok/s (4B Q8) / 4.5 tok/s (9B Q6)** — constante sin importar flags (fa on/off, kv-unified, cache q4/q5/q8, context 12k-36k, threads 4/8, build HEAD vs release b10453).
- Prefill: 59-65 tok/s. → turno EXECUTE de 2k tokens ≈ 8 min solo de decode.
- `--spec-draft` (draft-mtp) **crashea el server** (GGML_ASSERT n_outputs_max) en build HEAD.
- El 4B en build release se colgó 40 min sin emitir tools (incompatibilidad de template) → usar build homebrew.
- **Conclusión**: el techo es la MÁQUINA (Air sin ventilador, 16GB con swap), no los flags. Los flags de sampling (temp/top-p/reasoning) no cambian la velocidad de decode.

## Lecciones para el diseño futuro (idea de checklists/status del usuario)

- Los micro-pasos de UNA acción funcionan mejor que las tareas multiclase — evidencia: solo convergió cuando se dividió.
- `completed` debe ser DECIDIDO POR VERIFICACIÓN EXTERNA, no por el modelo.
- El costo de tokens del prompt inicial es el riesgo principal: retrieval top-k con presupuesto duro (~1.5k tokens), no historial completo.
- Esta misma infraestructura (harness) en mejor hardware multiplica 10-20x — el valor es el orquestador, no la velocidad de hoy.

## Cómo seguir

```bash
# cada tarea: el agente la ejecuta, el runner verifica y registra evidencia
.venv/bin/python benchmarks/run.py J2      # frontend tests
.venv/bin/python benchmarks/run.py --list  # todas las tareas
# micro-pasos manuales:
.venv/bin/python benchmarks/run.py --step "<prompt de UNA acción>" --step-label "<nombre>"
# velocidad del LLM:
.venv/bin/python benchmarks/speed_test.py 3 300
```

**Servidor recomendado** (config estable que funcionó): 4B Q8, build homebrew, context 16384-36384, temp 0.2, SIN reasoning off, SIN spec-draft. Server arriba: `curl localhost:8080/v1/models`.
