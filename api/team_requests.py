"""Requests to the shop's team (roadmap step 9): stored with consent, delivered by email + webhook.

A request is what a visitor sends with one of the fixed chat forms (chat_forms.py): a return
request, their contact details for a follow-up, or "talk to a person". Rules:

  * Nothing is stored without consent. The form has a required consent box; the exact wording
    the visitor agreed to is stored with the request (consent_text), with the time.
  * Retention: every request expires after the tenant's retention_days and is deleted by the
    worker (purge_expired). "Delete my data" removes a conversation's requests at once.
  * Delivery runs in the worker (job "deliver_request" with only the request id): an email to
    the team (Reply-To: the visitor) and, if the tenant set one, a webhook POST signed with
    HMAC-SHA256. A failed webhook is retried by the job queue; receivers dedupe by request id.

Webhook signature (for shop developers, also in the README):
    X-Easychat-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "<t>." + raw body)>
Reject a request whose signature doesn't match or whose t is more than 5 minutes old.
"""
import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from psycopg.types.json import Jsonb

import db
import mailer
from config import settings
from fetcher import FetchError, Fetcher, host_key
from jobs import RedisQueue
from models import RequestsConfig, TenantConfig
from replies import FormValue, text_value
from secretbox import open_sealed, seal

log = logging.getLogger("easychat.requests")

KINDS = ("return", "lead", "handoff")
CONSENT_FIELD = "consent"
CONSENT_VALUE = "yes"                       # what the widget sends for a ticked checkbox
DELIVER_JOB = "deliver_request"
WEBHOOK_EVENT = "request.created"
WEBHOOK_TEST_EVENT = "webhook.test"
WEBHOOK_SECRET_PREFIX = "ec_whk_"
SIGNATURE_HEADER = "X-Easychat-Signature"
WEBHOOK_RESPONSE_MAX_BYTES = 10_000
USER_AGENT = "easychat-webhooks/0.1"

KIND_TITLES = {"return": "Return request", "lead": "New enquiry", "handoff": "Someone asked to talk to the team"}


class DeliveryError(RuntimeError):
    """A request couldn't be delivered (the job queue retries it)."""


# --- Consent ------------------------------------------------------------------------------------

def consent_text(company: str, retention_days: int) -> str:
    """The consent box's wording: who stores what, for how long. Stored with each request."""
    return (f"I agree to {company} storing the details above to answer this request. "
            f"They're deleted after {retention_days} days.")


def consent_field(company: str, retention_days: int) -> dict:
    """The required consent checkbox every request form ends with (W-07)."""
    return {"kind": "checkbox", "name": CONSENT_FIELD, "label": consent_text(company, retention_days),
            "required": True}


def consent_given(values: dict[str, FormValue]) -> bool:
    """Did the visitor tick the consent box? Checked on the server: the widget's check is a courtesy."""
    return text_value(values, CONSENT_FIELD) == CONSENT_VALUE


# --- Storage ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Request:
    """One stored request."""
    id: UUID
    kind: str
    payload: dict
    consent_text: str
    created_at: datetime
    expires_at: datetime


