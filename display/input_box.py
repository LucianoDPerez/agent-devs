"""Caja de input estilo opencode: fondo pintado, alto dinámico, click-to-edit.

Reemplaza al PromptSession simple:
- TextArea con FONDO en toda la caja (no solo el prefijo ›)
- Alto DINÁMICO: crece con cada línea que agregás (tope configurable)
- Click posiciona el cursor (mouse_support=True)
- Barra de estado SIEMPRE visible debajo de la caja mientras escribís
"""
from __future__ import annotations

import shutil
import sys

from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.styles import Style, merge_styles
from prompt_toolkit.widgets import TextArea

from display.tui import _make_toolbar, _STYLE

_MIN_LINES = 3
_MAX_LINES = 14


def _build_input_app(status: dict) -> Application:
    """App de input: caja TextArea pintada + barra de estado fija abajo."""
    input_field = TextArea(
        text="",
        multiline=True,
        wrap_lines=True,
        focus_on_click=True,
    )

    def _height():
        n = input_field.text.count("\n") + 1
        term_h = shutil.get_terminal_size().lines
        hard_max = max(5, min(_MAX_LINES, term_h - 8))
        preferred = min(max(_MIN_LINES, n + 1), hard_max)
        return Dimension(min=_MIN_LINES, max=hard_max, preferred=preferred)

    kb = KeyBindings()

    @kb.add("enter", eager=True)
    def _submit(event):
        if event.current_buffer.text.strip():
            event.app.exit(result=event.current_buffer.text)

    @kb.add("escape", "enter")
    def _newline(event):
        event.current_buffer.newline()

    @kb.add("c-c")
    def _interrupt(event):
        event.app.exit(exception=KeyboardInterrupt)

    toolbar = Window(
        content=FormattedTextControl(text=lambda: _make_toolbar(status)),
        height=1,
        dont_extend_height=True,
        style="class:toolbar",
    )

    layout = Layout(
        HSplit([
            Window(
                content=input_field.control,
                # height acepta callable → se re-evalúa en cada render:
                # la caja crece con cada línea nueva (auto-grow).
                height=_height,
                style="class:input-box",
                dont_extend_height=True,
            ),
            toolbar,
        ]),
        focused_element=input_field.control,
    )
    return Application(
        layout=layout,
        key_bindings=kb,
        # Estilos combinados: TUI base + fondo de toda la caja
        style=merge_styles([
            _STYLE,
            Style.from_dict({
                "input-box": "bg:#2a2a3e fg:#e0e0e0",
            }),
        ]),
        mouse_support=True,
        full_screen=False,
    )


def get_user_input_boxed(status: dict) -> str:
    """Input con caja pintada y alto dinámico. Devuelve el texto ingresado.

    Ctrl+C propaga KeyboardInterrupt (mismo contrato que el prompt anterior).
    """
    if not sys.stdin.isatty():
        return input("› ")
    app = _build_input_app(status)
    try:
        return app.run() or ""
    except KeyboardInterrupt:
        raise
