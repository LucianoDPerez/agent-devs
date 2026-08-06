from __future__ import annotations

from langchain.agents import create_agent

from config import (
    EXECUTE_MAX_TOKENS,
    EXECUTE_MAX_REASONING_TOKENS,
    REVIEW_MAX_TOKENS,
    REVIEW_MAX_REASONING_TOKENS,
)
from core.roles import Role, load_prompt, tools_for_role
from orchestration.framework_rules import inject_framework_rules
from orchestration.tool_dedupe import ExploreBudget, ToolCallDedupe, wrap_tools_with_dedupe
from tools.mcp_client import load_mcp_tools, mcp_tool_count
from tools import WRITE_ONLY_TOOLS

# Solo ANALYZE/PLAN usan MCP. EXECUTE y REVIEW van locales-only:
# el 4B con 27+ schemas entra en loops y tool calls basura.
_ROLES_WITH_MCP = {Role.ANALYZE, Role.PLAN}

_ROLE_TEMPERATURE = {
    Role.EXECUTE: 0.2,
    Role.REVIEW: 0.3,
    Role.PLAN: 0.4,
    Role.ANALYZE: 0.5,
    Role.CHAT: 0.7,
}


async def init_mcp() -> tuple[list, int]:
    """Conecta MCP y devuelve (tools, count)."""
    tools = await load_mcp_tools()
    return tools, mcp_tool_count()


def _mcp_for_role(role: Role, mcp_tools: list | None) -> list:
    if role not in _ROLES_WITH_MCP:
        return []
    return list(mcp_tools or [])


async def build_agent(
    llm,
    role: Role,
    repo_path: str,
    cached_analysis: str = "",
    mcp_tools: list | None = None,
    dedupe: ToolCallDedupe | None = None,
    explore_budget: ExploreBudget | None = None,
    force_write: bool = False,
    read_cache: dict | None = None,
) -> tuple:
    """Construye un agente LangChain con tools y prompt del rol indicado.

    Devuelve (agent, local_tool_count, mcp_count_for_role).

    ``force_write=True`` (EXECUTE retry): usa SOLO tools de escritura
    (write/edit/delete) + git-write + verify. Sin read_file/list_files/
    search_code: el 4B entra en loops de lectura infinitos si las tiene
    disponibles, en vez de escribir. El contexto de tareas YA está en el
    prompt, así que no necesita leer más.

    ``read_cache`` (dict path→content): si se provee, read_file guarda su
    contenido ahí. El retry write-only lo inyecta como anclaje para que el
    modelo reescriba archivos sin leer.
    """
    local_tools = WRITE_ONLY_TOOLS if force_write else tools_for_role(role)
    role_mcp = _mcp_for_role(role, mcp_tools)
    all_tools = role_mcp + local_tools
    if dedupe is not None:
        # EXECUTE y REVIEW reciben explore_budget (evita loops infinitos del 4B)
        budget = explore_budget if role in (Role.EXECUTE, Role.REVIEW) else None
        all_tools = wrap_tools_with_dedupe(all_tools, dedupe, budget, read_cache)

    prompt_template = load_prompt(role)
    extra_context = ""
    if cached_analysis:
        extra_context = (
            f"ANÁLISIS CACHÉ (usalo como contexto base; no re-explores lo resumido):\n"
            f"{cached_analysis}\n"
        )
    fw_rules = inject_framework_rules(repo_path)
    if force_write:
        extra_context += (
            "\n⛔ RETRY TRAS LOOP DE LECTURA: NO tenés tools de lectura "
            "(read_file/list_files/search_code). El contexto de la tarea y el "
            "layout del repo YA están en el mensaje. ESCRIBÍ el código AHORA: "
            "si un archivo existe, REEISCRIBILO completo con write_file "
            "(no uses edit_file, no conocés el texto exacto). No intentes leer.\n"
        )
    system_prompt = prompt_template.format(
        repo_path=repo_path,
        framework_rules=fw_rules,
        extra_context=extra_context,
    )

    role_llm = llm
    update_kwargs = {}
    target_temp = _ROLE_TEMPERATURE.get(role)
    if target_temp is not None and getattr(llm, "temperature", None) != target_temp:
        update_kwargs["temperature"] = target_temp
    if role == Role.EXECUTE:
        update_kwargs["max_tokens"] = EXECUTE_MAX_TOKENS
        update_kwargs["max_reasoning_tokens"] = EXECUTE_MAX_REASONING_TOKENS
    elif role == Role.REVIEW:
        update_kwargs["max_tokens"] = REVIEW_MAX_TOKENS
        update_kwargs["max_reasoning_tokens"] = REVIEW_MAX_REASONING_TOKENS
    if update_kwargs:
        role_llm = llm.model_copy(update=update_kwargs, deep=False)
    if force_write:
        # Retry write-only: obliga al modelo a emitir tool calls siempre
        # (no puede responder con texto plano / monólogo circular).
        role_llm = role_llm.model_copy(update={"force_tool_calls": True}, deep=False)

    agent = create_agent(role_llm, all_tools, system_prompt=system_prompt)
    return agent, len(local_tools), len(role_mcp)