async def create(tenant_id: UUID, conversation_id: UUID | None, kind: str, payload: dict, consent: str,
                 retention_days: int) -> UUID:
    """Store a request the visitor consented to; return its id."""
    if kind not in KINDS:
        raise ValueError(f"unknown request kind {kind!r}")
    expires = datetime.now(UTC) + timedelta(days=retention_days)
    async with db.tenant_tx(tenant_id) as conn:
        cur = await conn.execute(
            "INSERT INTO requests (tenant_id, conversation_id, kind, payload, consent_text, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (tenant_id, conversation_id, kind, Jsonb(payload), consent, expires))
        return (await cur.fetchone())["id"]


async def load(tenant_id: UUID, request_id: UUID) -> Request | None:
    """A request of this tenant (RLS), or None (deleted or expired meanwhile)."""
    async with db.tenant_tx(tenant_id) as conn:
        cur = await conn.execute("SELECT id, kind, payload, consent_text, created_at, expires_at FROM requests "
                                 "WHERE id = %s", (request_id,))
        row = await cur.fetchone()
    return Request(**row) if row else None


async def delete_for_conversation(conn, conversation_id: UUID) -> int:
    """Delete a conversation's requests inside the caller's tenant transaction; how many went."""
    cur = await conn.execute("DELETE FROM requests WHERE conversation_id = %s", (conversation_id,))
    return cur.rowcount


async def purge_expired() -> int:
    """Delete every tenant's expired requests (retention); how many went."""
    deleted = await db.purge_expired_requests()
    if deleted:
        log.info("retention: deleted %d expired requests", deleted)
    return deleted


async def enqueue_delivery(queue: RedisQueue, tenant_id: UUID, request_id: UUID) -> None:
    """Hand the request to the worker. The job holds ids only, never the visitor's details."""
    await queue.enqueue(DELIVER_JOB, {"tenant_id": str(tenant_id), "request_id": str(request_id)})


# --- What the team (and the visitor) see --------------------------------------------------------

def _items_line(items: list[dict]) -> str:
    """'Squalane Day Gel ×1 ($26.00), Vitamin C Serum ×2'."""
    parts = []
    for item in items:
        price = f" ({item['price']})" if item.get("price") else ""
        parts.append(f"{item['title']} ×{item.get('quantity', 1)}{price}")
    return ", ".join(parts)


def summary_rows(kind: str, payload: dict, for_team: bool) -> list[tuple[str, str]]:
    """(label, value) pairs describing a request: for the team's email (everything) or for the
    visitor's confirmation card (their own details, as they entered them)."""
    if kind == "return":
        rows = [("Order", f"#{payload['order_number']}"), ("Items", _items_line(payload["items"])),
                ("Reason", payload["reason"]), ("Refund preference", payload["refund_preference"])]
        if payload.get("notes"):
            rows.append(("Notes", payload["notes"]))
        if for_team:
            rows.insert(1, ("Customer email", payload["customer_email"]))
        return rows
    rows = [("Name", payload["name"]), ("Email", payload["email"]), ("Summary", payload["summary"])]
    if kind == "handoff":
        rows.append(("Asked for", "Talk to a person"))
    return rows


def _one_line(text: str, limit: int) -> str:
    """A header-safe single line (no newlines can reach an email header)."""
    return " ".join(text.split())[:limit]


def team_email(company: str, request: Request) -> tuple[str, str, str | None]:
    """(subject, plain-text body, reply-to address) of the email to the team."""
    payload = request.payload
    if request.kind == "return":
        subject = f"Return request: order #{payload['order_number']}"
        reply_to = payload.get("customer_email")
    else:
        subject = f"{KIND_TITLES[request.kind]} from {_one_line(payload['name'], 60)}"
        reply_to = payload.get("email")
    lines = [f"{KIND_TITLES[request.kind]} from the {company} chat assistant.", ""]
    lines += [f"{label}: {value}" for label, value in summary_rows(request.kind, payload, for_team=True)]
    lines += ["", "Reply to this email to answer the customer directly.", "",
              f'Consent: the customer ticked "{request.consent_text}" on '
              f"{request.created_at.astimezone(UTC):%Y-%m-%d %H:%M} UTC.",
              f"Request id: {request.id}. Kept until {request.expires_at.astimezone(UTC):%Y-%m-%d}, then deleted.",
              "No changes were made to any order: the team decides on every request."]
    return _one_line(subject, 150), "\n".join(lines) + "\n", reply_to


def webhook_body(request: Request, event: str = WEBHOOK_EVENT) -> bytes:
    """The JSON a tenant's webhook receives."""
    return json.dumps({
        "event": event, "id": str(request.id), "kind": request.kind,
        "created_at": request.created_at.astimezone(UTC).isoformat(), "data": request.payload,
        "consent": {"text": request.consent_text, "given_at": request.created_at.astimezone(UTC).isoformat()},
    }, separators=(",", ":"), ensure_ascii=False).encode()


def signature(secret: str, timestamp: int, body: bytes) -> str:
    """The X-Easychat-Signature value for one delivery."""
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


# --- Webhook settings ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Webhook:
    """A tenant's webhook with its secret decrypted."""
    url: str
    secret: str
    checked_at: datetime | None


def _webhook_context(tenant_id: UUID) -> str:
    """secretbox context: the secret only decrypts for this tenant and this purpose."""
    return f"request-webhook:{tenant_id}"


def new_webhook_secret() -> str:
    """A fresh signing secret (shown to the owner once)."""
    return WEBHOOK_SECRET_PREFIX + secrets.token_urlsafe(32)


async def save_webhook(tenant_id: UUID, url: str, secret: str) -> None:
    """Store (or replace) the tenant's webhook, secret sealed."""
    sealed = seal({"secret": secret}, _webhook_context(tenant_id))
    async with db.tenant_tx(tenant_id) as conn:
        await conn.execute(
            "INSERT INTO request_webhooks (tenant_id, url, secret) VALUES (%s, %s, %s) ON CONFLICT (tenant_id) "
            "DO UPDATE SET url = EXCLUDED.url, secret = EXCLUDED.secret, created_at = now(), checked_at = NULL",
            (tenant_id, url, sealed))


async def load_webhook(tenant_id: UUID) -> Webhook | None:
    """The tenant's webhook, or None (none set, or its secret can't be read: logged)."""
    async with db.tenant_tx(tenant_id) as conn:
        cur = await conn.execute("SELECT url, secret, checked_at FROM request_webhooks")
        row = await cur.fetchone()
    if row is None:
        return None
    try:
        secret = open_sealed(bytes(row["secret"]), _webhook_context(tenant_id))["secret"]
    except Exception as exc:  # SealError / SecretsUnavailable / a damaged value
        log.error("webhook secret of tenant %s can't be read: %s", tenant_id, type(exc).__name__)
        return None
    return Webhook(row["url"], secret, row["checked_at"])


async def webhook_summary(tenant_id: UUID) -> dict | None:
    """What the onboarding app shows: never the secret."""
    async with db.tenant_tx(tenant_id) as conn:
        cur = await conn.execute("SELECT url, created_at, checked_at FROM request_webhooks")
        row = await cur.fetchone()
    if row is None:
        return None
    return {"url": row["url"], "created_at": row["created_at"].isoformat(),
            "checked_at": row["checked_at"].isoformat() if row["checked_at"] else None}


async def delete_webhook(tenant_id: UUID) -> None:
    """Remove the tenant's webhook."""
    async with db.tenant_tx(tenant_id) as conn:
        await conn.execute("DELETE FROM request_webhooks")


# --- Delivery -----------------------------------------------------------------------------------

async def http_post(url: str, headers: dict[str, str], body: bytes) -> int:
    """One SSRF-safe POST to the webhook's own host (no redirects); its HTTP status. Tests replace this."""
    try:
        async with Fetcher(frozenset({host_key(url)}), allow_local=settings.is_dev) as fetcher:
            status, _ = await fetcher.call("POST", url, headers, body, WEBHOOK_RESPONSE_MAX_BYTES)
            return status
    except FetchError as exc:
        raise DeliveryError(f"webhook unreachable: {exc}") from exc


async def send_webhook(webhook: Webhook, body: bytes) -> None:
    """POST a signed body; DeliveryError unless the receiver answers 2xx."""
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT,
               SIGNATURE_HEADER: signature(webhook.secret, int(time.time()), body)}
    status = await http_post(webhook.url, headers, body)
    if not 200 <= status < 300:
        raise DeliveryError(f"webhook answered HTTP {status}")


