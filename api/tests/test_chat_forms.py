"""The fixed chat forms (chat_forms.py via /chat): roadmap step 9's "Done when":
requests reach the shop/team by email + webhook; nothing is saved without consent."""
import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from conftest import KEY_A, KEY_B, ORIGIN_A, ORIGIN_B
from test_auth import outbox  # noqa: F401  (fixture: captured emails)
from test_chat_guards import tokens
from test_order_sources import CUSTOMER, MR_20417, shop  # noqa: F401  (fixture: fake shop)
from test_order_tracking import blocks, chat, code_from, lookup, orders_on  # noqa: F401  (fixture)

import chat_forms
import jobs
import limits
import team_requests
from chat_forms import (BAD_CONTACT_TEXT, BAD_RETURN_TEXT, DELETED_TEXT, EXPIRED_TEXT, NO_CONSENT_TEXT,
                        NOT_DELETED_TEXT, RETURN_VERIFY_TEXT, TRANSCRIPT_ASK_TEXT, TRANSCRIPT_WRONG_TEXT, intent_of)

TEAM = "team@shop-a.example"
VISITOR = "rafiq@meghnabev.example"
DRAFT = "I'd like a quote for 200 printed mugs by March."
CONSENT = {"consent": "yes"}


# --- Fixtures -----------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def draft(monkeypatch):
    """The summary draft comes from a stub, never the real model."""
    calls = []

    async def fake_complete(messages, usage, max_tokens):
        """Record the prompt; answer with a fixed draft."""
        calls.append(messages)
        usage.tokens_in, usage.tokens_out = 50, 10
        return DRAFT
    monkeypatch.setattr(chat_forms, "complete", fake_complete)
    return calls


@pytest.fixture
def team(owner_db):
    """Tenant A takes requests at TEAM (it has no owner account to fall back to); reset afterwards."""
    owner_db.execute("UPDATE tenants SET config = jsonb_set(config, '{requests}', %s::jsonb) WHERE slug = 'tenant-a'",
                     (json.dumps({"notify_email": TEAM, "booking_url": "https://cal.example/shop-a"}),))
    yield
    owner_db.execute("UPDATE tenants SET config = config - 'requests' WHERE slug = 'tenant-a'")


class Webhook:
    """Stands in for team_requests.http_post: records deliveries, answers `status`."""

    def __init__(self):
        """Nothing received; answers 200."""
        self.received: list[tuple[str, dict, bytes]] = []
        self.status = 200

    async def __call__(self, url, headers, body):
        """Record one POST."""
        self.received.append((url, headers, body))
        return self.status


@pytest.fixture
async def webhook(monkeypatch, owner_db):
    """Tenant A has a webhook (secret known to the test) answered by a fake endpoint."""
    fake = Webhook()
    monkeypatch.setattr(team_requests, "http_post", fake)
    tenant_id = owner_db.execute("SELECT id FROM tenants WHERE slug = 'tenant-a'").fetchone()[0]
    await team_requests.save_webhook(tenant_id, "https://hooks.shop-a.example/easychat", "ec_whk_test-secret")
    return fake


async def queued_jobs() -> list[dict]:
    """The jobs waiting in the queue (oldest first)."""
    return [json.loads(raw) for raw in reversed(await limits.client.lrange(jobs.QUEUE_KEY, 0, -1))]


async def run_deliveries(tenant_id) -> None:
    """Do what the worker does for every queued delivery job."""
    for job in await queued_jobs():
        await team_requests.deliver(tenant_id, job["payload"]["request_id"])


def request_rows(owner_db) -> list[tuple]:
    """(kind, payload, consent_text, expires_at) of every stored request."""
    return owner_db.execute("SELECT kind, payload, consent_text, expires_at FROM requests ORDER BY created_at").fetchall()


def form_of(events, name):
    """The form block submitting `name` in a reply."""
    return next(b for b in blocks(events) if b["type"] == "form" and b.get("submit") == name)


async def lead_offered(client, message="Can someone contact me about a bulk order of mugs?"):
    """A conversation where the contact form was offered; returns (events, conversation id)."""
    _, cid = await chat(client, "We run a cafe chain and want branded mugs.")
    events, _ = await chat(client, message, cid)
    return events, cid


async def send_lead(client, cid, **values):
    """Submit the contact form."""
    fields = {"name": "Rafiq Hasan", "email": VISITOR, "summary": DRAFT, **CONSENT, **values}
    return await chat(client, conversation_id=cid, form=("lead", {k: v for k, v in fields.items() if v is not None}))


