"""Order tracking in the chat (roadmap step 8, widget design 6).

Who may see an order is decided here, in code; the model never decides it:

  1. A visitor asks about an order ("where's my order #MR-20417?"). If the tenant has an order
     system connected, the reply is a form (order number + checkout email), not a model answer.
  2. Form `order_lookup`: the answer is always "if they match, we've emailed a code", and the
     real work (asking the shop, comparing emails, emailing the code) runs in the background,
     so neither the text nor the response time tells a visitor whether that order exists or
     which email it has.
  3. Form `order_code`: the right code (keyed hash in Redis, 10 minutes, 5 tries) grants THIS
     conversation access to THIS order for 30 minutes. The reply is the timeline card, built by
     code from live order data.
  4. Later turns in that conversation: the verified order goes into the prompt as a fenced
     <verified_order> section (status, dates, carrier, tracking; never email, name or address),
     and the output guard only lets the reply mention that order's number and tracking number.

Order details are never stored in the conversation: the card is sent to the visitor, the
stored reply says only that the order was shown.
"""
import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

import db
import email_codes
import limits
import mailer
import returns
from email_codes import CODE_DIGITS, CODE_TTL_SECONDS
from limits import LimitExceeded
from models import EMAIL_PATTERN
from order_connections import source_for
from order_sources import PROGRESS, Order, OrderSourceError, normalize_email, normalize_order_number
from replies import FormSubmission, ScriptedReply, text_value
from tenancy import Tenant

__all__ = ["FormSubmission", "ScriptedReply", "OrderTurn", "handle"]   # FormSubmission/ScriptedReply: replies.py

log = logging.getLogger("easychat.orders")

FORM_LOOKUP = "order_lookup"
FORM_CODE = "order_code"
FORMS = (FORM_LOOKUP, FORM_CODE)
CODE_PURPOSE = "order"                  # email_codes purpose: a code opens one order in one chat
ACCESS_TTL_SECONDS = 30 * 60

ASK_TEXT = ("I can check that for you. Enter your order number and the email you used at checkout, "
            "and I'll email you a code to confirm it's you.")
CODE_SENT_TEXT = ("Thanks! If that order number and email match, we've just emailed you a "
                  f"{CODE_DIGITS}-digit code. Enter it below (it expires in {CODE_TTL_SECONDS // 60} minutes).")
VERIFIED_TEXT = "Found it, verified with the email you used at checkout."
WRONG_CODE_TEXT = "That code didn't work. Check the email we sent and try again."
NO_CODE_TEXT = "That code has expired or doesn't match. Let's send a new one."
TOO_MANY_TEXT = "That's too many wrong codes, so I've cancelled it. You can ask for a new one."
BAD_NUMBER_TEXT = "That doesn't look like an order number. You'll find it in your order confirmation email."
BAD_EMAIL_TEXT = "Please enter the email address you used at checkout."
BAD_CODE_FORMAT_TEXT = f"Please enter the {CODE_DIGITS}-digit code from the email."
UNAVAILABLE_TEXT = "I can't reach the order system right now. Please try again in a few minutes."
OFF_TEXT = "Order tracking isn't available in this chat. Our team can check your order for you."

STATUS_LABELS = {"placed": "Order placed", "packed": "Packed", "shipped": "Shipped",
                 "out_for_delivery": "Out for delivery", "delivered": "Delivered", "cancelled": "Cancelled"}
STEP_LABELS = {"placed": "Ordered", "packed": "Packed", "shipped": "Shipped",
               "out_for_delivery": "Out for delivery", "delivered": "Delivered"}
STATUS_TONES = {"delivered": "success", "out_for_delivery": "warning", "cancelled": "danger"}

_background: set[asyncio.Task] = set()     # code lookups still running (kept referenced)


# --- Data ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class OrderTurn:
    """What order tracking contributes to one turn."""
    enabled: bool = False                    # the tenant has an order system connected
    reply: ScriptedReply | None = None       # answer with this instead of calling the model
    order: Order | None = None               # the conversation's verified order, for the prompt
    refs: frozenset[str] = field(default_factory=frozenset)   # identifiers the reply may mention


