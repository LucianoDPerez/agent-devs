"""Lint, tests y build con auto-detect de stack (Node, Python, Go)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from langchain_core.tools import tool

_MAX_OUTPUT_BYTES = 40_000
_DEFAULT_TIMEOUT = 180


def _truncate(text: str, limit: int = _MAX_OUTPUT_BYTES) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... (truncated at {limit:,} bytes)"


def _validate_cwd(path: str) -> str | None:
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


def _detect_stack(root: Path) -> str | None:
    if (root / "package.json").is_file():
        return "node"
    if (root / "go.mod").is_file():
        return "go"
    if any((root / name).is_file() for name in ("pyproject.toml", "requirements.txt", "setup.py")):
        return "python"
    return None


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

    # python
    if action == "lint":
        if _python_has_ruff(root):
            return ["ruff", "check", "."]
        return "No ruff configuration or dependency found for Python linting"
    if action == "test":
        return ["pytest"]
    if action == "build":
        if _python_has_build_system(root):
            return ["python", "-m", "build"]
        return "No [build-system] in pyproject.toml — build not configured"
    return f"Unknown action: {action}"


def _needs_node_install(root: Path) -> bool:
    """Detects if node_modules is missing or stale."""
    if not (root / "package.json").is_file():
        return False
    if not (root / "node_modules").is_dir():
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

    # Auto-detect missing dependencies and suggest install
    if "FAILED" in result:
        stack = _detect_stack(root)
        if stack == "node" and _needs_node_install(root):
            result += "\n⚠️  node_modules missing or incomplete. Run: run_install(path=...) first."
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
