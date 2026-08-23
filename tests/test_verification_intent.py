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


@pytest.mark.parametrize("prompt", [
    "verifica si estas tasks ya fueron implementadas, si no implementalas",
    "verificar si la Task 5 esta implementada, si no hacela",
    "verificá si el endpoint existe, si no crealo",
])
def test_verificacion_con_fallback_accion_ruta_execute(prompt):
    """'Verifica si X, SI NO implementala' → EXECUTE (trabajo condicional real).
    El 'no implementalas' se neutralizaba por _NEGATED_ACTION_RE y el check
    si-no moría: hay que evaluarlo sobre el prefix ORIGINAL."""
    assert classify_intent(None, prompt) == Intent.EXECUTE


def test_verificacion_con_tasks_pegadas_ruta_analyze():
    """Verificación + contenido pegado (que dice 'Implementar') → ANALYZE.
    El separador de guiones largos debe cortar el prefix antes del texto
    pegado; el verbo 'Implementar' del pegote NO puede activar EXECUTE."""
    prompt = (
        "verificar si estas tasks ya estan implementadas correctamente \n"
        "—————————————————        Task 4: Implementar CLI principal (start/end)\n"
        "Resumen: Implementar el entry point del binario con comandos start y end.\n"
        "- Implementar telemetry/cmd/spec-kitti-telemetry/main.go"
    )
    assert classify_intent(None, prompt) == Intent.ANALYZE


def test_orden_con_implementar_real_sigue_execute():
    assert classify_intent(None, "implementá el endpoint /dashboard") == Intent.EXECUTE
