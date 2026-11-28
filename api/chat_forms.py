"""The fixed set of chat forms (roadmap step 9). Like order tracking, these replies are written by
code, not by the model:

  The visitor says…                 The reply
  "I want to return …"              a verified, delivered order: the return form (design 7);
                                    not verified yet: the order lookup form first (step 8)
  "can I talk to a person?"         the contact form as a handoff (W-10), plus "Book a call" if set
  "can someone contact me / quote"  the contact form (W-07) with a summary draft to check and edit
  "email me this chat"              an address -> a code sent there -> the transcript (no storage)
  "delete my data"                  a confirmation -> the conversation, its messages and requests go

Rules enforced here:
  * Consent: return and contact forms end with a required consent box. Unticked = nothing is
    stored or sent (the server checks; the widget's check is only a convenience).
  * Only offered forms: each form shown is remembered in Redis for this conversation (30 min),
    with the choices it offered. A submission for a form that wasn't offered, or with options
    that weren't in it (another order's items), is refused.
  * The conversation stores a summary ("Sent my details to the team"), never the details.
  * Requests are delivered by the worker (team_requests.deliver): email to the team, webhook.
"""
import hashlib
import json
import logging
import re
from uuid import UUID

import db
import email_codes
import guards
import limits
import mailer
import order_tracking
import returns
import team_requests
from config import settings
from conversations import Turn
from jobs import RedisQueue
from limits import LimitExceeded
from llm import Usage, complete
from models import EMAIL_PATTERN
from order_tracking import mask_email
from prompts import summary_draft_prompt
from replies import FormSubmission, ScriptedReply, text_value
from returns import OfferedReturn
from tenancy import Tenant

log = logging.getLogger("easychat.forms")

FORM_RETURN = "return_request"
FORM_LEAD = "lead"
FORM_TRANSCRIPT_EMAIL = "transcript_email"
FORM_TRANSCRIPT_CODE = "transcript_code"
FORM_DELETE = "delete_data"
FORMS = (FORM_RETURN, FORM_LEAD, FORM_TRANSCRIPT_EMAIL, FORM_TRANSCRIPT_CODE, FORM_DELETE)
TRANSCRIPT_PURPOSE = "transcript"         # email_codes purpose
OFFER_TTL_SECONDS = 30 * 60
NAME_MAX_CHARS = 80
SUMMARY_MAX_CHARS = 600                   # the draft; the visitor may edit it up to schemas.MAX_FIELD_CHARS
SUMMARY_DRAFT_TOKENS = 200
TRANSCRIPT_MAX_MESSAGES = 200
CARD_VALUE_CHARS = 400                    # blocks.SummaryRow value limit
YES = team_requests.CONSENT_VALUE

queue = RedisQueue(limits.client)

# --- Texts --------------------------------------------------------------------------------------

EXPIRED_TEXT = "That form has expired. Tell me again what you need and I'll bring up a new one."
NO_CONSENT_TEXT = ("Nothing was sent or saved. To send it, tick the box to agree, or just keep chatting if you'd "
                   "rather not.")
BAD_CONTACT_TEXT = "Please enter your name and a valid email address."
BAD_RETURN_TEXT = "Please pick the items, a reason and how you'd like to be refunded."
RETURN_VERIFY_TEXT = ("I can help with a return. First let's confirm the order: enter its number and the email you "
                      "used at checkout, and I'll email you a code.")
TRANSCRIPT_ASK_TEXT = ("I can email you a copy of this chat. Enter your address and I'll send a code there first, "
                       "to check it's yours.")
TRANSCRIPT_BAD_EMAIL_TEXT = "Please enter a valid email address."
TRANSCRIPT_WRONG_TEXT = "That code didn't work. Check the email and try again."
TRANSCRIPT_NO_CODE_TEXT = "That code has expired. Let's send a new one."
TRANSCRIPT_TOO_MANY_TEXT = "That's too many wrong codes, so I've cancelled it. You can ask for a new one."
TRANSCRIPT_BAD_CODE_TEXT = f"Please enter the {email_codes.CODE_DIGITS}-digit code from the email."
MAIL_FAILED_TEXT = "I couldn't send the email just now. Please try again in a few minutes."
DELETE_ASK_TEXT = ("I can delete this conversation now: every message, and any request you sent from it. "
                   "Requests already emailed to the team can't be recalled from their inbox.")
