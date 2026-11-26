"""Order tracking in the chat (order_tracking.py via /chat): roadmap step 8's "Done when":
a verified visitor sees the timeline; a wrong code or email reveals nothing."""
import asyncio
import re

import pytest
from conftest import KEY_A, KEY_B, ORIGIN_A, ORIGIN_B
from test_auth import outbox  # noqa: F401  (fixture: captured emails)
from test_chat import parse_sse, send
from test_chat_guards import tokens
from test_order_sources import API_SECRET, API_URL, CUSTOMER, shop  # noqa: F401  (fixture: fake shop)

import order_connections
import order_tracking
from order_tracking import (ASK_TEXT, CODE_SENT_TEXT, NO_CODE_TEXT, OFF_TEXT, TOO_MANY_TEXT, VERIFIED_TEXT,
                            WRONG_CODE_TEXT, looks_like_order_question, mask_email, order_number_in)

_CODE = re.compile(r"^\s+(\d{6})$", re.MULTILINE)


@pytest.fixture
async def orders_on(owner_db, shop):  # noqa: F811
    """Tenant A has its generic order API connected (the fake shop answers it)."""
    tenant_id = owner_db.execute("SELECT id FROM tenants WHERE slug = 'tenant-a'").fetchone()[0]
    await order_connections.save(tenant_id, "api", API_URL, {"secret": API_SECRET})
    return tenant_id


async def chat(client, message=None, conversation_id=None, form=None, key=KEY_A, origin=ORIGIN_A):
    """One turn; returns (events, conversation id). `form` = (name, values)."""
    body = {"site_key": key}
    if message is not None:
        body["message"] = message
    if form is not None:
        body["form"] = {"name": form[0], "values": form[1]}
    if conversation_id:
        body["conversation_id"] = conversation_id
    response = await client.post("/chat", json=body, headers={"Origin": origin})
    assert response.status_code == 200, response.text
    events = parse_sse(response.text)
    await order_tracking.drain()                  # the background lookup, if one started
    return events, events[0][1]["conversation_id"]


def blocks(events) -> list[dict]:
    """The UI blocks of a reply."""
    return [d for e, d in events if e == "block"]


def code_from(outbox) -> str:  # noqa: F811
    """The one-time code in the latest email."""
    return _CODE.search(outbox.sent[-1][2]).group(1)


async def lookup(client, cid, number="MR-20417", email=CUSTOMER):
    """Submit the order lookup form."""
    return await chat(client, conversation_id=cid, form=("order_lookup", {"order_number": number, "email": email}))


async def verified_conversation(client, outbox) -> str:  # noqa: F811
    """A conversation that has verified order MR-20417."""
    _, cid = await chat(client, "Where's my order #MR-20417?")
    await lookup(client, cid)
    events, _ = await chat(client, conversation_id=cid, form=("order_code", {"code": code_from(outbox)}))
    assert tokens(events) == VERIFIED_TEXT
    return cid


# --- 1. Asking ----------------------------------------------------------------------------------

async def test_order_question_gets_the_lookup_form_not_the_model(client, fake_llm, orders_on):
    """With orders connected, the reply is written by code: a form, order number pre-filled."""
    events, _ = await chat(client, "Hi, where's my order #MR-20417?")
    assert tokens(events) == ASK_TEXT and not fake_llm.calls
    form = blocks(events)[0]
    assert form["submit"] == "order_lookup"
    assert form["fields"][0]["value"] == "MR-20417"


async def test_without_an_order_system_the_model_says_it_cannot_look_up_orders(client, fake_llm, shop):  # noqa: F811
    """Not connected: the model answers, under the 'can't look up orders' rule."""
    await chat(client, "Where's my order #MR-20417?")
    assert "can't look up orders in this chat" in fake_llm.calls[0][0]["content"]
    events, cid = await chat(client, "hi")
    events, _ = await chat(client, conversation_id=cid, form=("order_lookup", {"order_number": "1", "email": "a@b.co"}))
    assert tokens(events) == OFF_TEXT and not shop.calls


async def test_other_questions_still_go_to_the_model(client, fake_llm, orders_on):
    """Policy questions aren't order lookups; the model's rule 3 points to 'track my order'."""
    await chat(client, "How long does delivery take to Chittagong?")
    system = fake_llm.calls[0][0]["content"]
    assert 'type "track my order"' in system and "<verified_order>" not in system