# --- Recognising requests -----------------------------------------------------------------------

@pytest.mark.parametrize("text, intent", [
    ("Please delete my data", "delete_data"), ("can you erase this chat and my details?", "delete_data"),
    ("Email me a transcript of this chat", "transcript_email"), ("send me a copy of this conversation", "transcript_email"),
    ("Can I talk to a person?", "handoff"), ("I want to speak with someone from your team", "handoff"),
    ("connect me with a human agent", "handoff"),
    ("Can someone contact me?", "lead"), ("I'd like to get a quote for 200 mugs", "lead"),
    ("I want to return my order", "return_request"), ("How do I return an item?", "return_request"),
    ("I'd like to start a return", "return_request"),
])
def test_requests_are_recognised(text, intent):
    """What the visitor asks for, in ordinary words."""
    assert intent_of(text) == intent


@pytest.mark.parametrize("text", ["Are you human?", "What's your return policy?", "Do you ship to Sylhet?",
                                  "How do I contact you?", "Can I return a gift without a receipt?",
                                  "Where is my order?", "What data do you keep about me?"])
def test_ordinary_questions_are_not_requests(text):
    """Questions about policies (and 'are you human?') still go to the model."""
    assert intent_of(text) is None


# --- Contact form: nothing without consent, then email + webhook --------------------------------

async def test_contact_request_gets_the_form_with_a_draft(client, team, fake_llm, draft):
    """Code answers (not the chat model): name, email, the drafted summary, a required consent box
    naming the company and the retention period, and the privacy link."""
    events, _ = await lead_offered(client)
    form = form_of(events, "lead")
    fields = {f["name"]: f for f in form["fields"]}
    assert fields["summary"]["value"] == DRAFT
    assert fields["consent"]["required"] and "tenant-a Inc" in fields["consent"]["label"]
    assert "90 days" in fields["consent"]["label"]
    assert form["privacy_url"] == "https://example.com/privacy"
    assert len(fake_llm.calls) == 1                     # only the first, ordinary message
    assert "<conversation>" in draft[0][1]["content"] and "branded mugs" in draft[0][1]["content"]


async def test_without_consent_nothing_is_saved_or_sent(client, team, owner_db, outbox, webhook):  # noqa: F811
    """Unticked consent: no request row, no job, no email, no webhook; the form comes back filled in."""
    _, cid = await lead_offered(client)
    events, _ = await send_lead(client, cid, consent=None)
    assert tokens(events) == NO_CONSENT_TEXT
    again = {f["name"]: f.get("value") for f in form_of(events, "lead")["fields"]}
    assert again["email"] == VISITOR
    assert request_rows(owner_db) == [] and await queued_jobs() == []
    assert outbox.sent == [] and webhook.received == []
    stored = " ".join(r[0] for r in owner_db.execute("SELECT content FROM messages"))
    assert VISITOR not in stored and "Rafiq" not in stored


async def test_consented_request_is_stored_and_delivered_by_email_and_webhook(client, team, owner_db, outbox,
                                                                               webhook):  # noqa: F811
    """With consent: stored with the consent wording and an expiry, queued by id only, emailed to
    the team (Reply-To: the visitor) and POSTed to the webhook with a valid signature."""
    _, cid = await lead_offered(client)
    events, _ = await send_lead(client, cid)
    card = next(b for b in blocks(events) if b["type"] == "summary")
    assert card["title"] == "Sent to the tenant-a Inc team"
    assert {r["label"]: r["value"] for r in card["rows"]}["Email"] == VISITOR

    [(kind, payload, consent, expires)] = request_rows(owner_db)
    assert kind == "lead" and payload == {"name": "Rafiq Hasan", "email": VISITOR, "summary": DRAFT}
    assert consent.startswith("I agree to tenant-a Inc storing")
    assert timedelta(days=89) < expires - datetime.now(UTC) <= timedelta(days=90)
    [job] = await queued_jobs()
    assert job["name"] == "deliver_request" and VISITOR not in json.dumps(job)

    tenant_id = owner_db.execute("SELECT id FROM tenants WHERE slug = 'tenant-a'").fetchone()[0]
    await run_deliveries(tenant_id)
    [(to, subject, text)] = outbox.sent
    assert to == TEAM and outbox.reply_to == [VISITOR] and "Rafiq Hasan" in subject
    assert DRAFT in text and "Consent: the customer ticked" in text
    [(url, headers, body)] = webhook.received
    assert url == "https://hooks.shop-a.example/easychat"
    timestamp = int(re.match(r"t=(\d+),", headers["X-Easychat-Signature"]).group(1))
    assert headers["X-Easychat-Signature"] == team_requests.signature("ec_whk_test-secret", timestamp, body)
    sent = json.loads(body)
    assert sent["event"] == "request.created" and sent["kind"] == "lead" and sent["data"]["email"] == VISITOR
    sent_at = owner_db.execute("SELECT emailed_at, webhook_sent_at FROM requests").fetchone()
    assert all(sent_at)


