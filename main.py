#!/usr/bin/env python3
"""
AgentDevs — Agente de desarrollo con LLM local.

Orquestador multicapa: clasifica la intención del usuario y delega al rol
correspondiente (analyze, plan, execute, review, chat) cargando solo las
tools y el system prompt necesarios para cada tarea.

Uso:
    python main.py "/ruta/al/repo"            # modo interactivo
    python main.py --analyze "/ruta/al/repo"  # genera y guarda el análisis (caché)
    python main.py --list                     # lista los análisis guardados
"""

import argparse
import os
import sys
import warnings
from pathlib import Path

# Blindaje de imports: el repo del harness va PRIMERO en sys.path para que un
# CWD con módulos genéricos (config/, tools/) no haga shadowing de los
# nuestros (E2E: `python -c "import orchestration..."` desde otro repo fallaba
# con ImportError de `config` en ubicación desconocida). No hay colisión con
# paquetes instalados (verificado: ningún dist provee esos top-levels).
_harness_root = str(Path(__file__).resolve().parent)
if _harness_root not in sys.path:
    sys.path.insert(0, _harness_root)

from analyzer import run_analysis  # noqa: E402
from cache import (  # noqa: E402
    list_repos,
    load_analysis,
    save_analysis,
    snapshot_diff_files,
    snapshot_entries,
    snapshot_hash,
)
from config import (  # noqa: E402
    ANALYSIS_LLM_TIMEOUT,
    ANALYSIS_REUSE_MAX_FILES,
    LLM_BASE_URL,
    LLM_MAX_TOKENS,
    LLM_MODEL_NAME,
    LLM_TEMPERATURE,
)
from core.textutil import normalize  # noqa: E402
from display.commands import format_help, interpret_slash  # noqa: E402
from display.console import console, print_welcome  # noqa: E402
from display.tui import get_user_input  # noqa: E402
from llm_wrapper import LocalLLM, get_usage, reset_turn_usage  # noqa: E402
from orchestration.session import Session  # noqa: E402
from tools import ALL_TOOLS  # noqa: E402

warnings.filterwarnings("ignore", category=DeprecationWarning)


def _make_llm(max_tokens: int = LLM_MAX_TOKENS, temperature: float = LLM_TEMPERATURE,
             provider: str | None = None, model: str | None = None,
             reasoning: str | None = None) -> LocalLLM:
    from llm_config import resolve_llm

    resolved = resolve_llm(provider_override=provider, model_override=model,
                           reasoning_override=reasoning)
    return LocalLLM(
        base_url=resolved["base_url"],
        model_name=resolved["model"],
        temperature=temperature,
        max_tokens=max_tokens,
        api_key=resolved["api_key"] or "not-needed",
        transport=resolved.get("transport") or "chat",
        reasoning_effort=resolved.get("reasoning") or None,
    )


def _opencode_missing_exit() -> None:
    """Mensaje amigable cuando falta la API key de OpenCode. Nunca hace traceback."""
    from core.lang import resolve_language
    from core.lang import t as _t
    lang = resolve_language()
    print(_t("main_opencode_missing_1", lang), flush=True)
    print(_t("main_opencode_missing_2", lang), flush=True)
    print(_t("main_opencode_missing_3", lang), flush=True)
    print(_t("main_opencode_missing_4", lang), flush=True)
    print(_t("main_opencode_missing_5", lang), flush=True)
    sys.exit(1)


class _UseLocalFallback(Exception):
    """Señal interna: OpenCode falló pero hay llama.cpp vivo → reintentar en local."""

    def __init__(self, reason: str = ""):
        super().__init__(reason)
        self.reason = reason


def _describe_opencode_failure(exc: BaseException | None) -> str:
    """Resumen corto del fallo de OpenCode para el aviso de fallback (sin traceback)."""
    from core.lang import resolve_language
    from core.lang import t as _t
    lang = resolve_language()
    if exc is None:
        return _t("main_describe_no_detail", lang)
    raw = str(exc).strip()
    msg = raw.splitlines()[-1].strip() if raw else type(exc).__name__
    low = msg.lower()
    if "401" in low or "unauthorized" in low:
        return _t("main_describe_rejected", lang)
    if "429" in low or "quota" in low or "balance" in low or "credit" in low or "rate" in low:
        return _t("main_describe_quota", lang)
    if "ssl" in low or "certificate" in low:
        return f"SSL ({msg[-120:]})"
    if "timeout" in low or "timed out" in low:
        return _t("main_describe_timeout", lang)
    return (msg[-160:] or type(exc).__name__)


def _is_opencode_failure(exc: BaseException) -> bool:
    """True si la excepción parece un fallo de la nube (red/SSL/auth/cuota/protocolo/http).

    Cubre lo que el wrapper de OpenAI levanta: APIConnectionError (incluye
    fallos SSL como el CERTIFICATE_VERIFY_FAILED visto en campo),
    timeouts, errores de estado HTTP y errores de protocolo chat vs responses.
    """
    if _is_llama_connection_error(exc):
        return True
    try:
        from llm_wrapper import is_protocol_error

        if is_protocol_error(exc):
            return True
    except Exception:
        pass
    names: set[str] = set()
    msgs: list[str] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        names.add(type(cur).__name__)
        try:
            msgs.append(str(cur).lower())
        except Exception:
            pass
        nxt = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)
        cur = nxt if isinstance(nxt, BaseException) else None
    if names & {
        "APIConnectionError", "APITimeoutError", "APIStatusError",
        "AuthenticationError", "PermissionDeniedError", "RateLimitError",
        "BadRequestError", "InternalServerError", "TimeoutError",
        "SSLError", "SSLCertVerificationError", "CertificateError",
        "URLError", "HTTPError", "MaxRetriesExceeded",
        "ConnectTimeout", "ReadTimeout",
    }:
        return True
    blob = " ".join(msgs)
    keywords = (
        "ssl", "certificate verify", "tls", "handshake",
        "timeout", "timed out",
        "401", "403", "429", "500", "502", "503", "504",
        "unauthorized", "forbidden", "rate limit", "quota",
        "balance", "credit", "free-tier", "freetier",
        "network", "unreachable", "dns", "connection",
        "temporarily", "service unavailable", "internal server error",
    )
    return any(k in blob for k in keywords)


def _local_fallback_resolved() -> dict | None:
    """Resolved local si hay un llama-server vivo; si no, None (no bloquea)."""
    alive, _ = _llama_server_alive(timeout=1.5)
    if not alive:
        return None
    try:
        from llm_config import resolve_llm

        return resolve_llm(provider_override="local")
    except Exception:
        return None


def _announce_opencode_fallback(reason: str) -> None:
    """Aviso de que se sigue en local aunque el config diga opencode."""
    from core.lang import resolve_language
    from core.lang import t as _t
    lang = resolve_language()
    print(_t("main_fallback_1", lang).format(reason=reason),
          flush=True)
    print(_t("main_fallback_2", lang), flush=True)
    print(_t("main_fallback_3", lang), flush=True)


def _both_down_exit(op_reason: str) -> None:
    """Salida final cuando NI la nube NI el local responden. Nunca hace traceback."""
    from core.lang import resolve_language
    from core.lang import t as _t
    lang = resolve_language()
    print(_t("main_both_down_1", lang).format(op_reason=op_reason), flush=True)
    print(_t("main_both_down_2", lang).format(base=LLM_BASE_URL), flush=True)
    print("       llama-server -hf bartowski/InternScience_Agents-A1-4B-GGUF:Q4_K_M --port 8080",
          flush=True)
    print(_t("main_both_down_4", lang), flush=True)
    print(_t("main_both_down_5", lang), flush=True)
    sys.exit(1)


def _cached_analysis_valid(cached: dict) -> bool:
    """True si el summary cacheado es usable (no eco del prompt ni vacío)."""
    analysis = (cached.get("analysis") or "").strip()
    poison = (
        "resumen en español de 80-120 palabras",
        "qué hace el proyecto, arquitectura general y estructura de carpetas",
    )
    return not (any(p in analysis.lower() for p in poison) or len(analysis) < 40)