@pytest.mark.parametrize("text", ["Where is my order?", "has my package shipped yet", "track my order",
                                  "my parcel still hasn't arrived", "order #1001", "What's the status of order 1001?"])
def test_order_questions_are_recognised(text):
    """Status, tracking and 'where is it' questions, and order numbers."""
    assert looks_like_order_question(text)


@pytest.mark.parametrize("text", ["How long does delivery take?", "Do you ship to Sylhet?", "What's your return policy?",
                                  "Can I pay cash on delivery?", "hello"])
def test_policy_questions_are_not_order_questions(text):
    """General questions keep going to the model and the knowledge base."""
    assert not looks_like_order_question(text)


def test_order_numbers_and_emails_in_text():
    """Order numbers are read from messages; emails are masked for display."""
    assert order_number_in("where is #mr-20417?") == "MR-20417"
    assert order_number_in("my order number is 1001") == "1001"
    assert order_number_in("call me #maybe") is None
    assert mask_email("fatima@example.com") == "f•••@example.com"


# --- 2. Lookup: the same answer whatever the truth ----------------------------------------------

@pytest.mark.parametrize("number, email", [("MR-20417", CUSTOMER), ("MR-20417", "someone.else@example.com"),
                                           ("MR-99999", CUSTOMER)])
async def test_lookup_answer_never_reveals_whether_the_order_or_email_matched(client, orders_on, outbox, number,
                                                                               email):  # noqa: F811
    """Right email, wrong email, unknown order: identical text and blocks. Only the right one sends a code."""
    _, cid = await chat(client, "Where's my order?")
    events, _ = await lookup(client, cid, number, email)
    assert tokens(events) == CODE_SENT_TEXT
    assert [b["submit"] for b in blocks(events)] == ["order_code"]
    matched = (number, email) == ("MR-20417", CUSTOMER)
    assert len(outbox.sent) == (1 if matched else 0)
    if matched:
        to, subject, text = outbox.sent[0]
        assert to == CUSTOMER and "MR-20417" in subject and code_from(outbox) in text


async def test_lookup_replies_before_the_shop_is_even_asked(client, orders_on, shop, outbox):  # noqa: F811
    """The shop is asked after the reply, so response time can't reveal a match either."""
    _, cid = await chat(client, "Where's my order?")
    shop.gate = asyncio.Event()                       # the shop "hangs" until released
    response = await client.post("/chat", json={"site_key": KEY_A, "conversation_id": cid, "form": {
        "name": "order_lookup", "values": {"order_number": "MR-20417", "email": CUSTOMER}}}, headers={"Origin": ORIGIN_A})
    assert CODE_SENT_TEXT in "".join(d.get("t", "") for _, d in parse_sse(response.text))
    assert not outbox.sent                            # nothing decided yet
    shop.gate.set()
    await order_tracking.drain()
    assert len(outbox.sent) == 1


async def test_email_comparison_ignores_case_and_spaces(client, orders_on, outbox):  # noqa: F811
    """'  FATIMA@Example.com ' is the same address."""
    _, cid = await chat(client, "Where's my order?")
    await lookup(client, cid, email="  FATIMA@Example.com ")
    assert len(outbox.sent) == 1


async def test_the_conversation_never_stores_the_email_or_the_code(client, orders_on, outbox, owner_db):  # noqa: F811
    """Forms are stored as summaries; the order card isn't stored at all."""
    await verified_conversation(client, outbox)
    code = code_from(outbox)
    rows = owner_db.execute("SELECT role, content, blocks::text FROM messages ORDER BY id").fetchall()
    stored = " ".join(f"{r[1]} {r[2]}" for r in rows).lower()
    assert "fatima" not in stored and code not in stored and "pathao" not in stored
    assert rows[-1][1] == "Showed order #MR-20417 to the verified customer." and rows[-1][2] is None
    assert [r[1] for r in rows if r[0] == "user"] == ["Where's my order #MR-20417?", "Find order #MR-20417",
                                                      "Here's my code"]


async def test_lookups_are_rate_limited_per_conversation(client, orders_on, outbox):  # noqa: F811
    """Six lookups in an hour: the sixth is refused, and nothing is looked up."""
    _, cid = await chat(client, "Where's my order?")
    for _ in range(5):
        await lookup(client, cid, email="guess@example.com")
    events, _ = await lookup(client, cid)
    assert "several order lookups" in tokens(events) and not outbox.sent


