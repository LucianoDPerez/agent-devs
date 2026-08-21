"""Detección de variables de entorno requeridas por el proyecto, en una llamada.

Responde "¿qué env vars necesita para correr?" leyendo SOLO archivos de
ejemplo (.env.example / .env.template / .env.sample). NUNCA lee `.env` ni
`.env.local`: ahí viven secretos reales y esta tool es para onboarding,
no para filtrarlos al contexto del LLM.
"""

import re
from pathlib import Path

from langchain_core.tools import tool

from config import MAX_SEARCH_RESULT_CHARS

from ._helpers import _is_excluded, _read_text

_ENV_EXAMPLE_NAMES = {".env.example", ".env.template", ".env.sample", ".env.example.local"}
_KEY_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.MULTILINE)

# Nunca leer estos: contienen valores reales.
_FORBIDDEN = {".env", ".env.local", ".env.development", ".env.production"}


@tool
def inspect_env(path: str) -> str:
    """
    List environment variables the project expects, reading ONLY example files
    (.env.example / .env.template / .env.sample) at any depth. Real .env files
    are never read. Returns KEY=placeholder grouped by file, so you know which
    vars to configure before running the project.
    Usage: inspect_env(path="/Users/me/repo")
    """
    root = Path(path)
    if not root.exists():
        return (
            f"Path does not exist: {path}. "
            "Do not retry this path. Use list_files on a parent that exists."
        )

    groups: dict[str, list[str]] = {}
    used = 0
    truncated = False

    for p in sorted(root.rglob(".env*")):
        if _is_excluded(p) or not p.is_file():
            continue
        if p.name in _FORBIDDEN or p.name not in _ENV_EXAMPLE_NAMES:
            continue
        rel = p.relative_to(root).as_posix()
        text = _read_text(p)
        if not text:
            continue
        entries = []
        for key, value in _KEY_LINE_RE.findall(text):
            value = value.strip().strip('"').strip("'")
            if len(value) > 40:
                value = value[:37] + "..."
            entries.append(f"{key}={value}" if value else key)
            if used + len(key) > MAX_SEARCH_RESULT_CHARS:
                truncated = True
                break
        if entries:
            groups[rel] = entries

    parts = []
    for rel, keys in groups.items():
        parts.append(f"{rel}:\n" + "\n".join(keys))
    if truncated:
        parts.append("... (truncado)")
    if not parts:
        return (
            f"No se encontraron archivos de ejemplo de entorno en {path} "
            "(.env.example / .env.template / .env.sample)"
        )
    return "\n\n".join(parts)
