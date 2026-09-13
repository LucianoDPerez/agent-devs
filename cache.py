"""Capa de persistencia del análisis de repositorios (SQLite).

Guarda el análisis generado por `--analyze` en ~/.agent-cache/repo_lens.db.
El `snapshot_hash` permite detectar si el repo cambió y requiere re-análisis.
"""

import hashlib
import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from config import CACHE_DB, EXCLUDED_DIRS, EXCLUDED_FILES, MAX_SNAPSHOT_FILES

_SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    snapshot_hash TEXT,
    language TEXT,
    tech_stack TEXT,
    analysis TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS session_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    repo_path TEXT NOT NULL,
    role TEXT,
    user_message TEXT,
    assistant_message TEXT,
    tokens_used INTEGER DEFAULT 0,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_session_history_sid ON session_history(session_id);
CREATE INDEX IF NOT EXISTS idx_session_history_repo ON session_history(repo_path);

-- Cola de subtareas bulk: una tarea que toca N archivos se divide en batches
-- de 4-5 y su progreso sobrevive sesiones (E2E Task 8: 14 templates, corte a
-- mitad sin saber qué faltaba).
CREATE TABLE IF NOT EXISTS bulk_subtasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_hash TEXT NOT NULL,
    seq INTEGER NOT NULL,
    files_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(task_hash, seq)
);
CREATE INDEX IF NOT EXISTS idx_bulk_subtasks_hash ON bulk_subtasks(task_hash);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(CACHE_DB, timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.OperationalError:
        pass
    conn.executescript(_SCHEMA)
    # Migración liviana: lista de archivos del snapshot para el reuso por
    # diff chico. Fail-open: si la columna ya existe, se ignora el error.
    try:
        conn.execute("ALTER TABLE repos ADD COLUMN snapshot_files TEXT")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    return conn


def normalize_path(path: str) -> str:
    return str(Path(path).expanduser().resolve())


def _git_tracked_files(root: Path) -> list[Path]:
    """Archivos versionados por git (source of truth del código).

    Solo contenido committed/tracked cuenta para el snapshot. Carpetas de
    trabajo no versionadas (planes, outputs, etc.) no invalidan el caché.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--", "."],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    files = []
    for line in out.stdout.splitlines():
        if not line.strip():
            continue
        p = root / line.strip()
        if p.is_file():
            files.append(p)
    return files


def _repo_files(root: Path):
    """Itera archivos del repo respetando exclusiones, con tope de seguridad.

    Si el repo tiene git, usa SOLO los archivos versionados (los agregados
    externamente a la carpeta no invalidan el caché). Sin git, recorre el
    árbol físico completo.
    """
    count = 0
    tracked = _git_tracked_files(root) if (root / ".git").exists() else []
    if tracked:
        candidates = tracked
    else:
        candidates = None

    def _iter():
        if candidates is not None:
            yield from candidates
        else:
            yield from root.rglob("*")

    for p in _iter():
        if set(p.parts) & EXCLUDED_DIRS:
            continue
        if not p.is_file() or p.name in EXCLUDED_FILES:
            continue
        yield p
        count += 1
        if count >= MAX_SNAPSHOT_FILES:
            return


def snapshot_entries(repo_path: str) -> list[str]:
    """Lista ordenada 'rel\\tsize\\tmtime' de los archivos del snapshot.

    Es la materia prima de snapshot_hash y del diff para reuso: si entre dos
    snapshots cambian pocos archivos, el summary cacheado sigue válido y no
    hace falta regenerar con el LLM.
    """
    root = Path(repo_path)
    if not root.exists():
        return []
    lines = []
    try:
        for p in _repo_files(root):
            try:
                st = p.stat()
                rel = p.relative_to(root).as_posix()
                lines.append(f"{rel}\t{st.st_size}\t{st.st_mtime_ns}")
            except OSError:
                continue
    except OSError:
        return []
    lines.sort()
    return lines


def snapshot_hash(repo_path: str) -> str:
    """Hash del árbol de archivos (nombre+tamaño+mtime) para invalidación."""
    lines = snapshot_entries(repo_path)
    if not lines and not Path(repo_path).exists():
        return ""
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:16]


def snapshot_diff_files(old_files: str | None, new_entries: list[str]) -> int | None:
    """Cantidad de archivos que difieren entre dos snapshots.

    Compara por path relativo (un archivo modificado cuenta 1, no 2).
    Retorna None si no hay base de comparación (fila vieja sin snapshot_files):
    en ese caso corresponde regeneración completa, una sola vez.
    """
    if not old_files:
        return None
    old_rels = {ln.split("\t", 1)[0] for ln in old_files.splitlines() if ln.strip()}
    new_rels = {ln.split("\t", 1)[0] for ln in new_entries}
    old_map = {}
    for ln in old_files.splitlines():
        if not ln.strip():
            continue
        parts = ln.split("\t")
        old_map[parts[0]] = ln
    new_map = {}
    for ln in new_entries:
        parts = ln.split("\t")
        new_map[parts[0]] = ln
    changed = 0
    for rel in old_rels | new_rels:
        if old_map.get(rel) != new_map.get(rel):
            changed += 1
    return changed


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save_analysis(repo_path: str, *, snapshot: str, language: str,
                  tech_stack: str, analysis: str,
                  snapshot_files: str | None = None) -> None:
    conn = _connect()
    now = _now()
    if snapshot_files is None:
        conn.execute(
            """
            INSERT INTO repos (path, snapshot_hash, language, tech_stack, analysis, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                snapshot_hash = excluded.snapshot_hash,
                language = excluded.language,
                tech_stack = excluded.tech_stack,
                analysis = excluded.analysis,
                updated_at = excluded.updated_at
            """,
            (normalize_path(repo_path), snapshot, language, tech_stack, analysis, now, now),
        )
    else:
        conn.execute(
            """
            INSERT INTO repos (path, snapshot_hash, language, tech_stack, analysis, snapshot_files, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                snapshot_hash = excluded.snapshot_hash,
                language = excluded.language,
                tech_stack = excluded.tech_stack,
                analysis = excluded.analysis,
                snapshot_files = excluded.snapshot_files,
                updated_at = excluded.updated_at
            """,
            (normalize_path(repo_path), snapshot, language, tech_stack, analysis, snapshot_files, now, now),
        )
    conn.commit()
    conn.close()


def load_analysis(repo_path: str):
    """Devuelve el registro cacheado (dict) o None si no existe."""
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM repos WHERE path = ?",
        (normalize_path(repo_path),),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def list_repos():
    conn = _connect()
    rows = conn.execute(
        "SELECT path, language, tech_stack, updated_at FROM repos ORDER BY updated_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Session history ──────────────────────────────────────────────────────────


def save_turn(session_id: str, repo_path: str, role: str,
              user_message: str, assistant_message: str, tokens_used: int,
              max_chars: int = 8000) -> None:
    # Cap para no inflar repo_lens.db con preloads de 30k chars por turno
    if user_message and len(user_message) > max_chars:
        user_message = user_message[:max_chars] + "\n… (truncated)"
    if assistant_message and len(assistant_message) > max_chars:
        assistant_message = assistant_message[:max_chars] + "\n… (truncated)"
    conn = _connect()
    conn.execute(
        """INSERT INTO session_history
           (session_id, repo_path, role, user_message, assistant_message, tokens_used, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (session_id, normalize_path(repo_path), role, user_message, assistant_message, tokens_used, _now()),
    )
    conn.commit()
    conn.close()


def load_recent_turns(repo_path: str, limit: int = 10) -> list[dict]:
    """Devuelve los últimos N turnos de un repo (de cualquier sesión)."""
    conn = _connect()
    rows = conn.execute(
        """SELECT session_id, role, user_message, assistant_message, tokens_used, created_at
           FROM session_history
           WHERE repo_path = ?
           ORDER BY id DESC
           LIMIT ?""",
        (normalize_path(repo_path), limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in reversed(rows)]


def load_session_turns(session_id: str) -> list[dict]:
    """Devuelve todos los turnos de una sesión específica."""
    conn = _connect()
    rows = conn.execute(
        """SELECT role, user_message, assistant_message, tokens_used, created_at
           FROM session_history
           WHERE session_id = ?
           ORDER BY created_at ASC""",
        (session_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Bulk subtasks (cola de batches para tareas de N archivos) ───────────────


def ensure_bulk_plan(task_hash: str, batches: list[list[str]]) -> bool:
    """Crea el plan si no existe; si existe y coincide, conserva el progreso.

    Devuelve True si se creó un plan nuevo. Si el plan existente difiere
    (misma tarea re-planificada con archivos distintos), se reemplaza.
    """
    conn = _connect()
    now = _now()
    existing = conn.execute(
        "SELECT seq, files_json FROM bulk_subtasks WHERE task_hash = ? ORDER BY seq",
        (task_hash,),
    ).fetchall()
    same = (
        len(existing) == len(batches)
        and all(
            row["files_json"] == json.dumps(b, ensure_ascii=False)
            for row, b in zip(existing, batches)
        )
    )
    if not same and existing:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM bulk_subtasks WHERE task_hash = ?", (task_hash,))
            conn.executemany(
                """INSERT INTO bulk_subtasks
                   (task_hash, seq, files_json, status, attempts, created_at, updated_at)
                   VALUES (?, ?, ?, 'pending', 0, ?, ?)""",
                [
                    (task_hash, i, json.dumps(b, ensure_ascii=False), now, now)
                    for i, b in enumerate(batches)
                ],
            )
            conn.commit()
        except sqlite3.OperationalError:
            try:
                conn.rollback()
            except Exception:
                pass
        existing = []
        conn.close()
        return True
    if not existing:
        conn.executemany(
            """INSERT INTO bulk_subtasks
               (task_hash, seq, files_json, status, attempts, created_at, updated_at)
               VALUES (?, ?, ?, 'pending', 0, ?, ?)""",
            [
                (task_hash, i, json.dumps(b, ensure_ascii=False), now, now)
                for i, b in enumerate(batches)
            ],
        )
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False


def next_pending_batch(task_hash: str) -> dict | None:
    """Primer batch pendiente (o reanudable). {seq, files, attempts} o None."""
    conn = _connect()
    row = conn.execute(
        """SELECT seq, files_json, status, attempts FROM bulk_subtasks
           WHERE task_hash = ? AND status IN ('pending', 'in_progress')
           ORDER BY seq LIMIT 1""",
        (task_hash,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "seq": row["seq"],
        "files": json.loads(row["files_json"]),
        "status": row["status"],
        "attempts": row["attempts"],
    }


def mark_batch(task_hash: str, seq: int, status: str, *, bump_attempt: bool = False) -> None:
    conn = _connect()
    if bump_attempt:
        conn.execute(
            """UPDATE bulk_subtasks
               SET status = ?, attempts = attempts + 1, updated_at = ?
               WHERE task_hash = ? AND seq = ?""",
            (status, _now(), task_hash, seq),
        )
    else:
        conn.execute(
            "UPDATE bulk_subtasks SET status = ?, updated_at = ? WHERE task_hash = ? AND seq = ?",
            (status, _now(), task_hash, seq),
        )
    conn.commit()
    conn.close()


def fail_or_keep_batch(task_hash: str, seq: int, max_attempts: int) -> str:
    """Registra un intento fallido del batch; si agotó los intentos lo marca
    'failed' (permanente), si no vuelve a 'pending' para reanudar.
    Devuelve el nuevo status. Atómico: evita lost update entre workers."""
    conn = _connect()
    conn.execute(
        """UPDATE bulk_subtasks
           SET attempts = attempts + 1, updated_at = ?
           WHERE task_hash = ? AND seq = ?""",
        (_now(), task_hash, seq),
    )
    row = conn.execute(
        "SELECT attempts FROM bulk_subtasks WHERE task_hash = ? AND seq = ?",
        (task_hash, seq),
    ).fetchone()
    attempts = (row["attempts"] if row else 1) or 1
    status = "failed" if attempts >= max_attempts else "pending"
    conn.execute(
        """UPDATE bulk_subtasks
           SET status = ?, updated_at = ?
           WHERE task_hash = ? AND seq = ?""",
        (status, _now(), task_hash, seq),
    )
    conn.commit()
    conn.close()
    return status


def bulk_progress(task_hash: str) -> dict:
    """{total, done, failed, pending, in_progress} del plan."""
    conn = _connect()
    rows = conn.execute(
        "SELECT status FROM bulk_subtasks WHERE task_hash = ?",
        (task_hash,),
    ).fetchall()
    conn.close()
    statuses = [r["status"] for r in rows]
    return {
        "total": len(statuses),
        "done": sum(1 for s in statuses if s == "done"),
        "failed": sum(1 for s in statuses if s == "failed"),
        "pending": sum(1 for s in statuses if s == "pending"),
        "in_progress": sum(1 for s in statuses if s == "in_progress"),
    }
