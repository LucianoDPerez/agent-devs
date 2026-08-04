"""Operaciones de Git y GitHub (PRs) para el repositorio actual.

Usa `git` y `gh` vía subprocess. Todas las tools reciben `path` (raíz del repo)
y ejecutan el comando en ese directorio. Los límites de salida evitan
saturar el contexto del modelo local.
"""

import subprocess
from pathlib import Path

from langchain_core.tools import ToolException, tool

_MAX_DIFF_BYTES = 30_000
_MAX_LOG_LINES = 50


def _run(path: str, args: list[str], timeout: int = 60) -> str:
    """Ejecuta un comando git/gh en `path` y devuelve stdout+stderr limpio."""
    root = Path(path)
    if not root.exists():
        raise ToolException(f"Path does not exist: {path}")

    try:
        proc = subprocess.run(
            args,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise ToolException(f"Command timed out after {timeout}s: {' '.join(args)}") from None
    except OSError as e:
        raise ToolException(f"Failed to run command: {e}") from e

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        detail = err or out or "unknown error"
        raise ToolException(
            f"Command failed ({proc.returncode}): {' '.join(args)}\n{detail}"
        )
    return out


def _truncate(text: str, limit: int = _MAX_DIFF_BYTES) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... (truncated at {limit:,} bytes)"


# --- Implementaciones internas (sin StructuredTool) para reuso entre tools ---

def _current_branch_impl(path: str) -> str:
    return _run(path, ["git", "branch", "--show-current"]) or "(detached HEAD)"


def _changed_files_impl(path: str) -> str:
    out = _run(path, ["git", "status", "--porcelain"])
    if not out:
        return "Working tree clean: no changed files."
    return "Changed files (X|Y porcelain status):\n" + out


def _git_status_impl(path: str) -> str:
    branch = _run(path, ["git", "rev-parse", "--abbrev-ref", "HEAD"]) or None
    parts = [f"🌿 Branch: {branch or 'HEAD (detached)'}"]

    if branch and branch != "HEAD":
        try:
            aheadbehind = _run(
                path, ["git", "rev-list", "--left-right", "--count", f"origin/{branch}...HEAD"]
            )
            left, right = aheadbehind.split()
            parts.append(f"⬇️ behind {left} / ⬆️ ahead {right} (origin/{branch})")
        except ToolException:
            parts.append(f"(no upstream for {branch})")
        except Exception:
            parts.append(f"(no upstream for {branch})")

    short = _run(path, ["git", "status", "--short"])
    parts.append(short if short else "Working tree clean.")
    return "\n".join(parts)


def _git_log_impl(path: str, limit: int = 20) -> str:
    limit = max(1, min(int(limit), _MAX_LOG_LINES))
    fmt = "%h %ad %s"
    return _run(path, ["git", "log", "-n", str(limit), "--date=short", "--pretty=format:" + fmt])


# --- Tools públicas (wrappers) ---

@tool
def current_branch(path: str) -> str:
    """Get the name of the current branch in a git repository."""
    return _current_branch_impl(path)


@tool
def changed_files(path: str) -> str:
    """List files with changes not yet committed (staged + unstaged + untracked)."""
    return _changed_files_impl(path)


@tool
def git_status(path: str) -> str:
    """Summary of repo status: current branch, ahead/behind remote, and porcelain status."""
    return _git_status_impl(path)


@tool
def git_log(path: str, limit: int = 20) -> str:
    """Show recent commit history (oneline). `limit` caps how many commits to show."""
    return _git_log_impl(path)


@tool
def stage_files(path: str, files: str) -> str:
    """Stage one or more files before commit. Pass '.' to stage everything."""
    paths = [f.strip() for f in files.replace(",", " ").split() if f.strip()]
    paths = paths or ["."]
    _run(path, ["git", "add", "--"] + paths)
    return f"✅ staged {len(paths)} file(s): {', '.join(paths)}"


@tool
def create_commit(path: str, message: str) -> str:
    """Commit the staged changes with the given message. Use conventional commits."""
    staged = _run(path, ["git", "diff", "--cached", "--name-only"])
    if not staged:
        _run(path, ["git", "add", "-u"])
        message = message + "\n\n(auto-staged tracked changes)"
    _run(path, ["git", "commit", "-m", message])
    return "✅ Commit created:\n" + _run(path, ["git", "log", "-1"])


@tool
def push(path: str, remote: str | None = None, branch: str | None = None) -> str:
    """Push the current branch (or `branch`) to `remote` (default 'origin')."""
    branch = branch or _current_branch_impl(path)
    remote = remote or "origin"
    try:
        _run(path, ["git", "push", remote, branch])
    except ToolException:
        _run(path, ["git", "push", "-u", remote, branch])
    return f"🚀 Pushed {branch} to {remote}"


@tool
def create_pr(path: str, title: str, body: str = "", base: str = "main") -> str:
    """Push the current branch and open a Pull Request with `gh`."""
    branch = _current_branch_impl(path)
    if branch == base:
        raise ToolException(f"Cannot open a PR to {base} from the same branch.")
    _run(path, ["git", "push", "-u", "origin", branch])

    args = ["gh", "pr", "create", "--title", title, "--base", base, "--head", branch]
    if body:
        args += ["--body", body]
    url = _run(path, args)
    return f"✅ PR created: {url}"


@tool
def read_pr(path: str, number: int | None = None) -> str:
    """Read a PR for review: full metadata plus the diff. If `number` is None, targets the PR for the current branch."""
    # gh pr view takes the PR number as a positional arg (not --number)
    view_args = ["gh", "pr", "view"]
    if number is not None:
        view_args.append(str(number))
    view_args += [
        "--json",
        "number,title,state,url,body,additions,deletions,changedFiles,headRefName,baseRefName,reviewDecision",
    ]
    info = _run(path, view_args)

    diff_args = ["gh", "pr", "diff"]
    if number is not None:
        diff_args.append(str(number))
    diff = _truncate(_run(path, diff_args))
    return f"{info}\n\n--- DIFF ---\n{diff}"


@tool
def list_prs(path: str, state: str = "open") -> str:
    """List the pull requests of the repository for review. state: 'open' | 'closed' | 'merged' | 'all'."""
    valid = {"open", "closed", "merged", "all"}
    if state not in valid:
        raise ToolException(f"Invalid state '{state}'. Must be one of {sorted(valid)}")
    out = _run(path, [
        "gh", "pr", "list", "--state", state,
        "--json", "number,title,isDraft,headRefName,baseRefName,reviewStatus",
        "--limit", "30",
    ])
    return out or f"No PRs found for state '{state}'."