async def test_a_failed_webhook_is_retried_without_emailing_twice(client, team, owner_db, outbox, webhook):  # noqa: F811
    """The job fails (so the queue retries it), the error is recorded; the retry sends only the webhook."""
    _, cid = await lead_offered(client)
    await send_lead(client, cid)
    tenant_id = owner_db.execute("SELECT id FROM tenants WHERE slug = 'tenant-a'").fetchone()[0]
    webhook.status = 500
    with pytest.raises(team_requests.DeliveryError):
        await run_deliveries(tenant_id)
    assert owner_db.execute("SELECT webhook_error FROM requests").fetchone()[0] == "webhook answered HTTP 500"
    webhook.status = 204
    await run_deliveries(tenant_id)
    assert len(outbox.sent) == 1 and len(webhook.received) == 2
    assert owner_db.execute("SELECT webhook_error FROM requests").fetchone()[0] is None


async def test_the_conversation_keeps_a_note_not_the_details(client, team, owner_db):
    """Stored messages: 'Sent my contact details' and a note; never the name, email or summary."""
    _, cid = await lead_offered(client)
    await send_lead(client, cid)
    stored = [r[0] for r in owner_db.execute("SELECT content FROM messages WHERE conversation_id = %s ORDER BY id",
                                             (cid,))]
    assert stored[-2:] == ["Sent my contact details", "Sent the visitor's details to the tenant-a Inc team."]
    assert owner_db.execute("SELECT count(*) FROM messages WHERE blocks IS NOT NULL").fetchone()[0] == 0


async def test_a_form_that_was_not_offered_is_refused(client, team, owner_db):
    """A crafted submission in a chat that was never shown the form stores nothing."""
    _, cid = await chat(client, "hello")
    events, _ = await send_lead(client, cid)
    assert tokens(events) == EXPIRED_TEXT and request_rows(owner_db) == []


async def test_the_form_works_once(client, team, owner_db):
    """After sending, the same form can't be replayed."""
    _, cid = await lead_offered(client)
    await send_lead(client, cid)
    events, _ = await send_lead(client, cid)
    assert tokens(events) == EXPIRED_TEXT and len(request_rows(owner_db)) == 1


@pytest.mark.parametrize("values", [{"email": "not-an-email"}, {"name": "   "}, {"summary": ""}])
async def test_invalid_contact_details_are_refused(client, team, owner_db, values):
    """A bad email, a blank name or summary: nothing stored."""
    _, cid = await lead_offered(client)
    events, _ = await send_lead(client, cid, **values)
    assert tokens(events) == BAD_CONTACT_TEXT and request_rows(owner_db) == []


async def test_requests_are_rate_limited_per_conversation(client, team, owner_db):
    """A chat can send REQUESTS_PER_CONVERSATION_HOUR requests an hour."""
    _, cid = await lead_offered(client)
    for _ in range(limits.REQUESTS_PER_CONVERSATION_HOUR):
        await chat(client, "Can someone contact me?", cid)
        await send_lead(client, cid)
    await chat(client, "Can someone contact me?", cid)
    events, _ = await send_lead(client, cid)
    assert "several requests" in tokens(events)
    assert len(request_rows(owner_db)) == limits.REQUESTS_PER_CONVERSATION_HOUR


async def test_a_draft_that_adds_contact_details_is_replaced(client, team, monkeypatch):
    """The model may not slip links or emails into the summary: the visitor's own words are used."""
    async def sneaky(messages, usage, max_tokens):
        """A draft with a planted link and address."""
        return "Visit https://evil.example and write to boss@evil.example"
    monkeypatch.setattr(chat_forms, "complete", sneaky)
    events, _ = await lead_offered(client)
    summary = {f["name"]: f.get("value") for f in form_of(events, "lead")["fields"]}["summary"]
    assert summary == "We run a cafe chain and want branded mugs."


