"""Idioma del harness: detección, /language y directiva al modelo."""

import os

from core.lang import (
    SUPPORTED,
    detect_language,
    language_directive,
    normalize_lang,
    resolve_language,
    t,
)
from display.commands import format_help, interpret_slash


def test_supported_solo_es_en():
    assert set(SUPPORTED) == {"en", "es"}


def test_normalize():
    assert normalize_lang("es_AR.UTF-8") == "es"
    assert normalize_lang("en_US.UTF-8") == "en"
    assert normalize_lang("EN") == "en"
    assert normalize_lang("fr_FR.UTF-8") is None
    assert normalize_lang("") is None
    assert normalize_lang(None) is None


def test_detect_default_en(monkeypatch):
    for var in ("AGENT_DEVS_LANGUAGE", "LANGUAGE", "LC_ALL", "LANG"):
        monkeypatch.delenv(var, raising=False)
    assert detect_language() == "en"


def test_detect_es_por_lang(monkeypatch):
    for var in ("AGENT_DEVS_LANGUAGE", "LANGUAGE", "LC_ALL", "LANG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LANG", "es_AR.UTF-8")
    assert detect_language() == "es"


def test_resolve_override_manda(monkeypatch):
    for var in ("AGENT_DEVS_LANGUAGE", "LANGUAGE", "LC_ALL", "LANG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LANG", "es_AR.UTF-8")
    assert resolve_language("en") == "en"


def test_t_bilingue():
    assert "sesión" in t("cmd_new", "es")
    assert "session" in t("cmd_new", "en").lower()
    assert t("cmd_language", "en").startswith("UI")
    # fallback: clave desconocida se devuelve tal cual
    assert t("no-existe", "es") == "no-existe"


def test_directive_por_idioma():
    assert "English" in language_directive("en")
    assert "español" in language_directive("es").lower()
    assert "HIGHEST PRIORITY" in language_directive("en")


def test_suffix_solo_no_es():
    from core.lang import language_suffix

    assert language_suffix("es") == ""
    assert "English" in language_suffix("en")


def test_help_bilingue():
    es = format_help(lang="es")
    en = format_help(lang="en")
    assert "/language" in es and "/language" in en
    assert "sesión" in es
    assert "session" in en.lower()


def test_language_es_comando():
    assert interpret_slash("/language") == ("run", ("/language", ""))
    assert interpret_slash("/language en") == ("run", ("/language", "en"))


def test_slash_language_cambia_y_persiste(tmp_path, monkeypatch):
    import llm_config

    fake = tmp_path / "config.json"
    monkeypatch.setattr(llm_config, "CONFIG_FILE", fake)
    monkeypatch.setattr(llm_config, "LEGACY_CONFIG_FILE", tmp_path / "legacy.json")
    from orchestration.session import Session

    s = Session.__new__(Session)
    s._lang = "es"
    s.agent = object()
    out = Session.slash_language(s, "en")
    assert "en" in out
    assert s._lang == "en"
    assert s.agent is None  # el próximo turno se reconstruye
    import json

    assert json.loads(fake.read_text(encoding="utf-8"))["language"] == "en"
    out2 = Session.slash_language(s, "xx")
    assert "⛔" in out2
    assert s._lang == "en"


def test_no_use_before_bind_t():
    """Regresión: UnboundLocalError en _run_turn_inner (v0.5.0).

    Un `from core.lang import t as _t` tardío dentro de una función que ya
    usa el `_t` del módulo vuelve TODOS los usos locales y revienta con
    UnboundLocalError si el uso ejecuta antes que el import.
    """
    import ast
    from pathlib import Path

    for rel in ("orchestration/session.py", "main.py", "display/console.py",
                "display/tui.py", "display/fullscreen_tui.py",
                "orchestration/agent_builder.py"):
        tree = ast.parse((Path(__file__).resolve().parent.parent / rel).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            binds = sorted({
                n.lineno for n in ast.walk(node)
                if isinstance(n, (ast.Import, ast.ImportFrom))
                and any((a.asname or "") == "_t" for a in n.names)
            })
            if not binds:
                continue
            bad = [
                n.lineno for n in ast.walk(node)
                if isinstance(n, ast.Name) and n.id == "_t"
                and isinstance(n.ctx, ast.Load) and n.lineno < min(binds)
            ]
            assert not bad, f"{rel}:{node.name} usa _t antes de bindearlo: {bad[:3]}"


def test_panel_en_usa_ingles():
    from display.console import print_role_switch

    assert "local" in __import__("core.lang", fromlist=["t"]).t("role_tools", "en")
    assert "locales" in __import__("core.lang", fromlist=["t"]).t("role_tools", "es")


def test_context_exceeded_bilingue():
    from orchestration.session import _is_context_exceeded

    e = Exception(
        "request (27155 tokens) exceeds the available context size "
        "(26624 tokens), try increasing it"
    )
    ok, req, avail = _is_context_exceeded(e)
    assert ok and req == 27155 and avail == 26624
    from core.lang import t

    assert "Ran out" in t("ctx_title", "en") or "context" in t("ctx_title", "en").lower()
    assert "contexto" in t("ctx_title", "es").lower()
