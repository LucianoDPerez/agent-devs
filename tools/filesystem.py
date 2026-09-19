"""Tools de operaciones sobre el sistema de archivos."""

import contextlib
import re
from pathlib import Path

from langchain_core.tools import tool

from config import (
    MAX_EDIT_BLOCK_LINES,
    MAX_FILE_READ_BYTES,
    MAX_LIST_RESULTS,
    PROTECTED_TASK_DIRS,
    PROTECTED_TASK_FILENAMES,
    WRITE_FILE_OVERWRITE_MAX_LINES,
)

from ._helpers import _is_excluded

# Extensión → lenguaje para el chequeo de sintaxis post-write. Solo se valida
# cuando el binario del intérprete está disponible; en caso contrario se omite
# (fail-open, nunca bloquea el write ni genera falsos positivos).
_SYNTAX_CHECKERS = {
    ".sh": ["bash", "-n"],
    ".py": ["python3", "-m", "py_compile"],
}
# Extensiones cuyo contenido se valida por balance de delimitadores. Las cadenas
# y comentarios se respetan para evitar falsos positivos.
_INTEGRITY_EXTS = {".sh", ".py", ".go", ".ts", ".tsx", ".js", ".jsx", ".vue", ".svelte", ".md"}

# Paths autorizados por el ORQUESTADOR para sobrescribir con write_file pese al
# guard anti-destrucción. Se habilita SOLO tras N rechazos del guard quirúrgico
# de edit_file sobre el mismo archivo (escalamiento de estrategia, ver
# orchestration/tool_dedupe.py). El modelo no converge con cirugía fina →
# reemplazo completo anclado en el read cache. Se limpia al inicio de cada turno.
WRITE_OVERRIDE_PATHS: set[str] = set()


def _status_only_flip(before: object, after: object) -> bool:
    """True si between/after difieren ÚNICAMENTE en campos "status" cuyo valor
    nuevo es DONE (pending/in_progress → done).

    Flujo del usuario: el agente MANTIENE tasks.json marcando tareas Done
    (decisión de producto). La protección anti-corrupción frena iniciativa
    propia; esta excepción quirúrgica permite el flip de status sin permitir
    tocar NADA más (ids, diffs, estructura). Cualquier otro cambio → False.
    """
    if isinstance(before, dict) and isinstance(after, dict):
        if before.keys() != after.keys():
            return False
        for k, vb in after.items():
            va = before[k]
            if k == "status" and va != vb:
                if not (
                    isinstance(va, str) and isinstance(vb, str)
                    and va.strip().lower() != "done"
                    and vb.strip().lower() == "done"
                ):
                    return False
            elif not _status_only_flip(va, vb):
                return False
        return True
    if isinstance(before, list) and isinstance(after, list):
        return len(before) == len(after) and all(
            _status_only_flip(x, y) for x, y in zip(before, after, strict=False)
        )
    return before == after


# Paths que el harness YA leyó en esta sesión (read_file exitoso) o creó
# con write_file. edit_file/apply_patch EXIGEN haber leído antes: el modelo
# chico inventa old_str de memoria y corrompe el archivo (fase 1 little-coder:
# invariante Read-before-Edit). Se limpia solo al arrancar sesión nueva, no
# por turno (leer en turno N habilita editar en turno N+1).
READ_SEEN_PATHS: set[str] = set()


def clear_read_tracker() -> None:
    READ_SEEN_PATHS.clear()


def _mark_read(path: str) -> None:
    READ_SEEN_PATHS.add(_resolved_key(path))


def _was_read(path: str) -> bool:
    return _resolved_key(path) in READ_SEEN_PATHS


def clear_write_overrides() -> None:
    WRITE_OVERRIDE_PATHS.clear()


# Paths de planificación que el USUARIO citó explícitamente en su mensaje
# (.agent/tasks.json, plans/plan.md, ...): la orden explícita del usuario
# gana a la protección (E2E real: "marcá Done en .agent/tasks.json" →
# bloqueado aunque el usuario lo pidió). Solo el mensaje del usuario agrega
# acá — el modelo no tiene cómo (ninguna tool lo expone). Por turno.
TASK_PATH_ALLOW: set[str] = set()


def clear_task_allow() -> None:
    TASK_PATH_ALLOW.clear()


