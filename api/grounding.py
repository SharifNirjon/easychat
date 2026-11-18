"""Grounded answers: put retrieved knowledge in the prompt, and build the reply's blocks from it.

Two rules keep this safe (CLAUDE.md rule 5):
  * Crawled text reaches the model only inside a <knowledge> fence, described to the model as
    reference data; anything in it that looks like our own fence tags is defused first.
  * Every block (sources, product cards, fallback) is built HERE, by code, from database rows,
    never from model output, so the model can't invent a link, a price or an image. The blocks
    still pass through blocks.validate_blocks() like any other.

The model signals "the knowledge doesn't answer this" by ending its reply with NO_ANSWER_MARKER.
MarkerFilter removes the marker from the stream, so the visitor never sees it; the reply then
gets the tenant's fallback blocks instead of sources.
"""
import re
from urllib.parse import urlsplit

from blocks import LOCAL_HOSTS, AssetPolicy
from models import Fallback
from retrieval import Hit

NO_ANSWER_MARKER = "[[NO_ANSWER]]"
MAX_SOURCES = 3
MAX_PRODUCT_CARDS = 6
CURRENCY_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "BDT": "৳", "INR": "₹", "JPY": "¥"}

_FENCE_TAG = re.compile(r"<\s*/?\s*knowledge", re.IGNORECASE)


# --- Prompt -------------------------------------------------------------------------------------

def _defuse(text: str) -> str:
    """Crawled text can't close or reopen our <knowledge> fence."""
    return _FENCE_TAG.sub(lambda m: m.group(0).replace("<", "‹"), text)


def knowledge_block(hits: list[Hit]) -> str:
    """The retrieved chunks as one fenced <knowledge> section for the system prompt."""
    if not hits:
        # Said plainly: weaker models otherwise fill the gap from general knowledge.
        return ("<knowledge>\n(nothing relevant was found for this question: unless it is small talk, "
                "you don't know the answer)\n</knowledge>")
    parts = [f"--- {'Article' if hit.kind == 'article' else 'Page'}: {_defuse(hit.title or hit.url)} ---\n"
             f"{_defuse(hit.content)}" for hit in hits]
    return "<knowledge>\n" + "\n\n".join(parts) + "\n</knowledge>"


# --- Stream filter ------------------------------------------------------------------------------

class MarkerFilter:
    """Strips NO_ANSWER_MARKER from streamed text, even when it arrives split across chunks."""

    def __init__(self, marker: str = NO_ANSWER_MARKER):
        """Start with nothing held back and the marker not yet seen."""
        self.marker = marker
        self.found = False
        self._buffer = ""

    def feed(self, text: str) -> str:
        """Return the text that is safe to send now; hold back a possible start of the marker."""
        self._buffer += text
        if self.marker in self._buffer:
            self.found = True
            self._buffer = self._buffer.replace(self.marker, "")
        hold = 0
        for size in range(min(len(self.marker) - 1, len(self._buffer)), 0, -1):
            if self._buffer.endswith(self.marker[:size]):
                hold = size
                break
        ready, self._buffer = self._buffer[:len(self._buffer) - hold], self._buffer[len(self._buffer) - hold:]
        return ready

    def flush(self) -> str:
        """The held-back text at the end of the stream (it wasn't the marker after all)."""
        rest, self._buffer = self._buffer, ""
        return rest


# --- Blocks -------------------------------------------------------------------------------------

def _cut(text: str, limit: int) -> str:
    """Collapse whitespace and shorten to `limit` characters, ending with an ellipsis if cut."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _price(price: str | None, currency: str | None) -> str | None:
    """'12' + 'USD' -> '$12' (or '12 XYZ' for unknown currencies); None if too long for a card."""
    if not price:
        return None
    symbol = CURRENCY_SYMBOLS.get((currency or "").upper())
    label = f"{symbol}{price}" if symbol else f"{price} {currency or ''}".strip()
    return label if len(label) <= 20 else None


def _allowed_image(url: str | None, policy: AssetPolicy) -> str | None:
    """Keep a product image only if its host is allow-listed: otherwise the whole card would be
    rejected by validation, when dropping just the picture is enough."""
    if not url:
        return None
    parts = urlsplit(url)
    secure = parts.scheme == "https" or (policy.allow_local_http and parts.hostname in LOCAL_HOSTS)
    return url if secure and parts.netloc.lower() in policy.image_hosts else None


def product_card(row: dict, policy: AssetPolicy) -> dict:
    """A product row from the database -> a card dict for the card/carousel blocks."""
    card: dict = {
        "title": _cut(row["name"], 120),
        "action": {"kind": "send", "text": _cut(f"Tell me more about {row['name']}", 200)},
    }
    if row.get("brand"):
        card["subtitle"] = _cut(row["brand"], 120)
    if row.get("availability") and row["availability"] != "In stock":
        card["badge"] = _cut(row["availability"], 24)
    image = _allowed_image(row.get("image_url"), policy)
    if image:
        card["image"] = {"url": image, "alt": _cut(row["name"], 120)}
    if row.get("rating") is not None:
        card["rating"] = round(float(row["rating"]), 1)
    if row.get("review_count"):
        card["reviews"] = _cut(f"{row['review_count']:,} reviews", 24)
    price = _price(row.get("price"), row.get("currency"))
    if price:
        card["price"] = price
    return card


def product_blocks(rows: list[dict], policy: AssetPolicy) -> list[dict]:
    """One product -> a card block; several -> a carousel; none -> nothing."""
    cards = [product_card(row, policy) for row in rows[:MAX_PRODUCT_CARDS]]
    if not cards:
        return []
    if len(cards) == 1:
        return [{"type": "card", "card": cards[0]}]
    return [{"type": "carousel", "items": cards}]


def sources_block(hits: list[Hit]) -> list[dict]:
    """The distinct pages behind the answer, best first."""
    items, seen = [], set()
    for hit in hits:
        if hit.url in seen:
            continue
        seen.add(hit.url)
        items.append({"title": _cut(hit.title or hit.url, 120), "url": hit.url})
        if len(items) == MAX_SOURCES:
            break
    return [{"type": "sources", "items": items}] if items else []


def fallback_blocks(fallback: Fallback | None) -> list[dict]:
    """The tenant's 'I don't know' blocks: a notice, plus contact/handoff chips if set."""
    if fallback is None:
        return []
    blocks: list[dict] = [{"type": "notice", "tone": "info", "text": fallback.text}]
    chips = []
    if fallback.contact_url:
        chips.append({"label": fallback.contact_label, "action": {"kind": "link", "url": str(fallback.contact_url)}})
    if fallback.handoff_message:
        chips.append({"label": fallback.handoff_label, "action": {"kind": "send", "text": fallback.handoff_message}})
    if chips:
        blocks.append({"type": "chips", "items": chips})
    return blocks
