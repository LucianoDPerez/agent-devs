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