DELETE_CONFIRM_LABEL = "Yes, delete this chat and everything I sent through it"
NOT_DELETED_TEXT = "Nothing was deleted. Tick the box if you want me to delete this chat."
DELETED_TEXT = ("Done: this conversation and everything you sent through it have been deleted. Anything you "
                "write now starts a new chat.")

# --- Recognising what the visitor wants ---------------------------------------------------------

_I = re.IGNORECASE | re.DOTALL
_DELETE = re.compile(r"\b(?:delete|erase|remove|wipe|forget)\b.{0,30}?\b(?:my|this|our|all)\b.{0,20}?"
                     r"\b(?:data|chat|conversation|messages|history|information|info|details)\b", _I)
_TRANSCRIPT = re.compile(r"\btranscript\b|\b(?:email|send|mail)\b.{0,15}?\b(?:a\s+)?(?:copy\s+of\s+)?"
                         r"(?:this|the|our)\s+(?:chat|conversation)\b", _I)
_HANDOFF = re.compile(r"\b(?:talk|speak|chat)\s+(?:to|with)\s+(?:a\s+|an\s+|the\s+|some\s+|your\s+|real\s+)*"
                      r"(?:human|person|someone|somebody|agent|representative|rep|team|staff|support|manager)\b"
                      r"|\bconnect\s+me\s+(?:to|with)\b|\b(?:real|live)\s+(?:human|person|agent)\b|\bhuman\s+agent\b", _I)
_LEAD = re.compile(r"\b(?:get|request|want|need|send\s+me)\b.{0,20}?\b(?:a\s+)?(?:quote|quotation|estimate|proposal|"
                   r"call\s*back)\b|\b(?:contact|call|email|reach)\s+me\b|\bget\s+in\s+touch\s+with\s+me\b", _I)


def intent_of(message: str) -> str | None:
    """Which chat form a message asks for, if any (first match wins: the most specific first)."""
    if _DELETE.search(message):
        return FORM_DELETE
    if _TRANSCRIPT.search(message):
        return FORM_TRANSCRIPT_EMAIL
    if _HANDOFF.search(message):
        return "handoff"
    if _LEAD.search(message):
        return FORM_LEAD
    if returns.looks_like_return_request(message):
        return FORM_RETURN
    return None


def form_summary(form: FormSubmission) -> str:
    """What the conversation stores for a submitted form: never the details it carried."""
    if form.name in order_tracking.FORMS:
        return order_tracking.form_summary(form)
    return {FORM_RETURN: "Sent the return form", FORM_LEAD: "Sent my contact details",
            FORM_TRANSCRIPT_EMAIL: "Email me the transcript", FORM_TRANSCRIPT_CODE: "Here's my code",
            FORM_DELETE: "Delete my data"}.get(form.name, "Sent a form")


# --- Forms offered in this conversation (Redis) -------------------------------------------------

def _offered_key(tenant_id: UUID, conversation_id: UUID) -> str:
    """Hash of form name -> what was offered, for one conversation."""
    return f"forms:offered:{tenant_id}:{conversation_id}"


async def _offer(tenant_id: UUID, conversation_id: UUID, name: str, data: dict) -> None:
    """Remember that `name` was offered here (with its options), for OFFER_TTL_SECONDS."""
    key = _offered_key(tenant_id, conversation_id)
    async with limits.client.pipeline(transaction=True) as pipe:
        pipe.hset(key, name, json.dumps(data))
        pipe.expire(key, OFFER_TTL_SECONDS)
        await pipe.execute()


async def _offered(tenant_id: UUID, conversation_id: UUID, name: str) -> dict | None:
    """What `name` offered in this conversation, or None if it wasn't (or it expired)."""
    raw = await limits.client.hget(_offered_key(tenant_id, conversation_id), name)
    return json.loads(raw) if raw else None


async def _withdraw(tenant_id: UUID, conversation_id: UUID, name: str) -> None:
    """The form was used: it can't be sent again."""
    await limits.client.hdel(_offered_key(tenant_id, conversation_id), name)


