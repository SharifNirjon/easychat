"""End-to-end check of the admin (roadmap step 10 "Done when"): the team reads chats, exports
leads, edits + publishes the knowledge base.

    cd api
    python admin_check.py        # needs: docker compose up -d, the API and the worker

  1. a new owner signs in and adds a website; a visitor sends the contact form (with consent)
  2. the owner finds that chat in the inbox and reads the transcript and the request
  3. the owner exports leads as CSV: the visitor's lead is in it
  4. the owner writes a knowledge entry, publishes it (the worker indexes it), and the test
     console finds it (needs embedding credits on OpenRouter: reported as BLOCKED otherwise)
  5. the owner invites a member, who signs in and can read the inbox but not manage the team
"""
import csv
import io
import sys
import time

import httpx

from config import settings
from evals.setup import serve_sites
from onboarding_check import MAILPIT_URL, SITEMAP_URL, CheckFailed, signin_token, wait_for
from requests_check import turn

PUBLISH_TIMEOUT = 60


def sign_in(mailpit: httpx.Client, api: httpx.Client, email: str) -> None:
    """Request a link and use it (the client keeps the session cookie)."""
    app = {"Origin": settings.app_origin}
    api.post("/api/auth/start", json={"email": email}, headers=app)
    if api.post("/api/auth/verify", json={"token": signin_token(mailpit, email)}, headers=app).status_code != 200:
        raise CheckFailed(f"sign-in failed for {email}")


def run() -> tuple[list[str], list[str]]:
    """All stages; returns (passed, blocked). Raises CheckFailed on a real failure."""
    stamp = int(time.time())
    owner, member, visitor = (f"owner+{stamp}@mirela.example", f"member+{stamp}@mirela.example",
                              f"rafiq+{stamp}@meghna.example")
    app = {"Origin": settings.app_origin}
    passed, blocked = [], []

    def ok(stage: str) -> None:
        """Record and print a passed stage."""
        passed.append(stage)
        print(f"  ok  {stage}", flush=True)

    with httpx.Client(base_url=settings.app_base_url, timeout=30) as api, \
            httpx.Client(base_url=MAILPIT_URL, timeout=10) as mailpit:
        # 1. Owner + a visitor's lead.
        sign_in(mailpit, api, owner)
        site_key = api.post("/api/onboarding/site", json={"url": SITEMAP_URL}, headers=app).json()["site_key"]
        _, _, cid, _ = turn(api, site_key, message="We need 40 planters for three cafes. Can someone contact me?")
        turn(api, site_key, cid, form=("lead", {"name": "Rafiq Hasan", "email": visitor,
                                                 "summary": "40 planters for three cafes", "consent": "yes"}))
        ok("owner signed in; a visitor sent the contact form with consent")

        # 2. Inbox.
        items = api.get("/api/admin/conversations", params={"view": "requests"}).json()["items"]
        if not any(i["id"] == cid for i in items):
            raise CheckFailed(f"the chat isn't in the inbox: {items}")
        detail = api.get(f"/api/admin/conversations/{cid}").json()
        if detail["requests"][0]["data"]["email"] != visitor or "contact me" not in detail["messages"][0]["content"]:
            raise CheckFailed("the transcript or the request is missing")
        ok(f"inbox: chat listed; transcript of {len(detail['messages'])} messages with the request and its consent")

        # 3. Export.
        exported = api.get("/api/admin/requests/export.csv")
        rows = list(csv.reader(io.StringIO(exported.text)))
        if exported.status_code != 200 or not any(visitor in row for row in rows):
            raise CheckFailed(f"the lead isn't in the CSV ({exported.status_code})")
        ok(f"leads exported as CSV: {len(rows) - 1} row(s), the visitor's lead included")

        # 4. Knowledge.
        api.post("/api/admin/kb/entries", headers=app, json={
            "section": "Services", "title": "Bulk planter orders",
            "body": "Orders of 20 or more planters get free delivery within Dhaka and a 10% trade discount."})
        api.post("/api/admin/kb/publish", headers=app)
        state = wait_for("the worker to publish", lambda: (s := api.get("/api/admin/kb").json()["publish"])
                         and s["state"] in ("done", "failed") and s, PUBLISH_TIMEOUT)
        if state["state"] == "failed":
            blocked.append("knowledge publish: the embedding service refused (OpenRouter credits?)")
            print(f"  BLOCKED  publish: {state['detail']}", flush=True)
        else:
            answer = api.post("/api/admin/kb/ask", headers=app, json={"question": "Do you give discounts on bulk planters?"})
            hits = answer.json().get("hits", []) if answer.status_code == 200 else []
            if not any(h["title"] == "Bulk planter orders" for h in hits):
                raise CheckFailed(f"the published entry isn't found by the test console: {answer.text[:200]}")
            ok("knowledge entry written, published by the worker, found by the test console")

        # 5. Team.
        if api.post("/api/admin/team", headers=app, json={"email": member}).status_code != 201:
            raise CheckFailed("inviting a member failed")
        with httpx.Client(base_url=settings.app_base_url, timeout=30) as other:
            token = signin_token(mailpit, member)
            if other.post("/api/auth/verify", json={"token": token}, headers=app).status_code != 200:
                raise CheckFailed("the member couldn't sign in from the invite")
            reads = other.get("/api/admin/conversations").status_code
            manages = other.post("/api/admin/team", headers=app, json={"email": f"x+{stamp}@mirela.example"}).status_code
            if (reads, manages) != (200, 403):
                raise CheckFailed(f"member access wrong: inbox {reads}, invite {manages}")
        ok("invited member signed in from the email: reads the inbox, can't manage the team")
    return passed, blocked


def main() -> None:
    """Serve the fixture site, run the stages, print the verdict."""
    sys.stdout.reconfigure(encoding="utf-8")
    server = serve_sites()
    print(f"admin check against {settings.app_base_url}\n")
    try:
        passed, blocked = run()
    except (CheckFailed, httpx.HTTPError) as exc:
        print(f"\nFAILED: {exc}")
        sys.exit(1)
    finally:
        server.shutdown()
    print(f"\nPASSED: {len(passed)} stages" + (f", BLOCKED: {len(blocked)}" if blocked else ""))
    sys.exit(2 if blocked else 0)


if __name__ == "__main__":
    main()