async def test_talk_to_a_person_offers_the_team_and_a_call(client, team, fake_llm):
    """W-10: honest about being an AI, the contact form as a handoff, and 'Book a call'."""
    events, _ = await chat(client, "Can I talk to a real person please?")
    assert "AI assistant" in tokens(events) and not fake_llm.calls
    assert form_of(events, "lead")["title"] == "Talk to the tenant-a Inc team"
    chips = next(b for b in blocks(events) if b["type"] == "chips")["items"]
    assert chips[0]["action"] == {"kind": "link", "url": "https://cal.example/shop-a"}


async def test_handoff_requests_are_marked_as_such(client, team, owner_db):
    """A handoff is stored as kind 'handoff'."""
    _, cid = await chat(client, "I want to speak to someone from your team")
    await send_lead(client, cid)
    assert request_rows(owner_db)[0][0] == "handoff"


async def test_without_a_team_address_the_model_answers(client, fake_llm):
    """No notify email and no owner account: no form (there'd be nowhere to send it)."""
    await chat(client, "Can someone contact me?")
    assert fake_llm.calls


# --- Return requests (design 7) -----------------------------------------------------------------

DELIVERED = "MR-20388"


@pytest.fixture
def delivered(shop):  # noqa: F811
    """The fake shop also has a delivered order with two items (design 7)."""
    def add(days_ago: int = 6, number: str = DELIVERED):
        """Add `number`, delivered `days_ago` days ago."""
        at = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
        shop.api_orders[number] = {**MR_20417, "order_number": number, "status": "delivered",
                                   "events": [{"status": "delivered", "at": at}],
                                   "items": [{"title": "Squalane Day Gel", "quantity": 1, "price": "$26.00"},
                                             {"title": "Brightening Vitamin C Serum", "quantity": 2, "price": "$12.00"}]}
    add()
    return add


async def verified(client, outbox, number=DELIVERED):  # noqa: F811
    """A conversation that verified `number` (step 8's flow)."""
    _, cid = await chat(client, f"Where's my order #{number}?")
    await lookup(client, cid, number)
    events, _ = await chat(client, conversation_id=cid, form=("order_code", {"code": code_from(outbox)}))
    return events, cid


async def test_a_return_needs_the_order_verified_first(client, team, orders_on, delivered, fake_llm):
    """Not verified: the order lookup form (number pre-filled), not the model."""
    events, _ = await chat(client, f"I want to return order #{DELIVERED}")
    assert tokens(events) == RETURN_VERIFY_TEXT and not fake_llm.calls
    assert blocks(events)[0]["fields"][0]["value"] == DELIVERED


async def test_a_delivered_order_offers_start_a_return(client, team, orders_on, delivered, outbox):  # noqa: F811
    """The verified order card of a returnable order ends with a 'Start a return' chip."""
    events, _ = await verified(client, outbox)
    chips = [b for b in blocks(events) if b["type"] == "chips"]
    assert chips and chips[0]["items"][0]["label"] == "Start a return"


async def test_the_return_form_lists_the_orders_items(client, team, orders_on, delivered, outbox):  # noqa: F811
    """Design 7: items with price and delivery date, reason chips, refund preferences without
    amounts, and the consent box."""
    _, cid = await verified(client, outbox)
    events, _ = await chat(client, "I'd like to return something from this order", cid)
    assert tokens(events).startswith(f"Sure. Which items from order #{DELIVERED} are you returning?")
    fields = {f["name"]: f for f in form_of(events, "return_request")["fields"]}
    assert [o["label"] for o in fields["items"]["options"]] == ["Squalane Day Gel", "Brightening Vitamin C Serum"]
    assert fields["items"]["options"][1]["detail"].startswith("$12.00 · ×2 · delivered")
    assert fields["reason"]["style"] == "chips" and fields["consent"]["required"]
    assert not any("$" in o.get("detail", "") for o in fields["refund"]["options"])


async def submit_return(client, cid, **values):
    """Submit the return form (first item, first reason, first refund option, consent)."""
    fields = {"items": ["item_1"], "reason": "reason_2", "refund": "refund_1", "notes": "Box was crushed", **CONSENT,
              **values}
    return await chat(client, conversation_id=cid, form=("return_request", {k: v for k, v in fields.items() if v is not None}))


