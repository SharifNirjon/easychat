"""order_sources.py: the generic order API and Shopify, against a fake shop (no network)."""
import asyncio
import json
from urllib.parse import parse_qs, urlsplit

import pytest

import order_sources
from order_sources import (GenericApiSource, OrderSourceError, ShopifySource, normalize_order_number,
                           order_from_api, valid_shop_domain)

API_URL = "https://orders.shop-a.example/orders"
API_SECRET = "ec_ord_test-secret"
SHOP = "shop-a.myshopify.com"
CUSTOMER = "fatima@example.com"

# The design 6 order, in the generic API's format.
MR_20417 = {
    "order_number": "MR-20417", "email": "Fatima@Example.com", "status": "out_for_delivery",
    "placed_at": "2026-10-28T10:00:00+06:00", "item_count": 2,
    "events": [{"status": "placed", "at": "2026-10-28T10:00:00+06:00"},
               {"status": "packed", "at": "2026-10-29T15:00:00+06:00"},
               {"status": "shipped", "at": "2026-10-30T08:00:00+06:00", "location": "Dhaka hub"},
               {"status": "out_for_delivery", "at": "2026-10-31T09:12:00+06:00"}],
    "estimated_delivery": "2026-10-31T18:00:00+06:00",
    "carrier": {"name": "Pathao Courier", "tracking_number": "PTH-88213",
                "tracking_url": "https://track.pathao.example/PTH-88213"},
    "customer_address": "House 1, Road 2, Dhaka",          # extra fields are ignored, never shown
}

SHOPIFY_ORDER = {
    "name": "#1001", "email": "fatima@example.com", "createdAt": "2026-10-28T04:00:00Z", "cancelledAt": None,
    "displayFulfillmentStatus": "FULFILLED", "subtotalLineItemsQuantity": 3,
    "fulfillments": [{"displayStatus": "OUT_FOR_DELIVERY", "createdAt": "2026-10-30T02:00:00Z",
                      "inTransitAt": "2026-10-30T05:00:00Z", "deliveredAt": None,
                      "estimatedDeliveryAt": "2026-10-31T12:00:00Z",
                      "trackingInfo": [{"company": "DHL", "number": "JD0123456789", "url": "https://dhl.example/t"}]}],
}


class FakeShop:
    """Stands in for order_sources.http_call: a generic order API and a Shopify store."""

    def __init__(self):
        """Known orders, the credentials it accepts, and a log of calls."""
        self.api_orders = {"MR-20417": dict(MR_20417)}
        self.shopify_orders = [dict(SHOPIFY_ORDER)]
        self.client = ("client-id-123", "client-secret-456")
        self.api_secret = API_SECRET                 # the Bearer secret the order API accepts
        self.calls: list[tuple[str, str, dict]] = []
        self.gate: asyncio.Event | None = None      # set: requests wait until it is released
        self.down = False

    async def __call__(self, method, url, headers, body):
        """Answer like the real services would."""
        self.calls.append((method, url, headers))
        if self.gate is not None:
            await self.gate.wait()
        if self.down:
            raise OrderSourceError("couldn't reach the order system: timed out")
        parts = urlsplit(url)
        if parts.netloc == "orders.shop-a.example":
            if headers.get("Authorization") != f"Bearer {self.api_secret}":
                return 401, b""
            order = self.api_orders.get(parse_qs(parts.query)["order_number"][0])
            return (200, json.dumps(order).encode()) if order else (404, b"")
        if parts.path == "/admin/oauth/access_token":
            form = parse_qs(body.decode())
            if (form.get("client_id", [""])[0], form.get("client_secret", [""])[0]) != self.client:
                return 400, b'{"error":"invalid_client"}'
            return 200, json.dumps({"access_token": "shpat_exchanged", "expires_in": 86399}).encode()
        if parts.path.endswith("/graphql.json"):
            if headers.get("X-Shopify-Access-Token") not in ("shpat_exchanged", "shpat_legacy"):
                return 401, b""
            query = json.loads(body)
            if "shop {" in query["query"]:
                return 200, b'{"data":{"shop":{"name":"Shop A"}}}'
            return 200, json.dumps({"data": {"orders": {"nodes": self.shopify_orders}}}).encode()
        return 404, b""


@pytest.fixture
def shop(monkeypatch):
    """The fake shop, wired in for every order source."""
    fake = FakeShop()
    monkeypatch.setattr(order_sources, "http_call", fake)
    return fake


class MemoryCache:
    """An in-memory TokenCache."""

    def __init__(self):
        """Empty."""
        self.token = None

    async def get(self):
        """The cached token."""
        return self.token

    async def put(self, token, ttl_seconds):
        """Cache it."""
        self.token = token

    async def drop(self):
        """Forget it."""
        self.token = None


# --- Generic API --------------------------------------------------------------------------------

async def test_generic_api_returns_the_normalised_order(shop):
    """The order is found with the Bearer secret, and comes back normalised and minimal."""
    order = await GenericApiSource(API_URL, API_SECRET).find("MR-20417")
    assert order.number == "MR-20417" and order.email == CUSTOMER and order.status == "out_for_delivery"
    assert order.milestones["shipped"].detail == "Dhaka hub" and order.item_count == 2
    assert (order.carrier, order.tracking_number) == ("Pathao Courier", "PTH-88213")
    assert not hasattr(order, "customer_address")
    method, url, headers = shop.calls[-1]
    assert method == "GET" and url.endswith("?order_number=MR-20417")
    assert headers["Authorization"] == f"Bearer {API_SECRET}"


