"""Abuse and cost limits, counted in Redis.

The Origin check stops other websites' browsers, but anyone with curl can forge an Origin.
These limits are what actually bound cost:
  * per client IP: messages per minute
  * per tenant: new conversations per month, LLM tokens per day
  * per conversation: user turns (checked in Postgres, see conversations.py)

If Redis is unreachable we fail closed (503): an outage must not turn into an unmetered bill.
"""
import logging
from datetime import UTC, datetime
from uuid import UUID

import redis.asyncio as aioredis
from fastapi import HTTPException

from config import settings
from models import TenantLimits

log = logging.getLogger("easychat.limits")

client = aioredis.from_url(settings.redis_url, socket_timeout=2, socket_connect_timeout=2)


class LimitExceeded(HTTPException):
    def __init__(self, message: str, retry_after: int):
        super().__init__(status_code=429, detail=message, headers={"Retry-After": str(retry_after)})


def _unavailable(exc: Exception) -> HTTPException:
    log.error("redis unavailable: %s", type(exc).__name__)
    return HTTPException(status_code=503, detail="Service temporarily unavailable")


async def _hit(key: str, window_seconds: int) -> int:
    """Increment a fixed-window counter and return its new value."""
    try:
        async with client.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, window_seconds, nx=True)
            count, _ = await pipe.execute()
        return int(count)
    except aioredis.RedisError as exc:
        raise _unavailable(exc) from exc


def _now() -> datetime:
    return datetime.now(UTC)


async def check_ip(ip: str) -> None:
    minute = _now().strftime("%Y%m%d%H%M")
    if await _hit(f"rl:ip:{ip}:{minute}", 60) > settings.ip_messages_per_minute:
        raise LimitExceeded("You're sending messages quickly — please wait a moment.", retry_after=60)


async def count_new_conversation(tenant_id: UUID, limits: TenantLimits) -> None:
    month = _now().strftime("%Y%m")
    if await _hit(f"rl:conv:{tenant_id}:{month}", 40 * 86400) > limits.monthly_conversations:
        raise LimitExceeded("This assistant has reached its monthly chat limit.", retry_after=86400)


def _budget_key(tenant_id: UUID) -> str:
    return f"budget:tokens:{tenant_id}:{_now().strftime('%Y%m%d')}"


async def check_token_budget(tenant_id: UUID, limits: TenantLimits) -> None:
    try:
        used = int(await client.get(_budget_key(tenant_id)) or 0)
    except aioredis.RedisError as exc:
        raise _unavailable(exc) from exc
    if used >= limits.daily_tokens:
        raise LimitExceeded("This assistant has reached its daily limit. Please try again tomorrow.", retry_after=3600)


async def record_tokens(tenant_id: UUID, tokens: int) -> None:
    """Add spent tokens to today's budget. Best effort: the reply has already been paid for."""
    if tokens <= 0:
        return
    try:
        async with client.pipeline(transaction=True) as pipe:
            pipe.incrby(_budget_key(tenant_id), tokens)
            pipe.expire(_budget_key(tenant_id), 2 * 86400, nx=True)
            await pipe.execute()
    except aioredis.RedisError as exc:
        log.error("could not record %d tokens for tenant %s: %s", tokens, tenant_id, type(exc).__name__)


async def ping() -> None:
    await client.ping()
