"""Output del agente con rich: reasoning dim, tool badges, metrics panel."""
from __future__ import annotations

import asyncio
import contextlib
import time

from langchain_core.messages import AIMessageChunk
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

console = Console()

# Marcadores de segmento markdown para la TUI full-screen: delimitan cada
# tramo de contenido del LLM (los segmentos quedan interrumpidos por tool
# calls) para que el pane reemplace el texto CRUDO por la versión renderizada
# (rich Markdown) al cerrar el segmento. \u2063 = INVISIBLE SEPARATOR: no
# colisiona con contenido real y sobrevive intacto a markup.escape() y a
# Text.from_ansi. En modo simple (sin --tui) se emiten igual: son ancho-cero.
MD_BEGIN = "\u2063md-begin\u2063"
MD_END = "\u2063md-end\u2063"
# Los marcadores SOLO tienen sentido cuando el pane de la TUI los intercepta.
# En modo simple son texto visible en la terminal ("md-begin") — por defecto
# OFF; run_fullscreen (--tui) los activa al entrar.
MD_MARKERS_ENABLED = False


def _md_begin() -> None:
    if MD_MARKERS_ENABLED:
        console.print(MD_BEGIN, end="", highlight=False, soft_wrap=True)


def _md_end() -> None:
    if MD_MARKERS_ENABLED:
        console.print(MD_END, end="", highlight=False, soft_wrap=True)


class ReasoningOnlyResponse(Exception):
    """Raised when the model produces ONLY reasoning_content with no actual output.

    The 4B model can consume its entire token budget on thinking, leaving
    zero tokens for content or tool_calls. This exception lets the session
    layer catch it and retry with a forceful instruction + trimmed context.
    """

    def __init__(self, reasoning_text: str = "", reason: str = "reasoning-only"):
        self.reasoning_text = reasoning_text
        self.reason = reason
        super().__init__(
            f"Model produced only reasoning ({len(reasoning_text)} chars) "
            f"with no content/tool_calls. Reason: {reason}. Turn should be retried."
        )


class ToolCallLimitExceeded(ReasoningOnlyResponse):
    """Raised when the total number of tool calls in a turn exceeds the limit.

    The 4B model can enter read-loops (reading the same file 7+ times)
    because LangChain's StructuredTool catches dedupe exceptions and returns
    them as error strings, which the 4B model ignores. This is a hard limit
    enforced at the streaming layer — cannot be ignored.
    """

    def __init__(self, total_calls: int, limit: int):
        self.total_calls = total_calls
        self.limit = limit
        super().__init__(
            f"Exceeded {limit} tool calls in one turn (made {total_calls})",
            reason="tool-call-limit",
        )


