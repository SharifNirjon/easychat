"""UI blocks: the structured parts of an assistant reply (cards, chips, lists, forms...).

A reply is text plus an optional list of blocks. Blocks may come from tenant config, from our
own code and, later, from the model, so ALL of them are treated as untrusted: every block goes
through validate_blocks() before it is stored or sent to a browser. An invalid block is dropped
and logged; it never reaches the widget.

Rules enforced here (see tests/test_blocks.py):
  * a fixed catalogue of block types; unknown types and unknown fields are rejected
  * every text has a maximum length, every list a maximum number of items
  * links are https (plain http only for localhost, and only in local/test)
  * images additionally come only from the tenant's allow-listed hosts
  * a reply carries at most MAX_BLOCKS_PER_REPLY blocks and MAX_BLOCKS_BYTES of JSON
"""
import json
import logging
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, ValidationInfo

log = logging.getLogger("easychat.blocks")

MAX_BLOCKS_PER_REPLY = 6
MAX_BLOCKS_BYTES = 16_000
MAX_URL_CHARS = 500
LOCAL_HOSTS = ("localhost", "127.0.0.1")

# Icons the widget ships (widget/src/icons.ts). Anything else is rejected.
Icon = Literal[
    "sparkle", "truck", "return", "gift", "tag", "box", "grid", "calculator", "calendar",
    "play", "chat", "star", "info", "file", "check", "bell",
]
Tone = Literal["info", "success", "warning", "danger"]

# Text lengths: short labels, one-liners, and paragraphs.
Label = Annotated[str, Field(min_length=1, max_length=40)]
Line = Annotated[str, Field(min_length=1, max_length=120)]
Paragraph = Annotated[str, Field(min_length=1, max_length=400)]
FieldName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")]


@dataclass(frozen=True)
class AssetPolicy:
    """Per-tenant URL rules, passed to validation as context."""
    image_hosts: frozenset[str]          # "host" or "host:port", lowercase
    allow_local_http: bool = False       # local/test only


def _url_parts(value: str, info: ValidationInfo) -> tuple[AssetPolicy, str, str]:
    """Check the rules every block URL shares; return (policy, "host[:port]", hostname).

    The tenant's AssetPolicy arrives through pydantic's validation context (see validate_blocks).
    """
    policy = (info.context or {}).get("policy")
    if not isinstance(policy, AssetPolicy):
        raise ValueError("no asset policy given")  # programming error: always validate with a policy
    if len(value) > MAX_URL_CHARS or any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise ValueError("invalid url")
    parts = urlsplit(value)
    if parts.username or parts.password or not parts.hostname:
        raise ValueError("url must have a host and no credentials")
    local_http = parts.scheme == "http" and policy.allow_local_http and parts.hostname in LOCAL_HOSTS
    if parts.scheme != "https" and not local_http:
        raise ValueError("url must be https")
    return policy, parts.netloc.lower(), parts.hostname


def _check_link(value: str, info: ValidationInfo) -> str:
    """A link the visitor may click: any host, as long as the shared URL rules pass."""
    _url_parts(value, info)
    return value


def _check_image(value: str, info: ValidationInfo) -> str:
    """An image loads by itself (no click), so its host must also be on the tenant's allow-list."""
    policy, netloc, _ = _url_parts(value, info)
    if netloc not in policy.image_hosts:
        raise ValueError(f"image host {netloc!r} is not allowed for this tenant")
    return value


LinkUrl = Annotated[str, AfterValidator(_check_link)]
ImageUrl = Annotated[str, AfterValidator(_check_image)]


class Strict(BaseModel):
    """Base for every block part: unknown fields are an error, not silently ignored."""
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- Actions: what happens when the visitor taps something --------------------------------

class SendAction(Strict):
    """Post `text` as the visitor's next message."""
    kind: Literal["send"]
    text: Annotated[str, Field(min_length=1, max_length=200)]


class LinkAction(Strict):
    """Open `url` in a new tab."""
    kind: Literal["link"]
    url: LinkUrl


Action = Annotated[SendAction | LinkAction, Field(discriminator="kind")]


class Image(Strict):
    """A picture: URL (allow-listed host) and alt text for screen readers."""
    url: ImageUrl
    alt: Annotated[str, Field(max_length=120)] = ""


# --- Block types --------------------------------------------------------------------------

class Chip(Strict):
    """One quick-reply pill: a short label and what tapping it does."""
    label: Label
    action: Action


class ChipsBlock(Strict):
    """Quick replies under a message (designs 1, 4, 11)."""
    type: Literal["chips"]
    items: Annotated[list[Chip], Field(min_length=1, max_length=8)]


class Button(Strict):
    """A full button; `primary` is the filled main action, `secondary` the outlined one."""
    label: Label
    style: Literal["primary", "secondary"] = "secondary"
    action: Action


class ButtonsBlock(Strict):
    """One to three buttons in a row (e.g. "Change address", "Talk to a person")."""
    type: Literal["buttons"]
    items: Annotated[list[Button], Field(min_length=1, max_length=3)]


class Card(Strict):
    """A product/service card (designs 1, 8, 13)."""
    title: Line
    subtitle: Line | None = None
    badge: Annotated[str, Field(min_length=1, max_length=24)] | None = None
    image: Image | None = None
    rating: Annotated[float, Field(ge=0, le=5)] | None = None
    reviews: Annotated[str, Field(max_length=24)] | None = None   # preformatted, e.g. "1.2k reviews"
    price: Annotated[str, Field(max_length=20)] | None = None     # preformatted, e.g. "$12"
    action: Action | None = None


