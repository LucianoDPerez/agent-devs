"""Helpers compartidas entre las tools: exclusión de archivos y lectura segura."""

from pathlib import Path

from config import EXCLUDED_DIRS, EXCLUDED_FILES


def _is_excluded(path: Path) -> bool:
    # Case-insensitive + resolve para symlinks: en macOS Node_Modules ==
    # node_modules, y mylink -> node_modules no debe escanearse.
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    parts_lower = {p.lower() for p in resolved.parts}
    excluded_lower = {e.lower() for e in EXCLUDED_DIRS}
    if parts_lower & excluded_lower:
        return True
    name_lower = resolved.name.lower()
    excluded_files_lower = {e.lower() for e in EXCLUDED_FILES}
    if name_lower in excluded_files_lower:
        return True
    # .env* reales nunca se escanean (secretos). Los examples (.env.example /
    # .env.template / .env.sample) NO se excluyen: los maneja env.py por allowlist.
    _ENV_EXAMPLES = {".env.example", ".env.template", ".env.sample", ".env.example.local"}
    if name_lower in _ENV_EXAMPLES:
        return False
    if name_lower.startswith(".env.") or name_lower in {".env.local", ".env.development", ".env.production", ".env.staging"}:
        return True
    return path.name in EXCLUDED_FILES


def _read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
