"""Suite en español por defecto + config aislada del home real.

El harness resuelve idioma con override > config file > env > default(en).
Sin este fixture, la suite dependería del $LANG y del config de la máquina
(rompería las aserciones históricas en español). Los tests de idioma
(test_language.py) gestionan su propio entorno con monkeypatch.
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _spanish_hermetic_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DEVS_LANGUAGE", "es")
    for var in ("LANGUAGE", "LC_ALL", "LANG"):
        monkeypatch.delenv(var, raising=False)
    import llm_config

    monkeypatch.setattr(llm_config, "CONFIG_FILE", tmp_path / "cfg.json")
    monkeypatch.setattr(
        llm_config, "LEGACY_CONFIG_FILE", tmp_path / "legacy.json"
    )
