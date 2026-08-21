from __future__ import annotations

import asyncio
import re
import subprocess
import sys
import time
import uuid
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from cache import load_recent_turns, save_turn
from cache import (
    bulk_progress,
    ensure_bulk_plan,
    fail_or_keep_batch,
    mark_batch,
    next_pending_batch,
)
from config import (
    AGENT_RECURSION_LIMIT,
    ANALYZE_EXPLORE_BUDGET,
    ANALYZE_MAX_READS_AFTER_EXPLORE,
    EXECUTE_ASK_COMMIT,
    EXECUTE_EXPLORE_BUDGET,
    LLM_BASE_URL,
    EXECUTE_MAX_READS_AFTER_EXPLORE,
    EXECUTE_MAX_TOOLS_BEFORE_WRITE,
    EXECUTE_MAX_VERIFY_BEFORE_WRITE,
    EXECUTE_MAX_WRITES_BEFORE_VERIFY,
    EXECUTE_RECURSION_LIMIT,
    EXECUTE_MAX_REASONING_SECONDS,
    EXECUTE_REQUIRE_WRITE,
    EXECUTE_BULK_MIN_FILES,
    JUDGE_BASE_URL,
    PLAN_EXPLORE_BUDGET,
    PLAN_MAX_READS_AFTER_EXPLORE,
    REVIEW_EXPLORE_BUDGET,
    REVIEW_MAX_READS_AFTER_EXPLORE,
    REVIEW_MAX_TOOLS_BEFORE_WRITE,
    EXECUTE_BULK_MAX_ATTEMPTS,
    BULK_BATCH_SIZE,
    BULK_MAX_BATCH_ATTEMPTS,
    BULK_SESSION_ROTATION_CTX,
    JUDGE_ENABLED,
    JUDGE_MAX_TOKENS,
    JUDGE_MODEL_NAME,
    JUDGE_TEMPERATURE,
    POST_WRITE_GATE_ENABLED,
    POST_WRITE_GATE_MAX_RETRIES,
    TURN_IDLE_TIMEOUT,
    REASONING_RETRY_ENABLED,
    MAX_REASONING_SECONDS,
    MAX_TOOL_CALLS_PER_TURN,
    VERIFY_GATE_MAX_INJECTIONS,
)
from core.intents import Intent
from core.roles import Role, role_for_intent
from display.console import console, print_role_switch, print_turn_summary, stream_agent_turn, ReasoningOnlyResponse, ToolCallLimitExceeded
from tools import BUDGET_RETRY_TOOLS, GATE_RETRY_TOOLS
from display.esc_watcher import EscWatcher
from llm_wrapper import LocalLLM, get_usage, reset_turn_usage
from orchestration.agent_builder import build_agent, init_mcp
from orchestration.bulk_planner import (
    bulk_task_hash,
    build_batch_scope,
    canonical_task_text,
    detect_bulk_targets,
    split_into_batches,
)
from orchestration.execute_bootstrap import (
    _collect_cited_paths,
    build_paste_correction_suffix,
    detect_bulk_file_count,
    extract_requested_task_numbers,
    inject_repo_hints,
    preload_cited_files,
    preload_for_review,
)
from orchestration.framework_rules import inject_framework_rules
from orchestration.path_mismatch import apply_mismatch_fixes, detect_path_mismatches
from orchestration.runtime_diagnostics import runtime_status
from orchestration.router import _extract_command_prefix, classify_intent
from orchestration.tool_dedupe import (
    ExploreBudget,
    ToolBudgetExceeded,
    ToolCallDedupe,
    VERIFY_TOOL_NAMES,
    VerifyRequired,
    WRITE_TOOL_NAMES,
)

_ROLE_LABELS = {
    Role.ANALYZE: "🔍 Análisis", Role.PLAN: "📋 Planificación",
    Role.EXECUTE: "🛠️  Ejecución", Role.REVIEW: "🔎 Revisión",
    Role.CHAT: "💬 Charla",
}

# Context window: llama-server -c 62000 (configurado por el usuario).
# 90% = 55800 tokens.
# Fallback si no se puede detectar el n_ctx real del server. El valor VIVO
# vive en self._ctx_limit (Session.start lo detecta via GET /props).
_CONTEXT_LIMIT = 55800
_SUMMARY_THRESHOLD = 0.90
_WARNING_THRESHOLD = 0.80

# Mensaje para el retry EXECUTE: el agente se reconstruye con BUDGET_RETRY_TOOLS
# (read_file ACOTADO + edit_file + write_file; sin delete_file, sin verify, sin
# búsqueda). El mensaje DEBE ser coherente con esas tools — antes pedía
# search_code/run_lint (inexistentes en el retry) y el modelo entraba en
# espiral intentando acciones imposibles.
_EXECUTE_FORCE_WRITE_MSG = (
    "\n\n⛔ RETRY: ya leíste/analizaste bastante. El turno anterior terminó sin "
    "escribir nada y eso NO es válido para este rol.\n"
    "PROCEDÉ ASÍ:\n"
    "1) Tenés read_file (LIMITADO: el sistema corta si abusás), edit_file y "
    "write_file. delete_file NO está disponible en este retry.\n"
    "2) El CONTENIDO EXACTO de lo ya leído está inyectado abajo (ancla): "
    "copiá el old_str LITERAL de ahí. Si te falta un bloque, leé el archivo "
    "UNA vez con read_file.\n"
    "3) write_file SOLO para archivos NUEVOS (está BLOQUEADO para archivos "
    "existentes).\n"
    "4) Si un bloque es muy grande, partí el cambio en bloques MÁS CHICOS "
    "(≤20 líneas).\n"
    "NO respondas con texto: ejecutá write_file/edit_file AHORA."
)


def _bulk_budget(bulk: int) -> dict:
    """Budgets escalados para tareas que tocan N archivos (ej. 14 templates).

    El budget default de EXECUTE está calibrado para diagnóstico de 1-5
    archivos. Una tarea bulk necesita ~N lecturas + ~2N edits + verify:
    - reads: N + 4 (margen), tope 24
    - tools-before-write: 2N + 8 (no forzar write antes de leer los N archivos)
    - writes-before-verify: N (verificar al cerrar una pasada completa, no cada 6)
    - tool calls por turno: 4N + 8, tope 55 (14+28+verify ≈ 46)
    """
    return {
        "max_reads_after_explore": max(EXECUTE_MAX_READS_AFTER_EXPLORE, min(bulk + 4, 24)),
        "max_tools_before_write": max(EXECUTE_MAX_TOOLS_BEFORE_WRITE, 2 * bulk + 8),
        "max_writes_before_verify": max(EXECUTE_MAX_WRITES_BEFORE_VERIFY, bulk),
        "tool_calls_per_turn": max(MAX_TOOL_CALLS_PER_TURN, min(4 * bulk + 8, 55)),
    }

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

def _build_commit_message(user_input: str) -> str:
    """Mensaje conventional commit derivado del pedido del usuario."""
    prefix = _extract_command_prefix(user_input)
    kind = (
        "fix" if any(w in prefix for w in (
            "fix", "solucion", "resuelv", "arregl", "repar", "correg", "bug"
        )) else "feat"
    )
    summary = " ".join(prefix.split())[:70] or "autonomous edits"
    return f"{kind}: {summary}"


# Frases que sugieren un problema de RUNTIME (no de lógica): el sistema corre
# el diagnóstico de puertos (container Docker con código viejo, dev server
# caído) antes de que el modelo toque el código.
_RUNTIME_ERROR_HINTS = (
    "error interno", "internal server", "500",
    "no guarda", "no me guarda", "no se guarda", "no guardó", "no guardo",
    "no carga", "no se carga", "no funciona", "no anda", "no responde",
)


# Palabras que indican que el usuario quiere continuar con lo que estaba
# haciendo (no son verbos de acción explícitos). Si el rol anterior era
# EXECUTE, mantenerlo en vez de caer en ANALYZE.
_CONTINUATION_WORDS = frozenset({
    "continuar", "continua", "continúa", "continue",
    "sigue", "seguí", "seguir",
    "dale", "va", "vamos", "adelante",
    "ok", "okay", "sí", "si", "yes",
    "procedé", "procede", "proceder",
    "hacelo", "hazlo", "do it",
})


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


