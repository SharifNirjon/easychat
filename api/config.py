"""Application settings, read once from environment variables (api/.env locally).

Nothing in the code base reads os.environ directly — everything goes through `settings`.
"""
import base64
import binascii
import os
import secrets
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
    """Read a positive integer env var."""
    value = int(_required(name, default))
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return value


def _fraction(name: str, default: str) -> float:
    """Read an env var that must be a number strictly between 0 and 1."""
    value = float(_required(name, default))
    if not 0 < value < 1:
        raise RuntimeError(f"{name} must be between 0 and 1")
    return value


@dataclass(frozen=True)
class Settings:
    """Every setting the API uses. Built once at import (`settings` below); never mutated."""
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

    # Knowledge (roadmap step 5): embeddings + retrieval
    embed_model: str             # must return EMBED_DIMENSIONS-long vectors (see embeddings.py)
    rag_min_score: float         # cosine similarity a chunk needs to count as relevant (0..1)
    rag_top_k: int               # chunks given to the model per answer

    # Data stores
    database_url: str            # the API's login role: a member of easychat_app, never the owner
    redis_url: str

    # Abuse and cost limits (per-tenant caps live in tenants.limits, defaults in limits.py)
    ip_messages_per_minute: int
    max_request_bytes: int
    # How many reverse proxies in front of us append to X-Forwarded-For. 0 = use the socket
    # peer address (local dev). Must match the real deployment or clients can spoof their IP.
    trusted_proxy_hops: int

    # Self-serve onboarding (roadmap step 7)
    app_base_url: str            # where the onboarding app is served (/app); links in emails point here
    widget_url: str              # the widget.js URL put in tenants' script tags
    smtp_host: str               # Mailpit locally, SendGrid's SMTP relay later
    smtp_port: int
    smtp_username: str           # empty = no SMTP login (Mailpit)
    smtp_password: str
    mail_from: str
    signups_per_day: int         # platform-wide new tenants per day: abuse cap and kill switch

    # Commerce (roadmap step 8): encrypts tenants' shop credentials and keys one-time-code hashes.
    # None only in local dev without SECRETS_KEY: order tracking then can't be connected.
    secrets_key: bytes | None

    @property
    def app_origin(self) -> str:
        """scheme://host[:port] of the onboarding app: its widget preview runs from here."""
        parts = self.app_base_url.split("/")
        return "/".join(parts[:3]).lower()

    @property
    def is_dev(self) -> bool:
        """True in local/test: enables /docs, /demo, PRIVACY_MODE=dev and http://localhost URLs."""
        return self.app_env in DEV_ENVS


SECRETS_KEY_BYTES = 32           # AES-256


def _secrets_key(app_env: str) -> bytes | None:
    """SECRETS_KEY: 32 random bytes, base64. Required outside local/test. Tests without one get a
    throwaway key per run; local dev without one simply can't connect order tracking."""
    raw = os.getenv("SECRETS_KEY", "").strip()
    if not raw:
        if app_env == "test":
            return secrets.token_bytes(SECRETS_KEY_BYTES)
        if app_env in DEV_ENVS:
            return None
        raise RuntimeError("Missing required env var SECRETS_KEY (see api/.env.example)")
    try:
        key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (binascii.Error, ValueError) as exc:
        raise RuntimeError("SECRETS_KEY must be base64") from exc
    if len(key) != SECRETS_KEY_BYTES:
        raise RuntimeError(f"SECRETS_KEY must decode to {SECRETS_KEY_BYTES} bytes")
    return key


def load_settings() -> Settings:
    """Read and validate the environment. Fails fast at startup on anything missing or invalid."""
    app_env = _choice("APP_ENV", None, APP_ENVS)

    # Fail closed: strict privacy unless explicitly relaxed, and never relaxed outside dev.
    privacy_mode = _choice("PRIVACY_MODE", "strict", ("dev", "strict"))
    if privacy_mode == "dev" and app_env not in DEV_ENVS:
        raise RuntimeError(f"PRIVACY_MODE=dev is only allowed when APP_ENV is one of {DEV_ENVS}")
    # Sign-in cookies are Secure outside dev, so the app must be served over https there.
    if app_env not in DEV_ENVS and not os.getenv("APP_BASE_URL", "").startswith("https://"):
        raise RuntimeError("APP_BASE_URL must be an https URL outside local/test")

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
        embed_model=_required("EMBED_MODEL", "baai/bge-m3"),
        # bge-m3 on real pages (step 5 tuning): relevant chunks ~0.47-0.61, unrelated ~0.40-0.47.
        # The model's no-answer marker is the second gate, so the floor leans towards recall.
        rag_min_score=_fraction("RAG_MIN_SCORE", "0.45"),
        rag_top_k=_int("RAG_TOP_K", "6"),
        database_url=_required("DATABASE_URL"),
        redis_url=_required("REDIS_URL"),
        ip_messages_per_minute=_int("IP_MESSAGES_PER_MINUTE", "20"),
        max_request_bytes=_int("MAX_REQUEST_BYTES", "16384"),
        trusted_proxy_hops=int(_required("TRUSTED_PROXY_HOPS", "0")),
        app_base_url=_required("APP_BASE_URL", "http://localhost:8000").rstrip("/"),
        widget_url=_required("WIDGET_URL", "http://localhost:5173/widget.js"),
        smtp_host=_required("SMTP_HOST", "127.0.0.1"),
        smtp_port=_int("SMTP_PORT", "1025"),
        smtp_username=os.getenv("SMTP_USERNAME", "").strip(),
        smtp_password=os.getenv("SMTP_PASSWORD", "").strip(),
        mail_from=_required("MAIL_FROM", "easychat <hello@easychat.local>"),
        signups_per_day=_int("SIGNUPS_PER_DAY", "200"),
        secrets_key=_secrets_key(app_env),
    )


settings = load_settings()
