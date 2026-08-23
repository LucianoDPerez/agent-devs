from pathlib import Path

LLM_BASE_URL = "http://localhost:8080/v1"
LLM_MODEL_NAME = "qwen3.6-35b-a3b"
LLM_TEMPERATURE = 0.2
# 600s: el 4B con razonamiento Qwen3 puede tardar 2-3 min antes de emitir la
# respuesta final (razona 1500-2500 chars). 300s cortaba respuestas largas.
LLM_TIMEOUT = 600
LLM_MAX_TOKENS = 3584
# EXECUTE: 2560 era para el 4B que quemaba tokens en razonamiento.
# Modelos nuevos (Ling-3.0-tiny) necesitan más margen para tool calls + verify.
EXECUTE_MAX_TOKENS = 4096
# REVIEW: 4096 para leer archivos, correr verify y emitir informe detallado
REVIEW_MAX_TOKENS = 4096
# ANALYZE: 4096 para que el razonamiento (~300 tokens) no consuma todo el
# output budget y quede espacio para el análisis final. Antes 1024: el 4B
# agotaba el budget en reasoning_content → content vacío (respuesta nula).
ANALYZE_MAX_TOKENS = 4096
# PLAN: 4096 para generar documentos y planes
PLAN_MAX_TOKENS = 4096

# Reasoning budget: el 4B puede gastar TODO su output budget en reasoning_content,
# produciendo 0 tokens útiles. Se pasa via extra_body a llama-server.
# Si el server no lo soporta, se ignora silenciosamente.
# 256 era para el 4B. qwen3.6-35b-a3b es un modelo PENSANTE: 256 tokens cortan
# la cadena de pensamiento a mitad de un tool call y degradan la calidad
# (bloques inventados, malas decisiones). 1024 da margen sin permitir runaway.
EXECUTE_MAX_REASONING_TOKENS = 1024
REVIEW_MAX_REASONING_TOKENS = 512
# ANALYZE: reasoning muy limitado para mantener brevidad
ANALYZE_MAX_REASONING_TOKENS = 64
# PLAN: reasoning moderado para estructurar el plan
PLAN_MAX_REASONING_TOKENS = 128

# Si el modelo produce SOLO reasoning (sin content/tool_calls), reintentar
# una vez con instrucción forzada y contexto recortado.
REASONING_RETRY_ENABLED = True
# Si el modelo lleva razonando más tiempo que este límite sin producir
# content/tool_calls, cortar el stream. El 4B puede razonar minutos sin actuar.
# El Qwen3.5-4B razona 1500-2500 chars (~2-3 min) ANTES de emitir content en
# respuestas libres (retry ANALYZE/PLAN sin tools). 90s cortaba justo antes de
# la respuesta. El server llama.cpp NO respeta reasoning_budget per-request
# (necesita --reasoning-budget global), así que el único control es este timeout.
# 300s da margen al razonamiento real; si el modelo se traba de verdad, igual
# lo corta TURN_IDLE_TIMEOUT.
# Cortar razonamiento sin output. 300 era demasiado: dos E2E reales quemaron
# 300s y 90s pensando en espiral sin emitir nada. Con el rescate de
# _partial_reasoning (session.py) el corte ya no pierde el diagnóstico — se
# recicla en el retry — así que cortar antes es barato.
MAX_REASONING_SECONDS = 180
TURN_IDLE_TIMEOUT = 360  # 6 min — el modelo 4B tarda en razonar análisis complejos

# Archivos de PLANIFICACIÓN PROTEGIDOS: el agente NUNCA debe escribir/editar/
# borrar sobre ellos. El 4B tiende a reescribir tasks.md (precargado en el
# prompt) como "primer objetivo", corrompiendo el plan. Estos patterns matchean
# por nombre de archivo o por subdirectorio (case-insensitive).
PROTECTED_TASK_FILENAMES = frozenset({
    "tasks.md", "task.md", "plan.md", "prd.md", "roadmap.md",
    "backlog.md", "agenda.md", "user-stories.md", "stories.md",
    "task-dependency-analyzer.md", "story-to-plan.md",
})
PROTECTED_TASK_DIRS = frozenset({
    ".agent-devs", ".agent", "plans", "planning", "_plans",
    ".atl", "docsplans", "tasks",
})

