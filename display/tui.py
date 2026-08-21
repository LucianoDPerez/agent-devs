"""TUI con prompt_toolkit: input con fondo gris, multilinea, word wrap.

- Enter = enviar (usa validate_and_handle)
- ⌥+Enter (Option+Enter) = salto de línea
- CLICK en cualquier parte del texto = mover el cursor ahí (editar/insertar/
  borrar desde esa posición, como opencode)
- Fondo gris oscuro en el área de input
- Mínimo 2 líneas visibles
- Word wrap automático para líneas largas
- Status bar inferior con branch, tokens, rol, repo
"""
from __future__ import annotations

import sys
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style

_history = InMemoryHistory()
_session: PromptSession | None = None

_STYLE = Style.from_dict({
    "prompt": "fg:ansicyan bold",
    "input-area": "bg:#2a2a3e fg:#e0e0e0",
    "toolbar": "bg:#1a1a2e fg:#e0e0e0",
    "branch": "fg:ansigreen bold",
    "tokens": "fg:ansiyellow",
    "role": "fg:ansicyan bold",
    "repo": "fg:ansiblue",
    "hint": "fg:ansibrightblack",
})


def _make_keybindings() -> KeyBindings:
    kb = KeyBindings()

    @kb.add("enter", eager=True)
    def _submit(event):
        buf = event.current_buffer
        if buf.text.strip():
            buf.validate_and_handle()

    @kb.add("escape", "enter")
    def _newline(event):
        event.current_buffer.newline()

    return kb


def _short_repo(repo_path: str) -> str:
    return Path(repo_path).name if repo_path else "-"


def _make_toolbar(status: dict) -> FormattedText:
    return FormattedText([
        ("class:toolbar", " "),
        ("class:branch", f"🌿 {status.get('branch', '-')}"),
        ("class:toolbar", "  "),
        ("class:tokens", f"⚡ {status.get('tokens', 0):,} tokens"),
        ("class:toolbar", "  "),
        ("class:role", f"{status.get('role', '🔍')}"),
        ("class:toolbar", "  "),
        ("class:repo", f"📁 {_short_repo(status.get('repo', ''))}"),
        ("class:toolbar", "  "),
        ("class:hint", "Enter=envía · ⌥+Enter=salto · Click=posiciona cursor · ESC=cancela el turno · Ctrl+C=sale · exit=sale"),
    ])


def _prompt_text() -> FormattedText:
    return FormattedText([
        ("class:input-area", "\n  › "),
    ])


def _continuation(width: int, line_number: int, soft: bool) -> str:
    return "    " + " " * 0  # indent continuation lines


def get_user_input(status: dict) -> str:
    """Caja pintada con fondo, alto dinámico y click-to-edit (display/input_box).

    Fallback a input() plano si stdin no es TTY (tests/scripts).
    """
    from display.input_box import get_user_input_boxed
    return get_user_input_boxed(status)