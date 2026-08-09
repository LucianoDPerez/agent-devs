from __future__ import annotations

import asyncio
import re
import subprocess
import time
import uuid
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from cache import load_recent_turns, save_turn
from config import (
    AGENT_RECURSION_LIMIT,
    ANALYZE_EXPLORE_BUDGET,
    ANALYZE_MAX_READS_AFTER_EXPLORE,
    EXECUTE_EXPLORE_BUDGET,
    EXECUTE_MAX_READS_AFTER_EXPLORE,
    EXECUTE_MAX_TOOLS_BEFORE_WRITE,
    EXECUTE_RECURSION_LIMIT,
    JUDGE_BASE_URL,
    PLAN_EXPLORE_BUDGET,
    PLAN_MAX_READS_AFTER_EXPLORE,
    REVIEW_EXPLORE_BUDGET,
    REVIEW_MAX_READS_AFTER_EXPLORE,
    REVIEW_MAX_TOOLS_BEFORE_WRITE,
    JUDGE_ENABLED,
    JUDGE_MAX_TOKENS,
    JUDGE_MODEL_NAME,
    JUDGE_TEMPERATURE,
    TURN_IDLE_TIMEOUT,
    REASONING_RETRY_ENABLED,
    MAX_REASONING_SECONDS,
    MAX_TOOL_CALLS_PER_TURN,
)
from core.roles import Role, role_for_intent
from display.console import console, print_role_switch, print_turn_summary, stream_agent_turn, ReasoningOnlyResponse, ToolCallLimitExceeded
from display.esc_watcher import EscWatcher
from llm_wrapper import LocalLLM, get_usage, reset_turn_usage
from orchestration.agent_builder import build_agent, init_mcp
from orchestration.execute_bootstrap import (
    _collect_cited_paths,
    build_paste_correction_suffix,
    extract_requested_task_numbers,
    preload_cited_files,
    preload_for_review,
)
from orchestration.framework_rules import inject_framework_rules
from orchestration.router import _extract_command_prefix, classify_intent
from orchestration.tool_dedupe import ExploreBudget, ToolBudgetExceeded, ToolCallDedupe

_ROLE_LABELS = {
    Role.ANALYZE: "🔍 Análisis", Role.PLAN: "📋 Planificación",
    Role.EXECUTE: "🛠️  Ejecución", Role.REVIEW: "🔎 Revisión",
    Role.CHAT: "💬 Charla",
}

# Context window: llama-server -c 32000. 90% = 28800 tokens.
_CONTEXT_LIMIT = 28800
_SUMMARY_THRESHOLD = 0.90
_WARNING_THRESHOLD = 0.85

# Mensaje para el retry write-only: el 4B NO debe leer, debe escribir directo.
_EXECUTE_FORCE_WRITE_MSG = (
    "\n\n⛔ RETRY TRAS LOOP: NO tenés tools de lectura. El contexto de la tarea "
    "y el layout YA están arriba.\n"
    "ESCRIBÍ el código AHORA:\n"
    "- NUNCA escribas/edites/borres tasks.md ni archivos de planificación (están "
    "protegidos — si lo intentás, la tool te lo va a rechazar).\n"
    "- Si un archivo ya existe y necesitás modificarlo: usá write_file para "
    "REESCRIBIR el archivo completo (no uses edit_file, no conocés el texto exacto).\n"
    "- Si es un archivo nuevo: crealo con write_file.\n"
    "Tratá cada archivo como si lo reescribieras entero con los cambios requeridos. "
    "No intentes leer ni explorar. No razones en voz alta: ejecutá una tool call directa."
)

def _load_judge_prompt() -> str:
    """Carga el prompt del judge."""
    from pathlib import Path
    prompt_path = Path(__file__).resolve().parent.parent / "prompts" / "judge.md"
    return prompt_path.read_text(encoding="utf-8")


_REVIEW_CORRECTION_KEYWORDS = (
    "cambios del review", "cambios propuestos", "corregir los hallazgos",
    "implementar los cambios", "aplicar el review", "fix the",
    "implementar el review", "resolver los hallazgos",
)


def _is_review_correction(user_input: str) -> bool:
    """Detecta si el usuario pide corregir los hallazgos de un review previo.
    Usa el comando del usuario (primeras 120 chars) para no matchear keywords
    en contenido pegado (checklists con 'Implementación', etc.)."""
    prefix = _extract_command_prefix(user_input).lower()
    has_action = any(k in prefix for k in ("implement", "correg", "aplic", "fix", "resolver", "arregl"))
    has_review = any(k in prefix for k in (
        "review", "hallazgo", "hallazgos", "reporte", "cambios propuesto",
        "sugerencias", "sugerencia", "observaciones", "correcciones",
    ))
    return has_action and has_review


