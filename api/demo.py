"""Local-only sample replies: type `/demo <name>` in the chat (APP_ENV local/test only).

Each sample goes through the real pipeline (validate_blocks() -> messages table -> SSE `block`
events -> widget) without calling the model, so every block type can be checked against its
design in docs/designs/widget designs/. main.py never routes here outside local/test.

The shop and products are fictional ("Mirela"); images are local placeholders served by the
widget dev server, so they only appear for tenants that allow-list that host.
"""
from collections.abc import Callable
from dataclasses import dataclass

from blocks import LOCAL_HOSTS

DEMO_COMMAND = "/demo"
HELP_CHIPS_PER_BLOCK = 5   # a chips block holds at most 8 items (blocks.ChipsBlock)


@dataclass(frozen=True)
class DemoReply:
    """A canned reply: the text to stream and the raw blocks to validate and send."""
    text: str
    blocks: list[dict]


def _send(text: str) -> dict:
    """Action that posts `text` as the visitor's next message."""
    return {"kind": "send", "text": text}


def _chips(*labels: str) -> dict:
    """A chips block whose chips each send their own label."""
    return {"type": "chips", "items": [{"label": label, "action": _send(label)} for label in labels]}


class _Images:
    """Builds placeholder image refs on the tenant's first allow-listed host (or none at all)."""

    def __init__(self, host: str | None):
        """Remember the base URL for placeholder images (None = no images at all)."""
        self.base = None
        if host:
            scheme = "http" if host.split(":")[0] in LOCAL_HOSTS else "https"
            self.base = f"{scheme}://{host}/assets"

    def __call__(self, name: str, alt: str) -> dict | None:
        """Image reference for a placeholder file, or None when there is no image host."""
        return {"url": f"{self.base}/{name}.svg", "alt": alt} if self.base else None


def _without_none(card: dict) -> dict:
    """Drop None values so optional card fields are simply absent."""
    return {key: value for key, value in card.items() if value is not None}


def _carousel(img: _Images) -> DemoReply:
    """Design 1/8: three product cards to swipe, plus follow-up chips."""
    products = [
        ("Glow Drops Vitamin C Serum", "Mirela · orange extract", "Bestseller", "serum-orange", 4.8, "1.2k reviews", "$12"),
        ("Clear Pore Niacinamide 10%", "Mirela Lab · 30 ml", None, "serum-clear", 4.6, "640 reviews", "$9"),
        ("Licorice Bright Essence", "Mirela · 50 ml", "New", "essence-jar", 4.5, "210 reviews", "$16"),
    ]
    cards = [
        _without_none({
            "title": title, "subtitle": subtitle, "badge": badge, "image": img(image, title),
            "rating": rating, "reviews": reviews, "price": price,
            "action": _send(f"Tell me more about {title}"),
        })
        for title, subtitle, badge, image, rating, reviews, price in products
    ]
    return DemoReply(
        "Here are 3 serums for dullness and pores — all under $20:",
        [{"type": "carousel", "items": cards}, _chips("Compare these", "Fragrance-free", "Cheaper options", "Why these?")],
    )


def _card(img: _Images) -> DemoReply:
    """Design 13: one product card on its own."""
    card = _without_none({
        "title": "Clear Pore Niacinamide 10%", "subtitle": "Mirela Lab · 30 ml · in stock",
        "image": img("serum-clear", "Clear Pore Niacinamide serum"), "rating": 4.8,
        "reviews": "1,240 reviews", "price": "$9", "action": _send("Is it good for sensitive skin?"),
    })
    return DemoReply("Here's the one you asked about:", [{"type": "card", "card": card}])


def _quiz(_: _Images) -> DemoReply:
    """Design 2: a single-choice question with a step-progress header."""
    return DemoReply("Let's build your routine — 4 quick questions.", [{
        "type": "choice_list", "mode": "single", "step": {"current": 2, "total": 4},
        "question": "By midday, how does your skin feel?",
        "options": [
            {"id": "tight", "label": "Tight or flaky"},
            {"id": "tzone", "label": "Shiny on the T-zone only"},
            {"id": "shiny", "label": "Shiny all over"},
            {"id": "fine", "label": "Comfortable — no change"},
        ],
        "submit_label": "Next",
    }])


def _returns(img: _Images) -> DemoReply:
    """Design 7: pick items to return (multi-select with thumbnails)."""
    options = [
        _without_none({"id": "day_gel", "label": "Squalane Day Gel", "detail": "$26 · delivered Oct 22",
                       "image": img("gel-tube", "Squalane Day Gel")}),
        _without_none({"id": "glow_drops", "label": "Glow Drops Vitamin C Serum", "detail": "$12 · delivered Oct 22",
                       "image": img("serum-orange", "Glow Drops serum")}),
    ]
    return DemoReply("Sure. Which item from order #MR-20311 are you returning? Both are eligible until Nov 28.", [
        {"type": "choice_list", "mode": "multi", "question": "Items to return", "options": options,
         "submit_label": "Continue"},
    ])


