"""Requests to the team (team_requests.py, request_settings.py, returns.py, the worker job):
settings from the onboarding app, the webhook secret shown once, retention, return rules."""
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from test_auth import APP, outbox, sign_in  # noqa: F401  (fixture: captured emails)
from test_chat_forms import Webhook
from test_order_sources import MR_20417

import returns
import team_requests
import worker
from blocks import AssetPolicy
from models import ReturnsConfig
from order_sources import order_from_api

NO_IMAGES = AssetPolicy(frozenset())


@pytest.fixture
async def owner(client, outbox):  # noqa: F811
    """A signed-in owner (the client keeps the session cookie)."""
    await sign_in(client, outbox)
    return client


@pytest.fixture
def hook(monkeypatch):
    """A fake webhook endpoint."""
    fake = Webhook()
    monkeypatch.setattr(team_requests, "http_post", fake)
    return fake


# --- Settings from the onboarding app -----------------------------------------------------------

async def test_requests_go_to_the_owner_until_a_team_email_is_set(owner):
    """Defaults: contact + returns on, 90 days, emails to the account's address; the owner can change it."""
    state = (await owner.get("/api/onboarding")).json()["requests"]
    assert state["sends_to"] == "owner@green-leaf.example" and state["retention_days"] == 90
    assert state["leads"] and state["returns"] and state["webhook"] is None
    response = await owner.put("/api/onboarding/requests/settings", headers=APP, json={
        "notify_email": "Team@Green-Leaf.example", "booking_url": "https://cal.example/green", "retention_days": 30,
        "leads": True, "returns": False, "return_window_days": 14})
    assert response.status_code == 200, response.text
    saved = response.json()["requests"]
    assert saved["sends_to"] == "team@green-leaf.example" and not saved["returns"] and saved["retention_days"] == 30


@pytest.mark.parametrize("change", [{"notify_email": "not an email"}, {"booking_url": "http://cal.example"},
                                    {"retention_days": 3}, {"return_window_days": 0}])
async def test_invalid_settings_are_refused(owner, change):
    """Bad email, plain-http booking link, retention under 7 days, window under 1 day: 422."""
    body = {"notify_email": None, "booking_url": None, "retention_days": 90, "leads": True, "returns": True,
            "return_window_days": 30, **change}
    assert (await owner.put("/api/onboarding/requests/settings", headers=APP, json=body)).status_code == 422


async def test_the_webhook_secret_is_shown_once_and_stored_sealed(owner, owner_db, hook):
    """Connect: the secret is in that answer only. Test: a signed webhook.test event is sent."""
    response = await owner.put("/api/onboarding/requests/webhook", headers=APP,
                               json={"url": "https://hooks.green-leaf.example/easychat"})
    assert response.status_code == 200, response.text
    secret = response.json()["secret"]
    assert secret.startswith(team_requests.WEBHOOK_SECRET_PREFIX)
    assert secret not in (await owner.get("/api/onboarding")).text
    assert secret.encode() not in bytes(owner_db.execute("SELECT secret FROM request_webhooks").fetchone()[0])
    test = (await owner.post("/api/onboarding/requests/webhook/test", headers=APP)).json()
    assert test["ok"] and test["webhook"]["checked_at"]
    [(_, headers, body)] = hook.received
    timestamp = int(headers[team_requests.SIGNATURE_HEADER].split(",")[0][2:])
    assert headers[team_requests.SIGNATURE_HEADER] == team_requests.signature(secret, timestamp, body)
    assert json.loads(body)["event"] == "webhook.test"
    assert (await owner.delete("/api/onboarding/requests/webhook", headers=APP)).status_code == 204
    assert (await owner.get("/api/onboarding")).json()["requests"]["webhook"] is None


async def test_a_failing_test_delivery_is_reported(owner, hook):
    """The endpoint answering 500: ok false, with the reason."""
    await owner.put("/api/onboarding/requests/webhook", headers=APP, json={"url": "https://hooks.example/h"})
    hook.status = 500
    answer = (await owner.post("/api/onboarding/requests/webhook/test", headers=APP)).json()
    assert answer == {**answer, "ok": False, "detail": "webhook answered HTTP 500"}


@pytest.mark.parametrize("url", ["http://hooks.example/x", "https://user:pw@hooks.example/x", "nonsense"])
async def test_webhooks_must_be_https(owner, url):
    """Plain http (outside localhost in dev) or credentials in the URL: 422."""
    assert (await owner.put("/api/onboarding/requests/webhook", headers=APP, json={"url": url})).status_code == 422


async def test_settings_need_a_session(client):
    """Signed out: 401."""
    response = await client.put("/api/onboarding/requests/webhook", headers=APP, json={"url": "https://x.example/h"})
    assert response.status_code == 401


# --- Worker -------------------------------------------------------------------------------------

def test_the_worker_knows_the_delivery_job():
    """The job the chat queues has a handler."""
    assert team_requests.DELIVER_JOB in worker.HANDLERS


async def test_delivering_a_deleted_request_does_nothing(owner_db, outbox):  # noqa: F811
    """The visitor deleted their data before the worker ran: nothing is sent."""
    tenant_id = owner_db.execute("SELECT id FROM tenants WHERE slug = 'tenant-a'").fetchone()[0]
    await worker.deliver_request_job({"tenant_id": str(tenant_id), "request_id": str(uuid.uuid4())})
    assert outbox.sent == []


async def test_the_purge_runs_once_per_interval(monkeypatch):
    """The worker purges at start, then not again until the interval has passed."""
    runs = []

    async def purge():
        """Count runs."""
        runs.append(1)
        return 0
    monkeypatch.setattr(team_requests, "purge_expired", purge)
    last = await worker.purge_if_due(-float(worker.PURGE_INTERVAL_SECONDS))
    assert await worker.purge_if_due(last) == last and runs == [1]


# --- Return rules (returns.py) ------------------------------------------------------------------

def delivered_order(days_ago: float):
    """MR-20417, delivered `days_ago` days ago (no item lines)."""
    at = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    return order_from_api({**MR_20417, "status": "delivered", "events": [{"status": "delivered", "at": at}]})


@pytest.mark.parametrize("days_ago, ok", [(0, True), (29.9, True), (30.1, False)])
def test_the_return_window_counts_from_delivery(days_ago, ok):
    """A 30-day window: inside it a return can be asked for, after it not."""
    assert returns.check_returnable(delivered_order(days_ago), ReturnsConfig(window_days=30)).ok is ok


def test_without_item_lines_the_visitor_describes_the_items():
    """A shop that sends no items: a required text box instead of the item list."""
    consent = {"kind": "checkbox", "name": "consent", "label": "ok", "required": True}
    block, offered = returns.return_form(delivered_order(1), ReturnsConfig(), NO_IMAGES, consent, None)
    assert block["fields"][0]["name"] == "items_text" and "privacy_url" not in block
    payload = returns.return_payload(offered, {"items_text": "the blue mug", "reason": "reason_1",
                                               "refund": "refund_2"}, "a@b.example")
    assert payload["items"] == [{"title": "the blue mug", "quantity": 1, "price": None}]
    assert payload["refund_preference"] == "Original payment"


@pytest.mark.parametrize("text", ["I want to return my order", "how can i return this item?",
                                  "I'd like to send it back", "refund my order please"])
def test_return_requests_are_recognised(text):
    """Asking to return something (not asking about the policy)."""
    assert returns.looks_like_return_request(text)