def _build_review_correction_suffix() -> str:
    """Instrucción para aplicar correcciones del review previo (en history)."""
    return (
        "\n\n⛔ INSTRUCCIÓN (CORRECCIÓN POST-REVIEW): "
        "El reporte del review está en el historial de esta conversación (último mensaje del asistente). "
        "NO busques archivos. NO explores. LEÉ el review en el historial y aplicá cada hallazgo CRITICAL y WARNING. "
        "NUNCA escribas/edites/borres tasks.md ni archivos de planificación (están protegidos). "
        "Usá read_file UNA VEZ por archivo que debas modificar, luego edit_file/write_file/delete_file. "
        "Si el review pide eliminar un archivo, usá delete_file. "
        "NO razones en voz alta: aplicá las correcciones YA con una tool call directa. "
        "Máximo 2 archivos a leer. Después: stage, commit, install, lint, tests, build."
    )


# Comandos EXECUTE vagos: sin paths citados ni tarea explícita. El usuario dice
# "implementa" o "arreglá el bug" esperando que el agente retome lo analizado.
_AMBIGUOUS_EXECUTE_RE = re.compile(
    r"^\s*(implementa?r?|implementa?|hacelo|hace el fix|hacé el fix|arregl[aá]|"
    r"aplic[aá]|correg[ií]|resolv[eé]|fix(a|ea)?|pong[áa]|code[aá]|escrib[ií])\b",
    re.IGNORECASE,
)

# Extensions considered when extracting concrete file targets from a prior
# analysis so the chained EXECUTE read them directly.
_TARGET_FILE_RE = re.compile(r"([\w.\-/]+\.(?:tsx?|jsx?|go|py|ts|js))\b")


def _extract_target_files(analysis: str) -> list[str]:
    """Extrae paths de archivos mencionados en el análisis previo."""
    if not analysis:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _TARGET_FILE_RE.finditer(analysis):
        p = m.group(1)
        if p not in seen:
            seen.add(p)
            out.append(p)
        if len(out) >= 5:
            break
    return out


def _is_ambiguous_execute(user_input: str, repo_path: str | None = None) -> bool:
    """True si el mensaje es un comando EXECUTE vago (sin archivos, sin tarea
    concreta). Caso típico del día a día: "analizá X" → "implementa".
    Si hay paths/archivos citados explícitamente en el comando, NO es ambiguo
    (el preload ya resuelve la tarea)."""
    text = user_input.strip()
    if not _AMBIGUOUS_EXECUTE_RE.search(text):
        return False
    if _is_review_correction(text):
        return False  # lo captura el flujo de corrección post-review
    if len(text.split()) > 8:
        return False  # hay descripción, no es solo un verbo
    if re.search(r"(?:\w[\w.\-/]*\.\w{1,5}\b|/[\w./-]+/|/[\w./-]+\.\w{1,5})", text):
        return False  # menciona un path/archivo directamente
    return not _collect_cited_paths(text, repo_path)


def _derive_task_from_history(messages: list) -> str | None:
    """Devuelve el contenido del ÚLTIMO mensaje del asistente (análisis/plan/
    review) como tarea derivada. Retorna None si no hay historial útil."""
    for m in reversed(messages):
        if isinstance(m, AIMessage):
            content = str(m.content or "").strip()
            if len(content) >= 60:
                return content
            return None
        if isinstance(m, HumanMessage):
            # llegamos al turno previo del usuario sin respuesta asistente útil
            return None
    return None


_DISABLED_RE = re.compile(r"disabled\s*=\s*\{([^}]*)\}")


def _extract_edit_instruction(analysis: str, target_files: list[str]) -> str | None:
    """Si el análisis contiene una string concreta a reemplazar (ej: un
    `disabled={...}` viejo vs nuevo), devuelve una instrucción edit_file
    operativa para inyectar en el agent_input. Esto fuerza al modelo a ESCRIBIR
    en lugar de responder con texto descriptivo.

    Devuelve None si no se puede derivar con confianza."""
    matches = _DISABLED_RE.findall(analysis)
    if not matches:
        return None
    old = f"disabled={{{matches[0]}}}"
    # ¿el análisis menciona el nuevo disabled explícitamente?
    if len(matches) >= 2:
        new = f"disabled={{{matches[1]}}}"
    else:
        # Heurística: si el análisis menciona 'documento' como campo faltante,
        # agrego || !documento.trim() al disabled viejo.
        if "documento" in analysis.lower() and "!documento" not in matches[0]:
            new = old.rstrip("}") + " || !documento.trim()}"
        else:
            new = None
    if new is None:
        return None
    path = target_files[0] if target_files else ""
    return (
        "\n\n⛔ ACCIÓN OPERATIVA OBLIGATORIA — ejecutá EXACTAMENTE este edit_file "
        "(NO respondas con texto, ejecutá la tool):\n"
        f"edit_file(path=\"{path}\", old_str=\"{old}\", new_str=\"{new}\")\n"
        "Si la string old_str no existe exacta, leé el archivo y buscá la variante "
        "exacta (con mismos espacios), pero el new_str es el objetivo.\n"
    )