class CardBlock(Strict):
    """A single card on its own (one product or service)."""
    type: Literal["card"]
    card: Card


class CarouselBlock(Strict):
    """Horizontally swipeable cards."""
    type: Literal["carousel"]
    items: Annotated[list[Card], Field(min_length=2, max_length=10)]


class Step(Strict):
    """"Step 2 of 4" progress for a multi-question flow."""
    current: Annotated[int, Field(ge=1, le=20)]
    total: Annotated[int, Field(ge=1, le=20)]


class Choice(Strict):
    """One option in a choice list: stable id, label, optional detail line and thumbnail."""
    id: FieldName
    label: Line
    detail: Line | None = None
    image: Image | None = None


class ChoiceListBlock(Strict):
    """Radio (single) or checkbox (multi) list; submitting sends the chosen labels (designs 2, 7)."""
    type: Literal["choice_list"]
    mode: Literal["single", "multi"]
    question: Paragraph
    step: Step | None = None
    options: Annotated[list[Choice], Field(min_length=2, max_length=8)]
    submit_label: Label = "Next"


class TimelineStep(Strict):
    """One point on a timeline: done, current (highlighted) or still upcoming."""
    label: Line
    detail: Line | None = None
    state: Literal["done", "current", "upcoming"]


class Status(Strict):
    """The coloured status pill on a timeline ("Out for delivery")."""
    label: Label
    tone: Tone = "info"


class TimelineBlock(Strict):
    """Progress like order tracking (design 6)."""
    type: Literal["timeline"]
    eyebrow: Label | None = None        # e.g. "Order #MR-20417"
    title: Line
    status: Status | None = None
    steps: Annotated[list[TimelineStep], Field(min_length=1, max_length=8)]
    footer: Line | None = None
    link: LinkAction | None = None


class TextField(Strict):
    """A one-line text, email, or multi-line (textarea) form field."""
    kind: Literal["text", "email", "textarea"]
    name: FieldName
    label: Label
    placeholder: Annotated[str, Field(max_length=80)] = ""
    required: bool = False


class CheckboxField(Strict):
    """A single yes/no checkbox ("Also tell me about new launches")."""
    kind: Literal["checkbox"]
    name: FieldName
    label: Line


class RatingField(Strict):
    """A 1-5 star rating input."""
    kind: Literal["rating"]
    name: FieldName
    label: Label


class TagsField(Strict):
    """Multi-select chips inside a form (design 10, "what stood out?")."""
    kind: Literal["tags"]
    name: FieldName
    label: Label
    options: Annotated[list[Label], Field(min_length=1, max_length=8)]


FormField = Annotated[TextField | CheckboxField | RatingField | TagsField, Field(discriminator="kind")]


class FormBlock(Strict):
    """A small form (designs 8, 10). Rendered now; submitting is wired up in roadmap step 9."""
    type: Literal["form"]
    title: Line | None = None
    fields: Annotated[list[FormField], Field(min_length=1, max_length=6)]
    submit_label: Label


class Source(Strict):
    """One page an answer came from: its title and https URL."""
    title: Line
    url: LinkUrl


class SourcesBlock(Strict):
    """Pages the answer came from (design 11)."""
    type: Literal["sources"]
    items: Annotated[list[Source], Field(min_length=1, max_length=5)]


class NoticeBlock(Strict):
    """A short coloured note (info, success, warning, danger), e.g. the "I don't know" fallback."""
    type: Literal["notice"]
    tone: Tone = "info"
    text: Paragraph


Block = Annotated[
    ChipsBlock | ButtonsBlock | CardBlock | CarouselBlock | ChoiceListBlock | TimelineBlock
    | FormBlock | SourcesBlock | NoticeBlock,
    Field(discriminator="type"),
]
_block_adapter: TypeAdapter[Block] = TypeAdapter(Block)


def validate_blocks(raw_blocks: list[Any], policy: AssetPolicy) -> list[dict]:
    """Return the valid blocks as JSON-ready dicts, in order. Invalid ones are dropped and logged.

    Never raises for bad input: a broken block must not break the reply it belongs to.
    """
    valid: list[dict] = []
    used_bytes = 0
    for index, raw in enumerate(raw_blocks[:MAX_BLOCKS_PER_REPLY]):
        try:
            block = _block_adapter.validate_python(raw, context={"policy": policy})
        except ValidationError as exc:
            # Log the rule that failed, not the content (it may contain visitor data).
            log.warning("dropped block %d: %s", index, "; ".join(e["msg"] for e in exc.errors()[:3]))
            continue
        data = block.model_dump(mode="json", exclude_none=True)
        size = len(json.dumps(data, separators=(",", ":")).encode())
        if used_bytes + size > MAX_BLOCKS_BYTES:
            log.warning("dropped block %d: reply exceeds %d bytes of blocks", index, MAX_BLOCKS_BYTES)
            continue
        used_bytes += size
        valid.append(data)
    if len(raw_blocks) > MAX_BLOCKS_PER_REPLY:
        log.warning("dropped %d blocks over the per-reply limit", len(raw_blocks) - MAX_BLOCKS_PER_REPLY)
    return valid