async def notify_address(tenant_id: UUID, requests: RequestsConfig) -> str | None:
    """Where the team's emails go: the configured address, else the account owner's."""
    if requests.notify_email:
        return requests.notify_email
    async with db.tenant_tx(tenant_id) as conn:
        cur = await conn.execute("SELECT email FROM users ORDER BY created_at LIMIT 1")
        row = await cur.fetchone()
    return row["email"] if row else None


async def _tenant_config(tenant_id: UUID) -> TenantConfig | None:
    """The tenant's current config (the worker has no request to resolve it from)."""
    async with db.tenant_tx(tenant_id) as conn:
        cur = await conn.execute("SELECT config FROM tenants WHERE id = %s", (tenant_id,))
        row = await cur.fetchone()
    return TenantConfig.model_validate(row["config"]) if row else None


async def _mark(tenant_id: UUID, request_id: UUID, column: str, error: str | None = None) -> None:
    """Record a delivery step (emailed_at / webhook_sent_at), or a webhook error."""
    async with db.tenant_tx(tenant_id) as conn:
        if column == "webhook_error":
            await conn.execute("UPDATE requests SET webhook_error = %s WHERE id = %s", (error[:300], request_id))
        elif column == "emailed_at":
            await conn.execute("UPDATE requests SET emailed_at = now() WHERE id = %s", (request_id,))
        else:
            await conn.execute("UPDATE requests SET webhook_sent_at = now(), webhook_error = NULL WHERE id = %s",
                               (request_id,))


async def deliver(tenant_id: UUID, request_id: UUID) -> None:
    """Worker job: email the team, then POST the webhook. Steps already done are skipped, so a
    retry after a webhook failure doesn't email the team twice. Raises DeliveryError to retry."""
    async with db.tenant_tx(tenant_id) as conn:
        cur = await conn.execute("SELECT emailed_at, webhook_sent_at FROM requests WHERE id = %s", (request_id,))
        progress = await cur.fetchone()
    request = await load(tenant_id, request_id)
    config = await _tenant_config(tenant_id)
    if request is None or progress is None or config is None:
        log.info("request %s is gone (deleted or expired): nothing to deliver", request_id)
        return
    problems = []
    if progress["emailed_at"] is None:
        to = await notify_address(tenant_id, config.requests)
        if to is None:
            log.warning("tenant %s has no address for requests: email skipped", tenant_id)
        else:
            subject, text, reply_to = team_email(config.company_name, request)
            try:
                await mailer.send_email(to, subject, text, reply_to=reply_to)
                await _mark(tenant_id, request_id, "emailed_at")
            except mailer.MailError:
                problems.append("email not sent")
    if progress["webhook_sent_at"] is None and (webhook := await load_webhook(tenant_id)):
        try:
            await send_webhook(webhook, webhook_body(request))
            await _mark(tenant_id, request_id, "webhook_sent_at")
        except DeliveryError as exc:
            await _mark(tenant_id, request_id, "webhook_error", str(exc))
            problems.append(str(exc))
    if problems:
        raise DeliveryError("; ".join(problems))
    log.info("request %s (%s) delivered for tenant %s", request_id, request.kind, tenant_id)
