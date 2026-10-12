"""Verify con alcance al repo: paths fuera del turno se rechazan sin ejecutar.

E2E venture (35B): run_verify + read sobre venture-api-ulab cuando la tarea
era de venture-ueno-ads → rojos de otro checkout que fallaron el turno con
el entregable correcto. Las lecturas (read/list/search/copy) SÍ pueden
cruzar repos (pools contra otro checkout): no se tocan.
"""
from langchain_core.tools import tool as _tool

from orchestration.tool_dedupe import (
    ExploreBudget,
    ToolCallDedupe,
    _is_within_repo,
    wrap_tools_with_dedupe,
)


@_tool
def run_verify(path: str) -> str:
    """fake verify (no ejecuta builds reales)"""
    return "ran: " + path


@_tool
def read_file(path: str) -> str:
    """fake read"""
    return "read: " + path


def _wrapped(tools, repo):
    return wrap_tools_with_dedupe(
        tools, ToolCallDedupe(), ExploreBudget(), {},
        repo_path=(str(repo) if repo is not None else None))


class TestWithinRepo:
    def test_adentro(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        assert _is_within_repo(str(sub / "a.ts"), str(tmp_path)) is True

    def test_igual(self, tmp_path):
        assert _is_within_repo(str(tmp_path), str(tmp_path)) is True

    def test_afuera(self, tmp_path):
        otro = tmp_path.parent / "otro-repo"
        otro.mkdir(exist_ok=True)
        assert _is_within_repo(str(otro), str(tmp_path)) is False

    def test_fail_open(self):
        assert _is_within_repo("", None) is True
        assert _is_within_repo("/x", None) is True


class TestVerifyScopeGuard:
    def test_verify_fuera_se_rechaza(self, tmp_path):
        repo = tmp_path / "repo-a"
        repo.mkdir()
        fuera = tmp_path / "repo-b"
        fuera.mkdir()
        (w,) = _wrapped([run_verify], repo)
        out = w.invoke({"path": str(fuera)})
        assert "FUERA" in out
        assert "ran:" not in out

    def test_verify_adentro_ejecuta(self, tmp_path):
        repo = tmp_path / "repo-a"
        repo.mkdir()
        (w,) = _wrapped([run_verify], repo)
        out = w.invoke({"path": str(repo / "sub")})
        assert out.startswith("ran:")

    def test_sin_repo_no_bloquea(self, tmp_path):
        (w,) = _wrapped([run_verify], None)
        out = w.invoke({"path": str(tmp_path)})
        assert out.startswith("ran:")

    def test_lecturas_cruzan_repos(self, tmp_path):
        """read_file sobre otro checkout sigue permitido (pools multi-repo)."""
        repo = tmp_path / "repo-a"
        repo.mkdir()
        fuera = tmp_path / "repo-b"
        fuera.mkdir()
        (w,) = _wrapped([read_file], repo)
        out = w.invoke({"path": str(fuera / "tasks.json")})
        assert out.startswith("read:")


class TestSearchCodeEsGrep:
    def test_descripcion_mapea_grep(self):
        from tools.search import search_code

        desc = search_code.description
        assert "grep" in desc.lower()
        assert "search_code(path=" in desc


class TestTaskRepoManda:
    """Pool contra otro checkout: la verificación con alcance usa el repo de
    la tarea pinnada, no el de la sesión (E2E venture: sesión en ulab,
    tarea en ueno-ads)."""

    def test_task_repo_permite_y_sesion_no(self, tmp_path):
        from orchestration.tool_dedupe import ToolCallDedupe

        sesion = tmp_path / "api-ulab"
        sesion.mkdir()
        tarea = tmp_path / "ueno-ads"
        tarea.mkdir()
        dd = ToolCallDedupe()
        dd.task_repo = str(tarea)
        from orchestration.tool_dedupe import ExploreBudget, wrap_tools_with_dedupe

        (w,) = wrap_tools_with_dedupe(
            [run_verify], dd, ExploreBudget(), {}, repo_path=str(sesion))
        assert w.invoke({"path": str(tarea)}).startswith("ran:")
        out = w.invoke({"path": str(sesion)})
        assert "FUERA" in out
        assert str(tarea) in out

    def test_sin_pin_manda_sesion(self, tmp_path):
        from orchestration.tool_dedupe import ToolCallDedupe

        repo = tmp_path / "repo"
        repo.mkdir()
        dd = ToolCallDedupe()
        assert dd.task_repo is None
        from orchestration.tool_dedupe import ExploreBudget, wrap_tools_with_dedupe

        (w,) = wrap_tools_with_dedupe(
            [run_verify], dd, ExploreBudget(), {}, repo_path=str(repo))
        assert w.invoke({"path": str(repo)}).startswith("ran:")


class TestPinnedTaskRepo:
    def test_campo_repo(self, tmp_path):
        import json

        from orchestration.execute_bootstrap import pinned_task_repo

        otro = tmp_path / "producto"
        otro.mkdir()
        p = tmp_path / "tasks.json"
        p.write_text(json.dumps({
            "repo": str(otro),
            "tasks": [{"id": "T002", "ac": "x"}],
        }), encoding="utf-8")
        assert pinned_task_repo(f"implementar T002 desde {p}",
                                repo_path=str(tmp_path)) == str(otro)

    def test_git_root_walk(self, tmp_path):
        import subprocess

        from orchestration.execute_bootstrap import pinned_task_repo

        sub = tmp_path / "proj" / "deep"
        sub.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=str(tmp_path / "proj"),
                       check=True)
        p = sub / "tasks.json"
        p.write_text('{"tasks": [{"id": "T002", "ac": "x"}]}', encoding="utf-8")
        assert pinned_task_repo(f"implementar T002 desde {p}",
                                repo_path=str(tmp_path)) == str(tmp_path / "proj")

    def test_sin_pin_none(self, tmp_path):
        from orchestration.execute_bootstrap import pinned_task_repo

        assert pinned_task_repo("implementar algo",
                                repo_path=str(tmp_path)) is None


