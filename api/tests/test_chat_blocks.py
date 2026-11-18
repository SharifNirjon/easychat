"""UI blocks end to end: /demo -> validate_blocks -> messages.blocks -> SSE `block` events."""
import dataclasses

from conftest import KEY_A, ORIGIN_A
from test_chat import parse_sse, send

import main
from demo import SAMPLES


async def test_demo_streams_text_then_blocks_then_done(client, fake_llm, owner_db):
    """Blocks arrive after the text, and what is streamed is what is stored."""
    r = await send(client, "/demo carousel")
    events = parse_sse(r.text)
    names = [name for name, _ in events]
    assert names[0] == "meta" and names[-1] == "done"
    assert names.index("block") > names.index("token")              # text first, then blocks
    blocks = [data for name, data in events if name == "block"]
    assert [b["type"] for b in blocks] == ["carousel", "chips"]
    assert fake_llm.calls == []                                       # no model call for demos

    stored = owner_db.execute(
        "SELECT blocks FROM messages WHERE role = 'assistant' ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    assert stored == blocks                                           # what was streamed is what was saved


async def test_demo_images_use_the_tenants_allow_listed_host(client):
    """Demo images are built on the tenant's own allowed host."""
    blocks = [d for n, d in parse_sse((await send(client, "/demo carousel")).text) if n == "block"]
    images = [item["image"]["url"] for item in blocks[0]["items"]]
    assert images and all(url.startswith("http://localhost:3001/assets/") for url in images)


async def test_invalid_blocks_never_reach_the_browser_or_the_database(client, owner_db):
    """Invalid blocks are neither streamed nor stored."""
    events = parse_sse((await send(client, "/demo invalid")).text)
    blocks = [data for name, data in events if name == "block"]
    assert [b["type"] for b in blocks] == ["notice"]
    stored = owner_db.execute("SELECT blocks FROM messages WHERE role = 'assistant'").fetchone()[0]
    assert stored == blocks
    assert "javascript:" not in str(stored) and "evil.example" not in str(stored)


async def test_unknown_demo_name_lists_the_demos(client):
    """/demo with an unknown name lists every sample, none dropped."""
    blocks = [d for n, d in parse_sse((await send(client, "/demo nope")).text) if n == "block"]
    labels = [chip["label"] for block in blocks for chip in block["items"]]
    assert len(labels) == len(SAMPLES) and "/demo carousel" in labels   # every demo listed, none dropped


async def test_normal_replies_have_no_blocks(client, fake_llm, owner_db):
    """Without a knowledge base, ordinary replies carry no blocks."""
    events = parse_sse((await send(client, "Hello")).text)
    assert "block" not in [name for name, _ in events]
    assert len(fake_llm.calls) == 1
    assert owner_db.execute("SELECT blocks FROM messages WHERE role = 'assistant'").fetchone()[0] is None


async def test_demo_is_off_outside_local_and_test(client, fake_llm, monkeypatch):
    """In production /demo is just a message for the model."""
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, app_env="production"))
    events = parse_sse((await send(client, "/demo carousel")).text)
    assert "block" not in [name for name, _ in events]
    assert len(fake_llm.calls) == 1                                   # treated as an ordinary message


async def test_config_exposes_home_but_not_image_hosts(client):
    """The home screen is public; the image allow-list is not."""
    body = (await client.get("/config", params={"key": KEY_A}, headers={"Origin": ORIGIN_A})).json()
    assert body["home"]["chips"] == ["One?"]
    assert body["home"]["cards"][0]["title"] == "Track"
    assert "image_hosts" not in body
