"""Tests de normalización de texto del usuario (core.textutil).

La entrada del usuario llega con acentos/mayúsculas arbitrarias ("sí"/"SÍ",
"Revisá"/"revisa"). normalize() deja UNA forma canónica para comparar.
"""

from core.textutil import normalize


def test_normalize_quita_acentos_y_minusculas():
    assert normalize("SÍ") == "si"
    assert normalize("sí") == "si"
    assert normalize(" Sí ") == "si"
    assert normalize("Revisá la vista") == "revisa la vista"
    assert normalize("implementá") == "implementa"
    assert normalize("modificá") == "modifica"


def test_normalize_colapsa_espacios():
    assert normalize("  dale  ") == "dale"
    assert normalize("  ") == ""


def test_normalize_conserva_palabras_sin_acento():
    assert normalize("s") == "s"
    assert normalize("y") == "y"
    assert normalize("yes") == "yes"
    assert normalize("DALE") == "dale"


def test_normalize_respuestas_de_confirmacion():
    """Las respuestas de aprobación (main.py) matchean la forma canónica."""
    aprobaciones = {"s", "si", "y", "yes", "aprob", "ok", "dale"}
    for entrada in ("s", "S", "si", "SI", "sí", "SÍ", "y", "yes", "dale", "OK"):
        assert normalize(entrada) in aprobaciones
    for entrada in ("no", "n", "cancelar", "dejalo", ""):
        assert normalize(entrada) not in aprobaciones
