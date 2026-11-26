"""UI blocks: every rule in blocks.py has a test. Invalid blocks are dropped, valid ones kept."""
import pytest

from blocks import MAX_BLOCKS_PER_REPLY, AssetPolicy, validate_blocks
from demo import SAMPLES, _Images

POLICY = AssetPolicy(image_hosts=frozenset({"img.shop.example", "localhost:3001"}), allow_local_http=True)
PROD_POLICY = AssetPolicy(image_hosts=frozenset({"img.shop.example"}), allow_local_http=False)

SEND = {"kind": "send", "text": "Hi"}


def chips(*actions: dict) -> dict:
    """A chips block with one chip per given action."""
    return {"type": "chips", "items": [{"label": f"Chip {i}", "action": a} for i, a in enumerate(actions)]}


def card(**overrides) -> dict:
    """A card block titled 'Serum', with any card fields overridden."""
    return {"type": "card", "card": {"title": "Serum", **overrides}}


def kept(block: dict, policy: AssetPolicy = POLICY) -> bool:
    """True if the single block survives validation under `policy`."""
    return len(validate_blocks([block], policy)) == 1


# --- valid input ------------------------------------------------------------------------

@pytest.mark.parametrize("name", [n for n in SAMPLES if n != "invalid"])
def test_every_demo_sample_is_valid(name):
    """The /demo samples are the design reference: all of them must pass."""
    reply = SAMPLES[name](_Images("localhost:3001"))
    assert len(validate_blocks(reply.blocks, POLICY)) == len(reply.blocks)


def test_valid_block_round_trips_without_nulls():
    """Valid input comes back unchanged, with unset optional fields omitted."""
    out = validate_blocks([card(price="$9", rating=4.5)], POLICY)
    assert out == [{"type": "card", "card": {"title": "Serum", "price": "$9", "rating": 4.5}}]


def test_text_is_kept_verbatim_markup_is_not_interpreted_here():
    """Escaping is the widget's job (textContent); the server must not mangle text."""
    # The server stores text as-is; the widget renders it with textContent, never as HTML.
    out = validate_blocks([{"type": "notice", "text": "<script>alert(1)</script>"}], POLICY)
    assert out[0]["text"] == "<script>alert(1)</script>"


def test_order_is_preserved_and_bad_blocks_do_not_take_good_ones_down():
    """One bad block is dropped alone; the rest keep their order."""
    blocks = [{"type": "notice", "text": "one"}, {"type": "nope"}, {"type": "notice", "text": "two"}]
    assert [b["text"] for b in validate_blocks(blocks, POLICY)] == ["one", "two"]


def test_demo_invalid_sample_keeps_only_its_valid_block():
    """The /demo invalid sample shows the gate working: only its notice survives."""
    reply = SAMPLES["invalid"](_Images(None))
    assert [b["type"] for b in validate_blocks(reply.blocks, POLICY)] == ["notice"]


# --- structure ------------------------------------------------------------------------------

@pytest.mark.parametrize("block", [
    {"type": "marquee", "text": "unknown type"},
    {"text": "missing type"},
    {"type": "notice", "text": "hi", "onclick": "alert(1)"},                 # unknown field
    {"type": "chips", "items": [{"label": "x", "action": {"kind": "eval", "text": "1"}}]},  # unknown action
    {"type": "chips", "items": []},                                           # too few
    chips(*[SEND] * 9),                                                       # too many
    {"type": "notice", "text": "x" * 401},                                    # too long
    {"type": "carousel", "items": [{"title": "only one"}]},                   # carousel needs 2+
    card(rating=7),                                                           # out of range
    {"type": "choice_list", "mode": "maybe", "question": "?", "options": [{"id": "a", "label": "A"}] * 2},
    {"type": "form", "submit_label": "Go", "fields": [{"kind": "text", "name": "Bad Name", "label": "x"}]},
    "not even an object",
])
def test_invalid_structure_is_dropped(block):
    """Unknown types/fields/actions and out-of-range values never pass."""
    assert not kept(block)


def test_reply_is_capped_at_max_blocks():
    """A reply carries at most MAX_BLOCKS_PER_REPLY blocks."""
    many = [{"type": "notice", "text": str(i)} for i in range(MAX_BLOCKS_PER_REPLY + 3)]
    assert len(validate_blocks(many, POLICY)) == MAX_BLOCKS_PER_REPLY


