"""Tools de operaciones sobre el sistema de archivos."""

import re
from pathlib import Path

from langchain_core.tools import tool

from config import (
    MAX_FILE_READ_BYTES,
    MAX_LIST_RESULTS,
    PROTECTED_TASK_DIRS,
    PROTECTED_TASK_FILENAMES,
    WRITE_FILE_OVERWRITE_MAX_LINES,
)

from ._helpers import _is_excluded


def _is_protected_task_path(path: str) -> bool:
    """True si el path corresponde a un archivo de planificación que el agente
    NUNCA debe escribir/editar/borrar (tasks.md, .agent-devs/, plans/, etc.).

    El 4B tiende a reescribir tasks.md (precargado en el prompt) corrompiendo el
    plan. Detectamos por nombre de archivo o por subdirectorio protegido.
    """
    if not path:
        return False
    p = Path(path)
    name_lower = p.name.lower()
    if name_lower in PROTECTED_TASK_FILENAMES:
        return True
    parts_lower = {part.lower() for part in p.parts}
    if parts_lower & PROTECTED_TASK_DIRS:
        return True
    return False


@tool
def list_files(path: str, recursive: bool = False) -> str:
    """
    List files in a directory. If recursive=True, traverse the entire tree.
    Filters out common noise directories (.git, node_modules, __pycache__, etc).
    If the path does not exist, returns a message — do not retry the same path.
    Usage: list_files(path="/path/to/repo")
    """
    root = Path(path)
    if not root.exists():
        return (
            f"Path does not exist: {path}. "
            "Do not retry this path or search for name variants. Continue with another approach."
        )
    if not root.is_dir():
        return f"'{path}' is a file, not a directory. Use read_file to view it."

    if recursive:
        try:
            top_level = sum(1 for _ in root.iterdir())
        except OSError:
            top_level = 0
        if top_level > 15:
            return (
                f"Refusing recursive listing of '{path}' ({top_level} top-level entries). "
                "Use list_files(path=..., recursive=false) or a narrower subdirectory "
                "(e.g. apps/, src/, lib/). Then search_code for symbols."
            )

    results: list[str] = []
    count = 0

    for entry in sorted(root.rglob("*") if recursive else root.iterdir()):
        if _is_excluded(entry):
            continue
        if count >= MAX_LIST_RESULTS:
            results.append(f"... ({MAX_LIST_RESULTS} files shown, more truncated)")
            break

        rel = entry.relative_to(root)
        if entry.is_dir():
            results.append(f"📁 {rel}/")
        elif entry.is_file():
            size = entry.stat().st_size
            results.append(f"📄 {rel} ({size:,} bytes)")
        count += 1

    if not results:
        return f"Directory '{path}' is empty or all entries were filtered."

    return f"Contents of {path}:\n" + "\n".join(results)


def _suggest_paths(path: str, limit: int = 5) -> str:
    """'Did you mean' para paths inexistentes: busca en el repo archivos con
    el mismo nombre o similar. El modelo chico inventa paths (pacientesApi.ts
    cuando el real es services/api.ts) y quedaba en dead end. Escalable:
    funciona para cualquier repo, cualquier estructura."""
    try:
        name = Path(path).name
        if not name:
            return ""
        name_stem = Path(name).stem.lower()
        # Raíz del repo: subir hasta encontrar un marcador de proyecto
        root = Path(path)
        markers = (".git", "package.json", "go.mod", "pyproject.toml", "requirements.txt")
        while root != root.parent:
            if any((root / m).exists() for m in markers):
                break
            root = root.parent
        if root == root.parent:
            root = Path.cwd()  # último recurso: árbol del proceso
        candidates: list[str] = []
        for p in root.rglob("*"):
            if not p.is_file() or _is_excluded(p):
                continue
            p_stem = p.stem.lower()
            # match por nombre exacto o por stem contenido (pacientesApi.ts → api.ts)
            if p.name == name or p_stem in name_stem or name_stem in p_stem:
                candidates.append(str(p.relative_to(root)))
            if len(candidates) > limit:
                break
        candidates = list(dict.fromkeys(candidates))[:limit]
        if not candidates:
            return ""
        return "\nArchivos similares en el repo (candidatos reales):\n" + "\n".join(
            f"  - {c}" for c in candidates
        )
    except Exception:
        return ""


