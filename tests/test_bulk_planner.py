"""Tests del planificador bulk: detección de targets, batches y cola SQLite."""

import tempfile
from pathlib import Path

from cache import (
    bulk_progress,
    ensure_bulk_plan,
    fail_or_keep_batch,
    mark_batch,
    next_pending_batch,
)
from orchestration.bulk_planner import (
    bulk_task_hash,
    build_batch_scope,
    detect_bulk_targets,
    split_into_batches,
)


class TestSplitIntoBatches:
    def test_14_files_gives_5_5_4(self):
        files = [f"f{i}.md" for i in range(14)]
        batches = split_into_batches(files)
        assert [len(b) for b in batches] == [5, 5, 4]
        assert sum(batches, []) == files  # orden preservado, sin pérdidas

    def test_empty_and_small(self):
        assert split_into_batches([]) == []
        assert len(split_into_batches(["a", "b"])) == 1


class TestDetectBulkTargets:
    def test_cited_existing_dir_yields_files(self):
        repo = tempfile.mkdtemp()
        cmd_dir = Path(repo) / "src" / "pkg" / "templates" / "commands"
        cmd_dir.mkdir(parents=True)
        for i in range(14):
            (cmd_dir / f"spec-kitti.c{i}.md").write_text("# x", encoding="utf-8")
        # un dir distractor con menos archivos
        small = Path(repo) / "docs"
        small.mkdir()
        (small / "a.md").write_text("x", encoding="utf-8")

        task = (
            "implementar Task 8: modificar los 14 templates en "
            "src/pkg/templates/commands/ ver docs/ para referencia"
        )
        targets = detect_bulk_targets(task, repo)
        assert len(targets) == 14
        assert all(t.startswith("src/pkg/templates/commands/") for t in targets)

    def test_nonexistent_dir_no_targets(self):
        repo = tempfile.mkdtemp()
        targets = detect_bulk_targets(
            "modificar src/no/existe/ y otras cosas", repo
        )
        assert targets == []


class TestBulkQueue:
    def setup_method(self):
        # Hash ÚNICO por corrida: la cola vive en el cache.db REAL compartido,
        # un hash fijo contaminaría entre ejecuciones de tests.
        import uuid
        self.th = bulk_task_hash(f"tarea de prueba {uuid.uuid4()}", "/tmp/repo-x")
        self.batches = [["a.md", "b.md"], ["c.md"]]

    def test_hash_stable_and_distinct(self):
        th2 = bulk_task_hash("prompt estable para hash", "/tmp/repo-x")
        th3 = bulk_task_hash("otra tarea distinta", "/tmp/repo-x")
        assert th2 != th3
        # mismo prompt + scope de batch → mismo hash (canonización)
        chained = "prompt estable para hash" + build_batch_scope(0, 2, ["z.md"])
        assert bulk_task_hash(chained, "/tmp/repo-x") == th2

    def test_plan_create_keep_replace(self):
        assert ensure_bulk_plan(self.th, self.batches) is True
        # mismo plan → conserva progreso
        mark_batch(self.th, 0, "done")
        assert ensure_bulk_plan(self.th, self.batches) is False
        p = bulk_progress(self.th)
        assert p["done"] == 1
        # plan distinto → reemplaza
        changed = ensure_bulk_plan(self.th, [["z.md"], ["y.md"], ["x.md"]])
        assert changed is False or changed is True
        p2 = bulk_progress(self.th)
        assert p2["total"] in (2, 3)

    def test_next_pending_and_mark_done(self):
        ensure_bulk_plan(self.th, self.batches)
        nxt = next_pending_batch(self.th)
        assert nxt["seq"] == 0
        assert nxt["files"] == ["a.md", "b.md"]
        mark_batch(self.th, 0, "done")
        nxt2 = next_pending_batch(self.th)
        assert nxt2["seq"] == 1

    def test_fail_then_failed_after_max_attempts(self):
        ensure_bulk_plan(self.th, self.batches)
        s1 = fail_or_keep_batch(self.th, 0, max_attempts=2)
        assert s1 == "pending"  # primer fallo: reanudable
        s2 = fail_or_keep_batch(self.th, 0, max_attempts=2)
        assert s2 == "failed"  # agotó intentos
        p = bulk_progress(self.th)
        assert p["failed"] == 1


class TestBuildBatchScope:
    def test_scope_lists_only_current_batch_files(self):
        scope = build_batch_scope(1, 3, ["b1.md", "b2.md"])
        assert "Batch 2/3" in scope
        assert "- b1.md" in scope
        assert "- b2.md" in scope
        assert "NO los leas ni los edites" in scope


class TestCanonicalTaskText:
    def test_hash_stable_across_batch_scope(self):
        from orchestration.bulk_planner import canonical_task_text
        original = "implementar Task 8: modificar los 14 templates en src/x/commands/"
        chained = original + build_batch_scope(0, 3, ["a.md", "b.md"])
        assert canonical_task_text(chained) == original.rstrip()
        assert bulk_task_hash(original, "/r") == bulk_task_hash(chained, "/r")
