"""TUI con prompt_toolkit: input con fondo gris, multilinea, word wrap.

- Enter = enviar (usa validate_and_handle)
- ⌥+Enter (Option+Enter) = salto de línea
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
        ("class:hint", "Enter=envía · ⌥+Enter=salto · ESC=cancela el turno · Ctrl+C=sale · exit=sale"),
    ])


def _prompt_text() -> FormattedText:
    return FormattedText([
        ("class:input-area", "\n  › "),
    ])


def _continuation(width: int, line_number: int, soft: bool) -> str:
    return "    " + " " * 0  # indent continuation lines


def get_user_input(status: dict) -> str:
    """Muestra el prompt con fondo gris, multilinea, word wrap y status bar."""
    global _session
    if not sys.stdin.isatty():
        return input("› ")

    if _session is None:
        _session = PromptSession(
            key_bindings=_make_keybindings(),
            multiline=True,
            wrap_lines=True,
            mouse_support=False,
            history=_history,
            style=_STYLE,
            prompt_continuation=_continuation,
        )

    return _session.prompt(
        _prompt_text(),
        bottom_toolbar=lambda: _make_toolbar(status),
    )