def _resolved_key(path: str) -> str:
    """Key normalizada para comparar overrides: expanduser + resolve.

    Sin esto `/repo//src//a.ts`, `/repo/src/./a.ts` o `Src/A.ts` (macOS
    case-insensitive) bypassean el set por string exacto.
    """
    try:
        return str(Path(path).expanduser().resolve())
    except OSError:
        return str(Path(path).expanduser().absolute())


def _is_protected_task_path(path: str) -> bool:
    """True si el path corresponde a un archivo de planificación que el agente
    NUNCA debe escribir/editar/borrar por iniciativa propia (tasks.md,
    .agent-devs/, etc.).

    Excepción: paths que el USUARIO citó explícitamente en su mensaje
    (TASK_PATH_ALLOW, por turno) — la orden explícita gana a la protección.
    El 4B tiende a reescribir tasks.md (precargado en el prompt) corrompiendo el
    plan. Detectamos por nombre de archivo o por subdirectorio protegido.
    Resolve para que symlinks (evil.md -> tasks.md) no bypasseen.
    """
    if not path:
        return False
    if TASK_PATH_ALLOW and _resolved_key(path) in {
        _resolved_key(p) for p in TASK_PATH_ALLOW
    }:
        return False
    try:
        p = Path(path).expanduser().resolve()
    except OSError:
        p = Path(path)
    name_lower = p.name.lower()
    if name_lower in PROTECTED_TASK_FILENAMES:
        return True
    parts_lower = {part.lower() for part in p.parts}
    return bool(parts_lower & PROTECTED_TASK_DIRS)


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
        try:
            if entry.is_dir():
                results.append(f"📁 {rel}/")
            elif entry.is_file():
                size = entry.stat().st_size
                results.append(f"📄 {rel} ({size:,} bytes)")
        except OSError:
            # symlink roto (ej. node_modules/.bin/.rimraf-XXX de un npm install
            # interrumpido): is_dir/is_file/stat lanzan FileNotFoundError.
            continue
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
    # Git internals: el modelo no aprende nada de .git/config y en medio de
    # un commit perdía los tools de git (retry) y los leía crudo (E2E real:
    # "implementar commit" → read_file .git/config). Redirigir a las tools.
    parts = {part.lower() for part in p.parts}
    if ".git" in parts:
        return (
            f"⛔ No leas el interior de .git ({path}). Para git usá las TOOLS: "
            "git_status (rama + estado), current_branch, git_log, changed_files. "
            "No reimplementes git leyendo archivos internos."
        )
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

    _mark_read(str(p))
    return header + content


def _integrity_check(content: str) -> str | None:
    """Detecta contenido TRUNCADO por desbalance de delimitadores.

    El LLM local (Qwen 35B) a veces agota su presupuesto de output DURANTE un
    tool call de write_file y el contenido llega cortado a mitad de una apertura
    (E2E real: e2e_verify.sh terminó en `python3 -c "` sin cerrar). write_file
    escribía el fragmento y devolvía éxito — nadie lo detectaba.

    Devuelve un string con la descripción del imbalance, o None si el contenido
    parece íntegro. Respetamos cadenas (con escapes) y comentarios para no dar
    falsos positivos.
    """
    openers: list[str] = []
    stack: list[str] = []
    in_squote = in_dquote = in_backtick = False
    i = 0
    n = len(content)
    while i < n:
        ch = content[i]
        nxt = content[i + 1] if i + 1 < n else ""

        if in_backtick:
            if ch == "\\":
                i += 2
                continue
            if ch == "`":
                in_backtick = False
            i += 1
            continue
        if in_squote:
            if ch == "\\":
                i += 2
                continue
            if ch == "'":
                in_squote = False
            i += 1
            continue
        if in_dquote:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_dquote = False
            i += 1
            continue

        # Comentarios de línea (los de bloque no se rastrean: raro en estos ext)
        if ch == "#":
            while i < n and content[i] != "\n":
                i += 1
            continue
        # Comentario de línea en JS/TS
        if ch == "/" and nxt == "/":
            while i < n and content[i] != "\n":
                i += 1
            continue

        if ch in ('"', "'", "`"):
            if ch == '"':
                in_dquote = True
            elif ch == "'":
                in_squote = True
            else:
                in_backtick = True
            openers.append(ch)
            i += 1
            continue

        if ch in "([{":
            stack.append(ch)
            i += 1
            continue
        if ch in ")]}":
            pairs = {")": "(", "]": "[", "}": "{"}
            if not stack or stack[-1] != pairs[ch]:
                # Desbalance real (cierre sin apertura o cruce) → truncado probable
                return (
                    f"⚠️ INTEGRIDAD: delimitador de cierre '{ch}' sin su apertura "
                    f"correspondiente en el contenido (¿truncado?)."
                )
            stack.pop()
            i += 1
            continue

        i += 1

    problems: list[str] = []
    if in_squote or in_dquote or in_backtick:
        problems.append("cadena (comilla) abierta sin cerrar")
    if stack:
        problems.append(f"{len(stack)} delimitador(es) sin cerrar: {''.join(stack)}")
    if not problems:
        return None
    return (
        "⚠️ INTEGRIDAD: el contenido parece TRUNCADO — " + "; ".join(problems) + ". "
        "Releé el archivo con read_file y completá el contenido, o escribí el "
        "archivo en partes más chicas (el LLM tiene un límite de output por tool call)."
    )


