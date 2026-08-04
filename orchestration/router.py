from __future__ import annotations

import re

from core.intents import Intent


def classify_intent(_llm, user_message: str) -> Intent:
    text = user_message.strip().lower()

    # Review patterns (most specific)
    if _has_any(text, [
        "revisá", "revisa", "revisión", "revis", "review",
        "pr #", "buscá bugs", "busca bugs", "audit",
        "inspeccion", "crític", "critic",
    ]):
        return Intent.REVIEW

    # Plan patterns (must come before execute — "plan" overrides "implementar")
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

    # Execute patterns
    if _has_any(text, [
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
    ]):
        return Intent.EXECUTE

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
    return any(p in text for p in patterns)