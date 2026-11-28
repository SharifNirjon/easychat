"""Abuse and cost limits, counted in Redis.

The Origin check stops other websites' browsers, but anyone with curl can forge an Origin.
These limits are what actually bound cost:
  * per client IP: messages per minute
  * per tenant: new conversations per month, LLM tokens per day, crawls per day
  * per conversation: user turns (checked in Postgres, see conversations.py)
  * self-serve sign-up (step 7): sign-in emails per address and per IP, new accounts per IP
    and per day platform-wide (SIGNUPS_PER_DAY, which is also the kill switch)
  * order tracking (step 8): lookups per chat and per client, code emails per order, owner tests
  * chat forms (step 9): requests per chat, client and tenant (daily); transcript codes per chat,
    client and recipient address

If Redis is unreachable we fail closed (503): an outage must not turn into an unmetered bill.
"""
import logging
from datetime import UTC, datetime
from uuid import UUID

import redis.asyncio as aioredis
from fastapi import HTTPException, Request

from config import settings
from models import TenantLimits

log = logging.getLogger("easychat.limits")

client = aioredis.from_url(settings.redis_url, socket_timeout=2, socket_connect_timeout=2)

SIGNIN_EMAILS_PER_ADDRESS_HOUR = 3     # sign-in links one inbox can be sent per hour
SIGNIN_REQUESTS_PER_IP_HOUR = 10       # sign-in requests one client can make per hour
SIGNUPS_PER_IP_DAY = 3                 # new accounts one client can create per day
HOUR, DAY = 3600, 86400


class LimitExceeded(HTTPException):
    """HTTP 429 with a visitor-safe message and a Retry-After header."""
    def __init__(self, message: str, retry_after: int):
        """Build the 429 with its message and Retry-After seconds."""
        super().__init__(status_code=429, detail=message, headers={"Retry-After": str(retry_after)})


def _unavailable(exc: Exception) -> HTTPException:
    """Log that Redis is down and return the 503 to raise (fail closed)."""
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
    """Current UTC time (one place, so tests could freeze it)."""
    return datetime.now(UTC)


def _today() -> str:
    """The current UTC day as YYYYMMDD (for daily counters)."""
    return _now().strftime("%Y%m%d")


def client_ip(request: Request) -> str:
    """The visitor's IP for rate limiting.

    Each trusted proxy appends the address it received the request from to X-Forwarded-For,
    so the real client is the Nth entry from the right; anything further left is client-supplied
    and may be forged. With no trusted proxies, use the socket peer.
    """
    hops = settings.trusted_proxy_hops
    if hops > 0:
        forwarded = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
        if len(forwarded) >= hops:
            return forwarded[-hops]
    return request.client.host if request.client else "unknown"


async def check_ip(ip: str) -> None:
    """Count this message against the visitor IP's per-minute limit; 429 when over."""
    minute = _now().strftime("%Y%m%d%H%M")
    if await _hit(f"rl:ip:{ip}:{minute}", 60) > settings.ip_messages_per_minute:
        raise LimitExceeded("You're sending messages quickly — please wait a moment.", retry_after=60)


async def count_new_conversation(tenant_id: UUID, limits: TenantLimits) -> None:
    """Count a new conversation against the tenant's monthly cap; 429 when over."""
    month = _now().strftime("%Y%m")
    if await _hit(f"rl:conv:{tenant_id}:{month}", 40 * 86400) > limits.monthly_conversations:
        raise LimitExceeded("This assistant has reached its monthly chat limit.", retry_after=86400)


async def check_signin(email: str, ip: str) -> None:
    """Count a sign-in email request against the address's and the client's hourly limits."""
    hour = _now().strftime("%Y%m%d%H")
    if await _hit(f"rl:signin:ip:{ip}:{hour}", HOUR) > SIGNIN_REQUESTS_PER_IP_HOUR:
        raise LimitExceeded("Too many sign-in requests. Please try again later.", retry_after=HOUR)
    if await _hit(f"rl:signin:email:{email}:{hour}", HOUR) > SIGNIN_EMAILS_PER_ADDRESS_HOUR:
        raise LimitExceeded("We've already sent several links to this address. Check your inbox, "
                            "or try again in an hour.", retry_after=HOUR)


