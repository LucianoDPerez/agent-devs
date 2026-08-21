"""Tests de la compuerta de verificación post-escritura (verify_gate)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from verify_gate import (
    CODE_EXTS,
    _find_build_dir,
    _implicated,
    changed_code_files,
    syntax_gate,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
    )


def _init_repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.com")
    _git(tmp_path, "config", "user.name", "t")
    return tmp_path


# ── _implicated ──────────────────────────────────────────────────────────────


def test_implicated_matches_basename():
    err = "frontend/src/pages/PacientesPage.tsx(47,78): error TS1005: ',' expected."
    assert _implicated("frontend/src/pages/PacientesPage.tsx", err)


def test_implicated_matches_name_only():
    err = "error: Unexpected token in PacientesPage.tsx at 47:78"
    assert _implicated("some/other/dir/PacientesPage.tsx", err)


def test_implicated_no_match():
    err = "error: SomeOtherFile.tsx failed to compile"
    assert not _implicated("frontend/src/pages/PacientesPage.tsx", err)


# ── changed_code_files ───────────────────────────────────────────────────────


def test_changed_code_files_filters_by_extension(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "a.tsx").write_text("x")
    (repo / "b.md").write_text("x")
    (repo / "c.css").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    (repo / "a.tsx").write_text("y")
    (repo / "b.md").write_text("y")
    (repo / "d.py").write_text("new file")

    files = changed_code_files(str(repo))
    assert "a.tsx" in files
    assert "d.py" in files
    assert "b.md" not in files


def test_changed_code_files_empty_without_git(tmp_path):
    (tmp_path / "a.tsx").write_text("x")
    assert changed_code_files(str(tmp_path)) == []


def test_changed_code_files_detects_last_commit_when_clean(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "a.tsx").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    (repo / "a.tsx").write_text("y")
    _git(repo, "commit", "-qam", "change")

    files = changed_code_files(str(repo))
    assert "a.tsx" in files


def test_code_exts_cover_common_stacks():
    assert ".tsx" in CODE_EXTS
    assert ".py" in CODE_EXTS
    assert ".go" in CODE_EXTS
    assert ".sh" in CODE_EXTS  # Corrección 2: scripts bash entran en la compuerta
    assert ".md" not in CODE_EXTS


# ── _find_build_dir ──────────────────────────────────────────────────────────


def test_find_build_dir_monorepo_subdir(tmp_path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text("{}")
    (frontend / "src").mkdir()
    (frontend / "src" / "App.tsx").write_text("x")

    result = _find_build_dir(str(tmp_path), ["frontend/src/App.tsx"])
    assert result == str(frontend)


def test_find_build_dir_root_package_json(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "src").mkdir()

    result = _find_build_dir(str(tmp_path), ["src/index.ts"])
    assert result == str(tmp_path)


def test_find_build_dir_none_without_package_json(tmp_path):
    (tmp_path / "main.py").write_text("x")
    assert _find_build_dir(str(tmp_path), ["main.py"]) is None


# ── syntax_gate (fail-open) ──────────────────────────────────────────────────


def test_syntax_gate_fail_open_without_git(tmp_path):
    (tmp_path / "a.tsx").write_text("x")
    ok, err = syntax_gate(str(tmp_path))
    assert ok is True
    assert err == ""


def test_syntax_gate_fail_open_when_clean(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "a.md").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    ok, err = syntax_gate(str(repo))
    assert ok is True


# ── Corrección 2: scripts .sh rotos son detectados por la compuerta ──────────


def test_syntax_gate_detects_broken_sh(tmp_path):
    """Un script bash TRUNCADO (write_file devolvió éxito pero bash -n falla)
    debe ser detectado por syntax_gate — el caso que se escapó en E2E."""
    repo = _init_repo(tmp_path)
    broken = 'TOKENS_OK=$(echo "$NR_RESPONSE" | python3 -c "\n'  # comilla abierta
    (repo / "e2e_verify.sh").write_text(broken)
    # sin commitear → lo detecta changed_code_files como untracked
    ok, err = syntax_gate(str(repo))
    assert ok is False
    assert "e2e_verify.sh" in err


def test_syntax_gate_passes_valid_sh(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "ok.sh").write_text('#!/usr/bin/env bash\necho "ok"\n')
    ok, err = syntax_gate(str(repo))
    assert ok is True
