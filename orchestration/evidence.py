"""Validación determinística de citas archivo:línea en respuestas del agente.

VERIFICAR CON EVIDENCIA como orden exigible, no como consejo: si un informe
cita `[path:línea]` que no existe en disco (archivo inexistente o línea fuera
de rango, ej. `:0`), el turno reintenta UNA vez para corregir. E2E real
T1b/9B: informe con `src/lib/patient.service.ts:0` y otros paths Next.js
inexistentes en un repo NestJS.

Solo para roles de verificación (REVIEW/ANALYZE): PLAN cita archivos por
crear (falsos positivos garantizados) y EXECUTE resume lo que escribió.
"""
from __future__ import annotations

import re
from pathlib import Path

# **[path:línea]** (formato del informe de review)
_CITE_BOLD_RE = re.compile(r"\*\*\[([^\]\n]{1,160}):(\d{1,6})\]\*\*")
# [path:línea] suelto (path con extensión, sin URLs: exige punto+extensión
# antes de los dos puntos, así `[12:30]` o `arr[0:2]` no matchean).
_CITE_PLAIN_RE = re.compile(
    r"(?<![\w/])\[([A-Za-z0-9_.\-][\w.\-/ ]{0,140}\.\w{1,5}):(\d{1,6})\]"
)

# Archivos gigantes se saltean (fail-open): leer 500MB para contar líneas
# castigaría citas legítimas.
_MAX_CHECK_BYTES = 1_000_000


def _extract_cites(response: str) -> list[tuple[str, int]]:
    """Todas las citas (path, línea) en orden de aparición, sin duplicar."""
    seen: set[tuple[str, int]] = set()
    out: list[tuple[str, int]] = []
    for m in list(_CITE_BOLD_RE.finditer(response)) + list(
        _CITE_PLAIN_RE.finditer(response)
    ):
        key = (m.group(1).strip(), int(m.group(2)))
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def find_unverifiable_cites(response: str | None, repo_path: str) -> list[str]:
    """Citas `path:línea` que NO verifican en disco. Lista vacía = todo ok
    (incluido el caso sin citas: negativa honesta sin fabricar evidencia)."""
    if not response:
        return []
    bad: list[str] = []
    root = Path(repo_path)
    for path, line in _extract_cites(response):
        if path.startswith(("http://", "https://")):
            continue
        p = Path(path)
        if not p.is_absolute():
            p = root / path
        try:
            if not p.is_file():
                bad.append(f"{path}:{line}")
                continue
            if line < 1:
                bad.append(f"{path}:{line}")
                continue
            try:
                if p.stat().st_size > _MAX_CHECK_BYTES:
                    continue  # fail-open en gigantes
            except OSError:
                continue
            try:
                total = sum(1 for _ in p.open(encoding="utf-8", errors="replace"))
            except OSError:
                continue  # ilegible → fail-open
            if line > total:
                bad.append(f"{path}:{line}")
        except OSError:
            continue
    return bad
