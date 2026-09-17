"""Tests del indicador 'trabajando…' del TUI fullscreen.

Regresión: /autoapprove (slash instantáneo) apagaba el indicador aunque un
turno seguía en curso — _busy era booleano y el finally del slash lo ponía
en False. Ahora es contador thread-safe: el indicador se apaga solo cuando
terminan TODOS los submits.
"""

import threading

from display.fullscreen_tui import FullscreenTUI


def _app():
    return FullscreenTUI(status_provider=lambda: "", on_submit=lambda text: None)


def test_idle_no_trabajando():
    assert _app()._busy is False


def test_slash_instantaneo_no_apaga_turno_en_curso():
    app = _app()
    app._mark_busy()  # turno en curso
    app._mark_busy()  # submit del slash
    assert app._busy is True
    app._mark_idle()  # finally del slash instantáneo
    assert app._busy is True  # el turno sigue → indicador prendido
    app._mark_idle()  # finally del turno
    assert app._busy is False


def test_idle_nunca_negativo():
    app = _app()
    app._mark_idle()
    assert app._busy is False


def test_concurrente_thread_safe():
    app = _app()

    def _ciclo():
        app._mark_busy()
        app._mark_idle()

    threads = [threading.Thread(target=_ciclo) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert app._busy is False
