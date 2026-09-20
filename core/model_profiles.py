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
