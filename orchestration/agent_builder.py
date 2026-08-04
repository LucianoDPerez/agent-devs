from __future__ import annotations

from langchain.agents import create_agent

from core.roles import Role, load_prompt, tools_for_role
from tools.mcp_client import load_mcp_tools, mcp_tool_count


async def init_mcp() -> tuple[list, int]:
    """Conecta MCP y devuelve (tools, count)."""
    tools = await load_mcp_tools()
    return tools, mcp_tool_count()


async def build_agent(llm, role: Role, repo_path: str, cached_analysis: str = "", mcp_tools: list | None = None) -> tuple:
    """Construye un agente LangChain con tools y prompt del rol indicado.

    Devuelve (agent, local_tool_count).
    """
    local_tools = tools_for_role(role)
    all_tools = (mcp_tools or []) + local_tools

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

    agent = create_agent(llm, all_tools, system_prompt=system_prompt)
    return agent, len(local_tools)