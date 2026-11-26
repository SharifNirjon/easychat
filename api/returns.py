"""Return requests (roadmap step 9, widget design 7). Pure functions: no I/O.

A return is a REQUEST to the shop's team: nothing is written to the store, no label is created
and no refund amount is promised (the refund options are preferences, shown without amounts).
Only an order this conversation verified with an emailed code (order_tracking.py) can be
returned, and only once it was delivered and the tenant's return window hasn't closed.

The form lists the order's items as options with ids item_1..item_n; what was offered is kept
server-side (chat_forms.py), so a submission can only pick items, reasons and refund options
that were really shown, and only for that order.
"""
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from blocks import AssetPolicy
from models import ReturnsConfig
from order_sources import Order, OrderItem
from replies import FormValue, list_value, text_value

MAX_ITEM_OPTIONS = 8                       # blocks.ChoiceField's limit
NOTES_MAX_CHARS = 500

# "I want to return…", "how do I return my order", "start a return", "send it back", "refund my order".
_RETURN_INTENT = re.compile(
    r"\b(?:i(?:'d| would)?\s+(?:like|want|need)\s+to|i\s+wanna|how\s+(?:do|can)\s+i|help\s+me|can\s+you\s+help\s+me"
    r"|(?:start|make|open|request|begin)\s+an?)\s+(?:\w+\s+){0,3}?(?:return|refund|exchange|send\s+(?:it|this|them)\s+back)\b"
    r"|\b(?:return|refund|exchange)\s+(?:my|this|an?|the)\s+(?:order|item|items|product|purchase|parcel|package)\b",
    re.IGNORECASE)


def looks_like_return_request(text: str) -> bool:
    """The visitor wants to return something (not: 'what is your return policy?')."""
    return bool(_RETURN_INTENT.search(text))


def start_return_chips() -> dict:
    """A "Start a return" chip under a delivered order's card."""
    return {"type": "chips", "items": [{"label": "Start a return",
                                        "action": {"kind": "send", "text": "I'd like to return something from this order"}}]}


# --- Can this order be returned? ----------------------------------------------------------------

@dataclass(frozen=True)
class Returnable:
    """Whether a return can be requested now; if not, why (visitor-facing)."""
    ok: bool
    reason: str = ""
    deadline: datetime | None = None          # the last moment a return can be asked for


def short_day(value: datetime) -> str:
    """'Nov 28'."""
    return f"{value:%b} {value.day}"


def check_returnable(order: Order, config: ReturnsConfig, now: datetime | None = None) -> Returnable:
    """Delivered orders within the tenant's window can be returned; anything else can't."""
    now = now or datetime.now(UTC)
    if order.status == "cancelled":
        return Returnable(False, f"Order #{order.number} was cancelled, so there's nothing to return.")
    if order.status != "delivered":
        return Returnable(False, f"Order #{order.number} hasn't been delivered yet. Once it arrives, you can "
                                 "ask for a return here.")
    if order.delivered_at is None:
        return Returnable(True)                       # delivered, date unknown: let the team decide
    deadline = order.delivered_at + timedelta(days=config.window_days)
    if now > deadline:
        return Returnable(False, f"The return window for order #{order.number} closed on {short_day(deadline)} "
                                 f"({config.window_days} days after delivery). Our team can still look at it if "
                                 "you ask them.", deadline)
    return Returnable(True, deadline=deadline)


# --- The form (design 7) ------------------------------------------------------------------------

@dataclass(frozen=True)
class OfferedReturn:
    """What the return form offered: kept server-side, checked against the submission."""
    order_number: str
    items: dict[str, dict]           # option id -> {"title", "quantity", "price"}
    reasons: list[str]
    refunds: list[str]

    def to_json(self) -> dict:
        """For Redis (chat_forms.offer)."""
        return {"order_number": self.order_number, "items": self.items, "reasons": self.reasons,
                "refunds": self.refunds}

    @classmethod
    def from_json(cls, data: dict) -> "OfferedReturn":
        """Back from Redis."""
        return cls(data["order_number"], data["items"], data["reasons"], data["refunds"])


