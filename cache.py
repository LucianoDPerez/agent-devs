"""Capa de persistencia del análisis de repositorios (SQLite).

Guarda el análisis generado por `--analyze` en ~/.agent-cache/repo_lens.db.
El `snapshot_hash` permite detectar si el repo cambió y requiere re-análisis.
"""

import hashlib
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
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(CACHE_DB)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
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


def snapshot_hash(repo_path: str) -> str:
    """Hash del árbol de archivos (nombre+tamaño+mtime) para invalidación."""
    root = Path(repo_path)
    if not root.exists():
        return ""
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
        return ""
    lines.sort()
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save_analysis(repo_path: str, *, snapshot: str, language: str,
                  tech_stack: str, analysis: str) -> None:
    conn = _connect()
    now = _now()
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
              user_message: str, assistant_message: str, tokens_used: int) -> None:
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
           ORDER BY created_at DESC
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
