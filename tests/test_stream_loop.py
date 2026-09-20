"""Tests de la detección de loop de texto en el streaming.

El 4B a veces no emite EOS: termina su resumen final y sigue generando texto
nuevo indefinidamente (turnos de 10+ min colgados). TURN_IDLE_TIMEOUT no lo
atrapa (el modelo sigue emitiendo chunks — nunca es idle). Dos protecciones:
1. Detección por sufijo repetido (ventanas múltiples).
2. EXECUTE_MAX_CONTENT_SECONDS: corte por duración de generación continua.

Acá se valida la LÓGICA de detección (la integración en stream_agent_turn se
prueba E2E con el LLM real en tests/harness_*).
"""

import asyncio
import random
import string

import pytest
from langchain_core.messages import AIMessageChunk

from display.console import (
    ReasoningOnlyResponse,
    stream_agent_turn,
)
from display.console import (
    _text_loop_detected as _detect_loop,
)


class _FakeAgent:
    """Agente stub cuyo astream emite los chunks dados y termina."""

    def __init__(self, chunks):
        self._chunks = chunks

    def astream(self, *args, **kwargs):
        chunks = self._chunks

        async def gen():
            for c in chunks:
                yield (c, {})

        return gen()


def _tool_chunk(name="read_file"):
    return AIMessageChunk(
        content="",
        tool_call_chunks=[{"name": name, "args": '{"path": "x"}', "id": "c1", "index": 0}],
    )


def _text_chunk(text):
    return AIMessageChunk(content=text)


def _reason_chunk(text):
    return AIMessageChunk(
        content="",
        additional_kwargs={"is_reasoning": True, "reasoning_content": text},
    )


def _run(agent, **kwargs):
    return asyncio.run(
        stream_agent_turn(agent, [], {"configurable": {"thread_id": "t"}}, **kwargs)
    )


# NOTE: _detect_loop ES la función de producción (display.console).
# Antes era una réplica local que describía un comportamiento más robusto
# que la implementación real — los tests pasaban y el loop T004 igual
# llegaba a ×12. Ahora ejercen el código real.


def test_detecta_repeticion_real():
    """El 4B repite el mismo párrafo: debe detectarse."""
    bloque = (
        "Would you like me to commit this change? "
        "The system will ask about committing after this task."
    )
    assert _detect_loop(bloque * 8)


def test_detecta_parrafo_repetido_largo():
    parrafo = (
        "The verification tools cannot run because there's no project "
        "configuration file. This is expected for a simple text file "
        "repository. The task was just to create the file with content, "
        "which has been successfully completed. "
    )
    assert _detect_loop(parrafo * 6)


def test_no_detecta_texto_normal():
    """Texto legítimo largo con contenido variado: jamás loop."""
    random.seed(42)
    normal = "".join(random.choice(string.ascii_letters + " ") for _ in range(8000))
    assert not _detect_loop(normal)


def test_no_detecta_markdown_con_listas():
    """Listas markdown con items distintos: no es repetición."""
    md = "\n".join(f"- Item {i} con contenido único {i*7}" for i in range(300))
    assert not _detect_loop(md)


def test_texto_corto_no_se_evalua():
    """Menos de 512 chars: sin loop (la respuesta corta es legítima)."""
    assert not _detect_loop("hola " * 20)


_T004_BLOQUE = (
    "T004 está **LISTO**.\n\n"
    "- El test ya estaba implementado y cubre el payload cerrado, sin PII "
    "y la idempotencia: `tests/unit/pauta-desactivada-command.test.ts:6`.\n"
    "- Actualicé únicamente el campo de estado a `\"done\"` en "
    "`.agent/tasks/ulab-1671-desactivacion-banner-sqs/tasks.json`.\n"
    "<CPA_DONE>\n"
)


def test_detecta_bloque_listo_cpa_done_no_alineado():
    """Regresión T004: bloque LISTO + <CPA_DONE> ×4 con largo NO múltiplo
    de 256. La detección vieja (3 bloques consecutivos de 256 chars
    idénticos) no lo atrapaba por desalineación → 12 repeticiones hasta
    el corte de 90s de generación continua."""
    assert len(_T004_BLOQUE) % 256 != 0
    assert _detect_loop(_T004_BLOQUE * 4)
    assert not _detect_loop(_T004_BLOQUE)


def test_stream_corta_loop_listo_sin_colgarse():
    """Integración: el stream corta el loop de cierre en vez de emitirlo ×12."""
    out = _run(_FakeAgent([_text_chunk(_T004_BLOQUE) for _ in range(12)]))
    assert "<CPA_DONE>" in out
    assert len(out) < len(_T004_BLOQUE * 12)


# ── require_text (respuesta vacía tras tools) ────────────────────────────────


def test_empty_after_tools_raises_with_require_text():
    """Tools sin texto final + require_text → retry vía ReasoningOnlyResponse.

    E2E Medicos: el analyzer leyó el schema y el turno se guardó ''.
    """
    with pytest.raises(ReasoningOnlyResponse) as exc:
        _run(_FakeAgent([_tool_chunk()]), idle_timeout=5, require_text=True)
    assert exc.value.reason == "empty-after-tools"


def test_empty_after_tools_returns_empty_without_require_text():
    """Sin require_text (EXECUTE) se preserva el comportamiento anterior."""
    assert _run(_FakeAgent([_tool_chunk()]), idle_timeout=5) == ""


def test_text_after_tools_returns_text_with_require_text():
    """Con texto final no hay raise aunque require_text esté activo."""
    out = _run(
        _FakeAgent([_tool_chunk(), _text_chunk("campos: id, nombre")]),
        idle_timeout=5,
        require_text=True,
    )
    assert out == "campos: id, nombre"


# ── max_reasoning_chars (presupuesto proactivo de thinking) ──────────────────


def test_reasoning_sobre_presupuesto_corta_y_reintenta():
    """Bloque de reasoning > tope sin output → corte + ReasoningOnlyResponse
    (el retry posterior corre con thinking desactivado)."""
    with pytest.raises(ReasoningOnlyResponse):
        _run(
            _FakeAgent([_reason_chunk("x" * 5000) for _ in range(3)]),
            idle_timeout=5,
            max_reasoning_chars=12000,
        )


def test_reasoning_bajo_presupuesto_no_corta():
    """Reasoning corto + tool call: sin corte, sin raise."""
    out = _run(
        _FakeAgent([_reason_chunk("pienso"), _tool_chunk()]),
        idle_timeout=5,
        max_reasoning_chars=12000,
    )
    assert out == ""


def test_reasoning_presupuesto_fresco_por_bloque():
    """El presupuesto es por bloque: output intermedio lo resetea."""
    out = _run(
        _FakeAgent([
            _reason_chunk("y" * 10000),
            _tool_chunk(),
            _reason_chunk("z" * 10000),
            _tool_chunk(),
        ]),
        idle_timeout=5,
        max_reasoning_chars=12000,
    )
    assert out == ""
