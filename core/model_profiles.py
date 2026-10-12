"""Per-model profiles: el harness se adapta al SLM cargado, no al revés.

El server (llama.cpp) manda en sampling/contexto/threads; el harness solo
adapta SU comportamiento: presupuesto de thinking por bloque, temperatura
por request en EXECUTE y tokens. Detección vía /v1/models + /props
(llm_wrapper.detect_*); fallback a defaults si el modelo es desconocido.
"""

from __future__ import annotations

import re

# thinking_chars: el 4B razona 1500-2500 chars antes de actuar; Qwen razona
# largo y rinde con margen. temp_execute: por request (la API lo acepta),
# no pisa el --temp del server como default.
_PROFILES: list[dict] = [
    {
        "family": "spark",
        "patterns": [r"spark"],
        "thinking_chars_exec": 8000,
        "thinking_chars_other": 4000,
        "temp_execute": 0.2,
        "notes": "Ansioso y rápido: presupuesto corto, temp baja.",
    },
    {
        "family": "qwen",
        "patterns": [r"qwen"],
        "thinking_chars_exec": 12000,
        "thinking_chars_other": 6000,
        "temp_execute": 0.2,
        "notes": "Thinking largo útil: margen amplio.",
    },
    {
        "family": "gemma",
        "patterns": [r"gemma"],
        "thinking_chars_exec": 10000,
        "thinking_chars_other": 5000,
        "temp_execute": 0.3,
        "notes": "Intermedio; temp levemente mayor.",
    },
]

DEFAULT_PROFILE: dict = {
    "family": "default",
    "thinking_chars_exec": 12000,
    "thinking_chars_other": 6000,
    "temp_execute": 0.2,
    "notes": "Modelo desconocido: valores conservadores (= config).",
}


def match_profile(model_id: str | None) -> dict:
    """Ficha del modelo (copia) o DEFAULT_PROFILE si no matchea/none."""
    if model_id:
        low = model_id.lower()
        for prof in _PROFILES:
            if any(re.search(p, low) for p in prof["patterns"]):
                return dict(prof)
    return dict(DEFAULT_PROFILE)


# Umbral del tier amplio local: modelos con este N de B (o más) no loopean
# como el 4B y aprovechan exploración ancha (E2E: 35B con 5 queries
# distintas y pertinentes cortado por el techo de 4B). Heurística sobre el
# id (fail-open: si no parsea, techo ajustado actual).
BIG_LOCAL_MIN_B = 14

# Techos ANALYZE/PLAN por contexto detectado (n_ctx del server). El techo
# amplio solo sirve si los resultados entran: a 35k, 10 explores anchos
# (baseline ~6k tokens + architecture de miles) rozan el 400 de llama.cpp.
CTX_WIDE_MIN = 60_000
CTX_MID_MIN = 30_000

# (max_calls, max_reads) por tier.
_BUDGET_TIGHT = (4, 8)
_BUDGET_MID = (7, 10)
_BUDGET_WIDE = (10, 12)


def _parse_size_b(model_id: str | None) -> float | None:
    """N de B del id ('qwen3.6-35b-a3b' → 35, 'agents-a1-4b' → 4) o None.

    Toma el máximo de los matches (el total en MoE '35b-a3b'). 'qwen3.6'
    no matchea (el número no lleva B pegada).
    """
    if not model_id:
        return None
    found = re.findall(r"(\d+(?:\.\d+)?)\s*b\b", model_id.lower())
    if not found:
        return None
    try:
        return max(float(v) for v in found)
    except ValueError:
        return None


def _local_model_id(llm) -> str | None:
    """Id REAL del modelo en llama-server (vía /v1/models), o el alias del
    config como fallback. localhost: ~ms; fail-open (None si no hay server).
    """
    base_url = getattr(llm, "base_url", "") or ""
    if base_url:
        try:
            from llm_wrapper import detect_server_model

            live = detect_server_model(base_url)
            if live:
                return live
        except Exception:
            pass
    return getattr(llm, "model_name", None)


def _tier_for(ctx_limit: int | float | None) -> tuple[int, int]:
    """Techo por contexto: sin dato o chico → ajustado (status quo)."""
    try:
        ctx = float(ctx_limit or 0)
    except (TypeError, ValueError):
        ctx = 0
    if ctx >= CTX_WIDE_MIN:
        return _BUDGET_WIDE
    if ctx >= CTX_MID_MIN:
        return _BUDGET_MID
    return _BUDGET_TIGHT


def analyze_budget_for(llm, ctx_limit: int | float | None = None) -> tuple[int, int]:
    """(max_calls, max_reads) de ANALYZE según proveedor, modelo y contexto.

    Nube = amplio (modelos fuertes, no loopean). Local: el techo lo limita la
    CONFIANZA (solo modelos grandes ≥14B salen del techo de 4B) y el CONTEXTO
    detectado (a 35k, 10 explores anchos rozan el 400: tier intermedio).
    Sin datos → ajustado (status quo, fail-open). Ver CLOUD_* en config.py.
    """
    from config import (
        ANALYZE_EXPLORE_BUDGET,
        ANALYZE_MAX_READS_AFTER_EXPLORE,
        CLOUD_ANALYZE_EXPLORE_BUDGET,
        CLOUD_ANALYZE_MAX_READS_AFTER_EXPLORE,
    )
    from llm_config import is_cloud_url

    if is_cloud_url(getattr(llm, "base_url", "") or ""):
        return (CLOUD_ANALYZE_EXPLORE_BUDGET, CLOUD_ANALYZE_MAX_READS_AFTER_EXPLORE)
    size = _parse_size_b(_local_model_id(llm))
    if size is not None and size >= BIG_LOCAL_MIN_B:
        return _tier_for(ctx_limit)
    return (ANALYZE_EXPLORE_BUDGET, ANALYZE_MAX_READS_AFTER_EXPLORE)


def plan_budget_for(llm, ctx_limit: int | float | None = None) -> tuple[int, int]:
    """(max_calls, max_reads) de PLAN según proveedor, modelo y contexto."""
    from config import (
        CLOUD_PLAN_EXPLORE_BUDGET,
        CLOUD_PLAN_MAX_READS_AFTER_EXPLORE,
        PLAN_EXPLORE_BUDGET,
        PLAN_MAX_READS_AFTER_EXPLORE,
    )
    from llm_config import is_cloud_url

    if is_cloud_url(getattr(llm, "base_url", "") or ""):
        return (CLOUD_PLAN_EXPLORE_BUDGET, CLOUD_PLAN_MAX_READS_AFTER_EXPLORE)
    size = _parse_size_b(_local_model_id(llm))
    if size is not None and size >= BIG_LOCAL_MIN_B:
        return _tier_for(ctx_limit)
    return (PLAN_EXPLORE_BUDGET, PLAN_MAX_READS_AFTER_EXPLORE)