def _syntax_check(path: str, content: str) -> str | None:
    """Chequeo de sintaxis por extensión (bash -n / py_compile) en memoria.

    Devuelve None si la sintaxis es válida (o no se pudo verificar), o un string
    con el error. Fail-open: si el intérprete no existe o falla el subprocess,
    no bloquea (el write ya se persistió; esto es un aviso, no una compuerta).
    """
    import subprocess
    import tempfile

    ext = Path(path).suffix.lower()
    checker = _SYNTAX_CHECKERS.get(ext)
    if checker is None:
        return None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=ext, delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        try:
            proc = subprocess.run(
                [*checker, tmp_path],
                capture_output=True,
                text=True,
                timeout=30,
            )
        finally:
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink(missing_ok=True)
    except (OSError, subprocess.SubprocessError):
        # intérprete ausente o fallo de entorno → no podemos verificar, omitir
        return None

    if proc.returncode == 0:
        return None
    err = (proc.stderr or proc.stdout or "").strip()
    short_err = err.splitlines()[0] if err else f"exit {proc.returncode}"
    return (
        f"⚠️ SINTAXIS: el archivo {path} no pasa la verificación ({checker[0]}). "
        f"Error: {short_err}. Corregí el contenido o releé el archivo."
    )


def _json_check(path: str, content: str) -> str | None:
    """Valida que un .json siga parseando. E2E real 35B: un edit con old_str
    que abarcaba DOS tareas fusionó/borró bloques de tasks.json sin que nadie
    lo notara (36KB). Se valida EN MEMORIA antes de escribir: si rompe,
    se RECHAZA (no solo avisa)."""
    import json as _json

    try:
        _json.loads(content)
        return None
    except _json.JSONDecodeError as e:
        return (
            f"⛔ Edit RECHAZADO: {path} quedaría con JSON inválido ({e}). "
            f"El archivo NO fue modificado. Revisá que tu old_str/new_str no "
            f"cruce límites de objetos (no edites dos tareas en un solo bloque)."
        )


def _post_write_check(path: str, content: str) -> str:
    """Valida el contenido recién escrito y devuelve mensajes de advertencia.

    NO bloquea el write (ya se persistió) pero alerta al modelo para que corrija
    archivos truncados o con sintaxis inválida — el gap que dejó pasar el E2E
    real donde e2e_verify.sh quedó roto en disco sin que nadie lo notara.
    """
    warnings: list[str] = []
    if Path(path).suffix.lower() in _INTEGRITY_EXTS:
        integrity = _integrity_check(content)
        if integrity:
            warnings.append(integrity)
    syntax = _syntax_check(path, content)
    if syntax:
        warnings.append(syntax)
    return "\n".join(warnings)