def _ensure_analysis(llm: LocalLLM, repo_path: str, status: str | None = None) -> dict:
    from core.lang import resolve_language
    from core.lang import t as _t
    lang = resolve_language()
    cached = load_analysis(repo_path)
    if cached:
        current = snapshot_hash(repo_path)
        if current and cached["snapshot_hash"] == current and _cached_analysis_valid(cached):
            return cached
        if not _cached_analysis_valid(cached):
            print(_t("main_cache_invalid", lang), flush=True)
        else:
            # El repo cambió: si son pocos archivos, el summary sigue válido
            # (el resumen de un repo no cambia porque editaste una card).
            # Se refresca el hash sin pagar 1-2 min de LLM.
            entries = snapshot_entries(repo_path)
            changed = snapshot_diff_files(cached.get("snapshot_files"), entries)
            if changed is not None and changed <= ANALYSIS_REUSE_MAX_FILES:
                save_analysis(
                    repo_path, snapshot=current,
                    language=cached.get("language") or "",
                    tech_stack=cached.get("tech_stack") or "",
                    analysis=cached.get("analysis") or "",
                    snapshot_files="\n".join(entries),
                )
                print(
                    _t("main_cache_reused", lang).format(changed=changed),
                    flush=True,
                )
                return load_analysis(repo_path) or cached
    # Chequeo rápido según el LLM que SE VA A USAR (no el de config.py):
    # local → ping al server; nube → solo exigir key (el turno reporta el resto).
    # Si la nube falla pero hay llama vivo, se levanta _UseLocalFallback para
    # que el caller reintente en local (el config sigue en opencode).
    from llm_config import is_local_url

    if is_local_url(getattr(llm, "base_url", "") or ""):
        alive, _ = _llama_server_alive(timeout=1.5)
        if not alive:
            _llama_down_exit()
    elif not (getattr(llm, "api_key", "") or ""):
        if _local_fallback_resolved() is not None:
            _announce_opencode_fallback("sin API key")
            raise _UseLocalFallback("sin API key")
        _opencode_missing_exit()
    if status:
        print(status, flush=True)
    try:
        result = run_analysis(repo_path, llm, on_token=lambda tok: print(tok, end="", flush=True), timeout=ANALYSIS_LLM_TIMEOUT)
    except FileNotFoundError as e:
        print(f"❌ {e}")
        sys.exit(1)
    except (KeyboardInterrupt, SystemExit, _UseLocalFallback):
        raise
    except BaseException as e:
        try:
            from llm_config import is_local_url as _is_local

            _is_op = not _is_local(getattr(llm, "base_url", "") or "")
        except Exception:
            _is_op = False
        if _is_op and _is_opencode_failure(e):
            if _local_fallback_resolved() is not None:
                _announce_opencode_fallback(_describe_opencode_failure(e))
                raise _UseLocalFallback(_describe_opencode_failure(e)) from e
            # Sin local vivo no hay fallback: mensaje final combinado
            # (según el LLM en uso, no según el config global).
            _both_down_exit(_describe_opencode_failure(e))
        _llm_down_exit(e)
        raise
    save_analysis(repo_path, snapshot=result["snapshot"], language=result["language"],
                  tech_stack=result["tech_stack"], analysis=result["analysis"],
                  snapshot_files="\n".join(snapshot_entries(repo_path)))
    print(_t("main_analysis_saved", lang))
    print(_t("main_analysis_lang", lang).format(language=result['language']))
    print(_t("main_analysis_stack", lang).format(stack=result['tech_stack']))
    print(result["analysis"])
    print()
    return result


def _format_cached_context(cached: dict) -> str:
    """Contexto compacto para el system prompt (lenguaje + stack + summary)."""
    parts = []
    if cached.get("language"):
        parts.append(f"Lenguaje: {cached['language']}")
    if cached.get("tech_stack"):
        parts.append(f"Stack: {cached['tech_stack']}")
    if cached.get("analysis"):
        parts.append(cached["analysis"])
    return "\n".join(parts)


def deterministic_analysis(repo_path: str) -> dict:
    """Análisis sin LLM: lenguaje + stack + resumen determinista.

    Para `--no-analysis` (sesiones rápidas): usa el caché si es válido y si
    no, construye el contexto sin llamar al modelo. Nunca tarda más de ~1s.
    """
    from analyzer import detect_language, detect_stack, deterministic_summary

    cached = load_analysis(repo_path)
    if cached and cached.get("snapshot_hash") == snapshot_hash(repo_path) \
            and _cached_analysis_valid(cached):
        return cached
    language = detect_language(repo_path)
    stack = detect_stack(repo_path) or language
    entries = snapshot_entries(repo_path)
    current = snapshot_hash(repo_path)
    result = {
        "path": repo_path,
        "snapshot": current,
        "language": language,
        "tech_stack": stack,
        "analysis": deterministic_summary(repo_path, language, stack),
    }
    save_analysis(repo_path, snapshot=current, language=language,
                  tech_stack=stack, analysis=result["analysis"],
                  snapshot_files="\n".join(entries))
    return result


def do_analyze(llm: LocalLLM, repo_path: str):
    from core.lang import resolve_language
    from core.lang import t as _t
    lang = resolve_language()
    reset_turn_usage()
    start = __import__('time').monotonic()
    _ensure_analysis(llm, repo_path, status=_t("main_analyze_doing", lang).format(repo=repo_path))
    elapsed = __import__('time').monotonic() - start
    usage = get_usage()
    print(f"\n⚡ {elapsed:.1f}s | {usage['turn']['prompt'] + usage['turn']['completion']:,} tokens")


def do_list():
    from core.lang import resolve_language
    from core.lang import t as _t
    lang = resolve_language()
    repos = list_repos()
    if not repos:
        print(_t("main_list_empty", lang))
        return
    print(f"{_t('main_list_repo', lang):<48} {_t('main_list_lang', lang):<13} {_t('main_list_stack', lang):<32} {_t('main_list_updated', lang)}")
    print("─" * 130)
    for r in repos:
        print(f"{r['path']:<48} {(r['language'] or '-'):<13} {(r['tech_stack'] or '-')[:32]:<32} {r['updated_at']}")