async def test_code_emails_are_capped_per_order(client, orders_on, outbox):  # noqa: F811
    """Even from many chats, one order's customer gets at most 3 codes an hour."""
    for _ in range(4):
        _, cid = await chat(client, "Where's my order?")
        await lookup(client, cid)
    assert len(outbox.sent) == 3


# --- 3. The code --------------------------------------------------------------------------------

async def test_the_right_code_shows_the_timeline(client, orders_on, outbox, fake_llm):  # noqa: F811
    """Design 6: verified badge with the masked email, then the tracking card. No model call."""
    _, cid = await chat(client, "Where's my order #MR-20417?")
    await lookup(client, cid)
    events, _ = await chat(client, conversation_id=cid, form=("order_code", {"code": code_from(outbox)}))
    assert tokens(events) == VERIFIED_TEXT and not fake_llm.calls
    notice, card = blocks(events)
    assert notice == {"type": "notice", "tone": "success", "text": "Verified: f•••@example.com"}
    assert card["eyebrow"] == "Order #MR-20417" and card["title"] == "Arriving Oct 31 by 6:00 pm"
    assert card["status"] == {"label": "Out for delivery", "tone": "warning"}
    assert [(s["label"], s["state"]) for s in card["steps"]] == [
        ("Ordered", "done"), ("Packed", "done"), ("Shipped", "done"), ("Out for delivery", "current"),
        ("Delivered", "upcoming")]
    assert card["steps"][0]["detail"] == "Oct 28 · 2 items" and card["steps"][2]["detail"] == "Oct 30 · Dhaka hub"
    assert card["footer"] == "Pathao Courier · PTH-88213"
    assert card["link"] == {"kind": "link", "url": "https://track.pathao.example/PTH-88213"}


async def test_a_wrong_code_reveals_nothing_and_five_cancel_it(client, orders_on, outbox):  # noqa: F811
    """Wrong codes get no order data; after five, even the right code no longer works."""
    _, cid = await chat(client, "Where's my order?")
    await lookup(client, cid)
    right = code_from(outbox)
    wrong = f"{(int(right) + 1) % 1_000_000:06d}"
    for attempt in range(1, 6):
        events, _ = await chat(client, conversation_id=cid, form=("order_code", {"code": wrong}))
        assert not [b for b in blocks(events) if b["type"] == "timeline"]
        assert tokens(events) == (TOO_MANY_TEXT if attempt == 5 else WRONG_CODE_TEXT)
    events, _ = await chat(client, conversation_id=cid, form=("order_code", {"code": right}))
    assert tokens(events) == NO_CODE_TEXT


async def test_a_code_without_a_matching_lookup_reveals_nothing(client, orders_on, outbox):  # noqa: F811
    """Wrong email: no code was ever made, so every code is 'expired or doesn't match'."""
    _, cid = await chat(client, "Where's my order?")
    await lookup(client, cid, email="attacker@example.com")
    events, _ = await chat(client, conversation_id=cid, form=("order_code", {"code": "123456"}))
    assert tokens(events) == NO_CODE_TEXT and blocks(events)[0]["submit"] == "order_lookup"


async def test_a_code_works_once_and_only_in_its_own_conversation(client, orders_on, outbox):  # noqa: F811
    """Another chat can't use the code; the chat that asked can, once."""
    _, cid = await chat(client, "Where's my order?")
    await lookup(client, cid)
    code = code_from(outbox)
    _, other = await chat(client, "hello")
    events, _ = await chat(client, conversation_id=other, form=("order_code", {"code": code}))
    assert tokens(events) == NO_CODE_TEXT
    events, _ = await chat(client, conversation_id=cid, form=("order_code", {"code": code}))
    assert tokens(events) == VERIFIED_TEXT
    events, _ = await chat(client, conversation_id=cid, form=("order_code", {"code": code}))
    assert tokens(events) == NO_CODE_TEXT


async def test_access_belongs_to_one_conversation(client, orders_on, outbox, fake_llm):  # noqa: F811
    """A verified chat doesn't make the order visible to a new chat."""
    await verified_conversation(client, outbox)
    events, _ = await chat(client, "Where's my order #MR-20417?")
    assert tokens(events) == ASK_TEXT and not fake_llm.calls


