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


def _make_llm(max_tokens: int = LLM_MAX_TOKENS, temperature: float = LLM_TEMPERATURE) -> LocalLLM:
    return LocalLLM(
        base_url=LLM_BASE_URL,
        model_name=LLM_MODEL_NAME,
        temperature=temperature,
        max_tokens=max_tokens,
        api_key="not-needed",
    )


def _cached_analysis_valid(cached: dict) -> bool:
    """True si el summary cacheado es usable (no eco del prompt ni vacío)."""
    analysis = (cached.get("analysis") or "").strip()
    poison = (
        "resumen en español de 80-120 palabras",
        "qué hace el proyecto, arquitectura general y estructura de carpetas",
    )
    return not (any(p in analysis.lower() for p in poison) or len(analysis) < 40)


def _ensure_analysis(llm: LocalLLM, repo_path: str, status: str | None = None) -> dict:
    cached = load_analysis(repo_path)
    if cached:
        current = snapshot_hash(repo_path)
        if current and cached["snapshot_hash"] == current and _cached_analysis_valid(cached):
            return cached
        if not _cached_analysis_valid(cached):
            print("⚠️  Análisis cacheado inválido; se regenera.\n", flush=True)
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
                    f"[dim]📚 Análisis reutilizado ({changed} archivo(s) cambiado(s) "
                    f"— sin regenerar).[/dim]\n",
                    flush=True,
                )
                return load_analysis(repo_path) or cached
    # Chequeo rápido antes de esperar 420s de timeout si el server está caído
    alive, _ = _llama_server_alive(timeout=1.5)
    if not alive:
        _llama_down_exit()
    if status:
        print(status, flush=True)
    try:
        result = run_analysis(repo_path, llm, on_token=lambda tok: print(tok, end="", flush=True), timeout=ANALYSIS_LLM_TIMEOUT)
    except FileNotFoundError as e:
        print(f"❌ {e}")
        sys.exit(1)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as e:
        if _is_llama_connection_error(e):
            _llama_down_exit()
        raise
    save_analysis(repo_path, snapshot=result["snapshot"], language=result["language"],
                  tech_stack=result["tech_stack"], analysis=result["analysis"],
                  snapshot_files="\n".join(snapshot_entries(repo_path)))
    print("\n\n✅ Análisis guardado en el caché.")
    print(f"🌐 Lenguaje: {result['language']}")
    print(f"🧰 Stack: {result['tech_stack']}\n")
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
    reset_turn_usage()
    start = __import__('time').monotonic()
    _ensure_analysis(llm, repo_path, status=f"🔍 Analizando {repo_path} ...\n")
    elapsed = __import__('time').monotonic() - start
    usage = get_usage()
    print(f"\n⚡ {elapsed:.1f}s | {usage['turn']['prompt'] + usage['turn']['completion']:,} tokens")