async def count_signup(ip: str) -> None:
    """Count a new account against the client's daily limit and the platform's daily cap."""
    day = _today()
    if await _hit(f"rl:signup:ip:{ip}:{day}", DAY) > SIGNUPS_PER_IP_DAY:
        raise LimitExceeded("Too many new accounts from this network today. Please try again tomorrow.",
                            retry_after=DAY)
    if await _hit(f"rl:signup:all:{day}", DAY) > settings.signups_per_day:
        raise LimitExceeded("Sign-ups are paused for today. Please try again tomorrow.", retry_after=DAY)


def _crawl_key(tenant_id: UUID) -> str:
    """Redis key for the number of crawls a tenant started today."""
    return f"rl:crawl:{tenant_id}:{_today()}"


async def count_crawl(tenant_id: UUID, limits: TenantLimits) -> None:
    """Count a crawl against the tenant's daily limit; 429 when over."""
    if await _hit(_crawl_key(tenant_id), DAY) > limits.daily_crawls:
        raise LimitExceeded(f"You can refresh your website {limits.daily_crawls} times a day. "
                            "Please try again tomorrow.", retry_after=DAY)


async def crawls_left(tenant_id: UUID, limits: TenantLimits) -> int:
    """How many more crawls the tenant may start today."""
    try:
        used = int(await client.get(_crawl_key(tenant_id)) or 0)
    except aioredis.RedisError as exc:
        raise _unavailable(exc) from exc
    return max(0, limits.daily_crawls - used)


# --- Order tracking (step 8): one-time codes are cheap to request, so every path is capped ---------

ORDER_LOOKUPS_PER_CONVERSATION_HOUR = 5   # "find my order" forms one chat may send per hour
ORDER_LOOKUPS_PER_IP_HOUR = 10            # ... and one client, across chats
ORDER_CODES_PER_ORDER_HOUR = 3            # code emails one order's customer can receive per hour
ORDER_TESTS_PER_TENANT_HOUR = 30          # owner's test lookups from the onboarding app


async def check_order_lookup(tenant_id: UUID, conversation_id: UUID, ip: str) -> None:
    """Count an order lookup against the chat's and the client's hourly limits; 429 when over."""
    hour = _now().strftime("%Y%m%d%H")
    if await _hit(f"rl:order:conv:{conversation_id}:{hour}", HOUR) > ORDER_LOOKUPS_PER_CONVERSATION_HOUR or \
            await _hit(f"rl:order:ip:{tenant_id}:{ip}:{hour}", HOUR) > ORDER_LOOKUPS_PER_IP_HOUR:
        raise LimitExceeded("You've tried several order lookups. Please wait an hour, or ask our team.",
                            retry_after=HOUR)


async def allow_order_code(tenant_id: UUID, order_key: str) -> bool:
    """May one more code be emailed for this order this hour? (`order_key`: a hash, not the number.)"""
    hour = _now().strftime("%Y%m%d%H")
    return await _hit(f"rl:order:code:{tenant_id}:{order_key}:{hour}", HOUR) <= ORDER_CODES_PER_ORDER_HOUR


async def count_order_test(tenant_id: UUID) -> None:
    """Count an owner's test lookup; 429 when over."""
    hour = _now().strftime("%Y%m%d%H")
    if await _hit(f"rl:order:test:{tenant_id}:{hour}", HOUR) > ORDER_TESTS_PER_TENANT_HOUR:
        raise LimitExceeded("Too many test lookups. Please try again in an hour.", retry_after=HOUR)


# --- Chat forms (step 9): requests to the team, transcript emails -------------------------------

