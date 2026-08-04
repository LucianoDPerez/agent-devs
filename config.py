from pathlib import Path

LLM_BASE_URL = "http://localhost:8080/v1"
LLM_MODEL_NAME = "agents-a1-4b"
LLM_TEMPERATURE = 0.7
LLM_TIMEOUT = 300
LLM_MAX_TOKENS = 4096
TURN_IDLE_TIMEOUT = 120

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