# Patrones de pregunta autocontenida: el usuario pegó el error + código inline,
# no hay nada que explorar. El modelo (Qwen3.5 razonador) responde BIEN y rápido
# en single-shot con 0 tools; con tools entra en loop de exploración+reasoning y
# cuelga minutos. Ej: 'analizá este error "...PacientesPage.tsx: Unexpected
# token" 50 | <line ... />"'.
_SELFCONTAINED_ERROR_RE = re.compile(
    r"(unexpected token|syntax error|parse error|expected .*token|"
    r"eslint|\.tsx?\(\d+|\.jsx?\(\d+|:\d+:\d+|error TS\d|"
    r"cannot read|is not defined|is not a function|is not defined)",
    re.IGNORECASE,
)
_SELFCONTAINED_CODE_RE = re.compile(r"(```|<\w+[\s>]|=>|\{[^}]*\}|=\s*['\"]|;)")


def _is_selfcontained_analysis(user_input: str) -> bool:
    """True si la pregunta trae el error y código suficiente inline para
    responder sin explorar el repo."""
    text = user_input.strip()
    if len(text) < 40:
        return False
    if not _SELFCONTAINED_ERROR_RE.search(text):
        return False
    return bool(_SELFCONTAINED_CODE_RE.search(text))


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


_DISABLED_RE = re.compile(r"disabled\s*=\s*\{([^}]*)\}")


def _extract_edit_instruction(analysis: str, target_files: list[str]) -> str | None:
    """Extrae strings exactas de old_str y new_str del análisis para dar a
    edit_file. Esto fuerza al modelo a ESCRIBIR con tool call, no con texto.
    Devuelve None si no se puede derivar con confianza."""
    if not analysis:
        return None
    matches = _DISABLED_RE.findall(analysis)
    if len(matches) < 1:
        return None
    old = f"disabled={{{matches[0]}}}"
    # Busca el nuevo disabled explícitamente (patrón "correcto:" o similar)
    new = None
    correct_match = re.search(r"correcto[::]?[^}]*\{([^}]*)\}", analysis, re.IGNORECASE)
    if correct_match:
        new = f"disabled={{{{correct_match.group(1)}}}}"
    else:
        # Heurística: si el análisis menciona 'documento' como campo faltante
        # y old no tiene || !documento.trim(), lo agregamos.
        if "documento" in analysis.lower() and "!documento" not in matches[0]:
            new = old.rstrip("}") + " || !documento.trim()}"
    if new is None:
        return None
    path = target_files[0] if target_files else ""
    return (
        "\n\n⛔ ACCIÓN OPERATIVA OBLIGATORIA — EJECUTÁ ESTE edit_file EXACTAMENTE\n"
        f"edit_file(path=\"{path}\", old_str=\"{old}\", new_str=\"{new}\")\n"
        "NOTA: old_str debe coincidir EXACTO con el código del archivo (incluyendo espacios).\n"
        "Si no coincide, leé el archivo con read_file y buscá la variante exacta.\n"
    )


