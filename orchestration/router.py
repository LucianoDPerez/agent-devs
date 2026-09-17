from __future__ import annotations

import re

from core.intents import Intent

_EXECUTE_VERBS = [
    "implementá", "implementa", "implement", "implementar",
    # Typos frecuentes del verbo (E2E real: "implemenar T002 ahora" cayó en
    # ANALYZE y el turno no escribió). Tokens exactos: "implementada/o"
    # (participio) sigue sin matchear.
    "implemenar", "implmentar", "inplementar", "implemetar",
    "escribí", "escribi", "escribe código", "escribí código",
    "codeá", "codea",
    "creá un archivo", "crea un archivo",
    "creá", "crea", "crear",
    "generá", "genera", "generar",
    "sumá", "suma",
    "creá un commit", "crea un commit",
    "modificá", "modifica", "modificar",
    "editá", "edita", "editar",
    "commit", "commitea", "commiteá", "commitear", "commiteo",
    "pusheá", "pushea", "push",
    "continuá", "continua", "continuar", "seguí", "sigue",
    "creá un pr", "crea un pr", "abrí un pr",
    "write file", "create file", "edit file",
    "agregá un endpoint", "agrega un endpoint",
    "agregá", "agrega", "agregar", "añadí", "añade", "añadir",
    "eliminá", "elimina", "eliminar", "borrá", "borra", "borrar",
    "quitá", "quita", "quitar", "remové", "remueve", "remover",
    "actualizá", "actualiza", "actualizar",
    "renombrá", "renombra", "renombrar",
    "mové", "mueve", "mover",
    "reemplazá", "reemplaza", "reemplazar",
    "cambiá", "cambia", "cambiar",
    "aplicá", "aplica", "aplicar",
    "corregí", "corrige", "corregir",
    "solucioná", "soluciona", "solucionar",
    "resolvé", "resuelve", "resolver",
    "arreglá", "arregla", "arreglar",
    "fix", "fixeá", "fixear",
    "repará", "repara", "reparar",
    "asegurá", "asegura", "asegurar",
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
    "planificá", "planifica", "planificar",
    "diseñá", "diseña", "desglosá", "desglosa",
    "proponé", "propone", "propuesta",
    "hacer un plan", "armar un plan", "crea un plan", "creá un plan",
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
    "analizá", "analiza", "analizar", "analizá si", "analiza si", "analizá si estas",
    "analiza si estas", "análisis", "analisis",
    "verificá", "verifica", "verificar", "verificación", "verificacion",
    "confirmá", "confirma", "confirmar", "comprobá", "comprueba", "comprobar",
    "chequeá", "chequea", "chequear",
    "asegurate", "asegúrate", "asegurate que", "asegúrate que",
    "está implementada", "esta implementada", "están implementadas", "estan implementadas",
]

# (sin|no) + verbo de acción → el verbo NO cuenta como intención
# cre\w* era sobre-ancho (matcheaba "cree" de creer); toc\w* igual.
# Se acota a formas de crear/tocar código.
_NEGATED_ACTION_RE = re.compile(
    r"\b(?:sin|no)\s+(?:modific\w*|edit\w*|implement\w*|escrib\w*|crea\w*|crear|"
    r"arregl\w*|correg\w*|aplic\w*|toca\w*|toques|tocar|code\w*|elimin\w*|agreg\w*|"
    r"actualiz\w*|renombr\w*|mov\w*|borr\w*|quit\w*|remov\w*)\b"
)

# Stoplist idiomática: "elimina/quita/saca dudas" no es borrar código.
_IDIOM_NO_EXECUTE_RE = re.compile(
    r"\b(?:elimina|elimina?r|quita|quitar|saca|sacar)\s+(?:dudas|la\s+duda|mis\s+dudas)\b"
)

# Coordinación imperativa: "analizá Y arreglá el bug" / "revisá y corregí".
# El verbo de análisis/plan es leading pero la acción COORDINADA con "y/e"
# es una orden de ejecución → sube a EXECUTE. Sin esto, "analizá y arreglá"
# caería en ANALYZE (análisis) cuando el usuario quiere que ARREGLE.
# "e" solo vale ante i/hi (diseñá e implementá); "y elimina dudas" es idiomático.
# marc\w*/tild\w*: "verifica X y marcalas DONE" → EXECUTE (el marking es la
# acción; E2E real: cayó en ANALYZE y el analyzer no tiene write tools → el
# usuario tuvo que re-pedir con "implementar").
_COORDINATED_EXECUTE_RE = re.compile(
    r"(?:\by\s+(?:implement\w*|escrib\w*|crea\w*|crear|gener\w*|modific\w*|"
    r"edit\w*|elimin\w*|agreg\w*|añad\w*|actualiz\w*|renombr\w*|mov\w*|"
    r"reemplaz\w*|quit\w*|borr\w*|remov\w*|cambi\w*|arregl\w*|correg\w*|"
    r"aplic\w*|fix\w*|repar\w*|solucion\w*|resolv\w*|marc\w*|tild\w*)"
    r"|\be\s+(?:implement\w*|i\w*|hi\w*))"
)

