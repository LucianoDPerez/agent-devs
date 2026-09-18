"""Versión del harness siempre visible (hash por turno + doctor + update).

E2E: crash por mezcla de versiones (session.py nuevo + tool_dedupe viejo
en el mismo proceso) que ningún diagnóstico mostraba: el transcript no
decía qué corría ni el --update pedía reiniciar.
"""

import re
import subprocess
from pathlib import Path

import display.console as console_mod


def _harness_repo() -> Path:
    return Path(console_mod.__file__).resolve().parent.parent


def test_harness_head_coincide_con_git():
    h = console_mod.harness_head()
    assert re.fullmatch(r"[0-9a-f]+[*]?", h), h
    exp = subprocess.run(
        ["git", "-C", str(_harness_repo()), "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert h.rstrip("*") == exp


def test_role_switch_muestra_harness():
    import inspect

    assert "harness_head()" in inspect.getsource(console_mod.print_role_switch)


def test_update_avisa_reiniciar():
    import inspect

    import main

    assert "Reiniciá" in inspect.getsource(main.run_update)


def test_doctor_chequea_checkout():
    import inspect

    import main

    src = inspect.getsource(main.run_doctor)
    assert "Checkout del harness" in src
    assert "rev-list" in src
