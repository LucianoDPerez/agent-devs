"""Helpers compartidas entre las tools: exclusión de archivos y lectura segura."""

from pathlib import Path

from config import EXCLUDED_DIRS, EXCLUDED_FILES


def _is_excluded(path: Path) -> bool:
    parts = set(path.parts)
    if parts & EXCLUDED_DIRS:
        return True
    return path.name in EXCLUDED_FILES


def _read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
