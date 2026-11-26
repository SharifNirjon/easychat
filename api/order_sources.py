"""Where order data comes from (roadmap step 8): a shop's own order API, or Shopify.

Both return the same normalised `Order`, so the chat side (order_tracking.py) never knows which
source it talks to. Read-only by design: we look orders up, we never change them.

Everything a shop's server returns is untrusted input: it is validated (statuses from a fixed
set, short texts, https links) before it is shown to a visitor or put in a prompt, and an order
whose number differs from the one asked for is treated as not found.

  GenericApiSource  GET {url}?order_number=MR-20417 with "Authorization: Bearer <secret>".
                    The JSON contract is ApiOrder below (also in the README, for shop developers).
  ShopifySource     Admin GraphQL API, read_orders scope. Credentials are either a Dev Dashboard
                    app's client ID + secret (exchanged for a short-lived token, cached), or an
                    older admin-created app's access token ("shpat_...").

All HTTP goes through fetcher.Fetcher.call(): https only, public addresses only, no redirects,
size and time caps. Tests replace `http_call` with a fake.
"""
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Literal, Protocol
from urllib.parse import quote, urlencode, urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from config import settings
from fetcher import FetchError, Fetcher, host_key

log = logging.getLogger("easychat.orders")

USER_AGENT = "easychat-orders/0.1"
RESPONSE_MAX_BYTES = 200_000
SHOPIFY_API_VERSION = "2026-10"          # Shopify supports each quarterly version for 12 months
SHOPIFY_DOMAIN = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}\.myshopify\.com$")
SHOPIFY_TOKEN_SAFETY_SECONDS = 300       # refresh a cached token this long before it expires
ORDER_NUMBER = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,31}$")

# The progress a visitor sees, in order (design 6). "cancelled" is shown separately.
PROGRESS = ("placed", "packed", "shipped", "out_for_delivery", "delivered")
Status = Literal["placed", "packed", "shipped", "out_for_delivery", "delivered", "cancelled"]


class OrderSourceError(RuntimeError):
    """The shop's order system couldn't be asked (down, wrong credentials, bad answer)."""


def normalize_order_number(raw: str) -> str | None:
    """'#mr-20417 ' -> 'MR-20417'; None if it can't be an order number."""
    value = raw.strip().lstrip("#").strip().upper()
    return value if ORDER_NUMBER.match(value) else None


def normalize_email(raw: str) -> str:
    """Emails compare case-insensitively, without surrounding spaces."""
    return raw.strip().lower()


@dataclass(frozen=True)
class Milestone:
    """When an order reached a step, and an optional short detail ("Dhaka hub")."""
    at: datetime | None = None
    detail: str | None = None


MAX_ITEMS = 20                           # line items kept per order (the return form shows up to 8)


@dataclass(frozen=True)
class OrderItem:
    """One line of an order, for the return form (design 7). Never given to the model."""
    title: str
    quantity: int = 1
    price: str | None = None                      # preformatted by the shop or us, e.g. "$26.00"
    image_url: str | None = None                  # https only; blocks.py checks the host again


@dataclass(frozen=True)
class Order:
    """One order, as every source reports it. `email` is used only to verify the visitor; it
    is never shown in full or given to the model."""
    number: str                                   # normalised (normalize_order_number)
    email: str                                    # normalised (normalize_email)
    status: Status
    placed_at: datetime | None = None
    item_count: int | None = None
    milestones: dict[str, Milestone] = field(default_factory=dict)   # status -> when / where
    estimated_delivery: datetime | None = None
    carrier: str | None = None
    tracking_number: str | None = None
    tracking_url: str | None = None
    items: tuple[OrderItem, ...] = ()

    @property
    def delivered_at(self) -> datetime | None:
        """When it was delivered, if the shop said."""
        delivered = self.milestones.get("delivered")
        return delivered.at if delivered else None


class OrderSource(Protocol):
    """Something that can look an order up by its number."""

    async def find(self, number: str) -> Order | None:
        """The order with this (normalised) number, None if there is none; OrderSourceError
        if the shop's system couldn't be asked."""
        ...


# --- HTTP ---------------------------------------------------------------------------------------

async def http_call(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, bytes]:
    """One SSRF-safe API request to the URL's own host (no redirects). Tests replace this."""
    try:
        async with Fetcher(frozenset({host_key(url)}), allow_local=settings.is_dev) as fetcher:
            return await fetcher.call(method, url, headers, body, RESPONSE_MAX_BYTES)
    except FetchError as exc:
        raise OrderSourceError(f"couldn't reach the order system: {exc}") from exc