# --- Shared pieces ------------------------------------------------------------------------------

def _privacy_link(tenant: Tenant) -> dict:
    """{"privacy_url": ...} when the tenant's privacy page is a URL blocks.py accepts, else {}."""
    url = str(tenant.config.privacy_url)
    local = settings.is_dev and re.match(r"^http://(localhost|127\.0\.0\.1)[:/]", url)
    return {"privacy_url": url} if url.startswith("https://") or local else {}


def _consent(tenant: Tenant) -> dict:
    """The tenant's consent checkbox."""
    return team_requests.consent_field(tenant.config.company_name, tenant.config.requests.retention_days)


def _card(title: str, rows: list[tuple[str, str]], footer: str | None = None) -> dict:
    """A summary block (W-08), values cut to what the block allows."""
    block = {"type": "summary", "title": title[:120],
             "rows": [{"label": label[:40], "value": (value[:CARD_VALUE_CHARS - 1] + "…")
                       if len(value) > CARD_VALUE_CHARS else value} for label, value in rows if value]}
    if footer:
        block["footer"] = footer[:120]
    return block


def _transcript_chip() -> dict:
    """"Email me this transcript" (W-08)."""
    return {"label": "Email me this chat", "action": {"kind": "send", "text": "Email me a transcript of this chat"}}


def _booking_chip(tenant: Tenant) -> dict | None:
    """"Book a call" (W-08, W-10), if the team has a booking page."""
    url = tenant.config.requests.booking_url
    return {"label": "Book a call", "action": {"kind": "link", "url": str(url)}} if url else None


async def _send_request(tenant: Tenant, conversation_id: UUID, ip: str, kind: str, payload: dict) -> str | None:
    """Store a consented request and queue its delivery. Returns a visitor-facing problem, or None."""
    try:
        await limits.check_request(tenant.id, conversation_id, ip, tenant.limits)
    except LimitExceeded as exc:
        return str(exc.detail)
    request_id = await team_requests.create(tenant.id, conversation_id, kind, payload, team_requests.consent_text(
        tenant.config.company_name, tenant.config.requests.retention_days), tenant.config.requests.retention_days)
    await team_requests.enqueue_delivery(queue, tenant.id, request_id)
    log.info("request %s (%s) stored for tenant %s", request_id, kind, tenant.slug)
    return None


# --- Contact form: lead and "talk to a person" (W-07, W-08, W-10) --------------------------------

def _visitor_lines(history: list[dict]) -> list[str]:
    """What the visitor wrote, oldest first."""
    return [m["content"] for m in history if m["role"] == "user"]


def fallback_summary(history: list[dict]) -> str:
    """Without the model: the visitor's own recent messages (they can edit them)."""
    lines = _visitor_lines(history)
    earlier = lines[:-1] or lines                    # the last one is usually "can someone contact me"
    return " ".join(" ".join(line.split()) for line in earlier[-3:])[:SUMMARY_MAX_CHARS]


def _summary_is_clean(summary: str, history: list[dict]) -> bool:
    """A draft may not add links, emails or phone numbers the visitor didn't write."""
    said = "\n".join(_visitor_lines(history)).lower()
    return (not guards.link_hosts(summary)
            and all(email in said for email in guards.emails(summary))
            and all(phone in re.sub(r"\D", "", said) for phone in guards.phone_numbers(summary)))


_FENCE = re.compile(r"<\s*/?\s*conversation", re.IGNORECASE)


def _defuse(text: str) -> str:
    """Remove anything that could close or reopen the <conversation> fence."""
    return _FENCE.sub("", text)


