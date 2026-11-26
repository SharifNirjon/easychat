"""Connecting an order system from the onboarding app (order_connections.py)."""
import pytest
from test_auth import APP, outbox, sign_in  # noqa: F401  (fixture: captured emails)
from test_order_sources import SHOP, shop  # noqa: F401  (fixture: fake shop)

from order_connections import API_SECRET_PREFIX

LOCAL_API = "https://orders.shop-a.example/orders"


@pytest.fixture
async def owner(client, outbox):  # noqa: F811
    """A signed-in owner (the client keeps the session cookie)."""
    await sign_in(client, outbox)
    return client


async def test_connecting_an_api_shows_the_secret_once(owner, owner_db):
    """The new secret is in the connect answer only: not in the state, not readable in the database."""
    response = await owner.put("/api/onboarding/orders/api", json={"url": LOCAL_API}, headers=APP)
    assert response.status_code == 200, response.text
    secret = response.json()["secret"]
    assert secret.startswith(API_SECRET_PREFIX) and len(secret) > 40
    state = (await owner.get("/api/onboarding")).text
    assert secret not in state and '"kind":"api"' in state.replace(" ", "")
    stored = owner_db.execute("SELECT credentials FROM order_connections").fetchone()[0]
    assert secret.encode() not in bytes(stored)


@pytest.mark.parametrize("url", ["http://orders.shop-a.example/orders", "ftp://x.example/a",
                                 "https://user:pw@orders.example/x", "not a url at all"])
async def test_order_api_must_be_https(owner, url):
    """Plain http (except localhost in dev), credentials in the URL, or nonsense: 422."""
    response = await owner.put("/api/onboarding/orders/api", json={"url": url}, headers=APP)
    assert response.status_code == 422


async def test_shopify_is_checked_before_it_is_saved(owner, shop, owner_db):  # noqa: F811
    """Good client ID + secret: saved. Shopify refusing: 422 and nothing stored."""
    bad = await owner.put("/api/onboarding/orders/shopify", headers=APP, json={
        "shop": SHOP, "client_id": "client-id-123", "client_secret": "wrong-secret"})
    assert bad.status_code == 422 and "Shopify didn't accept" in bad.json()["detail"]
    assert owner_db.execute("SELECT count(*) FROM order_connections").fetchone()[0] == 0
    good = await owner.put("/api/onboarding/orders/shopify", headers=APP, json={
        "shop": f"https://{SHOP.upper()}/", "client_id": "client-id-123", "client_secret": "client-secret-456"})
    assert good.status_code == 200 and good.json()["orders"]["endpoint"] == SHOP
    assert "client-secret-456" not in good.text


async def test_only_myshopify_domains_are_accepted(owner, shop):  # noqa: F811
    """Credentials are never sent to an arbitrary host."""
    response = await owner.put("/api/onboarding/orders/shopify", headers=APP, json={
        "shop": "evil.example", "access_token": "shpat_legacy"})
    assert response.status_code == 422 and not shop.calls


async def test_the_owner_can_test_a_lookup(owner, shop):  # noqa: F811
    """A test lookup shows found/status (never the customer's email) and records the check."""
    response = await owner.put("/api/onboarding/orders/api", json={"url": LOCAL_API}, headers=APP)
    shop.api_secret = response.json()["secret"]          # the shop's developer installed the secret
    found = await owner.post("/api/onboarding/orders/test", json={"order_number": "#mr-20417"}, headers=APP)
    assert found.json()["found"] is True and found.json()["status"] == "out_for_delivery"
    assert "fatima" not in found.text.lower() and found.json()["orders"]["checked_at"]
    missing = await owner.post("/api/onboarding/orders/test", json={"order_number": "MR-1"}, headers=APP)
    assert missing.json()["found"] is False


async def test_a_wrong_secret_on_the_shops_side_is_reported(owner, shop):  # noqa: F811
    """The shop hasn't installed the secret yet: the owner sees an error, not 'not found'."""
    await owner.put("/api/onboarding/orders/api", json={"url": LOCAL_API}, headers=APP)
    response = await owner.post("/api/onboarding/orders/test", json={"order_number": "MR-20417"}, headers=APP)
    assert response.status_code == 502 and "HTTP 401" in response.json()["detail"]


async def test_disconnecting_removes_the_connection(owner, owner_db):
    """DELETE removes it; the state shows no order system."""
    await owner.put("/api/onboarding/orders/api", json={"url": LOCAL_API}, headers=APP)
    assert (await owner.delete("/api/onboarding/orders", headers=APP)).status_code == 204
    assert (await owner.get("/api/onboarding")).json()["orders"] is None


async def test_order_routes_need_a_session_and_the_apps_origin(client, owner_db):
    """No session: 401. Another site's origin: 403 (CSRF)."""
    assert (await client.put("/api/onboarding/orders/api", json={"url": LOCAL_API}, headers=APP)).status_code == 401
    other = {"Origin": "https://evil.example"}
    assert (await client.put("/api/onboarding/orders/api", json={"url": LOCAL_API}, headers=other)).status_code == 403
