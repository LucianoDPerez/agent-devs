"""Índice de tools por dominio.

Cada tool vive en su módulo por dominio:
- filesystem.py: list_files, read_file, write_file, edit_file
- search.py:     search_code
- routes.py:     inspect_routes

Este índice reexporta todas y expone un pool por tipo de agente para que cada
agente (analyzer, planner, executor, reviewer) use solo las que necesita.
"""

from .filesystem import delete_file, edit_file, list_files, read_file, write_file
from .git import (
    changed_files,
    create_commit,
    create_pr,
    current_branch,
    git_log,
    git_restore,
    git_status,
    list_prs,
    push,
    read_pr,
    stage_files,
)
from .routes import inspect_routes
from .models import inspect_models
from .env import inspect_env
from .search import search_code
from .verify import run_build, run_install, run_lint, run_npm_script, run_tests

ALL_TOOLS = [
    list_files, read_file, write_file, edit_file, delete_file, search_code, inspect_routes,
    inspect_models,
    run_install, run_lint, run_tests, run_build, run_npm_script,
    current_branch, changed_files, git_status, git_log,
    stage_files, create_commit, push, create_pr, read_pr, list_prs,
]

_READONLY_GIT = [current_branch, changed_files, git_status, git_log, read_pr, list_prs]
_GIT_WRITE = [stage_files, create_commit, push, create_pr]
_VERIFY = [run_install, run_lint, run_tests, run_build]

# Subsets por rol de agente. Read-only evita que compile modificadores.
ANALYZER_TOOLS = [list_files, read_file, search_code, inspect_routes, inspect_models, inspect_env, *_READONLY_GIT]
PLANNER_TOOLS = [list_files, read_file, search_code, inspect_routes, inspect_models, inspect_env, write_file, *_READONLY_GIT]
# EXECUTE: SOLO las esenciales. 35 tools (21 locales + 14 MCP) diluía la
# atención del modelo 4B — "olvidaba" que tenía edit_file y se escondía en
# read_file infinitos. 12 tools + trace_component (compuesta, agrega agent_builder).
EXECUTOR_TOOLS = [
    read_file, write_file, edit_file, delete_file,
    search_code, inspect_routes,
    run_lint, run_tests, run_build, run_npm_script,
    stage_files, create_commit, push, git_restore,
]
REVIEWER_TOOLS = [list_files, read_file, search_code, inspect_routes, inspect_models, inspect_env, *_READONLY_GIT, *_VERIFY]

# Retry de EXECUTE tras loop de lectura: SOLO escritura + git-write + verify.
# TRULY write-only: SIN read_file (el modelo se escondía ahí) — el contenido
# exacto de los archivos va inyectado en el mensaje (read_cache → anchor).
# Sin list_files/search_code (nada que explorar) y sin run_install/create_pr.
WRITE_ONLY_TOOLS = [
    write_file, edit_file, delete_file,
    stage_files, create_commit, push,
    run_lint, run_tests, run_build,
]

# LEGACY — ya NO se usa en el retry de budget (ver BUDGET_RETRY_TOOLS).
# Retry de EXECUTE tras NO escribir (budget/reasoning/no-write): read + edit +
# write + search_code. SIN verify tools: el modelo las usaba como "acción
# gratis" para esquivar la write pressure (corría run_lint/tests/build ANTES de
# escribir, quemaba el presupuesto y nunca escribía — visto en E2E real).
# Mantenido por compatibilidad; el 4B se escondía en read_file/search_code y
# nunca escribía (E2E T7: __init__.py leído en chunks hasta quemar 3 intentos).
WRITE_RETRY_TOOLS = [
    read_file, edit_file, write_file, delete_file,
    search_code,
]

# Retry de EXECUTE tras agotar el presupuesto de exploración: escritura con
# lectura ACOTADA. read_file SÍ está (limitado por limit_reads_now +
# max_reads_after_explore): sin lecturas el 35B alucina o destruye (E2E real:
# escribía tmp_read.sh/tmp_read.py para intentar leer el archivo). SIN
# delete_file: borrar + recrear bypassa el guard anti-sobrescritura (E2E real:
# el 35B borró __init__.py de 1851 líneas y escribió un stub de 40). SIN verify
# tools (el modelo las usaba como "acción gratis" para esquivar la write
# pressure). El contenido de lo ya leído va inyectado en el ancla; la compuerta
# de verificación (sistema) inyecta verify después.
BUDGET_RETRY_TOOLS = [
    read_file, edit_file, write_file,
]

# Retry de la compuerta post-escritura (error de compilación): corregir un
# error EXIGE ver el archivo real — sin read_file el 4B alucina old_str y
# termina reescribiendo el archivo entero de memoria (destructivo, ver
# PacientesPage.tsx). SIN herramientas de búsqueda (list_files/search_code:
# el error ya viene inyectado, no hay que explorar) y SIN git-write (el fix
# no debe volver a commiteear).
GATE_RETRY_TOOLS = [
    read_file, edit_file, write_file, delete_file,
    *_READONLY_GIT, *_VERIFY, run_npm_script,
]

AGENTS: dict[str, list] = {
    "analyzer": ANALYZER_TOOLS,
    "planner": PLANNER_TOOLS,
    "executor": EXECUTOR_TOOLS,
    "reviewer": REVIEWER_TOOLS,
}

# Compatibilidad: get_tools() sin argumentos devuelve el pool completo,
# como hacía el tools.py original (uso actual en main.py).
def get_tools(agent_type: str | None = None):
    """Devuelve el pool completo (None) o el subset de un tipo de agente."""
    if agent_type is None:
        return list(ALL_TOOLS)
    if agent_type not in AGENTS:
        raise KeyError(
            f"Tipo de agente desconocido: {agent_type!r}. "
            f"Disponibles: {', '.join(AGENTS)}"
        )
    return list(AGENTS[agent_type])


def get_tools_for(agent_type: str):
    """Alias semántico: herramientas de un agente concreto."""
    return get_tools(agent_type)
