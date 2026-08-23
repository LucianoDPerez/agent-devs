from __future__ import annotations

import re

from core.intents import Intent

_EXECUTE_VERBS = [
    "implementá", "implementa", "implement", "implementar",
    "escribí", "escribi", "escribe código", "escribí código",
    "codeá", "codea",
    "creá un archivo", "crea un archivo",
    "creá", "crea", "crear",
    "generá", "genera", "generar",
    "sumá", "suma",
    "creá un commit", "crea un commit",
    "modificá", "modifica", "modificar",
    "editá", "edita", "editar",
    "commit", "commitea", "pusheá", "push",
    "creá un pr", "crea un pr", "abrí un pr",
    "write file", "create file", "edit file",
    "agregá un endpoint", "agrega un endpoint",
    "aplicá", "aplica", "aplicar",
    "corregí", "corrige", "corregir",
    "solucioná", "soluciona", "solucionar",
    "resolvé", "resuelve", "resolver",
    "arreglá", "arregla", "arreglar",
    "fix", "fixeá", "fixear",
    "repará", "repara", "reparar",
]

# Leading command patterns — detected from the first ~100 chars
# (before any pasted content) to avoid false matches in pastes.
_REVIEW_LEADING = [
    "revisá", "revisa", "revisión", "review", "code review",
    "hacer review", "hacer code review", "hacer code-review",
    "hacer revisión", "hacer revisión de", "hacer review de",
    "buscá bugs", "busca bugs", "auditá", "audita",
]

_PLAN_LEADING = [
    "plan", "planificá", "planifica", "planificar",
    "diseñá", "diseña", "desglosá", "desglosa",
    "proponé", "propone", "propuesta",
]

_CHAT_LEADING = [
    "hola", "buenas", "buen día", "buen dia",
    "gracias", "muchas gracias",
    "cómo estás", "como estas", "cómo andas",
    "chau", "adiós", "adios", "nos vemos",
]

# Verificación PURA: "verificá/confirmá que X está implementada" — no es
# EXECUTE (no hay que escribir) ni REVIEW (no es un PR). E2E real: la tarea
# de verificación cayó en EXECUTE por "implementada" (participio) y el
# no-write retry castigó un turno que respondió correctamente.
_VERIFY_LEADING = [
    "verificá", "verifica", "verificar", "verificación", "verificacion",
    "confirmá", "confirma", "confirmar", "comprobá", "comprueba", "comprobar",
    "chequeá", "chequea", "chequear",
    "asegurate", "asegúrate", "asegurate que", "asegúrate que",
    "está implementada", "esta implementada", "están implementadas", "estan implementadas",
]

# (sin|no) + verbo de acción → el verbo NO cuenta como intención
_NEGATED_ACTION_RE = re.compile(
    r"\b(?:sin|no)\s+(?:modific\w*|edit\w*|implement\w*|escrib\w*|cre\w*|"
    r"arregl\w*|correg\w*|aplic\w*|toc\w*|code\w*)\b"
)


def _extract_command_prefix(text: str, max_chars: int = 120) -> str:
    """Extract the user's command from the start of the message.

    Stops at pasted content markers (quotes, markdown blocks, long blocks).
    """
    prefix = text[:max_chars]
    # Stop at common paste boundaries
    for marker in ['"✅', '"**', '\n---', '\n\n###', '\n\n---']:
        idx = prefix.find(marker)
        if idx >= 0:
            prefix = prefix[:idx]
    return prefix.strip().lower()


