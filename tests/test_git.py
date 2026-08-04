"""Tests para git tools."""

import subprocess
import tempfile
from pathlib import Path

from tools.git import (
    changed_files,
    create_commit,
    current_branch,
    git_log,
    git_status,
    stage_files,
)


def _init_repo() -> str:
    """Crea un repo git temporal con un commit inicial."""
    tmp = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=tmp, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "main"], cwd=tmp, check=True)
    (Path(tmp) / "README.md").write_text("# Test", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=tmp, check=True)
    return tmp


class TestCurrentBranch:
    def test_returns_branch_name(self):
        repo = _init_repo()
        result = current_branch.invoke({"path": repo})
        assert result == "main"


class TestChangedFiles:
    def test_clean_repo(self):
        repo = _init_repo()
        result = changed_files.invoke({"path": repo})
        assert "clean" in result.lower() or "no changed" in result.lower()

    def test_with_changes(self):
        repo = _init_repo()
        (Path(repo) / "new_file.txt").write_text("new", encoding="utf-8")
        result = changed_files.invoke({"path": repo})
        assert "new_file.txt" in result


class TestGitStatus:
    def test_shows_branch(self):
        repo = _init_repo()
        result = git_status.invoke({"path": repo})
        assert "main" in result


class TestGitLog:
    def test_shows_commits(self):
        repo = _init_repo()
        result = git_log.invoke({"path": repo, "limit": 5})
        assert "initial" in result


class TestStageFiles:
    def test_stage_single_file(self):
        repo = _init_repo()
        (Path(repo) / "test.txt").write_text("test", encoding="utf-8")
        result = stage_files.invoke({"path": repo, "files": "test.txt"})
        assert "staged" in result.lower() or "✅" in result


class TestCreateCommit:
    def test_commit_staged(self):
        repo = _init_repo()
        (Path(repo) / "test.txt").write_text("test", encoding="utf-8")
        stage_files.invoke({"path": repo, "files": "test.txt"})
        result = create_commit.invoke({"path": repo, "message": "test: add test file"})
        assert "Commit created" in result or "✅" in result