@tool
def write_file(path: str, content: str) -> str:
    """
    Write content to a file. Overwrites if the file exists.
    Creates parent directories if needed.
    If the path is a directory or the write fails, returns a message —
    accept it and choose a different path.
    Usage: write_file(path="/Users/me/repo/report.md", content="# Report")
    """
    p = Path(path)
    _allowed_status_flip = False
    if _is_protected_task_path(path):
        # Excepción quirúrgica (flujo del usuario: el agente mantiene el
        # status de las tareas): si el contenido nuevo es EXACTAMENTE el
        # anterior con status→DONE, se permite (validado abajo con
        # _status_only_flip). Cualquier otra cosa sigue PROHIBIDA.
        import json as _json

        try:
            _json.loads(content)
        except Exception:
            return (
                f"⛔ '{path}' es un archivo de PLANIFICACIÓN (tasks/PRD/plan) PROHIBIDO de escribir. "
                "NO lo toques: es tu fuente de verdad de la tarea. Implementá el código "
                "en los archivos del repo, no reescribas tasks.md ni planes."
            )
        if p.exists() and p.is_file():
            try:
                _allowed_status_flip = _status_only_flip(
                    _json.loads(p.read_text(encoding="utf-8")),
                    _json.loads(content),
                )
            except Exception:
                _allowed_status_flip = False
        if not _allowed_status_flip:
            return (
                f"⛔ '{path}' es un archivo de PLANIFICACIÓN (tasks/PRD/plan) PROHIBIDO de escribir. "
                "NO lo toques: es tu fuente de verdad de la tarea. Implementá el código "
                "en los archivos del repo, no reescribas tasks.md ni planes."
            )
    if p.exists() and p.is_dir():
        return (
            f"⛔ '{path}' is a DIRECTORY, not a file. "
            f"Do NOT call write_file on a directory path. "
            f"Call write_file with a FILE name inside it, for example: "
            f"write_file(path='{path}/index.ts', content='...')"
        )
    resolved = _resolved_key(path)
    resolved_overrides = {_resolved_key(o) for o in WRITE_OVERRIDE_PATHS}
    # status-flip de tasks.json (agente marca DONE) no pasa por el guard
    # anti-sobrescritura: el flip ya fue validado estructura-completa.
    if (
        p.exists() and p.is_file()
        and resolved not in resolved_overrides
        and not _allowed_status_flip
    ):
        # Guard anti-destrucción: write_file NO puede sobrescribir archivos
        # existentes (salvo configs triviales de ≤5 líneas). Reescribir desde
        # memoria pierde imports/hooks/lógica — el 4B mutiló PacientesPage.tsx.
        # Forzamos el path quirúrgico: read_file + edit_file.
        # EXCEPCIÓN: paths en WRITE_OVERRIDE_PATHS (autorizados por el
        # orquestador tras fallar la cirugía fina de edit_file N veces).
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

    if p.suffix.lower() == ".json":
        violation = _json_check(path, content)
        if violation:
            return violation
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    except (OSError, IsADirectoryError, FileExistsError) as e:
        return f"⛔ Failed to write {path}: {e}. Choose a different file path."
    _mark_read(str(p))
    result = f"✅ Written {len(content)} characters to {path}"
    if p.suffix.lower() == ".md":
        flags = _md_integrity(content)
        violation = ""
        if flags["fm"] is False and bool(content.splitlines()) and content.splitlines()[0].strip() == "---":
            violation = "frontmatter roto (línea 1 '---' sin cierre único en las primeras 40 líneas)"
        elif not flags["fences_even"]:
            violation = "cerca de código (```) desbalanceada"
        if violation:
            result += (
                f"\n⚠️ INTEGRIDAD: {violation}. El archivo fue creado pero "
                "probablemente quedó corrupto; corregilo con edit_file."
            )
    post = _post_write_check(path, content)
    if post:
        result += "\n" + post
    return result


def _md_integrity(content: str) -> dict:
    """Señales estructurales de un .md para detectar corrupción post-edit.

    E2E real (Task 8 spec-kitti): un edit_file fuzzy con old_str incorrecto
    DUPLICÓ el frontmatter (dos bloques ---) y fusionó ```text + $ARGUMENTS
    en una sola línea ($ARGUMENTS```), perdiendo el fence de apertura.
    - fm_clean: línea 1 es '---' y hay EXACTAMENTE otra '---' de cierre en
      las primeras 40 líneas (frontmatter YAML válido).
    - fences_merged: líneas que CONTIENEN ``` sin EMPEZAR por él (ej.
      '$ARGUMENTS```') — en markdown bien formado los fences viven solos
      en su línea; un fence incrustado al final de contenido es fusión.
    - fences_even: cantidad de ``` par (bloques balanceados).

    El guard solo RECHAZA cuando el archivo estaba sano ANTES del edit y el
    edit lo rompe — nunca toca archivos que ya estaban así.
    """
    lines = content.splitlines()
    fm_clean = bool(lines) and lines[0].strip() == "---" and (
        sum(1 for ln in lines[:40] if ln.strip() == "---") == 2
    )
    fences_merged = sum(
        1 for ln in lines if "```" in ln and not ln.lstrip().startswith("```")
    )
    fences_even = content.count("```") % 2 == 0
    return {
        "fm": fm_clean,
        "fences_merged": fences_merged,
        "fences_even": fences_even,
    }


