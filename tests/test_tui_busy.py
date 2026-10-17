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


def test_is_approval_text():
    from display.fullscreen_tui import _is_approval_text

    for good in ("s", "S", "sí", "SI", "n", "no", "y", "yes", "  s  "):
        assert _is_approval_text(good) is True, good
    for bad in ("hola", "continua", "dale", "/new", "", "1 3"):
        assert _is_approval_text(bad) is False, bad


def test_pane_drag_selecciona_filas_visibles():
    """Regresión: el drag sobre el pane devuelve el texto VISIBLE.

    Con el Static único el extract salía del acumulado completo (lento);
    con el RichLog inicial los offsets apuntaban a filas fantasmas
    (trailing newline → tira vacía) y el highlight caía en líneas
    invisibles: parecía que no se podía seleccionar.
    """
    import asyncio

    from textual.events import MouseMove

    from display.fullscreen_tui import FullscreenTUI

    async def main():
        app = FullscreenTUI(status_provider=lambda: {}, on_submit=lambda t: None)
        async with app.run_test(size=(100, 30)) as pilot:
            for i in range(120):
                app._write_lines(f"linea {i:04d}\n")
            await pilot.pause(0.5)
            pane = app.query_one("#pane")
            # Sin tiras fantasmas: 120 writes -> 120 filas.
            assert len(pane.lines) == 120
            # Auto-follow al fondo; drag en filas visibles del pane.
            await pilot.mouse_down("#pane", (5, 8))
            await pilot._post_mouse_events([MouseMove], "#pane", (30, 10), button=1)
            await pilot.mouse_up("#pane", (30, 10))
            await pilot.pause(0.5)
            sel = app.screen.get_selected_text()
            assert sel, "el drag debe producir selección"
            assert "linea 0000" not in sel, f"mapeo a filas invisibles: {sel[:80]!r}"
            assert "linea 01" in sel, f"debería traer filas del fondo: {sel[:80]!r}"

    asyncio.run(main())