async def test_generic_api_unknown_order_is_none(shop):
    """404 means there is no such order."""
    assert await GenericApiSource(API_URL, API_SECRET).find("MR-99999") is None


async def test_generic_api_answering_with_another_order_is_treated_as_not_found(shop):
    """A buggy or hostile endpoint that returns a different order never gets it shown."""
    shop.api_orders["MR-55555"] = dict(MR_20417)       # number in the body: MR-20417
    assert await GenericApiSource(API_URL, API_SECRET).find("MR-55555") is None


async def test_generic_api_wrong_secret_is_an_error(shop):
    """A rejected secret is an error (not 'not found'), so the owner can be told."""
    with pytest.raises(OrderSourceError):
        await GenericApiSource(API_URL, "wrong").find("MR-20417")


@pytest.mark.parametrize("change", [{"status": "teleported"}, {"email": ""}, {"order_number": "x" * 50},
                                    {"events": [{"status": "placed"}] * 30}])
def test_generic_api_answers_outside_the_contract_are_refused(change):
    """Unknown statuses, missing emails, oversized fields: an error, never shown."""
    with pytest.raises(OrderSourceError):
        order_from_api({**MR_20417, **change})


def test_plain_http_tracking_links_are_dropped():
    """Only https tracking links reach the card."""
    order = order_from_api({**MR_20417, "carrier": {"name": "X", "tracking_url": "http://track.example/1"}})
    assert order.tracking_url is None


# --- Shopify ------------------------------------------------------------------------------------

async def test_shopify_client_credentials_are_exchanged_and_the_token_cached(shop):
    """Client ID + secret -> an access token (cached), then the order query."""
    cache = MemoryCache()
    source = ShopifySource(SHOP, {"client_id": "client-id-123", "client_secret": "client-secret-456"}, cache)
    order = await source.find("1001")
    assert order.number == "1001" and order.status == "out_for_delivery" and order.item_count == 3
    assert (order.carrier, order.tracking_number) == ("DHL", "JD0123456789")
    assert cache.token == "shpat_exchanged"
    exchanges = [c for c in shop.calls if c[1].endswith("/admin/oauth/access_token")]
    await source.find("1001")
    assert len([c for c in shop.calls if c[1].endswith("/admin/oauth/access_token")]) == len(exchanges) == 1


async def test_shopify_legacy_token_is_used_directly(shop):
    """An older admin-created app's token needs no exchange."""
    order = await ShopifySource(SHOP, {"access_token": "shpat_legacy"}).find("1001")
    assert order is not None
    assert not any(c[1].endswith("/access_token") for c in shop.calls)


async def test_shopify_wrong_client_secret_is_an_error(shop):
    """Shopify refusing the exchange is reported, not treated as 'no order'."""
    with pytest.raises(OrderSourceError):
        await ShopifySource(SHOP, {"client_id": "client-id-123", "client_secret": "nope-nope"}).find("1001")


async def test_shopify_refused_token_is_dropped_from_the_cache(shop):
    """A cached token Shopify no longer accepts is forgotten, so the next lookup exchanges again."""
    cache = MemoryCache()
    cache.token = "shpat_revoked"
    with pytest.raises(OrderSourceError):
        await ShopifySource(SHOP, {"client_id": "client-id-123", "client_secret": "client-secret-456"},
                            cache).find("1001")
    assert cache.token is None


async def test_shopify_only_an_exact_name_match_counts(shop):
    """A search hit with a different order name is not the visitor's order."""
    shop.shopify_orders = [{**SHOPIFY_ORDER, "name": "#10010"}]
    assert await ShopifySource(SHOP, {"access_token": "shpat_legacy"}).find("1001") is None


@pytest.mark.parametrize("changes, status", [
    ({"cancelledAt": "2026-10-29T00:00:00Z"}, "cancelled"),
    ({"fulfillments": [], "displayFulfillmentStatus": "UNFULFILLED"}, "placed"),
    ({"fulfillments": [], "displayFulfillmentStatus": "IN_PROGRESS"}, "packed"),
    ({"fulfillments": [{**SHOPIFY_ORDER["fulfillments"][0], "displayStatus": "DELIVERED",
                        "deliveredAt": "2026-10-31T10:00:00Z"}]}, "delivered"),
])
async def test_shopify_statuses_map_to_our_steps(shop, changes, status):
    """Cancelled, unfulfilled, in progress and delivered orders land on the right step."""
    shop.shopify_orders = [{**SHOPIFY_ORDER, **changes}]
    assert (await ShopifySource(SHOP, {"access_token": "shpat_legacy"}).find("1001")).status == status


# --- Small helpers ------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [("#mr-20417 ", "MR-20417"), ("1001", "1001"), ("# 1001", "1001"),
                                           ("", None), ("a b", None), ("x" * 40, None), ("<script>", None)])
def test_order_numbers_are_normalised(raw, expected):
    """Case, '#' and spaces don't matter; anything that can't be an order number is refused."""
    assert normalize_order_number(raw) == expected


@pytest.mark.parametrize("raw, expected", [("My-Store.myshopify.com", "my-store.myshopify.com"),
                                           ("https://my-store.myshopify.com/", "my-store.myshopify.com"),
                                           ("evil.example", None), ("my-store.myshopify.com.evil.example", None)])
def test_shop_domains_must_be_myshopify(raw, expected):
    """Only *.myshopify.com hosts are called with Shopify credentials."""
    assert valid_shop_domain(raw) == expected
