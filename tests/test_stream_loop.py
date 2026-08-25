"""Tests de la detección de loop de texto en el streaming.

El 4B a veces no emite EOS: termina su resumen final y sigue generando texto
nuevo indefinidamente (turnos de 10+ min colgados). TURN_IDLE_TIMEOUT no lo
atrapa (el modelo sigue emitiendo chunks — nunca es idle). Dos protecciones:
1. Detección por sufijo repetido (ventanas múltiples).
2. EXECUTE_MAX_CONTENT_SECONDS: corte por duración de generación continua.

Acá se valida la LÓGICA de detección (la integración en stream_agent_turn se
prueba E2E con el LLM real en tests/harness_*).
"""

import random
import string


def _detect_loop(text: str, windows=(64, 128, 256)) -> bool:
    """Réplica exacta de la lógica de sufijo repetido en stream_agent_turn."""
    if len(text) < 512:
        return False
    for w in windows:
        window = text[-w:]
        if text[:-w].count(window) >= 2:
            return True
    return False


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