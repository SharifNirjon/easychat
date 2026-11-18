"""schema.org data from a page's JSON-LD: `Product` -> plain, length-capped product records, and
whether the page is an article (page_kind).

JSON-LD on the web is messy (lists vs objects, @graph wrappers, numbers as strings), and it is
untrusted, so every field is optional, type-checked and capped. Anything odd is dropped.
"""
import math
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

MAX_PRODUCTS_PER_PAGE = 20
MAX_NODES = 200                     # JSON-LD nodes visited per page (bounds deep/huge graphs)
MAX_DEPTH = 6

AVAILABILITY = {                    # schema.org ItemAvailability -> short label
    "instock": "In stock", "outofstock": "Out of stock", "preorder": "Pre-order",
    "backorder": "Backorder", "soldout": "Sold out", "limitedavailability": "Limited stock",
    "onlineonly": "Online only", "discontinued": "Discontinued",
}


@dataclass(frozen=True)
class Product:
    """One product as indexed: every field optional except name and URL, all length-capped."""
    name: str
    url: str
    description: str = ""
    image_url: str | None = None
    brand: str | None = None
    price: str | None = None
    currency: str | None = None
    availability: str | None = None
    rating: float | None = None
    review_count: int | None = None

    def summary(self) -> str:
        """The text indexed for this product (its own chunk), so product questions find it."""
        parts = [f"Product: {self.name}"]
        if self.brand:
            parts.append(f"Brand: {self.brand}")
        if self.price:
            parts.append(f"Price: {self.price} {self.currency or ''}".rstrip())
        if self.availability:
            parts.append(f"Availability: {self.availability}")
        if self.rating is not None:
            parts.append(f"Rating: {self.rating:.1f}/5" + (f" ({self.review_count} reviews)" if self.review_count else ""))
        if self.description:
            parts.append(self.description)
        return "\n".join(parts)


def _text(value, limit: int) -> str | None:
    """A JSON-LD value as clean text (first item of a list, `name` of an object), capped."""
    if isinstance(value, dict):
        value = value.get("name") or value.get("@value")
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value[:limit] if value else None


def _number(value) -> float | None:
    """A JSON-LD value as a finite float ('1,240' and '12.50' included), else None."""
    if isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _types(node: dict) -> set[str]:
    """A node's @type(s), lowercased without the schema.org prefix."""
    kind = node.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    return {k.rsplit("/", 1)[-1].lower() for k in kinds if isinstance(k, str)}


def _url(value, base_url: str) -> str | None:
    """A JSON-LD URL (string, object or list) made absolute; only http(s) is kept."""
    if isinstance(value, dict):
        value = value.get("url") or value.get("contentUrl") or value.get("@id")
    if isinstance(value, list):
        value = value[0] if value else None
    if not isinstance(value, str) or len(value) > 2000:
        return None
    url = urljoin(base_url, value.strip())
    return url if urlsplit(url).scheme in ("http", "https") else None


def _offer(node) -> dict | None:
    """The node's first offer (offers may be one object or a list)."""
    offers = node.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if offers else None
    return offers if isinstance(offers, dict) else None


def _price(offer: dict) -> tuple[str | None, str | None]:
    """Price and currency from an offer (price, lowPrice or priceSpecification)."""
    value = offer.get("price", offer.get("lowPrice"))
    if value is None and isinstance(offer.get("priceSpecification"), dict):
        value = offer["priceSpecification"].get("price")
    number = _number(value)
    if number is None or number < 0:
        return None, None
    price = f"{number:.2f}".rstrip("0").rstrip(".") if number != int(number) else str(int(number))
    currency = _text(offer.get("priceCurrency"), 8)
    return price[:20], currency.upper() if currency else None


def _product(node: dict, page_url: str) -> Product | None:
    """One Product node -> Product, or None if it has no usable name."""
    name = _text(node.get("name"), 200)
    if not name:
        return None
    offer = _offer(node) or {}
    price, currency = _price(offer)
    availability = _text(offer.get("availability"), 100)
    if availability:
        availability = AVAILABILITY.get(availability.rsplit("/", 1)[-1].lower())
    rating_node = node.get("aggregateRating") if isinstance(node.get("aggregateRating"), dict) else {}
    rating = _number(rating_node.get("ratingValue"))
    best = _number(rating_node.get("bestRating")) or 5
    if rating is not None:
        rating = round(max(0.0, min(5.0, rating * 5 / best if best > 0 else rating)), 1)
    count = _number(rating_node.get("reviewCount", rating_node.get("ratingCount")))
    return Product(
        name=name,
        url=_url(node.get("url") or offer.get("url"), page_url) or page_url,
        description=_text(node.get("description"), 1000) or "",
        image_url=_url(node.get("image"), page_url),
        brand=_text(node.get("brand"), 120),
        price=price,
        currency=currency,
        availability=availability,
        rating=rating,
        review_count=int(count) if count is not None and 0 <= count < 2**31 else None,
    )


ARTICLE_TYPES = {"article", "blogposting", "newsarticle", "techarticle", "scholarlyarticle", "report"}


def page_kind(blocks: list) -> str:
    """'article' if the page's JSON-LD declares an Article type at its top level, else 'page'.

    Articles explain topics in general; the prompt tells the model their examples (a sample refund
    policy in a blog post about RAG, say) are not the company's own facts.
    """
    nodes: list = []
    for block in blocks[:MAX_NODES]:
        if isinstance(block, dict):
            graph = block.get("@graph")
            nodes.extend(graph[:MAX_NODES] if isinstance(graph, list) else [block])
    return "article" if any(isinstance(n, dict) and _types(n) & ARTICLE_TYPES for n in nodes) else "page"


def products_from_json_ld(blocks: list, page_url: str) -> list[Product]:
    """Every Product node in the page's JSON-LD blocks, including inside @graph / ItemList."""
    found: list[Product] = []
    seen: set[str] = set()
    visited = 0

    def walk(node, depth: int) -> None:
        """Visit nodes depth-first, collecting products; bounded by depth and count."""
        nonlocal visited
        if depth > MAX_DEPTH or visited >= MAX_NODES or len(found) >= MAX_PRODUCTS_PER_PAGE:
            return
        if isinstance(node, list):
            for item in node:
                walk(item, depth + 1)
            return
        if not isinstance(node, dict):
            return
        visited += 1
        if _types(node) & {"product", "productgroup"}:
            product = _product(node, page_url)
            if product and product.name not in seen:
                seen.add(product.name)
                found.append(product)
            return
        for key in ("@graph", "itemListElement", "item", "mainEntity"):
            if key in node:
                walk(node[key], depth + 1)

    walk(blocks, 0)
    return found
