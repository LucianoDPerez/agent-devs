"""Tests de soporte AGENTS.md (convenciones del repo)."""

from orchestration.framework_rules import AGENTS_MD_MAX_CHARS, load_agents_md


def test_sin_archivo_devuelve_vacio(tmp_path):
    assert load_agents_md(str(tmp_path)) == ""


def test_repo_none_devuelve_vacio():
    assert load_agents_md(None) == ""


def test_inyecta_contenido(tmp_path):
    (tmp_path / "AGENTS.md").write_text(
        "# Convenciones\n- Type hints siempre.\n", encoding="utf-8"
    )
    out = load_agents_md(str(tmp_path))
    assert "AGENTS.md" in out
    assert "Type hints siempre" in out


def test_trunca_archivo_grande(tmp_path):
    (tmp_path / "AGENTS.md").write_text("x" * (AGENTS_MD_MAX_CHARS + 100), encoding="utf-8")
    out = load_agents_md(str(tmp_path))
    assert "truncado" in out
    assert len(out) < AGENTS_MD_MAX_CHARS + 500


def test_vacio_devuelve_vacio(tmp_path):
    (tmp_path / "AGENTS.md").write_text("   \n", encoding="utf-8")
    assert load_agents_md(str(tmp_path)) == ""