def classify_intent(_llm, user_message: str) -> Intent:
    text = user_message.strip().lower()
    prefix = _extract_command_prefix(user_message)

    # NEGRACIONES DE ACCIÓN: "SIN modificar nada", "no implementes aún" — el
    # verbo está negado y la intención real es ANÁLISIS/PLAN. E2E real: "decime
    # qué archivo habría que cambiar SIN modificar nada" → 'modificar' lo
    # mandaba a EXECUTE, que exigía escritura (require_write) y castigó con
    # reintentos un turno que por definición no escribe (45 min perdidos).
    # Neutralizamos el verbo negado para todo el matching posterior.
    text = _NEGATED_ACTION_RE.sub(" ", text)
    prefix = _NEGATED_ACTION_RE.sub(" ", prefix)

    # CORRECCIÓN POST-REVIEW: verbo de acción + mención de review/correcciones
    # → EXECUTE (aplicar los hallazgos del review). Debe ir ANTES del check de
    # REVIEW: "implementar las observaciones del review" contiene "review" pero
    # es una acción de implementación, no una petición de revisar.
    if _has_any(prefix, _EXECUTE_VERBS) and _has_any(prefix, (
        "review", "hallazgo", "observaciones", "sugerencias", "correcciones",
        "cambios", "code review", "critical", "crítico", "crític",
    )):
        return Intent.EXECUTE

    # LEADING INTENT: user's own command (first ~120 chars) takes priority
    # over keywords found in pasted completion reports/checklists.
    # "revisá + verbo de acción" ("revisá y corregí los errores") → EXECUTE:
    # hay algo que hacer, no solo mirar. Mismo patrón que el combo post-review.
    if _has_any(prefix, _REVIEW_LEADING) and _has_any(prefix, _EXECUTE_VERBS):
        return Intent.EXECUTE

    if _has_any(prefix, _REVIEW_LEADING):
        return Intent.REVIEW

    # Verificación pura → ANALYZE, salvo que haya verbos de implementación
    # ("verificá y arreglá" sigue siendo EXECUTE). "implementada" (participio
    # pasado, ya hecha) NO activa EXECUTE.
    if _has_any(prefix, _VERIFY_LEADING) and not _has_any(prefix, _EXECUTE_VERBS):
        return Intent.ANALYZE

    if _has_any(prefix, _PLAN_LEADING):
        return Intent.PLAN

    if _has_any(prefix, _CHAT_LEADING):
        return Intent.CHAT

    # EXECUTE gana si hay verbo de acción en el comando del usuario
    if _has_any(prefix, _EXECUTE_VERBS):
        return Intent.EXECUTE

    # Fallback: full text search (for messages without clear command prefix)
    if _has_any(text, _EXECUTE_VERBS):
        return Intent.EXECUTE

    if _has_any(text, [
        "revisá", "revisa", "revisión", "revis", "review",
        "pr #", "buscá bugs", "busca bugs", "audit",
        "inspeccion", "crític", "critic",
    ]):
        return Intent.REVIEW

    if _has_any(text, [
        "plan", "planific", "planifi",
        "diseñá", "diseña", "diseño", "diseñ",
        "desglosá", "desglosa", "desglose",
        "proponé", "propone", "propuesta", "proposal",
        "approach", "enfoque", "pasos a seguir",
        "cómo implementar", "como implementar",
        "qué archivos", "que archivos",
    ]):
        return Intent.PLAN

    if _has_any(text, [
        "hola", "buenas", "buen día", "buen dia",
        "gracias", "muchas gracias",
        "cómo estás", "como estas", "cómo andas",
        "chau", "adiós", "adios", "nos vemos",
        "genial", "perfecto", "de acuerdo",
        "sos", "sabés", "sabes",
        "opinión", "opinion",
    ]):
        return Intent.CHAT

    return Intent.ANALYZE


def _has_any(text: str, patterns: list[str]) -> bool:
    """Match patterns as whole tokens (not substrings inside paths/filenames).

    "plan" matches "hacé un plan" but NOT "lucho-plans/tasks.md".
    Stems like "planific" still match "planificación" via prefix + word chars.
    """
    for p in patterns:
        if " " in p:
            if p in text:
                return True
            continue
        # Stem (ends mid-word intentionally) → prefix at token start
        if p in {
            "planific", "planifi", "diseñ", "revis", "crític", "critic",
            # "implement" está EXCLUIDO a propósito: su stem matchea
            # "implementada/implementado" (participio = YA está hecha, no es
            # una orden de implementar) y mandaba verificaciones a EXECUTE.
            # El imperativo "implementá/implementa/implementar" matchea como
            # token exacto más abajo.
            "aplic", "correg",
        }:
            if re.search(rf"(?<!\w){re.escape(p)}\w*", text):
                return True
            continue
        # Whole token — avoids matching inside "lucho-plans", "pushkin", etc.
        if re.search(rf"(?<!\w){re.escape(p)}(?!\w)", text):
            return True
    return False
