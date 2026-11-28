"""Where chat-form requests go (roadmap step 9), managed from the onboarding app's "Requests" step.

  PUT    /api/onboarding/requests/settings      team email, booking link, retention, which forms
  PUT    /api/onboarding/requests/webhook       {url}: answers with a new signing secret, shown once
  POST   /api/onboarding/requests/webhook/test  send a signed test event, report the HTTP result
  DELETE /api/onboarding/requests/webhook       stop sending webhooks

All routes need the owner's session (same as onboarding.py); settings live in the server-only
`requests` part of the tenant's config, the webhook secret sealed in request_webhooks.
"""
import re
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, ValidationError

import db
import limits
import team_requests
from auth import Session, current_session, same_origin
from config import settings
from fetcher import FetchError, check_url, host_key
from models import EMAIL_PATTERN, RequestsConfig, ReturnsConfig, TenantConfig

router = APIRouter(prefix="/api/onboarding/requests", dependencies=[Depends(same_origin)])

TEST_PAYLOAD = {"name": "Test from easychat", "email": "test@example.com",
                "summary": "This is a test delivery. Your endpoint received and verified it."}


async def _config(tenant_id) -> TenantConfig:
    """The tenant's config (RLS: only its own row)."""
    async with db.tenant_tx(tenant_id) as conn:
        cur = await conn.execute("SELECT config FROM tenants WHERE id = %s", (tenant_id,))
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=401, detail="Please sign in")
    return TenantConfig.model_validate(row["config"])


async def summary(tenant_id, config: TenantConfig) -> dict:
    """What the app shows: settings, where emails go, the webhook without its secret."""
    requests = config.requests
    return {
        "notify_email": requests.notify_email,
        "sends_to": await team_requests.notify_address(tenant_id, requests),
        "booking_url": str(requests.booking_url) if requests.booking_url else None,
        "retention_days": requests.retention_days,
        "chat_retention_days": requests.chat_retention_days,
        "leads": requests.leads,
        "returns": requests.returns is not None,
        "return_window_days": (requests.returns or ReturnsConfig()).window_days,
        "webhook": await team_requests.webhook_summary(tenant_id),
    }


class SettingsIn(BaseModel):
    """The owner's choices (validated again by RequestsConfig)."""
    notify_email: str | None = Field(default=None, max_length=254)     # None or "": the account's email
    booking_url: str | None = Field(default=None, max_length=500)
    retention_days: int = Field(ge=7, le=365)
    chat_retention_days: int = Field(default=90, ge=7, le=730)
    leads: bool
    returns: bool
    return_window_days: int = Field(ge=1, le=365)


@router.put("/settings")
async def save_settings(body: SettingsIn, session: Session = Depends(current_session)) -> dict:
    """Save where requests go and which forms the assistant offers."""
    config = await _config(session.tenant_id)
    email = (body.notify_email or "").strip().lower() or None
    if email and not re.fullmatch(EMAIL_PATTERN, email):
        raise HTTPException(status_code=422, detail="Enter a valid email address for your team")
    returns = None
    if body.returns:
        current = config.requests.returns or ReturnsConfig()
        returns = current.model_copy(update={"window_days": body.return_window_days})
    try:
        requests = RequestsConfig(notify_email=email, booking_url=(body.booking_url or "").strip() or None,
                                  retention_days=body.retention_days,
                                  chat_retention_days=body.chat_retention_days, leads=body.leads, returns=returns)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="The booking link must be an https:// address") from exc
    stored = config.model_dump(mode="json")
    stored["requests"] = requests.model_dump(mode="json")
    async with db.tenant_tx(session.tenant_id) as conn:
        await conn.execute("UPDATE tenants SET config = %s WHERE id = %s", (Jsonb(stored), session.tenant_id))
    return {"requests": await summary(session.tenant_id, TenantConfig.model_validate(stored))}


class WebhookIn(BaseModel):
    """The endpoint that receives requests."""
    url: str = Field(min_length=10, max_length=500)


@router.put("/webhook")
async def connect_webhook(body: WebhookIn, session: Session = Depends(current_session)) -> dict:
    """Store the endpoint with a new signing secret; the secret is in this answer only."""
    if settings.secrets_key is None:
        raise HTTPException(status_code=503, detail="Webhooks aren't set up on this server (SECRETS_KEY).")
    url = body.url.strip()
    try:
        url = check_url(url, frozenset({host_key(url)}), allow_local=settings.is_dev)
    except FetchError as exc:
        raise HTTPException(status_code=422, detail="Enter an https:// address for your webhook") from exc
    secret = team_requests.new_webhook_secret()
    await team_requests.save_webhook(session.tenant_id, url, secret)
    return {"webhook": await team_requests.webhook_summary(session.tenant_id), "secret": secret}


@router.post("/webhook/test")
async def test_webhook(session: Session = Depends(current_session)) -> dict:
    """POST a signed `webhook.test` event; report whether the endpoint answered 2xx."""
    await limits.count_webhook_test(session.tenant_id)
    webhook = await team_requests.load_webhook(session.tenant_id)
    if webhook is None:
        raise HTTPException(status_code=409, detail="Add a webhook address first")
    now = datetime.now(UTC)
    sample = team_requests.Request(uuid.uuid4(), "lead", TEST_PAYLOAD, "Test event: no consent needed.", now, now)
    try:
        await team_requests.send_webhook(webhook, team_requests.webhook_body(sample, team_requests.WEBHOOK_TEST_EVENT))
    except team_requests.DeliveryError as exc:
        return {"ok": False, "detail": str(exc), "webhook": await team_requests.webhook_summary(session.tenant_id)}
    async with db.tenant_tx(session.tenant_id) as conn:
        await conn.execute("UPDATE request_webhooks SET checked_at = now()")
    return {"ok": True, "webhook": await team_requests.webhook_summary(session.tenant_id)}


@router.delete("/webhook", status_code=204)
async def disconnect_webhook(session: Session = Depends(current_session)) -> None:
    """Stop sending requests to the webhook (emails continue)."""
    await team_requests.delete_webhook(session.tenant_id)
