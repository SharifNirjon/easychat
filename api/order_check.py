"""End-to-end check of order tracking (roadmap step 8 "Done when"): a verified visitor sees the
timeline; a wrong code or a wrong email reveals nothing.

    cd api
    python order_check.py               # needs: docker compose up -d, the API (worker optional)
    python order_check.py --skip-chat   # without the model follow-up (e.g. no OpenRouter credits)

Against the running services (only the shop is a stand-in: demo_shop.py, the generic order API):
  1. a new owner signs in (Mailpit), adds a website, connects the demo shop's order API and
     runs a test lookup from the onboarding app's endpoint
  2. a visitor asks "where's my order?" and gets the lookup form, not a model answer
  3. wrong email: the same answer as a right one, and no email is sent to anyone
  4. wrong codes: no order data in any reply
  5. right email + the code from Mailpit: the timeline card for that order
  6. (unless --skip-chat) a follow-up question answered by the model from the verified order
"""
import argparse
import json
import sys
import time

import httpx

import demo_shop
from config import settings
from evals.setup import serve_sites
from onboarding_check import MAILPIT_URL, SITE_ORIGIN, SITEMAP_URL, CheckFailed, signin_token, wait_for

ORDER = "MR-20417"
WRONG_CODE_TRIES = 2
EMAIL_SETTLE_SECONDS = 3        # time for a (wrongly) sent email to reach Mailpit before we check


def chat(api: httpx.Client, site_key: str, conversation_id: str | None = None, message: str | None = None,
         form: tuple[str, dict] | None = None) -> tuple[str, list[dict], str]:
    """One /chat turn as a visitor on the shop's site: (text, blocks, conversation id)."""
    body: dict = {"site_key": site_key}
    if conversation_id:
        body["conversation_id"] = conversation_id
    if message is not None:
        body["message"] = message
    if form is not None:
        body["form"] = {"name": form[0], "values": form[1]}
    text, blocks, cid, event = "", [], conversation_id, None
    with api.stream("POST", "/chat", json=body, headers={"Origin": SITE_ORIGIN}, timeout=90) as response:
        if response.status_code != 200:
            raise CheckFailed(f"/chat returned HTTP {response.status_code}: {response.read()[:200]!r}")
        for line in response.iter_lines():
            if line.startswith("event: "):
                event = line[7:]
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
    return text, blocks, cid


def mails_to(mailpit: httpx.Client, address: str) -> list[dict]:
    """Emails in Mailpit addressed to `address`, newest first."""
    return mailpit.get("/api/v1/search", params={"query": f"to:{address}"}).json().get("messages", [])


def order_code(mailpit: httpx.Client, address: str) -> str:
    """The 6-digit code in the newest order email to `address`."""
    def probe():
        found = mails_to(mailpit, address)
        if not found:
            return None
        text = mailpit.get(f"/api/v1/message/{found[0]['ID']}").json().get("Text", "")
        return next((line.strip() for line in text.splitlines() if line.strip().isdigit() and len(line.strip()) == 6),
                    None)
    return wait_for("the order code email in Mailpit", probe, 30)


def timeline_in(blocks: list[dict]) -> dict | None:
    """The order card, if the reply has one."""
    return next((b for b in blocks if b.get("type") == "timeline"), None)


