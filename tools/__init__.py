"""Índice de tools por dominio.

Cada tool vive en su módulo por dominio:
- filesystem.py: list_files, read_file, write_file, edit_file
- search.py:     search_code
- routes.py:     inspect_routes

Este índice reexporta todas y expone un pool por tipo de agente para que cada
agente (analyzer, planner, executor, reviewer) use solo las que necesita.
"""

from .env import inspect_env
from .filesystem import apply_patch, delete_file, edit_file, list_files, read_file, write_file
from .git import (
    changed_files,
    create_branch,
    create_commit,
    create_pr,
    current_branch,
    git_log,
    git_restore,
    git_status,
    list_prs,
    pr_comment,
    push,
    read_pr,
    stage_files,
)
from .models import inspect_models
from .routes import inspect_routes
from .runtime_probe import capture_dev_server, probe_http, probe_tcp
from .search import search_code
from .verify import run_build, run_install, run_lint, run_npm_script, run_tests, run_verify

ALL_TOOLS = [
    list_files, read_file, write_file, edit_file, delete_file, search_code, inspect_routes,
    inspect_models,
    run_install, run_lint, run_tests, run_build, run_verify, run_npm_script,
    current_branch, changed_files, git_status, git_log,
    stage_files, create_branch, create_commit, push, create_pr, pr_comment, read_pr, list_prs,
]

_READONLY_GIT = [current_branch, changed_files, git_status, git_log, read_pr, list_prs]
_GIT_WRITE = [stage_files, create_branch, create_commit, push, create_pr, pr_comment]
_VERIFY = [run_install, run_lint, run_tests, run_build, run_verify]

# Subsets por rol de agente. Read-only evita que compile modificadores.
ANALYZER_TOOLS = [list_files, read_file, search_code, inspect_routes, inspect_models, inspect_env, probe_http, probe_tcp, capture_dev_server, *_READONLY_GIT]
PLANNER_TOOLS = [list_files, read_file, search_code, inspect_routes, inspect_models, inspect_env, write_file, *_READONLY_GIT]
# EXECUTE: SOLO las esenciales. 35 tools (21 locales + 14 MCP) diluía la
# atención del modelo 4B — "olvidaba" que tenía edit_file y se escondía en
# read_file infinitos. 12 tools + trace_component (compuesta, agrega agent_builder).
EXECUTOR_TOOLS = [
    read_file, write_file, edit_file, apply_patch, delete_file,
    search_code, inspect_routes,
    run_lint, run_tests, run_build, run_verify, run_npm_script,
    # git de LECTURA: 'hacer commit de los modificados' exige VER qué cambió
    # (E2E real: sin git_status el modelo intentó leer .git/HEAD con read_file).
    current_branch, changed_files, git_status, git_log,
    stage_files, create_branch, create_commit, push, git_restore,
    pr_comment, create_pr,
    probe_http, probe_tcp, capture_dev_server,
]
REVIEWER_TOOLS = [list_files, read_file, search_code, inspect_routes, inspect_models, inspect_env, probe_http, probe_tcp, *_READONLY_GIT, *_VERIFY, pr_comment]

# Retry de EXECUTE tras loop de lectura: SOLO escritura + git-write + verify.
# TRULY write-only: SIN read_file (el modelo se escondía ahí) — el contenido
# exacto de los archivos va inyectado en el mensaje (read_cache → anchor).
# Sin list_files/search_code (nada que explorar) y sin run_install/create_pr.
WRITE_ONLY_TOOLS = [
    write_file, edit_file, apply_patch, delete_file,
    stage_files, create_commit, push,
    run_lint, run_tests, run_build, run_verify,
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
# pressure). El contenido de los archivos ya leídos va inyectado en el ancla; la
# compuerta de verificación (sistema) inyecta verify después. El escalamiento a
# write_file completo queda DESHABILITADO en este retry (allow_overwrite_
# escalation=False): sobrescribir de memoria destruye aunque haya lecturas
# acotadas.
# GIT en el retry (E2E real "implementar commit...": corte de razonamiento →
# retry sin tools de git → el modelo leyó .git/config crudo para averiguar la
# rama). Git de lectura + stage/commit/push: el retry puede COMPLETAR un
# commit a mitad (tarea de git interrumpida), no solo editar código.
BUDGET_RETRY_TOOLS = [
    read_file, edit_file, apply_patch, write_file,
    current_branch, changed_files, git_status, git_log,
    stage_files, create_commit, push,
]

# Retry de la compuerta post-escritura (error de compilación): corregir un
# error EXIGE ver el archivo real — sin read_file el 4B alucina old_str y
# termina reescribiendo el archivo entero de memoria (destructivo, ver
# PacientesPage.tsx). SIN herramientas de búsqueda (list_files/search_code:
# el error ya viene inyectado, no hay que explorar) y SIN git-write (el fix
# no debe volver a commiteear).
GATE_RETRY_TOOLS = [
    read_file, edit_file, apply_patch, write_file, delete_file,
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