# Judge LLM: modelo más grande que valida reviews antes de aprobar.
# Si JUDGE_ENABLED es True y el review dice APROBADO, se llama al judge.
# El judge lee el diff + review report y decide si aprobar o bloquear.
# IMPORTANTE: usá un modelo DISTINTO y MÁS GRANDE que el executor (4B).
# Ejemplo: "Qwen3-14B", "Llama-3-8B", "deepseek-r1-distill-14B", etc.
JUDGE_ENABLED = False  # Desactivado: el 4B tarda demasiado como judge. Activá cuando tengas un modelo más grande.
JUDGE_BASE_URL = "http://localhost:8080/v1"
JUDGE_MODEL_NAME = "agents-a1-4b"  # ← CAMBIAR por un modelo más grande
JUDGE_TEMPERATURE = 0.3
JUDGE_MAX_TOKENS = 4096

# Límite de pasos modelo↔tools por turno (evita loops de exploración de 20+ min).
# En langgraph cada tool call consume ~2 pasos de recursion (nodo agente + nodo
# tools). 35 pasos ≈ 17 tool calls: ajustado para ANALYZE/PLAN/REVIEW, que
# exploran (trace_component + reads) y responden en texto.
AGENT_RECURSION_LIMIT = 50
# EXECUTE: límite por encima de MAX_TOOL_CALLS_PER_TURN×2+1 para que NUNCA corte
# antes que el tope de tool calls de stream (tool #15 = paso 30: con 30 el 35B
# murió justo después de run_tests sin ver el resultado — E2E T7 real). El
# antiloop REAL vive en el budget (dedupe + write-pressure + read caps), no acá.
EXECUTE_RECURSION_LIMIT = 70
# Pre-cargar en el mensaje de EXECUTE archivos .md/.txt citados por path absoluto
EXECUTE_PRELOAD_MAX_CHARS = 20_000
EXECUTE_PRELOAD_MAX_FILES = 2
# Máx list_files/search_code/inspect_routes por turno EXECUTE. Mantener BAJO:
# el modelo debe diagnosticar en 1-2 llamadas (trace_component) y escribir YA.
EXECUTE_EXPLORE_BUDGET = 3
# Tras agotar explore: máx read_file antes de forzar write. Dar margen razonable
# para diagnóstico de bugs (hook + página + API + backend + domain type).
EXECUTE_MAX_READS_AFTER_EXPLORE = 5
# Si no escribió nada tras N tool calls totales → forzar write. Con trace_component
# (1 llamada = source + usos + página) + 3-4 reads hay suficiente para diagnosticar.
# 12 da margen para el caso real con el 35B: archivo grande de 1800+ líneas que
# hay que leer por rangos (5-6 reads) + ubicar la función (1-2 search) + editar
# (1) + verificar (1-2). Antes 8 cortaba justo antes de escribir (E2E T7 re-run).
EXECUTE_MAX_TOOLS_BEFORE_WRITE = 12
# Límite duro total de tool calls por turno (EXECUTE). Contado por stream
# (stream_agent_turn). 30 = flujo completo real del 35B: leer un archivo de
# 1800+ líneas por rangos (5-8) + search (1-2) + edits (3-5) + lint/tests/build
# (3) + re-corregir tras fallo (2-4) + stage/commit (2) ≈ 20-28. El retry de
# budget (max_calls=0) se activa si se agota ANTES de escribir.
MAX_TOOL_CALLS_PER_TURN = 30
# Máx edit_file al MISMO archivo por turno sin correr verify (lint/tests/build)
# en el medio. El loop de la iteración E2E: 8 edit_file al mismo path con args
# distintos (el dedupe solo atrapa args idénticos) corrompiendo el JSX por
# partes. Al superar el tope se lanza ToolBudgetExceeded → retry con ancla.
MAX_EDITS_PER_FILE = 4
# edit_file: rechazo de bloques GRANDES (reescritura de memoria). 40 era el
# límite para el 4B. El 35B produce bloques de 50-90 líneas CORRECTOS (p. ej.
# insertar una función entera); rechazarlos disparaba el escalamiento a
# write_file → destrucción. 100 permite esos cambios sin habilitar
# reescrituras masivas: el old_str debe matchear el archivo real igual.
MAX_EDIT_BLOCK_LINES = 100
# Máx escrituras TOTALES (cualquier archivo) sin correr verify (lint/tests/build)
# en el medio. MAX_EDITS_PER_FILE solo capa el MISMO path; el spree multi-archivo
# (15 writes ciegos en la iteración de Medicos: scaffolding de tests roto sin
# instalar deps ni verificar) se colaba entre archivos distintos. Al superarlo
# se lanza VerifyRequired → session inyecta la compuerta de verificación
# (GATE_RETRY_TOOLS, que SÍ incluye run_lint/run_tests/run_build). 4 (antes 6):
# E2E real de búsqueda Medicos — 6 edits console.log-debugger en loop sin NINGÚN
# run_tests, diciendo "now let me run the tests" antes de cada edit_file.
EXECUTE_MAX_WRITES_BEFORE_VERIFY = 4
# Si un verify (lint/tests/build) FALLA y faltan dependencias declaradas en
# node_modules, el harness ejecuta npm install automáticamente y re-corre la
# verificación UNA vez. Los LLM chicos (4B/9B) ignoran sistemáticamente el
# hint "corré run_install" (E2E real: flujo trabado N turnos en "command not
# found"). npm install es idempotente: instala solo lo declarado que falte.
AUTO_INSTALL_ON_VERIFY_FAIL = True
# Máx verify calls SEGUIDAS sin escribir (loop anti-run_lint): el modelo
# entraba en loop de run_lint/run_tests sin escribir NADA (15 run_lint
# seguidos en iteración real). Una verificación honesta viene acompañada de
# escritura o cierre; el 6to verify sin write es un loop.
EXECUTE_MAX_VERIFY_BEFORE_WRITE = 5
# Tareas bulk: si la tarea toca ≥ este N de archivos (ej. Task 8 spec-kitti:
# modificar 14 templates), session.py escala los budgets de EXECUTE — lecturas,
# tools-before-write, writes-before-verify y tope de tool calls por turno. El
# budget default está calibrado para diagnóstico de 1-5 archivos: una tarea que
# modifica N archivos necesita ~N lecturas + ~2N edits + verify.
EXECUTE_BULK_MIN_FILES = 6
# Tamaño de batch: una tarea bulk se divide en subtareas de ~este N de
# archivos. Chico = radio de daño acotado y diff revisable; grande = menos
# turnos pero más riesgo (E2E Task 8: 14 archivos en un turno terminó en
# corrupción y corte a mitad).
BULK_BATCH_SIZE = 5
# Contexto (% del límite) que dispara rotación de sesión ENTRE batches.
# Nunca a mitad de un batch: se rota solo al terminar uno exitoso.
BULK_SESSION_ROTATION_CTX = 0.75
# Intentos por batch antes de marcarlo 'failed' y escalar al usuario.
BULK_MAX_BATCH_ATTEMPTS = 2
# Intentos máximos por turno EXECUTE bulk. El flujo real de una tarea de N
# archivos es: exploración → escritura → compuerta de verificación (VerifyRequired)
# → completar faltantes. max_attempts default (3: 1 + 2 retries) corta a mitad
# del trabajo (E2E real Task 8: 11/14 archivos, changelog corrupto, "cancelada
# (Ctrl+C)" engañoso → en realidad agotamiento de intentos). Con 6 el flujo
# bulk completo entra holgado.
EXECUTE_BULK_MAX_ATTEMPTS = 6
# Máx inyecciones MID-TURN de la compuerta de verificación (VerifyRequired)
# antes de declarar el turno fallido: evita ping-pong infinito write→gate.
VERIFY_GATE_MAX_INJECTIONS = 3
# Timeout de conexión al MCP externo (codebase-memory-mcp) en session.start().
# Si el MCP está colgado, el harness arranca SIN tools del knowledge graph en
# vez de bloquearse para siempre (E2E real: main.py sin emitir output 40 min).
MCP_CONNECT_TIMEOUT = 90
# Rechazos del guard quirúrgico de edit_file (bloque > 40 líneas) al mismo
# archivo antes de habilitar write_file completo para ese path: el modelo no
# converge con cirugía fina → cambiamos de estrategia con ancla del read cache.
MAX_EDIT_REJECTIONS_BEFORE_OVERWRITE = 2
# Razonamiento por bloque: si el modelo razona > N segundos SIN emitir output
# (content o tool call), cortar el stream. El 4B razona 30-60s antes de cada
# tool call; 90s por bloque corta runaway sin afectar el flujo normal.
EXECUTE_MAX_REASONING_SECONDS = 90

