"""Verificación pura → ANALYZE y sin require_write (no castigar turnos correctos).

E2E real: 'verifica que este implementada correctamente Task 5' cayó en
EXECUTE (el participio 'implementada' matcheaba el stem implement), el modelo
verificó perfecto y el no-write retry lo castigó → inventó un main.go
'truncado' inexistente y casi borra countTokens.
"""
import pytest

from core.intents import Intent
from orchestration.router import classify_intent
from orchestration.session import _is_verification_only


@pytest.mark.parametrize("prompt", [
    "verifica que este implementada correctamente Task 5",
    "verificá que esté bien implementado el endpoint /dashboard",
    "confirmá que la función countTokens existe",
    "comprobá que los tests pasan",
])
def test_verificacion_pura_ruta_a_analyze(prompt):
    assert classify_intent(None, prompt) == Intent.ANALYZE


@pytest.mark.parametrize("prompt", [
    "verificá y arreglá el bug del login",
    "verificá e implementá el fix",
    "revisá el código y corregí los errores",
])
def test_verificacion_con_accion_sigue_execute(prompt):
    assert classify_intent(None, prompt) == Intent.EXECUTE


def test_implementada_pasado_no_activa_execute():
    # "implementada" = ya está hecha → NO es orden de implementar
    assert classify_intent(None, "verifica que este implementada la Task 5") == Intent.ANALYZE


def test_is_verification_only():
    assert _is_verification_only("verifica que este implementada correctamente Task 5") is True
    assert _is_verification_only("confirmá que los tests pasan") is True
    assert _is_verification_only("verificá y arreglá el bug") is False
    assert _is_verification_only("implementá el endpoint") is False
