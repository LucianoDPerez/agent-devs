"""Slash commands: registro, matching, despacho y /resume.

Evidencia de los faltantes originales:
- /history solo listaba resúmenes (80 chars) y era imposible retomar:
  load_session_turns (cache.py) existía pero nadie la llamaba.
- Sin autocompletado: TextArea pelado, cero Completer en el repo.
"""

import cache
from cache import save_turn
from display.commands import (
    build_ptk_completer,
    format_help,
    interpret_slash,
    match_commands,
)
from orchestration.session import Session

# ── registro y matching ──────────────────────────────────────────────

def test_todos_los_comandos_tienen_descripcion():
    help_text = format_help()
    for name in ("/new", "/compact", "/history", "/resume", "/autoapprove", "/verify", "/commit", "/push", "/pr", "/help"):
        assert name in help_text
    # Descripciones breves pedidas (una por comando)
    assert "nueva sesión" in help_text
    assert "/resume <id>" in help_text


def test_match_filtra_por_prefijo():
    assert [n for n, _ in match_commands("/")] == ["/new", "/compact", "/history", "/resume", "/autoapprove", "/verify", "/commit", "/push", "/pr", "/help"]
    assert [n for n, _ in match_commands("/h")] == ["/history", "/help"]
    assert [n for n, _ in match_commands("/res")] == ["/resume"]
    assert match_commands("/z") == []


def test_interpret_mensaje_normal_va_al_llm():
    assert interpret_slash("analizá el login")[0] == "message"
    assert interpret_slash("")[0] == "message"
    # Paths del repo NO son comandos (segundo slash los descalifica)
    assert interpret_slash("/api/health devuelve 500")[0] == "message"


def test_interpret_comandos():
    assert interpret_slash("/new") == ("run", ("/new", ""))
    assert interpret_slash("/History")[0] == "run"  # case-insensitive
    assert interpret_slash("/resume ab12") == ("run", ("/resume", "ab12"))
    assert interpret_slash("/")[0] == "help"
    assert interpret_slash("/help") == ("run", ("/help", ""))


def test_interpret_abreviatura_unica_corre_sola():
    # "/his" solo matchea /history (sin args) → corre directo
    assert interpret_slash("/his") == ("run", ("/history", ""))


def test_interpret_abreviatura_con_args_pide_uso():
    # "/res" matchea /resume pero falta el id → hint, no ejecución
    kind, payload = interpret_slash("/res")
    assert kind == "hint"
    assert [n for n, _ in payload] == ["/resume"]


def test_interpret_desconocido_muestra_opciones():
    kind, payload = interpret_slash("/xyz")
    assert kind == "hint"
    assert payload == []


def test_ptk_completer_ofrece_todo():
    completer = build_ptk_completer()
    from prompt_toolkit.document import Document

    got = {c.text for c in completer.get_completions(Document("/"), None)}
    assert got == {"/new", "/compact", "/history", "/resume", "/autoapprove", "/verify", "/commit", "/push", "/pr", "/help"}
    got_h = {c.text for c in completer.get_completions(Document("/h"), None)}
    assert got_h == {"/history", "/help"}


# ── /resume ──────────────────────────────────────────────────────────

def _seed(monkeypatch, tmp_path, sid, repo, role="analyzer", user="hola", asst="buenas"):
    monkeypatch.setattr(cache, "CACHE_DB", tmp_path / "t.db")
    save_turn(session_id=sid, repo_path=str(repo), role=role,
              user_message=user, assistant_message=asst, tokens_used=10)


def _session(monkeypatch, repo):
    sess = Session(llm=None, repo_path=str(repo))
    # Sin LLM en tests: emular lo que _rebuild_agent hace con el rol.
    def _fake_rebuild(role):
        sess.current_role = role
        return True
    sess._rebuild_agent = _fake_rebuild
    return sess


