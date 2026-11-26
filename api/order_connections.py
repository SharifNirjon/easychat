"""A tenant's order system (roadmap step 8): stored encrypted, managed from the onboarding app.

  PUT    /api/onboarding/orders/shopify  {shop, client_id + client_secret | access_token}
                                         checks the credentials with Shopify, then saves them
  PUT    /api/onboarding/orders/api      {url}: the shop's own order endpoint; answers with a
                                         newly generated secret, shown this once only
  POST   /api/onboarding/orders/test     {order_number}: look one order up, as the chat would
  DELETE /api/onboarding/orders          disconnect

Credentials are sealed with secretbox (bound to the tenant) before they reach Postgres, and
never leave the server again: summary() shows only the kind and the shop / URL.
"""
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import db
import limits
from auth import Session, current_session, same_origin
from config import settings
from fetcher import FetchError, check_url, host_key
from order_sources import (GenericApiSource, OrderSource, OrderSourceError, ShopifySource, normalize_order_number,
                           valid_shop_domain)
from secretbox import SealError, SecretsUnavailable, open_sealed, seal

log = logging.getLogger("easychat.orders")

API_SECRET_PREFIX = "ec_ord_"
MAX_ENDPOINT_CHARS = 500


@dataclass(frozen=True)
class Connection:
    """A tenant's order system, with its credentials decrypted."""
    kind: str                     # 'shopify' | 'api'
    endpoint: str                 # shop domain or API URL
    credentials: dict
    checked_at: datetime | None


def _context(tenant_id: UUID) -> str:
    """secretbox context: the credentials only decrypt for this tenant and this purpose."""
    return f"order-credentials:{tenant_id}"


async def load(tenant_id: UUID) -> Connection | None:
    """The tenant's connection, or None if it has none (or its credentials can't be read)."""
    async with db.tenant_tx(tenant_id) as conn:
        cur = await conn.execute("SELECT kind, endpoint, credentials, checked_at FROM order_connections")
        row = await cur.fetchone()
    if row is None:
        return None
    try:
        credentials = open_sealed(bytes(row["credentials"]), _context(tenant_id))
    except (SealError, SecretsUnavailable) as exc:
        log.error("order credentials of tenant %s can't be read: %s", tenant_id, type(exc).__name__)
        return None
    return Connection(row["kind"], row["endpoint"], credentials, row["checked_at"])


async def save(tenant_id: UUID, kind: str, endpoint: str, credentials: dict) -> None:
    """Store (or replace) the tenant's connection, credentials sealed."""
    sealed = seal(credentials, _context(tenant_id))
    async with db.tenant_tx(tenant_id) as conn:
        await conn.execute(
            "INSERT INTO order_connections (tenant_id, kind, endpoint, credentials) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (tenant_id) DO UPDATE SET kind = EXCLUDED.kind, endpoint = EXCLUDED.endpoint, "
            "credentials = EXCLUDED.credentials, created_at = now(), checked_at = NULL",
            (tenant_id, kind, endpoint, sealed))
    await RedisTokenCache(tenant_id).drop()


# --- Shopify token cache ------------------------------------------------------------------------

class RedisTokenCache:
    """Shopify access tokens from the client-credentials exchange, sealed, until they expire."""

    def __init__(self, tenant_id: UUID):
        """One cache entry per tenant."""
        self.key = f"orders:shopify-token:{tenant_id}"
        self.context = f"shopify-token:{tenant_id}"

    async def get(self) -> str | None:
        """The cached token, if any (an unreadable entry counts as none)."""
        try:
            blob = await limits.client.get(self.key)
            return open_sealed(blob, self.context)["token"] if blob else None
        except (aioredis.RedisError, SealError, KeyError):
            return None

    async def put(self, token: str, ttl_seconds: int) -> None:
        """Cache a token (best effort: without the cache we just exchange again)."""
        try:
            await limits.client.set(self.key, seal({"token": token}, self.context), ex=ttl_seconds)
        except aioredis.RedisError:
            log.warning("shopify token not cached: redis unavailable")

    async def drop(self) -> None:
        """Forget the token (credentials changed, or Shopify refused it)."""
        try:
            await limits.client.delete(self.key)
        except aioredis.RedisError:
            log.warning("shopify token not dropped: redis unavailable")


def source_from(tenant_id: UUID, connection: Connection) -> OrderSource:
    """The OrderSource for a stored connection."""
    if connection.kind == "shopify":
        return ShopifySource(connection.endpoint, connection.credentials, RedisTokenCache(tenant_id))
    return GenericApiSource(connection.endpoint, connection.credentials["secret"])


async def source_for(tenant_id: UUID) -> OrderSource | None:
    """The tenant's order source, or None when it has no (readable) connection."""
    connection = await load(tenant_id)
    return source_from(tenant_id, connection) if connection else None


