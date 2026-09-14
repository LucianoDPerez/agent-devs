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
    # Negación => nunca EXECUTE (evita retries de escritura de 45 min).
    # ANALYZE o PLAN valen: ambos son solo-lectura. "decime qué archivo habría
    # que cambiar" clasifica PLAN desde que PLANNING va antes que VERIFY puro.
    assert classify_intent(None, prompt) != Intent.EXECUTE


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


def test_orden_explicita_de_edicion_con_path_ruta_execute():
    """E2E real: 'Marcá como Done la tarea 1 en .agent/tasks.json' caía en
    ANALYZE y el modelo verificaba en vez de ejecutar la orden directa."""
    from core.intents import Intent
    from orchestration.router import classify_intent

    assert classify_intent(
        None, "Marcá como Done la tarea 1 en .agent/tasks.json y nada más."
    ) == Intent.EXECUTE
    assert classify_intent(
        None, "Tildá el ítem 3 de plans/tarea.md"
    ) == Intent.EXECUTE


def test_marcar_sin_path_no_ruta_execute():
    """Sin path concreto no se roba nada ('marcá los errores' solo)."""
    from core.intents import Intent
    from orchestration.router import classify_intent

    assert classify_intent(None, "marcá los errores que veas") != Intent.EXECUTE
