"""Búsqueda de código por patrón regex en el repositorio."""

import re
from pathlib import Path

from langchain_core.tools import tool

from config import MAX_LINE_CHARS, MAX_SEARCH_RESULT_CHARS

from ._helpers import _is_excluded


@tool
def search_code(path: str, pattern: str) -> str:
    """
    Search for a regex pattern in all files under a directory.
    Returns matching file paths and line numbers with context.
    Usage: search_code(path="/Users/me/repo", pattern="TODO|FIXME")
    """
    root = Path(path)
    if not root.exists():
        return (
            f"Path does not exist: {path}. "
            "Accept this and continue — do not retry variants of this path."
        )

    compiled = re.compile(pattern, re.IGNORECASE)
    matches: list[str] = []
    total = 0
    used_chars = 0
    result_truncated = False

    targets = list(root.rglob("*")) if root.is_dir() else [root]
    for entry in targets:
        if _is_excluded(entry) or not entry.is_file():
            continue

        skip_ext = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip",
                    ".tar", ".gz", ".mp4", ".mp3", ".wav", ".ogg"}
        if entry.suffix.lower() in skip_ext or entry.name.endswith(".tsbuildinfo"):
            continue

        try:
            lines = entry.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue

        for i, line in enumerate(lines, 1):
            if compiled.search(line):
                total += 1
                if total > 100:
                    break
                rel = entry.relative_to(root if root.is_dir() else root.parent)
                text = line.strip()
                if len(text) > MAX_LINE_CHARS:
                    text = text[:MAX_LINE_CHARS] + "…"
                entry_str = f"{rel}:{i}: {text}"
                entry_chars = len(entry_str) + 1
                if used_chars + entry_chars > MAX_SEARCH_RESULT_CHARS:
                    result_truncated = True
                    break
                matches.append(entry_str)
                used_chars += entry_chars
        if result_truncated or total > 100:
            break

    if result_truncated:
        matches.append(
            f"... (resultado truncado a {MAX_SEARCH_RESULT_CHARS:,} caracteres)"
        )

    if not matches:
        return f"No matches for pattern '{pattern}' in {path}"

    return f"Found {total} match(es) for '{pattern}':\n" + "\n".join(matches)
