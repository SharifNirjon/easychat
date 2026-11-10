"""Application settings, read once from environment variables (api/.env locally).

Nothing in the code base reads os.environ directly — everything goes through `settings`.
"""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load api/.env if present. Real environment variables take precedence.
load_dotenv(Path(__file__).parent / ".env")


def _required(name: str, default: str | None = None) -> str:
    """Return a non-empty env var, or fail fast at startup with a clear message."""
    value = os.getenv(name, default)
    if value is None or not value.strip():
        raise RuntimeError(f"Missing required env var {name} (see api/.env.example)")
    return value.strip()


def _csv(name: str, default: str = "") -> list[str]:
    """Parse a comma-separated env var into a list, ignoring blanks."""
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


def _choice(name: str, default: str, allowed: tuple[str, ...]) -> str:
    """Read an env var that must be one of a fixed set of values."""
    value = _required(name, default).lower()
    if value not in allowed:
        raise RuntimeError(f"{name} must be one of {allowed}, got {value!r}")
    return value


@dataclass(frozen=True)
class Settings:
    # LLM provider (OpenRouter, OpenAI-compatible)
    llm_base_url: str
    api_key: str
    chat_model: str
    fallback_models: list[str]   # tried in order if chat_model fails
    privacy_mode: str            # "dev" | "strict"
    reasoning: str               # "off" | "low"
    max_tokens: int
    timeout_seconds: float

    # Allowed origins, names and branding are per tenant: see api/tenants/*.json


def load_settings() -> Settings:
    return Settings(
        llm_base_url=_required("LLM_BASE_URL", "https://openrouter.ai/api/v1"),
        api_key=_required("OPENROUTER_API_KEY"),
        chat_model=_required("CHAT_MODEL", "nvidia/nemotron-3.5-lightning:free"),
        fallback_models=_csv("FALLBACK_MODELS"),
        privacy_mode=_choice("PRIVACY_MODE", "dev", ("dev", "strict")),
        reasoning=_choice("REASONING", "off", ("off", "low")),
        max_tokens=int(_required("MAX_TOKENS", "600")),
        timeout_seconds=float(_required("LLM_TIMEOUT_SECONDS", "20")),
    )


settings = load_settings()
