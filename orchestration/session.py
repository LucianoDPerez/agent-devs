from __future__ import annotations

import asyncio
import contextlib
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from cache import (
    bulk_progress,
    ensure_bulk_plan,
    fail_or_keep_batch,
    load_recent_turns,
    load_session_turns,
    mark_batch,
    next_pending_batch,
    save_turn,
)
from config import (
    AGENT_RECURSION_LIMIT,
    ANALYZE_EXPLORE_BUDGET,
    ANALYZE_MAX_READS_AFTER_EXPLORE,
    BULK_MAX_BATCH_ATTEMPTS,
    BULK_SESSION_ROTATION_CTX,
    EXECUTE_ASK_COMMIT,
    EXECUTE_BULK_MAX_ATTEMPTS,
    EXECUTE_BULK_MIN_FILES,
    EXECUTE_CONFIRM_TIMEOUT,
    EXECUTE_CONFIRM_WRITES,
    EXECUTE_EXPLORE_BUDGET,
    EXECUTE_MAX_CONTENT_SECONDS,
    EXECUTE_MAX_READS_AFTER_EXPLORE,
    EXECUTE_MAX_REASONING_CHARS,
    EXECUTE_MAX_REASONING_SECONDS,
    EXECUTE_MAX_TOOLS_BEFORE_WRITE,
    EXECUTE_MAX_VERIFY_BEFORE_WRITE,
    EXECUTE_MAX_WRITES_BEFORE_VERIFY,
    EXECUTE_RECURSION_LIMIT,
    EXECUTE_REQUIRE_WRITE,
    JUDGE_BASE_URL,
    JUDGE_ENABLED,
    JUDGE_MAX_TOKENS,
    JUDGE_MODEL_NAME,
    JUDGE_TEMPERATURE,
    LLM_BASE_URL,
    MAX_REASONING_CHARS,
    MAX_REASONING_SECONDS,
    MAX_TOOL_CALLS_PER_TURN,
    PATH_FIX_ENABLED,
    PLAN_EXPLORE_BUDGET,
    PLAN_MAX_READS_AFTER_EXPLORE,
    POST_WRITE_GATE_ENABLED,
    POST_WRITE_GATE_MAX_RETRIES,
    PROTECTED_TASK_DIRS,
    PROTECTED_TASK_FILENAMES,
    REASONING_RETRY_ENABLED,
    REVIEW_EXPLORE_BUDGET,
    REVIEW_MAX_READS_AFTER_EXPLORE,
    REVIEW_MAX_TOOLS_BEFORE_WRITE,
    TURN_IDLE_TIMEOUT,
    VERIFY_GATE_MAX_INJECTIONS,
)
from core.intents import Intent
from core.roles import Role, role_for_intent
from core.textutil import normalize
from display.console import (
    ReasoningOnlyResponse,
    ToolCallLimitExceeded,
    console,
    print_role_switch,
    print_turn_summary,
    stream_agent_turn,
)
from display.esc_watcher import EscWatcher
from llm_wrapper import LocalLLM, get_usage, reset_turn_usage
from orchestration.agent_builder import build_agent, init_mcp
from orchestration.bulk_planner import (
    build_batch_scope,
    bulk_task_hash,
    canonical_task_text,
    detect_bulk_targets,
    split_into_batches,
)
from orchestration.execute_bootstrap import (
    _EVIDENCE_EXT_PATTERN,
    _collect_cited_paths,
    build_paste_correction_suffix,
    detect_bulk_file_count,
    extract_requested_task_numbers,
    inject_repo_hints,
    preload_cited_files,
    preload_for_analyze,
    preload_for_review,
)
from orchestration.path_mismatch import apply_mismatch_fixes, detect_path_mismatches
from orchestration.router import _extract_command_prefix, classify_intent
from orchestration.runtime_diagnostics import runtime_status
from orchestration.tool_dedupe import (
    EXPLORE_TOOL_NAMES,
    READISH_TOOL_NAMES,
    VERIFY_TOOL_NAMES,
    WRITE_TOOL_NAMES,
    ExploreBudget,
    ToolBudgetExceeded,
    ToolCallDedupe,
    VerifyRequired,
)
from tools import BUDGET_RETRY_TOOLS, GATE_RETRY_TOOLS


def _is_llama_connection_error(exc: BaseException) -> bool:
    """Detecta errores de conexión a llama-server sin importar el wrapper."""
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
        if "failed to connect" in msg:
            return True
        nxt = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)
        cur = nxt if isinstance(nxt, BaseException) else None
    return False


def _llama_down_console_msg() -> None:
    from config import LLM_BASE_URL as _BASE
    console.print(f"\n[red]❌ llama.cpp está apagado — no se pudo conectar a {_BASE}[/red]")
    console.print("[yellow]   Encendelo antes de seguir, por ejemplo:[/yellow]")
    console.print("[dim]     llama-server -hf bartowski/InternScience_Agents-A1-4B-GGUF:Q4_K_M --port 8080[/dim]")
    console.print("[dim]   Verificá con: agent-devs --doctor[/dim]\n")

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
    "5) Si varios campos van al MISMO archivo (ej. 5 campos de una misma "
    "interface/type), usá apply_patch con todos los cambios juntos — "
    "cuenta como 1 sola aprobación en vez de N.\n"
    "NO respondas con texto: ejecutá write_file/edit_file/apply_patch AHORA."
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


def _is_continuation(text: str) -> bool:
    """True si el input es una continuación ('continua', 'sigue', 'si', etc.).

    Robusto a acentos, mayúsculas y sufijos ('continua por favor', 'sigue con eso'):
    normaliza y compara por token inicial o prefijo. Así 'continua' no pierde
    contexto solo por agregar una palabra.
    Anti-FP: 'si' afirmativo solo si es mensaje de 1 token (si no, es condicional
    'si el test falla...'); 'va/vamos/sigue/dale' solo si mensaje corto (<=3
    tokens) o exacto — 'vamos a crear auth.py desde cero' es tarea nueva.
    """
    prefix = normalize(_extract_command_prefix(text))
    if not prefix:
        return False
    tokens = prefix.split()
    first = tokens[0] if tokens else ""
    for w in _CONTINUATION_WORDS:
        nw = normalize(w)
        if prefix == nw:
            return True
        if nw in ("si", "sí"):
            # 'si' solo vale como afirmación aislada, no condicional
            continue
        if nw in ("va", "vamos", "sigue", "dale", "adelante"):
            if first == nw and len(tokens) <= 3:
                return True
            if prefix.startswith(nw + " ") and len(tokens) <= 3:
                return True
            continue
        if first == nw or prefix.startswith(nw + " "):
            return True
    return False


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

# Órdenes de CORRECCIÓN explícita ("corregir los errores de tests"): NO entran
# a la rama ambigua ("corregir" no da boundary en el regex de arriba) pero los
# rojos guardados mandan igual — sin esto re-ejecutaban la batería entera sin
# reds (E2E). No incluye implement/hacer (eso lo cubre la rama ambigua).
_CORRECTION_INTENT_RE = re.compile(
    r"\b(correg|arregl|repar|fix|rojos?|fall[ao]s?|error(es)?|rot[oa]s?)\w*",
    re.IGNORECASE,
)

# Mención explícita de ramas por el usuario ("creame la branch X"): única vía
# (junto a estar en main/master) que habilita create_branch en el turno.
_BRANCH_INTENT_RE = re.compile(
    r"\b(branch|rama|sucursal|checkout -b)\w*",
    re.IGNORECASE,
)

# Fuentes críticas del harness para detectar proceso STALE: si estos archivos
# cambian bajo una sesión abierta (pull con el CLI corriendo), el proceso
# sigue corriendo código viejo y falla raro (E2E: session.py nuevo en disco +
# tool_dedupe viejo en memoria → AttributeError en cada turno). Se hace
# snapshot al crear la Session y se compara al arrancar cada turno.
_WATCHED_SOURCE_FILES = (
    "main.py",
    "cache.py",
    "config.py",
    "orchestration/session.py",
    "orchestration/tool_dedupe.py",
    "orchestration/agent_builder.py",
    "orchestration/execute_bootstrap.py",
    "tools/__init__.py",
    "tools/verify.py",
    "tools/filesystem.py",
    "tools/git.py",
    "tools/runtime_probe.py",
    "display/console.py",
    "display/fullscreen_tui.py",
)


def _repo_root() -> str:
    return str(Path(__file__).resolve().parent.parent)


def _snapshot_sources(
    paths: tuple[str, ...] = _WATCHED_SOURCE_FILES, root: str | None = None
) -> dict:
    """{(path): (mtime_ns, size)} de las fuentes vigiladas. Fail-open: {}."""
    import os

    snap: dict = {}
    try:
        root = root or _repo_root()
        for rel in paths:
            try:
                st = os.stat(os.path.join(root, rel))
                snap[rel] = (st.st_mtime_ns, st.st_size)
            except OSError:
                snap[rel] = None
    except Exception:
        return {}
    return snap


def _sources_changed(
    snap: dict,
    paths: tuple[str, ...] = _WATCHED_SOURCE_FILES,
    root: str | None = None,
) -> bool:
    """True si alguna fuente vigilada cambió o falta respecto al snapshot."""
    if not snap:
        return False
    import os

    try:
        root = root or _repo_root()
        for rel in paths:
            try:
                st = os.stat(os.path.join(root, rel))
                cur = (st.st_mtime_ns, st.st_size)
            except OSError:
                cur = None
            if snap.get(rel) != cur:
                return True
    except Exception:
        return False
    return False

# Extensions considered when extracting concrete file targets from a prior
# analysis so the chained EXECUTE read them directly.
# Extensions considered when extracting concrete file targets from a prior
# analysis so the chained EXECUTE read them directly. Patrón compartido
# (vale para todos los lenguajes populares + infra/config/docs).
_TARGET_FILE_RE = re.compile(
    rf"([\w.\-/]+\.(?:{_EVIDENCE_EXT_PATTERN}))\b"
)


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


# Evidencia grounded: archivo:línea (ej. src/foo.ts:45) o bloque de código.
# Un análisis sin esto es opinión, no diagnóstico — encadenarlo con explore=0
# obliga al executor a escribir desde una alucinación (E2E Medicos: el analyzer
# inventó "useDashboard no existe" y el "implementa" posterior lo perpetuó).
# Incluye infra/config/docs: .tf (Terraform), .json (tasks.json), .prisma,
# .sql, yaml, .md — los veredictos reales citan esas extensiones también
# (T005/T006: "infra/iam.tf:27" no contaba como evidencia → alarma ⚠️ falsa
# en el cierre; T001-docs: "doc.md:12" tampoco). Patrón compartido: vale
# para php/java/rust/swift/dart/etc. igual que para ts/py/go.
_GROUNDED_EVIDENCE_RE = re.compile(
    rf"[\w.\-/]+\.(?:{_EVIDENCE_EXT_PATTERN}):\d+"
)
_CODE_BLOCK_RE = re.compile(r"```")

# Lenguaje de VEREDICTO (conclusión, no plan): solo participios/estados, nunca
# infinitivos ("verificar", "implementar") ni futuro ("voy a", "haré"). Un
# path real citado con este lenguaje es evidencia; sin él es un plan
# (E2E: cierre hueco con 3 planes pegados que citaban tasks.json).
_VERDICT_MARKERS_RE = re.compile(
    r"\b(ya\s+(está|estan|están|estaba|estaban|quedó|quedo)|"
    r"implementad[oa]s?|cumpl(e|en|ido|ida|idos|idas)|verificad[oa]s?|"
    r"list[oa]s?|terminad[oa]s?|completad[oa]s?|hech[oa]s?|funciona?n?|"
    r"pas(ó|aron|ado|ada)|en verde|aprobad[oa]s?|correct[oa]s?|"
    r"done\b|passed\b|works?\b|fixed\b|"
    r"nada pendiente|sin pendientes|no\s+(falta|faltan|hay pendientes))",
    re.IGNORECASE,
)


def _has_verdict_markers(text: str | None) -> bool:
    """True si el texto concluye (no solo planea). Ver _VERDICT_MARKERS_RE."""
    if not text:
        return False
    return bool(_VERDICT_MARKERS_RE.search(text))


# Lenguaje de PLAN A FUTURO: anuncia trabajo pendiente. Con cita dura
# (archivo:línea) no bloquea el cierre (veredicto parcial con respaldo);
# con evidencia débil sí (todavía no concluyó nada).
_FUTURE_PLAN_RE = re.compile(
    r"\b(voy a|vamos a|har[ée]|haremos|plan\s*:|pendientes?|falta|faltan|"
    r"pr[óo]ximo|despu[ée]s|por hacer|voy a continuar)\b",
    re.IGNORECASE,
)


def _has_grounded_evidence(task: str) -> bool:
    """True si el análisis previo trae evidencia verificable.

    Grounded = al menos un path con línea (file:línea) o un bloque de código
    citado. Sin esto NO se debe encadenar con explore=0: el executor necesita
    explorar antes de escribir en vez de heredar una hipótesis sin sustento.
    """
    if not task:
        return False
    return bool(
        _GROUNDED_EVIDENCE_RE.search(task)
        or (_CODE_BLOCK_RE.search(task) and _TARGET_FILE_RE.search(task))
    )


class _ToolCallLog:
    """Registro de tool calls del turno: set de nombres + CONTADOR de llamadas.

    La interfaz es la de set (add/clear/__contains__/__and__/__iter__/__len__)
    para que todo el código existente siga funcionando; agrega ``counts`` y
    ``total_of()``: distinguir 2 read_file de 1 (T006: 2 lecturas + veredicto
    con evidencia → cierre válido; contar por NOMBRE lo daba como 1 → retry
    forzado que re-exploró todo).
    """

    def __init__(self) -> None:
        self.names: set[str] = set()
        self.counts: dict[str, int] = {}

    def add(self, name: str) -> None:
        self.names.add(name)
        self.counts[name] = self.counts.get(name, 0) + 1

    def discard(self, name: str) -> None:
        self.names.discard(name)
        self.counts.pop(name, None)

    def clear(self) -> None:
        self.names.clear()
        self.counts.clear()

    def total_of(self, names: frozenset[str] | set[str]) -> int:
        return sum(self.counts.get(n, 0) for n in names)

    def __contains__(self, x: object) -> bool:
        return x in self.names

    def __iter__(self):
        return iter(self.names)

    def __len__(self) -> int:
        return len(self.names)

    def __and__(self, other):
        return self.names & other

    __rand__ = __and__

    def __repr__(self) -> str:
        return repr(self.names)


def _response_has_evidence(text: str | None) -> bool:
    """True si la respuesta del modelo cita evidencia verificable.

    Se usa en el cierre determinístico: un "LISTO" sin archivo:línea ni
    bloque de código es una confirmación sin evidencia. Fail-open por diseño:
    solo informa, nunca bloquea el turno.
    """
    if not text:
        return False
    if _GROUNDED_EVIDENCE_RE.search(text):
        return True
    if _CODE_BLOCK_RE.search(text):
        return True
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "verificación:", "verificacion:", "lint", "pytest", "tsc",
            "build", "tests", "test ",
        )
    )


def _is_planning_file(path: str) -> bool:
    """True si el path es un archivo de planificación (tasks.json de
    .agent/tasks/, tasks.md, plan.md, …).

    Se usa en el cierre de turno fallido para señalar flips pending→done
    sin verificación. Mismos criterios que la protección anti-corrupción
    (config.PROTECTED_*), más tasks.json — el vehículo del flip de status.
    Solo informa, nunca bloquea (marcar done es flujo legítimo del usuario).
    """
    if not path:
        return False
    name_lower = Path(path).name.lower()
    if name_lower in PROTECTED_TASK_FILENAMES or name_lower == "tasks.json":
        return True
    parts_lower = {part.lower() for part in Path(path).parts}
    return bool(parts_lower & PROTECTED_TASK_DIRS)


_CLOSING_VERDICT_MAX_CHARS = 800


def _closing_verdict_excerpt(text: str | None) -> str:
    """Extracto del veredicto del modelo para mostrar al cerrar sin escribir.

    El razonamiento del modelo no se muestra en vivo: si el turno cierra
    sin writes, el usuario debe ver QUÉ concluyó y POR QUÉ no escribió nada
    (E2E T005: cierre honesto correcto pero opaco — ni la tarea ni los pasos
    quedaron a la vista). Fail-open: "" si vacío.
    """
    clean = (text or "").strip()
    if not clean:
        return ""
    if len(clean) > _CLOSING_VERDICT_MAX_CHARS:
        clean = clean[:_CLOSING_VERDICT_MAX_CHARS].rstrip() + "…"
    return clean


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


_VERIFY_ONLY_RE = re.compile(r"\b(analiz|verific|confirm|comprob|cheque|asegur(?:ate|áte)|revis)\w*", re.I)
_IMPL_VERBS_RE = re.compile(r"\b(escrib|cre[áa]|agreg|edit|modific|arregl|correg|refactor|fix|migr|refactoriz)\w*", re.I)
# "implement" SOLO si NO es participio pasado ("implementada/implementado" =
# ya está hecha → verificación, no orden de implementar).
_IMPL_STEM_RE = re.compile(r"\bimplement(?!ad[oa])", re.I)


def _is_verification_only(text: str) -> bool:
    """True si la orden es SOLO verificar (no escribir).

    E2E real: 'verifica que este implementada correctamente Task 5' cayó en
    EXECUTE, el modelo verificó PERFECTO (lint/tests/build + criterios) y
    EXECUTE_REQUIRE_WRITE lo castigó con no-write retry → el modelo inventó
    un 'main.go truncado' inexistente y casi borra countTokens para
    satisfacer la presión de escritura.
    """
    t = text.strip().lower()
    return bool(
        _VERIFY_ONLY_RE.search(t)
        and not _IMPL_VERBS_RE.search(t)
        and not _IMPL_STEM_RE.search(t)
    )


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
        new = "disabled={{correct_match.group(1)}}"
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


