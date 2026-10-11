"""Validación determinística de citas archivo:línea en respuestas del agente.

VERIFICAR CON EVIDENCIA como orden exigible, no como consejo: si un informe
cita `[path:línea]` que no existe en disco (archivo inexistente o línea fuera
de rango, ej. `:0`), el turno reintenta UNA vez para corregir. E2E real
T1b/9B: informe con `src/lib/patient.service.ts:0` y otros paths Next.js
inexistentes en un repo NestJS.

Nombres pelados (`validate.ts:15` sin el prefijo desde la raíz) se resuelven
por basename antes de marcarlos: el modelo suele citar corto lo que SÍ existe
(E2E Spark: retractó hallazgos verdaderos por citar sin prefijo). Si varios
archivos comparten el nombre, basta que UNO valide (fail-open a menos
falsos positivos; el objetivo es cazar paths inventados, no el formato).

Solo para roles de verificación (REVIEW/ANALYZE): PLAN cita archivos por
crear (falsos positivos garantizados) y EXECUTE resume lo que escribió.
"""
from __future__ import annotations

import re
from pathlib import Path

# **[path:línea]** (formato del informe de review)
_CITE_BOLD_RE = re.compile(r"\*\*\[([^\]\n]{1,160}):(\d{1,6})(?:-\d{1,6})?\]\*\*")
# [path:línea] suelto (path con extensión, sin URLs: exige punto+extensión
# antes de los dos puntos, así `[12:30]` o `arr[0:2]` no matchean).
_CITE_PLAIN_RE = re.compile(
    r"(?<![\w/])\[([A-Za-z0-9_.\-][\w.\-/ ]{0,140}\.\w{1,5}):(\d{1,6})(?:-\d{1,6})?\]"
)
# `path:línea` en inline-code (E2E real T2/9B: `CreatePaciente.ts:15` en un
# archivo de 14 líneas pasó el validador). Exige extensión para no marcar
# `arr[0:2]` o `12:30` entre backticks.
_CITE_TICK_RE = re.compile(
    r"`((?:[A-Za-z0-9_.\-][\w.\-/ ]{0,140}\.\w{1,5})):(\d{1,6})(?:-\d{1,6})?`"
)

# Archivos gigantes se saltean (fail-open): leer 500MB para contar líneas
# castigaría citas legítimas.
_MAX_CHECK_BYTES = 1_000_000


def _extract_cites(response: str) -> list[tuple[str, int]]:
    """Todas las citas (path, línea) en orden de aparición, sin duplicar."""
    seen: set[tuple[str, int]] = set()
    out: list[tuple[str, int]] = []
    for m in (
        list(_CITE_BOLD_RE.finditer(response))
        + list(_CITE_PLAIN_RE.finditer(response))
        + list(_CITE_TICK_RE.finditer(response))
    ):
        key = (m.group(1).strip(), int(m.group(2)))
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _resolve_candidates(path: str, root: Path) -> list[Path]:
    """Archivos reales candidatos para una cita: path desde la raíz, o basename.

    Lista vacía = la cita no existe en disco (path inventado). La búsqueda
    por basename cubre citas en corto (`validate.ts:15` de algo real).
    """
    p = Path(path)
    if p.is_absolute():
        return [p] if p.is_file() else []
    direct = root / path
    if direct.is_file():
        return [direct]
    # Nombre pelado o prefijo parcial: candidatos por basename.
    try:
        return [c for c in root.rglob(p.name) if c.is_file()]
    except OSError:
        return []


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
        if line < 1:
            bad.append(f"{path}:{line}")
            continue
        cands = _resolve_candidates(path, root)
        if not cands:
            bad.append(f"{path}:{line}")
            continue
        # Vale si ALGÚN candidato tiene esa línea (fail-open en gigantes o
        # ilegibles, igual que antes: el objetivo es cazar paths inventados).
        ok = False
        for cand in cands:
            try:
                if cand.stat().st_size > _MAX_CHECK_BYTES:
                    ok = True
                    break
            except OSError:
                ok = True
                break
            try:
                total = sum(1 for _ in cand.open(encoding="utf-8", errors="replace"))
            except OSError:
                ok = True
                break
            if line <= total:
                ok = True
                break
        if not ok:
            bad.append(f"{path}:{line}")
    return bad
