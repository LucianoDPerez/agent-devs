"""Tools de operaciones sobre el sistema de archivos."""

from pathlib import Path

from langchain_core.tools import ToolException, tool

from config import MAX_FILE_READ_BYTES, MAX_LIST_RESULTS

from ._helpers import _is_excluded


@tool
def list_files(path: str, recursive: bool = False) -> str:
    """
    List files in a directory. If recursive=True, traverse the entire tree.
    Filters out common noise directories (.git, node_modules, __pycache__, etc).
    Usage: list_files(path="/Users/luchop/PROYECTOS IA/Medicos")
    """
    root = Path(path)
    if not root.exists():
        raise ToolException(f"Path does not exist: {path}")
    if not root.is_dir():
        return f"'{path}' is a file, not a directory. Use read_file to view it."

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
    Usage: read_file(path="/Users/luchop/PROYECTOS IA/Medicos/README.md")
    """
    p = Path(path)
    if not p.exists():
        raise ToolException(f"File does not exist: {path}")

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
    Usage: write_file(path="/Users/me/repo/report.md", content="# Report")
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    p.write_text(content, encoding="utf-8")
    return f"✅ Written {len(content)} characters to {path}"


@tool
def edit_file(path: str, old_str: str, new_str: str) -> str:
    """
    Edit a file by replacing old_str with new_str (exact match required).
    Only replaces the first occurrence. Use carefully.
    Usage: edit_file(path="file.py", old_str="old code", new_str="new code")
    """
    p = Path(path)
    if not p.exists():
        raise ToolException(f"File does not exist: {path}")

    content = p.read_text(encoding="utf-8")

    if old_str not in content:
        raise ToolException(
            f"old_str not found in {path}. Cannot perform replacement."
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
