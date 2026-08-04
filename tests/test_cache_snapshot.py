"""Tests para la capa de persistencia (cache.py).

Verifica que el snapshot_hash use archivos git-tracked cuando el repo tiene
git: una carpeta untracked nueva (ej: lucho-plans/) NO debe invalidar el
caché, mientras que editar un archivo tracked SÍ debe cambiarlo.
"""

import os
import subprocess
import tempfile
from pathlib import Path

import cache as cache_mod


def _init_git_repo() -> str:
    tmp = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=tmp, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "main"], cwd=tmp, check=True)
    (Path(tmp) / "README.md").write_text("# Test\n", encoding="utf-8")
    (Path(tmp) / "src").mkdir()
    (Path(tmp) / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=tmp, check=True)
    return tmp


def _no_git_repo() -> str:
    tmp = tempfile.mkdtemp()
    (Path(tmp) / "README.md").write_text("# No git\n", encoding="utf-8")
    return tmp


class TestSnapshotGitTracked:
    def test_untracked_folder_does_not_invalidate(self):
        repo = _init_git_repo()
        h1 = cache_mod.snapshot_hash(repo)

        (Path(repo) / "lucho-plans").mkdir()
        (Path(repo) / "lucho-plans" / "plan.md").write_text("plan nuevo\n", encoding="utf-8")
        # la carpeta nueva está untracked -> el hash NO debe cambiar
        h2 = cache_mod.snapshot_hash(repo)
        assert h1 == h2, "carpeta untracked no debería invalidar el caché"

    def test_tracked_file_change_invalidates(self):
        repo = _init_git_repo()
        h1 = cache_mod.snapshot_hash(repo)

        target = Path(repo) / "src" / "app.py"
        target.write_text("print('changed')\n", encoding="utf-8")
        h2 = cache_mod.snapshot_hash(repo)
        assert h1 != h2, "cambiar un archivo tracked sí debe invalidar"


class TestSnapshotNoGit:
    def test_new_folder_invalidates_without_git(self):
        repo = _no_git_repo()
        h1 = cache_mod.snapshot_hash(repo)

        (Path(repo) / "plans").mkdir()
        (Path(repo) / "plans" / "p.md").write_text("x\n", encoding="utf-8")
        h2 = cache_mod.snapshot_hash(repo)
        assert h1 != h2, "sin git, cualquier cambio de árbol debe invalidar"