# Tras un turno EXECUTE con cambios sin commitear, preguntar al usuario si
# quiere commitear (nunca commit automático). Si el stdin no es un tty (tests,
# scripts), se omite la pregunta.
EXECUTE_ASK_COMMIT = True

# Codemod determinístico de paths frontend↔backend al arrancar EXECUTE: corrige
# automáticamente mismatches de imports/rutas (PATH FIX). Es modificador de
# código REAL del usuario: el mensaje lo disclosia, y este flag permite
# desactivarlo si no querés que el harness toque nada solo.
PATH_FIX_ENABLED = True

# EXECUTE: si el turno termina (el modelo responde texto) SIN haber llamado
# NINGUNA tool de escritura, lanzar retry write-only (sin read_file). Aider
# evita esto porque su formato de edición ES texto; acá el modelo puede
# "escapar" respondiendo un análisis — este flag lo impide desde el intento 1.
EXECUTE_REQUIRE_WRITE = True

# REVIEW budgets: el reviewer no escribe, solo lee y verifica
REVIEW_EXPLORE_BUDGET = 1
REVIEW_MAX_READS_AFTER_EXPLORE = 15
REVIEW_MAX_TOOLS_BEFORE_WRITE = 30

# ANALYZE/PLAN budgets: capan la búsqueda en el knowledge graph MCP (cm__*).
# El 4B se mareaba re-buscando lo mismo con queries distintas. Al agotarse
# lanza ToolBudgetExceeded → retry SIN tools de búsqueda (nunca write-only).
ANALYZE_EXPLORE_BUDGET = 4
ANALYZE_MAX_READS_AFTER_EXPLORE = 8
PLAN_EXPLORE_BUDGET = 4
PLAN_MAX_READS_AFTER_EXPLORE = 8