def _warn_dirty_repo(repo_path: str) -> None:
    """Advierte si el repo target tiene cambios sin commitear al ARRANCAR.

    Un repo sucio contamina la verificación: lint/tests fallan por daño
    pre-existente (ajeno a la tarea) y el modelo gasta el presupuesto
    arreglando lo de otros — E2E real: 4537 líneas borradas en rules_catalog
    sin restaurar entre corridas. Es solo un aviso de consola: no toca el
    contexto del modelo."""
    try:
        import subprocess
        proc = subprocess.run(
            ["git", "-C", repo_path, "status", "--porcelain"],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            return
        entries = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        if not entries:
            return
        untracked = sum(1 for ln in entries if ln.startswith("??"))
        changed = len(entries) - untracked
        from core.lang import resolve_language as _rl
        from core.lang import t as _t2
        _lang = _rl()
        console.print(
            _t2("main_dirty", _lang).format(changed=changed, untracked=untracked)
        )
    except Exception:
        pass


def run_fullscreen(session) -> None:
    """Modo --tui (Textual): panel de mensajes con scroll + input/toolbar fijos.

    Captura console.file y sys.stdout/sys.stderr a nivel PYTHON (Textual
    renderiza vía sys.__stderr__, por eso NO se tocan los file descriptors).
    Los logs nativos de procesos EXTERNOS (p. ej. llama-server compartiendo la
    terminal) no se pueden capturar desde acá — lanzalos con salida a archivo.
    """
    from display import console as _console_mod
    from display.console import console
    from display.fullscreen_tui import FullscreenTUI
    from core.lang import t as _t

    session._fullscreen = True  # sin EscWatcher ni prompts input() internos
    _console_mod.MD_MARKERS_ENABLED = True  # el pane intercepta los marcadores

    tui = FullscreenTUI(
        status_provider=session.get_status,
        on_submit=lambda text: None,  # se setea abajo (closure completa)
    )

    old_file = console.file
    console.file = tui.pane_writer
    # El writer es isatty()=True y tiene fileno → rich detecta el ancho real.
    # Además forzamos un ancho MUY grande para que rich NUNCA envuelva (si
    # envolviera a 80/otro, las respuestas llegarían al panel ya cortadas en
    # una columna angosta). El RichLog (wrap=True) es quien envuelve al ancho
    # REAL del panel.
    console._color_system = console._detect_color_system()
    console._width = 1000
    console._height = 1000

    # Capturar stdout/stderr PYTHON (print de libs, warnings, traces): Textual
    # escribe por sys.__stderr__, así que el swap no afecta su render.
    import sys as _sys

    class _StreamToPane:
        def __init__(self, pane):
            self._pane = pane
        def write(self, text):
            if text:
                self._pane.write(text)
            return len(text) if text else 0
        def flush(self):
            pass
        def isatty(self):
            return True

    _old_stdout, _old_stderr = _sys.stdout, _sys.stderr
    _sys.stdout = _StreamToPane(tui.pane_writer)
    _sys.stderr = _StreamToPane(tui.pane_writer)

    def _restore_streams():
        _sys.stdout, _sys.stderr = _old_stdout, _old_stderr
        console.file = old_file
        _console_mod.MD_MARKERS_ENABLED = False

    def on_submit(text: str) -> None:
        from core.lang import t as _t2
        _lang = getattr(session, "_lang", "es")
        stripped = text.strip().lower()
        if stripped in ("exit", "quit", "salir", "q"):
            console.print(_t2("main_bye_dim", _lang))
            tui.exit()
            return
        if not text.strip():
            return
        # ECO del prompt (también slash y sí/no: el input se limpia al enviar
        # y sin esto lo tipeado no aparece en el panel).
        from rich.markup import escape

        console.print()
        console.print(_t2("main_tui_you", _lang))
        console.print(f"[dark_orange]{escape(text)}[/dark_orange]")
        console.print()
        # Confirmación de write pendiente (EXECUTE_CONFIRM_WRITES): el input
        # resuelve la aprobación en vez de lanzar un turno nuevo. Se normaliza
        # el texto (minúsculas + sin acentos) para aceptar sí/SÍ/si por igual.
        if session.confirm_pending():
            if normalize(text) in ("s", "si", "y", "yes", "aprob", "ok", "dale"):
                session.resolve_confirm(True)
            else:
                session.resolve_confirm(False)
            return
        # Carrera s/n: el pedido viene en camino pero el evento aún no existe.
        # Esperar breve (hilo de fondo) a que se registre y resolverlo; si no
        # aparece, avisar sin abrir un turno nuevo (el s/n nunca es un prompt).
        if normalize(text) in ("s", "si", "y", "yes", "n", "no") and session.confirm_imminent():
            import time as _time

            for _ in range(20):
                if session.confirm_pending():
                    break
                _time.sleep(0.1)
            if session.confirm_pending():
                if normalize(text) in ("s", "si", "y", "yes"):
                    session.resolve_confirm(True)
                else:
                    session.resolve_confirm(False)
            else:
                console.print(
                    _t2("main_tui_confirm_wait", _lang)
                )
            return
        # Selección pendiente de untracked (/commit lista + "1 3"/"todos"):
        # se resuelve acá sin abrir turno nuevo.
        pick_msg = session.try_commit_pick(text)
        if pick_msg is not None:
            console.print(pick_msg)
            return
        kind, payload = interpret_slash(text)
        if kind == "message":
            pass  # sigue abajo: eco + run_turn
        elif kind == "help":
            console.print("[bold]Comandos:[/bold]\n" + format_help(lang=getattr(session, "_lang", "es")))
            return
        elif kind == "hint":
            matches = payload
            if matches:
                console.print("[yellow]¿Quisiste decir?[/yellow]\n" + format_help(matches, lang=getattr(session, "_lang", "es")))
            else:
                console.print("[yellow]Comando desconocido. Disponibles:[/yellow]\n" + format_help(lang=getattr(session, "_lang", "es")))
            return
        elif payload[0] == "/new":
            session.reset()
            session._fullscreen = True  # reset() no debe apagar el modo
            tui.clear_pane()  # sesión nueva → panel nuevo
            console.print(_t2("main_new_session_full", _lang).format(sid=session.session_id))
            return
        elif payload[0] == "/compact":
            console.print(_t2("main_compacting", _lang))
            session.force_summarize()
            console.print(_t2("main_compact_done", _lang).format(pct=f"{session.context_usage_pct():.0f}"))
            return
        elif payload[0] == "/history":
            turns = session.get_recent_history(limit=10)
            if not turns:
                console.print(_t2("main_no_turns", _lang))
                return
            for t in turns:
                user = (t.get("user_message") or "")[:80]
                asst = (t.get("assistant_message") or "")[:80]
                console.print(
                    _t2("main_history_line", _lang).format(role=t.get('role', '-'), sid=t.get('session_id', '-'), tokens=t.get('tokens_used', 0), user=user)
                )
                if asst:
                    console.print(_t2("main_agent_line", _lang).format(asst=asst))
            console.print(_t2("main_resume_hint", _lang))
            return
        elif payload[0] == "/resume":
            ok, msg, data = session.resume_session(payload[1])
            console.print(f"[green]{msg}[/green]" if ok else f"[red]{msg}[/red]")
            if ok:
                for t in data:
                    user = (t.get("user_message") or "")[:300]
                    asst = (t.get("assistant_message") or "")[:300]
                    console.print(_t2("main_vos_line", _lang).format(user=user))
                    if asst:
                        console.print(f"[dim]{asst}[/dim]")
            return
        elif payload[0] == "/autoapprove":
            msg = session.toggle_auto_approve(payload[1])
            if msg.startswith("✅"):
                console.print(f"[green]{msg}[/green]")
            else:
                console.print(f"[red]{msg}[/red]")
            return
        elif payload[0] == "/verify":
            from orchestration.session import run_commit_verification

            console.print(_t2("main_verifying", _lang))
            captured: dict = {}
            passed, report = run_commit_verification(
                session.repo_path, reuse=getattr(session, "_verify_results", None),
                capture=captured,
            )
            console.print(report)
            for area, tail in captured.get("details", {}).items():
                console.print(_t2("main_verify_tail", _lang).format(area=area, tail=tail))
            session.note_verify_result(passed, report, captured.get("details"))
            console.print(
                _t2("main_all_green", _lang) if passed
                else _t2("main_reds_above", _lang)
            )
            return
        elif payload[0] == "/commit":
            session.slash_commit(payload[1])
            return
        elif payload[0] == "/tasks-pool":
            session.slash_task_pool(payload[1])
            return
        elif payload[0] == "/push":
            session.slash_push(payload[1])
            return
        elif payload[0] == "/pr":
            session.slash_pr(payload[1])
            return
        elif payload[0] == "/model":
            console.print(session.slash_model(payload[1]))
            return
        elif payload[0] == "/language":
            console.print(session.slash_language(payload[1]))
            return
        elif payload[0] == "/log":
            import datetime
            try:
                count = int((payload[1] or "").strip() or 200)
            except ValueError:
                count = 200
            text = tui.dump_pane(count)
            lines = text.splitlines()
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            out = f"/tmp/agentdevs-pane-{stamp}.log"
            try:
                with open(out, "w", encoding="utf-8") as fh:
                    fh.write(text + ("\n" if text and not text.endswith("\n") else ""))
                console.print(_t2("main_pane_dumped", _lang).format(out=out, n=len(lines)))
            except OSError as e:
                console.print(_t2("main_pane_fail", _lang).format(out=out, err=e))
            return
        elif payload[0] == "/help":
            console.print("[bold]Comandos:[/bold]\n" + format_help(lang=getattr(session, "_lang", "es")))
            return

        # Turno normal del agente (el eco ya se imprimió arriba).
        session.run_turn(text)

    tui.on_submit = on_submit
    tui.on_cancel = session.request_cancel

    # El header FIJO de la TUI ya muestra LLM/repo/modelo/tools — no duplicar
    # el panel de welcome en el scrollable. Solo el hint inicial.
    console.print(_t("main_tui_hint", getattr(session, "_lang", "es")))
    _warn_dirty_repo(session.repo_path)

    try:
        tui.run()
    except KeyboardInterrupt:
        console.print("\n" + _t("main_bye_dim", getattr(session, "_lang", "es")))
    finally:
        _restore_streams()


# ── Doctor: verificación de entorno + instalación de faltantes ──────────────

# (requirement pip, módulo importable): el import es la prueba real de que se
# puede usar; el requirement es lo que se instala si falta.
_DOCTOR_DEPS = [
    ("langgraph>=1.0.0", "langgraph"),
    ("langchain>=1.0.0", "langchain"),
    ("langchain-openai>=1.0.0", "langchain_openai"),
    ("langchain-mcp-adapters>=0.3.1", "langchain_mcp_adapters"),
    ("mcp>=1.24.0,<2.0.0", "mcp"),
    ("prompt_toolkit>=3.0.0", "prompt_toolkit"),
    ("rich>=13.0.0", "rich"),
    ("textual>=1.0.0", "textual"),
]
# gnureadline solo aplica en macOS (en Linux sobra, en Windows no compila).
if sys.platform == "darwin":
    _DOCTOR_DEPS.append(("gnureadline>=8.1.2", "gnureadline"))

_CMCP_INSTALL_CMD = (
    "curl -fsSL https://raw.githubusercontent.com/DeusData/codebase-memory-mcp/main/install.sh | bash"
)

# Recomendación de modelo por RAM (verificado: los repos existen en HF y
# las familias pasaron por el banco del harness con tool calling real).
# Qwen3.6-35B-A3B es el mejor medido acá (7/7 + 19/25 Polyglot): en 32GB
# entra el Q4_K_M (22GB); en 24GB solo quants chicos (~13-17GB). El doctor
# nunca descarga solo.
_MODEL_TIERS = (
    # (ram_min_gb, etiqueta, comando -hf, peso aprox, nota)
    (48, "64GB o más",
     "unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q5_K_XL", "~27GB",
     "misma familia del mejor medido, quant mayor; alt liviano: "
     "Qwen3.8-27B Q4_K_M (~17GB)"),
    (28, "32GB",
     "unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M", "~22GB",
     "lo mejor medido acá: 7/7 + 19/25 Polyglot; Terminal-Bench 2.0 "
     "oficial 51.5 (vs 41.6 del 27B)"),
    (20, "24GB",
     "unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q3_K_XL", "~17GB",
     "medido acá (Q3_XXS): 19/25 Polyglot; el Q4_K_M (22GB) no entra "
     "con margen de KV — usar quants chicos"),
    (0, "16GB",
     "bartowski/InternScience_Agents-A1-4B-GGUF:Q4_K_M", "~2.5GB",
     "probado en el banco 6/7 (tiende a loopear; el harness lo contiene). "
     "Alt: Spark-X2.5-4B (mejor agentic, pero exige el fork XHToken/llama.cpp)"),
)


def _total_ram_gb() -> float | None:
    """RAM total en GB (multiplataforma, solo stdlib). None si no se puede."""
    import subprocess as sp

    try:
        if sys.platform == "win32":
            import ctypes

            class _MemStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            st = _MemStatus()
            st.dwLength = ctypes.sizeof(_MemStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return st.ullTotalPhys / 1_000_000_000
            return None
        if sys.platform == "darwin":
            out = sp.run(["sysctl", "-n", "hw.memsize"],
                         capture_output=True, text=True, timeout=10)
            return int((out.stdout or "0").strip() or 0) / 1_000_000_000 or None
        pages = os.sysconf("SC_PHYS_PAGES")
        return os.sysconf("SC_PAGE_SIZE") * pages / 1_000_000_000
    except Exception:
        return None


def _doctor_model_advice() -> None:
    """Tabla de modelos por RAM + highlight de tu tier. Solo informa."""
    from core.lang import resolve_language
    from core.lang import t as _t
    lang = resolve_language()
    ram = _total_ram_gb()
    print(_t("main_doctor_ram", lang))
    tiers = list(_MODEL_TIERS)  # ordenados de mayor a menor RAM mínima
    _note_keys = {48: "main_doctor_note_64", 28: "main_doctor_note_32", 20: "main_doctor_note_24", 0: "main_doctor_note_16"}
    for i, (ram_min, label, hf, size, note) in enumerate(tiers):
        ceiling = tiers[i - 1][0] if i > 0 else float("inf")
        mine = ram is not None and ram_min <= ram < ceiling
        mark = _t("main_doctor_your_tier", lang) if mine else ""
        label_out = _t("main_doctor_tier_64", lang) if label == "64GB o más" else label
        print(_t("main_doctor_tier_line", lang).format(label=label_out, hf=hf, size=size, mark=mark))
        note_out = _t(_note_keys.get(ram_min, ""), lang) if ram_min in _note_keys else note
        # fallback: si la key falta, usar nota original
        if note_out.startswith("main_doctor_note"):
            note_out = note
        print(f"       {note_out}")
    if ram is None:
        print(_t("main_doctor_no_ram", lang))
    else:
        print(_t("main_doctor_ram_found", lang).format(ram=ram))


def _doctor_ok(name, detail=""):
    print(f"  ✅ {name}" + (f" — {detail}" if detail else ""))


def _doctor_fixed(name, detail=""):
    from core.lang import resolve_language
    from core.lang import t as _t
    lang = resolve_language()
    print(_t("main_doctor_fixed", lang).format(name=name) + (f" ({detail})" if detail else ""))


def _doctor_fail(name, detail):
    print(f"  ❌ {name} — {detail}")


def _pip_install(requirement: str) -> bool:
    import subprocess
    from core.lang import resolve_language
    from core.lang import t as _t
    lang = resolve_language()

    print(_t("main_pip_installing", lang).format(req=requirement))
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", requirement],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(_t("main_pip_failed", lang).format(tail=(proc.stderr or proc.stdout).strip()[-300:]))
        return False
    return True


def _llama_server_alive(timeout: float = 2.0) -> tuple[bool, str]:
    """True si hay un llama-server respondiendo en LLM_BASE_URL."""
    import urllib.request

    base = LLM_BASE_URL.split("/v1")[0]
    for path in ("/health", "/v1/models"):
        try:
            with urllib.request.urlopen(base + path, timeout=timeout) as resp:
                if resp.status == 200:
                    return True, base
        except Exception:
            continue
    return False, base


def _is_llama_connection_error(exc: BaseException) -> bool:
    """Detecta errores de conexión a llama-server (httpx/httpcore/openai)."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        name = type(cur).__name__
        msg = str(cur).lower()
        if name in ("APIConnectionError", "ConnectError"):
            return True
        if "all connection attempts failed" in msg or "connection error" in msg:
            return True
        # openai.APIConnectionError suele tener mensaje "Connection error."
        if "failed to connect" in msg:
            return True
        # recorrer causa y contexto
        nxt = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)
        cur = nxt if isinstance(nxt, BaseException) else None
    return False


def _llama_down_exit() -> None:
    """Mensaje amigable cuando llama.cpp no está levantado. Nunca hace traceback."""
    # No re-chequear con timeout largo: LLM_BASE_URL ya es la fuente de verdad
    from core.lang import resolve_language
    from core.lang import t as _t
    lang = resolve_language()
    print(_t("main_llama_down_1", lang).format(base=LLM_BASE_URL), flush=True)
    print(_t("main_llama_down_2", lang), flush=True)
    print("     llama-server -hf bartowski/InternScience_Agents-A1-4B-GGUF:Q4_K_M --port 8080", flush=True)
    print(_t("main_llama_down_4", lang), flush=True)
    print(_t("main_llama_down_5", lang), flush=True)
    print(_t("main_llama_down_6", lang), flush=True)
    sys.exit(1)


def _opencode_down_exit(exc: BaseException | None = None) -> None:
    """Mensaje amigable cuando OpenCode falla (red, key inválida, sin saldo)."""
    from llm_wrapper import is_protocol_error

    from core.lang import resolve_language
    from core.lang import t as _t
    lang = resolve_language()
    msg = str(exc).lower() if exc else ""
    print(_t("main_opencode_down_title", lang), flush=True)
    if exc is not None and is_protocol_error(exc):
        print(_t("main_opencode_down_proto1", lang), flush=True)
        print(_t("main_opencode_down_proto2", lang), flush=True)
        print(_t("main_opencode_down_proto3", lang), flush=True)
    elif "401" in msg or "unauthorized" in msg or "invalid" in msg and "key" in msg:
        print(_t("main_opencode_down_401", lang), flush=True)
        print("     agent-devs --setup-opencode", flush=True)
    elif "429" in msg or "rate" in msg or "quota" in msg or "balance" in msg or "credit" in msg:
        print(_t("main_opencode_down_quota1", lang), flush=True)
        print(_t("main_opencode_down_quota2", lang), flush=True)
    else:
        print(_t("main_opencode_down_other1", lang), flush=True)
        print(_t("main_opencode_down_other2", lang), flush=True)
        print(_t("main_opencode_down_other3", lang), flush=True)
    print(flush=True)
    sys.exit(1)


def _messages_api_exit(model: str) -> None:
    """El modelo solo habla la API de Anthropic Messages (sin soporte acá)."""
    from core.lang import resolve_language
    from core.lang import t as _t
    lang = resolve_language()
    print(_t("main_messages_1", lang).format(model=model), flush=True)
    print(_t("main_messages_2", lang), flush=True)
    print(_t("main_messages_3", lang), flush=True)
    print("     agent-devs --provider opencode --model deepseek-v4.1-flash .", flush=True)
    print("     agent-devs --provider opencode --model muse-spark-1.3 .   # Responses + reasoning", flush=True)
    print(_t("main_messages_6", lang), flush=True)
    sys.exit(1)


def run_setup_opencode() -> int:
    """Pide la API key y la guarda en el config de usuario (600). Nunca la imprime."""
    import getpass

    from llm_config import (
        OPENCODE_DEFAULT_BASE_URL,
        DEFAULT_REASONING_EFFORT,
        active_config_file,
        load_file_config,
        save_config,
    )

    from core.lang import resolve_language
    from core.lang import t as _t
    lang = resolve_language()
    current = load_file_config()
    print(_t("main_setup_title", lang))
    print(_t("main_setup_get", lang))
    print(_t("main_setup_save", lang).format(path=active_config_file()))
    if current.get("opencode_api_key"):
        print(_t("main_setup_exists", lang))
    try:
        key = getpass.getpass(_t("main_setup_prompt", lang)).strip()
    except (EOFError, KeyboardInterrupt):
        print(_t("main_setup_cancel", lang))
        return 1
    if not key:
        if current.get("opencode_api_key"):
            key = current["opencode_api_key"]
        else:
            print(_t("main_setup_nokey", lang))
            return 1
    if not key.startswith("oc_sk_"):
        print(_t("main_setup_warn", lang))
    current["provider"] = "opencode"
    current["opencode_api_key"] = key
    current.setdefault("opencode_model", "deepseek-v4.1-flash")
    current.setdefault("opencode_reasoning", DEFAULT_REASONING_EFFORT)
    current.setdefault("opencode_base_url", OPENCODE_DEFAULT_BASE_URL)
    saved = save_config(current)
    print(_t("main_setup_saved", lang).format(path=saved, model=current['opencode_model'], reason=current['opencode_reasoning']))
    print(_t("main_setup_try", lang))
    print(_t("main_setup_use", lang))
    return 0


def run_show_config() -> int:
    """Muestra la config efectiva con la key tapada. Exit 0 siempre."""
    from llm_config import active_config_file, load_file_config, resolve_llm
    from core.lang import resolve_language
    from core.lang import t as _t
    lang = resolve_language()

    raw = load_file_config()
    resolved = resolve_llm()
    key = str(raw.get("opencode_api_key") or "")
    masked = f"{key[:6]}…{key[-4:]} ({len(key)} chars)" if key else "ausente"

    print(_t("main_config_title", lang))
    print(_t("main_config_file", lang).format(path=active_config_file()))
    print(_t("main_config_provider", lang).format(prov=resolved['provider']))
    if resolved["provider"] == "opencode":
        print(_t("main_config_model", lang).format(model=resolved['model'], transport=resolved.get('transport', 'chat'))
              + (f", reasoning {resolved.get('reasoning')}" if resolved.get("transport") == "responses" else "")
              + ")")
        print(_t("main_config_base", lang).format(url=resolved['base_url']))
        print(_t("main_config_key", lang).format(masked=masked))
        if "opencode_reasoning" not in raw:
            print(_t("main_config_reason_default", lang).format(reason=resolved.get('reasoning')))
    else:
        print(_t("main_config_local_model", lang).format(model=resolved['model']))
        print(_t("main_config_base_local", lang).format(url=resolved['base_url']))
    try:
        print(_t("main_config_lang", lang).format(lang=resolve_language()))
    except Exception:
        pass
    print(_t("main_config_flags", lang))
    return 0


def run_list_models() -> int:
    """Lista modelos OpenCode: 🆓/💰 + protocolo (chat/responses). Exit 0/1."""
    from llm_config import (
        fetch_opencode_models,
        format_model_list,
        is_free_model,
        needs_messages_api,
        resolve_llm,
        transport_for,
    )

    resolved = resolve_llm()
    if resolved["provider"] != "opencode":
        # --list-models implica opencode aunque el config diga local.
        resolved = resolve_llm(provider_override="opencode")
    if not resolved["api_key"]:
        _opencode_missing_exit()
    try:
        models = fetch_opencode_models(resolved["api_key"], resolved["base_url"], force=True)
    except Exception as e:
        _opencode_down_exit(e)
        return 1
    for m in models:
        mid = m["id"]
        if needs_messages_api(mid):
            m["proto"] = "messages✗"
        else:
            m["proto"] = "responses" if transport_for(mid) == "responses" else "chat"
    from core.lang import resolve_language
    from core.lang import t as _t
    lang = resolve_language()
    free = [m for m in models if is_free_model(m) is True]
    paid = [m for m in models if is_free_model(m) is False]
    unk = [m for m in models if is_free_model(m) is None]
    print(_t("main_models_count", lang).format(n=len(models), base=resolved['base_url']))
    print(_t("main_models_break", lang).format(free=len(free), paid=len(paid)) + (_t("main_models_unk", lang).format(unk=len(unk)) if unk else ""))
    print(_t("main_models_free_warn", lang))
    print(_t("main_models_proto", lang))
    print(_t("main_models_messages", lang))
    print(_t("main_models_verified", lang))
    print()
    # Gratis primero para que se vean sin scroll.
    ordered = free + unk + paid
    print(format_model_list(ordered))
    print()
    print(_t("main_models_current", lang).format(model=resolved['model'], reason=resolved.get('reasoning', 'medium')))
    print(_t("main_models_change", lang))
    return 0


def _llm_down_exit(exc: BaseException) -> None:
    """Mensaje amigable según el proveedor activo. Si el error no es del LLM,
    retorna sin hacer nada para que el caller lo re-lance (nunca traceback)."""
    from llm_config import resolve_llm

    try:
        provider = resolve_llm()["provider"]
    except Exception:
        provider = "local"
    if provider == "opencode":
        # Cualquier fallo de la nube (conexión, SSL, auth, cuota, HTTP,
        # protocolo) merece el mensaje de OpenCode — no un traceback.
        if _is_opencode_failure(exc):
            _opencode_down_exit(exc)
    elif _is_llama_connection_error(exc):
        _llama_down_exit()


def _locate_install_repo() -> Path | None:
    """Repo del cual esta instalación corre.

    El shim global ejecuta <install>/.venv/bin/agent-devs → sys.prefix ES el
    venv de la instalación y su padre el repo. Fallback: cwd si es un checkout
    del proyecto (modo dev).
    """
    candidates = [Path(sys.prefix).parent]
    candidates.append(Path.cwd())
    for repo in candidates:
        if (repo / ".git").exists() and (repo / "pyproject.toml").exists():
            return repo
    return None


def run_update() -> int:
    """Actualiza esta instalación: git pull --ff-only + pip install -e ."""
    # Reiniciá: la palabra debe seguir en el source (test_harness_version la busca).
    import subprocess as sp
    from core.lang import resolve_language
    from core.lang import t as _t
    lang = resolve_language()

    repo = _locate_install_repo()
    if repo is None:
        print(_t("main_update_no_repo1", lang))
        print(_t("main_update_no_repo2", lang))
        return 1

    def git(*args):
        return sp.run(["git", "-C", str(repo), *args], capture_output=True, text=True)

    head_antes = git("rev-parse", "--short", "HEAD").stdout.strip()
    print(_t("main_update_start", lang).format(repo=repo, head=head_antes))

    r = git("pull", "--ff-only", "-q")
    if r.returncode != 0:
        print(_t("main_update_pull_fail", lang).format(err=(r.stderr or r.stdout).strip()))
        print(_t("main_update_stash", lang).format(repo=repo))
        return 1

    head_despues = git("rev-parse", "--short", "HEAD").stdout.strip()
    if head_antes == head_despues:
        print(_t("main_update_uptodate", lang).format(head=head_despues))
        return 0
    print(f"⬆️  {head_antes} → {head_despues}")

    print(_t("main_update_refresh", lang))
    r = sp.run([sys.executable, "-m", "pip", "install", "-q", "-e", str(repo)])
    if r.returncode != 0:
        print(_t("main_update_pip_fail", lang))
        return 1
    print(_t("main_update_done", lang))
    # Reiniciá las sesiones abiertas (bilingüe main_update_restart).
    print(_t("main_update_restart", lang))
    return 0


def run_doctor() -> int:
    """Verifica el entorno completo e instala de a uno los faltantes.

    Chequeos: Python, venv, git, deps Python (instalables), MCP
    codebase-memory-mcp (instalable en mac/linux) y llama-server (binario:
    auto-instala con brew en macOS, en el resto solo detecta e instruye;
    server vivo en :8080 — siempre solo detecta). Si falta el modelo,
    recomienda cuál bajar según tu RAM (nunca descarga solo).
    Devuelve exit code: 0 = todo listo para usar agent-devs.
    """
    import importlib.util
    import shutil
    import subprocess as sp
    from core.lang import resolve_language
    from core.lang import t as _t
    lang = resolve_language()

    print(_t("main_doctor_title", lang))
    problems = 0

    # 1) Python
    v = sys.version_info
    if (v.major, v.minor) >= (3, 10):
        _doctor_ok(f"Python {v.major}.{v.minor}.{v.micro}")
    else:
        _doctor_fail("Python", _t("main_doctor_py_req", lang).format(v=f"{v.major}.{v.minor}"))
        return 1

    # 2) venv (aviso, no bloquea)
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    if in_venv:
        _doctor_ok(_t("main_doctor_venv_active", lang), sys.prefix)
    else:
        _doctor_fail(
            _t("main_doctor_venv", lang),
            _t("main_doctor_venv_missing", lang),
        )
        problems += 1

    # 3) git
    if shutil.which("git"):
        _doctor_ok("git")
    else:
        _doctor_fail("git", _t("main_doctor_git_missing", lang))
        problems += 1

    # 4) Dependencias Python: instalar de a una las que falten
    for requirement, module in _DOCTOR_DEPS:
        spec = importlib.util.find_spec(module)
        if spec is not None:
            _doctor_ok(module)
            continue
        if _pip_install(requirement):
            if importlib.util.find_spec(module) is not None:
                _doctor_fixed(module, requirement)
            else:
                _doctor_fail(module, _t("main_doctor_import_fail", lang).format(req=requirement))
                problems += 1
        else:
            _doctor_fail(module, _t("main_doctor_install_fail", lang).format(req=requirement))
            problems += 1

    # 5) codebase-memory-mcp (knowledge graph)
    cm = shutil.which("codebase-memory-mcp")
    if cm:
        try:
            out = sp.run(["codebase-memory-mcp", "--version"], capture_output=True, text=True, timeout=15)
            ver = (out.stdout or out.stderr).strip().splitlines()[-1] if (out.stdout or out.stderr) else "?"
            _doctor_ok("codebase-memory-mcp", ver)
        except Exception:
            _doctor_ok("codebase-memory-mcp", cm)
    elif sys.platform == "win32":
        _doctor_fail(
            "codebase-memory-mcp",
            _t("main_doctor_cmcp_win", lang),
        )
        problems += 1
    else:
        print(_t("main_doctor_cmcp", lang).format(cmd=_CMCP_INSTALL_CMD))
        r = sp.run(_CMCP_INSTALL_CMD, shell=True)
        if r.returncode == 0 and shutil.which("codebase-memory-mcp"):
            _doctor_fixed("codebase-memory-mcp")
        else:
            _doctor_fail(
                "codebase-memory-mcp",
                _t("main_doctor_cmcp_fail", lang),
            )
            problems += 1

    # 6) LLM: local (llama.cpp) + OpenCode. Solo el proveedor ACTIVO suma
    # problemas; el otro se informa sin bloquear.
    from llm_config import active_config_file, is_free_model, resolve_llm

    try:
        _active = resolve_llm()["provider"]
    except Exception:
        _active = "local"
    has_llama_bin = shutil.which("llama-server") or shutil.which("llama-server.exe")
    if not has_llama_bin and sys.platform == "darwin" and shutil.which("brew") and _active == "local":
        print(_t("main_doctor_brew", lang))
        r = sp.run(["brew", "install", "llama.cpp"], capture_output=True, text=True, timeout=900)
        if r.returncode == 0 and shutil.which("llama-server"):
            _doctor_fixed("llama.cpp (llama-server)", _t("main_doctor_brew_ok", lang))
            has_llama_bin = shutil.which("llama-server") or shutil.which("llama-server.exe")
        else:
            tail = ((r.stderr or r.stdout) or "").strip()[-300:]
            print(_t("main_doctor_brew_fail", lang).format(tail=tail))
    alive, base = _llama_server_alive()
    if has_llama_bin:
        _doctor_ok("llama.cpp (llama-server)", shutil.which("llama-server"))
    elif _active == "local":
        hint = {
            "darwin": _t("main_doctor_hint_darwin", lang),
            "windows": _t("main_doctor_hint_win", lang),
        }.get(sys.platform, _t("main_doctor_hint_other", lang))
        _doctor_fail("llama.cpp (llama-server)", hint)
        problems += 1
    else:
        print(_t("main_doctor_no_llama", lang))
    if alive:
        _doctor_ok(_t("main_doctor_local_name", lang), base)
    elif _active == "local":
        _doctor_fail(
            _t("main_doctor_local_name", lang),
            _t("main_doctor_local_down", lang).format(base=base),
        )
        _doctor_model_advice()
        problems += 1
    else:
        print(_t("main_doctor_no_local", lang).format(base=base))

    # 6b) OpenCode Zen: key presente (sin mostrarla) + conexión + conteo 🆓/💰.
    _resolved_oc = resolve_llm(provider_override="opencode")
    if _resolved_oc["api_key"]:
        _doctor_ok("OpenCode API key", _t("main_doctor_oc_key", lang).format(path=active_config_file(), model=_resolved_oc['model']))
        try:
            from llm_config import fetch_opencode_models

            _models = fetch_opencode_models(_resolved_oc["api_key"], _resolved_oc["base_url"])
            _free = sum(1 for m in _models if is_free_model(m) is True)
            _doctor_ok("OpenCode Zen", _t("main_doctor_oc_models", lang).format(n=len(_models), free=_free))
        except Exception as e:
            _msg = str(e)
            if "401" in _msg or "Unauthorized" in _msg:
                _doctor_fail("OpenCode Zen", _t("main_doctor_oc_401", lang))
            else:
                _doctor_fail("OpenCode Zen", _t("main_doctor_oc_list_fail", lang).format(tail=_msg[-200:]))
            if _active == "opencode":
                problems += 1
    elif _active == "opencode":
        _doctor_fail("OpenCode API key", _t("main_doctor_oc_missing", lang))
        problems += 1
    else:
        print(_t("main_doctor_no_key", lang))

    # 7) Checkout del harness: hash visible + estado (sucio/atrás).
    # E2E: crash por mezcla de versiones tras un update (session.py nuevo +
    # tool_dedupe viejo en el mismo proceso). El hash también va en cada
    # turno ([Ejecución] … harness X) para que salga en los transcripts.
    repo = _locate_install_repo()
    # Checkout del harness: mensaje bilingüe (main_doctor_no_checkout).
    if repo is None:
        print(_t("main_doctor_no_checkout", lang))
    else:
        head = sp.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        cur = (head.stdout or "").strip() or "?"
        br = sp.run(
            ["git", "-C", str(repo), "branch", "--show-current"],
            capture_output=True, text=True, timeout=10,
        )
        branch = (br.stdout or "").strip() or "?"
        st = sp.run(
            ["git", "-C", str(repo), "status", "--porcelain",
             "--untracked-files=no"],
            capture_output=True, text=True, timeout=10,
        )
        dirty = bool((st.stdout or "").strip())
        _doctor_ok(
            _t("main_doctor_checkout", lang),
            _t("main_doctor_checkout_cur", lang).format(cur=cur, branch=branch) + (_t("main_doctor_checkout_dirty", lang) if dirty else ""),
        )
        if dirty:
            print(_t("main_doctor_dirty_warn", lang))
        try:
            sp.run(
                ["git", "-C", str(repo), "fetch", "-q", "origin"],
                capture_output=True, timeout=20,
            )
            behind = sp.run(
                ["git", "-C", str(repo), "rev-list", "--count", "HEAD..@{u}"],
                capture_output=True, text=True, timeout=10,
            )
            n = int((behind.stdout or "0").strip() or 0)
            if n > 0:
                print(_t("main_doctor_behind", lang).format(n=n))
        except Exception:
            pass

    # Resumen
    print()
    if problems == 0:
        print(_t("main_doctor_done", lang))
    else:
        print(_t("main_doctor_pending", lang).format(n=problems))

    shim = shutil.which("agent-devs")
    if shim:
        print(_t("main_doctor_use1", lang))
        print(_t("main_doctor_use2", lang))
    else:
        print(_t("main_doctor_noshim", lang))
        print(_t("main_doctor_shim1", lang))
        print(_t("main_doctor_shim2", lang))
    return 0 if problems == 0 else 1


def main():
    parser = argparse.ArgumentParser(description="AgentDevs — agente de desarrollo con LLM local")
    parser.add_argument("repo", nargs="?", help="Ruta del repositorio (default: directorio actual)")
    parser.add_argument("--analyze", metavar="REPO", help="Genera y guarda el análisis del repo")
    parser.add_argument("--no-analysis", action="store_true",
                        help="Salta el análisis LLM al arrancar (usa caché o resumen determinista, ~1s)")
    parser.add_argument("--list", action="store_true", help="Lista los análisis guardados")
    parser.add_argument("--doctor", action="store_true",
                        help="Verifica el entorno (deps, git, MCP, LLM) e instala lo que falte")
    parser.add_argument("--update", action="store_true",
                        help="Actualiza esta instalación: git pull + reinstall editable")
    parser.add_argument("--provider", choices=("local", "opencode"), default=None,
                        help="Proveedor LLM (default: el de tu config o 'local')")
    parser.add_argument("--model", default=None,
                        help="Modelo a usar (default: el del config; en opencode ej. muse-spark-1.3)")
    parser.add_argument("--reasoning", choices=("low", "medium", "high"), default=None,
                        help="Nivel de razonamiento para modelos Responses (muse-spark-*, gpt-*, grok-*)")
    parser.add_argument("--language", choices=("es", "en"), default=None,
                        help="Idioma UI + respuestas (default: detecta $LANG, fallback en)")
    parser.add_argument("--list-models", action="store_true",
                        help="Lista los modelos de OpenCode (🆓 gratis vs 💰 pago) y sale")
    parser.add_argument("--setup-opencode", action="store_true",
                        help="Guarda tu API key de OpenCode en tu config (permiso 600)")
    parser.add_argument("--show-config", action="store_true",
                        help="Muestra la config efectiva (key tapada) y sale")
    args = parser.parse_args()

    if args.doctor:
        return run_doctor()
    if args.update:
        return run_update()
    if args.setup_opencode:
        return run_setup_opencode()
    if args.show_config:
        return run_show_config()
    if args.list_models:
        return run_list_models()
    if args.list:
        do_list()
        return
    if args.analyze:
        try:
            do_analyze(_make_llm(max_tokens=1024, temperature=0.4,
                                 provider=args.provider, model=args.model,
                                 reasoning=args.reasoning), args.analyze)
        except _UseLocalFallback:
            # _ensure_analysis ya anunció el motivo del fallback.
            do_analyze(_make_llm(max_tokens=1024, temperature=0.4,
                                 provider="local"), args.analyze)
        return

    # Normalizar a ABSOLUTO siempre: 'agent-devs .' dejaba repo_path='.'
    # relativo → cm__index_repository indexaba root_path='.' y CORROMPÍA el
    # store del knowledge graph (ERROR store.corrupt table=projects
    # bad_root_path=. en la máquina del otro usuario).
    repo_path = str(Path(args.repo or os.getcwd()).expanduser().resolve()).strip()

    # Fail-fast según proveedor (evita el traceback de httpx/openai).
    # Si la nube está mal configurada pero hay llama vivo, se sigue en local
    # (el config sigue en opencode; es solo esta sesión). Solo se bloquea si
    # NINGUNO de los dos responde.
    from llm_config import needs_messages_api, resolve_llm

    _resolved = resolve_llm(provider_override=args.provider, model_override=args.model,
                            reasoning_override=args.reasoning)
    from core.lang import resolve_language as _rl_main
    from core.lang import t as _t_main
    _main_lang = _rl_main()
    _eff_provider, _eff_model, _eff_reasoning = args.provider, args.model, args.reasoning
    if _resolved["provider"] == "opencode":
        _fb_reason = None
        if not _resolved["api_key"]:
            _fb_reason = _t_main("main_fb_no_key", _main_lang)
        elif needs_messages_api(_resolved["model"]):
            _fb_reason = _t_main("main_fb_no_support", _main_lang).format(model=_resolved['model'])
        if _fb_reason is not None:
            _fb = _local_fallback_resolved()
            if _fb is not None:
                _announce_opencode_fallback(_fb_reason)
                _resolved = _fb
                _eff_provider, _eff_model, _eff_reasoning = "local", None, None
            else:
                if not _resolved["api_key"]:
                    _opencode_missing_exit()
                else:
                    _messages_api_exit(_resolved["model"])
    else:
        alive, _ = _llama_server_alive(timeout=1.5)
        if not alive:
            _llama_down_exit()

    def _build_analysis_llm() -> LocalLLM:
        return _make_llm(max_tokens=1024, temperature=0.4,
                         provider=_eff_provider, model=_eff_model,
                         reasoning=_eff_reasoning)

    def _build_session_llm() -> LocalLLM:
        return _make_llm(provider=_eff_provider, model=_eff_model,
                         reasoning=_eff_reasoning)

    def _switch_effective_to_local() -> None:
        nonlocal _resolved, _eff_provider, _eff_model, _eff_reasoning
        _fb = _local_fallback_resolved()
        if _fb is not None:
            _resolved = _fb
        else:
            _resolved = resolve_llm(provider_override="local")
        _eff_provider, _eff_model, _eff_reasoning = "local", None, None

    if args.language:
        try:
            from core.lang import set_language

            set_language(args.language)
        except ValueError as e:
            print(f"❌ {e}")
            return 1

    reset_turn_usage()
    try:
        if args.no_analysis:
            print(_t_main("main_no_analysis", _main_lang))
            cached = deterministic_analysis(repo_path)
        else:
            try:
                cached = _ensure_analysis(
                    _build_analysis_llm(),
                    repo_path,
                    status=_t_main("main_analyze_long", _main_lang),
                )
            except _UseLocalFallback:
                # _ensure_analysis ya anunció el motivo.
                _switch_effective_to_local()
                cached = _ensure_analysis(
                    _build_analysis_llm(),
                    repo_path,
                    status=_t_main("main_analyze_long", _main_lang),
                )
    except SystemExit:
        raise
    except BaseException as e:
        # Fallback tardío: el error no se clasificó adentro pero igual es un
        # fallo de la nube y hay local vivo → reintentar una vez en local.
        if _resolved.get("provider") == "opencode" and _is_opencode_failure(e):
            if _local_fallback_resolved() is not None:
                _announce_opencode_fallback(_describe_opencode_failure(e))
                _switch_effective_to_local()
                try:
                    if args.no_analysis:
                        cached = deterministic_analysis(repo_path)
                    else:
                        cached = _ensure_analysis(
                            _build_analysis_llm(),
                            repo_path,
                            status=_t_main("main_analyze_long", _main_lang),
                        )
                except _UseLocalFallback:
                    _switch_effective_to_local()
                    cached = _ensure_analysis(
                        _build_analysis_llm(),
                        repo_path,
                        status=_t_main("main_analyze_long", _main_lang),
                    )
            else:
                _both_down_exit(_describe_opencode_failure(e))
        if _is_llama_connection_error(e):
            if _resolved.get("provider") == "opencode":
                _opencode_down_exit(e)
            else:
                _llama_down_exit()
        else:
            from llm_wrapper import is_protocol_error

            if _resolved.get("provider") == "opencode" and is_protocol_error(e):
                _opencode_down_exit(e)
        raise

    session = Session(_build_session_llm(),
                      repo_path, cached_analysis=_format_cached_context(cached))
    try:
        session.start()
    except BaseException as e:
        if _resolved.get("provider") == "opencode" and _is_opencode_failure(e):
            if _local_fallback_resolved() is not None:
                _announce_opencode_fallback(_describe_opencode_failure(e))
                _switch_effective_to_local()
                # Recrear la sesión en local (budgets/ safe, sin persistir el
                # config: la nube sigue siendo la preferida para la próxima).
                session = Session(_build_session_llm(),
                                  repo_path, cached_analysis=_format_cached_context(cached))
                session.start()
            else:
                _both_down_exit(_describe_opencode_failure(e))
        else:
            if _is_llama_connection_error(e):
                if _resolved.get("provider") == "opencode":
                    _opencode_down_exit(e)
                else:
                    _llama_down_exit()
            else:
                from llm_wrapper import is_protocol_error

                if _resolved.get("provider") == "opencode" and is_protocol_error(e):
                    _opencode_down_exit(e)
            raise

    # TUI siempre que haya terminal interactiva. Sin tty (pipe/script) el
    # fallback al input simple evita que Textual reviente — no es un modo
    # soportado, solo una red de seguridad.
    if sys.stdin.isatty():
        run_fullscreen(session)
        return

    from core.lang import t as _t_cli
    _cli_lang = getattr(session, "_lang", "es")
    print_welcome(
        repo_path, _resolved["model"], _resolved["base_url"], LLM_TEMPERATURE,
        (len(ALL_TOOLS), session._mcp_count),
        branch=session.get_status().get("branch", ""),
        lang=_cli_lang,
    )
    _warn_dirty_repo(repo_path)

    if cached.get("analysis"):
        print(_t_cli("main_cached_found", _cli_lang))
    print(_t_cli("main_what_next", _cli_lang))
    # No correr un turn LLM inicial: gastaba tokens y confundía al modelo 4B.

    try:
        while True:
            try:
                user_input = get_user_input(session.get_status())
            except (EOFError, KeyboardInterrupt):
                print("\n" + _t_cli("main_bye", getattr(session, "_lang", "es")))
                break
            stripped = normalize(user_input)
            if stripped in ("exit", "quit", "salir", "q"):
                print(_t_cli("main_bye", getattr(session, "_lang", "es")))
                break
            if not user_input.strip():
                continue
            # Selección pendiente de untracked (/commit lista + "1 3"/"todos").
            pick_msg = session.try_commit_pick(user_input)
            if pick_msg is not None:
                console.print(pick_msg + "\n")
                continue
            kind, payload = interpret_slash(user_input)
            if kind == "help":
                console.print("[bold]Comandos:[/bold]\n" + format_help(lang=getattr(session, "_lang", "es")) + "\n")
                continue
            if kind == "hint":
                matches = payload
                if matches:
                    console.print("[yellow]¿Quisiste decir?[/yellow]\n" + format_help(matches, lang=getattr(session, "_lang", "es")) + "\n")
                else:
                    console.print("[yellow]Comando desconocido. Disponibles:[/yellow]\n" + format_help(lang=getattr(session, "_lang", "es")) + "\n")
                continue
            if kind == "run" and payload[0] == "/new":
                session.reset()
                console.print(_t_cli("main_new_simple", getattr(session, "_lang", "es")))
                console.print(f"[dim]Session ID: {session.session_id}[/dim]\n")
                continue
            if kind == "run" and payload[0] == "/autoapprove":
                msg = session.toggle_auto_approve(payload[1])
                if msg.startswith("✅"):
                    console.print(f"[green]{msg}[/green]\n")
                else:
                    console.print(f"[red]{msg}[/red]\n")
                continue
            if kind == "run" and payload[0] == "/verify":
                from orchestration.session import run_commit_verification

                console.print(_t_cli("main_verifying", getattr(session, "_lang", "es")))
                captured: dict = {}
                passed, report = run_commit_verification(
                    session.repo_path, reuse=getattr(session, "_verify_results", None),
                    capture=captured,
                )
                console.print(report + "\n")
                for area, tail in captured.get("details", {}).items():
                    console.print(_t_cli("main_verify_tail", getattr(session, "_lang", "es")).format(area=area, tail=tail) + "\n")
                session.note_verify_result(passed, report, captured.get("details"))
                console.print(
                    (_t_cli("main_all_green", getattr(session, "_lang", "es")) + "\n") if passed
                    else (_t_cli("main_reds_above", getattr(session, "_lang", "es")) + "\n")
                )
                continue
            if kind == "run" and payload[0] == "/commit":
                session.slash_commit(payload[1])
                continue
            if kind == "run" and payload[0] == "/tasks-pool":
                session.slash_task_pool(payload[1])
                continue
            if kind == "run" and payload[0] == "/push":
                session.slash_push(payload[1])
                continue
            if kind == "run" and payload[0] == "/pr":
                session.slash_pr(payload[1])
                continue
            if kind == "run" and payload[0] == "/model":
                console.print(session.slash_model(payload[1]) + "\n")
                continue
            if kind == "run" and payload[0] == "/language":
                console.print(session.slash_language(payload[1]) + "\n")
                continue
            if kind == "run" and payload[0] == "/log":
                console.print(_t_cli("main_log_tui_only", getattr(session, "_lang", "es")))
                continue
            if kind == "run" and payload[0] == "/help":
                console.print("[bold]Comandos:[/bold]\n" + format_help(lang=getattr(session, "_lang", "es")) + "\n")
                continue
            if kind == "run" and payload[0] == "/resume":
                ok, msg, data = session.resume_session(payload[1])
                console.print(f"[green]{msg}[/green]\n" if ok else f"[red]{msg}[/red]\n")
                if ok:
                    for t in data:
                        user = (t.get("user_message") or "")[:300]
                        asst = (t.get("assistant_message") or "")[:300]
                        console.print(_t_cli("main_vos_line2", getattr(session, "_lang", "es")).format(user=user))
                        if asst:
                            console.print(f"  [dim]{asst}[/dim]")
                        console.print()
                continue
            if kind == "run" and payload[0] == "/compact":
                session.force_summarize()
                console.print(_t_cli("main_compact_done", getattr(session, "_lang", "es")).format(pct=f"{session.context_usage_pct():.0f}") + "\n")
                continue
            if kind == "run" and payload[0] == "/history":
                turns = session.get_recent_history(limit=10)
                if not turns:
                    console.print(_t_cli("main_no_prev_saved", getattr(session, "_lang", "es")))
                else:
                    console.print(_t_cli("main_last_turns", getattr(session, "_lang", "es")).format(n=len(turns)))
                    for t in turns:
                        role = t.get("role", "-")
                        user = (t.get("user_message") or "")[:80]
                        asst = (t.get("assistant_message") or "")[:80]
                        tokens = t.get("tokens_used", 0)
                        sid = t.get("session_id", "-")[:8]
                        console.print(_t_cli("main_user_simple", getattr(session, "_lang", "es")).format(role=role, sid=sid, tokens=tokens))
                        console.print(f"    {user}")
                        if asst:
                            console.print(_t_cli("main_agent_prefix", getattr(session, "_lang", "es")))
                            console.print(f"    [dim]{asst}[/dim]")
                        console.print()
                    console.print(_t_cli("main_resume_hint", getattr(session, "_lang", "es")) + "\n")
                continue
            try:
                from display.status_bar import run_turn_with_sticky_bar
                run_turn_with_sticky_bar(session, user_input)
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as e:
                from llm_wrapper import is_protocol_error

                if _is_llama_connection_error(e) or (
                    _resolved["provider"] == "opencode" and is_protocol_error(e)
                ):
                    if _resolved["provider"] == "opencode":
                        console.print(
                            _t_cli("main_opencode_fail_title", getattr(session, "_lang", "es"))
                            + _t_cli("main_opencode_fail_hint", getattr(session, "_lang", "es"))
                        )
                    else:
                        console.print(
                            _t_cli("main_llama_down_title", getattr(session, "_lang", "es")).format(base=LLM_BASE_URL)
                            + _t_cli("main_llama_hint", getattr(session, "_lang", "es"))
                            + "[dim]     llama-server -hf bartowski/InternScience_Agents-A1-4B-GGUF:Q4_K_M --port 8080[/dim]\n"
                            + _t_cli("main_llama_cloud", getattr(session, "_lang", "es"))
                            + _t_cli("main_llama_doctor", getattr(session, "_lang", "es"))
                        )
                    continue
                raise
    finally:
        session.close()


if __name__ == "__main__":
    main()
