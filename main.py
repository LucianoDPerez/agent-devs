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

from cache import list_repos, load_analysis, snapshot_hash, save_analysis
from config import LLM_BASE_URL, LLM_MAX_TOKENS, LLM_MODEL_NAME, LLM_TEMPERATURE
from llm_wrapper import LocalLLM, get_usage, reset_turn_usage
from orchestration.session import Session
from analyzer import run_analysis
from display.console import console, print_welcome
from display.tui import get_user_input

warnings.filterwarnings("ignore", category=DeprecationWarning)


def _make_llm(max_tokens: int = LLM_MAX_TOKENS, temperature: float = LLM_TEMPERATURE) -> LocalLLM:
    return LocalLLM(
        base_url=LLM_BASE_URL,
        model_name=LLM_MODEL_NAME,
        temperature=temperature,
        max_tokens=max_tokens,
        api_key="not-needed",
    )


def _ensure_analysis(llm: LocalLLM, repo_path: str, status: str | None = None) -> dict:
    cached = load_analysis(repo_path)
    if cached:
        current = snapshot_hash(repo_path)
        analysis = (cached.get("analysis") or "").strip()
        poison = (
            "resumen en español de 80-120 palabras",
            "qué hace el proyecto, arquitectura general y estructura de carpetas",
        )
        is_poison = any(p in analysis.lower() for p in poison) or len(analysis) < 40
        if current and cached["snapshot_hash"] == current and not is_poison:
            return cached
        if is_poison:
            print("⚠️  Análisis cacheado inválido; se regenera.\n", flush=True)
    if status:
        print(status, flush=True)
    try:
        result = run_analysis(repo_path, llm, on_token=lambda tok: print(tok, end="", flush=True), timeout=180)
    except FileNotFoundError as e:
        print(f"❌ {e}")
        sys.exit(1)
    save_analysis(repo_path, snapshot=result["snapshot"], language=result["language"],
                  tech_stack=result["tech_stack"], analysis=result["analysis"])
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


def main():
    parser = argparse.ArgumentParser(description="AgentDevs — agente de desarrollo con LLM local")
    parser.add_argument("repo", nargs="?", help="Ruta del repositorio (default: directorio actual)")
    parser.add_argument("--analyze", metavar="REPO", help="Genera y guarda el análisis del repo")
    parser.add_argument("--list", action="store_true", help="Lista los análisis guardados")
    args = parser.parse_args()

    if args.list:
        do_list()
        return
    if args.analyze:
        do_analyze(_make_llm(max_tokens=1024, temperature=0.4), args.analyze)
        return

    repo_path = (args.repo or os.getcwd()).strip()

    reset_turn_usage()
    cached = _ensure_analysis(
        _make_llm(max_tokens=1024, temperature=0.4),
        repo_path,
        status="🤔 Analizando el repositorio... (puede tardar 1-2 min). Ctrl+C para cancelar.",
    )

    session = Session(_make_llm(), repo_path, cached_analysis=_format_cached_context(cached))
    session.start()
    print_welcome(repo_path, LLM_MODEL_NAME, LLM_BASE_URL, LLM_TEMPERATURE, (session._local_count, session._mcp_count))

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
            stripped = user_input.strip().lower()
            if stripped in ("exit", "quit", "salir", "q"):
                print("👋 ¡Hasta luego!")
                break
            if not user_input.strip():
                continue
            if stripped == "/new":
                session.reset()
                console.print("[green]✅ Nueva sesión iniciada. Historial reseteado.[/green]")
                console.print(f"[dim]Session ID: {session.session_id}[/dim]\n")
                continue
            if stripped == "/history":
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
                            console.print(f"  [dim]Agente:[/dim]")
                            console.print(f"    [dim]{asst}[/dim]")
                        console.print()
                    console.print()
                continue
            session.run_turn(user_input)
    finally:
        session.close()


if __name__ == "__main__":
    main()