@tool
def read_file(path: str, start_line: int = 1, end_line: int | None = None) -> str:
    """
    Read a text file. Optionally specify line range (start_line, end_line).
    Files exceeding MAX_FILE_READ_BYTES are truncated and a notice is shown.
    If the file does not exist (or the path is a directory), returns a message —
    accept it and continue; do not retry the same path or hunt for name variants.
    Usage: read_file(path="/path/to/repo/README.md")
    """
    p = Path(path)
    if not p.exists():
        return (
            f"File does not exist: {path}. "
            "Do not retry this path or search for name variants "
            "(e.g. README.md / README / ENV.md). If documentation is required, create the file."
            + _suggest_paths(path)
        )
    if p.is_dir():
        return (
            f"'{path}' is a directory, not a file. Use list_files to browse it. "
            "Do not call read_file on this path again."
        )

    raw = p.read_text(encoding="utf-8", errors="replace")
    total_lines = raw.splitlines()

    start = max(0, start_line - 1)
    end = end_line if end_line else len(total_lines)
    lines = total_lines[start:end]
    content = "\n".join(lines)

    if start_line > 1:
        content = f"... (lines 1 to {start_line - 1} skipped) ...\n" + content

    truncated = False
    raw_bytes = content.encode("utf-8")
    if len(raw_bytes) > MAX_FILE_READ_BYTES:
        content = raw_bytes[:MAX_FILE_READ_BYTES].decode("utf-8", errors="replace")
        truncated = True

    header = f"📄 {path}"
    if truncated:
        header += f" [TRUNCATED at {MAX_FILE_READ_BYTES:,} bytes]"
    header += f"\n{'─' * 60}\n"

    return header + content


