"""Barra de estado sticky durante el turno (estilo opencode).

rich.Live fija la barra AL PIE mientras corre run_turn: todo lo que imprime
el turno usa el MISMO console, así que scrollea por ARRIBA de la barra y esta
se refresca en vivo (tokens acumulados + spinner). Al terminar queda impresa
como separador del turno. Nunca desaparece durante el procesamiento.
"""
from __future__ import annotations

import time

from rich.live import Live
from rich.text import Text

from display.console import console
from llm_wrapper import get_usage

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _render_bar(status: dict) -> Text:
    usage = get_usage()
    s = usage["session"]
    total = s["prompt"] + s["completion"]
    spin = _SPINNER[int(time.monotonic() * 4) % len(_SPINNER)]
    repo = (status.get("repo") or "-").rstrip("/").split("/")[-1]
    return Text.assemble(
        (f" {spin} ", "bold magenta"),
        (status.get("role", "🔍"), "bold cyan"),
        (" · ", "dim"),
        (f"🌿 {status.get('branch', '-')}", "green"),
        (" · ", "dim"),
        (f"⚡ {total:,} tokens", "yellow"),
        (" · ", "dim"),
        (f"📁 {repo}", "blue"),
        ("  —  ESC cancela el turno", "bright_black"),
    )


def run_turn_with_sticky_bar(session, user_input: str) -> None:
    """Ejecuta run_turn con la barra de estado clavada abajo."""
    try:
        status = session.get_status()
    except Exception:
        status = {}
    with Live(
        _render_bar(status),
        get_renderable=lambda: _render_bar(status),
        refresh_per_second=4,
        console=console,
        # transient=True: al terminar el turno la barra SE BORRA (no queda
        # impresa duplicando visualmente la toolbar del siguiente input).
        # Los totales del turno ya los reporta print_turn_summary.
        transient=True,
    ):
        session.run_turn(user_input)
