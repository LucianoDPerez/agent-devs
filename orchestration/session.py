from __future__ import annotations

import asyncio
import subprocess
import time
import uuid
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from cache import load_recent_turns, save_turn
from core.roles import Role, role_for_intent
from display.console import console, print_role_switch, print_turn_summary, stream_agent_turn
from display.esc_watcher import EscWatcher
from llm_wrapper import LocalLLM, get_usage, reset_turn_usage
from orchestration.agent_builder import build_agent, init_mcp
from orchestration.router import classify_intent

_ROLE_LABELS = {
    Role.ANALYZE: "🔍 Análisis", Role.PLAN: "📋 Planificación",
    Role.EXECUTE: "🛠️  Ejecución", Role.REVIEW: "🔎 Revisión",
    Role.CHAT: "💬 Charla",
}

# Context window: llama-server -c 32000. 90% = 28800 tokens.
_CONTEXT_LIMIT = 28800
_SUMMARY_THRESHOLD = 0.90
_WARNING_THRESHOLD = 0.85


def _git_branch(repo_path: str) -> str:
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=repo_path, capture_output=True, text=True, timeout=3,
        )
        return result.stdout.strip() or "(detached)"
    except Exception:
        return "-"


def _estimate_tokens(messages: list) -> int:
    """Estimación rápida: ~4 chars por token."""
    total = 0
    for m in messages:
        content = m.content if hasattr(m, "content") else str(m)
        total += len(str(content)) // 4
    return total


def _generate_summary(llm, messages: list) -> str:
    """Genera un resumen del historial usando el LLM."""
    history_text = "\n\n".join(
        f"{'Usuario' if isinstance(m, HumanMessage) else 'Asistente'}: {m.content[:500]}"
        for m in messages
        if hasattr(m, "content") and m.content
    )
    prompt = f"""Resumí la siguiente conversación de forma concisa (máx 200 palabras).
Incluí: qué tareas se hicieron, qué archivos se tocaron, decisiones técnicas, y tickets de Jira mencionados.

Conversación:
{history_text}

Resumen:"""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = llm._generate([HumanMessage(prompt)])
        return result.generations[0].message.content.strip()
    except Exception:
        return "Sesión previa resumida automáticamente."
    finally:
        loop.close()