def _json(body: bytes) -> dict:
    """A JSON object from a response body, or OrderSourceError."""
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise OrderSourceError("the order system didn't answer with JSON") from exc
    if not isinstance(data, dict):
        raise OrderSourceError("the order system's answer isn't a JSON object")
    return data


# --- Generic order API --------------------------------------------------------------------------

Short = Annotated[str, Field(min_length=1, max_length=80)]


class _Lenient(BaseModel):
    """Shops may send extra fields (ignored); the ones we use are checked."""
    model_config = ConfigDict(extra="ignore")


class ApiEvent(_Lenient):
    """One step the order reached."""
    status: Literal["placed", "packed", "shipped", "out_for_delivery", "delivered"]
    at: datetime | None = None
    location: Short | None = None


class ApiCarrier(_Lenient):
    """Who delivers it, and where to track it."""
    name: Short | None = None
    tracking_number: Annotated[str, Field(min_length=1, max_length=40)] | None = None
    tracking_url: Annotated[str, Field(max_length=500)] | None = None


class ApiItem(_Lenient):
    """One line of the order (optional; used for return requests)."""
    title: Annotated[str, Field(min_length=1, max_length=120)]
    quantity: Annotated[int, Field(ge=1, le=1000)] = 1
    price: Annotated[str, Field(min_length=1, max_length=20)] | None = None      # as the shop shows it: "$26"
    image_url: Annotated[str, Field(max_length=500)] | None = None


class ApiOrder(_Lenient):
    """What GET {url}?order_number=... must return (200), or 404 when there's no such order."""
    order_number: Annotated[str, Field(min_length=1, max_length=40)]
    email: Annotated[str, Field(min_length=3, max_length=254)]
    status: Status
    placed_at: datetime | None = None
    item_count: Annotated[int, Field(ge=0, le=10_000)] | None = None
    events: Annotated[list[ApiEvent], Field(max_length=20)] = []
    estimated_delivery: datetime | None = None
    carrier: ApiCarrier | None = None
    items: Annotated[list[ApiItem], Field(max_length=100)] = []


def https_link(url: str | None) -> str | None:
    """Keep a tracking link only if it is a plain https URL (blocks.py checks it again)."""
    if not url:
        return None
    parts = urlsplit(url)
    return url if parts.scheme == "https" and parts.hostname and not parts.username else None


def order_from_api(data: dict) -> Order:
    """Validate a generic-API answer into an Order (OrderSourceError if it doesn't fit)."""
    try:
        api = ApiOrder.model_validate(data)
    except ValidationError as exc:
        raise OrderSourceError(f"the order system's answer doesn't match the format: "
                               f"{exc.error_count()} problem(s)") from exc
    number = normalize_order_number(api.order_number)
    if number is None:
        raise OrderSourceError("the order system returned an invalid order number")
    milestones = {e.status: Milestone(e.at, e.location) for e in api.events}
    if api.placed_at and "placed" not in milestones:
        milestones["placed"] = Milestone(api.placed_at)
    carrier = api.carrier or ApiCarrier()
    items = tuple(OrderItem(" ".join(i.title.split()), i.quantity, i.price, https_link(i.image_url))
                  for i in api.items[:MAX_ITEMS])
    item_count = api.item_count if api.item_count is not None else (sum(i.quantity for i in items) or None)
    return Order(number=number, email=normalize_email(api.email), status=api.status, placed_at=api.placed_at,
                 item_count=item_count, milestones=milestones, estimated_delivery=api.estimated_delivery,
                 carrier=carrier.name, tracking_number=carrier.tracking_number,
                 tracking_url=https_link(carrier.tracking_url), items=items)


@dataclass(frozen=True)
class GenericApiSource:
    """A shop's own endpoint that follows the ApiOrder contract."""
    url: str
    secret: str

    async def find(self, number: str) -> Order | None:
        """GET the order; 404 means no such order."""
        separator = "&" if "?" in self.url else "?"
        status, body = await http_call(
            "GET", f"{self.url}{separator}order_number={quote(number, safe='')}",
            {"Authorization": f"Bearer {self.secret}", "Accept": "application/json", "User-Agent": USER_AGENT},
            None)
        if status == 404:
            return None
        if status != 200:
            raise OrderSourceError(f"the order system answered HTTP {status}")
        order = order_from_api(_json(body))
        return order if order.number == number else None     # never show a different order


# --- Shopify ------------------------------------------------------------------------------------

