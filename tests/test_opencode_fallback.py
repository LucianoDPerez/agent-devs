"""Fallback opencode → local en el arranque (sin red, todo mockeado).

Regresión del caso real: config en provider=opencode con la nube caída
(SSL CERTIFICATE_VERIFY_FAILED) y llama.cpp vivo → se sigue en local.
Solo se bloquea cuando NINGUNO de los dos responde.
"""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

import main
from llm_wrapper import LocalLLM


def _cloud_llm():
    return LocalLLM(base_url="https://opencode.ai/zen/v1",
                    model_name="deepseek-v4.1-flash", api_key="oc_sk_x")


def _tmp_repo():
    tmp = tempfile.mkdtemp()
    Path(tmp, "a.py").write_text("x = 1\n")
    return tmp


class TestIsOpencodeFailure:
    def test_ssl_es_fallo_nube(self):
        assert main._is_opencode_failure(
            RuntimeError("<urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] "
                         "certificate verify failed>"))

    def test_401_es_fallo_nube(self):
        assert main._is_opencode_failure(RuntimeError("Error code: 401 - Unauthorized"))

    def test_bug_real_no_es_fallo_nube(self):
        assert not main._is_opencode_failure(ValueError("bug de programacion"))


class TestEnsureAnalysisFallback:
    def test_con_local_vivo_levanta_fallback(self):
        repo = _tmp_repo()
        with patch.object(main, "_llama_server_alive",
                          return_value=(True, "http://localhost:8080")), \
             patch.object(main, "run_analysis",
                          side_effect=RuntimeError("<urlopen error [SSL: "
                                                   "CERTIFICATE_VERIFY_FAILED]>")), \
             patch("cache.snapshot_hash", return_value=None):
            with pytest.raises(main._UseLocalFallback):
                main._ensure_analysis(_cloud_llm(), repo, status=None)

    def test_sin_local_sale_con_exit_sin_traceback(self):
        repo = _tmp_repo()
        with patch.object(main, "_llama_server_alive",
                          return_value=(False, "http://localhost:8080")), \
             patch.object(main, "run_analysis",
                          side_effect=RuntimeError("Error code: 401 - Unauthorized")), \
             patch("cache.snapshot_hash", return_value=None):
            with pytest.raises(SystemExit) as exc:
                main._ensure_analysis(_cloud_llm(), repo, status=None)
            assert exc.value.code == 1

    def test_bug_real_sigue_con_traceback(self):
        repo = _tmp_repo()
        with patch.object(main, "_llama_server_alive",
                          return_value=(False, "http://localhost:8080")), \
             patch.object(main, "run_analysis",
                          side_effect=ValueError("bug de programacion")), \
             patch("cache.snapshot_hash", return_value=None):
            with pytest.raises(ValueError):
                main._ensure_analysis(_cloud_llm(), repo, status=None)


class TestSetupCompleto:
    def test_escribe_los_cinco_campos(self):
        import llm_config

        tmp = Path(tempfile.mkdtemp()) / "config.json"
        old = Path(tempfile.mkdtemp()) / "old.json"
        with patch.object(llm_config, "CONFIG_FILE", tmp), \
             patch.object(llm_config, "LEGACY_CONFIG_FILE", old), \
             patch("getpass.getpass", return_value="oc_sk_TESTKEY123"):
            assert main.run_setup_opencode() == 0
        data = json.loads(tmp.read_text())
        assert data["provider"] == "opencode"
        assert data["opencode_api_key"] == "oc_sk_TESTKEY123"
        assert data["opencode_model"] == "deepseek-v4.1-flash"
        assert data["opencode_reasoning"] == "medium"
        assert data["opencode_base_url"] == "https://opencode.ai/zen/v1"

    def test_respeta_valores_existentes(self):
        import llm_config

        tmp = Path(tempfile.mkdtemp()) / "config.json"
        old = Path(tempfile.mkdtemp()) / "old.json"
        tmp.write_text(json.dumps({
            "provider": "opencode",
            "opencode_api_key": "oc_sk_OLD",
            "opencode_model": "muse-spark-1.3",
            "opencode_reasoning": "high",
            "opencode_base_url": "https://opencode.ai/zen/v1",
        }))
        with patch.object(llm_config, "CONFIG_FILE", tmp), \
             patch.object(llm_config, "LEGACY_CONFIG_FILE", old), \
             patch("getpass.getpass", return_value=""):
            assert main.run_setup_opencode() == 0
        data = json.loads(tmp.read_text())
        assert data["opencode_model"] == "muse-spark-1.3"
        assert data["opencode_reasoning"] == "high"