async def draft_summary(tenant: Tenant, history: list[dict]) -> str:
    """A short summary of what the visitor wants, for them to check (the model, with a fallback)."""
    conversation = "\n".join(f"{'Visitor' if m['role'] == 'user' else 'Assistant'}: {_defuse(m['content'])}"
                             for m in history[-8:])
    messages = [{"role": "system", "content": summary_draft_prompt(tenant.config.company_name)},
                {"role": "user", "content": f"<conversation>\n{conversation}\n</conversation>"}]
    usage = Usage()
    try:
        draft = " ".join((await complete(messages, usage, SUMMARY_DRAFT_TOKENS)).split())[:SUMMARY_MAX_CHARS]
    except Exception as exc:  # the model is optional here: the visitor's own words will do
        log.warning("summary draft failed (%s): using the visitor's messages", type(exc).__name__)
        draft = ""
    finally:
        await limits.record_tokens(tenant.id, usage.total)
    return draft if draft and _summary_is_clean(draft, history) else fallback_summary(history)


def lead_form(tenant: Tenant, handoff: bool, name: str = "", email: str = "", summary: str = "") -> dict:
    """Name, email, the editable summary and the consent box (W-07)."""
    def text(kind: str, field: str, label: str, value: str, placeholder: str = "") -> dict:
        """One required text field, pre-filled when there's a value."""
        spec = {"kind": kind, "name": field, "label": label, "required": True, "placeholder": placeholder}
        return {**spec, "value": value} if value else spec

    company = tenant.config.company_name
    return {"type": "form", "title": (f"Talk to the {company} team" if handoff else f"Pass this to the {company} team")[:120],
            "submit": FORM_LEAD, "submit_label": "Agree & send to team",
            "fields": [text("text", "name", "Your name", name), text("email", "email", "Your email", email,
                                                                    "you@example.com"),
                       text("textarea", "summary", "What it's about", summary), _consent(tenant)],
            **_privacy_link(tenant)}


async def offer_lead(tenant: Tenant, turn: Turn, handoff: bool) -> ScriptedReply | None:
    """The contact form, if the tenant takes requests and has somewhere to send them."""
    requests = tenant.config.requests
    if not requests.leads or await team_requests.notify_address(tenant.id, requests) is None:
        return None
    company = tenant.config.company_name
    summary = await draft_summary(tenant, turn.history)
    await _offer(tenant.id, turn.conversation_id, FORM_LEAD, {"handoff": handoff})
    if handoff:
        text = (f"I'm {company}'s AI assistant, but I can pass this to a person on the team. Leave your name and "
                "email, check the summary, and they'll reply by email.")
    else:
        text = f"I can pass this to the {company} team so they can follow up. Check the summary and tell me where to reach you."
    blocks = [lead_form(tenant, handoff, summary=summary)]
    if handoff and (booking := _booking_chip(tenant)):
        blocks.append({"type": "chips", "items": [booking]})
    return ScriptedReply(text, blocks, store_blocks=False)


async def submit_lead(tenant: Tenant, conversation_id: UUID, form: FormSubmission, ip: str) -> ScriptedReply:
    """Check consent and fields, store the request, queue its delivery, confirm with a card (W-08)."""
    offered = await _offered(tenant.id, conversation_id, FORM_LEAD)
    if offered is None:
        return ScriptedReply(EXPIRED_TEXT, [])
    name = " ".join(text_value(form.values, "name").split())[:NAME_MAX_CHARS]
    email = text_value(form.values, "email").lower()
    summary = text_value(form.values, "summary")
    handoff = bool(offered.get("handoff"))
    again = lead_form(tenant, handoff, name, email if len(email) <= 254 else "", summary)
    if not team_requests.consent_given(form.values):
        return ScriptedReply(NO_CONSENT_TEXT, [again], store_blocks=False)
    if not name or not summary or not re.fullmatch(EMAIL_PATTERN, email):
        return ScriptedReply(BAD_CONTACT_TEXT, [again], store_blocks=False)
    kind = "handoff" if handoff else "lead"
    problem = await _send_request(tenant, conversation_id, ip, kind, {"name": name, "email": email, "summary": summary})
    if problem:
        return ScriptedReply(problem, [])
    await _withdraw(tenant.id, conversation_id, FORM_LEAD)
    company = tenant.config.company_name
    chips = [_transcript_chip()] + ([booking] if (booking := _booking_chip(tenant)) else [])
    return ScriptedReply(
        f"Done! I've sent this to the {company} team. They usually reply by email.",
        [_card(f"Sent to the {company} team", team_requests.summary_rows(kind, {"name": name, "email": email,
                                                                               "summary": summary}, for_team=False)),
         {"type": "chips", "items": chips}],
        stored_text=f"Sent the visitor's details to the {company} team.", store_blocks=False)