# Pregunta/creación de planificación: "qué archivos hay que eliminar",
# "decime qué habría que agregar", "cómo implementar X", "crear tarea para
# jira" → PLAN (el usuario pide un artefacto de planificación, no que se
# ejecute código). Estos patrones NO son órdenes de ejecución.
_PLANNING_LEADING = [
    "crear una tarea", "nueva tarea", "tarea para jira", "tarea para copiar",
    "copiar y pegar en jira", "copiarlo en jira", "copiar en jira",
    "entregar la tarea", "entregarme la tarea",
    "criterios de aceptación", "criterios de aceptacion",
    "qué archivos", "que archivos", "qué archivo", "que archivo",
    "qué pasos", "que pasos", "qué habría que", "qué hay que",
    "cómo implementar", "como implementar", "qué se necesita",
    "qué se debería", "cómo hacer para", "como hacer para",
    "cuáles archivos", "cuales archivos", "cuál archivo", "cual archivo",
]


def _extract_command_prefix(text: str, max_chars: int = 120) -> str:
    """Extract the user's command from the start of the message.

    Stops at pasted content markers (quotes, markdown blocks, long blocks).
    Busca marcadores en ventana amplia (400) y recién ahí trunca a 120 para
    matching — si no, un "si no" en char ~110 pierde su verbo.
    """
    wide = text[:400]
    # Stop at common paste boundaries
    for marker in ['"✅', '"**', '\n---', '\n\n###', '\n\n---']:
        idx = wide.find(marker)
        if idx >= 0:
            wide = wide[:idx]
    # Línea de guiones largos (———, ───, ---) = separador de contenido pegado
    # (tasks, logs, salidas). E2E real: "verificar si estas tasks ya están
    # implementadas" + pegote de Task 4 (que dice "Implementar...") caía en
    # EXECUTE porque el verbo del texto pegado entraba en los 120 chars.
    # El separador puede venir precedido de un \n o de un ESPACIO (como en
    # el paste real: '...correctamente ————— Task 4:') — cortar en ambos.
    m = re.search(r"[\s\n][─—\-]{3,}", wide)
    if m:
        wide = wide[: m.start() + 1]
    # Tasks PEGADAS sin separador ("Analiza si estas tasks están hechas
    # correctamente \n\nTask 4: Implementar...") — el "Implementar" del
    # pegote entra en los 120 chars y activa EXECUTE. Cortar en "Task N:"
    # / "Tarea N:" / "Resumen:" / "Descripción:" (marcadores de documento).
    m2 = re.search(
        r"\b(tasks?|tareas?)\s*\d+:|\bresumen:|\bdescripción:|\bdescripcion:|"
        r"\bacceptance criteria:",
        wide,
        re.IGNORECASE,
    )
    if m2:
        wide = wide[: m2.start()]
    prefix = wide[:max_chars]
    return prefix.strip().lower()