# Compuerta post-escritura (EXECUTE): tras escribir código, corre el build/lint
# y si falla por un archivo que acabamos de tocar, reintenta inyectando el error.
# Es la red de seguridad clave para LLM chicos que escriben código que no compila.
POST_WRITE_GATE_ENABLED = True
POST_WRITE_GATE_MAX_RETRIES = 1

# write_file: prohibido SOBRESCRIBIR archivos existentes (solo permite ≤ 5
# líneas para configs triviales como .env.example). El 4B que reescribe un
# archivo entero de memoria pierde imports/hooks/lógica (PacientesPage.tsx
# quedó mutilado). Para archivos existentes solo edit_file quirúrgico (previo
# read_file) — write_file queda para archivos nuevos.
WRITE_FILE_OVERWRITE_MAX_LINES = 5

MAX_FILE_READ_BYTES = 50_000
MAX_LIST_RESULTS = 100

EXCLUDED_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".env", ".mypy_cache", ".pytest_cache", "dist", "build",
    ".next", ".nuxt", "target", ".gradle", ".idea", ".vscode",
    ".wwebjs_session", ".wwebjs_cache", "https:",
}

EXCLUDED_FILES = {
    ".DS_Store", "Thumbs.db", ".env", ".log", ".tmp",
    "tsconfig.tsbuildinfo",
}

CACHE_DIR = Path.home() / ".agent-cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

CACHE_DB = CACHE_DIR / "repo_lens.db"

MAX_SNAPSHOT_FILES = 5_000
MAX_CONTEXT_CHARS = 12_000
MAX_LINE_CHARS = 400
MAX_SEARCH_RESULT_CHARS = 15_000