def _image(item: OrderItem, policy: AssetPolicy) -> dict | None:
    """The item's thumbnail, only from the tenant's allowed image hosts (else blocks.py would drop
    the whole form)."""
    if not item.image_url:
        return None
    parts = urlsplit(item.image_url)
    return {"url": item.image_url, "alt": item.title[:120]} if parts.netloc.lower() in policy.image_hosts else None


def _item_detail(item: OrderItem, order: Order) -> str:
    """'$26.00 · ×2 · delivered Oct 22'."""
    parts = [item.price] if item.price else []
    if item.quantity > 1:
        parts.append(f"×{item.quantity}")
    if order.delivered_at:
        parts.append(f"delivered {short_day(order.delivered_at)}")
    return " · ".join(parts)[:120]


def return_form(order: Order, config: ReturnsConfig, policy: AssetPolicy, consent: dict,
                privacy_url: str | None) -> tuple[dict, OfferedReturn]:
    """The return form and what it offers. Without item lines from the shop, the visitor
    describes the items in a text box instead."""
    items = order.items[:MAX_ITEM_OPTIONS]
    offered_items = {f"item_{i}": {"title": item.title, "quantity": item.quantity, "price": item.price}
                     for i, item in enumerate(items, start=1)}
    fields: list[dict] = []
    if items:
        options = []
        for (option_id, _), item in zip(offered_items.items(), items, strict=True):
            option = {"id": option_id, "label": item.title}
            if detail := _item_detail(item, order):
                option["detail"] = detail
            if image := _image(item, policy):
                option["image"] = image
            options.append(option)
        fields.append({"kind": "choice", "name": "items", "label": "Items to return", "mode": "multi",
                       "options": options, "required": True})
    else:
        fields.append({"kind": "textarea", "name": "items_text", "label": "Which items?", "required": True,
                       "placeholder": "e.g. the blue mug, 1 of 2"})
    reasons = list(config.reasons)
    refunds = [option.label for option in config.refund_options]
    fields += [
        {"kind": "choice", "name": "reason", "label": "Reason", "mode": "single", "style": "chips", "required": True,
         "options": [{"id": f"reason_{i}", "label": r} for i, r in enumerate(reasons, start=1)]},
        {"kind": "choice", "name": "refund", "label": "Refund as", "mode": "single", "required": True,
         "options": [{"id": f"refund_{i}", "label": o.label, **({"detail": o.detail} if o.detail else {})}
                     for i, o in enumerate(config.refund_options, start=1)]},
        {"kind": "textarea", "name": "notes", "label": "Anything else? (optional)", "required": False},
        consent,
    ]
    block = {"type": "form", "title": f"Return items from order #{order.number}"[:120], "submit": "return_request",
             "submit_label": "Send return request", "fields": fields}
    if privacy_url:
        block["privacy_url"] = privacy_url
    return block, OfferedReturn(order.number, offered_items, reasons, refunds)


def _pick(values: dict[str, FormValue], name: str, labels: list[str], prefix: str) -> str | None:
    """The label of the single option chosen in field `name` ('reason_2' -> its label)."""
    chosen = list_value(values, name)
    if len(chosen) != 1 or not chosen[0].startswith(prefix):
        return None
    index = chosen[0][len(prefix):]
    return labels[int(index) - 1] if index.isdigit() and 1 <= int(index) <= len(labels) else None


def return_payload(offered: OfferedReturn, values: dict[str, FormValue], customer_email: str) -> dict | None:
    """The request to store, built only from what was offered; None if the submission doesn't fit."""
    reason = _pick(values, "reason", offered.reasons, "reason_")
    refund = _pick(values, "refund", offered.refunds, "refund_")
    if reason is None or refund is None:
        return None
    if offered.items:
        chosen = list_value(values, "items")
        if not chosen or any(option_id not in offered.items for option_id in chosen):
            return None                       # an item that was never offered (e.g. from another order)
        items = [offered.items[option_id] for option_id in chosen]
    else:
        described = text_value(values, "items_text")
        if not described:
            return None
        items = [{"title": described[:300], "quantity": 1, "price": None}]
    return {"order_number": offered.order_number, "customer_email": customer_email, "items": items,
            "reason": reason, "refund_preference": refund, "notes": text_value(values, "notes")[:NOTES_MAX_CHARS]}