def _md_integrity_violation(before: dict, after: dict) -> str:
    """Mensaje de rechazo si el edit rompió estructura que antes estaba sana."""
    if before["fm"] and not after["fm"]:
        return (
            "frontmatter duplicado o roto (la edición introdujo bloques '---' "
            "extra en las primeras 40 líneas)"
        )
    if before["fences_merged"] == 0 and after["fences_merged"] > 0:
        return (
            "línea con cerca de código (```) FUSIONADA con contenido "
            "(ej. 'texto```') — la edición eliminó saltos de línea"
        )
    if before["fences_even"] and not after["fences_even"]:
        return (
            "cerca de código (```) desbalanceada — la edición eliminó un "
            "delimitador"
        )
    return ""


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
    # Guard anti-bypass: borrar + recrear bypassea el guard de write_file
    # (E2E real: 35B borró __init__.py de 1851 líneas y escribió un stub).
    # Exigir la misma autorización que el overwrite para archivos grandes.
    if p.is_file():
        try:
            existing_lines = len(p.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            existing_lines = 0
        resolved = _resolved_key(path)
        resolved_overrides = {_resolved_key(o) for o in WRITE_OVERRIDE_PATHS}
        if existing_lines > WRITE_FILE_OVERWRITE_MAX_LINES and resolved not in resolved_overrides:
            return (
                f"⛔ '{path}' tiene {existing_lines} líneas y delete_file está "
                f"BLOQUEADO sin autorización (borrar + recrear bypassea el guard "
                f"anti-destrucción). Usá edit_file/apply_patch quirúrgico en vez "
                f"de borrar y reescribir."
            )
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
    _protected = _is_protected_task_path(path)
    if _protected:
        # Excepción quirúrgica (flujo del usuario: el agente mantiene el
        # status de las tareas): un edit SOLO status pending→DONE se permite
        # (validado estrictamente abajo sobre el JSON completo). Cualquier
        # otro cambio sigue PROHIBIDO — se rechaza después de calcular el
        # new_content (más abajo, post-spans).
        o_low = (old_str or "").lower()
        n_low = (new_str or "").lower()
        if not ("status" in o_low and "status" in n_low and "done" in n_low):
            return (
                f"⛔ '{path}' es un archivo de PLANIFICACIÓN (tasks/PRD/plan) "
                "PROHIBIDO de editar. ÚNICA excepción: marcar status "
                "pending→DONE con edit_file. NO cambies nada más: es tu "
                "fuente de verdad. Implementá el código en los archivos del repo."
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

    # INVARIANTE Read-before-Edit (fase 1): editar sin haber leído antes
    # es adivinar old_str de memoria — el 4B corrompe el archivo. Exigir
    # read_file previo en esta sesión (o archivo creado con write_file).
    if not _was_read(str(p)):
        return (
            f"⛔ Edit RECHAZADO por read-before-edit en {path}: todavía no lo "
            f"leíste en esta sesión.\n"
            f"PROCEDÉ ASÍ:\n"
            f"  1) read_file(path='{path}') para ver el contenido EXACTO.\n"
            f"  2) Rehacé el edit con old_str copiado LITERAL del archivo "
            f"(2-5 líneas de contexto)."
        )

    # GUARD ANTI-BORRADO DE FUNCIONES: edit_file con new_str VACÍO que
    # pretende borrar una función/def/class completa es destructivo y
    # sospechoso (E2E real: el modelo intentó borrar countTokens con
    # new_str="" para 'arreglar' un truncado inexistente). Borrar código
    # sin reemplazo no es un fix quirúrgico.
    if (
        not new_str.strip()
        and re.search(r"^\s*(func |def |function |class |const \w+ = \()", old_str, re.MULTILINE)
    ):
        return (
            "⛔ edit_file con new_str VACÍO intenta BORRAR una función/class "
            "completa — eso es destructivo y casi nunca es el fix correcto.\n"
            "Si creés que el archivo está corrupto/truncado, leelo COMPLETO "
            "con read_file(path) SIN start_line (el archivo casi seguro está "
            "bien). Si realmente hay que borrar, hacelo por partes pequeñas."
        )

    # GUARD ANTI-NOOP: el modelo descubre que "escribir algo" satisface la
    # presión del harness y hace edit_file con old_str == new_str en loop
    # (E2E real Task 8 batch ya-completo: 20+ no-ops hasta agotar budget).
    if old_str.strip() == new_str.strip() and old_str.strip():
        return (
            "⛔ NO-OP edit: old_str == new_str (no cambiarías NADA).\n"
            "Si los archivos YA cumplen la tarea, NO llames edit_file: "
            "corré run_lint/run_tests para verificarlo y terminá con un "
            "resumen — ese cierre es válido."
        )

    content = p.read_text(encoding="utf-8")

    # GUARD ANTI-REESCRITURA MASIVA: el modelo chico usa edit_file con
    # old_str/new_str = archivo ENTERO para "reescribir de memoria" — rompe
    # el código (interfaces inválidas, funciones duplicadas 3 veces, TS1005…
    # visto en E2E real: useConsultasList/useMigracion). write_file ya está
    # bloqueado para archivos existentes; edit_file debe ser QUIRÚRGICO.
    total_lines = len(content.splitlines())
    block_lines = len(old_str.splitlines()) + len(new_str.splitlines())
    if block_lines > MAX_EDIT_BLOCK_LINES or (total_lines > 0 and len(old_str.splitlines()) > max(10, total_lines // 2)):
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
        # ¿El cambio YA está aplicado? (old_str viejo ausente + new_str
        # presente). E2E real: el modelo repetía el MISMO edit 4 veces —
        # decía "now let me run the tests" y volvía a llamar edit_file en vez
        # de verificar. El mensaje explícito corta el loop.
        if new_str.strip() and new_str.strip() in content:
            return (
                f"old_str not found in {path} — PERO tu new_str YA ESTÁ en el "
                "archivo: el cambio ya está aplicado.\n"
                "NO repitas este edit. Si venías diciendo 'corro los tests': "
                "llamá AHORA run_lint/run_tests/run_build y terminá."
            )
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

    # GUARD ANTI-ESCAPE JSON (E2E real 4B): el modelo escapa comillas como en
    # JSON (\\\"hola\\\") dentro del tool call y el archivo queda con backslashes
    # literales → SyntaxError. Detectar y rechazar ANTES de escribir.
    if ('\\"' in new_str or "\\'" in new_str) and '\\"' not in content and "\\'" not in content:
        return (
            f"⛔ Edit RECHAZADO en {path}: tu new_str contiene secuencias escapadas "
            f"literales (\\\\\\\" o \\\\'). Los tool calls ya son JSON — NO escapes "
            f"las comillas adentro. Pasá el código LITERAL: \"hola\", no \\\\\"hola\\\\\". "
            f"Rehacé el edit con el texto exacto."
        )

    # GUARD DE SINTAXIS (E2E real 4B): edit_file escribía sin validar y un
    # segundo edit con old_str escapado corrompió app.py (return \\\"hola\\\").
    # Si el archivo estaba sano y el edit lo rompe, BLOQUEAR (no solo avisar).
    if p.suffix.lower() in _SYNTAX_CHECKERS:
        old_err = _syntax_check(path, content)
        new_err = _syntax_check(path, new_content)
        if old_err is None and new_err is not None:
            return f"⛔ Edit RECHAZADO por sintaxis en {path}: {new_err} El archivo NO fue modificado. Releé con read_file y reintentá con el bloque literal."

    # GUARD DE INTEGRIDAD .md: un edit con old_str fuzzy-incorrecto DUPLICÓ
    # frontmatter y fusionó fences (E2E real Task 8: changelog.md y
    # sync-status.md inutilizables). Rechazar ANTES de escribir si el archivo
    # estaba sano y el edit rompe estructura.
    if p.suffix.lower() == ".md":
        before = _md_integrity(content)
        after = _md_integrity(new_content)
        violation = _md_integrity_violation(before, after)
        if violation:
            return (
                f"⛔ Edit RECHAZADO por integridad estructural en {path}: {violation}.\n"
                "El archivo NO fue modificado. El old_str que pasaste no "
                "corresponde al contenido real (el matcher encontró un bloque "
                "parecido pero distinto).\n"
                "PROCEDÉ ASÍ:\n"
                f"  1) read_file(path='{path}') para ver el contenido REAL.\n"
                "  2) Rehacé el edit con old_str copiado LITERAL del archivo "
                "(2-5 líneas de contexto alrededor del cambio)."
            )

    # GUARD JSON: validar en memoria antes de escribir (ver _json_check).
    if p.suffix.lower() == ".json":
        violation = _json_check(path, new_content)
        if violation:
            return violation

    # Autorización final del status-flip en protegidos: el JSON resultante
    # debe ser EXACTAMENTE el anterior con status→DONE (nada más).
    if _protected:
        import json as _jsonf

        try:
            ok_flip = _status_only_flip(
                _jsonf.loads(content), _jsonf.loads(new_content)
            )
        except Exception:
            ok_flip = False
        if not ok_flip:
            return (
                f"⛔ '{path}' es de PLANIFICACIÓN: solo se permite marcar "
                "status pending→DONE (el flip se valida sobre el JSON "
                "completo). Tu edit cambiaría MÁS COSAS — no está permitido. "
                "NO lo reintentes."
            )

    p.write_text(new_content, encoding="utf-8")
    return f"✅ Replaced block in {path}"


@tool
def apply_patch(path: str, edits: str) -> str:
    """
    Apply multiple SEARCH/REPLACE edits to a file atomically in one approval.
    Use this when you need 2+ edits to the SAME file (e.g. 5 fields of a
    type) — it counts as ONE approval instead of N.

    `edits` is a JSON array of {old_string, new_string} objects. Each old_string
    must match exactly (with 2-5 lines of context, like edit_file). All edits
    are validated before any write: if any old_string is not found, nothing is
    written and you get an error per edit.

    Example:
    apply_patch(path="src/a.ts", edits='[{"old_string":"type Foo = {\\n  x: string;","new_string":"type Foo = {\\n  x: string;\\n  y: number;"}]')
    """
    import json as _json

    _protected = _is_protected_task_path(path)
    if _protected:
        # Excepción quirúrgica: apply_patch puede marcar status pending→DONE
        # si TODOS los edits son flips (validación final estricta abajo, sobre
        # el JSON completo resultante). Cualquier otra cosa: PROHIBIDO.
        try:
            _cand = all(
                "status" in str(e.get("old_string") or e.get("old_str") or "").lower()
                and "pending" in str(e.get("old_string") or e.get("old_str") or "").lower()
                and "status" in str(e.get("new_string") or e.get("new_str") or "").lower()
                and "done" in str(e.get("new_string") or e.get("new_str") or "").lower()
                for e in (
                    _json.loads(edits) if isinstance(edits, str) else edits
                )
            )
        except Exception:
            _cand = False
        if not _cand:
            return (
                f"⛔ '{path}' es un archivo de PLANIFICACIÓN PROHIBIDO. "
                "ÚNICA excepción: marcar status pending→DONE (todos los edits "
                "del patch deben ser flips de status). NO toques nada más: es "
                "tu fuente de verdad."
            )
    p = Path(path)
    if not p.exists():
        return f"File does not exist: {path}. Create it with write_file first."
    if p.is_dir():
        return f"'{path}' is a directory, not a file."
    # INVARIANTE Read-before-Edit (fase 1, igual que edit_file).
    if not _was_read(str(p)):
        return (
            f"⛔ Patch RECHAZADO por read-before-edit en {path}: todavía no lo "
            f"leíste en esta sesión.\n"
            f"PROCEDÉ ASÍ:\n"
            f"  1) read_file(path='{path}') para ver el contenido EXACTO.\n"
            f"  2) Rehacé el patch con old_string copiados LITERALES."
        )
    try:
        parsed = _json.loads(edits) if isinstance(edits, str) else edits
    except Exception as e:
        return f"⛔ edits must be a JSON array of {{old_string, new_string}}: {e}"
    if not isinstance(parsed, list) or not parsed:
        return "⛔ edits must be a non-empty JSON array of {old_string, new_string}."
    # Idempotence: if every new_string already in file and no old_string found, skip.
    try:
        content = p.read_text(encoding="utf-8")
    except OSError as e:
        return f"⛔ Failed to read {path}: {e}"
    # Fast idempotence: if every new_string already in file, skip.
    # (old_str may still be substring of new_str, e.g. new = old + "\n  b;")
    if all(
        (e.get("new_string") or e.get("new_str") or "").strip() in content
        for e in parsed
    ):
        return (
            f"All {len(parsed)} edits already applied in {path} — no changes needed. "
            "Run verification and finish."
        )
    # Validate each edit has required keys and size guard
    for idx, e in enumerate(parsed):
        old = e.get("old_string") if "old_string" in e else e.get("old_str", "")
        new = e.get("new_string") if "new_string" in e else e.get("new_str", "")
        if old is None or new is None:
            return f"⛔ Edit #{idx+1} missing old_string/new_string."
        if old.strip() == new.strip() and old.strip():
            return f"⛔ Edit #{idx+1} is NO-OP (old_string == new_string)."
    # Apply sequentially on updated content (re-finding spans each time)
    new_content = content
    for idx, e in enumerate(parsed):
        old = e.get("old_string") if "old_string" in e else e.get("old_str", "")
        new = e.get("new_string") if "new_string" in e else e.get("new_str", "")
        spans = _find_spans(new_content, old)
        if not spans:
            if new.strip() and new.strip() in new_content:
                continue  # already applied, skip this patch
            return (
                f"⛔ Edit #{idx+1} old_string not found in {path} (after previous patches). "
                f"Re-read the file and fix old_string for patch {idx+1}."
            )
        if len(spans) > 1:
            return f"⛔ Edit #{idx+1} ambiguous ({len(spans)} matches) in {path} — add more context."
        s, en = spans[0]
        new_content = new_content[:s] + new + new_content[en:]
    if new_content == content:
        return f"No changes applied to {path} (all patches were no-ops)."
    # Integrity guard for .md like edit_file
    if p.suffix.lower() == ".md":
        before = _md_integrity(content)
        after = _md_integrity(new_content)
        violation = _md_integrity_violation(before, after)
        if violation:
            return f"⛔ Patch RECHAZADO por integridad en {path}: {violation}."
    if p.suffix.lower() == ".json":
        violation = _json_check(path, new_content)
        if violation:
            return violation
    # Autorización final del status-flip en protegidos (apply_patch): el JSON
    # resultante debe ser EXACTAMENTE el anterior con status→DONE.
    if _protected:
        try:
            ok_flip = _status_only_flip(
                _json.loads(content), _json.loads(new_content)
            )
        except Exception:
            ok_flip = False
        if not ok_flip:
            return (
                f"⛔ '{path}' es de PLANIFICACIÓN: solo se permite marcar "
                "status pending→DONE. Tu patch cambiaría MÁS COSAS — no está "
                "permitido. NO lo reintentes."
            )
    p.write_text(new_content, encoding="utf-8")
    return f"✅ Applied {len(parsed)} patch(es) to {path} atomically (1 approval)."


def _exact_spans(content: str, needle: str) -> list[tuple[int, int]]:
    """All spans of an exact substring match.

    Si el bloque es MULTILINEA, el span debe alinearse a líneas COMPLETAS:
    un match que termina a mitad de línea (ej. última línea 'from pathlib
    import Path' contra 'from pathlib import Path, PureWindowsPath') es un
    match por PREFIJO y reemplazar corrompe la línea (E2E real: borró
    ', PureWindowsPath' de un archivo real). Los old_str de UNA línea
    permiten fragmentos in-line (ej. 'useState(null)' dentro de una línea).
    """
    multiline = "\n" in needle
    spans: list[tuple[int, int]] = []
    idx = content.find(needle)
    while idx >= 0:
        end = idx + len(needle)
        if not multiline or (
            (idx == 0 or content[idx - 1] == "\n")
            and (end == len(content) or content[end] == "\n")
        ):
            spans.append((idx, end))
        idx = content.find(needle, idx + len(needle))
    return spans


def _build_fuzzy_regex(old_str: str) -> re.Pattern | None:
    """Regex tolerante a espacios/indentación: cada línea del bloque se matchea
    por su contenido stripped, permitiendo whitespace variable entre palabras
    y en los bordes. Ignora líneas vacías del bloque.

    Cada línea se ancla al FINAL (\\s*$): sin anclaje, el matcher hacía match
    por PREFIJO y reemplazaba líneas incompletas (E2E real: 'from pathlib
    import Path' matcheó 'from pathlib import Path, PureWindowsPath' y borró
    ', PureWindowsPath' de un archivo real)."""
    lines = [ln.strip() for ln in old_str.splitlines() if ln.strip()]
    if not lines:
        return None
    parts = []
    for ln in lines:
        parts.append(re.escape(ln).replace(r"\ ", r"\s+") + r"\s*$")
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
                start = sum(len(s) + 1 for s in content_lines[:i])
                end = start + sum(len(s) + 1 for s in content_lines[i : i + j + 1])
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