def _timeline(_: _Images) -> DemoReply:
    """Design 6: order tracking timeline, plus follow-up buttons."""
    return DemoReply("Found it — here's where your order is:", [
        {
            "type": "timeline", "eyebrow": "Order #MR-20417", "title": "Arriving today by 6 pm",
            "status": {"label": "Out for delivery", "tone": "warning"},
            "steps": [
                {"label": "Ordered", "detail": "Oct 28 · 2 items", "state": "done"},
                {"label": "Packed", "detail": "Oct 29", "state": "done"},
                {"label": "Shipped", "detail": "Oct 30 · Dhaka hub", "state": "done"},
                {"label": "Out for delivery", "detail": "Today, 9:12 am", "state": "current"},
                {"label": "Delivered", "state": "upcoming"},
            ],
            "footer": "Courier · PTH-88213",
            "link": {"kind": "link", "url": "https://example.com/track/PTH-88213"},
        },
        {"type": "buttons", "items": [
            {"label": "Change delivery address", "action": _send("I want to change my delivery address")},
            {"label": "Talk to a person", "action": _send("I want to talk to a person")},
        ]},
    ])


def _form(_: _Images) -> DemoReply:
    """Designs 8 and 10: a back-in-stock form and a review form."""
    return DemoReply("Not yet — it sold out on Oct 20. Want a message when it's back?", [
        {"type": "form", "title": "Glow Drops Vitamin C Serum · sold out", "submit_label": "Notify me", "fields": [
            {"kind": "email", "name": "email", "label": "Email", "placeholder": "you@example.com", "required": True},
            {"kind": "checkbox", "name": "launches", "label": "Also tell me about new launches"},
        ]},
        {"type": "form", "title": "How's your Oat Barrier Cream?", "submit_label": "Post review", "fields": [
            {"kind": "rating", "name": "stars", "label": "Your rating"},
            {"kind": "tags", "name": "liked", "label": "What stood out?",
             "options": ["Hydrating", "No breakouts", "Gentle", "Absorbs fast", "Pricey"]},
            {"kind": "textarea", "name": "review", "label": "Your review", "placeholder": "Tell other shoppers…"},
        ]},
    ])


def _sources(_: _Images) -> DemoReply:
    """Design 11: an honest 'can't say' answer with sources, a notice and chips."""
    return DemoReply(
        "I can't say whether it's safe during pregnancy — that's a question for your doctor. "
        "Here's the full ingredient list so you can check it with them first.",
        [
            {"type": "sources", "items": [
                {"title": "Glow Drops Vitamin C Serum · ingredients", "url": "https://example.com/ingredients"},
                {"title": "Pregnancy & skincare FAQ", "url": "https://example.com/help"},
            ]},
            {"type": "notice", "tone": "info",
             "text": "I answer from the shop's product data only. For skin conditions, reactions or medication "
                     "questions, ask a doctor or our care team."},
            _chips("See full ingredients", "Talk to a person", "Fragrance-free picks"),
        ],
    )


def _buttons(_: _Images) -> DemoReply:
    """Buttons that send a message or open a page."""
    return DemoReply("Buttons can send a message or open a page:", [{"type": "buttons", "items": [
        {"label": "Start a project", "style": "primary", "action": _send("I want to start a project")},
        {"label": "Open the website", "action": {"kind": "link", "url": "https://example.com"}},
    ]}])


def _invalid(_: _Images) -> DemoReply:
    """Four blocks, three of them invalid: only the notice should reach the widget."""
    return DemoReply("This reply was sent with 4 blocks; the server should drop 3 of them.", [
        {"type": "notice", "text": "I'm the only valid block in this reply."},
        {"type": "marquee", "text": "unknown block type"},
        {"type": "chips", "items": [{"label": "Click me", "action": {"kind": "link", "url": "javascript:alert(1)"}}]},
        {"type": "card", "card": {"title": "Tracking pixel", "image": {"url": "https://evil.example/pixel.gif"}}},
    ])


SAMPLES: dict[str, Callable[[_Images], DemoReply]] = {
    "carousel": _carousel, "card": _card, "quiz": _quiz, "returns": _returns, "timeline": _timeline,
    "form": _form, "sources": _sources, "buttons": _buttons, "invalid": _invalid,
}


def demo_reply(message: str, image_host: str | None) -> DemoReply | None:
    """The sample for `/demo <name>`, a help reply for an unknown name, or None if not a demo command."""
    words = message.strip().split()
    if not words or words[0].lower() != DEMO_COMMAND:
        return None
    build = SAMPLES.get(words[1].lower()) if len(words) > 1 else None
    if build is None:
        commands = [f"{DEMO_COMMAND} {name}" for name in SAMPLES]
        groups = [commands[i:i + HELP_CHIPS_PER_BLOCK] for i in range(0, len(commands), HELP_CHIPS_PER_BLOCK)]
        return DemoReply("Pick a demo:", [_chips(*group) for group in groups])
    return build(_Images(image_host))
