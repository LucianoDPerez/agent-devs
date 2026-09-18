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


def test_imports_blindados_contra_cwd_con_config(tmp_path):
    """Un CWD con config/ genérico no rompe los imports (shadowing).

    E2E: `python -c "import orchestration..."` desde otro repo fallaba con
    ImportError de `config` en ubicación desconocida.
    """
    import subprocess
    import sys

    (tmp_path / "config").mkdir()  # namespace package trampa, sin __init__
    r = subprocess.run(
        [sys.executable, "-c", "import orchestration.tool_dedupe; print('OK')"],
        capture_output=True, text=True, cwd=str(tmp_path), timeout=120,
    )
    assert r.returncode == 0, r.stderr[-500:]
    assert "OK" in r.stdout


def test_main_arranca_desde_cwd_con_config(tmp_path):
    """`python main.py --help` funciona aunque el CWD tenga config/."""
    import subprocess
    import sys

    (tmp_path / "config").mkdir()
    repo = Path(__file__).resolve().parent.parent
    r = subprocess.run(
        [sys.executable, str(repo / "main.py"), "--help"],
        capture_output=True, text=True, cwd=str(tmp_path), timeout=180,
    )
    assert r.returncode == 0, r.stderr[-500:]
    assert "AgentDevs" in r.stdout
