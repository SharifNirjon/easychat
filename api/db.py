"""Postgres access. All tenant data is read and written inside tenant_tx(), which sets the
`app.tenant_id` setting that the row-level-security policies check."""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
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
    await pool.open(wait=True, timeout=10)


async def close_pool() -> None:
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


async def ping() -> None:
    async with pool.connection() as conn:
        await conn.execute("SELECT 1")
