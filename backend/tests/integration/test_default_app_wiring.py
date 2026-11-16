"""The default app exposes knowledge + analytics, and execution prompts carry relevant knowledge."""
import pytest

from orchestrator.api.app import create_app


def _paths(app):
    return set(app.openapi()["paths"])


def test_default_app_registers_knowledge_analytics_skills(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_DISABLE_REAL_PROVIDERS", "1")
    app = create_app(data_dir=tmp_path)
    paths = _paths(app)
    assert "/api/knowledge/search" in paths
    assert "/api/analytics/summary" in paths
    assert any(p.startswith("/api/skills") for p in paths)
    assert "/api/leadership" in paths


def test_routing_service_reads_adaptive_settings_override():
    from orchestrator.analytics.adaptive import SETTINGS_KEY, effective_adaptive_cfg

    yaml_cfg = {"adaptive": {"enabled": True, "min_samples": 20, "max_adjustment": 10}}
    assert effective_adaptive_cfg({}, yaml_cfg)["min_samples"] == 20
    assert effective_adaptive_cfg({SETTINGS_KEY: {"enabled": False}}, yaml_cfg)["enabled"] is False
    # out-of-range override is ignored
    assert effective_adaptive_cfg({SETTINGS_KEY: {"min_samples": 1}}, yaml_cfg)["min_samples"] == 20
