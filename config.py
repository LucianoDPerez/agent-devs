from pathlib import Path

LLM_BASE_URL = "http://localhost:8080/v1"
LLM_MODEL_NAME = "agents-a1-4b"
LLM_TEMPERATURE = 0.2
# 600s: el 4B con razonamiento Qwen3 puede tardar 2-3 min antes de emitir la
# respuesta final (razona 1500-2500 chars). 300s cortaba respuestas largas.
LLM_TIMEOUT = 600
LLM_MAX_TOKENS = 3584
# EXECUTE: modelo 4B quema tokens en razonamiento; 2560 obliga a actuar rápido
EXECUTE_MAX_TOKENS = 2560
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
EXECUTE_MAX_REASONING_TOKENS = 256
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
MAX_REASONING_SECONDS = 300
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

# Límite de pasos modelo↔tools por turno (evita loops de exploración de 20+ min)
AGENT_RECURSION_LIMIT = 35
# EXECUTE: ya no corta el loop de lectura (eso lo hacen max_calls=0 + retry
# write-only). 30 da margen para el flujo completo: 5×write + stage + commit
# + lint + tests + build ≈ 12-15 steps. Antes 10 cortaba antes de terminar.
EXECUTE_RECURSION_LIMIT = 30
# Pre-cargar en el mensaje de EXECUTE archivos .md/.txt citados por path absoluto
EXECUTE_PRELOAD_MAX_CHARS = 20_000
EXECUTE_PRELOAD_MAX_FILES = 2
# Máx list_files/search_code/inspect_routes por turno EXECUTE. EXPLORE es lo que
# causa loops (el modelo re-busca lo mismo con queries distintas). Mantener BAJO.
EXECUTE_EXPLORE_BUDGET = 2
# Tras agotar explore: máx read_file antes de forzar write. Dar margen razonable
# para diagnóstico de bugs (hook + página + API + backend + domain type).
EXECUTE_MAX_READS_AFTER_EXPLORE = 5
# Si no escribió nada tras N tool calls totales → forzar write. read_file NO
# cuenta como productivo en EXECUTE (solo verify tools). Dar margen para 2
# explore + 4-5 reads = diagnóstico completo sin loop infinito.
EXECUTE_MAX_TOOLS_BEFORE_WRITE = 6
# Límite duro total de tool calls por turno (EXECUTE). Ya no previene read-loops
# (eso lo hace max_calls=0 + retry write-only); 20 da margen al flujo completo:
# 5-6 writes + stage + commits + verify. Solo corta si el 4B entra en runaway.
MAX_TOOL_CALLS_PER_TURN = 20

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
