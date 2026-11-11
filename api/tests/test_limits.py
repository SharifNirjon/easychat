"""Rate limits and budgets: forged Origins from curl still can't run up the bill."""
import dataclasses
import os

import pytest
import redis
from conftest import KEY_B, ORIGIN_B
from test_chat import parse_sse, send

import limits
import main


@pytest.fixture
def ip_limit(monkeypatch):
    def set_limit(n: int, hops: int = 0):
        patched = dataclasses.replace(limits.settings, ip_messages_per_minute=n, trusted_proxy_hops=hops)
        monkeypatch.setattr(limits, "settings", patched)
        monkeypatch.setattr(main, "settings", patched)
    return set_limit


async def test_ip_flood_gets_429_before_any_model_call(client, fake_llm, ip_limit):
    ip_limit(3)
    statuses = [(await send(client, f"m{i}")).status_code for i in range(5)]
    assert statuses == [200, 200, 200, 429, 429]
    assert len(fake_llm.calls) == 3


async def test_429_has_retry_after_and_safe_message(client, ip_limit):
    ip_limit(1)
    await send(client)
    r = await send(client)
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 0
    assert "quickly" in r.json()["detail"]


async def test_forged_forwarded_for_is_ignored_without_trusted_proxies(client, ip_limit):
    ip_limit(2)
    statuses = []
    for i in range(4):   # a new fake X-Forwarded-For each time must not reset the counter
        r = await client.post("/chat", json={"site_key": "pk_test_tenant_a", "message": "hi"},
                              headers={"Origin": "https://shop-a.example", "X-Forwarded-For": f"10.0.0.{i}"})
        statuses.append(r.status_code)
    assert statuses == [200, 200, 429, 429]


async def test_trusted_proxy_hop_uses_rightmost_entry(client, ip_limit):
    ip_limit(1, hops=1)
    hdrs = {"Origin": "https://shop-a.example"}
    body = {"site_key": "pk_test_tenant_a", "message": "hi"}
    # Client-forged left entries differ, but the proxy-appended rightmost address is the same.
    r1 = await client.post("/chat", json=body, headers={**hdrs, "X-Forwarded-For": "1.1.1.1, 198.51.100.9"})
    r2 = await client.post("/chat", json=body, headers={**hdrs, "X-Forwarded-For": "2.2.2.2, 198.51.100.9"})
    r3 = await client.post("/chat", json=body, headers={**hdrs, "X-Forwarded-For": "198.51.100.10"})
    assert (r1.status_code, r2.status_code, r3.status_code) == (200, 429, 200)


async def test_daily_token_budget(client, fake_llm, owner_db):
    tenant_id = owner_db.execute("SELECT id FROM tenants WHERE slug = 'tenant-a'").fetchone()[0]
    r = await send(client)
    assert r.status_code == 200
    for task in list(main._background):
        await task
    key = limits._budget_key(tenant_id)
    store = redis.from_url(os.environ["REDIS_URL"])
    assert int(store.get(key)) == 120            # 100 in + 20 out from the stub

    store.set(key, 1_000_000)                    # exhaust tenant-a's daily budget
    r = await send(client)
    assert r.status_code == 429
    assert "daily limit" in r.json()["detail"]


async def test_monthly_conversation_cap(client):
    # tenant-b allows 2 new conversations per month
    statuses = [(await send(client, key=KEY_B, origin=ORIGIN_B)).status_code for _ in range(3)]
    assert statuses == [200, 200, 429]


async def test_conversation_turn_cap(client, ip_limit):
    # tenant-b allows 2 visitor turns per conversation
    cid = parse_sse((await send(client, "1", key=KEY_B, origin=ORIGIN_B)).text)[0][1]["conversation_id"]
    assert (await send(client, "2", conversation_id=cid, key=KEY_B, origin=ORIGIN_B)).status_code == 200
    r = await send(client, "3", conversation_id=cid, key=KEY_B, origin=ORIGIN_B)
    assert r.status_code == 429
    assert "new chat" in r.json()["detail"]


async def test_redis_down_fails_closed(client, monkeypatch, fake_llm):
    async def broken(*_args, **_kwargs):
        raise redis.asyncio.ConnectionError("down")
    monkeypatch.setattr(limits.client, "get", broken)
    monkeypatch.setattr(limits.client, "pipeline", lambda **_: (_ for _ in ()).throw(redis.asyncio.ConnectionError()))
    r = await send(client)
    assert r.status_code == 503
    assert fake_llm.calls == []