def _build_failed_verify_suffix(report: str, details: str = "") -> str:
    """Sufijo EXECUTE con los rojos de la última verificación guardada.

    El turno vago ("implementar", "arreglalo") retoma ESTOS fallos en vez de
    re-ejecutar la batería a ciegas o encadenar un éxito viejo del historial
    (E2E: /verify con tests ❌ → "implementar" retomaba el "T006 ✅" porque
    los rojos se habían evaporado). Prohíbe repetir la batería completa sin
    un fix previo. Con detalle guardado (/verify muestra la cola), el turno
    va DIRECTO a los archivos sin siquiera correr el área.
    """
    head = (
        "\n\n⚠️ ÚLTIMA VERIFICACIÓN CON ROJOS (resultado guardado, NO "
        "re-ejecutés la batería completa todavía):\n"
        f"{report}\n"
    )
    classify = (
        "Paso 0 (ANTES de leer o editar nada): clasificá el rojo. ¿Dice "
        "ECONNREFUSED, timeout, connection refused/terminated o falla en "
        "beforeEach/connect? → es ENTORNO/CONEXIÓN (servicio caído), NO bug "
        "de código: no edites ni reescribas nada, reportalo como "
        "heredado/entorno. Solo si es fallo de LÓGICA seguí abajo."
    )
    if (details or "").strip():
        steps = (
            "Detalle guardado (cola de la salida — archivos que fallan):\n"
            f"{details[:2000]}\n"
            "Atacá DIRECTO esos archivos (ya tenés el detalle, no ejecutes "
            "nada antes de tener un fix): 1) leé el archivo que falla y "
            "corregí la causa raíz, 2) recién después volvé a correr la "
            "batería."
        )
    else:
        steps = (
            "Empezá por ESTOS fallos: 1) corré la tool del área en rojo para ver "
            "los archivos exactos (p. ej. run_tests si fallan tests), 2) leé el "
            "archivo que falla y corregí la causa raíz, 3) recién después volvé a "
            "correr la batería."
        )
    return head + classify + "\n" + steps + (
        " NO marques tasks como done sin verde."
    )


def _tasks_anchor_line(tasks_file: str | None) -> str:
    """Línea de ancla con la lista de tareas de la sesión para turnos vagos.
    Fail-open: "" sin archivo conocido."""
    if not tasks_file:
        return ""
    return (
        f"\n\n📌 Lista de tareas de esta sesión: {tasks_file} — leela si "
        "necesitás el criterio exacto. NO adivines otros paths de tareas "
        "ni traigas tareas de otro repo: si el path no existe en disco, "
        "la tarea no existe."
    )