class TokenCache(Protocol):
    """Where a Shopify access token from the client-credentials exchange is kept until it expires."""

    async def get(self) -> str | None:
        """The cached token, if any."""
        ...

    async def put(self, token: str, ttl_seconds: int) -> None:
        """Cache a token for ttl_seconds."""
        ...

    async def drop(self) -> None:
        """Forget the cached token (Shopify refused it)."""
        ...


SHOPIFY_ORDER_QUERY = """
query OrderByName($query: String!) {
  orders(first: 5, query: $query) {
    nodes {
      name email createdAt cancelledAt displayFulfillmentStatus subtotalLineItemsQuantity
      fulfillments(first: 5) {
        displayStatus createdAt inTransitAt deliveredAt estimatedDeliveryAt
        trackingInfo(first: 1) { company number url }
      }
      lineItems(first: 20) {
        nodes { name quantity image { url } originalUnitPriceSet { shopMoney { amount currencyCode } } }
      }
    }
  }
}"""

CURRENCY_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "BDT": "৳", "INR": "₹", "JPY": "¥", "CAD": "CA$", "AUD": "A$"}

# Shopify's fulfillment display statuses -> our progress steps.
_SHOPIFY_FULFILLMENT = {
    "DELIVERED": "delivered", "PICKED_UP": "delivered",
    "OUT_FOR_DELIVERY": "out_for_delivery", "ATTEMPTED_DELIVERY": "out_for_delivery",
    "IN_TRANSIT": "shipped", "FULFILLED": "shipped", "CONFIRMED": "shipped", "LABEL_PRINTED": "packed",
    "LABEL_PURCHASED": "packed", "READY_FOR_PICKUP": "shipped",
}
_SHOPIFY_ORDER = {"FULFILLED": "shipped", "PARTIALLY_FULFILLED": "shipped", "IN_PROGRESS": "packed",
                  "PENDING_FULFILLMENT": "packed"}


def _time(value: object) -> datetime | None:
    """An ISO timestamp from Shopify, or None."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _text(value: object, limit: int) -> str | None:
    """A short, single-line text from Shopify, or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    return " ".join(value.split())[:limit]


def _shopify_price(money: object) -> str | None:
    """{'shopMoney': {'amount': '26.0', 'currencyCode': 'USD'}} -> '$26.00' (or '26.00 XYZ')."""
    shop_money = money.get("shopMoney") if isinstance(money, dict) else None
    if not isinstance(shop_money, dict):
        return None
    try:
        amount = f"{float(shop_money.get('amount')):,.2f}"
    except (TypeError, ValueError):
        return None
    code = str(shop_money.get("currencyCode") or "")[:3]
    symbol = CURRENCY_SYMBOLS.get(code)
    return (f"{symbol}{amount}" if symbol else f"{amount} {code}".strip())[:20]


def _shopify_items(node: dict) -> tuple[OrderItem, ...]:
    """The order's line items (name, quantity, unit price, image)."""
    items = []
    for line in ((node.get("lineItems") or {}).get("nodes") or [])[:MAX_ITEMS]:
        if not isinstance(line, dict) or not (title := _text(line.get("name"), 120)):
            continue
        quantity = line.get("quantity")
        image = line.get("image") if isinstance(line.get("image"), dict) else {}
        url = image.get("url")
        items.append(OrderItem(title, quantity if isinstance(quantity, int) and quantity > 0 else 1,
                               _shopify_price(line.get("originalUnitPriceSet")),
                               https_link(url if isinstance(url, str) else None)))
    return tuple(items)


def order_from_shopify(node: dict) -> Order:
    """Map one Shopify order node to an Order."""
    number = normalize_order_number(str(node.get("name") or ""))
    email = node.get("email")
    if number is None or not isinstance(email, str):
        raise OrderSourceError("Shopify returned an order without a usable number or email")
    fulfillments = [f for f in node.get("fulfillments") or [] if isinstance(f, dict)]
    latest = max(fulfillments, key=lambda f: f.get("createdAt") or "", default=None)
    if node.get("cancelledAt"):
        status = "cancelled"
    elif latest and latest.get("displayStatus") in _SHOPIFY_FULFILLMENT:
        status = _SHOPIFY_FULFILLMENT[latest["displayStatus"]]
    else:
        status = _SHOPIFY_ORDER.get(str(node.get("displayFulfillmentStatus")), "placed")
    milestones = {"placed": Milestone(_time(node.get("createdAt")))}
    tracking: dict = {}
    if latest:
        milestones["shipped"] = Milestone(_time(latest.get("inTransitAt")) or _time(latest.get("createdAt")))
        if latest.get("deliveredAt"):
            milestones["delivered"] = Milestone(_time(latest.get("deliveredAt")))
        infos = latest.get("trackingInfo") or []
        tracking = infos[0] if infos and isinstance(infos[0], dict) else {}
    quantity = node.get("subtotalLineItemsQuantity")
    return Order(
        number=number, email=normalize_email(email), status=status, placed_at=_time(node.get("createdAt")),
        item_count=quantity if isinstance(quantity, int) and quantity >= 0 else None, milestones=milestones,
        estimated_delivery=_time(latest.get("estimatedDeliveryAt")) if latest else None,
        carrier=_text(tracking.get("company"), 80), tracking_number=_text(tracking.get("number"), 40),
        tracking_url=https_link(tracking.get("url") if isinstance(tracking.get("url"), str) else None),
        items=_shopify_items(node),
    )