def run(skip_chat: bool) -> list[str]:
    """All stages; returns what passed (raises CheckFailed on the first failure)."""
    stamp = int(time.time())
    owner, customer, stranger = (f"owner+{stamp}@mirela.example", f"customer+{stamp}@mirela.example",
                                 f"stranger+{stamp}@mirela.example")
    app = {"Origin": settings.app_origin}
    passed: list[str] = []

    def ok(stage: str) -> None:
        """Record and print a passed stage."""
        passed.append(stage)
        print(f"  ok  {stage}", flush=True)

    with httpx.Client(base_url=settings.app_base_url, timeout=30) as api, \
            httpx.Client(base_url=MAILPIT_URL, timeout=10) as mailpit:
        # 1. The owner: sign in, website, connect orders, test lookup.
        api.post("/api/auth/start", json={"email": owner}, headers=app)
        if api.post("/api/auth/verify", json={"token": signin_token(mailpit, owner)}, headers=app).status_code != 200:
            raise CheckFailed("owner sign-in failed")
        state = api.post("/api/onboarding/site", json={"url": SITEMAP_URL}, headers=app).json()
        site_key = state["site_key"]
        connected = api.put("/api/onboarding/orders/api", headers=app,
                            json={"url": f"http://{demo_shop.HOST}:{demo_shop.PORT}/orders"})
        if connected.status_code != 200:
            raise CheckFailed(f"connecting the order API failed: {connected.text[:200]}")
        shop = demo_shop.serve(connected.json()["secret"], customer)
        try:
            test = api.post("/api/onboarding/orders/test", json={"order_number": ORDER}, headers=app).json()
            if not test.get("found"):
                raise CheckFailed(f"the owner's test lookup didn't find {ORDER}: {test}")
            ok(f"owner connected the order API; test lookup: {test['status']}")

            # 2. The visitor asks.
            text, blocks, cid = chat(api, site_key, message=f"Hi, where is my order #{ORDER}?")
            if not blocks or blocks[0].get("submit") != "order_lookup":
                raise CheckFailed(f"expected the lookup form, got {text!r} {blocks}")
            ok("order question -> lookup form (no model answer)")

            # 3. Wrong email: same answer, no email anywhere.
            wrong_text, wrong_blocks, _ = chat(api, site_key, cid, form=("order_lookup",
                                                                          {"order_number": ORDER, "email": stranger}))
            time.sleep(EMAIL_SETTLE_SECONDS)
            if mails_to(mailpit, stranger) or mails_to(mailpit, customer):
                raise CheckFailed("a wrong email caused a code email")

            # 4. Wrong codes: nothing revealed.
            for _ in range(WRONG_CODE_TRIES):
                text, blocks, _ = chat(api, site_key, cid, form=("order_code", {"code": "000000"}))
                if timeline_in(blocks) or ORDER in text:
                    raise CheckFailed("a wrong code revealed order data")
            ok("wrong email: same reply, no email sent; wrong codes: no order data")

            # 5. The real customer.
            right_text, right_blocks, _ = chat(api, site_key, cid, form=("order_lookup",
                                                                          {"order_number": ORDER, "email": customer}))
            if (right_text, right_blocks) != (wrong_text, wrong_blocks):
                raise CheckFailed("the lookup answer differs between a right and a wrong email")
            ok("right email gets exactly the same answer as the wrong one")
            code = order_code(mailpit, customer)
            text, blocks, _ = chat(api, site_key, cid, form=("order_code", {"code": code}))
            card = timeline_in(blocks)
            if not card or card.get("eyebrow") != f"Order #{ORDER}":
                raise CheckFailed(f"no timeline after the right code: {text!r} {blocks}")
            steps = ", ".join(f"{s['label']} ({s['state']})" for s in card["steps"])
            ok(f"right code -> timeline '{card['title']}': {steps}")

            # 6. A follow-up, answered by the model from the verified order.
            if not skip_chat:
                text, _, _ = chat(api, site_key, cid, message="When will it arrive?")
                ok(f"follow-up answered: {text[:100]!r}")
        finally:
            shop.shutdown()
    return passed


def main() -> None:
    """Serve the fixture site, run the stages, print the verdict."""
    parser = argparse.ArgumentParser(description="End-to-end order tracking check (step 8).")
    parser.add_argument("--skip-chat", action="store_true", help="don't call the chat model")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    server = serve_sites()
    print(f"order tracking check against {settings.app_base_url}\n")
    try:
        passed = run(args.skip_chat)
    except (CheckFailed, httpx.HTTPError) as exc:
        print(f"\nFAILED: {exc}")
        sys.exit(1)
    finally:
        server.shutdown()
    print(f"\nPASSED: {len(passed)} stages")


if __name__ == "__main__":
    main()