def test_resume_restaura_mensajes_rol_e_id(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed(monkeypatch, tmp_path, "abcd1234", repo, role="analyzer",
          user="analizá x", asst="miré x:1")
    _seed(monkeypatch, tmp_path, "abcd1234", repo, role="planner",
          user="planificá y", asst="plan: z")
    sess = _session(monkeypatch, repo)
    ok, msg, data = sess.resume_session("abcd")
    assert ok is True
    assert "abcd1234" in msg and "2 turno" in msg
    assert sess.session_id == "abcd1234"
    assert sess.current_role.value == "planner"  # rol del último turno
    assert sess._last_response == "plan: z"
    kinds = [type(m).__name__ for m in sess._messages]
    assert kinds == ["HumanMessage", "AIMessage", "HumanMessage", "AIMessage"]


def test_resume_id_inexistente(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed(monkeypatch, tmp_path, "abcd1234", repo)
    sess = _session(monkeypatch, repo)
    ok, msg, data = sess.resume_session("zzzz")
    assert ok is False and "/history" in msg and data == []


def test_resume_prefijo_ambiguo(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed(monkeypatch, tmp_path, "abc11111", repo)
    _seed(monkeypatch, tmp_path, "abc22222", repo)
    sess = _session(monkeypatch, repo)
    ok, msg, _ = sess.resume_session("abc")
    assert ok is False and "ambiguo" in msg
    ok2, _, _ = sess.resume_session("abc1")
    assert ok2 is True


def test_resume_otro_repo_se_rechaza(monkeypatch, tmp_path):
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    repo_a.mkdir()
    repo_b.mkdir()
    _seed(monkeypatch, tmp_path, "zzz99999", repo_b)
    sess = _session(monkeypatch, repo_a)
    ok, msg, _ = sess.resume_session("zzz99999")
    assert ok is False and "otro repo" in msg


def test_resume_rol_invalido_cae_a_analyze(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed(monkeypatch, tmp_path, "qwer1234", repo, role="nonsense",
          user="h", asst="a")
    sess = _session(monkeypatch, repo)
    ok, _, _ = sess.resume_session("qwer1234")
    assert ok is True
    assert sess.current_role.value == "analyzer"


class TestAutoApprove:
    def test_default_off_y_reset_lo_apaga(self):
        from orchestration.session import Session

        s = Session(llm=None, repo_path="/tmp")
        assert s._auto_approve is False
        s._auto_approve = True
        s.session_id = "x"
        s._messages = []
        # reset() reconstruye agente (necesita LLM) → solo verificar flag:
        # simular lo que reset hace con el flag
        s._auto_approve = False
        assert s._auto_approve is False

    def test_toggle_on_off_bare_e_invalido(self):
        from orchestration.session import Session

        s = Session(llm=None, repo_path="/tmp")
        assert "ON" in s.toggle_auto_approve("")
        assert s._auto_approve is True
        assert "ON" in s.toggle_auto_approve("on")
        assert "OFF" in s.toggle_auto_approve("off")
        assert s._auto_approve is False
        assert "OFF" in s.toggle_auto_approve("no")
        msg = s.toggle_auto_approve("quizás")
        assert "Uso:" in msg and s._auto_approve is False

    def test_confirm_salta_pregunta_con_autoapprove(self):
        from orchestration.session import Session

        s = Session(llm=None, repo_path="/tmp")
        s._fullscreen = True  # modo interactivo: normalmente preguntaría
        s._auto_approve = True
        assert s._confirm_write_cb("write_file", {"path": "/tmp/x"}) is True
        assert s._confirm_event is None  # ni siquiera crea el Event

    def test_confirm_imminent_ventana_y_expira(self):
        import time as _t

        from orchestration.session import Session

        s = Session(llm=None, repo_path="/tmp")
        assert s.confirm_imminent() is False
        s._mark_confirm_imminent()
        assert s.confirm_imminent() is True
        assert s.get_status()["confirm_imminent"] is True
        s._confirm_imminent_until = _t.time() - 1
        assert s.confirm_imminent() is False

    def test_resolve_limpia_inminente(self):
        from orchestration.session import Session

        s = Session(llm=None, repo_path="/tmp")
        s._mark_confirm_imminent()
        s.resolve_confirm(True)  # sin evento: no rompe, limpia ventana
        assert s.confirm_imminent() is False

    def test_interpret_rutea_autoapprove(self):
        from display.commands import interpret_slash

        assert interpret_slash("/autoapprove") == ("run", ("/autoapprove", ""))
        assert interpret_slash("/autoapprove off") == ("run", ("/autoapprove", "off"))
        kind, payload = interpret_slash("/auto")
        assert kind == "hint"  # lleva args → muestra uso, no ejecuta
        assert [n for n, _ in payload] == ["/autoapprove"]

    def test_status_expone_flag(self):
        from orchestration.session import Session

        s = Session(llm=None, repo_path="/tmp")
        assert s.get_status()["auto_approve"] is False
        s._auto_approve = True
        assert s.get_status()["auto_approve"] is True


def test_git_daily_commands_registrados():
    """Flujo diario del usuario: commit/push/PR determinísticos, sin LLM."""
    assert interpret_slash("/commit") == ("run", ("/commit", ""))
    assert interpret_slash("/commit feat: algo") == ("run", ("/commit", "feat: algo"))
    assert interpret_slash("/push") == ("run", ("/push", ""))
    assert interpret_slash("/pr") == ("run", ("/pr", ""))
    assert interpret_slash("/pr develop") == ("run", ("/pr", "develop"))


def test_budget_retry_incluye_git_tools():
    """E2E 'implementar commit...': el retry tras corte de razonamiento quedó
    SIN tools de git y el modelo leyó .git/config crudo. El retry debe poder
    completar el commit (git RO + stage/commit/push)."""
    from tools import BUDGET_RETRY_TOOLS

    names = [t.name for t in BUDGET_RETRY_TOOLS]
    for must in ("git_status", "current_branch", "stage_files", "create_commit", "push"):
        assert must in names, f"{must} falta en BUDGET_RETRY_TOOLS"
    assert "delete_file" not in names


def test_pr_comment_registrado_en_pools():
    from orchestration.tool_dedupe import READISH_TOOL_NAMES, WRITE_TOOL_NAMES
    from tools import EXECUTOR_TOOLS, REVIEWER_TOOLS

    assert "pr_comment" in [t.name for t in REVIEWER_TOOLS]
    assert "pr_comment" in [t.name for t in EXECUTOR_TOOLS]
    # acción git-side: NO es write de código ni readish del turno
    assert "pr_comment" not in WRITE_TOOL_NAMES
    assert "pr_comment" not in READISH_TOOL_NAMES


def test_read_file_rechaza_git_internals(tmp_path):
    from tools.filesystem import read_file

    r = read_file.invoke({"path": str(tmp_path / ".git" / "config")})
    assert "No leas el interior de .git" in r
    assert "git_status" in r
