"""Planificador de tareas bulk: divide N archivos en batches de 4-5.

E2E real (Task 8 spec-kitti, 14 templates): un solo turno de 14 archivos
agotó intentos/budgets y dejó trabajo a mitad sin forma durable de saber
QUÉ faltaba. Este módulo:
1. Detecta los archivos objetivo citados por la tarea (directorios existentes).
2. Los divide en batches chicos.
3. Persiste la cola en SQLite (cache.db) → progreso sobrevive sesiones.
El EXECUTE recibe SOLO el alcance del batch actual; session.py encadena los
batch siguientes automáticamente y rota contexto entre batches si hace falta.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from config import BULK_BATCH_SIZE


def bulk_task_hash(user_input: str, repo_path: str) -> str:
    """Identidad estable de la tarea: mismo prompt + repo → misma cola.

    Canoniciza el texto ANTES de hashear: el chaining pasa (tarea + scope del
    batch) y DEBE caer en la misma cola que el prompt original.
    """
    raw = f"{repo_path}::{canonical_task_text(user_input)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


_SCOPE_MARKER = "📦 EJECUCIÓN POR SUBTAREAS"


def canonical_task_text(text: str) -> str:
    """Texto ORIGINAL de la tarea, sin sufijos de batch scope.

    El auto-chaining llama a run_turn con (tarea + scope del batch); si el
    hash se calculara sobre ese texto compuesto, CADA batch tendría un hash
    distinto y crearía una cola nueva perdiendo el progreso (bug detectado
    en revisión). Canonicalizar garantiza una sola cola por tarea.
    """
    return text.split(_SCOPE_MARKER)[0].rstrip()


def detect_bulk_targets(task_text: str, repo_path: str | None) -> list[str]:
    """Paths absolutos (relativos al repo) de archivos a modificar en bloque.

    Estrategia: paths relativos citados en la tarea → el primero que sea un
    directorio EXISTENTE con más archivos gana (Task 8 cita
    src/spec_kitti_cli/templates/commands/ con sus 14 .md). Complementa a
    detect_bulk_file_count, que estima el N del texto.
    """
    if not repo_path:
        return []
    root = Path(repo_path)
    if not root.is_dir():
        return []

    from orchestration.execute_bootstrap import _MISSING_PATH_RE, _NOISE_PATH_FIRST

    best: list[str] = []
    for match in _MISSING_PATH_RE.finditer(task_text):
        token = match.group(1).rstrip("/")
        if not token or token.split("/", 1)[0] in _NOISE_PATH_FIRST:
            continue
        d = root / token
        if not d.is_dir():
            continue
        try:
            files = sorted(
                p.relative_to(root).as_posix() for p in d.iterdir()
                # Ocultos fuera (.telemetry-verification-report.md quedó de
                # una iteración y entraba como target fantasma).
                if p.is_file() and not p.name.startswith(".")
            )
        except OSError:
            continue
        if len(files) > len(best):
            best = files
    return best[:60]


def split_into_batches(
    files: list[str], batch_size: int = BULK_BATCH_SIZE
) -> list[list[str]]:
    """Divide la lista en batches consecutivos de ~batch_size."""
    return [files[i : i + batch_size] for i in range(0, len(files), batch_size)]


def build_batch_scope(batch_seq: int, total_batches: int, files: list[str]) -> str:
    """Inyección para el EXECUTE del turno: alcance acotado AL BATCH actual.

    El texto completo de la tarea ya está arriba (AC, notas); este bloque solo
    acota QUÉ archivos tocar en ESTE turno — el resto llega en batches propios.
    """
    listing = "\n".join(f"- {f}" for f in files)
    return (
        f"\n\n📦 EJECUCIÓN POR SUBTAREAS — Batch {batch_seq + 1}/{total_batches}.\n"
        f"Modificá EXCLUSIVAMENTE estos {len(files)} archivos en este turno:\n"
        f"{listing}\n\n"
        "Los demás archivos del alcance NO se tocan acá: llegan en batches "
        "posteriores. NO los leas ni los edites.\n"
        "IMPORTANTE: si ALGÚN archivo de este lote YA cumple con lo que pide "
        "la tarea (trabajo de una iteración anterior), NO lo edites y NO "
        "inventes cambios: verificalo corriendo run_lint/run_tests sobre los "
        "archivos del lote y TERMINÁ tu turno con un resumen indicando qué "
        "archivos ya cumplían y cuáles modificaste. Cerrar SIN edits tras "
        "verificar es un resultado VÁLIDO y esperado.\n"
        "Cuando termines, no hagas commit."
    )
