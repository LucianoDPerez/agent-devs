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
    git_status,
    list_prs,
    push,
    read_pr,
    stage_files,
)
from .routes import inspect_routes
from .search import search_code
from .verify import run_build, run_install, run_lint, run_tests

ALL_TOOLS = [
    list_files, read_file, write_file, edit_file, delete_file, search_code, inspect_routes,
    run_install, run_lint, run_tests, run_build,
    current_branch, changed_files, git_status, git_log,
    stage_files, create_commit, push, create_pr, read_pr, list_prs,
]

_READONLY_GIT = [current_branch, changed_files, git_status, git_log, read_pr, list_prs]
_GIT_WRITE = [stage_files, create_commit, push, create_pr]
_VERIFY = [run_install, run_lint, run_tests, run_build]

# Subsets por rol de agente. Read-only evita que compile modificadores.
ANALYZER_TOOLS = [list_files, read_file, search_code, inspect_routes, *_READONLY_GIT]
PLANNER_TOOLS = [list_files, read_file, search_code, inspect_routes, write_file, *_READONLY_GIT]
EXECUTOR_TOOLS = list(ALL_TOOLS)
REVIEWER_TOOLS = [list_files, read_file, search_code, inspect_routes, *_READONLY_GIT, *_VERIFY]

# Retry de EXECUTE tras loop de lectura: SOLO escritura + git-write + verify.
# El 4B entra en loops de read_file infinitos si las tiene; sin ellas escribe.
WRITE_ONLY_TOOLS = [
    write_file, edit_file, delete_file,
    *_GIT_WRITE, *_VERIFY,
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
