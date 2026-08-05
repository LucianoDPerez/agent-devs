from pathlib import Path

LLM_BASE_URL = "http://localhost:8080/v1"
LLM_MODEL_NAME = "agents-a1-4b"
LLM_TEMPERATURE = 0.7
LLM_TIMEOUT = 300
LLM_MAX_TOKENS = 3584
# EXECUTE: modelo 4B quema tokens en razonamiento; 2560 obliga a actuar rápido
EXECUTE_MAX_TOKENS = 2560
# REVIEW: 4096 para leer archivos, correr verify y emitir informe detallado
REVIEW_MAX_TOKENS = 4096
TURN_IDLE_TIMEOUT = 180  # 3 min — el modelo 4B tarda en razonar reviews complejos

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
# EXECUTE: 4B ignora STOP y quema pasos en loops; 45 da margen real
EXECUTE_RECURSION_LIMIT = 45
# Pre-cargar en el mensaje de EXECUTE archivos .md/.txt citados por path absoluto
EXECUTE_PRELOAD_MAX_CHARS = 20_000
EXECUTE_PRELOAD_MAX_FILES = 2
# Máx list_files/search_code/inspect_routes por turno EXECUTE (enforceado en código)
EXECUTE_EXPLORE_BUDGET = 3
# Tras agotar explore: máx read_file antes de forzar write
EXECUTE_MAX_READS_AFTER_EXPLORE = 5
# Si no escribió nada tras N tool calls → forzar write_file
EXECUTE_MAX_TOOLS_BEFORE_WRITE = 8

# REVIEW budgets: el reviewer no escribe, solo lee y verifica
REVIEW_EXPLORE_BUDGET = 1
REVIEW_MAX_READS_AFTER_EXPLORE = 15
REVIEW_MAX_TOOLS_BEFORE_WRITE = 30

DEFAULT_REPO_PATH = "/Users/luchop/PROYECTOS IA/Medicos"

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
