"""Pre-flight por proveedor y /model en caliente (sin red)."""
import json
from types import SimpleNamespace

import pytest

from llm_wrapper import LocalLLM
from orchestration.session import Session


def _local_llm(**kw):
    args = dict(base_url="http://localhost:9999/v1", model_name="t", api_key="not-needed")
    args.update(kw)
    return LocalLLM(**args)


def _session(tmp_path, llm):
    return Session(llm, str(tmp_path), cached_analysis="")


class TestPreflight:
    def test_local_down_blocks(self, tmp_path):
        s = _session(tmp_path, _local_llm())
        assert s._preflight_llm() is False

    def test_cloud_with_key_passes(self, tmp_path):
        llm = _local_llm(base_url="https://opencode.ai/zen/v1", model_name="m",
                         api_key="oc_sk_fake", transport="chat")
        assert _session(tmp_path, llm)._preflight_llm() is True

    def test_cloud_without_key_blocks(self, tmp_path):
        llm = _local_llm(base_url="https://opencode.ai/zen/v1", model_name="m", api_key="")
        assert _session(tmp_path, llm)._preflight_llm() is False


class TestProfileBudgets:
    def test_local_tight(self):
        from core.model_profiles import analyze_budget_for, plan_budget_for

        local = SimpleNamespace(base_url="http://localhost:8080/v1")
        assert analyze_budget_for(local) == (4, 8)
        assert plan_budget_for(local) == (4, 8)

    def test_empty_base_tight(self):
        from core.model_profiles import analyze_budget_for

        assert analyze_budget_for(SimpleNamespace(base_url="")) == (4, 8)
        assert analyze_budget_for(object()) == (4, 8)

    def test_cloud_roomy(self):
        from core.model_profiles import analyze_budget_for, plan_budget_for

        cloud = SimpleNamespace(base_url="https://opencode.ai/zen/v1")
        assert analyze_budget_for(cloud) == (10, 12)
        assert plan_budget_for(cloud) == (10, 12)

    def test_session_builds_roomy_for_cloud(self, tmp_path):
        cloud = LocalLLM(base_url="https://opencode.ai/zen/v1", model_name="muse-spark-1.3",
                         api_key="k", transport="responses")
        assert _session(tmp_path, cloud)._analyze_budget.max_calls == 10

    def test_session_builds_tight_for_local(self, tmp_path):
        assert _session(tmp_path, _local_llm())._analyze_budget.max_calls == 4

    def test_switch_rebuilds_budget(self, tmp_path, monkeypatch):
        import llm_config

        cfg = tmp_path / "cfg.json"
        cfg.write_text(json.dumps({"opencode_api_key": "oc_sk_fake"}))
        monkeypatch.setattr(llm_config, "CONFIG_FILE", cfg)
        monkeypatch.setattr(llm_config, "LEGACY_CONFIG_FILE", tmp_path / "old.json")
        s = _session(tmp_path, _local_llm())
        assert s._analyze_budget.max_calls == 4
        ok, _ = s.switch_model(model="muse-spark-1.3")
        assert ok is True
        assert s._analyze_budget.max_calls == 10
        ok, _ = s.switch_model(provider="local")
        assert ok is True
        assert s._analyze_budget.max_calls == 4


class TestSwitchModel:
    def test_to_local(self, tmp_path, monkeypatch):
        import llm_config

        cfg = tmp_path / "cfg.json"
        monkeypatch.setattr(llm_config, "CONFIG_FILE", cfg)
        s = _session(tmp_path, _local_llm(base_url="https://opencode.ai/zen/v1",
                                          model_name="deepseek-v4.1-flash",
                                          api_key="oc_sk_fake"))
        s.agent = object()  # simula agente construido
        ok, msg = s.switch_model(provider="local")
        assert ok is True
        assert "local" in msg
        assert s.agent is None  # se reconstruye en el próximo turno
        assert "localhost" in s.llm.base_url
        saved = json.loads(cfg.read_text())
        assert saved["provider"] == "local"

    def test_messages_api_blocked(self, tmp_path):
        s = _session(tmp_path, _local_llm())
        ok, msg = s.switch_model(model="claude-sonnet-5")
        assert ok is False
        assert "Anthropic" in msg

    def test_slash_parsing(self, tmp_path, monkeypatch):
        import llm_config

        monkeypatch.setattr(llm_config, "CONFIG_FILE", tmp_path / "cfg.json")
        s = _session(tmp_path, _local_llm())
        assert "local" in s.slash_model("local")
        assert s.llm.base_url.startswith("http://localhost")
        bad = s.slash_model("claude-haiku-4-5")
        assert "Anthropic" in bad

    def test_status_exposes_llm(self, tmp_path):
        llm = _local_llm(base_url="https://opencode.ai/zen/v1", model_name="muse-spark-1.3",
                         api_key="k", transport="responses", reasoning_effort="high")
        st = _session(tmp_path, llm).get_status()
        assert st["model"] == "muse-spark-1.3"
        assert st["llm_base"] == "https://opencode.ai/zen/v1"
        assert st["transport"] == "responses"
        assert st["reasoning"] == "high"


class TestShowConfig:
    def test_key_masked(self, tmp_path, monkeypatch, capsys):
        import llm_config
        from main import run_show_config

        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({
            "provider": "opencode",
            "opencode_api_key": "oc_sk_FAKETESTKEY1234567890",
            "opencode_model": "muse-spark-1.3",
            "opencode_reasoning": "high",
        }))
        monkeypatch.setattr(llm_config, "CONFIG_FILE", cfg)
        monkeypatch.setattr(llm_config, "LEGACY_CONFIG_FILE", tmp_path / "old.json")
        assert run_show_config() == 0
        out = capsys.readouterr().out
        assert "oc_sk_FAKETESTKEY1234567890" not in out  # key completa jamás
        assert "muse-spark-1.3" in out
        assert "high" in out
        assert "responses" in out