# --- Recognising order questions ----------------------------------------------------------------

_NOUN = r"(?:orders?|package|parcel|shipment|delivery|purchase)"
_STATUS_WORDS = (r"(?:where|status|track|tracking|trace|arriv\w*|shipped|dispatched|delivered|late|delayed|"
                 r"lost|missing|still waiting|not (?:yet )?(?:arrived|received|come)|hasn'?t (?:arrived|come|shipped))")
_ORDER_QUESTION = re.compile(rf"\b{_STATUS_WORDS}\b.{{0,60}}\b{_NOUN}\b|\b{_NOUN}\b.{{0,60}}\b{_STATUS_WORDS}\b"
                             r"|\btrack(?:ing)?\s+(?:my|an?|the)\b", re.IGNORECASE | re.DOTALL)
_ORDER_NUMBER = re.compile(r"#\s?([A-Za-z0-9][A-Za-z0-9_-]{2,31})"
                           r"|\border\s*(?:number|no\.?|id)?\s*(?:is\s*)?[:#]?\s*([A-Za-z]{0,6}-?\d[A-Za-z0-9_-]{1,30})",
                           re.IGNORECASE)


def order_number_in(text: str) -> str | None:
    """An order number the visitor wrote ('#MR-20417', 'order 1001'), normalised; it must contain a digit."""
    for match in _ORDER_NUMBER.finditer(text):
        number = normalize_order_number(match.group(1) or match.group(2) or "")
        if number and any(ch.isdigit() for ch in number):
            return number
    return None


def looks_like_order_question(text: str) -> bool:
    """A question about the visitor's own order (status, tracking, where it is), or an order number."""
    return bool(_ORDER_QUESTION.search(text)) or order_number_in(text) is not None


def mask_email(email: str) -> str:
    """'fatima@gmail.com' -> 'f•••@gmail.com' (shows which inbox, reveals little)."""
    local, _, domain = email.partition("@")
    return f"{local[:1]}•••@{domain}" if domain else "•••"


# --- Forms --------------------------------------------------------------------------------------

def lookup_form(number: str | None = None) -> dict:
    """Order number + checkout email (the order number pre-filled when the visitor gave one)."""
    order_field = {"kind": "text", "name": "order_number", "label": "Order number", "placeholder": "e.g. 1001",
                   "required": True}
    if number:
        order_field["value"] = number
    return {"type": "form", "title": "Find your order", "submit": FORM_LOOKUP, "submit_label": "Email me a code",
            "fields": [order_field, {"kind": "email", "name": "email", "label": "Email used at checkout",
                                     "placeholder": "you@example.com", "required": True}]}


def code_form() -> dict:
    """The one-time code field."""
    return {"type": "form", "title": "Check your email", "submit": FORM_CODE, "submit_label": "Show my order",
            "fields": [{"kind": "code", "name": "code", "label": f"{CODE_DIGITS}-digit code", "required": True}]}


def form_summary(form: FormSubmission) -> str:
    """What the conversation stores for a submitted form: never the email or the code."""
    if form.name == FORM_LOOKUP:
        number = normalize_order_number(text_value(form.values, "order_number"))
        return f"Find order #{number}" if number else "Find my order"
    return "Here's my code"


# --- The order card (design 6) ------------------------------------------------------------------

def _day(value: datetime) -> str:
    """'Oct 28' (in the timezone the shop gave)."""
    return f"{value:%b} {value.day}"


def _time(value: datetime) -> str:
    """'9:12 am'."""
    return f"{value.hour % 12 or 12}:{value:%M} {'am' if value.hour < 12 else 'pm'}"


def _title(order: Order) -> str:
    """The card's headline: where the order is, in words."""
    eta, delivered = order.estimated_delivery, order.milestones.get("delivered")
    if order.status == "cancelled":
        return "This order was cancelled"
    if order.status == "delivered":
        return f"Delivered {_day(delivered.at)}" if delivered and delivered.at else "Delivered"
    if order.status in ("shipped", "out_for_delivery"):
        return f"Arriving {_day(eta)} by {_time(eta)}" if eta else "On its way to you"
    return "Packed and getting ready" if order.status == "packed" else "Order received"


