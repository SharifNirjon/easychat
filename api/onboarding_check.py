"""Timed end-to-end check of self-serve onboarding (roadmap step 7 "Done when"):
a brand-new email to a working, installed widget in under 5 minutes, with no staff action.

    cd api
    python onboarding_check.py              # needs: docker compose up -d, the API, the worker
    python onboarding_check.py --skip-chat  # without the model call (e.g. no OpenRouter credits)

What it does, against the running services (nothing is faked):
  1. asks for a sign-in link for a new address and reads the email from Mailpit's API
  2. uses the link: the account and tenant are created
  3. gives the Fernwood fixture site (evals/sites, served here on 127.0.0.1:8799) as the website
  4. waits for the worker to crawl it
  5. asks the new assistant a question through /chat, as a visitor on that site
  6. loads the widget config from the site's origin: the install is detected, the tenant goes live
Each stage is timed; the exit code is 0 only if everything worked within TIME_LIMIT_SECONDS.
"""
import argparse
import json
import os
import re
import sys
import time

import httpx

from config import settings
from evals.setup import SITE_HOST, SITE_PORT, serve_sites

TIME_LIMIT_SECONDS = 300
MAILPIT_URL = os.getenv("MAILPIT_URL", "http://localhost:8025").rstrip("/")
SITE_ORIGIN = f"http://{SITE_HOST}:{SITE_PORT}"
SITEMAP_URL = f"{SITE_ORIGIN}/fernwood/sitemap.xml"
QUESTION = "Do you offer free shipping?"
POLL_SECONDS = 1.0
_TOKEN = re.compile(r"#signin=([A-Za-z0-9_-]+)")


class CheckFailed(RuntimeError):
    """A stage didn't work: the message says which and why."""


def wait_for(what: str, probe, timeout: float):
    """Call probe() until it returns something truthy or `timeout` seconds pass."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = probe()
        if result:
            return result
        time.sleep(POLL_SECONDS)
    raise CheckFailed(f"timed out waiting for {what}")


def signin_token(mailpit: httpx.Client, email: str) -> str:
    """The token in the newest sign-in email to `email`, from Mailpit's API."""
    def probe():
        found = mailpit.get("/api/v1/search", params={"query": f"to:{email}"}).json().get("messages", [])
        if not found:
            return None
        text = mailpit.get(f"/api/v1/message/{found[0]['ID']}").json().get("Text", "")
        match = _TOKEN.search(text)
        return match.group(1) if match else None
    return wait_for("the sign-in email in Mailpit", probe, 30)


def ask(api: httpx.Client, site_key: str) -> str:
    """One question through /chat as a visitor on the owner's site; returns the reply text."""
    text, done = "", False
    with api.stream("POST", "/chat", json={"site_key": site_key, "message": QUESTION},
                    headers={"Origin": SITE_ORIGIN}, timeout=90) as response:
        if response.status_code != 200:
            raise CheckFailed(f"/chat returned HTTP {response.status_code}")
        event = None
        for line in response.iter_lines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: ") and event == "token":
                text += json.loads(line[6:])["t"]
            elif line.startswith("data: ") and event == "error":
                raise CheckFailed(f"/chat error: {line[6:]}")
            elif event == "done":
                done = True
    if not done or not text.strip():
        raise CheckFailed("/chat ended without a reply")
    return text


def run(skip_chat: bool) -> list[tuple[str, float]]:
    """All stages in order; returns (stage, seconds since start) marks."""
    email = f"owner+{int(time.time())}@fernwood.example"
    app = {"Origin": settings.app_origin}
    marks: list[tuple[str, float]] = []
    started = time.monotonic()

    def mark(stage: str) -> None:
        """Record and print a finished stage."""
        marks.append((stage, time.monotonic() - started))
        print(f"  {marks[-1][1]:6.1f}s  {stage}", flush=True)

    with httpx.Client(base_url=settings.app_base_url, timeout=30) as api, \
            httpx.Client(base_url=MAILPIT_URL, timeout=10) as mailpit:
        if api.post("/api/auth/start", json={"email": email}, headers=app).status_code != 202:
            raise CheckFailed("sign-in request refused")
        token = signin_token(mailpit, email)
        mark(f"sign-in email received ({email})")

        if api.post("/api/auth/verify", json={"token": token}, headers=app).status_code != 200:
            raise CheckFailed("sign-in link refused")
        mark("signed in: account and tenant created")

        response = api.post("/api/onboarding/site", json={"url": SITEMAP_URL}, headers=app)
        if response.status_code != 200:
            raise CheckFailed(f"website step failed: {response.text[:300]}")
        state = response.json()
        mark(f"website read: '{state['assistant']['company_name']}', key {state['site_key']}")

        def crawl_finished():
            """The latest state once the crawl is done (or failed)."""
            current = api.get("/api/onboarding").json()
            return current if current["crawl"] and current["crawl"]["status"] in ("done", "failed") else None
        state = wait_for("the crawl (is `python worker.py` running?)", crawl_finished, TIME_LIMIT_SECONDS)
        if state["crawl"]["status"] != "done":
            raise CheckFailed(f"crawl failed: {state['crawl']['error']}")
        mark(f"site learned: {state['crawl']['pages_indexed'] + state['crawl']['pages_unchanged']} pages")

        if not skip_chat:
            reply = ask(api, state["site_key"])
            mark(f"assistant answered: {reply[:80]!r}")

        if api.get("/config", params={"key": state["site_key"]}, headers={"Origin": SITE_ORIGIN}).status_code != 200:
            raise CheckFailed("the widget config didn't load on the owner's site")
        state = api.get("/api/onboarding").json()
        if not state["installed_at"] or state["status"] != "live":
            raise CheckFailed("install not detected")
        mark("widget seen on the site: installed ✓, tenant live")
    return marks


def main() -> None:
    """Serve the fixture site, run the stages, print the verdict."""
    parser = argparse.ArgumentParser(description="Timed end-to-end onboarding check (step 7).")
    parser.add_argument("--skip-chat", action="store_true", help="don't call the chat model")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    server = serve_sites()
    print(f"onboarding check against {settings.app_base_url} (limit {TIME_LIMIT_SECONDS}s)\n")
    try:
        marks = run(args.skip_chat)
    except (CheckFailed, httpx.HTTPError) as exc:
        print(f"\nFAILED: {exc}")
        sys.exit(1)
    finally:
        server.shutdown()
    total = marks[-1][1]
    verdict = "PASSED" if total < TIME_LIMIT_SECONDS else "FAILED (too slow)"
    print(f"\n{verdict}: email to installed widget in {total:.1f}s, no staff action")
    sys.exit(0 if total < TIME_LIMIT_SECONDS else 1)


if __name__ == "__main__":
    main()
