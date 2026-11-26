"""End-to-end check of the chat forms (roadmap step 9 "Done when"): requests reach the shop's team
by email + webhook; nothing is saved without consent.

    cd api
    python requests_check.py        # needs: docker compose up -d, the API and the worker

Against the running services (the shop's order API is demo_shop.py; the webhook receiver below
stands in for the shop's endpoint and checks every signature):
  1. a new owner signs in (Mailpit), adds a website, connects orders, sets the team's email and a
     webhook, and sends a signed test event
  2. a visitor asks to be contacted: the contact form; sent WITHOUT consent -> no row in the
     database, no email, no webhook
  3. sent WITH consent -> stored, then the worker emails the team (Reply-To: the visitor) and
     POSTs the signed webhook
  4. a customer verifies a delivered order (code from Mailpit), asks for a return, sends the
     return form -> the team gets the return request by email + webhook
  5. "delete my data" in the first chat -> the conversation and its request are gone
"""
import hashlib
import hmac
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import psycopg

import demo_shop
from config import settings
from evals.setup import serve_sites
from onboarding_check import MAILPIT_URL, SITE_ORIGIN, SITEMAP_URL, CheckFailed, signin_token, wait_for
from order_check import mails_to, order_code

HOOK_HOST, HOOK_PORT = "127.0.0.1", 8791
RETURN_ORDER = "MR-20388"                 # delivered 6 days ago in demo_shop.py
SETTLE_SECONDS = 4                        # time for a (wrongly) sent email/webhook to arrive before we check
DELIVERY_TIMEOUT = 45                     # the worker polls every few seconds


# --- The shop's webhook endpoint ----------------------------------------------------------------