def classify_intent(_llm, user_message: str) -> Intent:
    text = user_message.strip().lower()
    prefix = _extract_command_prefix(user_message)

    # NEGRACIONES DE ACCIÓN: "SIN modificar nada", "no implementes aún" — el
    # verbo está negado y la intención real es ANÁLISIS/PLAN. E2E real: "decime
    # qué archivo habría que cambiar SIN modificar nada" → 'modificar' lo
    # mandaba a EXECUTE, que exigía escritura (require_write) y castigó con
    # reintentos un turno que por definición no escribe (45 min perdidos).
    # Neutralizamos el verbo negado para todo el matching posterior. Pero el
    # patrón "si no + acción" ("si no implementalas") SE BASA en el "no":
    # evaluarlo sobre el texto limpio lo rompe, así que guardamos el original.
    raw_prefix = prefix
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

    # Idiomático: "elimina dudas" no es borrar código — nunca EXECUTE por esto.
    if _IDIOM_NO_EXECUTE_RE.search(prefix):
        # Quitar el falso verbo para el resto del matching
        prefix = _IDIOM_NO_EXECUTE_RE.sub(" ", prefix)
        text = _IDIOM_NO_EXECUTE_RE.sub(" ", text)

    # LEADING INTENT: user's own command (first ~120 chars) takes priority
    # over keywords found in pasted completion reports/checklists.
    # "revisá + verbo de acción" ("revisá y corregí los errores") → EXECUTE:
    # hay algo que hacer, no solo mirar. Mismo patrón que el combo post-review.
    if _has_any(prefix, _REVIEW_LEADING) and _has_any(prefix, _EXECUTE_VERBS):
        return Intent.EXECUTE

    if _has_any(prefix, _REVIEW_LEADING):
        return Intent.REVIEW

    # Verificación + FALLBACK de acción ("verifica si ya está, SI NO
    # implementala/hacela/arreglala") → EXECUTE: hay trabajo condicional real.
    # Va ANTES del check de verificación pura: "si no implementalas" no
    # matchea _EXECUTE_VERBS (implementalas no es token exacto) pero la
    # intención es ejecutar si falta. Ventana 40 (antes 20) para "si no ... <verbo>".
    if _has_any(prefix, _VERIFY_LEADING) and re.search(
        r"\bsi no\b[^\n]{0,40}?(implement\w*|hac\w*|arregl\w*|correg\w*|crea\w*|crear|escrib\w*|agreg\w*)",
        raw_prefix,
    ):
        return Intent.EXECUTE

    # Imperativo EXECUTE al inicio le gana al sustantivo "plan": "implementá el
    # plan de migración" es EXECUTE, no PLAN. Solo si el PRIMER token es verbo.
    first_token = prefix.split()[0] if prefix.split() else ""
    if first_token:
        for v in _EXECUTE_VERBS:
            if " " not in v and first_token == v:
                return Intent.EXECUTE

    # Orden EXPLÍCITA de edición sobre un path concreto ("marcá Done en
    # .agent/tasks.json", "tildá el ítem 3 de plans/x.md") → EXECUTE aunque el
    # verbo no esté en la lista general. E2E real: "marcá como Done..." caía en
    # ANALYZE y el modelo "verificaba" en vez de ejecutar la orden directa.
    # Exige path concreto para no robar análisis ("marcá los errores" solo).
    if first_token in ("marca", "marcá", "marcar", "tilda", "tildá", "tildar") and re.search(
        r"(?:\w[\w.\-/]*\.\w{1,5}\b|/[\w./-]+/)", prefix
    ):
        return Intent.EXECUTE

    # Pregunta de planificación: "qué archivos hay que eliminar", "decime qué
    # habría que agregar", "cómo implementar X" → PLAN (el usuario pregunta
    # QUÉ hacer, no lo está haciendo). VA ANTES que VERIFY puro: "verificá qué
    # archivos hay que eliminar" es planificación, no solo análisis.
    if _has_any(prefix, _PLAN_LEADING) or _has_any(prefix, _PLANNING_LEADING):
        return Intent.PLAN

    # Verificación de TAREAS citadas ("verificá si están hechas estas tareas
    # file.md") → REVIEW: hay un checklist que validar ítem por ítem contra el
    # código con evidencia. En ANALYZE genérico el modelo responde desde el
    # caché sin leer. Exige path/archivo concreto para no robar análisis puros
    # ("analizá si el login anda" sigue a ANALYZE).
    if (
        _has_any(prefix, _VERIFY_LEADING)
        and re.search(r"\b(tareas?|checklist|criterios)\b", prefix)
        and re.search(r"(?:\w[\w.\-/]*\.\w{1,5}\b|/[\w./-]+/)", prefix)
    ):
        return Intent.REVIEW

    # Verificación/análisis puro → ANALYZE (prioridad del PRIMER verbo).
    # "analizá cómo eliminar un endpoint" → ANALYZE aunque "eliminar" sea un
    # verbo de acción: está subordinado al análisis, no es una orden.
    # Excepción: COORDINACIÓN imperativa — "analizá y arreglá el bug" → EXECUTE
    # (el usuario quiere que ARREGLE, no solo que analice). Incluye "verifica
    # X y marcalas DONE" (E2E real: cayó en ANALYZE, el analyzer no escribe y
    # el usuario tuvo que re-pedir con "implementar").
    if _has_any(prefix, _VERIFY_LEADING) and _COORDINATED_EXECUTE_RE.search(prefix):
        return Intent.EXECUTE
    if _has_any(prefix, _VERIFY_LEADING) and not _COORDINATED_EXECUTE_RE.search(prefix):
        return Intent.ANALYZE

    # EXECUTE gana si hay verbo de acción en el comando del usuario. DEBE ir
    # ANTES del check de CHAT: un texto a escribir puede contener saludos
    # ("agregá al README: 'Hola Soy Agent-Devs'") y 'hola'/'chau' están en
    # _CHAT_LEADING — si CHAT evaluara primero, la orden de implementar caía
    # en chat y el turno nunca llegaba a EXECUTE.
    if _has_any(prefix, _EXECUTE_VERBS):
        return Intent.EXECUTE

    if _has_any(prefix, _CHAT_LEADING):
        return Intent.CHAT

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
