from __future__ import annotations

from langchain.agents import create_agent

from config import EXECUTE_MAX_TOKENS, REVIEW_MAX_TOKENS
from core.roles import Role, load_prompt, tools_for_role
from orchestration.tool_dedupe import ExploreBudget, ToolCallDedupe, wrap_tools_with_dedupe
from tools.mcp_client import load_mcp_tools, mcp_tool_count

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
) -> tuple:
    """Construye un agente LangChain con tools y prompt del rol indicado.

    Devuelve (agent, local_tool_count, mcp_count_for_role).
    Explore budget solo aplica a EXECUTE (pasarlo None en otros roles).
    """
    local_tools = tools_for_role(role)
    role_mcp = _mcp_for_role(role, mcp_tools)
    all_tools = role_mcp + local_tools
    if dedupe is not None:
        # EXECUTE y REVIEW reciben explore_budget (evita loops infinitos del 4B)
        budget = explore_budget if role in (Role.EXECUTE, Role.REVIEW) else None
        all_tools = wrap_tools_with_dedupe(all_tools, dedupe, budget)

    prompt_template = load_prompt(role)
    extra_context = ""
    if cached_analysis:
        extra_context = (
            f"ANÁLISIS CACHÉ (usalo como contexto base; no re-explores lo resumido):\n"
            f"{cached_analysis}\n"
        )
    system_prompt = prompt_template.format(
        repo_path=repo_path,
        extra_context=extra_context,
    )

    role_llm = llm
    update_kwargs = {}
    target_temp = _ROLE_TEMPERATURE.get(role)
    if target_temp is not None and getattr(llm, "temperature", None) != target_temp:
        update_kwargs["temperature"] = target_temp
    if role == Role.EXECUTE:
        update_kwargs["max_tokens"] = EXECUTE_MAX_TOKENS
    elif role == Role.REVIEW:
        update_kwargs["max_tokens"] = REVIEW_MAX_TOKENS
    if update_kwargs:
        role_llm = llm.model_copy(update=update_kwargs, deep=False)

    agent = create_agent(role_llm, all_tools, system_prompt=system_prompt)
    return agent, len(local_tools), len(role_mcp)