class Session:
    """Sesión con memoria, persistencia SQLite y gestión de contexto.

    - Acumula el historial de mensajes y lo pasa al agente en cada turno
    - Guarda cada turno completo en SQLite (session_history)
    - Genera un LLM summary cuando el contexto llega al 90%
    - /new resetea el historial manteniendo el análisis cacheado
    """

    def __init__(self, llm, repo_path: str, cached_analysis: str = ""):
        self.llm = llm
        self.repo_path = repo_path
        self.cached_analysis = cached_analysis

        self.current_role: Role = Role.ANALYZE
        self.agent: Any = None
        self._tools: list = []
        self._mcp_count: int = 0
        self._local_count: int = 0
        self._session_time: float = 0.0
        self.session_id: str = str(uuid.uuid4())[:8]

        self._messages: list = []  # historial de la sesión actual
        self._last_response: str = ""  # respuesta del último turno

    def start(self) -> str:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self._tools, self._mcp_count = loop.run_until_complete(init_mcp())
            self.current_role = Role.ANALYZE
            self._load_previous_sessions()
            self.agent, self._local_count = loop.run_until_complete(
                build_agent(self.llm, Role.ANALYZE, self.repo_path, self.cached_analysis, self._tools)
            )
        finally:
            loop.close()
        return f"🛠️  Tools: {self._local_count} locales + {self._mcp_count} graph (cm__*)"

    def _load_previous_sessions(self):
        """Carga los últimos turnos de sesiones anteriores y los agrega al contexto.

        Se inyectan en cached_analysis (que va al system prompt) en vez de
        como SystemMessage separado, porque llama-server --jinja solo permite
        SystemMessage al inicio del conversation.
        """
        try:
            turns = load_recent_turns(self.repo_path, limit=5)
            if not turns:
                return
            history_lines = []
            for t in turns:
                user = (t.get("user_message") or "")[:120]
                asst = (t.get("assistant_message") or "")[:120]
                if user and asst:
                    history_lines.append(f"- Usuario: {user}\n  Agente: {asst}")
            if history_lines:
                history_text = "\n".join(history_lines)
                self.cached_analysis = (
                    (self.cached_analysis or "")
                    + f"\n\nHISTORIAL DE SESIONES ANTERIORES "
                    f"(usa esto para responder 'qué hicimos la última vez'):\n{history_text}"
                )
        except Exception:
            pass

    def reset(self):
        """Resetea el historial para una nueva sesión. Mantiene cache de repo."""
        self._messages = []
        self._last_response = ""
        self.session_id = str(uuid.uuid4())[:8]
        self._session_time = 0.0
        self.current_role = Role.ANALYZE
        self._load_previous_sessions()
        self._rebuild_agent(Role.ANALYZE)

    def _rebuild_agent(self, role: Role) -> bool:
        if self.agent is not None and role == self.current_role:
            return False
        self.current_role = role
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.agent, self._local_count = loop.run_until_complete(
                build_agent(self.llm, role, self.repo_path, self.cached_analysis, self._tools)
            )
        finally:
            loop.close()
        return True

    def _check_context(self) -> str | None:
        """Verifica el contexto. Devuelve warning o None."""
        estimated = _estimate_tokens(self._messages)
        pct = estimated / _CONTEXT_LIMIT
        if pct >= _SUMMARY_THRESHOLD:
            return "summary"
        if pct >= _WARNING_THRESHOLD:
            return "warning"
        return None

    def _maybe_summarize(self):
        """Si el contexto llega al 90%, genera un summary y reemplaza historial viejo."""
        ctx_status = self._check_context()
        if ctx_status != "summary":
            return

        console.print("\n[yellow]📦 Contexto al 90% — generando resumen del historial…[/yellow]")

        old_messages = self._messages[:-2]  # preservar últimos 2 mensajes
        recent = self._messages[-2:] if len(self._messages) >= 2 else self._messages

        summary = _generate_summary(self.llm, old_messages)
        self._messages = [
            SystemMessage(f"Resumen de la conversación previa:\n{summary}"),
            *recent,
        ]
        console.print("[green]✅ Resumen generado. Historial comprimido.[/green]\n")

    def run_turn(self, user_input: str, status: str | None = None) -> None:
        """Clasifica, cambia rol, ejecuta con historial, persiste en SQLite."""
        intent = classify_intent(self.llm, user_input)
        new_role = role_for_intent(intent)

        role_changed = self._rebuild_agent(new_role)
        role_label = _ROLE_LABELS.get(new_role, "")

        if role_changed and new_role != Role.CHAT:
            print_role_switch(role_label, self._local_count, self._mcp_count)

        if status:
            print(status, flush=True)

        # Acumular el mensaje del usuario en el historial
        self._messages.append(HumanMessage(user_input))

        reset_turn_usage()
        start = time.monotonic()
        config = {"configurable": {"thread_id": f"session-{id(self)}"}}

        # Pasar todo el historial al agente
        messages_for_agent = list(self._messages)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        task = loop.create_task(
            stream_agent_turn(self.agent, messages_for_agent, config)
        )

        # Watcher de ESC: permite interrumpir el streaming y volver al prompt.
        watcher = EscWatcher(
            cancel_cb=lambda: loop.call_soon_threadsafe(task.cancel)
        )
        watcher.start()

        interrupted = False
        interrupted_by_esc = False
        try:
            loop.run_until_complete(task)
            self._last_response = task.result() if not task.cancelled() else ""
        except KeyboardInterrupt:
            interrupted = True
            task.cancel()
            try:
                loop.run_until_complete(asyncio.wait_for(task, timeout=2.0))
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
        except asyncio.CancelledError:
            interrupted = True
            interrupted_by_esc = watcher.interrupted.is_set()
        except Exception as e:
            print(f"\n\n❌ Error en la iteración: {e}", flush=True)
        finally:
            watcher.stop()
            loop.close()

        elapsed = time.monotonic() - start
        self._session_time += elapsed

        # Capturar la respuesta del agente para persistir
        # El stream_agent_turn imprime pero no retorna el texto. Lo reconstruimos
        # del último AIMessage del historial del agente. Por simplicidad,
        # guardamos lo que sabemos: el user_input y un placeholder.
        usage = get_usage()
        turn_tokens = usage["turn"]["prompt"] + usage["turn"]["completion"]

        # Acumular respuesta en el historial (para que el agente la recuerde)
        # Como no tenemos el texto exacto del response, usamos un AIMessage placeholder
        # que el agente verá en próximos turnos como contexto.
        if not interrupted:
            self._messages.append(AIMessage(self._last_response or "(respuesta generada)"))

        # Persistir en SQLite
        try:
            save_turn(
                session_id=self.session_id,
                repo_path=self.repo_path,
                role=new_role.value,
                user_message=user_input,
                assistant_message=self._last_response or "",
                tokens_used=turn_tokens,
            )
        except Exception:
            pass

        print_turn_summary(
            elapsed,
            interrupted,
            self._session_time,
            interrupt_source="ESC" if interrupted_by_esc else None,
        )

        # Warning de contexto
        ctx_status = self._check_context()
        if ctx_status == "warning":
            pct = _estimate_tokens(self._messages) / _CONTEXT_LIMIT * 100
            console.print(f"\n[yellow]⚠️  Contexto al {pct:.0f}% — usá /new para empezar sesión nueva[/yellow]\n")
        elif ctx_status == "summary":
            self._maybe_summarize()

    def get_recent_history(self, limit: int = 5) -> list[dict]:
        """Devuelve los últimos N turnos del repo (de cualquier sesión)."""
        return load_recent_turns(self.repo_path, limit=limit)

    def get_status(self) -> dict:
        usage = get_usage()
        total = usage["session"]["prompt"] + usage["session"]["completion"]
        return {
            "branch": _git_branch(self.repo_path),
            "tokens": total,
            "role": _ROLE_LABELS.get(self.current_role, ""),
            "repo": self.repo_path,
            "tools": f"{self._local_count}+{self._mcp_count}",
            "session_id": self.session_id,
        }

    def close(self):
        pass