REQUESTS_PER_CONVERSATION_HOUR = 3        # returns / contact requests one chat may send per hour
REQUESTS_PER_IP_HOUR = 5                  # ... and one client, across chats
TRANSCRIPT_CODES_PER_CONVERSATION_HOUR = 3  # "email me the transcript" codes one chat may ask for
TRANSCRIPT_CODES_PER_IP_HOUR = 6
TRANSCRIPT_CODES_PER_ADDRESS_HOUR = 3     # codes one inbox can be sent per hour (it isn't the sender's yet)


async def check_request(tenant_id: UUID, conversation_id: UUID, ip: str, limits: TenantLimits) -> None:
    """Count a request against the chat's, the client's and the tenant's (daily) limits; 429 when over."""
    hour = _now().strftime("%Y%m%d%H")
    if await _hit(f"rl:req:conv:{conversation_id}:{hour}", HOUR) > REQUESTS_PER_CONVERSATION_HOUR or \
            await _hit(f"rl:req:ip:{tenant_id}:{ip}:{hour}", HOUR) > REQUESTS_PER_IP_HOUR:
        raise LimitExceeded("You've sent several requests already. Please wait an hour, or contact the team "
                            "directly.", retry_after=HOUR)
    if await _hit(f"rl:req:tenant:{tenant_id}:{_today()}", DAY) > limits.daily_requests:
        raise LimitExceeded("The team can't take more requests through the chat today. Please try again "
                            "tomorrow.", retry_after=DAY)


async def check_transcript_code(tenant_id: UUID, conversation_id: UUID, ip: str, address_key: str) -> None:
    """Count a transcript code email (`address_key`: a hash of the address, not the address)."""
    hour = _now().strftime("%Y%m%d%H")
    if await _hit(f"rl:transcript:conv:{conversation_id}:{hour}", HOUR) > TRANSCRIPT_CODES_PER_CONVERSATION_HOUR or \
            await _hit(f"rl:transcript:ip:{tenant_id}:{ip}:{hour}", HOUR) > TRANSCRIPT_CODES_PER_IP_HOUR or \
            await _hit(f"rl:transcript:to:{address_key}:{hour}", HOUR) > TRANSCRIPT_CODES_PER_ADDRESS_HOUR:
        raise LimitExceeded("We've sent several codes already. Please try again in an hour.", retry_after=HOUR)


WEBHOOK_TESTS_PER_TENANT_HOUR = 20      # owner's test deliveries from the onboarding app


async def count_webhook_test(tenant_id: UUID) -> None:
    """Count an owner's test webhook delivery; 429 when over."""
    hour = _now().strftime("%Y%m%d%H")
    if await _hit(f"rl:webhook:test:{tenant_id}:{hour}", HOUR) > WEBHOOK_TESTS_PER_TENANT_HOUR:
        raise LimitExceeded("Too many test deliveries. Please try again in an hour.", retry_after=HOUR)


INVITES_PER_TENANT_DAY = 20              # team invites one account can send per day (step 10)
EXPORTS_PER_TENANT_HOUR = 30              # lead CSV exports per hour


async def count_invite(tenant_id: UUID) -> None:
    """Count a team invite email; 429 when over."""
    if await _hit(f"rl:invite:{tenant_id}:{_today()}", DAY) > INVITES_PER_TENANT_DAY:
        raise LimitExceeded("Too many invites today. Please try again tomorrow.", retry_after=DAY)


async def count_export(tenant_id: UUID) -> None:
    """Count a lead export; 429 when over."""
    hour = _now().strftime("%Y%m%d%H")
    if await _hit(f"rl:export:{tenant_id}:{hour}", HOUR) > EXPORTS_PER_TENANT_HOUR:
        raise LimitExceeded("Too many exports. Please try again in an hour.", retry_after=HOUR)


def _budget_key(tenant_id: UUID) -> str:
    """Redis key for a tenant's token spend today (UTC day)."""
    return f"budget:tokens:{tenant_id}:{_now().strftime('%Y%m%d')}"


async def check_token_budget(tenant_id: UUID, limits: TenantLimits) -> None:
    """Refuse (429) before calling the model if the tenant's daily tokens are used up."""
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
    """Prove Redis is reachable (used by /ready)."""
    await client.ping()
