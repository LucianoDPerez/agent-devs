"""Output del agente con rich: reasoning dim, tool badges, metrics panel."""
from __future__ import annotations

import asyncio
import contextlib

from langchain_core.messages import AIMessageChunk
from rich.console import Console
from rich.panel import Panel

console = Console()


async def stream_agent_turn(agent, messages, config, idle_timeout: float | None = 120.0):
    """Ejecuta el agente con streaming. Reasoning en dim, response normal.

    Retorna el texto completo de la respuesta (sin razonamiento) para persistencia.
    """
    reasoning_started = False
    response_started = False
    response_parts: list[str] = []

    stream = agent.astream(
        {"messages": messages},
        stream_mode="messages",
        config=config,
    )
    try:
        while True:
            if idle_timeout:
                try:
                    item = await asyncio.wait_for(stream.__anext__(), idle_timeout)
                except asyncio.TimeoutError:
                    console.print("\n⏱️  Sin actividad del modelo; se corta aquí.", style="yellow")
                    break
            else:
                item = await stream.__anext__()

            if isinstance(item, tuple) and len(item) >= 2:
                chunk, _meta = item
            else:
                chunk = item

            if not isinstance(chunk, AIMessageChunk):
                continue

            is_reasoning = bool(chunk.additional_kwargs.get("is_reasoning"))

            if is_reasoning and not reasoning_started:
                reasoning_started = True
                console.print("💭 [dim]Razonando…[/dim]")

            if is_reasoning:
                if chunk.content:
                    console.print(chunk.content, end="", style="dim cyan", highlight=False, soft_wrap=True)
            else:
                if not response_started:
                    response_started = True
                    if reasoning_started:
                        console.print("\n", end="")
                    console.print("─" * 40, style="dim")
                if chunk.content:
                    console.print(chunk.content, end="", highlight=False, soft_wrap=True)
                    response_parts.append(str(chunk.content))

                for tc in chunk.tool_call_chunks or []:
                    if tc.get("name"):
                        console.print(f"\n  [bold blue]🔧 {tc['name']}[/bold blue]", end="", highlight=False)
                    if tc.get("args"):
                        console.print(f"[blue]{tc['args']}[/blue]", end="", highlight=False)
    except StopAsyncIteration:
        pass
    finally:
        console.print("\n")
        with contextlib.suppress(Exception):
            await stream.aclose()

    return "".join(response_parts)


def print_turn_summary(elapsed: float, interrupted: bool, session_time: float,
                       interrupt_source: str | None = None):
    from llm_wrapper import get_usage

    usage = get_usage()
    t, s = usage["turn"], usage["session"]
    total = t["prompt"] + t["completion"]
    s_total = s["prompt"] + s["completion"]

    lines = []
    if interrupted:
        src = interrupt_source or "Ctrl+C"
        lines.append(f"⏹️  [yellow]Iteración cancelada ({src}).[/yellow]")
    cached_str = f", [green]{t['cached']:,} reutilizados[/green]" if t["cached"] else ""
    lines.append(f"⚡ Pregunta: [bold]{elapsed:.1f}s[/bold] | {total:,} tokens "
                 f"(in {t['prompt']:,} + out {t['completion']:,}{cached_str})")
    lines.append(f"📊 Sesión: {session_time:.1f}s | {s_total:,} tokens "
                 f"(in {s['prompt']:,} + out {s['completion']:,})")

    console.print(Panel("\n".join(lines), border_style="dim", padding=(0, 1)))
    console.print()


def print_role_switch(role_label: str, local_count: int, mcp_count: int):
    console.print(f"\n[{role_label}] [dim]Tools: {local_count} locales + {mcp_count} graph[/dim]")


def print_welcome(repo_path: str, model: str, url: str, temp: float, tool_counts: tuple):
    from config import LLM_MAX_TOKENS
    local, mcp = tool_counts
    info = (
        f"[bold cyan]🔌 LLM:[/bold cyan] {url}\n"
        f"[bold cyan]📁 Repo:[/bold cyan] {repo_path}\n"
        f"[bold cyan]⚡ Modelo:[/bold cyan] {model} | Temp: {temp} | Max: {LLM_MAX_TOKENS}\n"
        f"[bold cyan]🛠️  Tools:[/bold cyan] {local} locales + {mcp} graph (cm__*)"
    )
    console.print(Panel(info, title="[bold]AgentDevs[/bold]", border_style="cyan", padding=(0, 1)))