def _build_chained_execute_suffix(task: str, target_files: list[str] | None = None) -> str:
    """Construye el mensaje EXECUTE cuando el usuario retoma un análisis previo
    con un comando vago ("implementa"). Incluye el path correcto al archivo
    real (frontend/ monorepo) si aparece en el análisis."""
    snippet = task if len(task) <= 2000 else task[:2000] + "\n...(truncado)"
    files_line = ""
    if target_files:
        files_line = (
            "\nArchivo(s) objetivo (LEÉ ESTOS, NO explores, NO hagas list_files ni "
            "search_code en directorios):\n"
            + "\n".join(f"- {p}" for p in target_files[:5])
            + "\n"
        )
    edit_op = _extract_edit_instruction(task, target_files or [])
    return (
        "\n\n⛔ INSTRUCCIÓN (RETOMANDO ANÁLISIS PREVIO): "
        "El usuario te pidió implementar/arreglar algo analizado ANTES en esta "
        "conversación. TU TAREA es la siguiente (del análisis previo):\n"
        "---\n"
        f"{snippet}\n"
        "---\n"
        + files_line
        + (edit_op or "")
        + "\nIMPORTANTE:\n"
        "- Verificá SIEMPRE el path REAL del archivo antes de leerlo: en repos "
        "monorepo los archivos viven bajo frontend/src/ etc. Si un read_file "
        "falla, buscá con list_files UNA VEZ en la raíz para hallar el path.\n"
        "- Aplicá el fix del hallazgo. Verificá con run_lint/run_tests/run_build "
        "si es viable, y commiteá con conventional commit.\n"
    )