async def test_a_return_request_reaches_the_team(client, team, orders_on, delivered, outbox, owner_db):  # noqa: F811
    """Stored with the verified customer's email (for the team only), confirmed with a card that
    says nothing is approved yet, emailed with Reply-To: the customer."""
    _, cid = await verified(client, outbox)
    await chat(client, "I want to return an item", cid)
    events, _ = await submit_return(client, cid)
    assert "with the tenant-a Inc team" in tokens(events) and "f•••@example.com" in tokens(events)
    card = next(b for b in blocks(events) if b["type"] == "summary")
    assert "nothing is approved" in card["footer"]
    [(kind, payload, _, _)] = request_rows(owner_db)
    assert kind == "return" and payload["customer_email"] == CUSTOMER and payload["reason"] == "Arrived damaged"
    assert payload["items"] == [{"title": "Squalane Day Gel", "quantity": 1, "price": "$26.00"}]
    tenant_id = owner_db.execute("SELECT id FROM tenants WHERE slug = 'tenant-a'").fetchone()[0]
    await run_deliveries(tenant_id)
    to, subject, text = outbox.sent[-1]
    assert to == TEAM and subject == f"Return request: order #{DELIVERED}" and outbox.reply_to[-1] == CUSTOMER
    assert "No changes were made to any order" in text


@pytest.mark.parametrize("values", [{"items": ["item_9"]}, {"items": []}, {"reason": "reason_99"},
                                    {"refund": "something else"}, {"items": "item_1; DROP TABLE"}])
async def test_only_offered_choices_are_accepted(client, team, orders_on, delivered, outbox, owner_db, values):  # noqa: F811
    """Items, reasons and refund options that weren't in the form are refused; nothing stored."""
    _, cid = await verified(client, outbox)
    await chat(client, "I want to return an item", cid)
    events, _ = await submit_return(client, cid, **values)
    assert tokens(events) == BAD_RETURN_TEXT and request_rows(owner_db) == []


async def test_a_return_without_consent_saves_nothing(client, team, orders_on, delivered, outbox, owner_db):  # noqa: F811
    """No consent, no request."""
    _, cid = await verified(client, outbox)
    await chat(client, "I want to return an item", cid)
    events, _ = await submit_return(client, cid, consent=None)
    assert tokens(events) == NO_CONSENT_TEXT and request_rows(owner_db) == []


async def test_a_return_needs_the_verification_to_still_hold(client, team, orders_on, delivered, outbox,
                                                             owner_db):  # noqa: F811
    """Access expired between offering and sending: verify again, nothing stored."""
    _, cid = await verified(client, outbox)
    await chat(client, "I want to return an item", cid)
    tenant_id = owner_db.execute("SELECT id FROM tenants WHERE slug = 'tenant-a'").fetchone()[0]
    await limits.client.delete(f"orders:access:{tenant_id}:{cid}")
    events, _ = await submit_return(client, cid)
    assert tokens(events) == RETURN_VERIFY_TEXT and request_rows(owner_db) == []


async def test_orders_not_yet_delivered_or_too_old_cannot_be_returned(client, team, orders_on, delivered,
                                                                     outbox):  # noqa: F811
    """Out for delivery: not yet. Delivered 60 days ago (30-day window): closed, with the date."""
    _, cid = await verified(client, outbox, "MR-20417")
    events, _ = await chat(client, "I want to return this order", cid)
    assert "hasn't been delivered yet" in tokens(events) and not blocks(events)
    delivered(days_ago=60, number="MR-10001")
    _, cid = await verified(client, outbox, "MR-10001")
    events, _ = await chat(client, "I want to return this order", cid)
    assert "return window for order #MR-10001 closed on" in tokens(events)


async def test_another_orders_return_needs_its_own_check(client, team, orders_on, delivered, outbox):  # noqa: F811
    """Verified MR-20417, asking to return MR-20388: the lookup form for MR-20388."""
    _, cid = await verified(client, outbox, "MR-20417")
    events, _ = await chat(client, f"I want to return order #{DELIVERED}", cid)
    assert tokens(events) == RETURN_VERIFY_TEXT and blocks(events)[0]["fields"][0]["value"] == DELIVERED


async def test_returns_need_an_order_system(client, team, fake_llm):
    """No order system connected: the model answers (rule 3: the team can help)."""
    await chat(client, "I want to return my order")
    assert fake_llm.calls


# --- Email me the transcript --------------------------------------------------------------------