class Receiver:
    """Collects webhook deliveries whose signature checks out (and counts the ones that don't)."""

    def __init__(self):
        """No secret yet (set once the app shows it); nothing received."""
        self.secret = ""
        self.events: list[dict] = []
        self.bad_signatures = 0

    def verify(self, header: str, body: bytes) -> bool:
        """X-Easychat-Signature: t=<unix>,v1=<hex HMAC-SHA256(secret, "<t>." + body)>, at most 5 minutes old."""
        parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
        if not parts.get("t", "").isdigit() or abs(time.time() - int(parts["t"])) > 300:
            return False
        expected = hmac.new(self.secret.encode(), f"{parts['t']}.".encode() + body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, parts.get("v1", ""))

    def serve(self) -> ThreadingHTTPServer:
        """Listen on HOOK_PORT in a background thread."""
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            """POST /hook only."""

            def do_POST(self):  # noqa: N802 (http.server's naming)
                """Verify, record, answer 204 (401 for a bad signature)."""
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if receiver.verify(self.headers.get("X-Easychat-Signature", ""), body):
                    receiver.events.append(json.loads(body))
                    self.send_response(204)
                else:
                    receiver.bad_signatures += 1
                    self.send_response(401)
                self.end_headers()

            def log_message(self, format, *args):  # noqa: A002
                """Quiet."""

        server = ThreadingHTTPServer((HOOK_HOST, HOOK_PORT), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server


# --- Talking to the chat ------------------------------------------------------------------------

def turn(api: httpx.Client, site_key: str, cid: str | None = None, message: str | None = None,
         form: tuple[str, dict] | None = None) -> tuple[str, list[dict], str, set[str]]:
    """One /chat turn as a visitor: (text, blocks, conversation id, event names seen)."""
    body: dict = {"site_key": site_key, **({"conversation_id": cid} if cid else {})}
    if message is not None:
        body["message"] = message
    if form is not None:
        body["form"] = {"name": form[0], "values": form[1]}
    text, blocks, events, event = "", [], set(), None
    with api.stream("POST", "/chat", json=body, headers={"Origin": SITE_ORIGIN}, timeout=90) as response:
        if response.status_code != 200:
            raise CheckFailed(f"/chat returned HTTP {response.status_code}: {response.read()[:200]!r}")
        for line in response.iter_lines():
            if line.startswith("event: "):
                event = line[7:]
                events.add(event)
            elif line.startswith("data: "):
                data = json.loads(line[6:])
                if event == "meta":
                    cid = data["conversation_id"]
                elif event == "token":
                    text += data["t"]
                elif event == "replace":
                    text = data["text"]
                elif event == "block":
                    blocks.append(data)
                elif event == "error":
                    raise CheckFailed(f"/chat error: {data}")
    return text, blocks, cid, events


def form_in(blocks: list[dict], name: str) -> dict:
    """The form block that submits `name`, or CheckFailed."""
    found = next((b for b in blocks if b.get("type") == "form" and b.get("submit") == name), None)
    if found is None:
        raise CheckFailed(f"expected the {name} form, got {blocks}")
    return found


def db_count(sql: str, *args) -> int:
    """A count from the database, as the owner (sees every tenant; for checking only)."""
    with psycopg.connect(os.environ["MIGRATION_DATABASE_URL"]) as conn:
        return conn.execute(sql, args).fetchone()[0]


def team_mail(mailpit: httpx.Client, team: str, subject_part: str) -> dict:
    """Wait for the worker's email to the team whose subject contains `subject_part`; its full message."""
    def probe():
        for found in mails_to(mailpit, team):
            if subject_part in found.get("Subject", ""):
                return mailpit.get(f"/api/v1/message/{found['ID']}").json()
        return None
    return wait_for(f"the team email '{subject_part}'", probe, DELIVERY_TIMEOUT)


# --- The check ----------------------------------------------------------------------------------

def run() -> list[str]:
    """All stages; returns what passed (raises CheckFailed on the first failure)."""
    stamp = int(time.time())
    owner, team = f"owner+{stamp}@mirela.example", f"team+{stamp}@mirela.example"
    visitor, customer = f"rafiq+{stamp}@meghna.example", f"customer+{stamp}@mirela.example"
    app = {"Origin": settings.app_origin}
    receiver = Receiver()
    passed: list[str] = []

    def ok(stage: str) -> None:
        """Record and print a passed stage."""
        passed.append(stage)
        print(f"  ok  {stage}", flush=True)

    hook_server = receiver.serve()
    with httpx.Client(base_url=settings.app_base_url, timeout=30) as api, \
            httpx.Client(base_url=MAILPIT_URL, timeout=10) as mailpit:
        # 1. The owner.
        api.post("/api/auth/start", json={"email": owner}, headers=app)
        if api.post("/api/auth/verify", json={"token": signin_token(mailpit, owner)}, headers=app).status_code != 200:
            raise CheckFailed("owner sign-in failed")
        site_key = api.post("/api/onboarding/site", json={"url": SITEMAP_URL}, headers=app).json()["site_key"]
        tenant_requests = "SELECT count(*) FROM requests r JOIN site_keys k ON k.tenant_id = r.tenant_id " \
                          "WHERE k.public_key = %s"
        connected = api.put("/api/onboarding/orders/api", headers=app,
                            json={"url": f"http://{demo_shop.HOST}:{demo_shop.PORT}/orders"}).json()
        shop = demo_shop.serve(connected["secret"], customer)
        try:
            saved = api.put("/api/onboarding/requests/settings", headers=app, json={
                "notify_email": team, "booking_url": "https://cal.example/mirela", "retention_days": 30,
                "leads": True, "returns": True, "return_window_days": 30})
            if saved.status_code != 200:
                raise CheckFailed(f"saving request settings failed: {saved.text[:200]}")
            hook = api.put("/api/onboarding/requests/webhook", headers=app,
                           json={"url": f"http://{HOOK_HOST}:{HOOK_PORT}/hook"}).json()
            receiver.secret = hook["secret"]
            if not api.post("/api/onboarding/requests/webhook/test", headers=app).json().get("ok"):
                raise CheckFailed("the signed test event wasn't accepted by the receiver")
            ok("owner set the team email and a webhook; signed test event verified by the receiver")

            # 2. Contact form without consent: nothing.
            # Opens with the request itself (answered by code), so this stage needs no model credits.
            text, blocks, lead_cid, _ = turn(api, site_key, message="We need 40 large planters for three new "
                                                                    "cafes. Can someone from your team contact me?")
            summary = next(f.get("value", "") for f in form_in(blocks, "lead")["fields"] if f["name"] == "summary")
            values = {"name": "Rafiq Hasan", "email": visitor, "summary": summary or "40 large planters for 3 cafes."}
            text, _, _, _ = turn(api, site_key, lead_cid, form=("lead", values))
            time.sleep(SETTLE_SECONDS)
            if db_count(tenant_requests, site_key) or mails_to(mailpit, team) or len(receiver.events) != 1:
                raise CheckFailed("something was stored or sent without consent")
            ok(f"contact form without consent: nothing stored, emailed or posted ({text[:50]!r}…)")

            # 3. With consent: stored, emailed, posted.
            text, blocks, _, _ = turn(api, site_key, lead_cid, form=("lead", {**values, "consent": "yes"}))
            if not any(b.get("type") == "summary" for b in blocks) or db_count(tenant_requests, site_key) != 1:
                raise CheckFailed(f"the consented request wasn't stored/confirmed: {text!r}")
            mail = team_mail(mailpit, team, "New enquiry from Rafiq Hasan")
            reply_to = [a.get("Address") for a in mail.get("ReplyTo") or []]
            if reply_to != [visitor] or "Consent: the customer ticked" not in mail.get("Text", ""):
                raise CheckFailed(f"the team email is missing Reply-To or the consent line: {reply_to}")
            wait_for("the signed lead webhook", lambda: any(e["kind"] == "lead" for e in receiver.events),
                     DELIVERY_TIMEOUT)
            ok("with consent: stored, emailed to the team (Reply-To: visitor), signed webhook received")

            # 4. A return for a delivered order.
            _, _, ret_cid, _ = turn(api, site_key, message=f"Where is my order #{RETURN_ORDER}?")
            turn(api, site_key, ret_cid, form=("order_lookup", {"order_number": RETURN_ORDER, "email": customer}))
            _, blocks, _, _ = turn(api, site_key, ret_cid, form=("order_code", {"code": order_code(mailpit, customer)}))
            if not any(b.get("type") == "chips" and b["items"][0]["label"] == "Start a return" for b in blocks):
                raise CheckFailed(f"no 'Start a return' after verifying a delivered order: {blocks}")
            text, blocks, _, _ = turn(api, site_key, ret_cid, "I'd like to return something from this order")
            items = next(f for f in form_in(blocks, "return_request")["fields"] if f["name"] == "items")
            text, blocks, _, _ = turn(api, site_key, ret_cid, form=("return_request", {
                "items": [items["options"][0]["id"]], "reason": "reason_2", "refund": "refund_1", "consent": "yes"}))
            if not any(b.get("type") == "summary" for b in blocks):
                raise CheckFailed(f"the return request wasn't confirmed: {text!r}")
            mail = team_mail(mailpit, team, f"Return request: order #{RETURN_ORDER}")
            if items["options"][0]["label"] not in mail.get("Text", ""):
                raise CheckFailed("the return email doesn't name the item")
            wait_for("the signed return webhook", lambda: any(e["kind"] == "return" for e in receiver.events),
                     DELIVERY_TIMEOUT)
            ok(f"return request for {RETURN_ORDER} ({items['options'][0]['label']}): team email + signed webhook")

            # 5. Delete my data.
            _, blocks, _, _ = turn(api, site_key, lead_cid, "Please delete my data")
            form_in(blocks, "delete_data")
            text, _, _, events = turn(api, site_key, lead_cid, form=("delete_data", {"confirm": "yes"}))
            left = db_count("SELECT count(*) FROM conversations WHERE id = %s", lead_cid) + \
                db_count("SELECT count(*) FROM requests WHERE conversation_id = %s", lead_cid)
            if "forget" not in events or left:
                raise CheckFailed(f"delete my data left {left} rows (events {events})")
            if receiver.bad_signatures:
                raise CheckFailed(f"{receiver.bad_signatures} webhook deliveries had a bad signature")
            ok("delete my data: conversation, messages and its request gone; widget told to forget")
        finally:
            shop.shutdown()
            hook_server.shutdown()
    return passed


def main() -> None:
    """Serve the fixture site, run the stages, print the verdict."""
    sys.stdout.reconfigure(encoding="utf-8")
    server = serve_sites()
    print(f"chat forms check against {settings.app_base_url}\n")
    try:
        passed = run()
    except (CheckFailed, httpx.HTTPError) as exc:
        print(f"\nFAILED: {exc}")
        sys.exit(1)
    finally:
        server.shutdown()
    print(f"\nPASSED: {len(passed)} stages")


if __name__ == "__main__":
    main()
