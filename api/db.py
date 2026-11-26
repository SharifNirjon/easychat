"""Postgres access. All tenant data is read and written inside tenant_tx(), which sets the
`app.tenant_id` setting that the row-level-security policies check."""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from config import settings

pool = AsyncConnectionPool(
    settings.database_url,
    min_size=1,
    max_size=10,
    open=False,                      # opened in the app's lifespan
    kwargs={"row_factory": dict_row, "autocommit": True},
)


async def open_pool() -> None:
    """Open the connection pool (called once at startup, in main.lifespan)."""
    await pool.open(wait=True, timeout=10)


async def close_pool() -> None:
    """Close every pooled connection (called at shutdown)."""
    await pool.close()


@asynccontextmanager
async def tenant_tx(tenant_id: UUID) -> AsyncIterator[AsyncConnection]:
    """One transaction scoped to one tenant. RLS hides every other tenant's rows."""
    async with pool.connection() as conn, conn.transaction():
        # set_config(..., true) == SET LOCAL: ends with the transaction, never leaks via the pool.
        await conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))
        yield conn


async def resolve_site_key(public_key: str) -> dict | None:
    """Site key -> tenant row, via the SECURITY DEFINER function (the only pre-tenant lookup)."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT * FROM resolve_site_key(%s)", (public_key,))
        return await cur.fetchone()


async def resolve_login(email: str) -> dict | None:
    """Email -> {user_id, tenant_id}, via the SECURITY DEFINER function (sign-in, step 7)."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT * FROM resolve_login(%s)", (email,))
        return await cur.fetchone()


async def create_signup(email: str, slug: str, name: str, config: dict) -> dict | None:
    """Create a tenant + its owner for a new email (or return the existing account).
    None only if the random slug collided: the caller retries with another."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT * FROM create_signup(%s, %s, %s, %s)",
                                 (email, slug, name, Jsonb(config)))
        return await cur.fetchone()


async def purge_expired_requests() -> int:
    """Delete every tenant's expired chat-form requests (SECURITY DEFINER, step 9); how many went."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT purge_expired_requests() AS deleted")
        return (await cur.fetchone())["deleted"]


async def ping() -> None:
    """Cheapest possible query: proves Postgres is reachable (used by /ready)."""
    async with pool.connection() as conn:
        await conn.execute("SELECT 1")