def _step_detail(order: Order, step: str) -> str | None:
    """'Oct 28 · 2 items', 'Oct 30 · Dhaka hub', or None."""
    milestone = order.milestones.get(step)
    parts = [_day(milestone.at)] if milestone and milestone.at else []
    if milestone and milestone.detail:
        parts.append(milestone.detail)
    if step == "placed" and order.item_count:
        parts.append(f"{order.item_count} item{'s' if order.item_count != 1 else ''}")
    return " · ".join(parts)[:120] or None


def timeline_block(order: Order) -> dict:
    """The tracking card: status pill, progress steps, carrier footer and Track link."""
    if order.status == "cancelled":
        steps = [{"label": STEP_LABELS["placed"], "detail": _step_detail(order, "placed"), "state": "done"},
                 {"label": "Cancelled", "state": "current"}]
    else:
        current = PROGRESS.index(order.status)
        steps = [{"label": STEP_LABELS[step], "detail": _step_detail(order, step),
                  "state": "done" if i < current or order.status == "delivered" else
                  "current" if i == current else "upcoming"} for i, step in enumerate(PROGRESS)]
    block: dict = {"type": "timeline", "eyebrow": f"Order #{order.number}"[:40], "title": _title(order),
                   "status": {"label": STATUS_LABELS[order.status], "tone": STATUS_TONES.get(order.status, "info")},
                   "steps": [{k: v for k, v in s.items() if v is not None} for s in steps]}
    carrier = " · ".join(p for p in (order.carrier, order.tracking_number) if p)
    if carrier:
        block["footer"] = carrier[:120]
    if order.tracking_url:
        block["link"] = {"kind": "link", "url": order.tracking_url}
    return block


def order_blocks(order: Order, tenant: Tenant | None = None) -> list[dict]:
    """The verified badge and the card; a "Start a return" chip if the order can be returned."""
    blocks = [{"type": "notice", "tone": "success", "text": f"Verified: {mask_email(order.email)}"},
              timeline_block(order)]
    returns_config = tenant.config.requests.returns if tenant else None
    if returns_config and returns.check_returnable(order, returns_config).ok:
        blocks.append(returns.start_return_chips())
    return blocks


# --- Prompt context for the verified order ------------------------------------------------------

def _plain(value: object) -> str:
    """A shop-supplied value made safe for the fenced prompt section (no tags, one line)."""
    return re.sub(r"[<>]", "", " ".join(str(value).split()))[:120]


def order_prompt_block(order: Order) -> str:
    """The verified order for the system prompt. Only what a tracking page shows: no email,
    name, address or payment details ever reach the model."""
    lines = [f"Order number: #{order.number}", f"Status: {STATUS_LABELS[order.status]}"]
    if order.placed_at:
        lines.append(f"Placed: {_day(order.placed_at)}")
    if order.item_count is not None:
        lines.append(f"Items: {order.item_count}")
    for step in PROGRESS[1:]:
        milestone = order.milestones.get(step)
        if milestone and milestone.at:
            where = f" ({_plain(milestone.detail)})" if milestone.detail else ""
            lines.append(f"{STEP_LABELS[step]}: {_day(milestone.at)} {_time(milestone.at)}{where}")
    if order.estimated_delivery and order.status not in ("delivered", "cancelled"):
        lines.append(f"Estimated delivery: {_day(order.estimated_delivery)} by {_time(order.estimated_delivery)}")
    if order.carrier:
        lines.append(f"Carrier: {_plain(order.carrier)}")
    if order.tracking_number:
        lines.append(f"Tracking number: {_plain(order.tracking_number)}")
    if order.tracking_url:
        lines.append("A Track link to the carrier's page is shown on the order card.")
    return "<verified_order>\n" + "\n".join(lines) + "\n</verified_order>"


