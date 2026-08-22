"""Router: negaciones de acción no deben activar EXECUTE.

E2E real: 'decime qué archivo habría que cambiar SIN modificar nada' matcheaba
'modificar' → EXECUTE exigía escritura y castigó un turno ANALYZE con 3
reintentos de escritura (45 min).
"""

import pytest

from core.intents import Intent
from orchestration.router import classify_intent


@pytest.mark.parametrize("prompt", [
    "La búsqueda devuelve 500. Analizá la causa raíz SIN modificar nada y "
    "decime qué archivo habría que cambiar y por qué.",
    "Diagnosticá por qué falla el login, sin implementar nada todavía.",
    "Explicame el bug y decime qué tocarías, pero no toques código.",
])
def test_negated_actions_no_activan_execute(prompt):
    assert classify_intent(None, prompt) == Intent.ANALYZE


def test_accion_real_sigue_ganando():
    # verbo de acción SIN negar → EXECUTE aunque haya análisis en el medio
    assert classify_intent(
        None, "Analizá el bug de login y arreglá lo que encuentres"
    ) == Intent.EXECUTE


def test_plan_con_negacion():
    assert classify_intent(
        None, "Hacé un plan para el soft-delete. No implementes nada."
    ) == Intent.PLAN


def test_crea_el_archivo_es_execute():
    """E2E real: 'Creá el archivo backend/scripts/healthcheck.sh' fue a ANALYZE
    porque la lista solo tenía 'creá UN archivo' (con artículo)."""
    assert classify_intent(
        None, "Creá el archivo backend/scripts/healthcheck.sh que verifique el /api/health"
    ) == Intent.EXECUTE
    assert classify_intent(
        None, "Generá un script de migración para la tabla consultas"
    ) == Intent.EXECUTE