# --- Return request (design 7) ------------------------------------------------------------------

async def offer_return(tenant: Tenant, conversation_id: UUID, message: str) -> ScriptedReply | None:
    """The return form for this chat's verified order; the lookup form if none is verified yet."""
    config = tenant.config.requests.returns
    if config is None or not await order_tracking.has_connection(tenant.id):
        return None
    if await team_requests.notify_address(tenant.id, tenant.config.requests) is None:
        return None
    number, order = await order_tracking.verified_order(tenant, conversation_id)
    asked = order_tracking.order_number_in(message)
    if number is None or (asked and asked != number):
        return ScriptedReply(RETURN_VERIFY_TEXT, [order_tracking.lookup_form(asked)])
    if order is None:
        return ScriptedReply(order_tracking.UNAVAILABLE_TEXT, [])
    returnable = returns.check_returnable(order, config)
    if not returnable.ok:
        return ScriptedReply(returnable.reason, [])
    block, offered = returns.return_form(order, config, tenant.asset_policy(settings.is_dev), _consent(tenant),
                                         _privacy_link(tenant).get("privacy_url"))
    await _offer(tenant.id, conversation_id, FORM_RETURN, offered.to_json())
    until = f" You can ask until {returns.short_day(returnable.deadline)}." if returnable.deadline else ""
    return ScriptedReply(f"Sure. Which items from order #{order.number} are you returning?{until}", [block],
                         stored_text=f"Offered the return form for order #{order.number}.", store_blocks=False)


async def submit_return(tenant: Tenant, conversation_id: UUID, form: FormSubmission, ip: str) -> ScriptedReply:
    """Check that the form was offered, the order is still verified and returnable, and the
    choices are the offered ones; then store the request and confirm."""
    raw = await _offered(tenant.id, conversation_id, FORM_RETURN)
    config = tenant.config.requests.returns
    if raw is None or config is None:
        return ScriptedReply(EXPIRED_TEXT, [])
    offered = OfferedReturn.from_json(raw)
    number, order = await order_tracking.verified_order(tenant, conversation_id)
    if number != offered.order_number:
        return ScriptedReply(RETURN_VERIFY_TEXT, [order_tracking.lookup_form(offered.order_number)])
    if order is None:
        return ScriptedReply(order_tracking.UNAVAILABLE_TEXT, [])
    returnable = returns.check_returnable(order, config)
    if not returnable.ok:
        return ScriptedReply(returnable.reason, [])
    if not team_requests.consent_given(form.values):
        return ScriptedReply(NO_CONSENT_TEXT, [], store_blocks=False)
    payload = returns.return_payload(offered, form.values, order.email)
    if payload is None:
        return ScriptedReply(BAD_RETURN_TEXT, [])
    problem = await _send_request(tenant, conversation_id, ip, "return", payload)
    if problem:
        return ScriptedReply(problem, [])
    await _withdraw(tenant.id, conversation_id, FORM_RETURN)
    company = tenant.config.company_name
    return ScriptedReply(
        f"Thanks! Your return request for order #{order.number} is with the {company} team. They'll review it and "
        f"email you at {mask_email(order.email)} with the next steps.",
        [_card("Return request sent", team_requests.summary_rows("return", payload, for_team=False),
               "The team decides on returns and refunds: nothing is approved yet.")],
        stored_text=f"Sent a return request for order #{order.number} to the {company} team.", store_blocks=False)


# --- Email me the transcript --------------------------------------------------------------------

def transcript_email_form() -> dict:
    """The address to send the transcript to."""
    return {"type": "form", "title": "Email me this chat", "submit": FORM_TRANSCRIPT_EMAIL,
            "submit_label": "Send me a code",
            "fields": [{"kind": "email", "name": "email", "label": "Your email", "placeholder": "you@example.com",
                        "required": True}]}