def order_refs(order: Order) -> frozenset[str]:
    """Identifiers a reply may mention for this order (guards.OutputGuard checks the rest)."""
    refs = {order.number}
    if order.tracking_number:
        refs.add(normalize_order_number(order.tracking_number) or order.tracking_number.upper())
    return frozenset(refs)


# --- Redis state: granted access (pending codes: email_codes.py) ---------------------------------

def _access_key(tenant_id: UUID, conversation_id: UUID) -> str:
    """The order this conversation has proven it may see."""
    return f"orders:access:{tenant_id}:{conversation_id}"


def _order_key(tenant_id: UUID, number: str) -> str:
    """A short hash naming an order in rate-limit keys (order numbers stay out of Redis key names)."""
    return hashlib.sha256(f"{tenant_id}:{number}".encode()).hexdigest()[:24]


async def access_for(tenant_id: UUID, conversation_id: UUID) -> str | None:
    """The order number this conversation may see, if any."""
    raw = await limits.client.get(_access_key(tenant_id, conversation_id))
    return raw.decode() if isinstance(raw, bytes) else raw


async def forget(tenant_id: UUID, conversation_id: UUID) -> None:
    """Drop this conversation's order access and pending code ("delete my data")."""
    await limits.client.delete(_access_key(tenant_id, conversation_id))
    await email_codes.forget(CODE_PURPOSE, tenant_id, conversation_id)


async def has_connection(tenant_id: UUID) -> bool:
    """Does the tenant have an order system connected?"""
    async with db.tenant_tx(tenant_id) as conn:
        cur = await conn.execute("SELECT 1 FROM order_connections")
        return await cur.fetchone() is not None


# --- Step 2: lookup (answer now, check in the background) ---------------------------------------

def code_email(company: str, number: str, code: str) -> str:
    """The plain-text email with the one-time code."""
    return (
        "Hi,\n\n"
        f"Here is your code to see order #{number} in the {company} chat:\n\n"
        f"    {code}\n\n"
        f"It works once and expires in {CODE_TTL_SECONDS // 60} minutes. Don't share it with anyone: "
        f"{company} will never ask you for it.\n"
        "If you didn't ask for it, you can ignore this email. Nobody can see your order without the code.\n"
    )


async def send_code(tenant: Tenant, conversation_id: UUID, number: str, email: str) -> None:
    """Background: look the order up; only if the email matches, store a code and email it."""
    try:
        source = await source_for(tenant.id)
        order = await source.find(number) if source else None
        if order is None or order.email != normalize_email(email):
            log.info("order lookup for tenant %s: no match", tenant.slug)
            return
        if not await limits.allow_order_code(tenant.id, _order_key(tenant.id, number)):
            log.warning("order lookup for tenant %s: code limit reached for an order", tenant.slug)
            return
        code = email_codes.new_code()
        await email_codes.store(CODE_PURPOSE, tenant.id, conversation_id, number, code)
        await mailer.send_email(order.email, f"Your code for order #{number}",
                                code_email(tenant.config.company_name, number, code))
    except OrderSourceError as exc:
        log.warning("order lookup for tenant %s failed: %s", tenant.slug, exc)
    except Exception:  # background work: log, never crash the server
        log.exception("order code for tenant %s not sent", tenant.slug)


def _in_background(coro) -> None:
    """Run the lookup after the reply, keeping a reference so it isn't garbage-collected."""
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


async def drain() -> None:
    """Wait for running lookups (tests and shutdown). Waits on unfinished tasks only: a finished
    task's clean-up callback may not have run yet (or never will, if its event loop closed)."""
    while running := [task for task in _background if not task.done()]:
        await asyncio.gather(*running, return_exceptions=True)
    _background.difference_update([task for task in _background if task.done()])