def do_list():
    repos = list_repos()
    if not repos:
        print("📭 No hay análisis guardados. Usa: python main.py --analyze <repo>")
        return
    print(f"{'REPOSITORIO':<48} {'LENGUAJE':<13} {'STACK':<32} ACTUALIZADO")
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
        console.print(
            f"\n[yellow]⚠️  El repo tiene {changed} archivo(s) modificado(s) y "
            f"{untracked} sin trackear SIN COMMITEAR. Los lint/tests pueden "
            f"fallar por daño pre-existente (ajeno a tu tarea); si es así, "
            f"el agente puede gastar pasos de más.[/yellow]\n"
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
        stripped = text.strip().lower()
        if stripped in ("exit", "quit", "salir", "q"):
            console.print("[dim]👋 ¡Hasta luego![/dim]")
            tui.exit()
            return
        if not text.strip():
            return
        # ECO del prompt (también slash y sí/no: el input se limpia al enviar
        # y sin esto lo tipeado no aparece en el panel).
        from rich.markup import escape

        console.print()
        console.print("[bold dark_orange]🧑 vos ›[/bold dark_orange]")
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
            console.print("[bold]Comandos:[/bold]\n" + format_help())
            return
        elif kind == "hint":
            matches = payload
            if matches:
                console.print("[yellow]¿Quisiste decir?[/yellow]\n" + format_help(matches))
            else:
                console.print("[yellow]Comando desconocido. Disponibles:[/yellow]\n" + format_help())
            return
        elif payload[0] == "/new":
            session.reset()
            session._fullscreen = True  # reset() no debe apagar el modo
            tui.clear_pane()  # sesión nueva → panel nuevo
            console.print(f"[green]✅ Nueva sesión iniciada ({session.session_id}).[/green]")
            return
        elif payload[0] == "/compact":
            console.print("[yellow]📦 Compactando contexto (resumen del historial)…[/yellow]")
            session.force_summarize()
            console.print(f"[green]✅ Listo. Contexto ahora al {session.context_usage_pct():.0f}%.[/green]")
            return
        elif payload[0] == "/history":
            turns = session.get_recent_history(limit=10)
            if not turns:
                console.print("[dim]Sin turnos previos.[/dim]")
                return
            for t in turns:
                user = (t.get("user_message") or "")[:80]
                asst = (t.get("assistant_message") or "")[:80]
                console.print(
                    f"[bold]Usuario[/bold] [{t.get('role', '-')}] "
                    f"(id={t.get('session_id', '-')}, {t.get('tokens_used', 0)} tokens): {user}"
                )
                if asst:
                    console.print(f"  [dim]Agente: {asst}[/dim]")
            console.print("[dim]Retomá una con /resume <id>.[/dim]")
            return
        elif payload[0] == "/resume":
            ok, msg, data = session.resume_session(payload[1])
            console.print(f"[green]{msg}[/green]" if ok else f"[red]{msg}[/red]")
            if ok:
                for t in data:
                    user = (t.get("user_message") or "")[:300]
                    asst = (t.get("assistant_message") or "")[:300]
                    console.print(f"\n[bold]vos ›[/bold] {user}")
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

            console.print("[dim]🔍 Verificando (lint/tests/build)…[/dim]")
            captured: dict = {}
            passed, report = run_commit_verification(
                session.repo_path, reuse=getattr(session, "_verify_results", None),
                capture=captured,
            )
            console.print(report)
            for area, tail in captured.get("details", {}).items():
                console.print(f"[dim]--- {area} (cola) ---[/dim]\n{tail}")
            session.note_verify_result(passed, report, captured.get("details"))
            console.print(
                "[green]✅ Todo verde.[/green]" if passed
                else "[yellow]⛔ Hay rojos arriba — corregilos antes de commitear.[/yellow]"
            )
            return
        elif payload[0] == "/commit":
            session.slash_commit(payload[1])
            return
        elif payload[0] == "/push":
            session.slash_push(payload[1])
            return
        elif payload[0] == "/pr":
            session.slash_pr(payload[1])
            return
        elif payload[0] == "/help":
            console.print("[bold]Comandos:[/bold]\n" + format_help())
            return

        # Turno normal del agente (el eco ya se imprimió arriba).
        session.run_turn(text)

    tui.on_submit = on_submit
    tui.on_cancel = session.request_cancel

    # El header FIJO de la TUI ya muestra LLM/repo/modelo/tools — no duplicar
    # el panel de welcome en el scrollable. Solo el hint inicial.
    console.print("💡 Escribí qué querés hacer. [dim](ESC cancela turno · "
                  "rueda/scroll en el panel · / para comandos · commit manual al terminar)[/dim]\n")
    _warn_dirty_repo(session.repo_path)

    try:
        tui.run()
    except KeyboardInterrupt:
        console.print("\n[dim]👋 ¡Hasta luego![/dim]")
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
# las familias pasaron por el banco del harness — Qwen3 base NO: sin tool
# calling probado acá, no se recomienda). El doctor nunca descarga solo.
_MODEL_TIERS = (
    # (ram_min_gb, etiqueta, comando -hf, peso aprox, nota)
    (48, "64GB o más",
     "unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M", "~17GB",
     "lo mejor actual en benchmarks externos; acá probado al 100%: "
     "Qwen3.6-35B-A3B (7/7 en nuestro banco)"),
    (28, "32GB",
     "unsloth/gemma-4-12B-it-qat-GGUF:UD-Q4_K_XL", "~7GB",
     "probado en el banco small-llm"),
    (20, "24GB",
     "unsloth/Qwen3.5-9B-GGUF:Q4_K_M", "~5.5GB",
     "5/5 análisis + 1/2 ejecución en nuestro banco"),
    (0, "16GB",
     "bartowski/InternScience_Agents-A1-4B-GGUF:Q4_K_M", "~2.5GB",
     "probado en el banco (tiende a loopear; el harness lo contiene). "
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
    ram = _total_ram_gb()
    print("     📦 Bajá el modelo acorde a tu RAM (Q4_K_M en todos los casos):")
    tiers = list(_MODEL_TIERS)  # ordenados de mayor a menor RAM mínima
    for i, (ram_min, label, hf, size, note) in enumerate(tiers):
        ceiling = tiers[i - 1][0] if i > 0 else float("inf")
        mine = ram is not None and ram_min <= ram < ceiling
        mark = "  ← TU LIGA" if mine else ""
        print(f"     · {label}: llama-server -hf {hf} --port 8080  ({size}){mark}")
        print(f"       {note}")
    if ram is None:
        print("     (no pude detectar tu RAM — elegí por tamaño de tu máquina)")
    else:
        print(f"     (detecté ~{ram:.0f}GB de RAM)")


def _doctor_ok(name, detail=""):
    print(f"  ✅ {name}" + (f" — {detail}" if detail else ""))


def _doctor_fixed(name, detail=""):
    print(f"  🔧 {name} — INSTALADO AHORA" + (f" ({detail})" if detail else ""))


def _doctor_fail(name, detail):
    print(f"  ❌ {name} — {detail}")


def _pip_install(requirement: str) -> bool:
    import subprocess

    print(f"     ⏳ Instalando {requirement}…")
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", requirement],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(f"     ⚠️  pip falló: {(proc.stderr or proc.stdout).strip()[-300:]}")
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
    print(f"\n❌ llama.cpp está apagado — no se pudo conectar a {LLM_BASE_URL}", flush=True)
    print("   Encendelo antes de ejecutar agent-devs, por ejemplo:", flush=True)
    print("     llama-server -hf bartowski/InternScience_Agents-A1-4B-GGUF:Q4_K_M --port 8080", flush=True)
    print("   Si ya está corriendo en otro puerto/host, revisá config.py → LLM_BASE_URL", flush=True)
    print("   Verificá el estado con: agent-devs --doctor\n", flush=True)
    sys.exit(1)


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
    import subprocess as sp

    repo = _locate_install_repo()
    if repo is None:
        print("❌ No encontré la instalación (ni venv ni cwd con pyproject.toml).")
        print("   Re-corré el one-liner de instalación desde la carpeta que quieras.")
        return 1

    def git(*args):
        return sp.run(["git", "-C", str(repo), *args], capture_output=True, text=True)

    head_antes = git("rev-parse", "--short", "HEAD").stdout.strip()
    print(f"🔄 Actualizando AgentDevs en {repo} ({head_antes})…")

    r = git("pull", "--ff-only", "-q")
    if r.returncode != 0:
        print(f"❌ git pull falló: {(r.stderr or r.stdout).strip()}")
        print(f"   Si tocaste código en esa copia: git -C {repo} stash && repetí --update")
        return 1

    head_despues = git("rev-parse", "--short", "HEAD").stdout.strip()
    if head_antes == head_despues:
        print(f"✅ Ya estabas en la última versión ({head_despues}). Nada para instalar.")
        return 0
    print(f"⬆️  {head_antes} → {head_despues}")

    print("🔧 Refrescando instalación editable (+ deps nuevas si las hubo)…")
    r = sp.run([sys.executable, "-m", "pip", "install", "-q", "-e", str(repo)])
    if r.returncode != 0:
        print("❌ pip install -e . falló — revisá el output de pip arriba.")
        return 1
    print("✅ Actualizado. Corré agent-devs --doctor si querés verificar el entorno.")
    print("⚠️  Reiniciá cualquier sesión abierta de agent-devs: los módulos ya cargados no cambian solos.")
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

    print("🩺 AgentDevs doctor — verificando entorno…\n")
    problems = 0

    # 1) Python
    v = sys.version_info
    if (v.major, v.minor) >= (3, 10):
        _doctor_ok(f"Python {v.major}.{v.minor}.{v.micro}")
    else:
        _doctor_fail("Python", f"se requiere 3.10+, tenés {v.major}.{v.minor}")
        return 1

    # 2) venv (aviso, no bloquea)
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    if in_venv:
        _doctor_ok("Entorno virtual activo", sys.prefix)
    else:
        _doctor_fail(
            "Entorno virtual",
            "no estás en un venv — corré ./install.sh o creá uno "
            "(python3 -m venv .venv && source .venv/bin/activate)",
        )
        problems += 1

    # 3) git
    if shutil.which("git"):
        _doctor_ok("git")
    else:
        _doctor_fail("git", "instalalo con tu package manager (brew/apt/choco)")
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
                _doctor_fail(module, f"se instaló {requirement} pero el import sigue fallando")
                problems += 1
        else:
            _doctor_fail(module, f"no se pudo instalar {requirement}")
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
            "instalador automático no disponible en Windows — seguí "
            "https://github.com/DeusData/codebase-memory-mcp",
        )
        problems += 1
    else:
        print(f"     ⏳ Instalando codebase-memory-mcp…\n     $ {_CMCP_INSTALL_CMD}")
        r = sp.run(_CMCP_INSTALL_CMD, shell=True)
        if r.returncode == 0 and shutil.which("codebase-memory-mcp"):
            _doctor_fixed("codebase-memory-mcp")
        else:
            _doctor_fail(
                "codebase-memory-mcp",
                "el instalador falló — corrélo manualmente o seguí "
                "https://github.com/DeusData/codebase-memory-mcp (el harness "
                "funciona igual, solo sin knowledge graph)",
            )
            problems += 1

    # 6) llama.cpp: binario (auto-instala con brew en macOS) + server vivo
    has_llama_bin = shutil.which("llama-server") or shutil.which("llama-server.exe")
    if not has_llama_bin and sys.platform == "darwin" and shutil.which("brew"):
        print("     ⏳ llama-server no encontrado — instalando con brew…\n     $ brew install llama.cpp")
        r = sp.run(["brew", "install", "llama.cpp"], capture_output=True, text=True, timeout=900)
        if r.returncode == 0 and shutil.which("llama-server"):
            _doctor_fixed("llama.cpp (llama-server)", "instalado con brew")
            has_llama_bin = shutil.which("llama-server") or shutil.which("llama-server.exe")
        else:
            tail = ((r.stderr or r.stdout) or "").strip()[-300:]
            print(f"     ⚠️  brew falló: {tail}")
    alive, base = _llama_server_alive()
    if has_llama_bin:
        _doctor_ok("llama.cpp (llama-server)", shutil.which("llama-server"))
    else:
        hint = {
            "darwin": "brew install llama.cpp",
            "windows": "bajá el release de https://github.com/ggml-org/llama.cpp/releases y agregalo al PATH",
        }.get(sys.platform, "compilá con cmake o bajá un release de https://github.com/ggml-org/llama.cpp/releases")
        _doctor_fail("llama.cpp (llama-server)", hint)
        problems += 1
    if alive:
        _doctor_ok("Modelo corriendo", base)
    else:
        _doctor_fail(
            "Modelo corriendo",
            f"nadie responde en {base}. Bajá uno y levantalo antes de usar agent-devs.",
        )
        _doctor_model_advice()
        problems += 1

    # 7) Checkout del harness: hash visible + estado (sucio/atrás).
    # E2E: crash por mezcla de versiones tras un update (session.py nuevo +
    # tool_dedupe viejo en el mismo proceso). El hash también va en cada
    # turno ([Ejecución] … harness X) para que salga en los transcripts.
    repo = _locate_install_repo()
    if repo is None:
        print("  ⚠️  Checkout del harness: no lo ubiqué (¿instalación rota?)")
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
            "Checkout del harness",
            f"{cur} en {branch}" + (" (sucio: cambios locales sin commitear)" if dirty else ""),
        )
        if dirty:
            print("     ⚠️  Con cambios locales, un --update puede fallar o dejar mezcla vieja/nueva: commiteá o stash antes.")
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
                print(f"     ⚠️  Estás {n} commit(s) atrás del remoto: corré agent-devs --update Y reiniciá las sesiones abiertas.")
        except Exception:
            pass

    # Resumen
    print()
    if problems == 0:
        print("🎉 Todo listo. Usalo desde cualquier repositorio:\n")
    else:
        print(f"⚠️  {problems} problema(s) pendiente(s). Después de resolverlos:\n")

    shim = shutil.which("agent-devs")
    if shim:
        print("   cd /ruta/a/tu/proyecto && agent-devs .          # '.' = repo actual")
        print("   agent-devs /ruta/a/otro/repo                   # o ruta explícita")
    else:
        print("   El comando global 'agent-devs' no está en tu PATH todavía:")
        print("   • Desde este repo:   .venv/bin/agent-devs .   (mac/linux)")
        print("   • O activá el venv:  source .venv/bin/activate && agent-devs .")
    return 0 if problems == 0 else 1


def main():
    parser = argparse.ArgumentParser(description="AgentDevs — agente de desarrollo con LLM local")
    parser.add_argument("repo", nargs="?", help="Ruta del repositorio (default: directorio actual)")
    parser.add_argument("--analyze", metavar="REPO", help="Genera y guarda el análisis del repo")
    parser.add_argument("--no-analysis", action="store_true",
                        help="Salta el análisis LLM al arrancar (usa caché o resumen determinista, ~1s)")
    parser.add_argument("--list", action="store_true", help="Lista los análisis guardados")
    parser.add_argument("--doctor", action="store_true",
                        help="Verifica el entorno (deps, git, MCP, llama-server) e instala lo que falte")
    parser.add_argument("--update", action="store_true",
                        help="Actualiza esta instalación: git pull + reinstall editable")
    args = parser.parse_args()

    if args.doctor:
        return run_doctor()
    if args.update:
        return run_update()
    if args.list:
        do_list()
        return
    if args.analyze:
        do_analyze(_make_llm(max_tokens=1024, temperature=0.4), args.analyze)
        return

    # Normalizar a ABSOLUTO siempre: 'agent-devs .' dejaba repo_path='.'
    # relativo → cm__index_repository indexaba root_path='.' y CORROMPÍA el
    # store del knowledge graph (ERROR store.corrupt table=projects
    # bad_root_path=. en la máquina del otro usuario).
    repo_path = str(Path(args.repo or os.getcwd()).expanduser().resolve()).strip()

    # Fail-fast: si llama.cpp no responde, salir con mensaje amigable antes de
    # intentar cualquier llamada LLM (evita el traceback de httpx/openai).
    alive, _ = _llama_server_alive(timeout=1.5)
    if not alive:
        _llama_down_exit()

    reset_turn_usage()
    try:
        if args.no_analysis:
            print("📚 Modo --no-analysis: contexto determinista (~1s, sin LLM).")
            cached = deterministic_analysis(repo_path)
        else:
            cached = _ensure_analysis(
                _make_llm(max_tokens=1024, temperature=0.4),
                repo_path,
                status="🤔 Analizando el repositorio... (puede tardar 1-2 min). Ctrl+C para cancelar.",
            )
    except SystemExit:
        raise
    except BaseException as e:
        if _is_llama_connection_error(e):
            _llama_down_exit()
        raise

    session = Session(_make_llm(), repo_path, cached_analysis=_format_cached_context(cached))
    try:
        session.start()
    except BaseException as e:
        if _is_llama_connection_error(e):
            _llama_down_exit()
        raise

    # TUI siempre que haya terminal interactiva. Sin tty (pipe/script) el
    # fallback al input simple evita que Textual reviente — no es un modo
    # soportado, solo una red de seguridad.
    if sys.stdin.isatty():
        run_fullscreen(session)
        return

    print_welcome(
        repo_path, LLM_MODEL_NAME, LLM_BASE_URL, LLM_TEMPERATURE,
        (len(ALL_TOOLS), session._mcp_count),
        branch=session.get_status().get("branch", ""),
    )
    _warn_dirty_repo(repo_path)

    if cached.get("analysis"):
        print("📚 Análisis cacheado encontrado. No se re-explora.")
    print("💡 Decime qué querés hacer (implementá, planificá, revisá, analizá…).\n")
    # No correr un turn LLM inicial: gastaba tokens y confundía al modelo 4B.

    try:
        while True:
            try:
                user_input = get_user_input(session.get_status())
            except (EOFError, KeyboardInterrupt):
                print("\n👋 ¡Hasta luego!")
                break
            stripped = normalize(user_input)
            if stripped in ("exit", "quit", "salir", "q"):
                print("👋 ¡Hasta luego!")
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
                console.print("[bold]Comandos:[/bold]\n" + format_help() + "\n")
                continue
            if kind == "hint":
                matches = payload
                if matches:
                    console.print("[yellow]¿Quisiste decir?[/yellow]\n" + format_help(matches) + "\n")
                else:
                    console.print("[yellow]Comando desconocido. Disponibles:[/yellow]\n" + format_help() + "\n")
                continue
            if kind == "run" and payload[0] == "/new":
                session.reset()
                console.print("[green]✅ Nueva sesión iniciada. Historial reseteado.[/green]")
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

                console.print("[dim]🔍 Verificando (lint/tests/build)…[/dim]")
                captured: dict = {}
                passed, report = run_commit_verification(
                    session.repo_path, reuse=getattr(session, "_verify_results", None),
                    capture=captured,
                )
                console.print(report + "\n")
                for area, tail in captured.get("details", {}).items():
                    console.print(f"[dim]--- {area} (cola) ---[/dim]\n{tail}\n")
                session.note_verify_result(passed, report, captured.get("details"))
                console.print(
                    "[green]✅ Todo verde.[/green]\n" if passed
                    else "[yellow]⛔ Hay rojos arriba — corregilos antes de commitear.[/yellow]\n"
                )
                continue
            if kind == "run" and payload[0] == "/commit":
                session.slash_commit(payload[1])
                continue
            if kind == "run" and payload[0] == "/push":
                session.slash_push(payload[1])
                continue
            if kind == "run" and payload[0] == "/pr":
                session.slash_pr(payload[1])
                continue
            if kind == "run" and payload[0] == "/help":
                console.print("[bold]Comandos:[/bold]\n" + format_help() + "\n")
                continue
            if kind == "run" and payload[0] == "/resume":
                ok, msg, data = session.resume_session(payload[1])
                console.print(f"[green]{msg}[/green]\n" if ok else f"[red]{msg}[/red]\n")
                if ok:
                    for t in data:
                        user = (t.get("user_message") or "")[:300]
                        asst = (t.get("assistant_message") or "")[:300]
                        console.print(f"  [bold]vos ›[/bold] {user}")
                        if asst:
                            console.print(f"  [dim]{asst}[/dim]")
                        console.print()
                continue
            if kind == "run" and payload[0] == "/compact":
                session.force_summarize()
                console.print(f"[green]✅ Listo. Contexto ahora al {session.context_usage_pct():.0f}%.[/green]\n")
                continue
            if kind == "run" and payload[0] == "/history":
                turns = session.get_recent_history(limit=10)
                if not turns:
                    console.print("[dim]No hay turnos previos guardados.[/dim]\n")
                else:
                    console.print(f"[bold]Últimos {len(turns)} turnos:[/bold]\n")
                    for t in turns:
                        role = t.get("role", "-")
                        user = (t.get("user_message") or "")[:80]
                        asst = (t.get("assistant_message") or "")[:80]
                        tokens = t.get("tokens_used", 0)
                        sid = t.get("session_id", "-")[:8]
                        console.print(f"  [bold]Usuario[/bold] [{role}] (sid={sid}, {tokens} tokens):")
                        console.print(f"    {user}")
                        if asst:
                            console.print("  [dim]Agente:[/dim]")
                            console.print(f"    [dim]{asst}[/dim]")
                        console.print()
                    console.print("[dim]Retomá una con /resume <id>.[/dim]\n")
                continue
            try:
                from display.status_bar import run_turn_with_sticky_bar
                run_turn_with_sticky_bar(session, user_input)
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as e:
                if _is_llama_connection_error(e):
                    console.print(
                        f"\n[red]❌ llama.cpp está apagado — no se pudo conectar a {LLM_BASE_URL}[/red]\n"
                        "[yellow]   Encendelo antes de seguir, por ejemplo:[/yellow]\n"
                        "[dim]     llama-server -hf bartowski/InternScience_Agents-A1-4B-GGUF:Q4_K_M --port 8080[/dim]\n"
                        "[dim]   Verificá con: agent-devs --doctor[/dim]\n"
                    )
                    continue
                raise
    finally:
        session.close()


if __name__ == "__main__":
    main()