def _build_chained_execute_suffix(task: str, target_files: list[str] | None = None) -> str:
    """Construye el mensaje EXECUTE cuando el usuario retoma un análisis previo
    con un comando vago ("implementa"). Incluye el path correcto y una
    instrucción edit_file operativa para forzar tool call."""
    files_line = ""
    if target_files:
        files_line = (
            "\nArchivo(s) objetivo (LEÉ ESTOS, NO explores, NO hagas list_files ni "
            "search_code en directorios):\n"
            + "\n".join(f"- {p}" for p in target_files[:5])
            + "\n"
        )
    edit_op = _extract_edit_instruction(task, target_files or [])
    if edit_op:
        # El sufijo es solo la instrucción operativa — el análisis está en historial
        return (
            "\n\n⛔ INSTRUCCIÓN (RETOMANDO ANÁLISIS PREVIO): "
            "El usuario te pidió implementar/arreglar algo analizado ANTES en esta "
            "conversación. EJECUTÁ ESTE edit_file EXACTAMENTE (NO respondas con texto):\n"
            "---\n"
            f"{edit_op}\n"
            "---\n"
            + files_line +
            "\nIMPORTANTE:\n"
            "- El archivo está en el historial con su código real. Leé con read_file si old_str no coincide.\n"
            "- NO uses list_files, search_code ni otros tools. Solo edit_file.\n"
        )
    else:
        # Sin edit_file operativo, fallback a análisis narrativo (menos efectivo con 35B)
        snippet = task if len(task) <= 2000 else task[:2000] + "\n...(truncado)"
        return (
            "\n\n⛔ INSTRUCCIÓN (RETOMANDO ANÁLISIS PREVIO): "
            "El usuario te pidió implementar/arreglar algo analizado ANTES en esta "
            "conversación. TU TAREA es la siguiente (del análisis previo):\n"
            "---\n"
            f"{snippet}\n"
            "---\n"
            + files_line
            + "\nIMPORTANTE:\n"
            "- Verificá SIEMPRE el path REAL del archivo antes de leerlo: en repos "
            "monorepo los archivos viven bajo frontend/src/ etc.\n"
            "- Aplicá el fix del hallazgo con edit_file o write_file.\n"
        )


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
    """Estimación del tamaño del contexto en tokens.

    OJO: el código/JSON tokeniza denso (~1.3-1.6 chars/token con Qwen), NO a
    4 chars/token. El factor antiguo (//4) SUBESTIMABA ~2.5-3x: E2E real, la
    request llegó a 63,022 tokens reales (superando n_ctx=62208 del servidor)
    cuando la estimación no superaba el umbral de summary → falló con error 400
    del LLM. Con //2 la estimación es conservadora y el summary (90% del límite)
    dispara ANTES de llenar el contexto físico, evitando el 400.
    """
    total = 0
    for m in messages:
        content = m.content if hasattr(m, "content") else str(m)
        total += len(str(content)) // 2
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
        # Tool calls reales del turno (los tool calls viven en el estado del
        # grafo, NO en self._messages — la compuerta de verificación escaneaba
        # self._messages y daba falsos positivos: "no corrió verify" cuando sí).
        self._called_tools: set[str] = set()
        # Resultado del diagnóstico de runtime del turno actual (True=entorno
        # sano, False=hallazgos, None=no se ejecutó). Lo usa _closing_message
        # para concluir "probablemente ya está resuelto" cuando corresponde.
        self._runtime_healthy: bool | None = None
        # Reporte completo del runtime (para la evidencia del cierre).
        self._runtime_report: str | None = None
        self._dedupe = ToolCallDedupe(max_repeats=1)
        self._explore_budget = ExploreBudget(
            max_calls=EXECUTE_EXPLORE_BUDGET,
            max_reads_after_explore=EXECUTE_MAX_READS_AFTER_EXPLORE,
            max_tools_before_write=EXECUTE_MAX_TOOLS_BEFORE_WRITE,
            productive_names=VERIFY_TOOL_NAMES,
            max_writes_before_verify=EXECUTE_MAX_WRITES_BEFORE_VERIFY,
            max_verify_before_write=EXECUTE_MAX_VERIFY_BEFORE_WRITE,
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
        # Tarea bulk detectada (≥ EXECUTE_BULK_MIN_FILES archivos): escala
        # budgets de EXECUTE y permite lecturas en el retry (0) para releer
        # los archivos que faltan.
        self._bulk_scope: int = 0
        # Cola bulk persistida (cache.db): hash de la tarea + seq del batch
        # que ESTE turno está ejecutando. Lo usa el auto-chaining al cerrar
        # el turno exitoso y el marcado de fallos.
        self._bulk_task_hash: str = ""
        self._bulk_current_seq: int = -1
        # Modo full-screen (--tui): stdin lo dueña la TUI de prompt_toolkit →
        # sin EscWatcher (ESC vía request_cancel) y sin prompts interactivos
        # de input() a mitad del turno (commit/credenciales).
        self._fullscreen: bool = False
        # Cancel del turno EN CURSO: lo setea run_turn; lo llama la TUI con ESC.
        self._turn_cancel = None
        # Límite de contexto VIVO: se detecta del server en start() (GET /props).
        # El config hardcodeado quedaba viejo (asumía -c 62000, había 36608) y
        # el summary automático nunca alcanzaba a disparar antes del overflow.
        self._ctx_limit: int = _CONTEXT_LIMIT
        # Razonamiento parcial del intento fallido (tail): el retry debe
        # CONTINUAR el diagnóstico, no reiniciarlo desde cero. E2E real: el
        # modelo encontró la causa raíz (route GET / sin query param), el
        # budget cortó, y el retry —sin sus conclusiones— re-derivó otra
        # hipótesis equivocada desde cero.
        self._partial_reasoning: str = ""

    def start(self) -> str:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self._tools, self._mcp_available = loop.run_until_complete(init_mcp())
            # n_ctx REAL del server (GET /props): el config hardcodeado quedaba
            # viejo y el summary automático no alcanzaba a disparar.
            from llm_wrapper import detect_context_limit
            detected_ctx = detect_context_limit(LLM_BASE_URL)
            if detected_ctx:
                self._ctx_limit = detected_ctx
            # Resolver UNA vez la key del knowledge graph del repo actual:
            # el 4B la inventa (trace_component fallaba y repetía la llamada).
            self._graph_project = ""
            try:
                from tools.graph_trace import _resolve_project_key
                by_name = {t.name: t for t in self._tools}
                self._graph_project = loop.run_until_complete(
                    _resolve_project_key(by_name, self.repo_path)
                )
            except Exception:
                pass
            self.current_role = Role.ANALYZE
            self._load_previous_sessions()
            self.agent, self._local_count, self._mcp_count = loop.run_until_complete(
                build_agent(
                    self.llm, Role.ANALYZE, self.repo_path,
                    self.cached_analysis, self._tools, self._dedupe,
                    self._explore_budget, self._analyze_budget,
                    tool_call_logger=self._called_tools,
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
                    tool_call_logger=self._called_tools,
                )
            )
        finally:
            loop.close()
        return True

    def _has_read_cache_content(self) -> bool:
        """True si el cache tiene contenido ÚTIL de archivos (no solo errores
        de trace_component ni entradas [trace:...])."""
        return any(
            not k.startswith("[") and len(v.strip()) > 30
            for k, v in self._read_cache.items()
        )

    def _rebuild_agent_write_only(self):
        """Reconstruye el agente EXECUTE para el retry: BUDGET_RETRY_TOOLS.

        read_file (ACOTADO por el budget) + edit_file + write_file. Sin
        delete_file (borrar+recrear bypassa el guard anti-sobrescritura — el
        35B borró __init__.py de 1851 líneas y escribió un stub), sin
        search_code (nada que explorar) y sin verify tools (el modelo las
        usaba como "acción gratis" para esquivar la write pressure). El
        contenido de los archivos ya leídos va inyectado en el mensaje
        (read_cache → anchor). La compuerta de verificación (sistema) inyecta
        verify después. El escalamiento a write_file completo queda
        DESHABILITADO en este retry (allow_overwrite_escalation=False):
        sobrescribir de memoria destruye aunque haya lecturas acotadas.
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
                    tools_override=BUDGET_RETRY_TOOLS,
                    force_tool_calls=True,
                    read_cache=self._read_cache,
                    tool_call_logger=self._called_tools,
                    allow_overwrite_escalation=False,
                )
            )
        finally:
            loop.close()

    def _inject_verify_gate(self) -> None:
        """Inyecta la compuerta de verificación (obliga a correr
        run_lint/run_tests/run_build) y reconstruye el agente gate-retry."""
        self._messages.append(HumanMessage(
            "⚠️ No ejecutaste run_lint, run_tests ni run_build.\n"
            "Es OBLIGATORIO verificar que el código compila y pasa "
            "tests ANTES de dar la tarea por terminada.\n\n"
            "Ejecutá AHORA (no respondas con texto — ejecutá las "
            "tools directamente):\n"
            f"  run_lint(path=\"{self.repo_path}\")\n"
            f"  run_tests(path=\"{self.repo_path}\")\n"
            f"  run_build(path=\"{self.repo_path}\")\n"
            "\nSi alguna falla, CORREGÍ el error y volvé a ejecutar "
            "la verificación hasta que las tres pasen."
        ))
        self._explore_budget.max_calls = 3
        self._explore_budget.max_reads_after_explore = 4
        self._explore_budget.max_tools_before_write = 6
        self._explore_budget.reset()
        self._dedupe.max_repeats = 2
        self._rebuild_agent_gate_retry()

    def _rebuild_agent_gate_retry(self):
        """Reconstruye EXECUTE para el retry de la compuerta post-escritura.

        DIFERENTE del retry write-only: corregir un error de compilación EXIGE
        ver el estado real del archivo. Sin read_file el 4B alucina old_str,
        edit_file falla, y termina reescribiendo el archivo entero de memoria
        (destructivo: perdió imports/hooks en PacientesPage.tsx).

        Usa GATE_RETRY_TOOLS: read_file + edit_file + verify, SIN búsqueda
        (list_files/search_code — el error ya viene inyectado) y SIN git-write
        (el fix no debe volver a commiteear). force_tool_calls=True para que
        actúe (read_file → edit_file) y no monologue.
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
                    read_cache=self._read_cache,
                    tools_override=GATE_RETRY_TOOLS,
                    force_tool_calls=True,
                    tool_call_logger=self._called_tools,
                )
            )
        finally:
            loop.close()

    def _check_context(self) -> str | None:
        """Verifica el contexto. Devuelve warning o None."""
        estimated = _estimate_tokens(self._messages)
        pct = estimated / self._ctx_limit
        if pct >= _SUMMARY_THRESHOLD:
            return "summary"
        if pct >= _WARNING_THRESHOLD:
            return "warning"
        return None

    def force_summarize(self):
        """Compacta el contexto AHORA: summary del historial + recorte.

        Expuesto como comando /compact en la TUI — el usuario no debería
        esperar al 90% automático si sabe que la sesión ya no aporta.
        """
        old_messages = self._messages[:-2]  # preservar últimos 2 mensajes
        recent = self._messages[-2:] if len(self._messages) >= 2 else self._messages
        summary = _generate_summary(self.llm, old_messages)
        self._messages = [
            SystemMessage(f"Resumen de la conversación previa:\n{summary}"),
            *recent,
        ]

    def _maybe_summarize(self):
        """Si el contexto llega al 90%, genera un summary y reemplaza historial viejo."""
        ctx_status = self._check_context()
        if ctx_status != "summary":
            return

        console.print("\n[yellow]📦 Contexto al 90% — generando resumen del historial…[/yellow]")
        self.force_summarize()
        console.print("[green]✅ Resumen generado. Historial comprimido.[/green]\n")

    def _findings_block(self) -> str:
        """Bloque con el razonamiento parcial del intento fallido (tail).

        El diagnóstico que el modelo ya hizo es el activo más valioso del
        retry: sin él, re-deriva hipótesis desde cero y a veces llega a una
        DISTINTA (E2E real: había encontrado la causa raíz correcta en el
        backend y el retry fue a 'arreglar' el frontend).
        """
        tail = (self._partial_reasoning or "").strip()
        if not tail:
            return ""
        if len(tail) > 4000:
            tail = "…(recortado: lo más reciente al final)…\n" + tail[-4000:]
        self._partial_reasoning = ""
        return (
            "\n\nHALLAZGOS DE TU ANÁLISIS EN CURSO (continuá EXACTAMENTE desde "
            "acá — NO reinicies el diagnóstico ni cambies de hipótesis):\n"
            + tail + "\n"
        )

    def _retry_with_read_anchor(self) -> str:
        """Construye el mensaje de retry: contenido de archivos ya leídos
        (ancla) + instrucción de escribir YA con el old_str literal del ancla.

        El retry usa BUDGET_RETRY_TOOLS (read_file ACOTADO + edit_file +
        write_file; sin delete_file): el ancla es la fuente principal de
        contenido, por eso va priorizada por tamaño (los helpers chicos
        primero; los monolitos no tapan al archivo del fix). read_file del
        retry permite releer bloques exactos si el ancla quedó corto."""
        anchor = ""
        already_committed = self._repo_has_recent_commit()
        if self._read_cache:
            blocks = []
            total = 0
            # Priorizar los archivos MÁS PEQUEÑOS: suelen ser los helpers de
            # implementación (donde vive el fix). Los monolitos (p. ej.
            # __init__.py con 2000+ líneas) consumían todo el presupuesto del
            # ancla y dejaban fuera al archivo que el modelo necesita editar.
            entries = sorted(
                self._read_cache.items(),
                key=lambda kv: (len(kv[1]), kv[0]),
            )
            for path, content in entries:
                if total >= 18000:
                    break
                take = min(len(content), 8000)
                if take <= 30:
                    continue
                blocks.append(
                    f"--- CONTENIDO DE {path} (ya leído — copiá el old_str "
                    f"LITERAL de acá) ---\n{content[:take]}\n--- FIN {path} ---"
                )
                total += take
            if blocks:
                anchor = (
                    "\n\nCONTENIDO DE ARCHIVOS YA LEÍDOS (usalo de referencia; "
                    "el old_str de edit_file debe ser literal):\n"
                    + "\n".join(blocks)
                )
        if already_committed:
            return (
                "\n\n⛔ RETRY: el intento anterior YA escribió y commiteó código "
                "(se detectó un commit reciente). NO reescribas los mismos archivos "
                "ni dupliques commits. "
                "Si todo quedó aplicado, terminá con un resumen breve; "
                "la verificación la inyecta el sistema. "
                "NO hagas write_file de archivos que ya modificaste." + anchor
            )
        return _EXECUTE_FORCE_WRITE_MSG + self._findings_block() + anchor

    def _enter_budget_retry(self, retry_msg: str) -> list:
        """Prepara el retry de EXECUTE tras un turno que no convergió (budget
        agotado, recursion limit o reasoning-only): acota las lecturas, resetea
        dedupe/budget, reconstruye el agente con BUDGET_RETRY_TOOLS
        (read_file acotado + edit_file + write_file; sin delete_file) y
        devuelve messages_for_agent para el siguiente intento.

        _called_tools.clear(): la compuerta final debe exigir verify en ESTE
        intento — no dejar pasar verify stale del intento anterior."""
        self._messages = self._messages[-3:] if len(self._messages) > 3 else self._messages
        self._messages.append(HumanMessage(self._retry_with_read_anchor()))
        messages_for_agent = list(self._messages)
        self._called_tools.clear()
        self._dedupe.reset()
        self._dedupe.max_repeats = 2
        self._explore_budget.max_calls = 0
        self._explore_budget.max_reads_after_explore = (
            _bulk_budget(self._bulk_scope)["max_reads_after_explore"]
            if self._bulk_scope >= EXECUTE_BULK_MIN_FILES
            else EXECUTE_MAX_READS_AFTER_EXPLORE
        )
        self._explore_budget.reset()
        self._explore_budget.limit_reads_now()
        self._rebuild_agent_write_only()
        console.print(f"\n[dim]↻ {retry_msg}[/dim]")
        return messages_for_agent

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
        findings = self._findings_block()
        if findings:
            retry_body += (
                findings
                + "\nRESPONDÉ AHORA el diagnóstico final en texto — ya no tenés "
                "tools de búsqueda y no las necesitás: todo lo que relevaste "
                "está arriba."
            )
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

    def _post_write_gate(self) -> tuple[bool, str]:
        """Verifica que el código recién escrito compile. Fail-open: cualquier
        excepción o escenario no soportado devuelve (True, '') para no bloquear."""
        try:
            from verify_gate import syntax_gate
            return syntax_gate(self.repo_path)
        except Exception:
            return True, ""

    def _verify_tools_called(self, messages: list | None = None) -> bool:
        """True si run_lint, run_tests o run_build fue llamado en el turno actual.

        Usa ``self._called_tools`` (set que alimenta el wrapper de tools en
        cada invocación real). Antes escaneaba ``self._messages`` — pero los
        tool calls viven en el estado del grafo, NO en el historial de la
        sesión, así que la compuerta daba falsos positivos y disparaba un
        turno extra de verificación aunque el modelo YA había verificado."""
        return bool(self._called_tools & VERIFY_TOOL_NAMES)

    def _closing_message(self, base: str) -> str:
        """Mensaje de cierre contexto-dependiente: si el entorno fue chequeado
        y está SANO, y el turno no logró escribir, la conclusión honesta es
        que el problema probablemente ya está resuelto — no "no logré escribir
        el fix" a secas (feedback real del usuario: el bug estaba arreglado y
        el agente nunca se lo dijo). Incluye EVIDENCIA de lo revisado:
        archivos leídos, herramientas usadas y el reporte de runtime."""
        if not self._runtime_healthy:
            return base
        msg = (
            "\n\n✓ No se aplicaron cambios de código. El entorno está SANO "
            "(backend responde, sin containers Docker pisando puertos) y "
            "no se encontró un bug evidente — es probable que el problema "
            "ya esté resuelto.\n"
            "Probá la acción de nuevo en tu app. Si el error persiste, "
            "decime exactamente qué respuesta ves (mensaje, pantalla, "
            "endpoint) y lo investigo más fino."
        )
        evidence: list[str] = []
        if self._runtime_report:
            evidence.append(
                "Runtime:\n"
                + "\n".join(f"    {ln}" for ln in self._runtime_report.splitlines())
            )
        files = sorted(
            k for k in self._read_cache
            if k and not k.startswith("[") and not k.startswith("(")
        )
        if files:
            evidence.append(
                "Archivos inspeccionados:\n"
                + "\n".join(f"    - {f}" for f in files[:12])
                + ("\n    …" if len(files) > 12 else "")
            )
        tools = sorted(self._called_tools)
        if tools:
            evidence.append("Herramientas usadas: " + ", ".join(tools))
        if evidence:
            msg += "\n\n📋 Evidencia de lo revisado:\n" + "\n".join(evidence)
        return msg

    def _maybe_ask_commit(self, user_input: str) -> None:
        """Tras un turno EXECUTE con cambios sin commitear, preguntar al usuario.

        Nunca commit automático (estilo aider con /undo no aplica acá: el
        usuario quiere control). Si stdin no es tty (tests/scripts) o el repo
        no es git, se omite silenciosamente. Fail-open: nunca rompe el flujo.
        """
        if not EXECUTE_ASK_COMMIT or not sys.stdin.isatty() or self._fullscreen:
            if self._fullscreen and EXECUTE_ASK_COMMIT:
                console.print(
                    "[dim]ℹ️  Modo --tui: commit manual al terminar el turno "
                    "(git add + git commit). El prompt interactivo está "
                    "deshabilitado.[/dim]"
                )
            return
        try:
            import subprocess
            proc = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self.repo_path, capture_output=True, text=True, timeout=5,
            )
            if proc.returncode != 0 or not proc.stdout.strip():
                return
        except Exception:
            return

        try:
            answer = input("📦 Hay cambios sin commitear. ¿Los commiteo? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return
        if answer not in ("y", "yes", "s", "si", "sí"):
            console.print("[dim]OK, no se commitea.[/dim]")
            return

        try:
                # git add -u: SOLO cambios en archivos TRACKED. Los untracked
                # quedan fuera A PROPÓSITO: E2E real Task 7 — 'git add -A'
                # stageó rules_catalog/.cursor/mcp.json con el CLIENT_ID de
                # New Relic (credencial) y casi se commitea. Sobre archivos
                # nuevos decide siempre el usuario, explícitamente.
                subprocess.run(
                    ["git", "add", "-u"],
                    cwd=self.repo_path, capture_output=True, text=True, timeout=10,
                )
                message = _build_commit_message(user_input)
                result = subprocess.run(
                    ["git", "commit", "-m", message],
                    cwd=self.repo_path, capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0:
                    console.print(f"[green]✅ Commit creado: {message}[/green]")
                    try:
                        left = subprocess.run(
                            ["git", "status", "--porcelain"],
                            cwd=self.repo_path, capture_output=True, text=True, timeout=5,
                        ).stdout
                        untracked = [
                            ln[3:] for ln in left.splitlines() if ln.startswith("??")
                        ]
                        if untracked:
                            console.print(
                                "[yellow]⚠️  Quedaron SIN commitear (untracked — "
                                "revisá antes de agregarlos a mano):[/yellow]"
                            )
                            for u in untracked:
                                console.print(f"   · {u}")
                    except Exception:
                        pass
                else:
                    console.print(f"[dim]Commit falló: {(result.stderr or '').strip()[:200]}[/dim]")
        except Exception as e:
            console.print(f"[dim]Commit falló: {e}[/dim]")

    def run_turn(self, user_input: str, status: str | None = None) -> None:
        """Clasifica, cambia rol, ejecuta con historial, persiste en SQLite."""
        self._no_explore_retry = False
        intent = classify_intent(self.llm, user_input)
        new_role = role_for_intent(intent)

        # Continuación: palabras como "continuar", "sigue", "dale" no son
        # verbos EXECUTE pero el usuario claramente quiere seguir con lo
        # que estaba haciendo. Mantener el rol del turno anterior si era
        # EXECUTE o REVIEW (nunca forzar ANALYZE/PLAN).
        if intent == Intent.ANALYZE and self.current_role in (Role.EXECUTE, Role.REVIEW):
            prefix = _extract_command_prefix(user_input).lower().strip()
            if prefix in _CONTINUATION_WORDS:
                new_role = self.current_role

        # Pregunta autocontenida (error + código inline): ANALYZE no necesita
        # explorar. El Qwen3.5 razonador responde BIEN y rápido single-shot con
        # 0 tools; con tools entra en loop exploración+reasoning y cuelga minutos.
        # Reconstruimos con no_explore desde el inicio → path directo.
        selfcontained = (
            new_role == Role.ANALYZE and _is_selfcontained_analysis(user_input)
        )
        role_changed = self._rebuild_agent(new_role, no_explore=selfcontained)
        role_label = _ROLE_LABELS.get(new_role, "")

        if role_changed and new_role != Role.CHAT:
            print_role_switch(role_label, self._local_count, self._mcp_count)

        if status:
            print(status, flush=True)

        if selfcontained:
            console.print("[dim]📐 Pregunta autocontenida — respondiendo directo (explore=0).[/dim]\n")

        # Reset dedupe + explore budget cada turno (siempre restaurar defaults)
        self._dedupe.reset()
        self._explore_budget.reset()
        self._analyze_budget.reset()
        self._called_tools.clear()
        self._runtime_healthy = None
        self._runtime_report = None
        # Los overrides de write_file (habilitados tras fallar la cirugía fina
        # de edit_file) son por TURNO: limpiar para que el próximo turno
        # arranque con los guards de sobrescritura activos.
        try:
            from tools.filesystem import clear_write_overrides
            clear_write_overrides()
        except Exception:
            pass
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
            # EXECUTE SIEMPRE recibe el mapa del repo (layout + símbolos), haya
            # paths citados o no. Sin esto, el modelo chico está ciego a la
            # estructura (E2E f2: leyó la raíz del repo como archivo 3 veces)
            # y quema el presupuesto explorando. Escalable: aplica a cualquier
            # repo, cualquier stack.
            if "CONTEXTO DE REPO PRECARGADO" not in agent_input:
                repo_hints = inject_repo_hints(self.repo_path)
                if repo_hints:
                    agent_input = agent_input + "\n\n" + repo_hints
            if self._graph_project:
                # El 4B inventa la key del grafo y trace_component falla;
                # inyectarla determinísticamente evita el loop de reintentos.
                agent_input = (
                    f"{agent_input}\n\n[KNOWLEDGE GRAPH] Project key de este repo: "
                    f"'{self._graph_project}'. Si usás trace_component, pasá "
                    f"project='{self._graph_project}' (o omitilo: el sistema "
                    f"lo resuelve solo)."
                )
            # Detección + FIX determinístico de mismatch frontend↔backend.
            # El modelo chico no hace este diagnóstico cross-file y aplica
            # fixes mecánicos a medias (E2E: arregló 6 paths, se saltó 6).
            # El SISTEMA detecta y aplica el codemod; el modelo solo verifica.
            if detect_path_mismatches(self.repo_path):
                fix_report = apply_mismatch_fixes(self.repo_path)
                if fix_report:
                    agent_input = f"{agent_input}\n\n{fix_report}"
                    console.print("[dim]🔧 PATH FIX aplicado por el sistema (codemod determinístico).[/dim]\n")
            # Diagnóstico de RUNTIME: si el usuario reporta errores de servidor
            # ("Error interno del servidor", 500, no guarda...), el problema
            # puede ser del ENTORNO (container Docker con código viejo pisando
            # el puerto, dev server caído) — el modelo no puede descubrirlo
            # (sin shell) y el error handler esconde la causa real.
            # runtime_status SIEMPRE reporta: el modelo debe saber que el
            # entorno fue chequeado (tanto si está roto como si está sano).
            if any(k in _extract_command_prefix(user_input).lower() for k in _RUNTIME_ERROR_HINTS):
                runtime_report = runtime_status(self.repo_path)
                if runtime_report:
                    agent_input = f"{agent_input}\n\n{runtime_report}"
                    self._runtime_healthy = "entorno SANO" in runtime_report
                    self._runtime_report = runtime_report
                    console.print("[dim]🩺 RUNTIME: diagnóstico de puertos ejecutado.[/dim]\n")
            if agent_input != user_input:
                nums = extract_requested_task_numbers(user_input)
                scope = f" (solo Tarea(s) {', '.join(map(str, nums))})" if nums else ""
                hints_on = "CONTEXTO DE REPO PRECARGADO" in agent_input
                # Con hints ya inyectados, explore=0: cualquier list_files/search_code
                # lanza ToolBudgetExceeded (GraphBubbleUp) → propaga → retry write-only.
                # El 4B con max_calls=1 recibe strings STOP y los ignora, quemando el
                # recursion limit sin escribir. Con 0, la excepción corta de inmediato.
                if hints_on:
                    # Los hints cubren stack/config del repo, NO el archivo
                    # objetivo de la tarea. Budget acotado pero real: ubicar el
                    # componente + leer el archivo a tocar ANTES de la presión
                    # de escritura. Con max_calls=1 el modelo quemaba su única
                    # exploración (trace_component con nombre equivocado) y
                    # escribía de memoria — el guard lo bloqueó, pero alucinó
                    # clases CSS inexistentes (ConsultaTable sin estilos).
                    self._explore_budget.max_calls = 3
                    self._explore_budget.max_reads_after_explore = 8
                    self._explore_budget.max_tools_before_write = 12
                    self._dedupe.max_repeats = 1
                extra = " · explore=acotado (hints)" if hints_on else ""
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

        # Tareas bulk: escalar budgets de EXECUTE + dividir en batches con
        # cola persistida. El budget default corta una tarea de 14 templates a
        # mitad de las lecturas (max_tools_before_write=12 < 14 reads) → loop
        # de retries sin escribir (E2E real Task 8 spec-kitti). DEBE correr
        # ANTES del append: inyecta el alcance del batch en agent_input.
        if new_role == Role.EXECUTE:
            canonical = canonical_task_text(user_input)
            bulk = detect_bulk_file_count(canonical)
            if bulk >= EXECUTE_BULK_MIN_FILES:
                self._bulk_scope = bulk
                bb = _bulk_budget(bulk)
                self._explore_budget.max_reads_after_explore = bb["max_reads_after_explore"]
                self._explore_budget.max_tools_before_write = bb["max_tools_before_write"]
                self._explore_budget.max_writes_before_verify = bb["max_writes_before_verify"]
                targets = detect_bulk_targets(canonical, self.repo_path)
                if len(targets) >= EXECUTE_BULK_MIN_FILES:
                    th = bulk_task_hash(canonical, self.repo_path)
                    created = ensure_bulk_plan(th, split_into_batches(targets))
                    progress = bulk_progress(th)
                    cur = next_pending_batch(th)
                    total_b = progress["total"]
                    if cur:
                        self._bulk_task_hash = th
                        self._bulk_current_seq = cur["seq"]
                        mark_batch(th, cur["seq"], "in_progress")
                        agent_input += build_batch_scope(
                            cur["seq"], total_b, cur["files"]
                        )
                        console.print(
                            f"[dim]📦 Tarea bulk (~{bulk} archivos) dividida en "
                            f"{total_b} batches — batch {cur['seq'] + 1}: "
                            f"{len(cur['files'])} archivo(s). Progreso: "
                            f"{progress['done']} done · {progress['failed']} failed · "
                            f"{progress['pending']} pendientes.[/dim]\n"
                        )
                    elif created or progress["pending"] == 0:
                        console.print(
                            f"[dim]📦 Tarea bulk ya COMPLETA según la cola "
                            f"({progress['done']}/{total_b} batches done). Si "
                            f"querés re-ejecutarla, cambiá el texto del prompt.[/dim]\n"
                        )

        self._messages.append(HumanMessage(agent_input))

        reset_turn_usage()
        start = time.monotonic()
        recursion = (
            EXECUTE_RECURSION_LIMIT if new_role == Role.EXECUTE else AGENT_RECURSION_LIMIT
        )
        if self._bulk_scope >= EXECUTE_BULK_MIN_FILES:
            recursion = max(recursion, 2 * _bulk_budget(self._bulk_scope)["tool_calls_per_turn"] + 2)
        config = {
            "configurable": {"thread_id": f"session-{id(self)}"},
            "recursion_limit": recursion,
        }

        # Pasar todo el historial al agente
        messages_for_agent = list(self._messages)

        # 1 intento normal + hasta 2 retries write-only (si el primero tampoco
        # escribe, el 2do con instrucción aún más estricta)
        max_attempts = 1 + (2 if REASONING_RETRY_ENABLED else 0)
        if new_role == Role.EXECUTE and self._bulk_scope >= EXECUTE_BULK_MIN_FILES:
            max_attempts = max(max_attempts, EXECUTE_BULK_MAX_ATTEMPTS)
        attempt = 0
        gate_retries = 0
        verify_injections = 0
        interrupted = False
        interrupted_by_esc = False
        auto_stopped = False
        # Turno terminó en FALLO (loop, recursion, error): los cambios pueden
        # estar incompletos/rotos sin pasar la compuerta de verificación →
        # NO se ofrece commit (E2E: recursion limit + commit de JSX corrupto).
        turn_failed = False

        while attempt < max_attempts:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            # require_write: EXECUTE desde el INTENTO 1 (EXECUTE_REQUIRE_WRITE):
            # si el modelo termina el turno en texto sin NINGUNA tool de
            # escritura, stream_agent_turn lanza ReasoningOnlyResponse → retry
            # write-only. Antes solo aplicaba a retries y el 4B "escapaba"
            # respondiendo un análisis sin tocar el repo.
            # REVIEW y ANALYZE/PLAN NUNCA se fuerzan a escribir.
            # EXCEPCIÓN BULK: en batches ya completos el cierre correcto es
            # "verifico (lint/tests) + resumen SIN edits" — exigir escritura
            # empuja al modelo a no-op edits en loop (E2E real Task 8 batch 1).
            require_write = (
                new_role == Role.EXECUTE
                and EXECUTE_REQUIRE_WRITE
                and REASONING_RETRY_ENABLED
                and not self._bulk_task_hash
            ) or (
                attempt > 0
                and REASONING_RETRY_ENABLED
                and new_role == Role.REVIEW
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
                    # EXECUTE: 90s por bloque de razonamiento (el 4B razona
                    # 30-60s antes de cada tool call; si razona más, se colgó).
                    max_reasoning_seconds=(
                        None if self._no_explore_retry
                        else (
                            EXECUTE_MAX_REASONING_SECONDS
                            if new_role == Role.EXECUTE
                            else MAX_REASONING_SECONDS
                        )
                    ),
                    max_tool_calls=(
                        _bulk_budget(self._bulk_scope)["tool_calls_per_turn"]
                        if new_role == Role.EXECUTE and self._bulk_scope >= EXECUTE_BULK_MIN_FILES
                        else MAX_TOOL_CALLS_PER_TURN
                    ),
                    require_write=require_write,
                )
            )

            # Watcher de ESC: permite interrumpir el streaming y volver al prompt.
            # En full-screen stdin lo dueña la TUI → ESC llega por key binding
            # (request_cancel) y el watcher NO debe arrancar (competería por
            # los bytes del teclado con prompt_toolkit).
            watcher = None
            if not self._fullscreen:
                watcher = EscWatcher(
                    cancel_cb=lambda: loop.call_soon_threadsafe(task.cancel)
                )
                watcher.start()
            self._turn_cancel = lambda: loop.call_soon_threadsafe(task.cancel)

            try:
                loop.run_until_complete(task)
                self._last_response = task.result() if not task.cancelled() else ""
                # BULK sin escrituras: válido SOLO si verificó (lint/tests/
                # build). "Leí los 5 archivos, ya cumplen, tests verdes" es un
                # cierre legítimo del batch (E2E Task 8 batch 1 ya-completo).
                # Sin escritura Y sin verificación = modelo vago → mismo
                # tratamiento que no-write (retry / fallo al agotar intentos).
                if (
                    new_role == Role.EXECUTE
                    and self._bulk_task_hash
                    and not (self._called_tools & WRITE_TOOL_NAMES)
                    and not self._verify_tools_called()
                ):
                    raise ReasoningOnlyResponse(
                        self._last_response or "",
                        reason="no-write",
                    )
                # Compuerta post-escritura (EXECUTE): si el código escrito no
                # compila y el error apunta a un archivo que tocamos, reintentar
                # UNA vez inyectando el error exacto. Red de seguridad para LLM
                # chicos que escriben código roto sin verificarlo.
                if (
                    new_role == Role.EXECUTE
                    and POST_WRITE_GATE_ENABLED
                    and gate_retries < POST_WRITE_GATE_MAX_RETRIES
                ):
                    gate_ok, gate_err = self._post_write_gate()
                    if not gate_ok:
                        gate_retries += 1
                        # Foto del contenido original ANTES de que el modelo
                        # dañara los archivos: la inyectamos en el mensaje
                        # para que el retry pueda restaurar funcionalidad
                        # perdida (botones, estado, handlers).
                        _orig_snapshot = ""
                        for cp, cc in self._read_cache.items():
                            if not cp.startswith("[") and len(cc.strip()) > 30:
                                _orig_snapshot += (
                                    f"\n--- {cp} (CONTENIDO ORIGINAL antes de tu escritura) ---\n"
                                    f"{cc}\n"
                                )
                        _orig_block = ""
                        if _orig_snapshot:
                            _orig_block = (
                                "\n\n⚠️ CONTENIDO ORIGINAL de los archivos "
                                "(leído ANTES de que escribieras — PRESERVÁ "
                                "TODA esta funcionalidad: botones, estado, "
                                "handlers, SVG, imports):\n"
                                f"{_orig_snapshot}\n"
                            )
                        self._messages.append(HumanMessage(
                            "⛔ El código que acabás de escribir NO compila.\n"
                            f"Error del build:\n{gate_err}\n"
                            f"{_orig_block}\n"
                            "Corregí SOLO el error de build, PRESERVANDO toda "
                            "la funcionalidad del contenido original:\n"
                            "1) Leé el archivo indicado con read_file para ver su estado EXACTO.\n"
                            "2) Aplicá el fix con edit_file (old_str/new_str copiados del "
                            "contenido REAL del archivo).\n"
                            "3) NO borres componentes que estaban en el original (botones, "
                            "estado, handlers, SVG, imports) — solo arreglá el build.\n"
                            "write_file está BLOQUEADO para archivos existentes "
                            "(la tool lo rechaza): NO reescribas archivos enteros.\n"
                            "NO respondas con texto ni repitas el análisis: ejecutá "
                            "read_file → edit_file ahora."
                        ))
                        messages_for_agent = list(self._messages)
                        # Budget ajustado: sin tools de búsqueda en GATE_RETRY_TOOLS,
                        # pero read_file sí está limitado post-explore; reset con
                        # margen controlado para lecturas del fix (no infinito).
                        self._explore_budget.max_calls = 3
                        self._explore_budget.max_reads_after_explore = 4
                        self._explore_budget.max_tools_before_write = 6
                        self._explore_budget.reset()
                        self._dedupe.max_repeats = 2
                        self._rebuild_agent_gate_retry()
                        console.print(
                            "\n[yellow]🔧 Compuerta: el build falló tras la escritura. "
                            "Reintentando el fix con read_file + edit_file…[/yellow]\n"
                        )
                        continue
                # Compuerta de verificación (EXECUTE): si el modelo NO corrió
                # run_lint / run_tests / run_build, inyectamos un gate que lo
                # obliga a verificar. El 4B/9B tiende a saltarse la verificación
                # y responder LISTO sin validar que el código compila.
                if (
                    new_role == Role.EXECUTE
                    and gate_retries < POST_WRITE_GATE_MAX_RETRIES
                    and not self._verify_tools_called(messages_for_agent)
                ):
                    gate_retries += 1
                    self._inject_verify_gate()
                    # Bug latente: messages_for_agent quedaba STALE y el gate
                    # NUNCA llegaba al modelo — la verificación era un no-op.
                    messages_for_agent = list(self._messages)
                    console.print(
                        "\n[dim]↻ Verificando los cambios (lint/tests/build)…[/dim]\n"
                    )
                    continue
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
                # Full-screen: el watcher es None (stdin lo dueña la TUI) y
                # TODO cancel acá viene de ESC vía request_cancel. En modo
                # simple el EscWatcher es quien marca interrupted.
                interrupted_by_esc = (
                    True if watcher is None else watcher.interrupted.is_set()
                )
                break
            except VerifyRequired as e:
                # El modelo escribió N veces sin verificar: inyectar la
                # compuerta de verificación AHORA (GATE_RETRY_TOOLS tiene
                # run_lint/run_tests/run_build). El retry write-only NO tiene
                # verify tools y chocaría con el mismo tope al instante.
                if new_role != Role.EXECUTE or verify_injections >= VERIFY_GATE_MAX_INJECTIONS:
                    turn_failed = True
                    interrupted = True
                    auto_stopped = True
                    print(
                        self._closing_message(
                            "\n\n↻ El modelo escribió demasiado sin verificar "
                            "y la compuerta de verificación se inyectó "
                            f"{verify_injections} veces sin converger. "
                            "Reintentá con un prompt más específico "
                            "(archivo, endpoint o línea concreta)."
                        ),
                        flush=True,
                    )
                    break
                verify_injections += 1
                self._called_tools.clear()
                self._inject_verify_gate()
                messages_for_agent = list(self._messages)
                console.print(
                    "\n[yellow]🔧 El modelo escribió sin verificar. Inyectando "
                    "compuerta de verificación (lint/tests/build)…[/yellow]\n"
                )
                continue
            except ToolBudgetExceeded as e:
                if attempt + 1 >= max_attempts:
                    turn_failed = True
                    interrupted = True
                    auto_stopped = True
                    print(
                        self._closing_message(
                            "\n\n↻ Este turno no logró escribir el fix. "
                            "Podés reintentar con un prompt más específico "
                            "(archivo, endpoint o línea concreta) o pedir un "
                            "análisis primero."
                        ),
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
                messages_for_agent = self._enter_budget_retry(
                    "Presupuesto de exploración agotado. Reintentando "
                    "con lectura acotada + escritura…"
                )
                continue
            except ReasoningOnlyResponse as e:
                # Rescatar el razonamiento parcial ANTES de cualquier retry:
                # es la materia prima del bloque HALLAZGOS.
                self._partial_reasoning = (e.reasoning_text or "").strip()[-4500:]
                if attempt + 1 >= max_attempts:
                    turn_failed = True
                    auto_stopped = True
                    if isinstance(e, ToolCallLimitExceeded):
                        print(
                            f"\n\n⚠️  El modelo hizo {e.total_calls} tool calls (límite {e.limit}) "
                            "y entró en loop. Reinicia con /new o reduce el prompt.",
                            flush=True,
                        )
                    elif getattr(e, "reason", "") == "no-write":
                        print(
                            self._closing_message(
                                "\n\n↻ El modelo no logró escribir el cambio. "
                                "Reintentá con un prompt más específico "
                                "(archivo o línea concreta) o pedí un análisis primero."
                            ),
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
                if isinstance(e, ToolCallLimitExceeded):
                    retry_msg = (
                        f"Muchas tool calls seguidas ({e.total_calls}). "
                        "Reintentando con lectura acotada + escritura…"
                    )
                elif getattr(e, "reason", "") == "no-write":
                    retry_msg = "Reintentando con lectura acotada + escritura…"
                else:
                    retry_msg = (
                        f"El modelo gastó {len(e.reasoning_text)} chars razonando. "
                        "Reintentando con lectura acotada + escritura…"
                    )
                messages_for_agent = self._enter_budget_retry(retry_msg)
                continue
            except Exception as e:
                name = type(e).__name__
                err_text = str(e)
                # Error de GRAMMAR de llama.cpp (peg-gemma4): el modelo emitió
                # un tool call malformado tras escribir (con tool_choice=required
                # no puede terminar en texto y el retry sin verify no le da una
                # acción natural). Si YA escribió, el fix está aplicado: pasar
                # a las compuertas de verificación (que SÍ tienen verify tools).
                # Si NO escribió: retry con un nudge para que emita output válido.
                if "peg-gemma4" in err_text or "does not match the expected" in err_text:
                    wrote = bool(self._called_tools & WRITE_TOOL_NAMES)
                    if wrote and new_role == Role.EXECUTE:
                        # El modelo escribió y el parser de tool calls rechazó
                        # su respuesta de cierre (con tool_choice=required no
                        # puede terminar en texto). El fix quedó aplicado:
                        # seguimos con la compuerta de verificación real.
                        console.print(
                            "\n[green]✓ Cambios aplicados. Verificando el resultado…[/green]"
                        )
                        if gate_retries < POST_WRITE_GATE_MAX_RETRIES and not (
                            self._verify_tools_called(messages_for_agent)
                        ):
                            gate_retries += 1
                            self._inject_verify_gate()
                            # Bug latente: el mensaje de la compuerta se
                            # agregaba a self._messages pero messages_for_agent
                            # quedaba STALE → el gate NUNCA llegaba al modelo.
                            messages_for_agent = list(self._messages)
                            console.print(
                                "\n[dim]↻ Verificando los cambios (lint/tests/build)…[/dim]\n"
                            )
                            continue
                        break
                    if attempt + 1 >= max_attempts:
                        turn_failed = True
                        print(
                            self._closing_message(
                                "\n\n↻ El modelo tuvo problemas para emitir "
                                "una respuesta válida (parser de tool calls). "
                                "Reintentá con un prompt más específico o pedí "
                                "un análisis primero."
                            ),
                            flush=True,
                        )
                        break
                    attempt += 1
                    self._messages.append(HumanMessage(
                        "⚠️ Tu último output no fue válido (el parser de tool calls "
                        "lo rechazó). Respondé con UNA tool call VÁLIDA: "
                        "write_file/edit_file/read_file (retry de budget; "
                        "delete_file NO está disponible)."
                    ))
                    messages_for_agent = list(self._messages)
                    self._called_tools.clear()
                    self._dedupe.reset()
                    self._explore_budget.max_calls = 0
                    self._explore_budget.max_reads_after_explore = EXECUTE_MAX_READS_AFTER_EXPLORE
                    self._explore_budget.reset()
                    self._explore_budget.limit_reads_now()
                    self._rebuild_agent_write_only()
                    console.print(
                        "\n[dim]↻ Reintentando con lectura acotada + escritura…[/dim]"
                    )
                    continue
                if "Recursion" in name or "recursion" in err_text.lower():
                    lim = (
                        EXECUTE_RECURSION_LIMIT
                        if new_role == Role.EXECUTE
                        else AGENT_RECURSION_LIMIT
                    )
                    if attempt + 1 >= max_attempts:
                        turn_failed = True
                        print(
                            self._closing_message(
                                f"\n\n↻ El turno se alargó demasiado ({lim} pasos) sin "
                                "completar. Reintentá con un prompt más específico "
                                "(archivo o endpoint concreto)."
                            ),
                            flush=True,
                        )
                    elif new_role == Role.EXECUTE:
                        # El modelo suele quedar a 1-2 pasos de terminar (E2E
                        # real: murió en la tool #15 = run_tests, justo antes
                        # de ver el resultado). En vez de fallar el turno,
                        # reintentar con el agente de budget (read acotado +
                        # edit + write). Los cambios ya aplicados siguen en
                        # disco; el ancla inyecta el contenido leído.
                        attempt += 1
                        messages_for_agent = self._enter_budget_retry(
                            f"El turno se alargó demasiado ({lim} pasos de "
                            "langgraph). Reintentando con lectura acotada + "
                            "escritura…"
                        )
                        continue
                    else:
                        turn_failed = True
                        print(
                            self._closing_message(
                                f"\n\n↻ El turno se alargó demasiado ({lim} pasos) sin "
                                "completar. Reintentá con un prompt más específico "
                                "(archivo o endpoint concreto)."
                            ),
                            flush=True,
                        )
                else:
                    turn_failed = True
                    print(f"\n\n❌ Error en la iteración: {e}", flush=True)
                break
            finally:
                if watcher is not None:
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

        # Summary del turno: en modo --tui (full-screen) se omite — el panel
        # queda limpio solo con "vos ›" + respuesta del LLM; el summary era
        # ruido sin aporte (el toolbar ya muestra tokens en vivo).
        if not self._fullscreen:
            print_turn_summary(
                elapsed,
                interrupted,
                self._session_time,
                interrupt_source=(
                    "ESC" if interrupted_by_esc
                    else ("auto — límite de intentos" if auto_stopped else None)
                ),
            )
        elif interrupted_by_esc:
            # En full-screen el summary se omite, pero el cancelo hay que
            # informarlo: si no, la respuesta queda cortada a mitad sin
            # explicación y parece un bug del streaming.
            console.print(
                "\n[yellow]⏹️  Turno cancelado (ESC). Podés seguir con otro "
                "prompt.[/yellow]\n"
            )

        # Bulk: contabilidad del batch ANTES del commit-ask (el estado en la
        # cola decide si este es el último batch → recién ahí se ofrece commit)
        _bulk_chain: tuple[str, int] | None = None
        if new_role == Role.EXECUTE and self._bulk_task_hash:
            th = self._bulk_task_hash
            seq = self._bulk_current_seq
            if not interrupted and not turn_failed:
                mark_batch(th, seq, "done")
                _bulk_chain = (th, seq)
            elif turn_failed:
                status = fail_or_keep_batch(th, seq, BULK_MAX_BATCH_ATTEMPTS)
                if status == "failed":
                    console.print(
                        f"\n[bold red]⛔ Batch {seq + 1} marcado FAILED tras "
                        f"{BULK_MAX_BATCH_ATTEMPTS} intentos. Revisá esos "
                        "archivos; la cola sigue en cache.db para reanudar.[/bold red]\n"
                    )
                    self._bulk_task_hash = ""

        # Commit preguntado: tras un turno EXECUTE EXITOSO, si hay cambios
        # sin commitear y el usuario no interrumpió, ofrecer commitear.
        # Turnos FALLIDOS (loop/recursion/error) pueden dejar el árbol roto
        # sin pasar la compuerta de verificación → NO se ofrece commit
        # (E2E: JSX corrupto commiteado tras recursion limit).
        # En tareas bulk se pregunta SOLO al cerrar el último batch.
        _bulk_more_pending = (
            _bulk_chain is not None
            and next_pending_batch(_bulk_chain[0]) is not None
        )
        if new_role == Role.EXECUTE and not interrupted and not turn_failed:
            if not _bulk_more_pending:
                self._maybe_ask_commit(user_input)
        elif new_role == Role.EXECUTE and turn_failed and not interrupted:
            # Turno fallido: además de no ofrecer commit, verificar si el árbol
            # quedó con código ROTO (archivo truncado/sintaxis inválida). El
            # write_file ahora avisa INTEGRIDAD/SINTAXIS, pero si el modelo igual
            # cerró, esta compuerta detecta y REPORTE el archivo exacto para que
            # el usuario no commitee código roto a ciegas (E2E real: e2e_verify.sh
            # truncado sin que nadie lo notara).
            gate_ok, gate_err = self._post_write_gate()
            if not gate_ok:
                console.print(
                    "\n[bold red]⛔ El turno falló y además el código quedó ROTO.[/bold red]\n"
                    f"{gate_err}\n"
                    "[dim]Revisá y corregí el archivo señalado ANTES de commitear.[/dim]\n"
                )
            else:
                console.print(
                    "\n[dim]↻ Turno fallido (sin verificación) — no se ofrece "
                    "commit. Revisá los cambios antes de commitearlos.[/dim]"
                )
            # CREDENCIALES / ENTORNO EXTERNO: si el turno falló (probablemente
            # porque la tarea requiere una credencial/entorno que el agente no
            # tiene), invitar al usuario a proveerla o pedir los pasos manuales.
            # Solo en modo interactivo (TTY); fail-open si no hay stdin.
            # En full-screen lo saltea: stdin pertenece a la TUI.
            if sys.stdin.isatty() and not self._fullscreen:
                try:
                    console.print(
                        "\n[bold cyan]🔑 Si la tarea requiere una credencial o entorno "
                        "externo (API key, servicio cloud, VM, cuenta, etc.) que el "
                        "agente no tiene:[/bold cyan]"
                        "\n  · pegala acá (p. ej. NEW_RELIC_API_KEY=... y reintentá), o"
                        "\n  · pedile al agente que te explique los pasos manuales."
                    )
                    answer = input("› ").strip()
                    if answer:
                        # Reintento el turno con la credencial como nuevo input
                        # (el usuario pegó algo, p. ej. una variable de entorno).
                        console.print(
                            f"[dim]Credencial recibida — reintentando con tu input…[/dim]\n"
                        )
                        # Procesar el input como un nuevo turno
                        self.run_turn(answer)
                except (EOFError, KeyboardInterrupt):
                    console.print()

        # ── Bulk: auto-chaining del próximo batch ───────────────────────────
        # El batch exitoso ya quedó 'done' arriba; si quedan pendientes,
        # rotar contexto (si >75%) y ejecutar el siguiente AHORA. La recursión
        # está acotada por la cantidad de batches (cada llamada consume uno).
        # Interrupción (ESC/Ctrl+C) → el batch queda reanudable en la cola.
        if _bulk_chain is not None:
            th = _bulk_chain[0]
            nxt = next_pending_batch(th)
            if nxt is None:
                p = bulk_progress(th)
                # Solo el ÚLTIMO batch ejecutado anuncia el cierre (los marcos
                # externos de la recursión se desenrollan sin repetirlo).
                if (
                    p["done"] == p["total"] and p["total"] > 0
                    and seq == p["total"] - 1
                ):
                    console.print(
                        f"\n[bold green]✅ Tarea bulk COMPLETA: "
                        f"{p['done']}/{p['total']} batches.[/bold green]\n"
                    )
            else:
                if (
                    _estimate_tokens(self._messages) / self._ctx_limit
                    >= BULK_SESSION_ROTATION_CTX
                ):
                    console.print(
                        "\n[yellow]🔄 Contexto alto entre batches — "
                        "rotando sesión (la cola persiste en SQLite)…[/yellow]\n"
                    )
                    self.reset()
                nxt_input = canonical_task_text(user_input) + build_batch_scope(
                    nxt["seq"], bulk_progress(th)["total"], nxt["files"]
                )
                console.print(
                    f"[dim]📦 Batch {nxt['seq'] + 1}/"
                    f"{bulk_progress(th)['total']} — continuando automáticamente…[/dim]\n"
                )
                self.run_turn(nxt_input)

        ctx_status = self._check_context()
        if ctx_status == "warning":
            pct = _estimate_tokens(self._messages) / self._ctx_limit * 100
            console.print(
                f"\n[yellow]⚠️  Contexto al {pct:.0f}% (límite {self._ctx_limit:,} tokens) — "
                f"escribí /compact para resumir ahora o /new para empezar limpio[/yellow]\n"
            )
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

    def request_cancel(self) -> None:
        """Cancela el turno en curso (lo llama la TUI full-screen con ESC).

        El callback usa call_soon_threadsafe, así que es seguro invocarlo
        desde el hilo de la UI mientras run_turn corre en otro thread.
        """
        cb = self._turn_cancel
        if cb is not None:
            try:
                cb()
            except Exception:
                pass

    def context_usage_pct(self) -> float:
        """% del ctx_limit consumido por la sesión actual (para /compact, TUI)."""
        return _estimate_tokens(self._messages) / self._ctx_limit * 100

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