async def lookup(tenant: Tenant, conversation_id: UUID, values: dict, ip: str) -> ScriptedReply:
    """Form `order_lookup`: validate, rate-limit, start the background check, ask for the code."""
    number = normalize_order_number(text_value(values, "order_number"))
    email = normalize_email(text_value(values, "email"))
    if number is None:
        return ScriptedReply(BAD_NUMBER_TEXT, [lookup_form()])
    if not re.fullmatch(EMAIL_PATTERN, email):
        return ScriptedReply(BAD_EMAIL_TEXT, [lookup_form(number)])
    try:
        await limits.check_order_lookup(tenant.id, conversation_id, ip)
    except LimitExceeded as exc:
        return ScriptedReply(str(exc.detail), [])
    _in_background(send_code(tenant, conversation_id, number, email))
    return ScriptedReply(CODE_SENT_TEXT, [code_form()])


# --- Step 3: the code ---------------------------------------------------------------------------

async def verify(tenant: Tenant, conversation_id: UUID, values: dict) -> ScriptedReply:
    """Form `order_code`: check the code; on success grant access and show the order."""
    result = await email_codes.check(CODE_PURPOSE, tenant.id, conversation_id, text_value(values, "code"))
    if result.outcome == "bad_format":
        return ScriptedReply(BAD_CODE_FORMAT_TEXT, [code_form()])
    if result.outcome == "missing":
        return ScriptedReply(NO_CODE_TEXT, [lookup_form()])
    if result.outcome == "too_many":
        return ScriptedReply(TOO_MANY_TEXT, [lookup_form(result.bound)])
    if result.outcome == "wrong":
        return ScriptedReply(WRONG_CODE_TEXT, [code_form()])
    number = result.bound
    await limits.client.set(_access_key(tenant.id, conversation_id), number, ex=ACCESS_TTL_SECONDS)
    log.info("order access granted in tenant %s", tenant.slug)
    order = await fetch(tenant, number)
    if order is None:
        return ScriptedReply(UNAVAILABLE_TEXT, [], stored_text=f"Verified the customer for order #{number}.")
    return ScriptedReply(VERIFIED_TEXT, order_blocks(order, tenant),
                         stored_text=f"Showed order #{number} to the verified customer.", store_blocks=False)


async def verified_order(tenant: Tenant, conversation_id: UUID) -> tuple[str | None, Order | None]:
    """(the order number this chat verified, that order fresh from the shop or None if unreachable)."""
    number = await access_for(tenant.id, conversation_id)
    return number, (await fetch(tenant, number) if number else None)


async def fetch(tenant: Tenant, number: str) -> Order | None:
    """The order, fresh from the shop; None if it can't be reached (logged)."""
    try:
        source = await source_for(tenant.id)
        return await source.find(number) if source else None
    except OrderSourceError as exc:
        log.warning("order fetch for tenant %s failed: %s", tenant.slug, exc)
        return None


# --- One turn -----------------------------------------------------------------------------------

async def handle(tenant: Tenant, conversation_id: UUID, message: str, form: FormSubmission | None,
                 ip: str) -> OrderTurn:
    """Decide what order tracking does this turn: answer a form, offer the lookup form, give the
    model the verified order, or nothing at all (no order system, or not about an order)."""
    if not await has_connection(tenant.id):
        return OrderTurn(reply=ScriptedReply(OFF_TEXT, [])) if form else OrderTurn()
    if form is not None:
        reply = await (lookup(tenant, conversation_id, form.values, ip) if form.name == FORM_LOOKUP
                       else verify(tenant, conversation_id, form.values))
        return OrderTurn(enabled=True, reply=reply)

    asked = order_number_in(message)
    granted = await access_for(tenant.id, conversation_id)
    if granted and asked in (None, granted):
        order = await fetch(tenant, granted)
        if order is not None:
            return OrderTurn(enabled=True, order=order, refs=order_refs(order))
        if looks_like_order_question(message):
            return OrderTurn(enabled=True, reply=ScriptedReply(UNAVAILABLE_TEXT, []))
        return OrderTurn(enabled=True)
    if looks_like_order_question(message):
        return OrderTurn(enabled=True, reply=ScriptedReply(ASK_TEXT, [lookup_form(asked)]))
    return OrderTurn(enabled=True)
