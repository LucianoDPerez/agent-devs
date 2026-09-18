"""Tests de /commit con alcance (lista + todo/sesion).

El usuario decide con la lista a la vista: pendientes agrupados con marca
[sesión] vs [previo]; untracked jamás se stagea automático.
"""

import subprocess
from pathlib import Path

from orchestration.session import (
    _commit_stage_set,
    _parse_commit_candidates,
    _porcelain_paths,
)


def _init_repo(path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(path), check=True)
    (path / "README.md").write_text("# repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(path), check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(path), check=True)
    return path


def _git_log_count(repo: Path) -> int:
    out = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"], cwd=str(repo),
        capture_output=True, text=True, check=True,
    )
    return int(out.stdout.strip())


def test_porcelain_paths_renombres_y_cortos():
    assert _porcelain_paths(["R  viejo -> nuevo", "M  a", "?? b", "x"]) == {
        "nuevo", "a", "b",
    }


def test_parse_agrupa_y_marca():
    lines = ["M  a", " M b", "?? c", "A  d"]
    out = _parse_commit_candidates(lines, {"b", "c"})
    assert "[previo] a" in out
    assert "[sesión] b" in out
    assert "[sesión] c" in out
    assert "Staged (2)" in out
    assert "Modificados tracked (1)" in out
    assert "Nuevos untracked (1)" in out


def test_parse_arbol_limpio():
    assert "limpio" in _parse_commit_candidates([], set())


def test_stage_set_todo_vs_sesion():
    lines = [" M a", "M  b", "?? c", " D d"]
    assert _commit_stage_set(lines, "todo", set()) == ["a", "b", "d"]
    assert _commit_stage_set(lines, "sesion", {"b", "c"}) == ["b"]
    assert _commit_stage_set(lines, "sesion", set()) == []


def test_accumulate_suma_diferencia(tmp_path):
    """Pre-existente queda fuera; lo ensuciado en el turno entra."""
    from orchestration.session import Session

    repo = _init_repo(tmp_path)
    (repo / "C").write_text("previo", encoding="utf-8")
    s = Session(llm=None, repo_path=str(repo))
    s._turn_start_raw = {"C"}
    (repo / "D").write_text("nuevo", encoding="utf-8")
    s._accumulate_session_files()
    assert s._session_touched_files == {"D"}


def test_slash_commit_lista_no_commitea(tmp_path, capsys):
    """Bare /commit: lista con marcas, NO commitea."""
    from orchestration.session import Session

    repo = _init_repo(tmp_path)
    (repo / "A").write_text("x", encoding="utf-8")
    s = Session(llm=None, repo_path=str(repo))
    s._session_touched_files = {"A"}
    s.slash_commit("")
    out = capsys.readouterr().out
    assert "[sesión] A" in out
    assert "/commit sesion" in out
    assert _git_log_count(repo) == 1


def test_slash_commit_sesion_solo_sesion(tmp_path, capsys):
    """sesion stagea/commitea solo tracked tocado; lo previo sigue sucio."""
    from orchestration.session import Session

    repo = _init_repo(tmp_path)
    (repo / "A").write_text("previo", encoding="utf-8")
    (repo / "B").write_text("base", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=str(repo), check=True)
    (repo / "A").write_text("previo editado", encoding="utf-8")
    s = Session(llm=None, repo_path=str(repo))
    s._turn_start_raw = {"A"}
    (repo / "B").write_text("nuevo", encoding="utf-8")
    s._accumulate_session_files()
    assert s._session_touched_files == {"B"}
    s.slash_commit("sesion trabajo x")
    out = capsys.readouterr().out
    assert "Commit creado" in out
    log = subprocess.run(
        ["git", "log", "-1", "--format=%s"], cwd=str(repo),
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert log == "trabajo x"
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(repo),
        capture_output=True, text=True, check=True,
    ).stdout
    assert "A" in status and "B" not in status


def test_slash_commit_untracked_solo_avisa(tmp_path, capsys):
    """Solo-untracked: no commitea nada solo, indica agregar a mano."""
    from orchestration.session import Session

    repo = _init_repo(tmp_path)
    (repo / "N").write_text("nuevo", encoding="utf-8")
    s = Session(llm=None, repo_path=str(repo))
    s._turn_start_raw = set()
    s._accumulate_session_files()
    s.slash_commit("sesion")
    out = capsys.readouterr().out
    assert "Nada para commitear" in out
    assert "agregar a mano" in out
    assert "git add -- N" in out
    assert _git_log_count(repo) == 1


def test_slash_commit_sin_nada_stageable(tmp_path, capsys):
    """Árbol limpio: mensaje limpio SIN correr la batería, sin error crudo."""
    from orchestration.session import Session

    repo = _init_repo(tmp_path)
    s = Session(llm=None, repo_path=str(repo))
    s.slash_commit("todo")
    out = capsys.readouterr().out
    assert "Nada para commitear" in out
    assert "Verificando antes de commitear" not in out
    assert "falló" not in out
    assert _git_log_count(repo) == 1
