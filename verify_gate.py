"""Compuerta de verificación post-escritura para turnos EXECUTE.

Los LLM chicos a veces escriben código que no compila (paréntesis sin cerrar,
JSX malformado, etc.) y no lo verifican. Esta compuerta corre el build/lint del
proyecto justo después de que EXECUTE escribe, y si el error apunta a un archivo
que acabamos de tocar, devuelve (False, error) para que el orquestador reintente
inyectándole el error exacto al modelo.

Diseño FAIL-OPEN: ante cualquier duda (sin git, sin build, timeout, error en un
archivo que no tocamos) devuelve (True, "") — nunca bloquea al usuario.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

CODE_EXTS = {
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".py", ".go", ".vue", ".svelte", ".sh",
}

# Extensiones cuyo archivo se valida con un chequeo de sintaxis directo (sin
# build del proyecto) porque son scripts sueltos sin stack compilable. Mapeo
# extensión → (comando, descripción). Fail-open si el binario no existe.
_SYNTAX_EXTS = {
    ".sh": ("bash", "-n"),
}

_MAX_ERROR_CHARS = 3_500
_GIT_TIMEOUT = 10


def _git(repo_path: str, args: list[str], sink: set[str]) -> None:
    try:
        out = subprocess.run(
            args, cwd=repo_path, capture_output=True, text=True, timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return
    if out.returncode != 0:
        return
    for line in out.stdout.splitlines():
        line = line.strip()
        if line:
            sink.add(line)


def changed_code_files(repo_path: str) -> list[str]:
    """Archivos de código modificados por el último turno EXECUTE.

    Prioriza cambios sin commitear (staged + unstaged + untracked). Si no hay
    ninguno (el agente ya commiteó), mira el último commit.
    """
    root = Path(repo_path)
    if not (root / ".git").exists():
        return []

    names: set[str] = set()
    _git(repo_path, ["git", "diff", "--name-only", "HEAD"], names)
    _git(repo_path, ["git", "ls-files", "--others", "--exclude-standard"], names)
    if not names:
        _git(repo_path, ["git", "diff", "--name-only", "HEAD~1", "HEAD"], names)

    return sorted(n for n in names if Path(n).suffix.lower() in CODE_EXTS)


def _find_build_dir(repo_path: str, files: list[str]) -> str | None:
    """Directorio con package.json más cercano a los archivos cambiados.

    En monorepos (frontend/ con su propio package.json) el build debe correr en
    el subdirectorio, no en la raíz. Devuelve None si no hay package.json.
    """
    root = Path(repo_path).resolve()
    candidates: dict[str, int] = {}
    for rel in files:
        current = (root / rel).resolve().parent
        while True:
            if (current / "package.json").is_file():
                key = str(current)
                candidates[key] = candidates.get(key, 0) + 1
                break
            if current == root or current.parent == current:
                break
            current = current.parent

    if not candidates:
        if (root / "package.json").is_file():
            return str(root)
        return None
    # El directorio que cubre más archivos cambiados; empate → el más profundo.
    return max(candidates, key=lambda k: (candidates[k], len(k)))


def _run_build(build_dir: str) -> str:
    from tools.verify import _run_verify

    result = _run_verify(build_dir, "build")
    # Si no hay script/config de build, caemos a lint como verificación de sintaxis.
    if "No 'build' script" in result or "build not configured" in result:
        result = _run_verify(build_dir, "lint")
    return result


def _implicated(changed_file: str, error_text: str) -> bool:
    """True si el error del build menciona el archivo que cambiamos."""
    name = Path(changed_file).name
    if name and name in error_text:
        return True
    # También matchea el path relativo (los builds suelen imprimirlo).
    return changed_file.replace("\\", "/") in error_text.replace("\\", "/")


def _syntax_errors(repo_path: str, files: list[str]) -> tuple[bool, str]:
    """Valida por sintaxis directa los scripts sueltos (p. ej. .sh con bash -n).

    El caso que se escapó en E2E: un script bash que el LLM escribió TRUNCADO
    (write_file devolvió éxito pero `bash -n` falla). No hay stack/package.json
    que el build detecte, así que la compuerta no lo veía. Acá se corre el
    intérprete sobre CADA archivo modificado con extensión sintáctica.

    Retorna (ok, error_recortado). Fail-open: si el binario no existe o falla
    el subprocess, no bloquea.
    """
    import subprocess

    root = Path(repo_path)
    first_error = ""
    for rel in files:
        ext = Path(rel).suffix.lower()
        checker = _SYNTAX_EXTS.get(ext)
        if checker is None:
            continue
        target = root / rel
        if not target.is_file():
            continue
        try:
            proc = subprocess.run(
                [*checker, str(target)],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            continue  # binario ausente / fallo de entorno → fail-open
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            first_error = (
                f"Script {rel} no pasa la verificación de sintaxis "
                f"({checker[0]}):\n{err[:_MAX_ERROR_CHARS]}"
            )
            break
    if first_error:
        return False, first_error
    return True, ""


def syntax_gate(repo_path: str) -> tuple[bool, str]:
    """Corre el build/lint tras una escritura. Devuelve (ok, error_recortado).

    (True, "")  → el código compila, o no podemos/No-debemos bloquear.
    (False, err)→ el build falló por un archivo que acabamos de tocar.
    """
    try:
        files = changed_code_files(repo_path)
        if not files:
            return True, ""

        # 1) Scripts sueltos (p. ej. .sh) con sintaxis directa — el caso que el
        #    build del proyecto no detecta (no hay package.json/stack compilable).
        ok, err = _syntax_errors(repo_path, files)
        if not ok:
            return False, err

        build_dir = _find_build_dir(repo_path, files)
        if not build_dir:
            # Sin package.json: puede ser un repo Python/Go → verificamos en la raíz.
            from tools.verify import _detect_stack

            if _detect_stack(Path(repo_path)) in ("python", "go"):
                build_dir = str(Path(repo_path).resolve())
            else:
                return True, ""  # nada que compilar

        result = _run_build(build_dir)
        if "[PASSED]" in result:
            return True, ""
        if "[FAILED]" not in result:
            # Mensaje informativo (sin script, sin deps) → no bloqueamos.
            return True, ""

        if any(_implicated(f, result) for f in files):
            return False, result[:_MAX_ERROR_CHARS]
        return True, ""  # fallo en un archivo que no tocamos → preexistente.
    except Exception:
        return True, ""