def transcript_code_form() -> dict:
    """The code sent to that address."""
    return {"type": "form", "title": "Check your email", "submit": FORM_TRANSCRIPT_CODE,
            "submit_label": "Email me the chat",
            "fields": [{"kind": "code", "name": "code", "label": f"{email_codes.CODE_DIGITS}-digit code",
                        "required": True}]}


def _address_key(email: str) -> str:
    """A hash naming an address in rate-limit keys (addresses stay out of Redis key names)."""
    return hashlib.sha256(email.encode()).hexdigest()[:24]


async def send_transcript_code(tenant: Tenant, conversation_id: UUID, form: FormSubmission,
                               ip: str) -> ScriptedReply:
    """Email a code to the address the visitor gave (proves it's theirs before we send the chat)."""
    if await _offered(tenant.id, conversation_id, FORM_TRANSCRIPT_EMAIL) is None:
        return ScriptedReply(EXPIRED_TEXT, [])
    email = text_value(form.values, "email").lower()
    if not re.fullmatch(EMAIL_PATTERN, email):
        return ScriptedReply(TRANSCRIPT_BAD_EMAIL_TEXT, [transcript_email_form()])
    try:
        await limits.check_transcript_code(tenant.id, conversation_id, ip, _address_key(email))
    except LimitExceeded as exc:
        return ScriptedReply(str(exc.detail), [])
    code = email_codes.new_code()
    await email_codes.store(TRANSCRIPT_PURPOSE, tenant.id, conversation_id, _address_key(email), code, {"email": email})
    company = tenant.config.company_name
    try:
        await mailer.send_email(email, f"Your code for the {company} chat", (
            "Hi,\n\n"
            f"Here is your code to get a copy of your chat with {company}'s assistant:\n\n    {code}\n\n"
            f"It works once and expires in {email_codes.CODE_TTL_SECONDS // 60} minutes.\n"
            "If you didn't ask for it, ignore this email: nothing will be sent to you.\n"))
    except mailer.MailError:
        await email_codes.forget(TRANSCRIPT_PURPOSE, tenant.id, conversation_id)
        return ScriptedReply(MAIL_FAILED_TEXT, [])
    return ScriptedReply(f"I've emailed a {email_codes.CODE_DIGITS}-digit code to {mask_email(email)}. Enter it below "
                         "and I'll send the chat there.", [transcript_code_form()], store_blocks=False)


async def transcript_text(tenant: Tenant, conversation_id: UUID) -> str:
    """The conversation as plain text, oldest first."""
    async with db.tenant_tx(tenant.id) as conn:
        cur = await conn.execute(
            "SELECT role, content, created_at FROM (SELECT id, role, content, created_at FROM messages "
            "WHERE conversation_id = %s ORDER BY id DESC LIMIT %s) recent ORDER BY id",
            (conversation_id, TRANSCRIPT_MAX_MESSAGES))
        rows = await cur.fetchall()
    bot = tenant.config.bot_name
    return "\n\n".join(f"{'You' if r['role'] == 'user' else bot} ({r['created_at']:%Y-%m-%d %H:%M} UTC):\n{r['content']}"
                       for r in rows)


async def send_transcript(tenant: Tenant, conversation_id: UUID, form: FormSubmission) -> ScriptedReply:
    """Check the code; email the transcript to the address it proved."""
    result = await email_codes.check(TRANSCRIPT_PURPOSE, tenant.id, conversation_id, text_value(form.values, "code"))
    if result.outcome == "bad_format":
        return ScriptedReply(TRANSCRIPT_BAD_CODE_TEXT, [transcript_code_form()])
    if result.outcome == "wrong":
        return ScriptedReply(TRANSCRIPT_WRONG_TEXT, [transcript_code_form()])
    if result.outcome in ("missing", "too_many"):
        await _offer(tenant.id, conversation_id, FORM_TRANSCRIPT_EMAIL, {})
        text = TRANSCRIPT_TOO_MANY_TEXT if result.outcome == "too_many" else TRANSCRIPT_NO_CODE_TEXT
        return ScriptedReply(text, [transcript_email_form()])
    email = result.data["email"]
    company = tenant.config.company_name
    body = (f"Your chat with {company}'s AI assistant, as you asked.\n\n{await transcript_text(tenant, conversation_id)}"
            "\n\n--\nSent because this address was entered in the chat and confirmed with a code. "
            "The assistant can make mistakes: check anything important with the team.\n")
    try:
        await mailer.send_email(email, f"Your chat with {company}", body)
    except mailer.MailError:
        return ScriptedReply(MAIL_FAILED_TEXT, [])
    await _withdraw(tenant.id, conversation_id, FORM_TRANSCRIPT_EMAIL)
    return ScriptedReply(f"Sent! Check your inbox at {mask_email(email)}.", [],
                         stored_text="Emailed the transcript to the visitor.")