class TestScopePadresYFlips:
    def test_padre_de_citado_in_scope(self):
        from orchestration.tool_dedupe import _in_scope

        scope = frozenset({"assets/css/tokens.css"})
        assert _in_scope("/repo/assets/css", scope) is True
        assert _in_scope("assets/css", scope) is True
        assert _in_scope("/repo/assets/css/tokens.css", scope) is True
        assert _in_scope("/repo/otra/cosa.css", scope) is False

    def test_flip_planeado_no_es_creep(self, tmp_path):
        """El tasks.json citado (TASK_PATH_ALLOW) no cuenta como violación
        aunque no esté en scope_files: el flip es el flujo indicado."""
        from orchestration.tool_dedupe import ToolCallDedupe
        from tools.filesystem import TASK_PATH_ALLOW

        @_tool
        def write_file(path: str, content: str) -> str:
            """fake write"""
            return "written: " + path

        tasks = tmp_path / "tasks.json"
        tasks.write_text("{}")
        scope = frozenset({"assets/css/tokens.css"})
        dd = ToolCallDedupe()
        dd.scope_files = scope
        TASK_PATH_ALLOW.add(str(tasks))
        try:
            from orchestration.tool_dedupe import ExploreBudget, wrap_tools_with_dedupe

            (w,) = wrap_tools_with_dedupe(
                [write_file], dd, ExploreBudget(), {}, repo_path=str(tmp_path))
            out = w.invoke({"path": str(tasks), "content": "{}"})
            assert out.startswith("written:")
            assert dd.scope_violations == {}
        finally:
            TASK_PATH_ALLOW.discard(str(tasks))


class TestPinPreservaDiseno:
    def test_design_y_stack_sobreviven(self):
        import json

        from orchestration.execute_bootstrap import filter_json_task_sections

        raw = json.dumps({
            "design": {"colors": {"bg": "#0B0E14"}},
            "stack": {"tech": "HTML5"},
            "tasks": [
                {"id": "T002", "ac": "verificar"},
                {"id": "T003", "ac": "tokens"},
            ],
        })
        out = json.loads(filter_json_task_sections(raw, [3]))
        assert [e["id"] for e in out["tasks"]] == ["T003"]
        assert out["design"]["colors"]["bg"] == "#0B0E14"
        assert out["stack"]["tech"] == "HTML5"


class TestRunTestFileEsVitest:
    def test_descripcion_mapea_vitest(self):
        from tools.verify import run_test_file

        assert "vitest" in run_test_file.description