async def summary(tenant_id: UUID) -> dict | None:
    """What the onboarding app shows: never the credentials."""
    async with db.tenant_tx(tenant_id) as conn:
        cur = await conn.execute("SELECT kind, endpoint, created_at, checked_at FROM order_connections")
        row = await cur.fetchone()
    if row is None:
        return None
    return {"kind": row["kind"], "endpoint": row["endpoint"], "connected_at": row["created_at"].isoformat(),
            "checked_at": row["checked_at"].isoformat() if row["checked_at"] else None}


# --- Routes -------------------------------------------------------------------------------------

router = APIRouter(prefix="/api/onboarding/orders", dependencies=[Depends(same_origin)])


def _secrets_ready() -> None:
    """503 when this server has no SECRETS_KEY (local dev without one)."""
    if settings.secrets_key is None:
        raise HTTPException(status_code=503, detail="Order tracking isn't set up on this server (SECRETS_KEY).")


class ShopifyIn(BaseModel):
    """A Shopify store and its app credentials: client ID + secret, or a legacy access token."""
    shop: str = Field(min_length=3, max_length=200)
    client_id: str | None = Field(default=None, min_length=8, max_length=200)
    client_secret: str | None = Field(default=None, min_length=8, max_length=300)
    access_token: str | None = Field(default=None, min_length=8, max_length=300)


@router.put("/shopify")
async def connect_shopify(body: ShopifyIn, session: Session = Depends(current_session)) -> dict:
    """Check the credentials with Shopify, then store them encrypted."""
    _secrets_ready()
    shop = valid_shop_domain(body.shop)
    if shop is None:
        raise HTTPException(status_code=422, detail="Enter your store's address, like my-store.myshopify.com")
    if body.access_token:
        credentials = {"access_token": body.access_token.strip()}
    elif body.client_id and body.client_secret:
        credentials = {"client_id": body.client_id.strip(), "client_secret": body.client_secret.strip()}
    else:
        raise HTTPException(status_code=422, detail="Enter the app's client ID and secret (or an access token)")
    try:
        await ShopifySource(shop, credentials).check()
    except OrderSourceError as exc:
        raise HTTPException(status_code=422, detail=f"Shopify didn't accept these details: {exc}") from exc
    await save(session.tenant_id, "shopify", shop, credentials)
    return {"orders": await summary(session.tenant_id)}


class ApiIn(BaseModel):
    """The shop's own order endpoint."""
    url: str = Field(min_length=10, max_length=MAX_ENDPOINT_CHARS)


@router.put("/api")
async def connect_api(body: ApiIn, session: Session = Depends(current_session)) -> dict:
    """Store the endpoint with a new secret; the secret is shown in this answer only."""
    _secrets_ready()
    url = body.url.strip()
    try:
        url = check_url(url, frozenset({host_key(url)}), allow_local=settings.is_dev)
    except FetchError as exc:
        raise HTTPException(status_code=422, detail="Enter an https:// address for your order API") from exc
    secret = API_SECRET_PREFIX + secrets.token_urlsafe(32)
    await save(session.tenant_id, "api", url, {"secret": secret})
    return {"orders": await summary(session.tenant_id), "secret": secret}


class TestIn(BaseModel):
    """An order number to try."""
    order_number: str = Field(min_length=1, max_length=40)


@router.post("/test")
async def test_lookup(body: TestIn, session: Session = Depends(current_session)) -> dict:
    """Look one order up the way the chat will. Shows the owner the status, never the email."""
    await limits.count_order_test(session.tenant_id)
    number = normalize_order_number(body.order_number)
    if number is None:
        raise HTTPException(status_code=422, detail="That doesn't look like an order number")
    source = await source_for(session.tenant_id)
    if source is None:
        raise HTTPException(status_code=409, detail="Connect your orders first")
    try:
        order = await source.find(number)
    except OrderSourceError as exc:
        raise HTTPException(status_code=502, detail=f"We couldn't reach your order system: {exc}") from exc
    if order is None:
        return {"found": False, "orders": await summary(session.tenant_id)}
    async with db.tenant_tx(session.tenant_id) as conn:
        await conn.execute("UPDATE order_connections SET checked_at = now()")
    return {"found": True, "status": order.status, "item_count": order.item_count,
            "orders": await summary(session.tenant_id)}


@router.delete("", status_code=204)
async def disconnect(session: Session = Depends(current_session)) -> None:
    """Remove the connection; the chat stops offering order tracking."""
    async with db.tenant_tx(session.tenant_id) as conn:
        await conn.execute("DELETE FROM order_connections")
    await RedisTokenCache(session.tenant_id).drop()
