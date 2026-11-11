"""Application settings, read once from environment variables (api/.env locally).

Nothing in the code base reads os.environ directly — everything goes through `settings`.
"""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load api/.env if present. Real environment variables take precedence.
load_dotenv(Path(__file__).parent / ".env")

APP_ENVS = ("local", "test", "staging", "production")
DEV_ENVS = ("local", "test")


def _required(name: str, default: str | None = None) -> str:
    """Return a non-empty env var, or fail fast at startup with a clear message."""
    value = os.getenv(name, default)
    if value is None or not value.strip():
        raise RuntimeError(f"Missing required env var {name} (see api/.env.example)")
    return value.strip()


def _csv(name: str, default: str = "") -> list[str]:
    """Parse a comma-separated env var into a list, ignoring blanks."""
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


def _choice(name: str, default: str | None, allowed: tuple[str, ...]) -> str:
    """Read an env var that must be one of a fixed set of values."""
    value = _required(name, default).lower()
    if value not in allowed:
        raise RuntimeError(f"{name} must be one of {allowed}, got {value!r}")
    return value


def _int(name: str, default: str) -> int:
    value = int(_required(name, default))
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True)
class Settings:
    app_env: str                 # local | test | staging | production

    # LLM provider (OpenRouter, OpenAI-compatible)
    llm_base_url: str
    api_key: str
    chat_model: str
    fallback_models: list[str]   # tried in order if chat_model fails
    privacy_mode: str            # "strict" (default) | "dev" (local/test only)
    reasoning: str               # "off" | "low"
    max_tokens: int
    timeout_seconds: float

    # Data stores
    database_url: str            # the API's login role: a member of easychat_app, never the owner
    redis_url: str

    # Abuse and cost limits (per-tenant caps live in tenants.limits, defaults in limits.py)
    ip_messages_per_minute: int
    max_request_bytes: int
    # How many reverse proxies in front of us append to X-Forwarded-For. 0 = use the socket
    # peer address (local dev). Must match the real deployment or clients can spoof their IP.
    trusted_proxy_hops: int

    @property
    def is_dev(self) -> bool:
        return self.app_env in DEV_ENVS


def load_settings() -> Settings:
    app_env = _choice("APP_ENV", None, APP_ENVS)

    # Fail closed: strict privacy unless explicitly relaxed, and never relaxed outside dev.
    privacy_mode = _choice("PRIVACY_MODE", "strict", ("dev", "strict"))
    if privacy_mode == "dev" and app_env not in DEV_ENVS:
        raise RuntimeError(f"PRIVACY_MODE=dev is only allowed when APP_ENV is one of {DEV_ENVS}")

    return Settings(
        app_env=app_env,
        llm_base_url=_required("LLM_BASE_URL", "https://openrouter.ai/api/v1"),
        api_key=_required("OPENROUTER_API_KEY"),
        chat_model=_required("CHAT_MODEL", "openai/gpt-oss-20b"),
        fallback_models=_csv("FALLBACK_MODELS"),
        privacy_mode=privacy_mode,
        reasoning=_choice("REASONING", "off", ("off", "low")),
        max_tokens=_int("MAX_TOKENS", "600"),
        timeout_seconds=float(_required("LLM_TIMEOUT_SECONDS", "20")),
        database_url=_required("DATABASE_URL"),
        redis_url=_required("REDIS_URL"),
        ip_messages_per_minute=_int("IP_MESSAGES_PER_MINUTE", "20"),
        max_request_bytes=_int("MAX_REQUEST_BYTES", "16384"),
        trusted_proxy_hops=int(_required("TRUSTED_PROXY_HOPS", "0")),
    )


settings = load_settings()
