"""Proveedor de herramientas MCP para conectar código-memory-mcp al agente.

El servidor `codebase-memory-mcp` (script `~/.local/bin/codebase-memory-mcp`)
expone un knowledge graph persistente (SQLite: ~/.codebase-memory/graph.db.zst)
con tools como `search_graph`, `trace_path`, `get_code_snippet`,
`get_architecture`.

Las tools NO se inyectan como texto al contexto del LLM: se consultan
on-demand. El grafo persiste en disco; el contexto solo recibe el resultado
acotado de cada query. Por eso no genera bloat de tokens.

Este módulo expone:
- load_mcp_tools(): abre el server stdio, devuelve list[BaseTool] con prefijo
  `cm__` para evitar colisiones con las tools locales (ej. search_code).
- close_mcp_client(): cierra sessions del server stdio.
- mcp_tool_count(): helper sync para reportar cuántas tools hay cargadas.

Ciclo de vida: se crea una sola vez al inicio del agente interactivo y se
mantiene vivo toda la sesión (el server stdio permanece levantado entre
turnos para no reconectar en cada pregunta). Al salir, close_mcp_client()
cierra sessions. Los subprocesos stdio hijos se terminan al salir de Python
(heredados por este proceso) — por diseño no matamos procesos ajenos.
"""

import shutil

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StdioConnection

_SERVER_LABEL = "codebase-memory"
_TOOL_PREFIX = "cm__"

_client: MultiServerMCPClient | None = None
_tools: list[BaseTool] | None = None


def _binary() -> str | None:
    return shutil.which("codebase-memory-mcp")


def _prefixed(tools: list[BaseTool]) -> list[BaseTool]:
    """Renombra tools con `cm__` para no colisionar con tools locales.

    codebase-memory incluye `search_code`, que colisiona con `tools/search.py`.
    El prefijo desenmascara al LLM y evita ambigüedades en la tool choice.
    """
    return [t.model_copy(update={"name": _TOOL_PREFIX + t.name}) for t in tools]


async def load_mcp_tools() -> list[BaseTool]:
    """Abre codebase-memory-mcp (stdio) y devuelve sus tools como BaseTool.

    Requiere un bucle de eventos activo (asyncio.run / loop.run_until_complete).
    FileNotFoundError si el binario no está en PATH.
    La caché `_tools` evita relevantar el server entre turnos.
    """
    global _client, _tools
    if _tools is not None:
        return _tools

    binary = _binary()
    if not binary:
        raise FileNotFoundError(
            "codebase-memory-mcp not found in PATH. Install/ensure it is on PATH "
            "(~/.local/bin/codebase-memory-mcp)."
        )

    client = MultiServerMCPClient({
        _SERVER_LABEL: StdioConnection(command=binary, args=[], transport="stdio"),
    })
    try:
        tools = await client.get_tools()
    except Exception:
        raise

    _client = client
    _tools = _prefixed(tools)
    return _tools


async def close_mcp_client() -> None:
    """Cierra las sessions del server stdio y limpia el estado del módulo.

    Langchain-mcp-adapters 0.3.1 no soporta __aexit__ como async context
    manager (NotImplementedError), así que delegamos el cierre definitivo de los
    subprocesos stdio al exit de Python (heredados por este proceso).
    """
    global _client, _tools
    _client = None
    _tools = None


def mcp_tool_count() -> int:
    """Sync helper: cuántas tools MCP están cargadas actualmente (para prints)."""
    return len(_tools) if _tools is not None else 0
