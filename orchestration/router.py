from __future__ import annotations

import re

from core.intents import Intent

_EXECUTE_VERBS = [
    "implementá", "implementa", "implement", "implementar",
    "escribí", "escribi", "escribe código", "escribí código",
    "codeá", "codea",
    "creá un archivo", "crea un archivo",
    "creá un commit", "crea un commit",
    "modificá", "modifica", "modificar",
    "editá", "edita", "editar",
    "commit", "commitea", "pusheá", "push",
    "creá un pr", "crea un pr", "abrí un pr",
    "write file", "create file", "edit file",
    "agregá un endpoint", "agrega un endpoint",
    "aplicá", "aplica", "aplicar",
    "corregí", "corrige", "corregir",
]


def classify_intent(_llm, user_message: str) -> Intent:
    text = user_message.strip().lower()

    # EXECUTE gana si hay verbo de acción — aunque el paste diga "revisión/críticos"
    # (ej. "implementar estas correcciones detectadas ### PROBLEMAS CRÍTICOS…")
    if _has_any(text, _EXECUTE_VERBS):
        return Intent.EXECUTE

    # Review patterns (most specific)
    if _has_any(text, [
        "revisá", "revisa", "revisión", "revis", "review",
        "pr #", "buscá bugs", "busca bugs", "audit",
        "inspeccion", "crític", "critic",
    ]):
        return Intent.REVIEW

    # Plan patterns — whole tokens so paths like "lucho-plans/tasks.md" don't trigger PLAN
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

    # Chat patterns
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
            "implement", "aplic", "correg",
        }:
            if re.search(rf"(?<!\w){re.escape(p)}\w*", text):
                return True
            continue
        # Whole token — avoids matching inside "lucho-plans", "pushkin", etc.
        if re.search(rf"(?<!\w){re.escape(p)}(?!\w)", text):
            return True
    return False
