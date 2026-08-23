"""Lint, tests y build con auto-detect de stack (Node, Python, Go)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from langchain_core.tools import tool

try:
    from config import AUTO_INSTALL_ON_VERIFY_FAIL
except ImportError:  # pragma: no cover - tests sin config
    AUTO_INSTALL_ON_VERIFY_FAIL = True

_MAX_OUTPUT_BYTES = 40_000
_DEFAULT_TIMEOUT = 180


def _truncate(text: str, limit: int = _MAX_OUTPUT_BYTES) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... (truncated at {limit:,} bytes)"


def _validate_cwd(path: str) -> str | None:
    if not path or not str(path).strip():
        return "No path provided. Pass the project root directory: run_lint(path=\"/abs/path/to/repo\")"
    root = Path(path)
    if not root.exists():
        return f"Path does not exist: {path}"
    if not root.is_dir():
        return f"'{path}' is not a directory. Pass the project root directory."
    return None


def _run_command(path: str, args: list[str], timeout: int = _DEFAULT_TIMEOUT) -> str:
    """Ejecuta un comando y siempre devuelve string con exit code (nunca raise por fallo)."""
    try:
        proc = subprocess.run(
            args,
            cwd=path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout}s: {' '.join(args)}"
    except OSError as e:
        return f"Failed to run command {' '.join(args)}: {e}"

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    combined = "\n".join(part for part in (out, err) if part)
    combined = _truncate(combined) if combined else "(no output)"

    status = "PASSED" if proc.returncode == 0 else "FAILED"
    return (
        f"[{status}] exit={proc.returncode}\n"
        f"$ {' '.join(args)}\n"
        f"{'─' * 60}\n"
        f"{combined}"
    )


def _node_package_manager(root: Path) -> str:
    if (root / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (root / "yarn.lock").exists():
        return "yarn"
    return "npm"


def _read_package_json(root: Path) -> dict | None:
    pkg_path = root / "package.json"
    if not pkg_path.is_file():
        return None
    try:
        return json.loads(pkg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _node_script(root: Path, script_name: str) -> list[str] | str:
    pkg = _read_package_json(root)
    if pkg is None:
        return f"Could not read package.json in {root}"

    scripts = pkg.get("scripts") or {}
    if script_name not in scripts:
        return f"No '{script_name}' script in package.json"

    pm = _node_package_manager(root)
    if pm == "pnpm":
        return ["pnpm", "run", script_name]
    if pm == "yarn":
        return ["yarn", script_name]
    return ["npm", "run", script_name]


def _python_has_ruff(root: Path) -> bool:
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        text = pyproject.read_text(encoding="utf-8", errors="replace")
        if "ruff" in text:
            return True

    for req_file in ("requirements.txt", "requirements-dev.txt"):
        req_path = root / req_file
        if req_path.is_file() and "ruff" in req_path.read_text(encoding="utf-8", errors="replace"):
            return True
    return False


def _python_has_build_system(root: Path) -> bool:
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return False
    text = pyproject.read_text(encoding="utf-8", errors="replace")
    return "[build-system]" in text


def _python_uses_uv(root: Path) -> bool:
    """True si el proyecto Python se gestiona con uv (uv.lock presente).

    Los proyectos modernos con uv (pyproject.toml + uv.lock) NO se pueden
    verificar con `pytest`/`ruff` planos del PATH — sus deps viven en el
    entorno uv. El harness fallaba E2E real: demo-spec-kitti usa uv y
    el agente no lograba verificar porque run_tests corría `pytest` plano (sin
    deps) → loop infinito. Si hay uv.lock, usamos `uv run` / `uv sync`.
    """
    if (root / "uv.lock").is_file():
        return True
    # También aceptar pyproject.toml con sección [tool.uv] o dependency de uv
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        text = pyproject.read_text(encoding="utf-8", errors="replace")
        if "[tool.uv]" in text:
            return True
    return False


def _java_build_tool(root: Path) -> str | None:
    """Detecta el build tool de un proyecto Java/Gradle/Maven."""
    if (root / "build.gradle").is_file() or (root / "build.gradle.kts").is_file():
        return "gradle"
    if (root / "pom.xml").is_file():
        return "maven"
    return None


def _detect_stack(root: Path) -> str | None:
    if (root / "package.json").is_file():
        return "node"
    if (root / "go.mod").is_file():
        return "go"
    if any((root / name).is_file() for name in ("pyproject.toml", "requirements.txt", "setup.py")):
        return "python"
    if _java_build_tool(root):
        return "java"
    return None


def _gradle_cmd(root: Path, *args: str) -> list[str]:
    """Comando gradle usando el wrapper si existe, sino gradle global."""
    wrapper = root / "gradlew"
    if wrapper.is_file():
        return [str(wrapper), *args]
    if (root / "gradlew.bat").is_file():
        return ["cmd", "/c", "gradlew.bat", *args]
    return ["gradle", *args]


def _resolve_command(root: Path, action: str) -> list[str] | str:
    stack = _detect_stack(root)
    if stack is None:
        return (
            f"Could not detect project type in {root}. "
            "Looked for package.json, pyproject.toml, requirements.txt, setup.py, go.mod."
        )

    if stack == "node":
        script_map = {"lint": "lint", "test": "test", "build": "build"}
        return _node_script(root, script_map[action])

    if stack == "go":
        go_cmds = {
            "lint": ["go", "vet", "./..."],
            "test": ["go", "test", "./..."],
            "build": ["go", "build", "./..."],
        }
        return go_cmds[action]

    if stack == "java":
        tool = _java_build_tool(root)
        if tool == "gradle":
            gradle_cmds = {
                # Gradle no tiene lint standard; compileJava valida compilación (tipos).
                "lint": _gradle_cmd(root, "compileJava", "--console=plain"),
                # test con --tests vacío? No: corre la suite completa. Usar test.
                "test": _gradle_cmd(root, "test", "--console=plain"),
                # build -x test: compila + empaqueta sin repetir la suite (la
                # suite ya la corrió run_tests). --offline evita red si las deps
                # están cacheadas; si falla, el auto-install/retry online la re-resuelve.
                "build": _gradle_cmd(root, "build", "-x", "test", "--console=plain"),
            }
            return gradle_cmds[action]
        if tool == "maven":
            mvn_cmds = {
                "lint": ["mvn", "compile", "-q"],
                "test": ["mvn", "test", "-q"],
                "build": ["mvn", "package", "-DskipTests", "-q"],
            }
            return mvn_cmds[action]

    # python
    uv_py = _python_uses_uv(root)
    if action == "lint":
        if _python_has_ruff(root):
            return ["uv", "run", "ruff", "check", "."] if uv_py else ["ruff", "check", "."]
        return "No ruff configuration or dependency found for Python linting"
    if action == "test":
        return ["uv", "run", "pytest"] if uv_py else ["pytest"]
    if action == "build":
        if _python_has_build_system(root):
            return ["uv", "build"] if uv_py else ["python", "-m", "build"]
        return "No [build-system] in pyproject.toml — build not configured"
    return f"Unknown action: {action}"


def _any_dep_missing(root: Path, pkg: dict) -> bool:
    """True si alguna dependency/devDependency declarada NO está en node_modules.

    Con workspaces, npm hoistea casi todo a node_modules raíz: chequear que
    cada dep tenga su carpeta ahí detecta installs incompletos (ej: npm install
    falló a mitad por un 404 de otra dep, o se agregó una dep después de la
    última instalación — el caso @types/supertest: declarada y lockeada pero
    nunca materializada).
    """
    for section in ("dependencies", "devDependencies"):
        for dep in (pkg.get(section) or {}):
            dep_dir = root / "node_modules" / dep
            if not dep_dir.is_dir():
                return True
    return False


def _needs_node_install(root: Path) -> bool:
    """Detects if node_modules is missing or stale."""
    if not (root / "package.json").is_file():
        return False
    if not (root / "node_modules").is_dir():
        return True
    pkg = _read_package_json(root)
    # Deps declaradas (raíz o workspaces) pero ausentes en node_modules →
    # install incompleto. npm install las completa (es idempotente).
    if pkg and _any_dep_missing(root, pkg):
        return True
    # Workspaces declarados en package.json pero SIN sus symlinks en
    # node_modules → install stale (ej: se agregó "workspaces" después de la
    # última instalación; npm no materializó los bins del workspace y los
    # scripts -w fallan con "command not found"). OJO: npm crea el symlink con
    # el NOMBRE DEL PAQUETE (node_modules/<name>), no con la carpeta.
    workspaces = pkg.get("workspaces") or [] if pkg else []
    for ws in workspaces:
        ws_pkg = _read_package_json(root / ws)
        link_name = (ws_pkg or {}).get("name") or Path(ws).name
        if not (root / "node_modules" / link_name).exists():
            return True
        if ws_pkg and _any_dep_missing(root, ws_pkg):
            return True
    # node_modules/.package-lock.json missing → likely incomplete install
    pm = _node_package_manager(root)
    lockfile = f"node_modules/.{pm}-install"
    return not (root / lockfile).is_file() and not (root / "node_modules/.package-lock.json").is_file()


def _run_node_install(root: Path) -> str:
    pm = _node_package_manager(root)
    cmd = [pm, "install"] if pm != "pnpm" else ["pnpm", "install", "--frozen-lockfile"]
    # Fall back to plain install if frozen-lockfile fails
    result = _run_command(str(root), cmd)
    if "FAILED" in result and "frozen-lockfile" in result:
        result = _run_command(str(root), ["pnpm", "install"])
    return result


def _run_python_install(root: Path) -> str:
    if _python_uses_uv(root):
        # Proyecto con uv: uv sync crea/actualiza el .venv desde uv.lock y
        # resuelve todas las deps. Mucho más robusto que venv+pip para estos
        # proyectos (E2E real: el harness corría pip -e . y el agente no
        # lograba instalar, entrando en loop de verificación fallida).
        return _run_command(str(root), ["uv", "sync"])
    venv = root / ".venv"
    if not venv.is_dir():
        return _run_command(str(root), ["python3", "-m", "venv", ".venv"])
    pip = venv / "bin" / "pip"
    if not pip.is_file():
        return f"No pip in {venv}"
    return _run_command(str(root), [str(pip), "install", "-e", "."])


def _run_verify(path: str, action: str) -> str:
    error = _validate_cwd(path)
    if error:
        return error

    root = Path(path)
    command = _resolve_command(root, action)
    if isinstance(command, str):
        return command

    result = _run_command(path, command)

    # Auto-detect missing dependencies: los LLM chicos (4B/9B) sistemáticamente
    # ignoran el hint de correr run_install (visto N veces en E2E real: el
    # flujo se traba para siempre en "command not found"). Cuando el verify
    # FALLA y faltan deps declaradas, el harness instala solo (npm install es
    # idempotente) y re-corre el verify UNA vez. Config: AUTO_INSTALL_ON_VERIFY_FAIL.
    if "FAILED" in result and AUTO_INSTALL_ON_VERIFY_FAIL:
        stack = _detect_stack(root)
        if stack == "node" and _needs_node_install(root):
            result += (
                "\n⚠️  Dependencias faltantes detectadas (node_modules incompleto). "
                "El sistema ejecuta npm install automáticamente y re-corre la verificación..."
            )
            install_result = _run_node_install(root)
            result += "\n\n--- npm install ---\n" + install_result
            rerun = _run_command(path, command)
            result += "\n\n--- Re-run después del install ---\n" + rerun
        elif stack == "python" and not (root / ".venv").is_dir():
            result += "\n⚠️  .venv missing. Run: run_install(path=...) first."

    return result


@tool
def run_lint(path: str) -> str:
    """
    Run the linter for the project at path (auto-detects Node/Python/Go).
    Node: npm/pnpm/yarn run lint. Python: ruff check. Go: go vet ./...
    Usage: run_lint(path="/Users/me/repo")
    """
    return _run_verify(path, "lint")


@tool
def run_tests(path: str) -> str:
    """
    Run the test suite for the project at path (auto-detects Node/Python/Go).
    Node: npm/pnpm/yarn run test. Python: pytest. Go: go test ./...
    Usage: run_tests(path="/Users/me/repo")
    """
    return _run_verify(path, "test")


@tool
def run_build(path: str) -> str:
    """
    Run the build for the project at path (auto-detects Node/Python/Go).
    Node: npm/pnpm/yarn run build. Python: python -m build. Go: go build ./...
    Usage: run_build(path="/Users/me/repo")
    """
    return _run_verify(path, "build")


@tool
def run_install(path: str) -> str:
    """
    Install project dependencies (auto-detects Node/Python/Go).
    Node: npm/pnpm/yarn install. Python: pip install -e . Go: go mod download.
    Run this BEFORE run_lint / run_tests / run_build if dependencies are missing.
    Usage: run_install(path="/Users/me/repo")
    """
    error = _validate_cwd(path)
    if error:
        return error

    root = Path(path)
    stack = _detect_stack(root)
    if stack is None:
        return f"Could not detect project type in {root}"

    if stack == "node":
        if _needs_node_install(root):
            return _run_node_install(root)
        return "✅ Dependencies already installed (node_modules exists)."
    if stack == "go":
        return _run_command(str(root), ["go", "mod", "download"])
    if stack == "python":
        return _run_python_install(root)
    return f"Unknown stack: {stack}"


@tool
def run_npm_script(path: str, script: str) -> str:
    """Run a script DECLARED in the project's package.json (npm/pnpm/yarn run).

    Use it for project-specific lifecycle scripts that the verify tools don't
    cover — e.g. prisma generate (db:generate), db:migrate, db:studio, dev.
    Only scripts that EXIST in package.json can run (no arbitrary commands).
    If the script doesn't exist, the available scripts are listed.

    Usage: run_npm_script(path="/Users/me/repo", script="db:generate")
    """
    error = _validate_cwd(path)
    if error:
        return error

    root = Path(path)
    stack = _detect_stack(root)
    if stack is not None and stack != "node":
        # Este repo NO es Node: run_npm_script solo sirve para scripts declarados
        # en package.json. Devolver un string NO basta — el modelo local ignora
        # los mensajes de las tools y repite la misma llamada (E2E real: llamó
        # run_npm_script con "install" dos veces en un repo Python, quemando el
        # presupuesto). Lanzamos ToolBudgetExceeded (GraphBubbleUp): ToolNode lo
        # RE-LANZA, session.py lo captura y reintenta con un agente write-only
        # que NO tiene run_npm_script — el modelo ya no puede volver a llamarlo
        # y queda forzado a run_install/run_lint/run_tests.
        hint = {
            "python": "run_install(path=...) / run_lint / run_tests (pip/uv/pytest)",
            "go": "run_install(path=...) / run_lint / run_tests / run_build (go mod/vet/test)",
            "java": "run_install(path=...) / run_lint / run_tests / run_build (gradle/maven)",
        }.get(stack, "run_install / run_lint / run_tests")
        from orchestration.tool_dedupe import ToolBudgetExceeded
        raise ToolBudgetExceeded(
            f"⛔ run_npm_script solo funciona en repos NODE (con package.json). "
            f"Este proyecto se detectó como {stack.upper()}. "
            f"Usá {hint} en su lugar."
        )

    resolved = _node_script(root, script)
    if isinstance(resolved, str):
        pkg = _read_package_json(root)
        scripts = ", ".join(sorted((pkg or {}).get("scripts") or {}))
        return f"{resolved}. Available scripts: {scripts or '(none)'}"

    result = _run_command(path, resolved)
    return result
