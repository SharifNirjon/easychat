"""Test fixtures. Runs against a throwaway Postgres database and Redis db (see docker-compose
`api-test`). The LLM is always stubbed: tests never call OpenRouter or spend money."""
import os
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse

# --- Environment must be set before any app module is imported (config loads at import). ---
os.environ["OPENROUTER_API_KEY"] = "test-key-never-used"
if os.environ.get("APP_ENV") != "test":
    sys.exit("Refusing to run tests: APP_ENV must be 'test' (use `docker compose run --rm api-test`).")
for var in ("DATABASE_URL", "MIGRATION_DATABASE_URL"):
    if not urlparse(os.environ.get(var, "")).path.endswith("_test"):
        sys.exit(f"Refusing to run tests: {var} must point at a database whose name ends in _test.")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import psycopg  # noqa: E402
import pytest  # noqa: E402
import redis  # noqa: E402

import migrate  # noqa: E402
import seed_tenant  # noqa: E402
from models import TenantSeed  # noqa: E402

OWNER_URL = os.environ["MIGRATION_DATABASE_URL"]
APP_URL = os.environ["DATABASE_URL"]
ORIGIN_A = "https://shop-a.example"
ORIGIN_B = "https://shop-b.example"
KEY_A = "pk_test_tenant_a"
KEY_B = "pk_test_tenant_b"


def _tenant_seed(slug: str, key: str, origin: str, **limits) -> TenantSeed:
    return TenantSeed.model_validate({
        "slug": slug,
        "name": slug.title(),
        "site_key": key,
        "allowed_origins": [origin],
        "limits": limits,
        "config": {
            "company_name": f"{slug} Inc",
            "bot_name": "Bot",
            "greeting": {"title": "Hi", "headline": "What *now*?", "subtitle": "Ask me."},
            "disclosure": "AI assistant.",
            "teaser": "Hello!",
            "suggested_prompts": ["One?"],
            "privacy_url": "https://example.com/privacy",
            "theme": {"primary": "#112233", "primary_dark": "#001122", "accent": "#AABBCC"},
        },
    })


def _recreate_test_database() -> None:
    url = urlparse(OWNER_URL)
    name = url.path.lstrip("/")
    admin_url = urlunparse(url._replace(path="/postgres"))
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{name}"')


@pytest.fixture(scope="session", autouse=True)
def database():
    """Fresh database: migrations, the non-owner API role, and two tenants."""
    _recreate_test_database()
    with psycopg.connect(OWNER_URL, autocommit=True) as conn:
        migrate.apply_migrations(conn)
        migrate.ensure_dev_app_role(conn, APP_URL)
        seed_tenant.seed(conn, _tenant_seed("tenant-a", KEY_A, ORIGIN_A))
        seed_tenant.seed(conn, _tenant_seed("tenant-b", KEY_B, ORIGIN_B, monthly_conversations=2,
                                            conversation_user_turns=2))
    yield


@pytest.fixture
def owner_db():
    """Owner connection (bypasses RLS) for arranging and inspecting data."""
    with psycopg.connect(OWNER_URL, autocommit=True) as conn:
        yield conn


@pytest.fixture(autouse=True)
def clean_state(database):
    with psycopg.connect(OWNER_URL, autocommit=True) as conn:
        conn.execute("TRUNCATE messages, conversations")
    redis.from_url(os.environ["REDIS_URL"]).flushdb()
    yield


class FakeLLM:
    """Stands in for llm.stream_reply. Records the prompts it was given."""

    def __init__(self):
        self.calls: list[list[dict]] = []
        self.reply = ["Hello", " there"]
        self.fail_with: Exception | None = None

    async def __call__(self, messages, usage):
        self.calls.append(messages)
        if self.fail_with:
            raise self.fail_with
        usage.model = "stub/model"
        usage.tokens_in, usage.tokens_out = 100, 20
        for chunk in self.reply:
            yield chunk


@pytest.fixture
def fake_llm(monkeypatch):
    import main
    fake = FakeLLM()
    monkeypatch.setattr(main, "stream_reply", fake)
    return fake


@pytest.fixture(scope="session")
async def app():
    import db
    import main
    await db.open_pool()
    yield main.app
    await db.close_pool()


@pytest.fixture
async def client(app, fake_llm):
    transport = httpx.ASGITransport(app=app, client=("203.0.113.7", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as c:
        yield c
    import main
    for task in list(main._background):   # let token bookkeeping finish before the next test
        await task