def _porcelain_paths(lines: list[str] | None) -> set[str]:
    """Paths mencionados en un porcelain (renombres resueltos a destino)."""
    out: set[str] = set()
    for ln in lines or []:
        if len(ln) < 4:
            continue
        path = ln[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        path = path.strip().strip('"')
        if path:
            out.add(path)
    return out


def _parse_commit_candidates(
    lines: list[str] | None, session_files: set[str] | None,
    max_show: int = 15,
) -> str:
    """Bloque de texto: pendientes agrupados (staged/modificados/untracked)
    con marca [sesión] vs [previo]. Puro y testeable. Untracked jamás se
    stagea automático (credenciales): se listan con el comando para agregarlos.
    """
    staged, modified, untracked = [], [], []
    known = session_files or set()
    for ln in lines or []:
        if len(ln) < 4:
            continue
        status, path = ln[:2], ln[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        path = path.strip().strip('"')
        if not path:
            continue
        mark = "[sesión]" if path in known else "[previo]"
        if status == "??":
            untracked.append((f"{len(untracked) + 1}. {mark} {path}", path))
        elif status[0] not in (" ", "?"):
            staged.append(f"  {mark} {path}")
        else:
            modified.append(f"  {mark} {path}")

    def _block(title: str, items: list[str]) -> str:
        shown = "\n".join(items[:max_show])
        extra = f"\n  …(+{len(items) - max_show} más)" if len(items) > max_show else ""
        return f"{title} ({len(items)}):\n{shown}{extra}" if items else ""

    blocks = [
        _block("● Staged", staged),
        _block("● Modificados tracked", modified),
        _block(
            "● Nuevos untracked (respondé 1 2 3, 'todos' o 'ninguno')",
            [label for label, _ in untracked],
        ),
    ]
    body = "\n".join(b for b in blocks if b)
    if not body:
        return "↻ Árbol limpio — nada pendiente."
    return "📦 Pendientes de stage/commit:\n" + body


def _commit_stage_set(
    lines: list[str] | None, scope: str, session_files: set[str] | None,
) -> list[str]:
    """Paths tracked a stagear según alcance ('todo' | 'sesion').

    Untracked JAMÁS (credenciales: el usuario los agrega a mano).
    """
    out: list[str] = []
    known = session_files or set()
    for ln in lines or []:
        if len(ln) < 4:
            continue
        status, path = ln[:2], ln[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        path = path.strip().strip('"')
        if not path or status == "??":
            continue
        if scope == "sesion" and path not in known:
            continue
        out.append(path)
    return out


def _parse_commit_pick(text: str) -> str | list[int] | None:
    """Interpreta la respuesta a la lista de untracked: 'todos' | 'ninguno' |
    [números 1-based] | None (no es pick: sigue flujo normal y se limpia)."""
    t = (text or "").strip().lower()
    if t in ("todos", "todo"):
        return "all"
    if t in ("ninguno", "ninguna", "no", "nada"):
        return "none"
    if not re.fullmatch(r"[\d\s,]+", t):
        return None
    return sorted({int(n) for n in re.findall(r"\d+", t) if int(n) >= 1})


def _baseline_key(item: str) -> str:
    """Archivo del identificador de fallo (sin ::caso ni sufijos)."""
    return re.split(r"::| — ", (item or "").strip(), maxsplit=1)[0].strip()


def _split_reds(
    current: list[str], baseline: list[str] | None
) -> tuple[list[str], list[str]] | None:
    """(heredados, nuevos) comparando fallos actuales vs baseline anterior.

    None si no hay baseline (se desconoce: sin línea de atribución). []
    como baseline es válido (estaba todo verde → todo rojo actual es nuevo).
    """
    if baseline is None:
        return None
    base = {_baseline_key(b) for b in baseline}
    heredados = [c for c in current if _baseline_key(c) in base]
    nuevos = [c for c in current if _baseline_key(c) not in base]
    return heredados, nuevos


def _format_reds_attribution(heredados: list[str], nuevos: list[str]) -> str:
    """Línea compacta de atribución para el cierre (nombres, no logs)."""
    def _short(items: list[str], n: int = 2) -> str:
        shown = ", ".join(_baseline_key(i).split("/")[-1] for i in items[:n])
        extra = f" (+{len(items) - n} más)" if len(items) > n else ""
        return f"{shown}{extra}" if shown else "—"

    return (
        f"   Heredados: {len(heredados)} (ya fallaban: {_short(heredados)}) · "
        f"Nuevos: {len(nuevos)} ({_short(nuevos) if nuevos else 'ninguno'})"
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


def run_commit_verification(
    repo_path: str, reuse: dict[str, bool | None] | None = None,
    capture: dict | None = None,
) -> tuple[bool, str]:
    """Batería lint/tests/build para el gate de commit y /verify.

    Retorna (pasó_todo, reporte_corto). `[PASSED]` cuenta como verde y
    `[SKIPPED]` (repo sin stack: docs puros) como N/A que NO bloquea;
    cualquier fallo, timeout o tool ausente → False. Fail-open ante errores
    del propio harness (nunca raisea).

    ``reuse``: resultados del turno actual (session._verify_results). Si ya
    están lint+tests+build en verde (o run_verify verde que los cubre), se
    reusa sin re-ejecutar: evita la 3ª batería del turno (modelo + post-write
    gate + commit gate) que quemaba contexto y tiempo.

    ``capture``: si se pasa un dict, se llena capture["details"] con la cola
    (últimos 1500 chars) de cada área en rojo — es lo que nombra los archivos
    que fallan. /verify lo muestra y lo guarda en sesión para que el próximo
    turno vaya directo a los archivos sin re-correr la batería.
    """
    if reuse:
        trio_ok = all(reuse.get(k) is True for k in ("run_lint", "run_tests", "run_build"))
        uni_ok = reuse.get("run_verify") is True
        if trio_ok or uni_ok:
            src = "run_verify" if uni_ok and not trio_ok else "turno actual"
            return True, (
                f"  ✅ lint: reusa verificación verde ({src})\n"
                f"  ✅ tests: reusa verificación verde ({src})\n"
                f"  ✅ build: reusa verificación verde ({src})"
            )
    try:
        from tools.verify import run_build, run_lint, run_tests
    except Exception as e:
        return False, f"no se pudo cargar verificación: {e}"
    results: list[tuple[str, bool, str]] = []
    for name, fn in (("lint", run_lint), ("tests", run_tests), ("build", run_build)):
        try:
            out = str(fn.invoke({"path": repo_path}))
        except Exception as e:
            out = f"[FAILED] {type(e).__name__}: {e}"
        # SKIPPED (sin stack: docs/infra) vale como ok-neutral: no bloquea.
        ok = out.startswith("[PASSED]") or out.startswith("[SKIPPED]")
        first = out.splitlines()[0][:120] if out else "(sin salida)"
        results.append((name, ok, first))
        if capture is not None and not ok:
            tail = out[-1500:] if len(out) > 1500 else out
            capture.setdefault("details", {})[name] = tail
    passed = all(ok for _, ok, _ in results)
    report = "\n".join(
        f"  {'✅' if ok else '❌'} {name}: {first}" for name, ok, first in results
    )
    return passed, report


def _resolve_repo_file(repo_path: str, cand: str) -> str | None:
    """Resuelve un path citado a archivo real del repo (o None).

    1) Exacto relativo/absoluto en disco. 2) Basename con match ÚNICO en el
    índice git (tracked + untracked no ignorados). Lo 2) cubre citas sin
    directorio; con 0 o 2+ matches no se atribuye (anti-fantasma).
    Fail-open: None ante cualquier error.
    """
    import subprocess

    try:
        if not cand:
            return None
        p = Path(cand)
        if p.is_absolute():
            return str(p) if p.is_file() else None
        root = Path(repo_path)
        if (root / cand).is_file():
            return str(root / cand)
        name = p.name
        if "/" in cand or not name:
            return None
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "--others",
             "--exclude-standard", "--", f"*{name}"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            return None
        matches = [
            ln.strip() for ln in proc.stdout.splitlines()
            if ln.strip() and ln.strip().rsplit("/", 1)[-1] == name
        ]
        if len(matches) == 1:
            return matches[0]
        return None
    except Exception:
        return None


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
        # Tool calls reales del turno (los tool calls viven en el estado del
        # grafo, NO en self._messages — la compuerta de verificación escaneaba
        # self._messages y daba falsos positivos: "no corrió verify" cuando sí).
        # _ToolCallLog: set + contador de llamadas (T006: 2 read_file = turno
        # de verificación legítimo; contar por nombre daba 1 y forzaba retry).
        self._called_tools = _ToolCallLog()
        # Journal de evidencia harness-side (P1): el wrapper agrega
        # {"tool","path","ok"} por ejecución real; se persiste en SQLite al
        # cerrar el turno y se inyecta en retry/summary (sobrevive compact).
        self._evidence: list = []
        self._evidence_flushed = 0
        # Primer fallo de verify por nombre canónico (trampa del wrapper):
        # el gate-retry lo inyecta para que el modelo arregle UN punto.
        self._turn_failures: dict = {}
        # Resultado ([PASSED]/[FAILED]) de cada verify tool del turno. Sin esto
        # el cierre decía "build ✅" con solo haber LLAMADO la tool (E2E real:
        # run_build en raíz rota contó como verificado). Solo `[PASSED]`
        # explícito cuenta como pasado.
        self._verify_results: dict[str, bool] = {}
        # Memoria ACUMULADA de verify del turno (los reintentos limpian
        # _called_tools/_verify_results: sin esto el cierre "olvida" que la
        # verificación CORRIÓ — E2E T006: run_verify ✅ + flip y el cierre
        # fallido posterior afirmaba "SIN verificación"). La COMPUERTA sigue
        # usando el estado del intento actual (verify stale no vale).
        self._turn_verify_tools: set[str] = set()
        self._turn_verify_results: dict[str, bool | None] = {}
        self._turn_wrote = False
        self._turn_saw_pass = False
        self._turn_verify_failed = False
        self._turn_writes_after_pass = False
        # Última verificación corrida FUERA del turno (/verify o gate de
        # commit): {"passed": bool, "report": str} o None. Los rojos no se
        # evaporan entre turnos (E2E: /verify con tests ❌ y el "implementar"
        # siguiente arrancaba ciego y re-ejecutaba la batería). Vive a nivel
        # SESIÓN: no se limpia por turno ni por /new (describe el repo, no el
        # turno). Verde → None (rojos resueltos). Se consume una sola vez.
        self._last_verify: dict | None = None
        # Lista de tareas de la sesión (primer planning citado que exista en
        # disco). Los turnos vagos la reinyectan como ancla (E2E: turno vago
        # leyó .agent/tasks.json y .agent-devs/tasks.md inexistentes y deliró
        # un T8 de otro repo). Vive a nivel SESIÓN, no se limpia por turno.
        self._last_tasks_file: str | None = None
        # Archivos tocados por turnos EXECUTE de ESTA sesión (para /commit
        # sesion): unión por turno de (dirty al cerrar − dirty al abrir),
        # con paths CRUDOS de porcelain (sin filtros). /new lo limpia.
        self._session_touched_files: set[str] = set()
        # Selección pendiente de untracked para stagear (/commit lista y el
        # usuario responde "1 3" o "todos"). Se consume al usarse; /new limpia.
        self._pending_commit_pick: list[str] | None = None
        # Snapshot crudo (sin filtrar) del inicio del turno, para el cálculo
        # anterior. None = git falló (no se atribuye ese turno).
        self._turn_start_raw: set[str] | None = None
        # Baseline flaco: SET de tests que fallaban al cerrar el turno ANTERIOR
        # {"failing": [...], "snapshot": ...} o None si se desconoce. El cierre
        # del turno N lo guarda (cosecha salidas ya corridas: cero baterías
        # extra) y el turno N+1 distingue heredados de nuevos. [] = estaba
        # todo verde (cualquier rojo actual es nuevo).
        self._baseline: dict | None = None
        # Hidratación desde disco: un restart/pull entre /verify y el turno
        # no debe evaporar los rojos ni el ancla (E2E turno rojo: sesión
        # fresca sin "🔗 Retomando rojos"). Fail-open.
        try:
            from cache import load_repo_state

            _st = load_repo_state(self.repo_path)
            if isinstance(_st.get("last_verify"), dict):
                self._last_verify = _st["last_verify"]
            if _st.get("last_tasks_file"):
                self._last_tasks_file = _st["last_tasks_file"]
        except Exception:
            pass
        # Resultado del diagnóstico de runtime del turno actual (True=entorno
        # sano, False=hallazgos, None=no se ejecutó). Lo usa _closing_message
        # para concluir "probablemente ya está resuelto" cuando corresponde.
        self._runtime_healthy: bool | None = None
        # Snapshot de fuentes vigiladas para detectar proceso STALE (ver
        # _WATCHED_SOURCE_FILES). Se toma una vez al crear la Session.
        self._source_snapshot: dict = _snapshot_sources(_WATCHED_SOURCE_FILES)
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
        # Retry de solo-lectura en curso (ANALYZE/PLAN con solo read_file):
        # el guard PLAN-EXPLORA no debe exigir exploración imposible.
        self._readonly_retry = False
        self._turn_question = ""
        # El agente actual se construyó SIN tools (no_explore)? Reutilizarlo
        # para una orden NUEVA dejaría al usuario sin exploración — cada orden
        # debe arrancar con el agente completo.
        self._agent_no_explore = False
        # Preload de ANALYZE en ESTE turno (archivos .md/.txt citados): lo usa
        # el guard de evidencia para exigir lecturas antes de dictaminar.
        self._analyze_preloaded: bool = False
        # Alcance pinnado por tarea (breaker de scope creep, por turno):
        # números pinnados + archivos citados en esas entradas. El wrapper
        # cuenta escrituras fuera del alcance en dedupe.scope_violations.
        self._scope_nums: list[int] = []
        # Snapshot del dirty tree al inicio del turno (cierre honesto).
        # None = snapshot fallido (sin claim de atribución).
        self._turn_start_dirty: frozenset[str] | None = frozenset()
        # Timestamp (epoch) de inicio del turno: distingue un commit hecho
        # DURANTE el turno de un commit previo (setup/benchmark). Sin esto el
        # cierre confunde "setup commiteado hace segundos" con "trabajo hecho"
        # (pilot P01: repo con 'init tests' commiteado → cierre prematuro).
        # Se refresca en start()/reset()/cada turno (ver snapshot del turno).
        self._turn_start_ts: float = time.time()
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
        # Confirmación de writes (EXECUTE_CONFIRM_WRITES): el wrapper de tools
        # pausa write/edit/delete hasta que el usuario aprueba. En TUI el turno
        # corre en un thread aparte, así que el callback espera este Event y la
        # TUI lo resuelve desde su input box (main.py on_submit). Sin TUI no se
        # registra callback → fail-open.
        self._confirm_writes: bool = False
        self._confirm_event: threading.Event | None = None
        self._confirm_answer: bool | None = None
        self._confirm_timeout: float = EXECUTE_CONFIRM_TIMEOUT
        # Auto-aprobación (/autoapprove): con True, los writes se aprueban sin
        # preguntar (igual que en modo no-interactivo). Alcance: SOLO
        # aprobaciones de escritura/edición (incluido PATH FIX). El commit
        # sigue siendo manual siempre. Se apaga con /new (re-activar explícito
        # por sesión, para no olvidar que está prendido).
        self._auto_approve: bool = False
        self._confirm_lock = threading.Lock()
        # Exclusión mutua de turnos (un turno a la vez) + cancelación de TODOS
        # los vivos: dos run_turn concurrentes mezclaban _messages/tools y el
        # ESC solo alcanzaba al último (E2E T011/T013 + post-ESC escribiendo).
        self._turn_lock = threading.Lock()
        self._turn_thread: int | None = None
        self._turn_depth = 0
        self._turn_tasks: set = set()
        # Edit pendiente cuando la confirmación vence por timeout: el usuario
        # dijo 'continua' después y espera que NO se re-explore todo. Se guarda
        # acá y el próximo turno lo reaplica sin LLM.
        self._pending_write: tuple[str, dict] | None = None
        # Timeouts consecutivos por pendiente (key=name::path::hash): evita el
        # loop de reintentar el MISMO write tras cada timeout (E2E Medicos:
        # 6x el mismo write_file tras "Confirmación vencida"). Al 2do timeout
        # seguido del mismo pendiente se descarta y se exige otra estrategia.
        self._pending_timeouts: dict[str, int] = {}
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
        # Banner de rol: el default current_role=ANALYZE suprimía el anuncio
        # del primer turno analyze (role_changed=False) y el benchmark perdía
        # la métrica role_routed.
        self._role_announced = False

    def start(self) -> str:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # Sesión nueva: el tracker read-before-edit arranca vacío (leer en
            # la sesión anterior no habilita editar en esta). El timestamp de
            # turno también: commits previos a la sesión son setup, no trabajo.
            try:
                from tools.filesystem import clear_read_tracker

                clear_read_tracker()
            except Exception:
                pass
            self._turn_start_ts = time.time()
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
                if not self._graph_project:
                    # El store del harness no tiene el repo indexado (difiere
                    # del MCP del editor): indexarlo UNA vez y re-resolver.
                    # Sin esto la fuzzy match inventaba keys (medicos-sandbox)
                    # y ningún cm__search_code encontraba nada.
                    idx = by_name.get("cm__index_repository")
                    if idx is not None:
                        console.print(
                            "\n[yellow]🧠 Indexando el repo en el knowledge graph "
                            "(primera vez — puede tardar 1-2 min)…[/yellow]"
                        )
                        try:
                            out = loop.run_until_complete(
                                idx.ainvoke({"repo_path": self.repo_path, "mode": "moderate"})
                            )
                            self._graph_project = loop.run_until_complete(
                                _resolve_project_key(by_name, self.repo_path)
                            )
                            if "store.corrupt" in str(out) or "bad_root_path" in str(out):
                                # Store del MCP con filas corruptas (root_path
                                # relativo de una sesión vieja con 'agent-devs .'):
                                # avisar cómo limpiarlo en vez de fallar mudo.
                                console.print(
                                    "[red]⚠️ El store del knowledge graph reportó "
                                    "corrupción (root_path relativo). Limpialo con:[/red]\n"
                                    "[yellow]   codebase-memory-mcp delete-project "
                                    "--root \".\"[/yellow]"
                                )
                        except Exception as e:
                            if "store.corrupt" in str(e) or "bad_root_path" in str(e):
                                console.print(
                                    "[red]⚠️ Knowledge graph corrupto — limpiá el "
                                    "proyecto con root_path='.' (ver docs). El agente "
                                    "sigue sin tools cm__*.[/red]"
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
                    tool_call_results=self._verify_results,
                    evidence_sink=self._evidence,
                    failure_sink=self._turn_failures,
                    graph_project=self._graph_project,
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
        self._auto_approve = False
        self._session_touched_files = set()
        self._pending_commit_pick = None
        self.current_role = Role.ANALYZE
        self._evidence = []
        self._evidence_flushed = 0
        try:
            from tools.filesystem import clear_read_tracker

            clear_read_tracker()
        except Exception:
            pass
        self._turn_start_ts = time.time()
        self._load_previous_sessions()
        self._rebuild_agent(Role.ANALYZE)

    def _rebuild_agent(self, role: Role, no_explore: bool = False,
                       tools_override: list | None = None) -> bool:
        # no_explore=True construye un agente SIN tools: reutilizarlo para una
        # orden NUEVA normal dejaría al usuario sin exploración (E2E real:
        # "arquitectura del backend" agota el budget → retry no_explore → la
        # orden siguiente "arquitectura del frontend" arrancaba SIN tools).
        if (
            self.agent is not None
            and role == self.current_role
            and not no_explore
            and tools_override is None
            and not getattr(self, "_agent_no_explore", False)
        ):
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
                    tools_override=tools_override,
                    tool_call_logger=self._called_tools,
                    tool_call_results=self._verify_results,
                    evidence_sink=self._evidence,
                    failure_sink=self._turn_failures,
                    graph_project=self._graph_project,
                    confirm_callback=(
                        self._confirm_write_cb
                        if EXECUTE_CONFIRM_WRITES and role == Role.EXECUTE
                        else None
                    ),
                )
            )
            self._agent_no_explore = no_explore or tools_override is not None
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
        # Agente RESTRINGIDO (write-only): una orden NUEVA debe reconstruirlo
        # completo, no reutilizar este subconjunto crippleado.
        self._agent_no_explore = True
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
                    tool_call_results=self._verify_results,
                    evidence_sink=self._evidence,
                    failure_sink=self._turn_failures,
                    allow_overwrite_escalation=False,
                    confirm_callback=(
                        self._confirm_write_cb if EXECUTE_CONFIRM_WRITES else None
                    ),
                )
            )
        finally:
            loop.close()

    def _first_failure_block(self) -> str:
        """Primer fallo de verify del turno para enfocar el fix (1 punto).

        El 4B ante 200 líneas de output reescribe a ciegas; con el primer
        fallo aislado puede corregir UN lugar y re-verificar.
        """
        if not self._turn_failures:
            return ""
        parts = []
        for name, excerpt in self._turn_failures.items():
            parts.append(f"--- {name} ---\n{excerpt}")
        return (
            "\n\nPRIMER FALLO DETECTADO (arreglá SOLO esto, no reescribas "
            "todo — un fix chico, después re-verificá):\n" + "\n".join(parts) + "\n"
        )

    def _inject_verify_gate(self) -> None:
        """Inyecta la compuerta de verificación (obliga a correr run_verify
        en UNA llamada) y reconstruye el agente gate-retry."""
        self._messages.append(HumanMessage(
            "⚠️ No verificaste lo que escribiste.\n"
            "Es OBLIGATORIO verificar ANTES de dar la tarea por terminada, "
            "en UNA sola llamada (no 3 separadas):\n"
            f"  run_verify(path=\"{self.repo_path}\")\n"
            "\nSi algo falla, CORREGÍ el error y volvé a ejecutar "
            "run_verify hasta que pase." + self._first_failure_block()
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
        # Agente RESTRINGIDO (compuerta): una orden NUEVA debe reconstruirlo
        # completo, no reutilizar este subconjunto.
        self._agent_no_explore = True
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
                    tool_call_results=self._verify_results,
                    evidence_sink=self._evidence,
                    failure_sink=self._turn_failures,
                    confirm_callback=(
                        self._confirm_write_cb if EXECUTE_CONFIRM_WRITES else None
                    ),
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
        Fail-safe: si el LLM falla (fallback genérico), NO se destruye el
        historial — se aborta para no perder mensajes irrecuperables.
        """
        if len(self._messages) <= 2:
            return
        old_messages = self._messages[:-2]  # preservar últimos 2 mensajes
        recent = self._messages[-2:] if len(self._messages) >= 2 else self._messages
        summary = _generate_summary(self.llm, old_messages)
        if summary.strip() == "Sesión previa resumida automáticamente.":
            console.print("[yellow]⚠️ No se pudo generar resumen (LLM no disponible) — historial intacto.[/yellow]")
            return
        self._messages = [
            SystemMessage(f"Resumen de la conversación previa:\n{summary}\n{self._evidence_block()}"),
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

    def _flush_evidence(self) -> None:
        """Persiste en SQLite las entradas del journal aún no guardadas."""
        try:
            from cache import record_evidence
            for entry in self._evidence[self._evidence_flushed:]:
                record_evidence(
                    self.session_id, self.repo_path,
                    entry.get("tool", ""), entry.get("path", ""),
                    entry.get("ok", True),
                )
            self._evidence_flushed = len(self._evidence)
        except Exception:
            pass

    def _evidence_block(self) -> str:
        """Hechos del turno para retry/summary (1 línea por tool call)."""
        lines = []
        for e in self._evidence[-12:]:
            mark = "✅" if e.get("ok", True) else "❌"
            lines.append(f"{mark} {e.get('tool', '')} {e.get('path', '')}")
        if not lines:
            return ""
        return (
            "\n\nEVIDENCIA DEL TURNO (hechos ya ocurridos — no los repitas, "
            "partí de acá):\n" + "\n".join(lines) + "\n"
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
        return _EXECUTE_FORCE_WRITE_MSG + self._findings_block() + anchor + self._evidence_block()

    def _trim_for_retry(self) -> None:
        """Recorta historial para retry preservando el SystemMessage summary.

        El summary es la única memoria comprimida — perderlo deja al retry
        sin tarea canónica bulk ni contexto previo. Se preservan: summary (si
        existe) + últimos 3 mensajes.
        """
        summaries = [m for m in self._messages if isinstance(m, SystemMessage)]
        tail = self._messages[-3:] if len(self._messages) > 3 else list(self._messages)
        # Evitar duplicar el summary si ya está en el tail
        seen = {id(m) for m in tail}
        self._messages = [m for m in summaries if id(m) not in seen] + tail

    def _enter_budget_retry(self, retry_msg: str) -> list:
        """Prepara el retry de EXECUTE tras un turno que no convergió (budget
        agotado, recursion limit o reasoning-only): acota las lecturas, resetea
        dedupe/budget, reconstruye el agente con BUDGET_RETRY_TOOLS
        (read_file acotado + edit_file + write_file; sin delete_file) y
        devuelve messages_for_agent para el siguiente intento.

        _called_tools.clear(): la compuerta final debe exigir verify en ESTE
        intento — no dejar pasar verify stale del intento anterior. El CIERRE
        usa la memoria acumulada (_snapshot_turn_verify), así que no olvida
        que la verificación corrió.

        FAIL-CLOSED por rol: este retry otorga edit_file/write_file. Si llega
        acá un rol que nunca escribe (REVIEW/CHAT/...) es un bug de ruteo del
        retry — fallar LOUD en vez de regalar escritura (E2E real T1b/9B:
        un review terminó con edit_file a package.json)."""
        if self.current_role != Role.EXECUTE:
            raise ToolBudgetExceeded(
                f"retry de escritura inválido en rol {self.current_role.value}: "
                "los roles de solo-lectura usan el retry de solo-lectura."
            )
        self._trim_for_retry()
        self._messages.append(HumanMessage(self._retry_with_read_anchor()))
        messages_for_agent = list(self._messages)
        self._snapshot_turn_verify()
        self._called_tools.clear()
        self._verify_results.clear()
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
            import asyncio
            import json
            import threading

            from tools.graph_trace import build_trace_component

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

    def _retry_analyze_anchor(self, max_blocks: int = 3, max_chars: int = 12000) -> str:
        """Ancla para el retry de ANALYZE/PLAN: lo que trace_component y
        read_file cachearon en el PASS1. Sin esto, el 4B no tiene NADA que
        analizar (los ToolMessages del graph se pierden al cortar por budget)
        y razona en círculos adivinando paths.

        Se inyectan TODOS los leídos (no solo el primero: anclar en 1 solo
        obliga al modelo a rellenar con caché en preguntas amplias — E2E real:
        recomendó archivos de src/modules/* nunca leídos citando "líneas
        inferidas"). Lo que no entra por presupuesto va como ÍNDICE de paths
        para que al menos no invente nombres.
        """
        if not self._read_cache:
            return ""
        # Traces primero (son la pista principal del PASS1); el trace del
        # sistema (que puede duplicar contenido) solo como fallback cuando el
        # PASS1 no cacheó nada propio.
        traces = [k for k in self._read_cache if k.startswith("[trace:")]
        own_traces = [k for k in traces if k != "[trace:sistema]"]
        snippets = [k for k in self._read_cache if k.startswith("[snippet:")]
        ordered = (own_traces or traces) + snippets + [
            k for k in self._read_cache
            if not k.startswith("[trace:") and not k.startswith("[snippet:")
        ]
        blocks: list[str] = []
        indexed: list[str] = []
        used = 0
        for key in ordered:
            content = self._read_cache[key]
            if len(blocks) < max_blocks and used < max_chars:
                take = min(len(content), 5000, max_chars - used)
                if take > 200:
                    if key.startswith("[trace:"):
                        label = f"RESULTADO DE TRACE_COMPONENT ({key})"
                    elif key.startswith("[snippet:"):
                        label = f"SOURCE DEL GRAFO ({key})"
                    else:
                        label = f"CONTENIDO REAL DE {key}"
                    blocks.append(f"--- {label} ---\n{content[:take]}\n--- FIN ---")
                    used += take
                    continue
            indexed.append(key)
        # EVIDENCIA VIGENTE del turno (presente, no "intento anterior": ese
        # encuadre hacía que el modelo descartara lo leído como invalidado —
        # E2E real T2/9B: leyó 3 archivos exactos y declaró "no fueron leídos
        # con contenido"). Lo de abajo CUENTA como lectura: citalo.
        tools = ", ".join(sorted(self._called_tools)) or "ninguna"
        # TODAS las claves del caché son código obtenido (archivos, snippets
        # del grafo, traces): filtrar las [trace:/[snippet: mentía ("ninguno")
        # aunque el modelo sí había leído source (E2E real T1).
        read_paths = ", ".join(self._read_cache.keys()) or "ninguno"
        parts = [
            "\n\nEVIDENCIA VERIFICADA DE ESTE TURNO (contenido real obtenido "
            "por vos con tools, vigente AHORA). Analizá EN BASE A ESTO y citá "
            "archivo:línea de lo que cada tool devolvió. PROHIBIDO inventar "
            "paths, firmas o archivos fuera de esta lista y del historial "
            "del turno.",
            f"REGISTRO DEL TURNO: ejecutaste estas tools: {tools}. "
            f"Obtuviste contenido de: {read_paths}. Este contenido CUENTA "
            f"como lectura válida — citalo con archivo:línea. Todo archivo "
            f"fuera de esa lista que menciones debe marcarse como NO verificado.",
        ]
        if indexed:
            parts.append(
                "LEÍDO PERO SIN CONTENIDO POR PRESUPUESTO (solo paths, no cites "
                "líneas de estos):\n" + "\n".join(f"- {k}" for k in indexed)
            )
        parts.extend(blocks)
        parts.append(
            "Si lo que te piden excede lo que leíste, NO completes con el análisis "
            "cacheado: decí explícito QUÉ te falta (paths, logs, criterios) y "
            "pedilo. Un veredicto sin evidencia es peor que pedir datos."
        )
        return "\n".join(parts)

    def _retry_analyze_readonly(self, new_role: Role, reason: str) -> None:
        """Retry de solo-lectura para roles que NUNCA escriben (ANALYZE/PLAN/
        REVIEW), en dos etapas:

        1ª vez: SOLO read_file (sin búsqueda ni listados). El retry anterior
        (0 tools) no podía convertir listados en lecturas: el PASS1 moría en
        breadth (E2E real T1: 3 intentos, 0 read_file) y el retry respondía a
        ciegas. Acá se conserva el historial (los listados quedan visibles
        para elegir QUÉ leer) y el agente solo puede leer archivos clave.
        2ª vez (si también se agota): SIN tools + ancla completa — responder
        con lo leído. Dos etapas calzan justo en max_attempts=3.
        """
        from tools.filesystem import read_file

        # REVIEW usa el budget de explore (no el de analyze).
        budget = (
            self._analyze_budget if new_role in (Role.ANALYZE, Role.PLAN)
            else self._explore_budget
        )
        doc = "el informe" if new_role == Role.REVIEW else "el análisis"

        # Pregunta ORIGINAL del turno (no el último HumanMessage: ese puede ser
        # un retry inyectado y anidaría "Reanalizá: Reanalizá: ..." — E2E T1).
        user_msg = (getattr(self, "_turn_question", "") or "").strip()
        if not user_msg:
            for m in reversed(self._messages):
                if isinstance(m, HumanMessage):
                    user_msg = str(m.content)
                    break
        if self._readonly_retry:
            # Segunda vuelta: ya se leyó lo legible; responder SIN tools con
            # contexto MÍNIMO (pregunta + ancla regenerada). El historial largo
            # ahoga al 4B (lost-in-the-middle): con todo el contexto encima
            # declaró "sin acceso al código" teniendo 3 snippets (E2E real T1).
            # Rumiar hasta agotar output también queda bloqueado así.
            anchor = self._retry_analyze_anchor()
            summaries = [m for m in self._messages if isinstance(m, SystemMessage)]
            self._messages = summaries
            self._messages.append(HumanMessage(
                f"Pregunta original: \"{user_msg}\"\n\n"
                "RESPONDÉ AHORA el análisis final en texto con el ancla de "
                "abajo: citá archivo:línea por afirmación (los paths están en "
                "los encabezados --- --- y en HECHOS). Ya no tenés tools — no "
                "intentes leer más. PROHIBIDO decir que no tenés acceso al "
                "código: el ancla CONTIENE código real. Si algo no se pudo "
                "verificar, decí QUÉ falta en vez de completar."
                + anchor
            ))
            self._rebuild_agent(new_role, no_explore=True)
            self._analyze_budget.reset()
            console.print(
                f"\n[yellow]⚠️  {reason} — Respondiendo con lo leído "
                "(contexto mínimo, sin tools)…[/yellow]\n"
            )
            return

        # Turno interactivo con tools: timeouts normales (no el modo paciente
        # del retry sin tools) y flag para que el guard PLAN-EXPLORA no exija
        # exploración imposible en este agente restringido.
        self._readonly_retry = True
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
            retry_body += findings
        retry_body += (
            "\n\n⛔ El intento anterior agotó la exploración en listados y "
            "búsquedas sin leer código. Ahora SOLO tenés read_file (sin "
            "list_files ni búsqueda): la estructura ya la ubicaste arriba — "
            "leé los 2-4 ARCHIVOS clave del tema (read_file es para archivos, "
            "NO directorios: pasar un directorio devuelve error y pierdes "
            "tiempo; los nombres citados en la pregunta son tus objetivos). "
            "PROHIBIDO responder con un plan de pasos futuros ('voy a leer…'): "
            "tu PRÓXIMA ACCIÓN debe ser una tool call read_file real. Recién "
            f"con el contenido leído respondé {doc} citando archivo:línea. Si "
            "con esas lecturas no alcanza, decí QUÉ falta en vez de completar."
        )
        # Sin trim: los listados del PASS1 deben quedar visibles para elegir
        # qué leer. El agente restringido no puede hacer crecer el contexto
        # con búsquedas (solo reads acotados).
        self._messages.append(HumanMessage(retry_body))
        # Orden: primero topes (explore=0 bloquea listas/búsquedas, reads
        # acotados), DESPUÉS reset (calcula _explore_exhausted con lo nuevo).
        budget.max_calls = 0
        budget.reset()
        self._rebuild_agent(new_role, tools_override=[read_file])
        console.print(
            f"\n[yellow]⚠️  {reason} — Reintentando en modo solo-lectura "
            "(leé los archivos clave y respondé con evidencia)…[/yellow]\n"
        )

    def _retry_analyze_no_explore(self, new_role: Role, reason: str) -> None:
        """Compat: redirige al retry de solo-lectura (conserva el nombre que
        usan los callers y tests)."""
        self._retry_analyze_readonly(new_role, reason)

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

    def _commit_during_turn(self) -> bool:
        """True si el commit más reciente se hizo DESPUÉS de iniciar el turno.

        `_repo_has_recent_commit()` (últimos 5 min) no distingue un commit de
        setup/benchmark previo al turno del trabajo real del turno (pilot P01:
        'init tests' commiteado antes → el cierre declaró 'nada pendiente' sin
        haber escrito nada). Mismo reloj (git y python, misma máquina): sin
        gracia, comparación estricta.
        """
        try:
            import subprocess
            result = subprocess.run(
                ["git", "log", "-1", "--format=%ct"],
                cwd=self.repo_path, capture_output=True, text=True, timeout=5,
            )
            ts = result.stdout.strip()
            if not ts.isdigit():
                return False
            start = getattr(self, "_turn_start_ts", 0.0) or 0.0
            return int(ts) >= int(start)
        except Exception:
            return False

    def _nothing_pending_to_write(self) -> bool:
        """True si exigir escritura al modelo sería dañino: árbol limpio y el
        trabajo ya está commiteado DURANTE este turno o verificado (verify
        tools corridas). Un commit previo al turno (setup/benchmark) NO cuenta:
        con cambios pendientes o sin evidencia de trabajo del turno, devuelve
        False (el escape vago SÍ debe reintentar)."""
        if self._changed_files():
            return False
        return bool(self._commit_during_turn() or self._verify_tools_called())

    def _readonly_evidence_turn(self, text: str) -> bool:
        """True si el turno fue SOLO lectura/verify con evidencia citada.

        El modelo leyó ≥2 archivos/tools y respondió un veredicto con
        archivo:línea (E2E real: 'implementar T003' → leído iam.tf → 'ya está
        implementada, línea 28 cumple el AC'). Reintentar con foco en
        escritura lo empuja a edits no-op (5x idénticos). Es el mismo contrato
        que _is_verification_only pero determinado por LO QUE HIZO el turno,
        no por el texto del usuario (que dijo 'implementar').
        """
        if self._called_tools & WRITE_TOOL_NAMES:
            return False
        # Contar LLAMADAS (no nombres): 2 read_file a archivos distintos es
        # un turno de verificación legítimo (E2E real T006: tasks.json +
        # home-client.ts + veredicto con archivo:línea → cerrar sin retry).
        counts = getattr(self._called_tools, "counts", None)
        if counts is not None:
            productive_calls = self._called_tools.total_of(
                READISH_TOOL_NAMES | VERIFY_TOOL_NAMES
            )
            if productive_calls < 2:
                return False
        else:
            productive = self._called_tools & (READISH_TOOL_NAMES | VERIFY_TOOL_NAMES)
            if len(productive) < 2:
                return False
        # Cita dura (archivo:línea o bloque de código): veredicto con respaldo.
        if _GROUNDED_EVIDENCE_RE.search(text or "") or _CODE_BLOCK_RE.search(text or ""):
            return True
        # La CONCLUSIÓN debe ser lo último: el plan inicial ("Plan:", "voy
        # a…") no contamina el veredicto final (E2E T011: veredicto válido
        # fue a retry por el plan del inicio), pero un plan AL FINAL
        # ("después completo") significa que todavía no concluyó (E2E cierre
        # hueco con 3 planes pegados). Vale: último veredicto después del
        # último plan. La cita dura de arriba vale siempre (respaldo).
        body = text or ""
        _verdict_pos = max(
            (m.end() for m in _VERDICT_MARKERS_RE.finditer(body)), default=-1
        )
        if _verdict_pos < 0:
            return False
        _future_pos = max(
            (m.end() for m in _FUTURE_PLAN_RE.finditer(body)), default=-1
        )
        if _future_pos > _verdict_pos:
            return False
        # Evidencia en español: el veredicto cita un path REAL del repo
        # ("infra/iam.tf línea 28"). Anti-fantasma: se valida en DISCO — un
        # path inventado no cuenta (mismo criterio que find_unverifiable_cites).
        # Vale nombre pelado si resuelve a UN solo archivo en el índice git
        # (E2E T016/T018: el modelo citó "handle-pauta-finalizada.ts" sin
        # directorio y el veredicto válido fue a retry).
        root = Path(self.repo_path)
        for m in re.finditer(rf"[\w.\-/]+\.(?:{_EVIDENCE_EXT_PATTERN})\b", text or ""):
            cand = m.group(0)
            p = Path(cand) if Path(cand).is_absolute() else root / cand
            if p.is_file():
                return True
            if _resolve_repo_file(self.repo_path, cand) is not None:
                return True
        return False

    def _post_write_gate(self) -> tuple[bool, str]:
        """Verifica que el código recién escrito compile. Fail-open: cualquier
        excepción o escenario no soportado devuelve (True, '') para no bloquear."""
        try:
            from verify_gate import syntax_gate
            return syntax_gate(self.repo_path)
        except Exception:
            return True, ""

    def _snapshot_turn_verify(self) -> None:
        """Acumula el estado de verify del intento que termina en la memoria
        del turno (los reintentos limpian _called_tools/_verify_results).

        Sin esto el cierre "olvida" que la verificación CORRIÓ (E2E T006:
        run_verify ✅ + flip, y el cierre fallido posterior afirmaba "SIN
        verificación"). La COMPUERTA sigue exigiendo verify en el intento
        actual (verify stale no vale para seguir escribiendo).
        """
        try:
            called = self._called_tools & VERIFY_TOOL_NAMES
            wrote = bool(self._called_tools & WRITE_TOOL_NAMES)
        except Exception:
            return
        if wrote:
            self._turn_wrote = True
            if self._turn_saw_pass:
                self._turn_writes_after_pass = True
        verify_here = {
            k: v for k, v in self._verify_results.items() if k in called
        }
        if not verify_here:
            return
        self._turn_verify_tools |= set(verify_here)
        self._turn_verify_results.update(verify_here)
        if any(v is True for v in verify_here.values()):
            self._turn_saw_pass = True
        if any(v is False for v in verify_here.values()):
            self._turn_verify_failed = True

    def note_verify_result(self, passed: bool, report: str, details: dict | None = None) -> None:
        """Guarda el resultado de una verificación corrida FUERA del turno
        (/verify o gate de commit) para que el próximo turno vago lo retome.
        Verde → limpia (los rojos quedaron resueltos). Fail-open: nunca raisea.
        """
        try:
            if passed:
                self._last_verify = None
            else:
                det = ""
                if details:
                    bits = []
                    for area, tail in details.items():
                        tail = str(tail or "")[-1500:]
                        if tail:
                            bits.append(f"--- {area} (cola) ---\n{tail}")
                    det = "\n\n".join(bits)[:2000]
                self._last_verify = {
                    "passed": False,
                    "report": (report or "")[:1500],
                    "details": det,
                }
        except Exception:
            pass
        self._persist_turn_state()

    def _current_failing_tests(self) -> list[str]:
        """Unión de fallos del turno (colas del budget), sin duplicados."""
        out: list[str] = []
        seen: set[str] = set()
        try:
            tails = self._explore_budget._failure_tails
        except Exception:
            return []
        for items in (tails or {}).values():
            for it in items or []:
                if it not in seen:
                    seen.add(it)
                    out.append(it)
        return out

    def _ensure_baseline(self) -> list[str] | None:
        """SET de fallos del turno anterior (memoria o disco con snapshot).

        None = se desconoce (sin línea de atribución). [] = estaba todo
        verde (cualquier rojo actual es nuevo). Solo lee disco una vez por
        sesión (después queda en memoria).
        """
        if self._baseline is not None:
            failing = self._baseline.get("failing")
            return list(failing) if isinstance(failing, list) else None
        try:
            from cache import load_test_baseline

            loaded = load_test_baseline(self.repo_path)
        except Exception:
            return None
        failing = loaded.get("failing") if isinstance(loaded, dict) else None
        if not isinstance(failing, list):
            return None
        self._baseline = {"failing": [str(x) for x in failing]}
        return list(self._baseline["failing"])

    def _store_baseline(self) -> None:
        """Guarda el SET de fallos del turno como baseline del siguiente
        (memoria + disco). Cuesta cero baterías: cosecha salidas ya corridas
        por las tools del turno."""
        try:
            from cache import save_test_baseline, snapshot_hash

            failing = self._current_failing_tests()
            try:
                snap = snapshot_hash(self.repo_path)
            except Exception:
                snap = ""
            self._baseline = {"failing": failing}
            save_test_baseline(self.repo_path, failing=failing, snapshot=snap)
        except Exception:
            pass

    def _pop_failed_verify_record(self) -> dict:
        """Registro de la última verificación con rojos, una sola vez (consume).
        Sin rojos guardados → {}."""
        try:
            last = self._last_verify
            self._last_verify = None
        except Exception:
            return {}
        if not last or last.get("passed", True):
            return {}
        return dict(last)

    def _persist_turn_state(self) -> None:
        """Persiste rojos + ancla en disco (sobreviven restart/pull). Fail-open."""
        try:
            from cache import save_repo_state, snapshot_hash

            try:
                snap = snapshot_hash(self.repo_path)
            except Exception:
                snap = ""
            save_repo_state(
                self.repo_path, last_verify=self._last_verify,
                last_tasks_file=self._last_tasks_file, snapshot=snap,
            )
        except Exception:
            pass

    def _pop_failed_verify(self) -> str:
        """Reporte de la última verificación con rojos, una sola vez (consume).
        Sin rojos guardados → ""."""
        return str(self._pop_failed_verify_record().get("report") or "")

    def _verify_tools_called(self, messages: list | None = None) -> bool:
        """True si run_lint, run_tests o run_build fue llamado en el turno actual.

        Usa ``self._called_tools`` (set que alimenta el wrapper de tools en
        cada invocación real). Antes escaneaba ``self._messages`` — pero los
        tool calls viven en el estado del grafo, NO en el historial de la
        sesión, así que la compuerta daba falsos positivos y disparaba un
        turno extra de verificación aunque el modelo YA había verificado."""
        return bool(self._called_tools & VERIFY_TOOL_NAMES)

    def _verify_all_skipped(self) -> bool:
        """True si el turno solo corrió verifies N/A (docs/infra sin stack)."""
        called = self._called_tools & VERIFY_TOOL_NAMES
        if called:
            known = {k: v for k, v in self._verify_results.items() if k in called}
            if known:
                return all(v is None for v in known.values())
            return False
        if not self._turn_verify_tools:
            return False
        return all(v is None for v in self._turn_verify_results.values())

    def _verify_all_passed(self) -> bool | None:
        """Estado de la verificación del turno: True si corrió ≥1 verify tool
        y TODAS pasaron (`[PASSED]`); False si alguna falló; None si no corrió
        ninguna o solo hubo N/A (`[SKIPPED]`: docs/infra sin stack).
        Sin resultados registrados (tests que setean el set a mano) se
        conserva la semántica vieja: llamado = verificado.
        Sin verify en el intento actual, consulta la memoria acumulada del
        turno (los reintentos limpian el estado del intento). Un PASSED
        acumulado solo vale si no hubo escrituras después (verify stale no
        bendice código nuevo); un FAILED acumulado sí contamina el cierre.
        """
        called = self._called_tools & VERIFY_TOOL_NAMES
        if called:
            known = {k: v for k, v in self._verify_results.items() if k in called}
            if not known:
                return True  # compat: sin registro de resultados
            if any(v is False for v in known.values()):
                return False
            if any(v is True for v in known.values()):
                return True
            return None  # solo SKIPPED: se verificó pero no aplicaba
        if self._turn_saw_pass and not self._turn_writes_after_pass:
            return True
        if self._turn_verify_failed or any(
            v is False for v in self._turn_verify_results.values()
        ):
            return False
        return None

    def _changed_files(self) -> list[str]:
        """Archivos realmente modificados en el working tree (git, determinístico).

        NO se le cree al modelo: si git no ve cambios, la tarea no se anuncia
        como hecha. Fail-open: cualquier error devuelve [] (no rompe el turno).
        Filtra artefactos de benchmarks y dirs protegidos: solo cuenta cambios
        tracked ( M, M , A , D , R ) y untracked que NO sean basura
        (benchmarks/, plans/, lucho-plans/, docs/integraciones/ y los dos
        archivos que benchmarks arrastra: scripts/healthcheck.sh,
        src/shared/utils/slug.ts). Sin este filtro el reporte contaba 6
        archivos cuando la sesión solo tocó 2.
        """
        porcelain = self._porcelain_lines()
        if porcelain is None:
            return []
        return self._filter_porcelain(porcelain)

    def _porcelain_lines(self) -> list[str] | None:
        """Líneas crudas de git status --porcelain, o None si git falló.

        Tri-state a propósito: [] = árbol limpio, None = DESCONOCIDO (timeout,
        repo roto). El snapshot del turno necesita distinguirlos: con []
        fallido, el cierre atribuiría al turno archivos que ya estaban sucios.
        """
        try:
            import subprocess
            proc = subprocess.run(
                # -uall: lista archivos untracked UNO POR UNO. Sin esto git
                # colapsa dirs ("src/") y los filtros por archivo no aplican
                # (E2E real 35B: el cierre anunció "7 archivos" cuando el turno
                # tocó 1, sin poder distinguirlo del clutter).
                ["git", "status", "--porcelain", "--untracked-files=all"],
                cwd=self.repo_path, capture_output=True, text=True, timeout=5,
            )
            if proc.returncode != 0:
                return None
            return proc.stdout.splitlines()
        except Exception:
            return None

    @staticmethod
    @staticmethod
    def _filter_porcelain(lines: list[str]) -> list[str]:
        """Filtra líneas porcelain a paths modificados (sin artefactos).

        Fail-open: cualquier error devuelve [] (no rompe el turno).
        """
        try:
            # Prefijos de artefactos que nunca deben contarse como "modificados
            # por la tarea" — son basura untracked de benchmarks o dirs
            # protegidos. Se filtra solo para ?? (untracked); los tracked se
            # cuentan siempre (son cambios reales en el working tree).
            _ARTIFACT_PREFIXES = (
                "benchmarks/",
                "plans/",
                "lucho-plans/",
                "docs/integraciones/",
                ".opencode/",
                ".claude/",
                ".atl/",
                # Clutter untracked de otros agentes/sesiones (E2E real: el
                # cierre anunció "7 archivos" cuando el turno tocó 1).
                ".agents/",
                ".cursor/",
                ".devbase/",
                ".agent/evidence/",
                "scripts/e2e-",
            )
            _ARTIFACT_FILES = frozenset({
                "scripts/healthcheck.sh",
                "src/shared/utils/slug.ts",
            })
            files = []
            for ln in lines:
                if len(ln) < 4:
                    continue
                status = ln[:2]
                path = ln[3:].strip()
                # Solo ?? untracked se filtra como artefacto; los tracked
                # ( M, M , A , D , R , etc.) son cambios reales del repo.
                if status == "??":
                    if path in _ARTIFACT_FILES or any(
                        path.startswith(p) for p in _ARTIFACT_PREFIXES
                    ):
                        continue
                    # También filtra por EXCLUDED_DIRS genérico (vendor,
                    # node_modules, etc.) para no contar binarios/deps.
                    from config import EXCLUDED_DIRS

                    if any(part in EXCLUDED_DIRS for part in Path(path).parts):
                        continue
                files.append(path)
            return files
        except Exception:
            return []

    def _accumulate_session_files(self) -> None:
        """Suma a _session_touched_files lo que el turno ensució (para /commit
        sesion): dirty crudo al cerrar menos dirty crudo al abrir. Fail-open."""
        try:
            if self._turn_start_raw is None:
                return
            cur = self._porcelain_lines()
            if cur is None:
                return
            self._session_touched_files |= _porcelain_paths(cur) - set(self._turn_start_raw)
        except Exception:
            pass

    def _deterministic_close(self) -> str:
        """Resumen de cierre determinístico del SISTEMA (no del modelo).

        Anuncia la tarea realizada con EVIDENCIA REAL: archivos modificados
        (git) + verificación corrida (tools llamadas) + evidencia citada en
        la respuesta (archivo:línea). El modelo 4B alucina "Archivo creado ✅"
        sin haber creado nada; acá el sistema verifica en disco antes de
        decir que está hecho.
        """
        files = self._changed_files()
        verify_state = self._verify_all_passed()
        # Cambios DEL TURNO vs dirty tree pre-existente: el tree puede traer
        # archivos modificados de turnos/sesiones anteriores (.gitignore,
        # tasks.json sin commitear...). Atribuirlos a ESTE turno mentía
        # (E2E T006: "1 archivo modificado" en un turno de solo verificación,
        # era el tasks.json del turno ANTERIOR).
        # _turn_start_dirty None = snapshot fallido (git con timeout): NO se
        # puede atribuir → se lista todo SIN el claim "en este turno".
        try:
            _snap = getattr(self, "_turn_start_dirty", frozenset())
            unknown_start = _snap is None
            pre_dirty = frozenset(_snap or ())
        except Exception:
            unknown_start = True
            pre_dirty = frozenset()
        own_files = files if unknown_start else [f for f in files if f not in pre_dirty]
        turn_suffix = "" if unknown_start else " en este turno"
        scope_line = ""
        try:
            _viol = getattr(self._dedupe, "scope_violations", None) or {}
            if getattr(self, "_scope_nums", None) and _viol:
                _listed = ", ".join(f"Tarea {n}" for n in self._scope_nums)
                _paths = ", ".join(sorted(_viol)[:6])
                scope_line = (
                    f"\n   Alcance {_listed}: ⚠️ {len(_viol)} archivo(s) fuera "
                    f"del alcance pinnado: {_paths}"
                )
        except Exception:
            scope_line = ""
        reds_line = ""
        if verify_state is False:
            # Atribución heredados vs nuevos contra el baseline del turno
            # anterior (cero baterías: cosecha salidas ya corridas).
            try:
                _reds_now = self._current_failing_tests()
                _base_now = self._ensure_baseline()
            except Exception:
                _reds_now, _base_now = [], None
            if _reds_now and _base_now is not None:
                _her_now, _new_now = _split_reds(_reds_now, _base_now) or ([], [])
                reds_line = "\n" + _format_reds_attribution(_her_now, _new_now)
        if own_files:
            head = f"✅ Tarea realizada: {len(own_files)} archivo(s) modificado(s){turn_suffix}"
            detail = "   · " + "\n   · ".join(own_files[:8])
            if len(own_files) > 8:
                detail += f"\n   · …y {len(own_files) - 8} más"
            if verify_state is True:
                verify_line = "   Verificación: lint/tests/build ✅"
            elif verify_state is False:
                verify_line = (
                    "   Verificación: ⚠️ FALLÓ o quedó incompleta "
                    "(ver output arriba) — no commitear sin revisar"
                )
            elif self._verify_all_skipped():
                verify_line = (
                    "   Verificación: N/A — sin stack compilable (docs/infra); "
                    "vale la verificación documental con citas"
                )
            else:
                verify_line = "   Verificación: no se corrió (podés pedirla con 'revisá los cambios')"
            evidence_line = ""
            has_ev = _response_has_evidence(self._last_response)
            if not ((verify_state is True or self._verify_all_skipped()) and has_ev):
                evidence_line = (
                    "\n   Evidencia: ⚠️ la respuesta no cita archivo:línea ni "
                    "verificación exitosa — VERIFICAR CON EVIDENCIA ANTES DE "
                    "CONFIRMAR (revisá el diff antes de commitear)"
                )
            return f"{head}\n{detail}\n{verify_line}{reds_line}{evidence_line}{scope_line}"
        if pre_dirty and not unknown_start:
            head = "↻ Este turno NO modificó archivos propios"
            _shown = sorted(pre_dirty)[:5]
            _rest = len(pre_dirty) - len(_shown)
            detail = (
                f"   · {len(pre_dirty)} modificación(es) previa(s) en el árbol, "
                "sin commitear — no son de este turno: "
                + ", ".join(_shown)
                + (f" (+{_rest} más)" if _rest else "")
            )
            if verify_state is True:
                verify_line = "   Verificación: lint/tests/build ✅"
            elif verify_state is False:
                verify_line = "   Verificación: ⚠️ FALLÓ (ver output arriba)"
            elif self._verify_all_skipped():
                verify_line = "   Verificación: N/A — sin stack compilable (docs/infra)"
            else:
                verify_line = (
                    "   Verificación: no se corrió (sin escrituras propias "
                    "no había nada que verificar con lint/tests/build)"
                )
            return f"{head}\n{detail}\n{verify_line}{reds_line}{scope_line}"
        if verify_state is True:
            return "✅ Turno completado (verificación corrida y exitosa, sin cambios en disco)."
        if verify_state is False:
            return "⚠️ Turno sin cambios en disco y la verificación FALLÓ (ver output arriba)." + reds_line
        return "↻ El turno terminó sin cambios detectados en disco."

    def _failed_turn_close(self) -> str:
        """Mensaje de cierre para turnos EXECUTE fallidos (sin oferta de commit).

        Tres casos, en orden:
        1. Código ROTO en disco → reportar el archivo exacto (no commitear).
        2. Árbol limpio + commit reciente → el trabajo se hizo y commiteó; el
           "fallo" fue solo del cierre (E2E real: loop narrativo post-push
           declarado "fallido" con todo commiteado). Informar, no alarmar.
        3. Resto → mensaje cauteloso original.
        """
        gate_ok, gate_err = self._post_write_gate()
        if not gate_ok:
            return (
                "\n[bold red]⛔ El turno falló y además el código quedó ROTO.[/bold red]\n"
                f"{gate_err}\n"
                "[dim]Revisá y corregí el archivo señalado ANTES de commitear.[/dim]\n"
            )
        # La verificación CORRIÓ en el turno pero el cierre falló (E2E T006:
        # run_verify ✅ + flip, y la cola del turno divagó hasta agotar
        # intentos). Decirlo explícito en vez del genérico "sin verificación"
        # que contradecía el trabajo hecho. No se ofrece commit igual.
        if self._verify_all_passed() is True:
            return (
                "\n[dim]⚠️ El turno no cerró limpio, pero la verificación "
                "CORRIÓ en este turno (lint/tests/build ✅). Revisá el diff "
                "antes de commitear — no se ofrece commit automático.[/dim]"
            )
        if not self._changed_files() and self._commit_during_turn():
            return (
                "\n[dim]✅ Turno completado (cambios ya commiteados — el cierre "
                "final no tenía nada que verificar).[/dim]"
            )
        msg = (
            "\n[dim]↻ Turno fallido (sin verificación) — no se ofrece "
            "commit. Revisá los cambios antes de commitearlos.[/dim]"
        )
        # Flip de planning sin verificación: si el turno marcó tareas como
        # done (tasks.json) pero NUNCA corrió verify —ni en este intento ni
        # en anteriores del turno—, el archivo afirma algo sin respaldo.
        # Decirlo explícito para que no se dé por hecho (E2E T004).
        # Con verify corrido (aunque sea SKIPPED de docs) no hay nada que
        # avisar. Solo informa, nunca bloquea.
        if not (self._called_tools & VERIFY_TOOL_NAMES) and not self._turn_verify_tools:
            flips = [f for f in self._changed_files() if _is_planning_file(f)]
            if flips:
                msg += (
                    "\n[dim]⚠️ " + ", ".join(flips[:5]) + ": marcado(s) como "
                    "done SIN verificación corrida — no dar la tarea por "
                    "hecha hasta verificar.[/dim]"
                )
        try:
            _reds_f = self._current_failing_tests()
            _base_f = self._ensure_baseline()
        except Exception:
            _reds_f, _base_f = [], None
        if _reds_f and _base_f is not None:
            _her_f, _new_f = _split_reds(_reds_f, _base_f) or ([], [])
            msg += "\n[dim]" + _format_reds_attribution(_her_f, _new_f).strip() + "[/dim]"
        return msg

    def _git_cmd(self, args: list[str], timeout: int = 30) -> tuple[int, str]:
        """Ejecuta git en el repo del turno (para slash commands sin LLM)."""
        proc = subprocess.run(
            ["git", *args],
            cwd=self.repo_path, capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode, (proc.stdout or "").strip() + (proc.stderr or "").strip()

    def slash_commit(self, arg: str) -> None:
        """Comando /commit — determinístico, SIN pasar por el LLM.

        Sin alcance: LISTA pendientes (tracked/untracked) marcando [sesión]
        vs [previo] y explica los subcomandos (no commitea nada).
        /commit todo [msg]: stagea tracked (add -u) + gate + commit.
        /commit sesion [msg]: stagea SOLO tracked tocados en esta sesión
        (paths explícitos) + gate + commit.
        Untracked JAMÁS se stagea automático (credenciales): se listan con
        el comando exacto para agregarlos a mano. Repite el flujo de
        _maybe_ask_commit usable desde la TUI (donde el prompt interactivo
        está deshabilitado y el LLM tardaba minutos en commitear).
        """
        from rich.markup import escape

        parts = (arg or "").strip().split(None, 1)
        scope = parts[0].lower() if parts else ""
        message = parts[1].strip() if len(parts) > 1 else ""
        _code, status = self._git_cmd(
            ["status", "--porcelain", "--untracked-files=all"]
        )
        lines = status.splitlines() if status.strip() else []
        if scope not in ("todo", "sesion"):
            console.print(
                escape(_parse_commit_candidates(lines, self._session_touched_files))
            )
            untracked_pick = [
                ln[3:].strip() for ln in lines
                if len(ln) >= 4 and ln.startswith("??")
            ]
            self._pending_commit_pick = untracked_pick or None
            console.print(
                "[dim]" + escape(
                    "Usá /commit todo [mensaje] (todos los tracked) o "
                    "/commit sesion [mensaje] (solo lo tocado en esta sesión). "
                    "Para untracked respondé con números (1 3), 'todos' o "
                    "'ninguno' — nunca se stagean solos."
                ) + "[/dim]"
            )
            return
        paths = _commit_stage_set(lines, scope, self._session_touched_files)
        untracked = [
            ln[3:].strip() for ln in lines
            if len(ln) >= 4 and ln.startswith("??")
        ]
        if not paths:
            # Sin nada stageable no se corre la batería (E2E: 3 baterías
            # seguidas para descubrir que todo era untracked).
            console.print(
                "[yellow]↻ Nada para commitear en ese alcance "
                "(¿todo untracked? agregalo a mano).[/yellow]"
            )
            self._print_untracked_hint(untracked)
            return
        console.print("[dim]🔍 Verificando antes de commitear (lint/tests/build)…[/dim]")
        try:
            passed, report = run_commit_verification(self.repo_path, reuse=self._verify_results)
        except Exception as e:
            console.print(f"[dim]No se pudo verificar ({escape(str(e))}): commiteo igual bajo tu responsabilidad.[/dim]")
            passed, report = True, ""
        console.print(escape(report))
        if not passed:
            console.print(
                "[yellow]⛔ Verificación en rojo — NO commiteo. Corregí arriba o usá /verify.[/yellow]"
            )
            self.note_verify_result(passed, report)
            return
        try:
            if scope == "todo":
                self._git_cmd(["add", "-u"], timeout=15)
            else:
                self._git_cmd(["add", "--", *paths], timeout=15)
            staged_code, staged = self._git_cmd(["diff", "--cached", "--name-only"])
            if staged_code != 0 or not staged.strip():
                console.print(
                    "[yellow]↻ Nada quedó stageado — no commiteo "
                    "(revisá el árbol a mano).[/yellow]"
                )
                return
            if not message:
                n = len(staged.splitlines())
                message = (
                    f"chore: cambios pendientes ({n} archivos)"
                    if scope == "todo"
                    else f"chore: cambios de la sesión ({n} archivos)"
                )
            code, out = self._git_cmd(["commit", "-m", message])
            if code == 0:
                console.print(f"[green]✅ Commit creado: {escape(message)}[/green]")
                self._print_untracked_hint(untracked)
            else:
                console.print(f"[red]⛔ git commit falló:\n{escape(out)}[/red]")
        except Exception as e:
            console.print(f"[red]⛔ git falló: {escape(str(e))}[/red]")

    def try_commit_pick(self, text: str) -> str | None:
        """Consume la selección pendiente de untracked (/commit lista y el
        usuario responde '1 3', 'todos' o 'ninguno'). Retorna el mensaje a
        mostrar si la manejó, None si sigue flujo normal (limpiando el
        pendiente para no secuestrar turnos futuros). Solo stagea paths que
        SIGUEN untracked (revalida contra git)."""
        if not self._pending_commit_pick:
            return None
        from rich.markup import escape

        pending = self._pending_commit_pick
        parsed = _parse_commit_pick(text)
        self._pending_commit_pick = None
        if parsed is None:
            return None
        if parsed == "none":
            return "[dim]OK, no stageo nada.[/dim]"
        _code, status = self._git_cmd(
            ["status", "--porcelain", "--untracked-files=all"]
        )
        still_untracked = {
            ln[3:].strip() for ln in (status.splitlines() if status.strip() else [])
            if len(ln) >= 4 and ln.startswith("??")
        }
        if parsed == "all":
            chosen = [p for p in pending if p in still_untracked]
        else:
            chosen = [
                pending[i - 1] for i in parsed
                if 1 <= i <= len(pending) and pending[i - 1] in still_untracked
            ]
        if not chosen:
            return (
                "[yellow]↻ Ningún número válido (o ya no están untracked). "
                "Pedí /commit de nuevo para ver la lista actual.[/yellow]"
            )
        code, out = self._git_cmd(["add", "--", *chosen], timeout=15)
        if code != 0:
            return f"[red]⛔ git add falló:\n{escape(out)}[/red]"
        return (
            f"[green]✅ Stageados {len(chosen)}: {escape(', '.join(chosen[:8]))}[/green]\n"
            "[dim]Ahora /commit todo [mensaje] o /commit sesion [mensaje] "
            "para commitear.[/dim]"
        )

    def _print_untracked_hint(self, untracked: list[str]) -> None:
        """Lista untracked + comando exacto para agregarlos a mano.

        Untracked JAMÁS se stagea automático (credenciales): el usuario
        copia, revisa y ejecuta. Hasta 12 paths; el resto se avisa.
        """
        from rich.markup import escape

        if not untracked:
            return
        shown = untracked[:12]
        extra = f" (+{len(untracked) - len(shown)} más)" if len(untracked) > len(shown) else ""
        console.print(
            "[dim]Untracked (agregar a mano): "
            + escape(", ".join(shown)) + extra + "[/dim]"
        )
        console.print(
            "[dim]  git add -- " + escape(" ".join(shown)) + "[/dim]"
        )

    def slash_push(self, arg: str) -> None:
        """Comando /push [remote] — push de la rama actual, determinístico."""
        remote = arg.strip() or "origin"
        code, branch_out = self._git_cmd(["branch", "--show-current"])
        branch = branch_out.strip() or "HEAD"
        if not branch:
            console.print("[red]⛔ HEAD detached — no se puede pushear.[/red]")
            return
        console.print(f"[dim]🚀 Pusheando {branch} → {remote}…[/dim]")
        code, out = self._git_cmd(["push", "--", remote, branch])
        if code != 0:
            # Sin upstream: la primera vez push -u lo setea.
            code2, out2 = self._git_cmd(["push", "-u", "--", remote, branch])
            if code2 == 0:
                console.print(f"[green]🚀 Pushed {branch} → {remote} (upstream seteado)[/green]")
                return
            console.print(f"[red]⛔ push falló:\n{(out2 or out)}[/red]\n[dim]Si el remote pide auth, verificá credenciales con agent-devs --doctor.[/dim]")
            return
        console.print(f"[green]🚀 Pushed {branch} → {remote}[/green]")

    def slash_pr(self, arg: str) -> None:
        """Comando /pr [base] — abre PR de la rama actual con gh, determinístico."""
        base = arg.strip() or "main"
        _code, branch = self._git_cmd(["branch", "--show-current"])
        branch = branch.splitlines()[0].strip() if branch else ""
        if not branch:
            console.print("[red]⛔ HEAD detached — no hay rama para PR.[/red]")
            return
        if branch == base:
            console.print(f"[yellow]⛔ La rama actual ES {base}: cambiá de rama antes (create_branch).[/yellow]")
            return
        pretty = branch.split("/", 1)[-1].replace("-", " ").strip() or branch
        title = f"{branch.split('/', 1)[0] or 'feat'}: {pretty}" if "/" in branch else f"feat: {pretty}"
        from langchain_core.tools import ToolException

        from tools.git import create_pr

        console.print(f"[dim]🚀 Creando PR: {branch} → {base} (title: {title})…[/dim]")
        try:
            url = create_pr.invoke({"path": self.repo_path, "title": title, "base": base})
            console.print(f"[green]✅ {url}[/green]")
        except ToolException as e:
            console.print(
                f"[red]⛔ No se pudo crear el PR: {e}[/red]\n"
                "[dim]¿Tenés gh instalado y autenticado (gh auth login)? agent-devs --doctor lo verifica.[/dim]"
            )

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
        if normalize(answer) not in ("y", "yes", "s", "si"):
            console.print("[dim]OK, no se commitea.[/dim]")
            return

        # Gate de commit: verificar ANTES de commitear. Nada roto llega a git:
        # si la batería falla, se aborta y se pide corregir primero.
        console.print("[dim]🔍 Verificando antes de commitear (lint/tests/build)…[/dim]")
        try:
            passed, report = run_commit_verification(self.repo_path, reuse=self._verify_results)
        except Exception as e:
            console.print(f"[dim]No se pudo verificar ({e}): commiteo igual bajo tu responsabilidad.[/dim]")
            passed, report = True, ""
        console.print(report)
        if not passed:
            console.print(
                "[yellow]⛔ Verificación en rojo — NO commiteo. Corregí lo de "
                "arriba y volvé a pedir el commit (o usá /verify cuando quieras).[/yellow]"
            )
            self.note_verify_result(passed, report)
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
        """Punto de entrada de turnos con exclusión mutua (un turno a la vez).

        Un segundo turno concurrente (otra hebra, ej. submit en TUI mientras
        uno corre) se rechaza con aviso en vez de mezclar _messages/tools y
        dejar al ESC apuntando al turno equivocado. La misma hebra puede
        anidar (bulk-chain, retry de credencial). Slash y approvals no llegan
        acá con turno ajeno en curso (la TUI los deja pasar igual).
        """
        if _sources_changed(self._source_snapshot, _WATCHED_SOURCE_FILES):
            console.print(
                "[red]⛔ Los archivos del harness cambiaron bajo esta sesión "
                "abierta (pull o edición con el CLI corriendo): este proceso "
                "sigue corriendo código viejo y puede fallar raro. Cerrá el "
                "programa del todo y abrilo de nuevo (/new NO alcanza: no "
                "recarga módulos). Tu mensaje no se ejecutó.[/red]"
            )
            return
        if not self._try_claim_turn():
            console.print(
                "[yellow]⏳ Hay un turno en curso — esperá a que termine o "
                "cancelalo con ESC. Tu mensaje no se ejecutó: reenvialo.[/yellow]"
            )
            return
        try:
            return self._run_turn_inner(user_input, status=status)
        finally:
            self._release_turn()

    def _try_claim_turn(self) -> bool:
        """True si esta hebra puede correr un turno (con anidado misma-hebra)."""
        ident = threading.get_ident()
        with self._turn_lock:
            if self._turn_thread is not None and self._turn_thread != ident:
                return False
            if self._turn_thread is None:
                self._turn_thread = ident
            self._turn_depth += 1
            return True

    def _release_turn(self) -> None:
        """Libera el reclamo (solo la hebra dueña, por profundidad)."""
        ident = threading.get_ident()
        with self._turn_lock:
            if self._turn_thread != ident:
                return
            self._turn_depth = max(0, self._turn_depth - 1)
            if self._turn_depth == 0:
                self._turn_thread = None

    def _track_turn_task(self, loop, task) -> None:
        """Registra una task viva para que ESC las cancele a TODAS."""
        with self._turn_lock:
            self._turn_tasks.add((id(task), loop, task))

    def _untrack_turn_task(self, task) -> None:
        ident = id(task)
        with self._turn_lock:
            self._turn_tasks = {t for t in self._turn_tasks if t[0] != ident}

    def _reset_turn_state(self) -> None:
        """Limpia el estado POR TURNO (budgets, dedupe, verify, overrides).

        Extraído como método para poder testearlo sin LLM: un error acá rompe
        TODOS los turnos (E2E: clear_failure_tails llamado sobre _dedupe en
        vez de _explore_budget → AttributeError en cada EXECUTE).
        """
        self._dedupe.reset()
        self._explore_budget.reset()
        self._analyze_budget.reset()
        self._called_tools.clear()
        self._verify_results.clear()
        self._explore_budget.clear_failure_tails()
        self._turn_verify_tools = set()
        self._turn_verify_results = {}
        self._turn_wrote = False
        self._turn_saw_pass = False
        self._turn_verify_failed = False
        self._turn_writes_after_pass = False
        # Alcance pinnado por tarea (breaker de scope creep): se recalcula
        # abajo para EXECUTE con Tarea(s) pinnada(s). Los retries del turno
        # reutilizan el mismo dedupe → el contador persiste en el turno.
        self._scope_nums = []
        self._dedupe.scope_files = frozenset()
        self._dedupe.scope_violations = {}
        # Contadores anti-loop por TURNO (los retries NO los borran): writes
        # a planificación protegida y edits no-op. Solo la sesión limpia.
        self._dedupe.protected_rejects = {}
        self._dedupe.noop_rejects = {}
        self._dedupe.fail_guides = {}
        # Primer fallo de verify del turno (lo llena el wrapper): el gate lo
        # usa para enfocar el fix. Se limpia por turno, no por retry.
        self._turn_failures = {}
        self._runtime_healthy = None
        self._runtime_report = None

    def _run_turn_inner(self, user_input: str, status: str | None = None) -> None:
        """Clasifica, cambia rol, ejecuta con historial, persiste en SQLite."""
        # Chequeo rápido: si llama.cpp está apagado, no intentar clasificar
        # (también usa LLM). Evita 60s de timeout + traceback crudo.
        try:
            import urllib.request

            from config import LLM_BASE_URL as _BU
            _base = _BU.split("/v1")[0]
            _alive = False
            for _p in ("/health", "/v1/models"):
                try:
                    with urllib.request.urlopen(_base + _p, timeout=1.0) as _r:
                        if _r.status == 200:
                            _alive = True
                            break
                except Exception:
                    continue
            if not _alive:
                _llama_down_console_msg()
                return
        except Exception:
            pass

        self._readonly_retry = False
        # Pregunta ORIGINAL del turno (los reintentos inyectan HumanMessages
        # propios; sin esto la 2ª vuelta anida "Reanalizá: Reanalizá: ...").
        self._turn_question = user_input
        # ── Edit pendiente por timeout de confirmación ──────────────────
        # Si el turno anterior quedó con un write pendiente (confirmación
        # vencida), y el usuario dice "continua/si/dale" (exacto), reaplicar
        # SIN re-explorar ni LLM. 'continua por favor' mantiene rol pero NO
        # reaplica pendiente si trae instrucción extra.
        _norm_pending = normalize(user_input)
        _pending_exact = _norm_pending in {normalize(w) for w in _CONTINUATION_WORDS}
        if (
            self._pending_write is not None
            and _pending_exact
            and self._try_execute_pending_write(user_input)
        ):
            return
        try:
            intent = classify_intent(self.llm, user_input)
        except BaseException as e:
            if _is_llama_connection_error(e):
                _llama_down_console_msg()
                return
            raise
        new_role = role_for_intent(intent)
        # Instrumentación de routing: el benchmark necesita auditar por qué un
        # prompt cayó en un rol (E2E: 'Creá el archivo' llegó como Análisis).
        console.print(f"[dim]🎯 intent={intent.value} → rol={new_role.value}[/dim]")

        # Continuación: palabras como "continuar", "sigue", "dale" no son
        # verbos EXECUTE pero el usuario claramente quiere seguir con lo
        # que estaba haciendo. Mantener el rol del turno anterior si era
        # EXECUTE o REVIEW (nunca forzar ANALYZE/PLAN).
        if (
            intent == Intent.ANALYZE
            and self.current_role in (Role.EXECUTE, Role.REVIEW)
            and _is_continuation(user_input)
        ):
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

        if new_role != Role.CHAT and (role_changed or not self._role_announced):
            print_role_switch(role_label, self._local_count, self._mcp_count)
            self._role_announced = True

        if status:
            print(status, flush=True)

        if selfcontained:
            console.print("[dim]📐 Pregunta autocontenida — respondiendo directo (explore=0).[/dim]\n")

        # Reset dedupe + explore budget cada turno (siempre restaurar defaults)
        # Orden: primero restaurar max_* a defaults, DESPUÉS reset() — reset()
        # calcula _explore_exhausted desde max_calls, si se resetea con el valor
        # stale (=0 de un retry previo) el turno arranca con lecturas capadas.
        # Bulk scope es por-turno (se re-detecta abajo): resetear para no fugar
        # recursion/budgets/require_write a turnos normales siguientes.
        self._bulk_scope = 0
        self._bulk_task_hash = ""
        self._bulk_current_seq = -1
        self._analyze_preloaded = False
        self._dedupe.max_repeats = 1
        if new_role == Role.EXECUTE:
            self._explore_budget.max_calls = EXECUTE_EXPLORE_BUDGET
            self._explore_budget.max_reads_after_explore = EXECUTE_MAX_READS_AFTER_EXPLORE
            self._explore_budget.max_tools_before_write = EXECUTE_MAX_TOOLS_BEFORE_WRITE
            self._explore_budget.max_writes_before_verify = EXECUTE_MAX_WRITES_BEFORE_VERIFY
            self._explore_budget.max_verify_before_write = EXECUTE_MAX_VERIFY_BEFORE_WRITE
            self._explore_budget.write_pressure = True
        elif new_role == Role.REVIEW:
            self._explore_budget.max_calls = REVIEW_EXPLORE_BUDGET
            self._explore_budget.max_reads_after_explore = REVIEW_MAX_READS_AFTER_EXPLORE
            self._explore_budget.max_tools_before_write = REVIEW_MAX_TOOLS_BEFORE_WRITE
            self._explore_budget.max_writes_before_verify = EXECUTE_MAX_WRITES_BEFORE_VERIFY
            self._explore_budget.max_verify_before_write = EXECUTE_MAX_VERIFY_BEFORE_WRITE
            # REVIEW nunca escribe: sin write-pressure (si no, al superar 30
            # tools le ordena "write_file AHORA", incorrecto para reviewer).
            self._explore_budget.write_pressure = False
        elif new_role == Role.ANALYZE:
            self._analyze_budget.max_calls = ANALYZE_EXPLORE_BUDGET
            self._analyze_budget.max_reads_after_explore = ANALYZE_MAX_READS_AFTER_EXPLORE
        elif new_role == Role.PLAN:
            self._analyze_budget.max_calls = PLAN_EXPLORE_BUDGET
            self._analyze_budget.max_reads_after_explore = PLAN_MAX_READS_AFTER_EXPLORE
        self._reset_turn_state()
        # Los overrides de write_file (habilitados tras fallar la cirugía fina
        # de edit_file) son por TURNO: limpiar para que el próximo turno
        # arranque con los guards de sobrescritura activos.
        try:
            from tools.filesystem import clear_task_allow, clear_write_overrides
            clear_write_overrides()
            clear_task_allow()
            import tools.git as _gitmod

            _gitmod.BRANCH_CHANGE_ALLOWED = True
        except Exception:
            pass
        # Archivos de planificación citados EXPLÍCITAMENTE por el usuario
        # ("marcá Done en .agent/tasks.json"): su orden gana a la protección
        # anti-corrupción (que solo frena iniciativa propia del modelo).
        try:
            from tools.filesystem import TASK_PATH_ALLOW, _is_protected_task_path

            for _p in _collect_cited_paths(user_input, self.repo_path):
                if _is_protected_task_path(str(_p)):
                    TASK_PATH_ALLOW.add(str(_p))
                    console.print(
                        f"[dim]📎 Archivo protegido citado por vos: {Path(_p).name} "
                        f"(edición permitida este turno).[/dim]"
                    )
        except Exception:
            pass

        # Acumular el mensaje del usuario en el historial
        agent_input = user_input
        if new_role == Role.EXECUTE:
            agent_input = preload_cited_files(user_input, self.repo_path)
            # Alcance pinnado: archivos citados en las entradas pinnadas
            # (T002). Las escrituras fuera del alcance cuentan en el wrapper
            # y el runaway se frena con excepción (ver SCOPE_MAX_* en config).
            try:
                from orchestration.execute_bootstrap import pinned_task_scope_files

                _nums = extract_requested_task_numbers(user_input)
                if _nums:
                    self._scope_nums = _nums
                    self._dedupe.scope_files = frozenset(
                        pinned_task_scope_files(user_input, self.repo_path)
                    )
            except Exception:
                pass
            # create_branch por iniciativa propia: bloqueado salvo orden
            # explícita o main/master (E2E T012: creó feat/... sin que se lo
            # pidieran, estando ya en feature branch; el prompt solo lo
            # permite en main/master). Silencioso: si el modelo lo intenta,
            # la tool devuelve el bloqueo con la rama actual.
            try:
                import tools.git as _gitmod

                _branch_ok = True
                if not _BRANCH_INTENT_RE.search(user_input or ""):
                    _code, _cur = self._git_cmd(["branch", "--show-current"])
                    _cur = (_cur or "").strip().splitlines()[0] if _cur else ""
                    if _code == 0 and _cur and _cur not in ("main", "master"):
                        _branch_ok = False
                _gitmod.BRANCH_CHANGE_ALLOWED = _branch_ok
            except Exception:
                pass
            # Ancla de la lista de tareas: el primer planning citado que
            # exista en disco queda registrado para los turnos vagos.
            try:
                for _cp in _collect_cited_paths(user_input, self.repo_path):
                    if _is_planning_file(str(_cp)):
                        if self._last_tasks_file != str(_cp):
                            self._last_tasks_file = str(_cp)
                            self._persist_turn_state()
                        break
            except Exception:
                pass
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
            # Opt-in: PATH_FIX_ENABLED (config) — modifica código REAL del
            # usuario, se disclosia en consola.
            if PATH_FIX_ENABLED and detect_path_mismatches(self.repo_path):
                def _path_fix_confirm(path: str, desc: str) -> bool:
                    # Sin TUI interactiva: fail-open (igual que antes). Con TUI:
                    # una aprobación por archivo; rechazar/vencer solo omite
                    # ese archivo, sin cancelar el turno ni dejar pendiente.
                    return self._confirm_write_cb(
                        "path_fix", {"path": path, "desc": desc},
                        cancel_on_reject=False, keep_pending=False,
                    )
                fix_report = apply_mismatch_fixes(
                    self.repo_path, confirm_fn=_path_fix_confirm)
                if fix_report:
                    console.print(
                        "[yellow]🔧 PATH FIX: corregí mismatches de paths "
                        "frontend↔backend determinísticamente "
                        "(config PATH_FIX_ENABLED=False para desactivar)[/yellow]\n"
                    )
                    agent_input = f"{agent_input}\n\n{fix_report}"
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
                try:
                    from orchestration.execute_bootstrap import pinned_task_header

                    _task_header = pinned_task_header(user_input, self.repo_path)
                except Exception:
                    _task_header = ""
                if _task_header:
                    console.print(f"[bold cyan]{_task_header}[/bold cyan]\n")
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
                # Rojos frescos mandan sobre el historial (E2E: /verify con
                # tests ❌ y luego "implementar" encadenaba el ÉXITO viejo
                # porque los rojos se evaporaban entre turnos). Se consumen
                # una sola vez; verde en un turno los limpia (ver cierre).
                record = self._pop_failed_verify_record()
                reds = str(record.get("report") or "")
                task = None if reds else _derive_task_from_history(self._messages)
                if reds:
                    agent_input = user_input + _build_failed_verify_suffix(
                        reds, details=str(record.get("details") or "")
                    )
                    console.print("[dim]🔗 Retomando rojos de la última verificación (sin re-ejecutar la batería).[/dim]\n")
                _anchor = _tasks_anchor_line(self._last_tasks_file)
                if _anchor:
                    agent_input += _anchor
                if task:
                    targets = _extract_target_files(task)
                    if _has_grounded_evidence(task) and targets:
                        agent_input = user_input + _build_chained_execute_suffix(
                            task, target_files=targets
                        )
                        # Mismos límites que la corrección post-review: prohibir
                        # exploración y forzar lectura-directa de los objetivos.
                        self._explore_budget.max_calls = 0
                        self._explore_budget.max_tools_before_write = 4
                        console.print("[dim]🔗 Retomando análisis previo como tarea (explore=0).[/dim]\n")
                    else:
                        # Análisis previo SIN evidencia (sin file:línea ni código
                        # citado): encadenarlo con explore=0 perpetúa una
                        # hipótesis sin sustento. Se mantiene presupuesto de
                        # exploración y se exige verificar antes de escribir.
                        agent_input = (
                            user_input
                            + "\n\n⛔ El análisis previo NO trae evidencia grounded "
                            "(sin archivo:línea ni código citado). NO lo tomes como "
                            "verdad: explorá el repo (trace_component/read_file) y "
                            "VERIFICÁ CON EVIDENCIA ANTES DE CONFIRMAR cualquier "
                            "diagnóstico previo antes de escribir código."
                        )
                        console.print("[dim]🔗 Análisis previo sin evidencia — explore acotado, verificar antes de escribir.[/dim]\n")
            # Orden de CORRECCIÓN explícita ("corregir los errores de tests"):
            # no entra a la rama ambigua pero los rojos guardados mandan igual
            # (E2E: re-ejecutó batería + run_tests sin reds). Una sola vez;
            # convive con preload pinnado (los números salen de user_input).
            if not _is_ambiguous_execute(user_input, self.repo_path):
                _corr = _CORRECTION_INTENT_RE.search(user_input or "")
                if _corr:
                    _crec = self._pop_failed_verify_record()
                    if _crec:
                        agent_input += _build_failed_verify_suffix(
                            str(_crec.get("report") or ""),
                            details=str(_crec.get("details") or ""),
                        )
                        console.print("[dim]🔗 Retomando rojos de la última verificación (sin re-ejecutar la batería).[/dim]\n")
        elif new_role == Role.REVIEW:
            agent_input = preload_for_review(user_input, self.repo_path)
            self._dedupe.max_repeats = 1
            if agent_input != user_input:
                # Con git context precargado necesita IGUAL explorar un poco:
                # ubicar código por search/list antes de leer (con 1 sola el
                # budget muere al primer search y el turno escalaba a retry de
                # escritura — E2E real T1b/9B). Las lecturas siguen acotadas.
                self._explore_budget.max_calls = 4
                # Reviewer needs to read ALL modified files + run verify tools
                self._explore_budget.max_reads_after_explore = 15
                self._explore_budget.max_tools_before_write = 30
                nums = extract_requested_task_numbers(user_input)
                scope = f" (Tarea(s) {', '.join(map(str, nums))})" if nums else ""
                console.print(
                    f"[dim]📎 Checklist AC pre-cargado para review{scope}.[/dim]\n"
                )
        elif new_role == Role.ANALYZE:
            # Archivos citados (.md/.txt): inyectar contenido + checklist para
            # que la verificación se haga contra el código con evidencia, no
            # desde el análisis cacheado (E2E real: veredicto sin leer nada).
            # Sin citados devuelve el input intacto (cero cambio de conducta).
            self._analyze_preloaded = False
            preloaded = preload_for_analyze(user_input, self.repo_path)
            if preloaded != user_input:
                agent_input = preloaded
                self._analyze_preloaded = True
                nums = extract_requested_task_numbers(user_input)
                scope = f" (Tarea(s) {', '.join(map(str, nums))})" if nums else ""
                console.print(
                    f"[dim]📎 Tareas citadas pre-cargadas para verificación{scope} "
                    f"(verificá cada ítem contra el código).[/dim]\n"
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
                            f"{progress.get('in_progress', 0)} en curso · "
                            f"{progress['pending']} pendientes.[/dim]\n"
                        )
                    elif created or (progress["pending"] == 0 and progress.get("in_progress", 0) == 0):
                        console.print(
                            f"[dim]📦 Tarea bulk ya COMPLETA según la cola "
                            f"({progress['done']}/{total_b} batches done). Si "
                            f"querés re-ejecutarla, cambiá el texto del prompt.[/dim]\n"
                        )

        self._messages.append(HumanMessage(agent_input))

        # Foto del dirty tree AL INICIAR el turno: el cierre atribuye a la
        # tarea SOLO lo que cambió desde acá (T006: tasks.json del turno
        # anterior aparecía como "1 archivo modificado" en un turno de solo
        # verificación). Tri-state: None si git falló → el cierre NO hace
        # claim de atribución (lista todo sin "en este turno").
        _snap_lines = self._porcelain_lines()
        self._turn_start_dirty = (
            None if _snap_lines is None
            else frozenset(self._filter_porcelain(_snap_lines))
        )
        self._turn_start_raw = (
            None if _snap_lines is None
            else set(_porcelain_paths(_snap_lines))
        )
        self._turn_start_ts = time.time()

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
        evidence_retried = False
        cite_retried = False
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
            # require_write es SOLO de EXECUTE. REVIEW también lo tenía en
            # retries (attempt>0) y castigaba al reviewer por no escribir:
            # un review que termina con informe y sin writes es CORRECTO
            # (E2E real T1b/9B: dos retries "con foco en escritura" en un
            # review que ya había leído todo → cierre vacío). REVIEW exige
            # TEXTO (require_text), nunca escritura.
            require_write = (
                new_role == Role.EXECUTE
                and EXECUTE_REQUIRE_WRITE
                and REASONING_RETRY_ENABLED
                and not self._bulk_task_hash
                # Verificación pura: el turno no debe escribir; castigarlo
                # con no-write retry empuja al modelo a inventar fixes
                # destructivos (E2E real: 'main.go truncado' inexistente).
                and not _is_verification_only(user_input)
            )
            task = loop.create_task(
                stream_agent_turn(
                    self.agent,
                    messages_for_agent,
                    config,
                    idle_timeout=TURN_IDLE_TIMEOUT,
                    # EXECUTE: 90s por bloque de razonamiento (el 4B razona
                    # 30-60s antes de cada tool call; si razona más, se colgó).
                    # Retry de solo-lectura: SIN corte (None) — el 4B necesita
                    # minutos para componer la respuesta final con evidencia
                    # (E2E real T1: dos cortes de 180s → respuesta vacía).
                    # idle_timeout cubre un modelo realmente colgado.
                    max_reasoning_seconds=(
                        None if self._readonly_retry
                        else (
                            EXECUTE_MAX_REASONING_SECONDS
                            if new_role == Role.EXECUTE
                            else MAX_REASONING_SECONDS
                        )
                    ),
                    max_reasoning_chars=(
                        None if self._readonly_retry
                        else (
                            EXECUTE_MAX_REASONING_CHARS
                            if new_role == Role.EXECUTE
                            else MAX_REASONING_CHARS
                        )
                    ),
                    max_tool_calls=(
                        _bulk_budget(self._bulk_scope)["tool_calls_per_turn"]
                        if new_role == Role.EXECUTE and self._bulk_scope >= EXECUTE_BULK_MIN_FILES
                        else MAX_TOOL_CALLS_PER_TURN
                    ),
                    require_write=require_write,
                    max_content_seconds=(
                        EXECUTE_MAX_CONTENT_SECONDS
                        if new_role == Role.EXECUTE
                        else None
                    ),
                    # ANALYZE/PLAN/REVIEW deben cerrar con texto: si corrieron
                    # tools pero la respuesta quedó vacía (corte o cierre vacío
                    # del modelo), reintentar con el ancla en vez de guardar ''.
                    # REVIEW exige informe en texto (nunca escritura).
                    # EXECUTE no lo usa (cierre determinístico propio).
                    require_text=new_role in (Role.ANALYZE, Role.PLAN, Role.REVIEW),
                )
            )

            # Watcher de ESC: permite interrumpir el streaming y volver al prompt.
            # En full-screen stdin lo dueña la TUI → ESC llega por key binding
            # (request_cancel) y el watcher NO debe arrancar (competería por
            # los bytes del teclado con prompt_toolkit).
            watcher = None
            if not self._fullscreen:
                watcher = EscWatcher(
                    # Bindeo por valor (B023): el callback puede dispararse
                    # tarde desde el thread del watcher, cuando `loop`/`task`
                    # ya apuntan al reintento siguiente. Como cancela solo su
                    # intento, _cancel_turn() cubre además a TODOS los vivos.
                    cancel_cb=lambda _loop=loop, _task=task: _loop.call_soon_threadsafe(
                        _task.cancel
                    )
                )
                watcher.start()
            self._track_turn_task(loop, task)

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
                # GUARD PLAN-EXPLORA: el rol PLAN debe EXPLORAR antes de
                # planificar (E2E real: los 3 modelos respondían del análisis
                # cacheado con 0 tools → planes sin archivos concretos). Si el
                # turno terminó sin ninguna llamada de exploración y quedan
                # intentos, forzamos un retry que pide explícitamente
                # explorar antes de redactar.
                if (
                    new_role == Role.PLAN
                    and not self._readonly_retry
                    and not (self._called_tools & EXPLORE_TOOL_NAMES)
                    and attempt + 1 < max_attempts
                ):
                    attempt += 1
                    console.print(
                        "\n[yellow]📋 PLAN sin exploración: reintentando con "
                        "exigencia de explorar (list_files/read_file/inspect) "
                        "antes de redactar el plan…[/yellow]\n"
                    )
                    retry_plan = (
                        user_input
                        + "\n\n⛔ Tu plan anterior salió SIN explorar el código "
                        "(0 tool calls de exploración). Un plan válido REQUIERE "
                        "evidencia: 1) list_files de la estructura relevante, "
                        "2) read_file de los archivos clave, 3) inspect_routes/"
                        "inspect_models si aplica. Recién DESPUÉS de explorar, "
                        "redactá el plan con archivos concretos."
                    )
                    self._messages.append(HumanMessage(retry_plan))
                    messages_for_agent = list(self._messages)
                    continue
                # GUARD ANALYZE-EVIDENCIA: el usuario citó archivos concretos
                # (preload con checklist) y el turno terminó sin leer NADA de
                # código ni citar evidencia → el veredicto vendría del caché.
                # UNA vez se reintenta exigiendo lecturas (E2E real: "verificá
                # si están hechas estas tareas file.md" respondió sin abrir ni
                # el .md ni el código).
                if (
                    new_role == Role.ANALYZE
                    and not evidence_retried
                    and self._analyze_preloaded
                    and not (self._called_tools & (EXPLORE_TOOL_NAMES | READISH_TOOL_NAMES))
                    and not _response_has_evidence(self._last_response)
                    and attempt + 1 < max_attempts
                ):
                    evidence_retried = True
                    attempt += 1
                    console.print(
                        "\n[yellow]🔍 Veredicto sin evidencia (0 lecturas de código) — "
                        "reintentando con exigencia de leer y citar…[/yellow]\n"
                    )
                    self._messages.append(HumanMessage(
                        "⛔ Tu veredicto anterior salió SIN leer código (0 tool calls "
                        "de lectura) y SIN citar evidencia. Eso NO es verificar: es "
                        "adivinar desde el caché.\n"
                        "REHACÉ la verificación AHORA: 1) el contenido de la tarea "
                        "YA ESTÁ ARRIBA, no lo releas; 2) leé con read_file/"
                        "trace_component el CÓDIGO de CADA ítem del checklist; "
                        "3) dictaminá cumplido/no-cumplido citando archivo:línea "
                        "de lo que cada tool devolvió. Si algo no se puede "
                        "verificar, decilo explícito."
                    ))
                    messages_for_agent = list(self._messages)
                    continue
                # GUARD CITAS-FANTASMA: el informe cita [archivo:línea] que no
                # existe en disco (archivo inexistente, línea 0 o fuera de
                # rango — E2E real T1b/9B: `src/lib/patient.service.ts:0` en
                # un repo NestJS). UNA vez se reintenta exigiendo corregir.
                # Negativa honesta SIN citas pasa (no se castiga pedir datos).
                # Solo REVIEW/ANALYZE: PLAN cita archivos por crear.
                if (
                    new_role in (Role.ANALYZE, Role.REVIEW)
                    and not cite_retried
                    and attempt + 1 < max_attempts
                ):
                    from orchestration.evidence import find_unverifiable_cites

                    bad = find_unverifiable_cites(
                        self._last_response, self.repo_path)
                    if bad:
                        cite_retried = True
                        attempt += 1
                        shown = ", ".join(bad[:8])
                        console.print(
                            "\n[yellow]🔍 Citas sin respaldo en disco "
                            f"({shown}) — reintentando con exigencia de "
                            "corregir…[/yellow]\n"
                        )
                        self._messages.append(HumanMessage(
                            f"⛔ Tu respuesta cita evidencia que NO existe en "
                            f"disco: {shown}. Cada [archivo:línea] debe ser un "
                            f"archivo REAL del repo con la línea dentro del "
                            f"rango (la línea 0 no existe). Corregí el informe "
                            f"citando SOLO paths que leíste con tools, o marcá "
                            f"esos puntos como NO verificados sin citarlos. "
                            f"Respondé el informe corregido AHORA."
                        ))
                        messages_for_agent = list(self._messages)
                        continue
                break
            except KeyboardInterrupt:
                interrupted = True
                task.cancel()
                with contextlib.suppress(
                    asyncio.TimeoutError, asyncio.CancelledError, Exception
                ):
                    loop.run_until_complete(asyncio.wait_for(task, timeout=2.0))
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
            except VerifyRequired:
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
                self._snapshot_turn_verify()
                self._called_tools.clear()
                self._verify_results.clear()
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
                if new_role in (Role.ANALYZE, Role.PLAN, Role.REVIEW):
                    # Retry de solo-lectura (NUNCA write-only): estos roles no
                    # escriben; leen archivos clave y responden con evidencia.
                    # REVIEW caía en _enter_budget_retry y GANABA edit_file
                    # (E2E real T1b/9B: 5 edits a package.json en un review).
                    self._retry_analyze_no_explore(
                        new_role,
                        f"Exploración agotada en {role_label}: {e}",
                    )
                    messages_for_agent = list(self._messages)
                    continue
                # GUARD "ya escribió": si el intento anterior YA tuvo un write
                # efectivo (write/edit/delete), el trabajo está hecho — NO
                # reintentar write-only (re-escribiría lo mismo y volvería a
                # pedir aprobación). El 4B a veces no sabe cerrar el turno y
                # sigue llamando tools (git_status/lint) hasta agotar el
                # budget; re-escribir es destructivo y duplica el trabajo.
                # PERO sin verify en el intento, cerrar directo deja un éxito
                # sin respaldo (E2E T005: "Tarea realizada" con
                # "Verificación: no se corrió"). Una sola inyección de la
                # compuerta antes de cerrar — acotada por gate_retries.
                # MISMO rebuild que la compuerta post-write: el agente actual
                # puede ser write-only (retry sin verify tools) y el gate le
                # pediría tools que no tiene → el modelo alucina "no
                # disponibles" y loopa (E2E turno rojo: CPA_DONE ×N tras
                # "Verificando los cambios"). Sin rebuild, mejor cerrar.
                # El cierre determinístico lo imprime el bloque post-turno.
                if self._called_tools & WRITE_TOOL_NAMES:
                    if (
                        gate_retries < POST_WRITE_GATE_MAX_RETRIES
                        and not self._verify_tools_called()
                    ):
                        gate_retries += 1
                        self._inject_verify_gate()
                        messages_for_agent = list(self._messages)
                        self._explore_budget.max_calls = 3
                        self._explore_budget.max_reads_after_explore = 4
                        self._explore_budget.max_tools_before_write = 6
                        self._explore_budget.reset()
                        self._dedupe.max_repeats = 2
                        self._rebuild_agent_gate_retry()
                        console.print(
                            "\n[dim]↻ Verificando los cambios "
                            "(lint/tests/build)…[/dim]\n"
                        )
                        continue
                    self._last_response = self._deterministic_close()
                    break
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
                if new_role in (Role.ANALYZE, Role.PLAN, Role.REVIEW):
                    # Estos roles nunca van write-only: retry de solo-lectura
                    # y respuesta con evidencia (ver fix del handler de arriba).
                    if isinstance(e, ToolCallLimitExceeded):
                        reason = f"El modelo hizo {e.total_calls} tool calls (loop)"
                    elif getattr(e, "reason", "") == "empty-after-tools":
                        reason = (
                            "El modelo corrió tools pero cerró sin texto "
                            "(respuesta vacía)"
                        )
                    else:
                        reason = f"El modelo gastó {len(e.reasoning_text)} chars razonando sin actuar"
                    self._retry_analyze_no_explore(new_role, reason)
                    messages_for_agent = list(self._messages)
                    continue
                if isinstance(e, ToolCallLimitExceeded):
                    retry_msg = (
                        f"Muchas tool calls seguidas ({e.total_calls}). "
                        "Reintentando con lectura acotada + escritura…"
                    )
                elif getattr(e, "reason", "") == "no-write":
                    # Cierre honesto SIN retry: si no hay nada pendiente por
                    # escribir, forzar escritura solo produce no-ops
                    # destructivos (E2E real: turno "hacer commit" tras e7c8f3a
                    # → el retry inventó edits sobre archivos commiteados).
                    # El escape vago (sin commit reciente ni verify) SÍ reintenta.
                    if new_role == Role.EXECUTE and self._nothing_pending_to_write():
                        if e.reasoning_text.strip():
                            self._last_response = e.reasoning_text
                        console.print(
                            "\n[dim]✅ Nada pendiente por escribir (trabajo ya "
                            "commiteado o verificado) — cerrando sin "
                            "reintentar.[/dim]"
                        )
                        _verdict = _closing_verdict_excerpt(self._last_response)
                        if _verdict:
                            console.print(f"\n[dim]📋 Veredicto del turno:\n{_verdict}[/dim]")
                        break
                    # Turno ya-implementado con evidencia: el modelo leyó ≥2
                    # archivos/tools y dictaminó "ya cumple el AC" con
                    # archivo:línea (E2E real T003: 2 reads + veredicto →
                    # retry forzado produjo 5 no-ops + 4 protected). Cierre
                    # honesto sin retry.
                    if new_role == Role.EXECUTE and self._readonly_evidence_turn(
                        e.reasoning_text or ""
                    ):
                        self._last_response = e.reasoning_text or ""
                        console.print(
                            "\n[dim]✅ Solo-lectura con evidencia — nada que "
                            "escribir, cerrando sin reintentar.[/dim]"
                        )
                        _verdict = _closing_verdict_excerpt(self._last_response)
                        if _verdict:
                            console.print(f"\n[dim]📋 Veredicto del turno:\n{_verdict}[/dim]")
                        break
                    retry_msg = "Reintentando con lectura acotada + escritura…"
                else:
                    # Éxito ya verificado: el modelo divaga pensando tras
                    # terminar (E2E T006: 3×150s de thinking post-éxito con
                    # retries que terminaron en un cierre fallido que
                    # contradecía el trabajo hecho). Reintentar pensar no
                    # aporta nada: cerrar con el resumen determinístico.
                    # Vale con evidencia del intento actual o acumulada del
                    # turno (los reintentos limpian el estado del intento).
                    if (
                        new_role == Role.EXECUTE
                        and (
                            bool(self._called_tools & WRITE_TOOL_NAMES)
                            or self._turn_wrote
                        )
                        and self._verify_all_passed() is True
                    ):
                        console.print(
                            "\n[dim]✅ Trabajo verificado — el modelo seguía "
                            "razonando sin actuar: cerrando sin reintentar.[/dim]"
                        )
                        self._last_response = self._deterministic_close()
                        break
                    retry_msg = (
                        f"El modelo gastó {len(e.reasoning_text)} chars razonando. "
                        "Reintentando con lectura acotada + escritura…"
                    )
                messages_for_agent = self._enter_budget_retry(retry_msg)
                continue
            except Exception as e:
                if _is_llama_connection_error(e):
                    _llama_down_console_msg()
                    turn_failed = True
                    interrupted = True
                    break
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
                    self._snapshot_turn_verify()
                    self._called_tools.clear()
                    self._verify_results.clear()
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
                self._untrack_turn_task(task)
                loop.close()

        elapsed = time.monotonic() - start
        self._session_time += elapsed
        if new_role == Role.EXECUTE:
            self._accumulate_session_files()

        usage = get_usage()
        turn_tokens = usage["turn"]["prompt"] + usage["turn"]["completion"]

        if not interrupted:
            self._messages.append(AIMessage(self._last_response or "(respuesta generada)"))

        # Judge: valida reviews que dicen APROBADO
        if new_role == Role.REVIEW and not interrupted:
            self._maybe_judge_review(user_input)

        with contextlib.suppress(Exception):
            save_turn(
                session_id=self.session_id,
                repo_path=self.repo_path,
                role=new_role.value,
                user_message=user_input,
                assistant_message=self._last_response or "",
                tokens_used=turn_tokens,
            )
        self._flush_evidence()

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
        # Cierre determinístico: el SISTEMA anuncia la tarea realizada con
        # evidencia real (git) en vez de creerle al resumen del modelo (el 4B
        # alucina "Archivo creado ✅"). En dim para no duplicar la narrativa
        # del LLM — el sistema verifica, el modelo narra.
        if new_role == Role.EXECUTE and not interrupted and not turn_failed:
            console.print(f"\n[dim]{self._deterministic_close()}[/dim]")
            if self._verify_all_passed() is True:
                self._last_verify = None  # verde en el turno: rojos resueltos
            self._store_baseline()  # el cierre de este turno es baseline del próximo
        _bulk_more_pending = (
            _bulk_chain is not None
            and next_pending_batch(_bulk_chain[0]) is not None
        )
        if new_role == Role.EXECUTE and not interrupted and not turn_failed:
            if not _bulk_more_pending:
                self._maybe_ask_commit(user_input)
        elif new_role == Role.EXECUTE and turn_failed and not interrupted:
            console.print(self._failed_turn_close(), end="")
            self._store_baseline()  # los rojos observados son baseline del próximo
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
                            "[dim]Credencial recibida — reintentando con tu input…[/dim]\n"
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

        # Build judge message (con el prompt del judge: sin esto el LLM
        # juzgaba sin instrucciones — el prompt se cargaba y se tiraba).
        judge_message = (
            f"{judge_prompt}\n\n"
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

    def toggle_auto_approve(self, arg: str = "") -> str:
        """Prende/apaga/revierte la auto-aprobación de escrituras (/autoapprove).

        Alcance: solo aprobaciones de write/edit/delete (+ PATH FIX). El
        commit sigue siendo manual siempre. Se apaga solo con /new.
        """
        a = (arg or "").strip().lower()
        if a in ("on", "si", "sí", "s", "1", "true", "activar"):
            self._auto_approve = True
        elif a in ("off", "no", "n", "0", "false", "desactivar"):
            self._auto_approve = False
        elif a:
            return (
                f"⛔ Uso: /autoapprove [on|off] (recibí {arg!r}). "
                f"Estado actual: {'ON' if self._auto_approve else 'OFF'}."
            )
        else:
            self._auto_approve = not self._auto_approve
        if self._auto_approve:
            return (
                "✅ Auto-approve ON: las escrituras se aprueban sin preguntar "
                "(vale para esta sesión; /new lo apaga). El commit sigue "
                "siendo manual."
            )
        return "✅ Auto-approve OFF: vuelvo a pedir confirmación por escritura."

    def resume_session(self, session_prefix: str) -> tuple[bool, str, list[dict]]:
        """Retoma una sesión previa del MISMO repo por id (completo o prefijo).

        Restaura _messages (pares usuario/asistente), session_id (los próximos
        turnos se guardan en la misma sesión), rol y última respuesta, y
        reconstruye el agente del rol. Retorna (ok, mensaje, turnos).
        """
        from cache import normalize_path

        prefix = (session_prefix or "").strip()
        if not prefix:
            return False, "Uso: /resume <id> (mirálos con /history).", []
        try:
            recent = load_recent_turns(self.repo_path, limit=50)
        except Exception:
            return False, "⛔ No se pudo leer el historial.", []
        sids: list[str] = []
        for t in recent:
            sid = t.get("session_id") or ""
            if sid and sid not in sids:
                sids.append(sid)
        matches = [s for s in sids if s.startswith(prefix)]
        if not matches:
            # ¿Existe pero en OTRO repo? (id completo de otro proyecto)
            if len(prefix) >= 4:
                try:
                    foreign = load_session_turns(prefix)
                except Exception:
                    foreign = []
                if foreign:
                    return False, f"⛔ La sesión {prefix} es de otro repo. Parate en ese repo para retomarla.", []
            return False, f"⛔ No hay sesión '{prefix}' en este repo. Mirá los ids con /history.", []
        if len(matches) > 1:
            return False, f"⛔ Prefijo ambiguo: {', '.join(matches)}. Pasá más caracteres.", []
        sid = matches[0]
        try:
            data = load_session_turns(sid)
        except Exception:
            return False, "⛔ No se pudo cargar esa sesión.", []
        if not data:
            return False, f"⛔ La sesión {sid} no tiene turnos.", []
        try:
            here = normalize_path(self.repo_path)
        except Exception:
            here = self.repo_path
        if any((t.get("repo_path") or "") != here for t in data):
            return False, f"⛔ La sesión {sid} es de otro repo. Parate en ese repo para retomarla.", []
        # Tope: últimas 20 — sesiones larguísimas reventarían el contexto.
        shown_truncated = len(data) > 20
        data = data[-20:]
        msgs: list = []
        for t in data:
            u = (t.get("user_message") or "").strip()
            a = (t.get("assistant_message") or "").strip()
            if u:
                msgs.append(HumanMessage(u))
            if a:
                msgs.append(AIMessage(a))
        if not msgs:
            return False, f"⛔ La sesión {sid} no tiene contenido recuperable.", []
        self._messages = msgs
        self.session_id = sid
        self._last_response = (data[-1].get("assistant_message") or "").strip()
        self._read_cache.clear()
        self._session_time = 0.0
        self._role_announced = False
        raw_role = (data[-1].get("role") or "").strip()
        try:
            role = Role(raw_role)
        except ValueError:
            role = Role.ANALYZE
        self._rebuild_agent(role)
        extra = " (últimos 20 turnos)" if shown_truncated else ""
        return True, f"✅ Sesión {sid} retomada: {len(data)} turno(s){extra}. Seguí donde habías quedado.", data

    def _cancel_turn(self) -> None:
        """Cancela TODOS los turnos vivos (mismo camino que ESC).

        Antes apuntaba solo al último (_turn_cancel sobrescrito por intento):
        el ESC mataba al turno nuevo y el viejo seguía escribiendo (E2E
        post-ESC con rewrite + flip de tasks.json)."""
        with self._turn_lock:
            live = list(self._turn_tasks)
        for _, loop, task in live:
            with contextlib.suppress(Exception):
                loop.call_soon_threadsafe(task.cancel)

    def request_cancel(self) -> None:
        """Cancela el turno en curso (lo llama la TUI full-screen con ESC).

        El callback usa call_soon_threadsafe, así que es seguro invocarlo
        desde el hilo de la UI mientras run_turn corre en otro thread.
        """
        # Si hay una confirmación de write pendiente, desbloquearla como
        # RECHAZO: ESC significa "no aprobás nada", no dejar el thread del
        # turno colgado esperando la respuesta.
        self.resolve_confirm(False)
        self._cancel_turn()

    def _pending_key(self, name: str, kwargs: dict) -> str:
        """Key estable para dedupear pendientes (name + path + hash del cambio)."""
        import hashlib
        import json

        try:
            payload = json.dumps(kwargs, sort_keys=True, default=str)
        except TypeError:
            payload = str(kwargs)
        digest = hashlib.sha1(payload.encode("utf-8", errors="replace")).hexdigest()[:12]
        return f"{name}::{kwargs.get('path', '?')}::{digest}"

    def _pending_already_applied(self, name: str, kwargs: dict) -> str | None:
        """Chequeo de idempotencia ANTES de reejecutar un pendiente.

        El retry de pendientes bypasea el wrapper (llama a filesystem directo),
        así que el guard idempotente de tool_dedupe no aplica. Sin esto, un
        'continua' tras un timeout re-ejecuta el MISMO write aunque el archivo
        ya lo tenga (o el usuario lo haya aplicado a mano).
        Retorna mensaje de descarte o None si hay que ejecutar."""
        from pathlib import Path as _Path

        path = kwargs.get("path", "")
        if not path:
            return None
        try:
            content = _Path(path).read_text(encoding="utf-8")
        except OSError:
            return None
        if name == "edit_file":
            old = (kwargs.get("old_str", "") or "").strip()
            new = (kwargs.get("new_str", "") or "").strip()
            if new and new in content and (not old or old not in content):
                return (
                    f"El cambio {name} sobre '{path}' YA ESTÁ aplicado "
                    "(new_str presente, old_str ausente). No se reejecuta."
                )
        elif name == "write_file":
            new_content = kwargs.get("content", "")
            if isinstance(new_content, str) and new_content and new_content == content:
                return (
                    f"El archivo '{path}' YA TIENE ese contenido. "
                    "No se reescribe."
                )
        return None

    def _try_execute_pending_write(self, user_input: str) -> bool:
        """Si hay un write pendiente por timeout, reejecutarlo sin LLM ni re-exploración.

        Retorna True si se manejó (turno consumido), False si no había pendiente
        o no era una continuación -> el caller sigue con el flujo normal."""
        if self._pending_write is None:
            return False
        # 'no' explícito descarta el pendiente
        norm = normalize(user_input)
        if norm in ("no", "n", "nope", "cancelar", "cancela", "rechazo"):
            console.print("[dim]Pendiente descartado.[/dim]")
            self._pending_write = None
            self._pending_timeouts.clear()
            return False
        name, kwargs = self._pending_write
        path = kwargs.get("path", "?")
        already = self._pending_already_applied(name, kwargs)
        if already is not None:
            console.print(f"[dim]{already}[/dim]")
            self._pending_write = None
            self._pending_timeouts.clear()
            return True
        console.print(
            f"\n[dim]↻ Edit pendiente detectado — reintentando {name} sobre '{path}' "
            f"sin re-explorar (contexto del turno anterior conservado)…[/dim]"
        )
        # Ejecutar SIN pasar por confirmación (el usuario ya dijo 'continua/si')
        # Usamos las tools de filesystem directamente para evitar re-confirm loop.
        self._pending_write = None
        self._pending_timeouts.clear()
        try:
            if name == "edit_file":
                from tools.filesystem import edit_file as _ef
                # _ef es un StructuredTool; invocar via .invoke
                result = _ef.invoke(kwargs)  # type: ignore
            elif name == "write_file":
                from tools.filesystem import write_file as _wf
                result = _wf.invoke(kwargs)  # type: ignore
            elif name == "apply_patch":
                from tools.filesystem import apply_patch as _ap
                result = _ap.invoke(kwargs)  # type: ignore
            elif name == "delete_file":
                from tools.filesystem import delete_file as _df
                result = _df.invoke(kwargs)  # type: ignore
            else:
                result = f"⛔ Tipo pendiente desconocido: {name}"
        except Exception as e:
            result = f"⛔ Error reejecutando pendiente: {e}"
        console.print(f"[dim]{result}[/dim]")
        # Persistir en historial como turno EXECUTE (sin LLM)
        human_msg = f"[pending retry] {name} {path}"
        from langchain_core.messages import AIMessage, HumanMessage
        self._messages.append(HumanMessage(human_msg))
        self._messages.append(AIMessage(str(result)))
        # Si fue éxito, mostrar cierre determinístico y ofrecer commit
        if isinstance(result, str) and result.strip().startswith("✅"):
            console.print(f"\n[dim]{self._deterministic_close()}[/dim]")
            self._maybe_ask_commit(human_msg)
        # Guardar en SQLite para historial
        try:
            from cache import save_turn
            from llm_wrapper import get_usage
            usage = get_usage()
            turn_tokens = usage["turn"]["prompt"] + usage["turn"]["completion"]
            save_turn(
                session_id=self.session_id,
                repo_path=self.repo_path,
                role="execute",
                user_message=user_input,
                assistant_message=str(result),
                tokens_used=turn_tokens,
            )
        except Exception:
            pass
        self._flush_evidence()
        return True

    def _confirm_write_cb(
        self, name: str, kwargs: dict,
        *, cancel_on_reject: bool = True, keep_pending: bool = True,
    ) -> bool:
        """Callback del wrapper de tools: pedir aprobación antes de ejecutar.

        Se llama desde el wrapper (orchestration/tool_dedupe) para
        write_file/edit_file/delete_file, ANTES de ejecutar. Corre en un
        thread aparte (asyncio.to_thread), así que esperar el Event no
        congela el event loop del grafo.

        Fail-open sin TUI interactiva (tests/scripts): no hay usuario que
        responder, mismo criterio que EXECUTE_ASK_COMMIT.

        ``cancel_on_reject=False`` + ``keep_pending=False``: para aprobaciones
        puntuales que NO son writes del turno (codemod PATH FIX): rechazar o
        vencer solo omite ese cambio, sin cancelar el turno ni dejar pendiente
        re-ejecutable.
        """
        if not EXECUTE_CONFIRM_WRITES or self._auto_approve or not self._fullscreen:
            return True
        # Serializar confirmaciones: un solo slot. Si ya hay una pendiente, el
        # segundo write espera su turno en vez de pisar el Event (carrera que
        # dejaba al primero en wait(180s) huérfano y perdía su write).
        with self._confirm_lock:
            if self._confirm_event is not None:
                return False
            path = kwargs.get("path", "?")
            extra = f"\n[dim]{kwargs['desc']}[/dim]\n" if kwargs.get("desc") else ""
            continua_hint = (
                "Podés decir 'continua' o 'sí' para reintentarlo sin perder contexto."
                if keep_pending else "Si vence, se omite este cambio."
            )
            console.print(
                f"\n[bold yellow]❓ ¿Aprobás {name} sobre '{path}'?[/bold yellow]\n"
                f"{extra}"
                "[dim]Escribí 'sí'/'no' (o 's'/'n') en el input y Enter. "
                f"Sin respuesta en {int(self._confirm_timeout)}s se venció. "
                f"{continua_hint}[/dim]"
            )
            self._confirm_answer = None
            self._confirm_event = threading.Event()
            # Guardar pendiente ANTES de esperar: si vence, queda para 'continua'
            self._pending_write = (name, dict(kwargs)) if keep_pending else None
            event = self._confirm_event
        try:
            answered = event.wait(self._confirm_timeout)
        finally:
            with self._confirm_lock:
                self._confirm_event = None
        if not answered or self._confirm_answer is None:
            key = self._pending_key(name, kwargs)
            n = self._pending_timeouts.get(key, 0) + 1
            self._pending_timeouts[key] = n
            if n >= 2:
                # Mismo pendiente vencido 2 veces seguidas: no es falta de
                # tiempo, es falta de decisión. Descartarlo corta el loop
                # 'timeout → continua → timeout' (E2E Medicos: 6x mismo write).
                self._pending_write = None
                console.print(
                    "[yellow]⏱️  Confirmación vencida 2 veces para el mismo cambio — "
                    "lo descarto para no loopear. Si lo necesitás, pedilo de nuevo "
                    "con otra estrategia o aprobá dentro del timeout.[/yellow]"
                )
                return False
            if keep_pending:
                console.print(
                    "[yellow]⏱️  Confirmación vencida — podés reintentarlo con 'continua' o 'sí' "
                    "(sin re-explorar, sin perder lo ya leído).[/yellow]"
                )
            else:
                console.print("[yellow]⏱️  Confirmación vencida — se omite este cambio.[/yellow]")
            # NO cancelar el turno: dejar que el wrapper devuelva 'rechazado'
            # y el turno cierre normal (interrupted=False) para que el historial
            # y el read_cache se preserven para el próximo 'continua'.
            return False
        if self._confirm_answer:
            console.print("[green]✅ Aprobado.[/green]")
            self._pending_write = None
            self._pending_timeouts.pop(self._pending_key(name, kwargs), None)
            return True
        if cancel_on_reject:
            console.print("[red]⛔ Rechazado — se cancela el turno.[/red]")
        else:
            console.print("[red]⛔ Rechazado — se omite este cambio.[/red]")
        self._pending_write = None
        self._pending_timeouts.pop(self._pending_key(name, kwargs), None)
        if cancel_on_reject:
            self._cancel_turn()
        return False

    def confirm_pending(self) -> bool:
        """True si hay una confirmación de write esperando respuesta."""
        return self._confirm_event is not None and not self._confirm_event.is_set()

    def resolve_confirm(self, answer: bool) -> None:
        """Resuelve la confirmación pendiente (lo llama la TUI desde su input).

        Un RECHAZO cancela el turno (semántica igual a ESC): no seguimos
        trabajando tras un "no" del usuario. La aprobación solo desbloquea.
        """
        with self._confirm_lock:
            if self._confirm_event is not None:
                self._confirm_answer = bool(answer)
                self._confirm_event.set()
        if not answer:
            self._cancel_turn()

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
            "confirm_pending": self.confirm_pending(),
            "auto_approve": self._auto_approve,
        }

    def close(self):
        pass