def _derive_task_from_history(messages: list) -> str | None:
    """Devuelve el contenido del ÚLTIMO mensaje del asistente (análisis/plan/
    review) como tarea derivada. Retorna None si el último turno del asistente
    fue vacío o no hay historial útil."""
    for m in reversed(messages):
        if isinstance(m, AIMessage):
            content = str(m.content or "").strip()
            if len(content) >= 60:
                return content
            return None
        if isinstance(m, HumanMessage):
            # llegamos al turno previo del usuario sin respuesta asistente útil
            return None
    return None


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

        # Lógica de negocio: reglas concretas (campos requeridos, validaciones)
        # que el 4B ignora cuando solo ve la estructura general. Se inyecta en
        # el system prompt de TODOS los roles via cached_analysis.
        try:
            from business_logic import get_business_context
            biz = get_business_context(repo_path, mcp_tools=None)
            if biz:
                sep = "\n\n" if self.cached_analysis else ""
                self.cached_analysis = self.cached_analysis + sep + biz
        except Exception:
            pass

        self.current_role: Role = Role.ANALYZE
        self.agent: Any = None
        self._tools: list = []
        self._mcp_available: int = 0  # total MCP cargados
        self._mcp_count: int = 0      # MCP activos en el rol actual
        self._local_count: int = 0
        self._dedupe = ToolCallDedupe(max_repeats=2)
        self._explore_budget = ExploreBudget(
            max_calls=EXECUTE_EXPLORE_BUDGET,
            max_reads_after_explore=EXECUTE_MAX_READS_AFTER_EXPLORE,
            max_tools_before_write=EXECUTE_MAX_TOOLS_BEFORE_WRITE,
        )
        # ANALYZE/PLAN: capa la búsqueda MCP pero NUNCA presiona a escribir.
        # Al agotarse lanza ToolBudgetExceeded → retry no_explore (no write-only).
        self._analyze_budget = ExploreBudget(
            max_calls=ANALYZE_EXPLORE_BUDGET,
            max_reads_after_explore=ANALYZE_MAX_READS_AFTER_EXPLORE,
            max_tools_before_write=0,
            write_pressure=False,
        )
        self._session_time: float = 0.0
        self.session_id: str = str(uuid.uuid4())[:8]

        self._messages: list = []  # historial de la sesión actual
        self._last_response: str = ""  # respuesta del último turno

        # Cache de archivos leídos (read_file) durante el turno: el retry
        # write-only lo inyecta como anclaje para reescribir sin leer.
        self._read_cache: dict[str, str] = {}
        self._no_explore_retry = False

    def start(self) -> str:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self._tools, self._mcp_available = loop.run_until_complete(init_mcp())
            self.current_role = Role.ANALYZE
            self._load_previous_sessions()
            self.agent, self._local_count, self._mcp_count = loop.run_until_complete(
                build_agent(
                    self.llm, Role.ANALYZE, self.repo_path,
                    self.cached_analysis, self._tools, self._dedupe,
                    self._explore_budget, self._analyze_budget,
                )
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

    def _rebuild_agent(self, role: Role, no_explore: bool = False) -> bool:
        if self.agent is not None and role == self.current_role and not no_explore:
            return False
        self.current_role = role
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.agent, self._local_count, self._mcp_count = loop.run_until_complete(
                build_agent(
                    self.llm, role, self.repo_path,
                    self.cached_analysis, self._tools, self._dedupe,
                    self._explore_budget, self._analyze_budget,
                    read_cache=self._read_cache,
                    no_explore=no_explore,
                )
            )
        finally:
            loop.close()
        return True

    def _rebuild_agent_write_only(self):
        """Reconstruye el agente EXECUTE con SOLO tools de escritura.

        Se llama en el retry tras un loop de lectura. El 4B con read_file/
        list_files disponibles entra en loops infinitos de lectura y nunca
        escribe. Sin esas tools, escribe directo (verificado en pruebas).
        """
        self.current_role = Role.EXECUTE
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.agent, self._local_count, self._mcp_count = loop.run_until_complete(
                build_agent(
                    self.llm, Role.EXECUTE, self.repo_path,
                    self.cached_analysis, self._tools, self._dedupe,
                    self._explore_budget, self._analyze_budget,
                    force_write=True,
                    read_cache=self._read_cache,
                )
            )
        finally:
            loop.close()

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

    def _retry_with_read_anchor(self) -> str:
        """Construye el mensaje de retry write-only incluyendo el contenido
        de los archivos que read_file cacheó en el PASS1. El 4B sin tools de
        lectura NECESITA este anclaje para reescribir código real (si no,
        razona en círculos)."""
        anchor = ""
        already_committed = self._repo_has_recent_commit()
        if self._read_cache:
            blocks = []
            for path, content in list(self._read_cache.items())[:6]:
                blocks.append(f"--- CONTENIDO REAL DE {path} ---\n{content[:4000]}\n--- FIN {path} ---")
            if blocks:
                anchor = (
                    "\n\nCONTENIDO DE ARCHIVOS YA LEÍDOS (úsalo como base para "
                    "reescribir, NO los vuelvas a pedir):\n" + "\n".join(blocks)
                )
        if already_committed:
            return (
                "\n\n⛔ RETRY: el intento anterior YA escribió y commiteó código "
                "(se detectó un commit reciente). NO reescribas los mismos archivos "
                "ni dupliques commits. "
                "Corré run_lint, run_tests y run_build para confirmar que todo "
                "está verde, y si ya lo están, terminá con un resumen breve. "
                "NO hagas write_file de archivos que ya modificaste." + anchor
            )
        return _EXECUTE_FORCE_WRITE_MSG + anchor

    def _system_trace_for(self, user_msg: str) -> str:
        """El SISTEMA (no el 4B) resuelve el término del usuario con
        trace_component y devuelve el texto resultante.

        El 4B en el PASS1 suele trazar componentes equivocadas (varianza).
        En el retry, el sistema traza el término del usuario EN CÓDIGO con la
        misma tool compuesta que funciona (resolve + source + usos + página),
        garantizando que el ancla tenga la cadena correcta sin depender de que
        el 4B orqueste la exploración."""
        try:
            from tools.graph_trace import build_trace_component
            import asyncio, json, threading

            async def _run():
                # 1) project key: el indexado del repo actual (sandbox o repo del usuario)
                project = ""
                by_name = {t.name: t for t in self._tools}
                lp = by_name.get("cm__list_projects")
                text = ""
                if lp:
                    raw = await lp.ainvoke({})
                    if isinstance(raw, list):
                        text = "".join(
                            b.get("text", "") for b in raw
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                    elif isinstance(raw, str):
                        text = raw
                    if text:
                        data = json.loads(text)
                        for p in data.get("projects", []):
                            if p.get("root_path") == self.repo_path:
                                project = p.get("name", "")
                                break

                if not project:
                    import os
                    basename = os.path.basename(self.repo_path.rstrip("/")).lower()
                    if basename and text:
                        matches = [
                            p.get("name", "")
                            for p in data.get("projects", [])
                            if basename in p.get("root_path", "").lower()
                        ]
                        if matches:
                            project = matches[0]

                if not project:
                    return ""

                tc = build_trace_component(self._tools, self.repo_path)
                term = self._extract_component_term(user_msg)
                if not term:
                    return ""
                result = await tc.ainvoke({"component": term, "project": project})
                return str(result)

            out: dict = {"r": ""}

            def _thread():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    out["r"] = loop.run_until_complete(_run())
                finally:
                    loop.close()

            t = threading.Thread(target=_thread, daemon=True)
            t.start()
            t.join(timeout=120)
            if t.is_alive():
                print("[system_trace_for] timeout 120s", flush=True)
            return out["r"]
        except Exception as e:
            print(f"[system_trace_for] error: {type(e).__name__}: {e}", flush=True)
            return ""

    def _extract_component_term(self, user_msg: str) -> str:
        """Extrae el término de componente/bug del mensaje del usuario para
        trace_component. Usa la frase de la pregunta directamente (trace_component
        resuelve lenguaje natural via _extract_exported_component)."""
        return user_msg.strip()[:120]

    def _retry_analyze_anchor(self) -> str:
        """Ancla para el retry de ANALYZE/PLAN: el contenido que trace_component
        y read_file cachearon en el PASS1. Sin esto, el 4B no tiene NADA que
        analizar (los ToolMessages del graph se pierden al cortar por budget)
        y razona en círculos adivinando paths."""
        if not self._read_cache:
            return ""
        # El 4B se enfoca en lo PRIMERO que ve y se confunde con componentes
        # duplicados. Si el PASS1 cacheó traces, usar SOLO esos (en orden de
        # exploración): el primero suele ser la componente central del bug.
        # El trace del sistema (que puede resolver un hook y duplicar contenido)
        # se usa SOLO como fallback cuando el PASS1 no cacheó nada.
        pass1 = [k for k in self._read_cache.keys() if k != "[trace:sistema]"]
        if not pass1:
            pass1 = [k for k in self._read_cache.keys() if k == "[trace:sistema]"]
        blocks = []
        for key in pass1[:1]:
            content = self._read_cache[key]
            if key.startswith("[trace:"):
                label = f"RESULTADO DE TRACE_COMPONENT ({key})"
            else:
                label = f"CONTENIDO REAL DE {key}"
            blocks.append(f"--- {label} ---\n{content[:5000]}\n--- FIN ---")
        return (
            "\n\nCONTENIDO QUE YA LEÍSTE EN EL INTENTO ANTERIOR (es lo ÚNICO que "
            "tenés; analizá EN BASE A ESTO, NO inventes otros paths).\n"
            "🔴 LA COMPONENTE SIGUIENTE ES EL CÓDIGO REAL DEL BUG. Analizá su "
            "código línea por línea y compará la validación del submit con la "
            "condición del botón Guardar. Respondé el análisis AHORA.\n"
            + "\n\n".join(blocks)
        )

    def _retry_analyze_no_explore(self, new_role: Role, reason: str) -> None:
        """Retry de ANALYZE/PLAN: reconstruye el agente SIN tools (no write-only
        — estos roles NUNCA escriben en el repo del usuario). El 4B usa cualquier
        tool como muleta y razona "qué más leer" en vez de responder. El contexto
        correcto lo arma el SISTEMA (ancla de traces + _system_trace_for); con
        0 tools el modelo responde el análisis en texto plano (verificado)."""
        self._no_explore_retry = True
        user_msg = ""
        for m in reversed(self._messages):
            if isinstance(m, HumanMessage):
                user_msg = str(m.content)
                break
        anchor = self._retry_analyze_anchor()
        # El SISTEMA complementa el ancla SOLO si el PASS1 no cacheó traces
        # (si exploró mal y el budget se agotó antes de tocar código). Si el
        # PASS1 cacheó traces, esos van primero y son la pista principal; el
        # trace del sistema resolvería hooks que DUPLICAN contenido y distraen
        # al 4B (verificado: con usePacientes duplicado, ignora el modal).
        if not any(k.startswith("[trace:") for k in self._read_cache):
            sys_trace = self._system_trace_for(user_msg)
            if sys_trace:
                self._read_cache["[trace:sistema]"] = sys_trace
                anchor = self._retry_analyze_anchor()
        retry_body = f"Reanalizá la pregunta: \"{user_msg}\""
        if anchor:
            retry_body += anchor
        self._messages = self._messages[-3:] if len(self._messages) > 3 else self._messages
        self._messages.append(HumanMessage(retry_body))
        self._rebuild_agent(new_role, no_explore=True)
        self._analyze_budget.reset()
        console.print(
            f"\n[yellow]⚠️  {reason} — Reintentando sin tools de búsqueda "
            "(responde con lo ya leído)...[/yellow]"
        )

    def _repo_has_recent_commit(self) -> bool:
        """Detecta si el repo tiene un commit en los últimos ~5 minutos
        (el intento anterior pudo haber commiteado antes de ser cortado)."""
        try:
            import subprocess
            result = subprocess.run(
                ["git", "log", "-1", "--format=%ct"],
                cwd=self.repo_path, capture_output=True, text=True, timeout=5,
            )
            ts = result.stdout.strip()
            if not ts.isdigit():
                return False
            import time as _t
            return ( _t.time() - int(ts) ) < 300
        except Exception:
            return False

    def run_turn(self, user_input: str, status: str | None = None) -> None:
        """Clasifica, cambia rol, ejecuta con historial, persiste en SQLite."""
        self._no_explore_retry = False
        intent = classify_intent(self.llm, user_input)
        new_role = role_for_intent(intent)

        role_changed = self._rebuild_agent(new_role)
        role_label = _ROLE_LABELS.get(new_role, "")

        if role_changed and new_role != Role.CHAT:
            print_role_switch(role_label, self._local_count, self._mcp_count)

        if status:
            print(status, flush=True)

        # Reset dedupe + explore budget cada turno (siempre restaurar defaults)
        self._dedupe.reset()
        self._explore_budget.reset()
        self._analyze_budget.reset()
        if new_role == Role.EXECUTE:
            self._explore_budget.max_calls = EXECUTE_EXPLORE_BUDGET
            self._explore_budget.max_reads_after_explore = EXECUTE_MAX_READS_AFTER_EXPLORE
            self._explore_budget.max_tools_before_write = EXECUTE_MAX_TOOLS_BEFORE_WRITE
        elif new_role == Role.REVIEW:
            self._explore_budget.max_calls = REVIEW_EXPLORE_BUDGET
            self._explore_budget.max_reads_after_explore = REVIEW_MAX_READS_AFTER_EXPLORE
            self._explore_budget.max_tools_before_write = REVIEW_MAX_TOOLS_BEFORE_WRITE
        elif new_role == Role.ANALYZE:
            self._analyze_budget.max_calls = ANALYZE_EXPLORE_BUDGET
            self._analyze_budget.max_reads_after_explore = ANALYZE_MAX_READS_AFTER_EXPLORE
        elif new_role == Role.PLAN:
            self._analyze_budget.max_calls = PLAN_EXPLORE_BUDGET
            self._analyze_budget.max_reads_after_explore = PLAN_MAX_READS_AFTER_EXPLORE

        # Acumular el mensaje del usuario en el historial
        agent_input = user_input
        if new_role == Role.EXECUTE:
            agent_input = preload_cited_files(user_input, self.repo_path)
            if agent_input != user_input:
                nums = extract_requested_task_numbers(user_input)
                scope = f" (solo Tarea(s) {', '.join(map(str, nums))})" if nums else ""
                hints_on = "CONTEXTO DE REPO PRECARGADO" in agent_input
                # Con hints ya inyectados, explore=0: cualquier list_files/search_code
                # lanza ToolBudgetExceeded (GraphBubbleUp) → propaga → retry write-only.
                # El 4B con max_calls=1 recibe strings STOP y los ignora, quemando el
                # recursion limit sin escribir. Con 0, la excepción corta de inmediato.
                if hints_on:
                    self._explore_budget.max_calls = 0
                    self._explore_budget.max_reads_after_explore = 5
                    self._explore_budget.max_tools_before_write = 8
                    self._dedupe.max_repeats = 1
                extra = " · explore=0 (hints)" if hints_on else ""
                console.print(
                    f"[dim]📎 Archivos de tareas pre-cargados{scope} "
                    f"+ checklist AC{extra}.[/dim]\n"
                )
            # Correcciones pegadas (sin path a tasks.md): forzar escritura
            elif len(user_input) > 400 and any(
                k in user_input.lower()
                for k in ("correc", "problema", "falta", "critical", "crític")
            ):
                agent_input = user_input + build_paste_correction_suffix(user_input)
            # Review → corrección: "implementar los cambios del review"
            elif _is_review_correction(user_input):
                agent_input = user_input + _build_review_correction_suffix()
                self._explore_budget.max_calls = 0
                self._explore_budget.max_tools_before_write = 4
                console.print("[dim]🔗 Corrección post-review — explore=0, force write.[/dim]\n")
            # Retomar análisis previo: "implementa" / "arreglá" sin tarea explícita
            elif _is_ambiguous_execute(user_input, self.repo_path):
                task = _derive_task_from_history(self._messages)
                if task:
                    targets = _extract_target_files(task)
                    agent_input = user_input + _build_chained_execute_suffix(
                        task, target_files=targets
                    )
                    # Mismos límites que la corrección post-review: prohibir
                    # exploración y forzar lectura-directa de los objetivos.
                    self._explore_budget.max_calls = 0
                    self._explore_budget.max_tools_before_write = 4
                    console.print("[dim]🔗 Retomando análisis previo como tarea (explore=0).[/dim]\n")
        elif new_role == Role.REVIEW:
            agent_input = preload_for_review(user_input, self.repo_path)
            self._dedupe.max_repeats = 1
            if agent_input != user_input:
                # Con git context precargado, no necesita explorar
                self._explore_budget.max_calls = 1
                # Reviewer needs to read ALL modified files + run verify tools
                self._explore_budget.max_reads_after_explore = 15
                self._explore_budget.max_tools_before_write = 30
                nums = extract_requested_task_numbers(user_input)
                scope = f" (Tarea(s) {', '.join(map(str, nums))})" if nums else ""
                console.print(
                    f"[dim]📎 Checklist AC pre-cargado para review{scope}.[/dim]\n"
                )

        self._messages.append(HumanMessage(agent_input))

        reset_turn_usage()
        start = time.monotonic()
        recursion = (
            EXECUTE_RECURSION_LIMIT if new_role == Role.EXECUTE else AGENT_RECURSION_LIMIT
        )
        config = {
            "configurable": {"thread_id": f"session-{id(self)}"},
            "recursion_limit": recursion,
        }

        # Pasar todo el historial al agente
        messages_for_agent = list(self._messages)

        # 1 intento normal + hasta 2 retries write-only (si el primero tampoco
        # escribe, el 2do con instrucción aún más estricta)
        max_attempts = 1 + (2 if REASONING_RETRY_ENABLED else 0)
        attempt = 0
        interrupted = False
        interrupted_by_esc = False

        while attempt < max_attempts:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            # require_write solo aplica a retries write-only de EXECUTE/REVIEW.
            # ANALYZE/PLAN NUNCA deben ser forzados a escribir (el usuario los
            # usa para analizar/planificar, no para tocar el repo).
            is_write_retry = (
                attempt > 0
                and REASONING_RETRY_ENABLED
                and new_role not in (Role.ANALYZE, Role.PLAN)
            )
            task = loop.create_task(
                stream_agent_turn(
                    self.agent,
                    messages_for_agent,
                    config,
                    idle_timeout=TURN_IDLE_TIMEOUT,
                    # Retry no_explore (ANALYZE/PLAN con 0 tools): el modelo NO
                    # puede entrar en loop de tools; razona 3-6 min y responde.
                    # Cortar por MAX_REASONING_SECONDS mata justo antes de la
                    # respuesta (verificado: responde en ~387s con ancla).
                    # idle_timeout cubre un modelo realmente colgado.
                    max_reasoning_seconds=(
                        None if self._no_explore_retry else MAX_REASONING_SECONDS
                    ),
                    max_tool_calls=MAX_TOOL_CALLS_PER_TURN,
                    require_write=is_write_retry,
                )
            )

            # Watcher de ESC: permite interrumpir el streaming y volver al prompt.
            watcher = EscWatcher(
                cancel_cb=lambda: loop.call_soon_threadsafe(task.cancel)
            )
            watcher.start()

            try:
                loop.run_until_complete(task)
                self._last_response = task.result() if not task.cancelled() else ""
                break
            except KeyboardInterrupt:
                interrupted = True
                task.cancel()
                try:
                    loop.run_until_complete(asyncio.wait_for(task, timeout=2.0))
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    pass
                break
            except asyncio.CancelledError:
                interrupted = True
                interrupted_by_esc = watcher.interrupted.is_set()
                break
            except ToolBudgetExceeded as e:
                if attempt + 1 >= max_attempts:
                    interrupted = True
                    print(
                        f"\n\n⚡ Presupuesto de tools agotado: {e} "
                        "Reintentá pidiendo implementar directamente o con un path más concreto.",
                        flush=True,
                    )
                    break
                attempt += 1
                if new_role in (Role.ANALYZE, Role.PLAN):
                    # Retry SIN tools de búsqueda (no write-only): ANALYZE/PLAN
                    # no escriben; deben responder con lo que ya leyeron.
                    self._retry_analyze_no_explore(
                        new_role,
                        f"Exploración agotada en {role_label}: {e}",
                    )
                    messages_for_agent = list(self._messages)
                    continue
                self._messages = self._messages[-3:] if len(self._messages) > 3 else self._messages
                self._messages.append(HumanMessage(self._retry_with_read_anchor()))
                messages_for_agent = list(self._messages)
                self._rebuild_agent_write_only()
                console.print(
                    f"\n[yellow]⚠️  Tool budget agotado: {e}. "
                    "Reintentando con SOLO tools de escritura (sin read_file)...[/yellow]"
                )
                continue
            except ReasoningOnlyResponse as e:
                if attempt + 1 >= max_attempts:
                    if isinstance(e, ToolCallLimitExceeded):
                        print(
                            f"\n\n⚠️  El modelo hizo {e.total_calls} tool calls (límite {e.limit}) "
                            "y entró en loop. Reinicia con /new o reduce el prompt.",
                            flush=True,
                        )
                    else:
                        print(
                            f"\n\n⚠️  El modelo gastó todo el output ({len(e.reasoning_text)} chars) "
                            "en razonamiento sin producir acción. Reinicia con /new o reduce el prompt.",
                            flush=True,
                        )
                    break
                attempt += 1
                if new_role in (Role.ANALYZE, Role.PLAN):
                    # ANALYZE/PLAN nunca van write-only: sin tools de búsqueda,
                    # responde con el contexto que ya tiene.
                    reason = (
                        f"El modelo hizo {e.total_calls} tool calls (loop)"
                        if isinstance(e, ToolCallLimitExceeded)
                        else f"El modelo gastó {len(e.reasoning_text)} chars razonando sin actuar"
                    )
                    self._retry_analyze_no_explore(new_role, reason)
                    messages_for_agent = list(self._messages)
                    continue
                self._messages = self._messages[-3:] if len(self._messages) > 3 else self._messages
                self._messages.append(HumanMessage(self._retry_with_read_anchor()))
                messages_for_agent = list(self._messages)
                self._rebuild_agent_write_only()
                if isinstance(e, ToolCallLimitExceeded):
                    console.print(
                        f"\n[yellow]⚠️  El modelo hizo {e.total_calls} tool calls (loop de reads). "
                        "Reintentando con SOLO tools de escritura (sin read_file)...[/yellow]"
                    )
                else:
                    console.print(
                        f"\n[yellow]⚠️  El modelo gastó {len(e.reasoning_text)} chars en razonamiento "
                        "sin actuar. Reintentando con SOLO tools de escritura (sin read_file)...[/yellow]"
                    )
                continue
            except Exception as e:
                name = type(e).__name__
                if "Recursion" in name or "recursion" in str(e).lower():
                    lim = (
                        EXECUTE_RECURSION_LIMIT
                        if new_role == Role.EXECUTE
                        else AGENT_RECURSION_LIMIT
                    )
                    print(
                        f"\n\n⚠️  Turno cortado: demasiados pasos de exploración "
                        f"(límite {lim}). "
                        "Reintentá pidiendo implementar directamente o con un path más concreto.",
                        flush=True,
                    )
                else:
                    print(f"\n\n❌ Error en la iteración: {e}", flush=True)
                break
            finally:
                watcher.stop()
                loop.close()

        elapsed = time.monotonic() - start
        self._session_time += elapsed

        usage = get_usage()
        turn_tokens = usage["turn"]["prompt"] + usage["turn"]["completion"]

        if not interrupted:
            self._messages.append(AIMessage(self._last_response or "(respuesta generada)"))

        # Judge: valida reviews que dicen APROBADO
        if new_role == Role.REVIEW and not interrupted:
            self._maybe_judge_review(user_input)

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

        ctx_status = self._check_context()
        if ctx_status == "warning":
            pct = _estimate_tokens(self._messages) / _CONTEXT_LIMIT * 100
            console.print(f"\n[yellow]⚠️  Contexto al {pct:.0f}% — usá /new para empezar sesión nueva[/yellow]\n")
        elif ctx_status == "summary":
            self._maybe_summarize()

    def _maybe_judge_review(self, user_input: str) -> None:
        """Si el review dice APROBADO, llama al judge LLM para validar."""
        if not JUDGE_ENABLED:
            return

        response = self._last_response or ""
        if not any(k in response for k in ("APROBADO", "APROBADA", "✅ APROBAR")):
            return

        console.print("\n[bold yellow]⚖️  JUDGE — validando review con modelo externo…[/bold yellow]")

        # Get git diff
        diff = self._get_git_diff()
        if not diff:
            console.print("[dim]   (sin diff para evaluar, se omite judge)[/dim]\n")
            return

        # Load judge prompt
        judge_prompt = _load_judge_prompt()

        # Build judge message
        judge_message = (
            f"## DIFF DE LA RAMA\n\n{diff}\n\n"
            f"## INFORME DE REVIEW DEL AGENTE\n\n{response}\n\n"
            f"## CONTEXTO DEL USUARIO\n\n{user_input}\n\n"
            "## TU TAREA: Validá si el review fue exhaustivo y si el veredicto es correcto."
        )

        # Call judge LLM
        try:
            judge_llm = LocalLLM(
                base_url=JUDGE_BASE_URL,
                model_name=JUDGE_MODEL_NAME,
                temperature=JUDGE_TEMPERATURE,
                max_tokens=JUDGE_MAX_TOKENS,
                api_key="not-needed",
            )
            result = judge_llm.invoke(judge_message)
            verdict = result.content or ""

            # Print verdict
            if "NO APROBAR" in verdict or "CRITICAL" in verdict:
                console.print("\n[bold red]⛔ JUDGE: NO APROBAR[/bold red]")
            elif "REVISAR" in verdict:
                console.print("\n[bold yellow]⚠️  JUDGE: REVISAR[/bold yellow]")
            else:
                console.print("\n[bold green]✅ JUDGE: APROBADO (confirmado)[/bold green]")

            console.print(f"\n[dim]{verdict}[/dim]\n")
        except Exception as e:
            console.print(f"[dim red]⚠️  Judge falló: {e}[/dim red]\n")

    def _get_git_diff(self) -> str:
        """Get the diff of changed files in the repo."""
        try:
            branch = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=self.repo_path, capture_output=True, text=True, timeout=3,
            ).stdout.strip()
            if not branch:
                return ""

            # Diff against main or upstream
            result = subprocess.run(
                ["git", "diff", f"main...{branch}", "--stat"],
                cwd=self.repo_path, capture_output=True, text=True, timeout=10,
            )
            stat = result.stdout.strip()

            result = subprocess.run(
                ["git", "diff", f"main...{branch}"],
                cwd=self.repo_path, capture_output=True, text=True, timeout=10,
            )
            diff = result.stdout.strip()
            if not diff:
                # Try last N commits if diff is empty
                result = subprocess.run(
                    ["git", "diff", "--cached"],
                    cwd=self.repo_path, capture_output=True, text=True, timeout=10,
                )
                diff = result.stdout.strip()
            if not diff:
                result = subprocess.run(
                    ["git", "log", "-3", "--diff-filter=d", "--patch"],
                    cwd=self.repo_path, capture_output=True, text=True, timeout=10,
                )
                diff = result.stdout.strip()

            return f"### STAT\n{stat}\n\n### DIFF\n{diff[:15000]}" if diff else ""
        except Exception:
            return ""

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