async def stream_agent_turn(agent, messages, config, idle_timeout: float | None = 120.0,
                            max_reasoning_seconds: float | None = None,
                            max_tool_calls: int | None = None,
                            require_write: bool = False,
                            max_content_seconds: float | None = None):
    """Ejecuta el agente con streaming. Reasoning en dim, response normal.

    Retorna el texto completo de la respuesta (sin razonamiento) para persistencia.
    Levanta ReasoningOnlyResponse si el modelo solo razonó (sin content/tool_calls).

    ``max_reasoning_seconds``: si el modelo lleva razonando más tiempo que este
    límite sin producir content/tool_calls, corta el stream.

    ``max_tool_calls``: límite duro de tool calls (contados por nombre) en toda
    la conversación del turno. Si se supera, corta el stream.

    ``max_content_seconds``: si el modelo lleva generando CONTENIDO (texto)
    continuamente más tiempo que este límite sin emitir una tool call, corta.
    El 4B a veces no emite EOS: termina su resumen final y SIGUE generando
    texto nuevo hasta el límite de max_tokens del servidor (turnos de 10+
    min colgados). El idle_timeout NO lo atrapa (el modelo sigue emitiendo
    chunks — nunca es idle).

    ``require_write`` (EXECUTE retry): si el turno termina sin haber hecho
    NINGÚN write_file/edit_file/delete_file, levanta ReasoningOnlyResponse.
    El 4B a veces "responde" con texto/monólogo sin escribir nada real.
    """
    reasoning_started = False
    response_started = False
    response_parts: list[str] = []
    reasoning_text: list[str] = []
    saw_tool_call = False
    produced_output = False
    reasoning_since: float | None = None
    total_tool_calls = 0
    tool_call_limit_hit = False
    wrote_something = False
    # Último tipo de output emitido (content | tool | None): gobierna los
    # saltos de línea entre transiciones texto↔tool-call para que nada
    # quede pegado en el pane.
    last_emitted: str | None = None
    # Segmento markdown ABIERTO (marcadores MD_BEGIN/MD_END para la TUI).
    md_open = False
    # Detección de LOOP DE TEXTO: el 4B a veces repite el MISMO bloque
    # infinitamente ("Would you like me to commit this change?" ×17). El
    # idle_timeout NO lo atrapa (el modelo sigue emitiendo chunks — nunca
    # es idle). Se detecta por sufijo repetido en el texto acumulado.
    loop_detected = False
    # Duración de generación continua de CONTENIDO (sin tool calls): el 4B
    # a veces no emite EOS y sigue generando texto nuevo indefinidamente.
    # Timer por bloque de contenido: arranca con el primer chunk de texto y
    # se resetea al emitir una tool call o razonamiento.
    content_since: float | None = None

    WRITE_NAMES = frozenset({"write_file", "edit_file", "delete_file", "stage_files", "create_commit"})

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

            if is_reasoning:
                # Contenido→razonamiento cierra el segmento markdown abierto
                # (el razonamiento va dim, fuera del render).
                if md_open:
                    _md_end()
                    md_open = False
                reasoning_text.append(chunk.additional_kwargs.get("reasoning_content") or "")
                # Timeout POR BLOQUE de razonamiento: el timer arranca cuando
                # empieza el bloque y se resetea al emitir output (content o
                # tool call). Si un MISMO bloque razona > límite sin emitir
                # nada, cortar. Antes el timer era global y tras el primer
                # output nunca más cortaba → el 4B razonaba 30-60s antes de
                # cada tool call sin límite (turnos de 400-500s).
                if reasoning_since is None:
                    reasoning_since = time.monotonic()
                    if not reasoning_started:
                        reasoning_started = True
                        console.print("💭 [dim]Razonando…[/dim]")
                # Empezó a razonar → reset del timer de contenido continuo.
                content_since = None
                if max_reasoning_seconds is not None:
                    elapsed = time.monotonic() - reasoning_since
                    if elapsed > max_reasoning_seconds:
                        console.print(
                            f"\n[dim]↻ Modelo pensando sin producir output "
                            f"({elapsed:.0f}s). Reintentando…[/dim]"
                        )
                        break
            else:
                if not response_started:
                    response_started = True
                    if reasoning_started:
                        console.print("\n", end="")
                    console.print("─" * 40, style="dim")
                if chunk.content:
                    # Transición tool→texto: cerrar la línea del eco del tool
                    # call ANTES del contenido. Sin esto la respuesta arrancaba
                    # PEGADA al JSON de args (🔧 inspect_routes{...}**Texto).
                    if last_emitted == "tool":
                        console.print("\n", end="", highlight=False)
                    if not md_open:
                        _md_begin()
                        md_open = True
                    # Timer de generación continua de contenido: arranca con
                    # el primer chunk y se resetea al razonar o emitir tool.
                    # Si el modelo lleva > max_content_seconds generando texto
                    # SIN tool calls, no emitió EOS (loop de generación).
                    if content_since is None:
                        content_since = time.monotonic()
                    elif max_content_seconds is not None:
                        elapsed = time.monotonic() - content_since
                        if elapsed > max_content_seconds:
                            console.print(
                                f"\n[dim]↻ Modelo generando texto continuo sin "
                                f"tool calls ({elapsed:.0f}s). Se corta.[/dim]"
                            )
                            break
                    console.print(escape(chunk.content), end="", highlight=False, soft_wrap=True)
                    last_emitted = "content"
                    response_parts.append(str(chunk.content))
                    produced_output = True
                    # Loop de texto: si el MISMO bloque de ~TEXT_REPEAT_WINDOW
                    # chars se repite N veces CONSECUTIVAS, cortar. Se comparan
                    # bloques completos de tamaño fijo en posiciones contiguas
                    # (el 4B repite el mismo párrafo textualmente).
                    text_so_far = "".join(response_parts)
                    # Loop de texto: el 4B repite el MISMO párrafo infinitamente
                    # ("Would you like me to commit this change?" ×17 en E2E).
                    # Detección por SUFIJO: el último bloque de ~N chars del
                    # texto acumulado que ya apareció 2+ veces ANTES = el modelo
                    # está re-emitiendo el mismo contenido. Ventanas múltiples
                    # para cubrir párrafos cortos y largos.
                    if len(text_so_far) >= 512:
                        loop_detected = False
                        for w in (64, 128, 256):
                            window = text_so_far[-w:]
                            if text_so_far[:-w].count(window) >= 2:
                                console.print(
                                    "\n\n[dim]↻ Modelo repitiendo el mismo texto "
                                    "(loop). Se corta el turno.[/dim]"
                                )
                                loop_detected = True
                                break
                        if loop_detected:
                            break

                for tc in chunk.tool_call_chunks or []:
                    if tc.get("name"):
                        # Contenido→tool cierra el segmento markdown abierto.
                        if md_open:
                            _md_end()
                            md_open = False
                        total_tool_calls += 1
                        if tc["name"] in WRITE_NAMES:
                            wrote_something = True
                        # Tool call = el modelo volvió a la acción: reset del
                        # timer de contenido continuo.
                        content_since = None
                        # Cada tool call va en SU PROPIA línea (el \n acá cubre
                        # texto→tool y tool→tool). El cierre a fin de args lo
                        # hace la transición tool→texto / el final del stream.
                        if last_emitted == "content":
                            console.print("\n", end="", highlight=False)
                        console.print(f"\n  [bold blue]🔧 {tc['name']}[/bold blue]", end="", highlight=False)
                        last_emitted = "tool"
                        if max_tool_calls is not None and total_tool_calls > max_tool_calls:
                            tool_call_limit_hit = True
                            break
                    if tc.get("args"):
                        console.print(f"[blue]{escape(tc['args'])}[/blue]", end="", highlight=False)
                    saw_tool_call = True
                    produced_output = True
                if produced_output:
                    # Reset del timer: el próximo bloque de razonamiento
                    # arranca con presupuesto fresco.
                    reasoning_since = None
                if tool_call_limit_hit:
                    break
    except StopAsyncIteration:
        pass
    finally:
        # Cerrar segmento markdown si quedó abierto (fin de stream / break).
        if md_open:
            _md_end()
        console.print("\n")
        with contextlib.suppress(Exception):
            await stream.aclose()

    if tool_call_limit_hit:
        console.print(
            f"\n[red]⛔ Límite de {max_tool_calls} tool calls alcanzado en un turno. "
            "El 4B entró en loop. Corte forzado → retry con contexto recortado.[/red]",
            style="red",
        )
        raise ToolCallLimitExceeded(total_tool_calls, max_tool_calls)

    if reasoning_started and not produced_output:
        raise ReasoningOnlyResponse("".join(reasoning_text))

    if require_write and not wrote_something:
        console.print(
            "\n[dim]↻ El modelo terminó su turno sin escribir cambios. "
            "Reintentando con foco en escritura…[/dim]"
        )
        raise ReasoningOnlyResponse("".join(response_parts), reason="no-write")

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


def print_welcome(repo_path: str, model: str, url: str, temp: float, tool_counts: tuple, branch: str = ""):
    from config import LLM_MAX_TOKENS
    local, mcp = tool_counts
    repo_line = f"[bold cyan]📁 Repo:[/bold cyan] {repo_path}"
    if branch:
        repo_line += f"  [bold green]🌿 {branch}[/bold green]"
    info = (
        f"[bold cyan]🔌 LLM:[/bold cyan] {url}\n"
        f"{repo_line}\n"
        f"[bold cyan]⚡ Modelo:[/bold cyan] {model} | Temp: {temp} | Max: {LLM_MAX_TOKENS}\n"
        f"[bold cyan]🛠️  Tools:[/bold cyan] {local} locales + {mcp} graph (cm__*)"
    )
    console.print(Panel(info, title="[bold]AgentDevs[/bold]", border_style="cyan", padding=(0, 1)))