@tool
def write_file(path: str, content: str) -> str:
    """
    Write content to a file. Overwrites if the file exists.
    Creates parent directories if needed.
    If the path is a directory or the write fails, returns a message —
    accept it and choose a different path.
    Usage: write_file(path="/Users/me/repo/report.md", content="# Report")
    """
    if _is_protected_task_path(path):
        return (
            f"⛔ '{path}' es un archivo de PLANIFICACIÓN (tasks/PRD/plan) PROHIBIDO de escribir. "
            "NO lo toques: es tu fuente de verdad de la tarea. Implementá el código "
            "en los archivos del repo, no reescribas tasks.md ni planes."
        )
    p = Path(path)
    if p.exists() and p.is_dir():
        return (
            f"⛔ '{path}' is a DIRECTORY, not a file. "
            f"Do NOT call write_file on a directory path. "
            f"Call write_file with a FILE name inside it, for example: "
            f"write_file(path='{path}/index.ts', content='...')"
        )
    if p.exists() and p.is_file():
        # Guard anti-destrucción: write_file NO puede sobrescribir archivos
        # existentes (salvo configs triviales de ≤5 líneas). Reescribir desde
        # memoria pierde imports/hooks/lógica — el 4B mutiló PacientesPage.tsx.
        # Forzamos el path quirúrgico: read_file + edit_file.
        try:
            existing_lines = len(p.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            existing_lines = 0
        if existing_lines > WRITE_FILE_OVERWRITE_MAX_LINES:
            return (
                f"⛔ '{path}' YA EXISTE ({existing_lines} líneas) y write_file está "
                f"BLOQUEADO para sobrescribirlo (riesgo de destruir código).\n"
                f"Hacé el cambio de forma quirúrgica:\n"
                f"  1) read_file(path='{path}') para ver el contenido EXACTO.\n"
                f"  2) edit_file(path='{path}', old_str='...', new_str='...') con "
                f"cadenas exactas del archivo real.\n"
                f"write_file solo está permitido para archivos NUEVOS o de ≤ "
                f"{WRITE_FILE_OVERWRITE_MAX_LINES} líneas (configs triviales)."
            )

    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    except (OSError, IsADirectoryError, FileExistsError) as e:
        return f"⛔ Failed to write {path}: {e}. Choose a different file path."
    return f"✅ Written {len(content)} characters to {path}"


@tool
def delete_file(path: str) -> str:
    """
    Delete a file from the filesystem.
    If the file does not exist, returns a message — accept it and continue.
    Usage: delete_file(path="/Users/me/repo/old-file.ts")
    """
    if _is_protected_task_path(path):
        return (
            f"⛔ '{path}' es un archivo de PLANIFICACIÓN (tasks/PRD/plan) PROHIBIDO de borrar. "
            "NO lo elimines: es tu fuente de verdad de la tarea."
        )
    p = Path(path)
    if not p.exists():
        return (
            f"File does not exist: {path}. "
            "Do not retry this path. Continue with other tasks."
        )
    if p.is_dir():
        return f"'{path}' is a directory. Use a different approach to remove directories."
    p.unlink()
    return f"✅ Deleted {path}"


@tool
def edit_file(path: str, old_str: str, new_str: str) -> str:
    """
    Edit a file by replacing a block of code (aider-style SEARCH/REPLACE).
    Include 2-5 lines of context around the change (the lines you change plus
    neighbors). The matcher is tolerant: it tries exact match first, then
    ignores whitespace differences (trailing spaces, indentation), then
    matches by the first/last anchor lines of your block.
    Usage: edit_file(path="file.py", old_str="old code block", new_str="new code block")
    """
    if _is_protected_task_path(path):
        return (
            f"⛔ '{path}' es un archivo de PLANIFICACIÓN (tasks/PRD/plan) PROHIBIDO de editar. "
            "NO lo toques: es tu fuente de verdad. Implementá el código en los archivos del repo."
        )
    p = Path(path)
    if not p.exists():
        return (
            f"File does not exist: {path}. "
            "Do not retry this path. Create it with write_file if you need to edit it."
        )
    if p.is_dir():
        return (
            f"'{path}' is a directory, not a file. Use list_files to browse it. "
            "Do not call edit_file on this path again."
        )

    content = p.read_text(encoding="utf-8")

    # GUARD ANTI-REESCRITURA MASIVA: el modelo chico usa edit_file con
    # old_str/new_str = archivo ENTERO para "reescribir de memoria" — rompe
    # el código (interfaces inválidas, funciones duplicadas 3 veces, TS1005…
    # visto en E2E real: useConsultasList/useMigracion). write_file ya está
    # bloqueado para archivos existentes; edit_file debe ser QUIRÚRGICO.
    total_lines = len(content.splitlines())
    block_lines = len(old_str.splitlines()) + len(new_str.splitlines())
    if block_lines > 40 or (total_lines > 0 and len(old_str.splitlines()) > max(10, total_lines // 2)):
        return (
            f"⛔ edit_file es para ediciones QUIRÚRGICAS, no para reescribir "
            f"archivos enteros.\n"
            f"Tu bloque cubre {len(old_str.splitlines())} de {total_lines} líneas "
            f"del archivo — reescribirlo de memoria rompe el código "
            f"(imports, interfaces, duplicados).\n"
            f"PROCEDÉ ASÍ:\n"
            f"  1) read_file(path='{path}') para ver el contenido EXACTO.\n"
            f"  2) edit_file con UN CAMBIO CHICO a la vez (máx ~20 líneas por "
            f"bloque): la línea que cambiás + 2-5 de contexto.\n"
            f"  3) Repetí para cada cambio. Verificá con run_lint/run_build."
        )

    spans = _find_spans(content, old_str)
    if not spans:
        return (
            f"old_str not found in {path}. Cannot perform replacement.\n"
            f"Re-read the file with read_file and copy the block LITERALLY, "
            f"with 2-5 lines of context around the change.{_context_hint(content, old_str)}"
        )

    if len(spans) > 1:
        return (
            f"⚠️ Found {len(spans)} possible matches for old_str in {path}. "
            "Add more surrounding context lines to disambiguate."
        )

    start, end = spans[0]
    new_content = content[:start] + new_str + content[end:]
    p.write_text(new_content, encoding="utf-8")
    return f"✅ Replaced block in {path}"


def _exact_spans(content: str, needle: str) -> list[tuple[int, int]]:
    """All spans of an exact substring match."""
    spans: list[tuple[int, int]] = []
    idx = content.find(needle)
    while idx >= 0:
        spans.append((idx, idx + len(needle)))
        idx = content.find(needle, idx + len(needle))
    return spans


def _build_fuzzy_regex(old_str: str) -> re.Pattern | None:
    """Regex tolerante a espacios/indentación: cada línea del bloque se matchea
    por su contenido stripped, permitiendo whitespace variable entre palabras
    y en los bordes. Ignora líneas vacías del bloque."""
    lines = [ln.strip() for ln in old_str.splitlines() if ln.strip()]
    if not lines:
        return None
    parts = []
    for ln in lines:
        parts.append(re.escape(ln).replace(r"\ ", r"\s+"))
    return re.compile(r"(?m)^\s*" + r"\s*\n\s*".join(parts))


def _anchor_spans(content: str, old_str: str) -> list[tuple[int, int]]:
    """Fallback estilo aider: matchea por la primera y última línea no vacía
    del bloque. Devuelve todos los spans candidatos (el caller decide si es
    ambiguo)."""
    old_lines = [ln.strip() for ln in old_str.splitlines() if ln.strip()]
    if len(old_lines) < 2:
        return []
    first, last = old_lines[0], old_lines[-1]
    content_lines = content.splitlines()
    spans: list[tuple[int, int]] = []
    for i, ln in enumerate(content_lines):
        if ln.strip() != first:
            continue
        window = content_lines[i : i + len(old_lines)]
        for j, wln in enumerate(window):
            if wln.strip() == last:
                start = sum(len(l) + 1 for l in content_lines[:i])
                end = start + sum(len(l) + 1 for l in content_lines[i : i + j + 1])
                spans.append((start, end))
                break
    return spans


def _find_spans(content: str, old_str: str) -> list[tuple[int, int]]:
    """Cascada de matching: exacto → rstrip del bloque → regex fuzzy → anclas."""
    spans = _exact_spans(content, old_str)
    if spans:
        return spans
    stripped = old_str.rstrip()
    if stripped and stripped != old_str:
        spans = _exact_spans(content, stripped)
        if spans:
            return spans
    pat = _build_fuzzy_regex(old_str)
    if pat is not None:
        spans = [(m.start(), m.end()) for m in pat.finditer(content)]
        if spans:
            return spans
    return _anchor_spans(content, old_str)


def _context_hint(content: str, old_str: str) -> str:
    """Ayuda para el error: muestra las líneas reales del archivo alrededor de
    la primera línea del bloque buscado, para que el modelo corrija el block."""
    anchor = next((ln.strip() for ln in old_str.splitlines() if ln.strip()), None)
    if not anchor:
        return ""
    lines = content.splitlines()
    for i, ln in enumerate(lines):
        if ln.strip() == anchor:
            lo, hi = max(0, i - 3), min(len(lines), i + 4)
            return (
                "\nContexto real del archivo "
                f"(líneas {lo + 1}-{hi}):\n" + "\n".join(lines[lo:hi])
            )
    return ""
