"""Tests para la capa de persistencia (cache.py).

Verifica que el snapshot_hash use archivos git-tracked cuando el repo tiene
git: una carpeta untracked nueva (ej: lucho-plans/) NO debe invalidar el
caché, mientras que editar un archivo tracked SÍ debe cambiarlo.
"""

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


class TestSnapshotDiff:
    def test_identical_is_zero(self):
        repo = _init_git_repo()
        entries = cache_mod.snapshot_entries(repo)
        assert cache_mod.snapshot_diff_files("\n".join(entries), entries) == 0

    def test_one_modified_file_counts_one(self):
        repo = _init_git_repo()
        old = "\n".join(cache_mod.snapshot_entries(repo))
        (Path(repo) / "src" / "app.py").write_text("print('changed')\n", encoding="utf-8")
        new = cache_mod.snapshot_entries(repo)
        assert cache_mod.snapshot_diff_files(old, new) == 1

    def test_two_tracked_changes_count_two(self):
        # el snapshot solo mira tracked: untracked no cuenta (diseño),
        # dos cambios tracked (edit + delete) cuentan 2
        repo = _init_git_repo()
        old = "\n".join(cache_mod.snapshot_entries(repo))
        (Path(repo) / "src" / "new.py").write_text("x = 1\n", encoding="utf-8")
        (Path(repo) / "src" / "app.py").write_text("print('v2')\n", encoding="utf-8")
        (Path(repo) / "README.md").unlink()
        new = cache_mod.snapshot_entries(repo)
        assert cache_mod.snapshot_diff_files(old, new) == 2

    def test_no_base_returns_none(self):
        repo = _init_git_repo()
        assert cache_mod.snapshot_diff_files(None, cache_mod.snapshot_entries(repo)) is None
        assert cache_mod.snapshot_diff_files("", cache_mod.snapshot_entries(repo)) is None

    def test_hash_matches_entries(self):
        import hashlib

        repo = _init_git_repo()
        entries = cache_mod.snapshot_entries(repo)
        expected = hashlib.sha256("\n".join(entries).encode()).hexdigest()[:16]
        assert cache_mod.snapshot_hash(repo) == expected


class TestReuseRoundtrip:
    def test_save_and_reuse_small_diff(self, tmp_path, monkeypatch):
        import tempfile

        db = str(Path(tempfile.mkdtemp()) / "test.db")
        monkeypatch.setattr(cache_mod, "CACHE_DB", db)
        repo = _init_git_repo()
        entries = cache_mod.snapshot_entries(repo)
        cache_mod.save_analysis(
            repo, snapshot=cache_mod.snapshot_hash(repo), language="python",
            tech_stack="python", analysis="Un resumen válido y largo para el test de reuso.",
            snapshot_files="\n".join(entries),
        )
        # toco 2 archivos tracked -> diff chico -> reuso permitido (<=10)
        (Path(repo) / "src" / "app.py").write_text("print('v2')\n", encoding="utf-8")
        (Path(repo) / "README.md").write_text("# Test v2\n", encoding="utf-8")
        cached = cache_mod.load_analysis(repo)
        assert cached["snapshot_files"] is not None
        changed = cache_mod.snapshot_diff_files(
            cached["snapshot_files"], cache_mod.snapshot_entries(repo)
        )
        assert changed == 2


class TestDeterministicSummary:
    def test_summary_has_language_and_modules(self):
        from analyzer import deterministic_summary

        repo = _init_git_repo()
        out = deterministic_summary(repo, "python", "python")
        assert "python" in out
        assert "src" in out
        assert len(out) >= 40

    def test_summary_without_dirs(self, tmp_path):
        from analyzer import deterministic_summary

        out = deterministic_summary(str(tmp_path), "go", "go")
        assert "go" in out
        assert len(out) >= 20
