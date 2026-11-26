"""Settings fail closed: strict privacy by default, dev mode refused outside local/test."""
import base64
import dataclasses

import pytest

import config
import llm


@pytest.fixture
def env(monkeypatch):
    """A monkeypatch with PRIVACY_MODE unset (to test the defaults), an https app URL and a
    valid SECRETS_KEY (32 bytes, base64)."""
    monkeypatch.delenv("PRIVACY_MODE", raising=False)
    monkeypatch.setenv("APP_BASE_URL", "https://app.easychat.example")
    monkeypatch.setenv("SECRETS_KEY", base64.urlsafe_b64encode(bytes(range(32))).decode())
    return monkeypatch


def test_privacy_defaults_to_strict(env):
    """Zero data retention is the default; nobody has to remember to turn it on."""
    env.setenv("APP_ENV", "production")
    assert config.load_settings().privacy_mode == "strict"


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_dev_privacy_refused_outside_dev(env, app_env):
    """PRIVACY_MODE=dev can't be used in staging or production."""
    env.setenv("APP_ENV", app_env)
    env.setenv("PRIVACY_MODE", "dev")
    with pytest.raises(RuntimeError, match="only allowed"):
        config.load_settings()


def test_dev_privacy_allowed_locally(env):
    """Local development may use free models (dev privacy mode)."""
    env.setenv("APP_ENV", "local")
    env.setenv("PRIVACY_MODE", "dev")
    assert config.load_settings().privacy_mode == "dev"


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_plain_http_app_url_refused_outside_dev(env, app_env):
    """Sign-in cookies are Secure outside dev, so the app URL must be https there."""
    env.setenv("APP_ENV", app_env)
    env.setenv("APP_BASE_URL", "http://app.easychat.example")
    with pytest.raises(RuntimeError, match="APP_BASE_URL"):
        config.load_settings()


def test_app_origin_is_derived_from_the_app_url(env):
    """The preview origin is scheme://host[:port] of APP_BASE_URL."""
    env.setenv("APP_ENV", "local")
    env.setenv("APP_BASE_URL", "http://LocalHost:8000/")
    assert config.load_settings().app_origin == "http://localhost:8000"


def test_app_env_is_required(env):
    """APP_ENV has no default: a missing one stops the app at startup."""
    env.delenv("APP_ENV")
    with pytest.raises(RuntimeError, match="APP_ENV"):
        config.load_settings()


def test_strict_mode_sends_zdr_provider_prefs(monkeypatch):
    """Strict mode sends the ZDR provider preferences; dev mode sends none."""
    monkeypatch.setattr(llm, "settings", dataclasses.replace(llm.settings, privacy_mode="strict"))
    assert llm._openrouter_options()["provider"] == {"data_collection": "deny", "zdr": True, "sort": "latency"}
    monkeypatch.setattr(llm, "settings", dataclasses.replace(llm.settings, privacy_mode="dev"))
    assert "provider" not in llm._openrouter_options()


def test_secrets_key_is_required_outside_dev(env):
    """Staging/production can't start without the key that protects tenants' shop credentials."""
    env.setenv("APP_ENV", "production")
    env.delenv("SECRETS_KEY")
    with pytest.raises(RuntimeError, match="SECRETS_KEY"):
        config.load_settings()


@pytest.mark.parametrize("value", ["not base64!", base64.b64encode(b"too short").decode()])
def test_secrets_key_must_be_32_base64_bytes(env, value):
    """A malformed key stops the app instead of weakening the encryption."""
    env.setenv("APP_ENV", "production")
    env.setenv("SECRETS_KEY", value)
    with pytest.raises(RuntimeError, match="SECRETS_KEY"):
        config.load_settings()


def test_local_dev_may_run_without_a_secrets_key(env):
    """Locally, order tracking is simply unavailable without one."""
    env.setenv("APP_ENV", "local")
    env.delenv("SECRETS_KEY")
    assert config.load_settings().secrets_key is None