@dataclass
class ShopifySource:
    """A Shopify store, read through the Admin GraphQL API."""
    shop: str                          # 'my-store.myshopify.com'
    credentials: dict                  # {"client_id", "client_secret"} or {"access_token"}
    cache: TokenCache | None = None

    @property
    def base(self) -> str:
        """https://my-store.myshopify.com"""
        return f"https://{self.shop}"

    async def access_token(self) -> str:
        """A legacy app's token as is; otherwise the cached or a freshly exchanged one."""
        if self.credentials.get("access_token"):
            return self.credentials["access_token"]
        if self.cache and (cached := await self.cache.get()):
            return cached
        # OAuth 2 client-credentials grant, form-encoded as the OAuth spec defines it.
        body = urlencode({"grant_type": "client_credentials", "client_id": self.credentials.get("client_id", ""),
                          "client_secret": self.credentials.get("client_secret", "")}).encode()
        status, raw = await http_call("POST", f"{self.base}/admin/oauth/access_token",
                                      {"Content-Type": "application/x-www-form-urlencoded",
                                       "Accept": "application/json", "User-Agent": USER_AGENT}, body)
        if status != 200:
            raise OrderSourceError(f"Shopify refused the app's client ID / secret (HTTP {status})")
        data = _json(raw)
        token = data.get("access_token")
        if not isinstance(token, str) or not token:
            raise OrderSourceError("Shopify didn't return an access token")
        expires = data.get("expires_in")
        if self.cache and isinstance(expires, int) and expires > 2 * SHOPIFY_TOKEN_SAFETY_SECONDS:
            await self.cache.put(token, expires - SHOPIFY_TOKEN_SAFETY_SECONDS)
        return token

    async def query(self, query: str, variables: dict) -> dict:
        """Run one GraphQL query; its `data`, or OrderSourceError."""
        token = await self.access_token()
        status, raw = await http_call(
            "POST", f"{self.base}/admin/api/{SHOPIFY_API_VERSION}/graphql.json",
            {"X-Shopify-Access-Token": token, "Content-Type": "application/json", "Accept": "application/json",
             "User-Agent": USER_AGENT},
            json.dumps({"query": query, "variables": variables}).encode())
        if status in (401, 403):
            if self.cache:
                await self.cache.drop()
            raise OrderSourceError(f"Shopify refused the credentials (HTTP {status})")
        if status != 200:
            raise OrderSourceError(f"Shopify answered HTTP {status}")
        data = _json(raw)
        if data.get("errors"):
            raise OrderSourceError("Shopify rejected the query (missing read_orders access?)")
        return data.get("data") or {}

    async def find(self, number: str) -> Order | None:
        """Search orders by name ('#1001' and '1001' both find it); exact match only."""
        data = await self.query(SHOPIFY_ORDER_QUERY, {"query": f'name:"#{number}" OR name:"{number}"'})
        for node in ((data.get("orders") or {}).get("nodes") or []):
            if isinstance(node, dict) and normalize_order_number(str(node.get("name") or "")) == number:
                return order_from_shopify(node)
        return None

    async def check(self) -> None:
        """Prove the credentials work (used when the owner connects the store)."""
        await self.query("query { shop { name } }", {})


def valid_shop_domain(raw: str) -> str | None:
    """'My-Store.myshopify.com' / 'https://my-store.myshopify.com/' -> 'my-store.myshopify.com'."""
    value = raw.strip().lower().removeprefix("https://").removeprefix("http://").rstrip("/")
    return value if SHOPIFY_DOMAIN.match(value) else None
