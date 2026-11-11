"""Settings fail closed: strict privacy by default, dev mode refused outside local/test."""
import pytest

import config
import llm


@pytest.fixture
def env(monkeypatch):
    monkeypatch.delenv("PRIVACY_MODE", raising=False)
    return monkeypatch


def test_privacy_defaults_to_strict(env):
    env.setenv("APP_ENV", "production")
    assert config.load_settings().privacy_mode == "strict"


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_dev_privacy_refused_outside_dev(env, app_env):
    env.setenv("APP_ENV", app_env)
    env.setenv("PRIVACY_MODE", "dev")
    with pytest.raises(RuntimeError, match="only allowed"):
        config.load_settings()


def test_dev_privacy_allowed_locally(env):
    env.setenv("APP_ENV", "local")
    env.setenv("PRIVACY_MODE", "dev")
    assert config.load_settings().privacy_mode == "dev"


def test_app_env_is_required(env):
    env.delenv("APP_ENV")
    with pytest.raises(RuntimeError, match="APP_ENV"):
        config.load_settings()


def test_strict_mode_sends_zdr_provider_prefs(monkeypatch):
    import dataclasses
    monkeypatch.setattr(llm, "settings", dataclasses.replace(llm.settings, privacy_mode="strict"))
    assert llm._openrouter_options()["provider"] == {"data_collection": "deny", "zdr": True, "sort": "latency"}
    monkeypatch.setattr(llm, "settings", dataclasses.replace(llm.settings, privacy_mode="dev"))
    assert "provider" not in llm._openrouter_options()
