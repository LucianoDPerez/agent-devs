# tools

Pool de tools para los agentes. Cada tool vive en un módulo por **dominio**
(cohesión por responsabilidad técnica), no un archivo por tool.

## Módulos

| Módulo            | Tools                                                        |
|-------------------|--------------------------------------------------------------|
| `filesystem.py`   | `list_files`, `read_file`, `write_file`, `edit_file`        |
| `search.py`       | `search_code`                                               |
| `routes.py`       | `inspect_routes` (detección de endpoints multi-lenguaje)    |
| `verify.py`       | `run_lint`, `run_tests`, `run_build` (auto-detect Node/Python/Go) |
| `git.py`          | `current_branch`, `changed_files`, `git_status`, `git_log`, `stage_files`, `create_commit`, `push`, `create_pr`, `read_pr`, `list_prs` (vía `git` + `gh`) |
| `mcp_client.py`   | Proveedor: carga tools de `codebase-memory-mcp` expuestas como `cm__*` (graph on-demand, no en contexto) |
| `_helpers.py`     | `_is_excluded`, `_read_text` (internas compartidas)          |

## Índice (`__init__.py`)

Reexporta todas las tools y expone un pool **por tipo de agente** para
limitar el alcance de cada rol:

| Rol         | Tools incluidas                                              |
|-------------|-------------------------------------------------------------|
| `analyzer`  | `list_files`, `read_file`, `search_code`, `inspect_routes` + git read-only |
| `planner`   | + `write_file` + git read-only                             |
| `executor`  | todas (incluye `edit_file`, stage/commit/push/create_pr, verify) |
| `reviewer`  | solo lectura (filesystem sin `write_file`/`edit_file`) + `read_pr`, `list_prs` + verify |

## Uso

```python
from tools import get_tools, get_tools_for

pool = get_tools()            # completo (compatibilidad con AgentDevs)
planner = get_tools_for("planner")   # subset por rol
```

## Knowledge graph (codebase-memory-mcp) — `cm__*`

`mcp_client.py` levanta el servidor `codebase-memory-mcp` (stdio) al inicio del
agente y expone sus tools con prefijo `cm__` para evitar colisionar con las
locales (ej. `cm__search_code` vs `search_code`). Las más útiles para el agente:

| Tool `cm__*`              | Uso                                                              |
|---------------------------|------------------------------------------------------------------|
| `cm__search_graph`        | Buscar símbolos/clases/funciones por nombre o semántica          |
| `cm__trace_path`          | "¿Qué rompe si cambio X?" — callers/callees/imports              |
| `cm__get_code_snippet`    | Leer la implementación exacta de una función/clase              |
| `cm__get_architecture`    | Resumen estructural del repo                                     |
| `cm__list_projects`       | Listar repos indexados (para el param `project` de las demás)   |
| `cm__index_repository`    | Indexar/reindexar un repo en el graph                           |
| `cm__index_status`        | Estado del index de un repo (nodos/edges/status)                |
| `cm__detect_changes`      | Detectar cambios desde el último index                           |

El graph persiste en SQLite (`~/.codebase-memory/graph.db.zst`); **NO se
inyecta como texto al contexto del LLM** — las queries devuelven resultados
acotados on-demand, por eso no generan bloat de tokens.

## Contratos

- Todas las tools son `@tool` de langchain, con `ToolException` para errores
  y descriptivos siempre en inglés (la UI de AgentDevs los muestra).
- Resultados acotados por `MAX_FILE_READ_BYTES`, `MAX_LIST_RESULTS` y
  `MAX_SEARCH_RESULT_CHARS` (config.py) para no reventar el contexto del
  modelo local (`n_ctx=32000`).
- `inspect_routes` detecta endpoints en una sola llamada, cubriendo
  Next.js/Express/Fastify/NestJS, FastAPI/Flask/Django, Go, Rust, Java/Kotlin,
  PHP, C# y Ruby.