async def test_another_tenant_cannot_use_the_conversation(client, orders_on, outbox):  # noqa: F811
    """Conversation ids are tenant-scoped: tenant B's site key can't even find it."""
    cid = await verified_conversation(client, outbox)
    response = await client.post("/chat", json={"site_key": KEY_B, "conversation_id": cid, "message": "status?"},
                                 headers={"Origin": ORIGIN_B})
    assert response.status_code == 404


# --- 4. After verification ----------------------------------------------------------------------

async def test_follow_up_questions_get_the_verified_order_only(client, orders_on, outbox, fake_llm):  # noqa: F811
    """The model sees the order's tracking facts, fenced; never the email or address."""
    cid = await verified_conversation(client, outbox)
    fake_llm.reply = ["It's out for delivery and should arrive by 6 pm."]
    events, _ = await chat(client, "When will it arrive?", conversation_id=cid)
    assert tokens(events) == "It's out for delivery and should arrive by 6 pm."
    system = fake_llm.calls[-1][0]["content"]
    assert "<verified_order>" in system and "Order number: #MR-20417" in system
    assert "Tracking number: PTH-88213" in system and "the visitor proved, with a code" in system
    assert "fatima" not in system.lower() and "dhaka, " not in system.lower() and "House 1" not in system


async def test_after_verification_the_reply_may_name_only_that_order(client, orders_on, outbox, fake_llm):  # noqa: F811
    """The guard lets the verified order's numbers through and stops any other order's."""
    cid = await verified_conversation(client, outbox)
    fake_llm.reply = ["Your order #MR-20417 is out for delivery; tracking number PTH-88213."]
    events, _ = await chat(client, "status?", conversation_id=cid)
    assert not [e for e, _ in events if e == "replace"]
    fake_llm.reply = ["Also, order #MR-55555 has shipped."]
    events, _ = await chat(client, "anything else?", conversation_id=cid)
    assert [d["text"] for e, d in events if e == "replace"] and "MR-55555" not in str(events[-3:])


async def test_asking_about_another_order_needs_its_own_check(client, orders_on, outbox, fake_llm):  # noqa: F811
    """Verified for MR-20417, asking about #MR-55555: the lookup form again, no model, no data."""
    cid = await verified_conversation(client, outbox)
    events, _ = await chat(client, "And where is order #MR-55555?", conversation_id=cid)
    assert tokens(events) == ASK_TEXT and blocks(events)[0]["fields"][0]["value"] == "MR-55555"


async def test_an_unverified_order_claim_points_to_tracking(client, orders_on, fake_llm):
    """Without verification, an invented status is replaced by the way to check it safely."""
    fake_llm.reply = ["Your order has shipped yesterday."]
    events, _ = await chat(client, "hello there")
    replaced = [d["text"] for e, d in events if e == "replace"]
    assert replaced and "track my order" in replaced[0]


async def test_shop_outage_after_verification_says_so(client, orders_on, outbox, shop):  # noqa: F811
    """If the shop can't be reached, the visitor is told; nothing is invented."""
    cid = await verified_conversation(client, outbox)
    shop.down = True
    events, _ = await chat(client, "Where is my order now?", conversation_id=cid)
    assert "can't reach the order system" in tokens(events)


# --- Request validation -------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    {"form": {"name": "order_lookup", "values": {}}},                                  # no conversation
    {"form": {"name": "delete_everything", "values": {}}, "conversation_id": "00000000-0000-0000-0000-000000000000"},
    {"form": {"name": "order_code", "values": {"code": "1"}}, "message": "hi",
     "conversation_id": "00000000-0000-0000-0000-000000000000"},                      # both
    {"form": {"name": "order_code", "values": {"Bad Name": "1"}},
     "conversation_id": "00000000-0000-0000-0000-000000000000"},
])
async def test_malformed_form_submissions_are_rejected(client, body):
    """Unknown forms, forms without a conversation or with a message too: 422."""
    response = await client.post("/chat", json={"site_key": KEY_A, **body}, headers={"Origin": ORIGIN_A})
    assert response.status_code == 422


async def test_send_helper_still_works_for_plain_messages(client):
    """Plain chat is unchanged by forms."""
    assert (await send(client, "hi")).status_code == 200