# --- Delete my data -----------------------------------------------------------------------------

def delete_form() -> dict:
    """An explicit confirmation: deleting can't be undone."""
    return {"type": "form", "title": "Delete this chat", "submit": FORM_DELETE, "submit_label": "Delete my data",
            "fields": [{"kind": "checkbox", "name": "confirm", "label": DELETE_CONFIRM_LABEL, "required": True}]}


async def delete_conversation(tenant_id: UUID, conversation_id: UUID) -> bool:
    """Delete the conversation, its messages (cascade) and requests, and its Redis state (the
    visitor's "delete my data", or the team from the inbox). False if there was no such chat."""
    async with db.tenant_tx(tenant_id) as conn:
        await team_requests.delete_for_conversation(conn, conversation_id)
        cur = await conn.execute("DELETE FROM conversations WHERE id = %s", (conversation_id,))
    await limits.client.delete(_offered_key(tenant_id, conversation_id))
    await order_tracking.forget(tenant_id, conversation_id)
    await email_codes.forget(TRANSCRIPT_PURPOSE, tenant_id, conversation_id)
    log.info("conversation deleted in tenant %s", tenant_id)
    return cur.rowcount > 0


async def submit_delete(tenant: Tenant, conversation_id: UUID, form: FormSubmission) -> ScriptedReply:
    """Delete everything, if the visitor confirmed."""
    if await _offered(tenant.id, conversation_id, FORM_DELETE) is None:
        return ScriptedReply(EXPIRED_TEXT, [])
    if text_value(form.values, "confirm") != YES:
        return ScriptedReply(NOT_DELETED_TEXT, [delete_form()])
    await delete_conversation(tenant.id, conversation_id)
    return ScriptedReply(DELETED_TEXT, [], forget=True)


# --- One turn -----------------------------------------------------------------------------------

async def _submit(tenant: Tenant, conversation_id: UUID, form: FormSubmission, ip: str) -> ScriptedReply:
    """Route a submitted chat form to its handler."""
    if form.name == FORM_LEAD:
        return await submit_lead(tenant, conversation_id, form, ip)
    if form.name == FORM_RETURN:
        return await submit_return(tenant, conversation_id, form, ip)
    if form.name == FORM_TRANSCRIPT_EMAIL:
        return await send_transcript_code(tenant, conversation_id, form, ip)
    if form.name == FORM_TRANSCRIPT_CODE:
        return await send_transcript(tenant, conversation_id, form)
    return await submit_delete(tenant, conversation_id, form)


async def handle(tenant: Tenant, turn: Turn, form: FormSubmission | None, ip: str) -> ScriptedReply | None:
    """This turn's chat-form reply, or None (order tracking or the model answers instead)."""
    cid = turn.conversation_id
    if form is not None:
        return None if form.name in order_tracking.FORMS else await _submit(tenant, cid, form, ip)
    intent = intent_of(turn.history[-1]["content"])
    if intent == FORM_DELETE:
        await _offer(tenant.id, cid, FORM_DELETE, {})
        return ScriptedReply(DELETE_ASK_TEXT, [delete_form()])
    if intent == FORM_TRANSCRIPT_EMAIL:
        await _offer(tenant.id, cid, FORM_TRANSCRIPT_EMAIL, {})
        return ScriptedReply(TRANSCRIPT_ASK_TEXT, [transcript_email_form()])
    if intent in ("handoff", FORM_LEAD):
        return await offer_lead(tenant, turn, handoff=intent == "handoff")
    if intent == FORM_RETURN:
        return await offer_return(tenant, cid, turn.history[-1]["content"])
    return None
