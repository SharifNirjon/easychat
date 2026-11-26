"""A demo order API for the fictional Mirela shop (roadmap step 8): local demos and order_check.py.

It implements the generic order API contract (order_sources.ApiOrder) the way a shop's developer
would: GET /orders?order_number=MR-20417 with "Authorization: Bearer <secret>" -> the order as
JSON, 404 if there is none, 401 without the right secret.

    cd api
    python demo_shop.py --secret ec_ord_...  --email you@example.com

In the onboarding app's "Order tracking" step choose "My own system", enter
http://127.0.0.1:8790/orders, and start this with the secret it shows. Codes are emailed to
--email (read them in Mailpit, http://localhost:8025). Local only: the data is made up.
"""
import argparse
import hmac
import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

HOST, PORT = "127.0.0.1", 8790
DEFAULT_EMAIL = "customer@mirela.example"


def demo_orders(email: str) -> dict[str, dict]:
    """Three orders in different states, dated relative to now (design 6 is MR-20417; MR-20388 was
    delivered 6 days ago, so it can be returned: design 7)."""
    now = datetime.now(UTC).replace(microsecond=0)
    day = timedelta(days=1)

    def at(delta: timedelta, hour: int, minute: int = 0) -> str:
        """An ISO timestamp `delta` from today at hour:minute (UTC)."""
        return (now + delta).replace(hour=hour, minute=minute, second=0).isoformat()

    carrier = {"name": "Pathao Courier", "tracking_number": "PTH-88213",
               "tracking_url": "https://example.com/track/PTH-88213"}
    return {
        "MR-20417": {"order_number": "MR-20417", "email": email, "status": "out_for_delivery",
                     "placed_at": at(-3 * day, 10), "item_count": 2,
                     "events": [{"status": "placed", "at": at(-3 * day, 10)},
                                {"status": "packed", "at": at(-2 * day, 15)},
                                {"status": "shipped", "at": at(-day, 8), "location": "Dhaka hub"},
                                {"status": "out_for_delivery", "at": at(timedelta(), 9, 12)}],
                     "estimated_delivery": at(timedelta(), 18), "carrier": carrier,
                     "items": [{"title": "Squalane Day Gel", "quantity": 1, "price": "$26.00"},
                               {"title": "Brightening Vitamin C Serum", "quantity": 1, "price": "$12.00"}]},
        "MR-20388": {"order_number": "MR-20388", "email": email, "status": "delivered",
                     "placed_at": at(-9 * day, 11), "item_count": 1,
                     "events": [{"status": "placed", "at": at(-9 * day, 11)},
                                {"status": "shipped", "at": at(-8 * day, 9)},
                                {"status": "delivered", "at": at(-6 * day, 14, 30)}],
                     "carrier": {**carrier, "tracking_number": "PTH-87102"},
                     "items": [{"title": "Squalane Day Gel", "quantity": 1, "price": "$26.00"},
                               {"title": "Brightening Vitamin C Serum", "quantity": 2, "price": "$12.00"}]},
        "MR-20311": {"order_number": "MR-20311", "email": email, "status": "cancelled",
                     "placed_at": at(-12 * day, 16), "item_count": 3},
    }


def make_handler(secret: str, orders: dict[str, dict]):
    """A request handler class bound to one secret and one set of orders."""

    class Handler(BaseHTTPRequestHandler):
        """GET /orders?order_number=... only."""

        def do_GET(self):  # noqa: N802 (http.server's naming)
            """Check the secret, then answer with the order or 404."""
            parts = urlsplit(self.path)
            if parts.path != "/orders":
                return self._send(404, {"error": "not found"})
            given = self.headers.get("Authorization", "")
            if not hmac.compare_digest(given.encode(), f"Bearer {secret}".encode()):
                return self._send(401, {"error": "unauthorized"})
            number = parse_qs(parts.query).get("order_number", [""])[0].upper()
            order = orders.get(number)
            return self._send(200, order) if order else self._send(404, {"error": "no such order"})

        def _send(self, status: int, body: dict) -> None:
            """Write a JSON response."""
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args):  # noqa: A002
            """Quiet: one short line per request."""
            print(f"demo shop: {self.command} {urlsplit(self.path).path} -> {args[1] if len(args) > 1 else ''}")

    return Handler


def serve(secret: str, email: str = DEFAULT_EMAIL, port: int = PORT) -> ThreadingHTTPServer:
    """Start the demo shop in a background thread; call .shutdown() to stop it."""
    server = ThreadingHTTPServer((HOST, port), make_handler(secret, demo_orders(email)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> None:
    """Run until Ctrl+C."""
    parser = argparse.ArgumentParser(description="Demo order API for the Mirela shop (local only).")
    parser.add_argument("--secret", required=True, help="the secret the onboarding app showed")
    parser.add_argument("--email", default=DEFAULT_EMAIL, help="checkout email of every demo order")
    args = parser.parse_args()
    server = ThreadingHTTPServer((HOST, PORT), make_handler(args.secret, demo_orders(args.email.lower())))
    print(f"demo shop on http://{HOST}:{PORT}/orders  (orders MR-20417, MR-20388, MR-20311 for {args.email})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
