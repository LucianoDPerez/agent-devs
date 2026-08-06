"""Tools de operaciones sobre el sistema de archivos."""

from pathlib import Path

from langchain_core.tools import tool

from config import (
    MAX_FILE_READ_BYTES,
    MAX_LIST_RESULTS,
    PROTECTED_TASK_DIRS,
    PROTECTED_TASK_FILENAMES,
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
    Usage: list_files(path="/Users/luchop/PROYECTOS IA/Medicos")
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


@tool
def read_file(path: str, start_line: int = 1, end_line: int | None = None) -> str:
    """
    Read a text file. Optionally specify line range (start_line, end_line).
    Files exceeding MAX_FILE_READ_BYTES are truncated and a notice is shown.
    If the file does not exist (or the path is a directory), returns a message —
    accept it and continue; do not retry the same path or hunt for name variants.
    Usage: read_file(path="/Users/luchop/PROYECTOS IA/Medicos/README.md")
    """
    p = Path(path)
    if not p.exists():
        return (
            f"File does not exist: {path}. "
            "Do not retry this path or search for name variants "
            "(e.g. README.md / README / ENV.md). If documentation is required, create the file."
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
    Edit a file by replacing old_str with new_str (exact match required).
    Only replaces the first occurrence. Use carefully.
    Usage: edit_file(path="file.py", old_str="old code", new_str="new code")
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

    if old_str not in content:
        return (
            f"old_str not found in {path}. Cannot perform replacement. "
            "Re-read the file and use an exact substring that exists."
        )

    occurrences = content.count(old_str)
    if occurrences > 1:
        return (
            f"⚠️ Found {occurrences} occurrences of old_str in {path}. "
            f"Only the first was replaced. Provide more context to disambiguate."
        )

    new_content = content.replace(old_str, new_str, 1)
    p.write_text(new_content, encoding="utf-8")
    return f"✅ Replaced text in {path} (1 occurrence)"