async def test_the_transcript_goes_only_to_a_confirmed_address(client, outbox, owner_db):  # noqa: F811
    """Address -> code to that address -> wrong code: nothing; right code: the transcript."""
    _, cid = await chat(client, "Do you have the mug in blue?")
    events, _ = await chat(client, "Email me a transcript of this chat", cid)
    assert tokens(events) == TRANSCRIPT_ASK_TEXT
    events, _ = await chat(client, conversation_id=cid, form=("transcript_email", {"email": VISITOR}))
    assert "r•••@meghnabev.example" in tokens(events) and len(outbox.sent) == 1
    events, _ = await chat(client, conversation_id=cid, form=("transcript_code", {"code": "000000"
                                                                                   if code_from(outbox) != "000000"
                                                                                   else "111111"}))
    assert tokens(events) == TRANSCRIPT_WRONG_TEXT and len(outbox.sent) == 1
    events, _ = await chat(client, conversation_id=cid, form=("transcript_code", {"code": code_from(outbox)}))
    assert tokens(events).startswith("Sent!")
    to, subject, text = outbox.sent[-1]
    assert to == VISITOR and "Do you have the mug in blue?" in text and "Bot (" in text
    assert request_rows(owner_db) == []          # nothing stored about the address
    stored = " ".join(r[0] for r in owner_db.execute("SELECT content FROM messages"))
    assert VISITOR not in stored


async def test_a_transcript_code_only_works_in_its_conversation(client, outbox):  # noqa: F811
    """A code sent in one chat doesn't release another chat's transcript."""
    _, cid = await chat(client, "Email me this chat")
    await chat(client, conversation_id=cid, form=("transcript_email", {"email": VISITOR}))
    _, other = await chat(client, "hello")
    events, _ = await chat(client, conversation_id=other, form=("transcript_code", {"code": code_from(outbox)}))
    assert "expired" in tokens(events) and len(outbox.sent) == 1


async def test_transcript_codes_are_capped_per_address(client, outbox):  # noqa: F811
    """Someone can't use the chat to flood an inbox with codes."""
    for _ in range(limits.TRANSCRIPT_CODES_PER_ADDRESS_HOUR):
        _, cid = await chat(client, "Email me this chat")
        await chat(client, conversation_id=cid, form=("transcript_email", {"email": VISITOR}))
    _, cid = await chat(client, "Email me this chat")
    events, _ = await chat(client, conversation_id=cid, form=("transcript_email", {"email": VISITOR}))
    assert "several codes" in tokens(events)
    assert len(outbox.sent) == limits.TRANSCRIPT_CODES_PER_ADDRESS_HOUR


# --- Delete my data -----------------------------------------------------------------------------

async def test_delete_my_data_removes_the_conversation_and_its_requests(client, team, owner_db):
    """Confirmed: messages, conversation and its requests are gone, the widget is told to forget,
    and the old conversation id no longer works. Other chats are untouched."""
    _, keep = await chat(client, "another visitor's chat")
    _, cid = await lead_offered(client)
    await send_lead(client, cid)
    events, _ = await chat(client, "Please delete my data", cid)
    assert form_of(events, "delete_data")
    events, _ = await chat(client, conversation_id=cid, form=("delete_data", {}))
    assert tokens(events) == NOT_DELETED_TEXT
    events, _ = await chat(client, conversation_id=cid, form=("delete_data", {"confirm": "yes"}))
    assert tokens(events) == DELETED_TEXT and ("forget", {}) in events
    assert owner_db.execute("SELECT count(*) FROM conversations WHERE id = %s", (cid,)).fetchone()[0] == 0
    assert owner_db.execute("SELECT count(*) FROM messages WHERE conversation_id = %s", (cid,)).fetchone()[0] == 0
    assert request_rows(owner_db) == []
    assert owner_db.execute("SELECT count(*) FROM conversations WHERE id = %s", (keep,)).fetchone()[0] == 1
    response = await client.post("/chat", json={"site_key": KEY_A, "conversation_id": cid, "message": "hi"},
                                 headers={"Origin": ORIGIN_A})
    assert response.status_code == 404


async def test_another_tenant_cannot_delete_a_conversation(client, owner_db):
    """Tenant B's key can't reach tenant A's conversation (404 before anything runs)."""
    _, cid = await chat(client, "Please delete my data")
    response = await client.post("/chat", headers={"Origin": ORIGIN_B}, json={
        "site_key": KEY_B, "conversation_id": cid, "form": {"name": "delete_data", "values": {"confirm": "yes"}}})
    assert response.status_code == 404
    assert owner_db.execute("SELECT count(*) FROM conversations WHERE id = %s", (cid,)).fetchone()[0] == 1
