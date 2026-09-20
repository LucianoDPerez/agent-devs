"""Registro central de slash commands: nombres, descripciones y matching.

Una sola fuente de verdad para:
- el autocompletado del input encajonado (prompt_toolkit, modo simple)
- la tira de sugerencias de la TUI fullscreen (Textual)
- el despacho en main.py (ambos modos) y /help

Comandos (descripción breve, la que ve el usuario en el menú):
- /new      Inicia una nueva sesión (limpia historial y panel)
- /compact  Resume el historial para liberar contexto
- /history  Muestra los últimos turnos guardados con su id
- /resume   Retoma una sesión anterior: /resume <id>
- /help     Muestra esta ayuda de comandos
"""
from __future__ import annotations

# (nombre, descripción breve, lleva_argumentos)
COMMANDS: tuple[tuple[str, str, bool], ...] = (
    ("/new", "Inicia una nueva sesión (limpia historial y panel)", False),
    ("/compact", "Resume el historial para liberar contexto", False),
    ("/history", "Muestra los últimos turnos guardados con su id", False),
    ("/resume", "Retoma una sesión anterior: /resume <id>", True),
    ("/autoapprove", "Aprueba escrituras sin preguntar: /autoapprove [on|off]", True),
    ("/verify", "Corre lint/tests/build del repo ahora", False),
    ("/tasks-pool", "Pool autónomo: /tasks-pool T001-T010 [archivo] [--commit]", True),
    ("/commit", "Lista pendientes o commitea: /commit | /commit todo|sesion [mensaje|ai]", True),
    ("/push", "Pushea la rama actual: /push [remote]", True),
    ("/pr", "Abre PR de la rama actual con gh: /pr [base]", True),
    ("/help", "Muestra esta ayuda de comandos", False),
)

_NAMES = [c[0] for c in COMMANDS]
_TAKES_ARGS = {c[0] for c in COMMANDS if c[2]}


def command_names() -> list[str]:
    return list(_NAMES)


def takes_args(name: str) -> bool:
    return name in _TAKES_ARGS


def match_commands(prefix: str) -> list[tuple[str, str]]:
    """Comandos cuyo nombre empieza con `prefix` (insensible a mayúsculas)."""
    p = prefix.lower()
    return [(n, d) for (n, d, _) in COMMANDS if n.startswith(p)]


def format_help(matches: list[tuple[str, str]] | None = None) -> str:
    """Una línea por comando: '  /new      descripción'."""
    items = matches if matches is not None else [(n, d) for (n, d, _) in COMMANDS]
    if not items:
        return "  (sin coincidencias)"
    return "\n".join(f"  {n:<9} {d}" for n, d in items)


def interpret_slash(text: str) -> tuple[str, object]:
    """Clasifica un input. Retorna (kind, payload):

    - ("message", None): texto normal → va al LLM.
    - ("run", (name, arg)): comando exacto (o abreviatura única sin args).
    - ("help", None): "/" o "/help" → mostrar la ayuda.
    - ("hint", matches): parcial ambiguo o desconocido → mostrar sugerencias.
    """
    stripped = text.strip()
    if not stripped.startswith("/"):
        return ("message", None)
    token = stripped.split()[0].lower()
    if "/" in token[1:]:
        # "/api/health..." es contenido (paths del repo), no comando.
        return ("message", None)
    if token == "/":
        return ("help", None)
    if token in _NAMES:
        arg = stripped[len(token):].strip()
        return ("run", (token, arg))
    matches = match_commands(token)
    if len(matches) == 1 and not takes_args(matches[0][0]):
        # Abreviatura única de un comando sin args ("/his" → /history).
        return ("run", (matches[0][0], ""))
    return ("hint", matches)


class SlashCompleter:
    """Completer propio: WordCompleter parte el texto por '/' y nunca matchea.

    Completa el primer token cuando empieza con '/' (una sola línea, cursor
    al final). Menú flotante con descripción; Enter acepta, Esc lo cierra.
    """

    def get_completions(self, document, complete_event):
        if not document.is_cursor_at_the_end:
            return
        text = document.text
        if "\n" in text:
            return
        stripped = text.strip()
        if not stripped.startswith("/"):
            return
        token = stripped.split()[0]
        if "/" in token[1:]:
            return
        for name, desc in match_commands(token if token != "/" else "/"):
            from prompt_toolkit.completion import Completion

            yield Completion(
                name,
                start_position=-len(token),
                display=name,
                display_meta=desc,
            )


def build_ptk_completer():
    """Completer para el input encajonado (prompt_toolkit)."""
    return SlashCompleter()