def test_reply_is_capped_by_total_size():
    """A reply's blocks stop at MAX_BLOCKS_BYTES, however few there are."""
    item = {"title": "t" * 120, "subtitle": "s" * 120, "badge": "b" * 24, "reviews": "r" * 24, "price": "p" * 20}
    fat = {"type": "carousel", "items": [item] * 10}      # ~3.8 KB each; five exceed MAX_BLOCKS_BYTES
    out = validate_blocks([fat] * 5, POLICY)
    assert 0 < len(out) < 5


# --- URLs -----------------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "http://shop.example/page",              # plain http to a real host
    "https://user:pass@shop.example/",       # credentials
    "https://",                              # no host
    "https://shop.example/a b",              # whitespace
    "https://shop.example/" + "a" * 600,     # too long
])
def test_bad_links_are_rejected(url):
    """Script URLs, plain http, credentials and malformed URLs are refused."""
    assert not kept(chips({"kind": "link", "url": url}))


def test_https_link_is_allowed():
    """An ordinary https link with a query string is fine."""
    assert kept(chips({"kind": "link", "url": "https://shop.example/page?x=1"}))


def test_local_http_only_in_dev():
    """http://localhost works in local/test and nowhere else."""
    local = chips({"kind": "link", "url": "http://localhost:3000/page"})
    assert kept(local, POLICY)
    assert not kept(local, PROD_POLICY)


def test_images_only_from_allow_listed_hosts():
    """Images (tracking pixels) only load from hosts the tenant allowed."""
    assert kept(card(image={"url": "https://img.shop.example/a.png"}))
    assert not kept(card(image={"url": "https://evil.example/pixel.gif"}))      # tracking pixel
    assert not kept(card(image={"url": "https://IMG.shop.example.evil.example/a.png"}))


def test_image_host_match_includes_port():
    """The allow-list matches host AND port."""
    assert kept(card(image={"url": "http://localhost:3001/a.svg"}))
    assert not kept(card(image={"url": "http://localhost:9999/a.svg"}))


# --- chat forms (step 9) ------------------------------------------------------------------------

def form(*fields: dict, **extra) -> dict:
    """A submittable form with the given fields."""
    return {"type": "form", "submit": "lead", "submit_label": "Send", "fields": list(fields), **extra}


CHOICE = {"kind": "choice", "name": "items", "label": "Items", "mode": "multi",
          "options": [{"id": "item_1", "label": "Squalane Day Gel", "detail": "$26.00",
                       "image": {"url": "https://img.shop.example/gel.png"}}]}
CONSENT = {"kind": "checkbox", "name": "consent", "required": True,
           "label": "I agree to Mirela storing the details above to answer this request. They're deleted after 90 days."}


def test_choice_fields_consent_boxes_and_summary_cards_are_valid():
    """Design 7's item list, W-07's consent box (long label, required) and W-08's card."""
    assert kept(form(CHOICE, CONSENT, privacy_url="https://shop.example/privacy"))
    assert kept({"type": "summary", "title": "Sent to the Mirela team", "footer": "We reply by email.",
                 "rows": [{"label": "Name", "value": "Rafiq Hasan"}, {"label": "Summary", "value": "x" * 400}]})


@pytest.mark.parametrize("block", [
    form({**CHOICE, "mode": "some"}),                                                     # unknown mode
    form({**CHOICE, "options": []}),                                                      # no options
    form({**CHOICE, "options": [{"id": "Item 1", "label": "x"}]}),                        # bad option id
    form({**CHOICE, "options": [{"id": "a", "label": "x", "image": {"url": "https://evil.example/x.png"}}]}),
    form(CHOICE, privacy_url="javascript:alert(1)"),                                      # unsafe link
    form(CHOICE, submit="send_money"),                                                    # unknown form
    {"type": "summary", "title": "x", "rows": []},                                        # card without rows
    {"type": "summary", "title": "x", "rows": [{"label": "a", "value": "x" * 401}]},      # value too long
])
def test_invalid_chat_form_blocks_are_dropped(block):
    """Unknown modes/forms, foreign images and unsafe links never reach the widget."""
    assert not kept(block)
