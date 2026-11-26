"""Sign-in by email link (auth.py): one-time tokens, sessions, CSRF, no enumeration, sign-up caps."""
import re

import httpx
import pytest

import limits
import mailer
from config import settings

APP = {"Origin": settings.app_origin}
_LINK_TOKEN = re.compile(r"#signin=([A-Za-z0-9_-]+)")


class Outbox:
    """Stands in for mailer.send_email: keeps what would have been sent."""

    def __init__(self):
        """Nothing sent yet; sending works."""
        self.sent: list[tuple[str, str, str]] = []
        self.reply_to: list[str | None] = []        # each email's Reply-To, in the same order
        self.fail = False

    async def __call__(self, to: str, subject: str, text: str, reply_to: str | None = None) -> None:
        """Record the email (or fail like an SMTP outage)."""
        if self.fail:
            raise mailer.MailError("down")
        self.sent.append((to, subject, text))
        self.reply_to.append(reply_to)

    def last_token(self) -> str:
        """The token in the most recent sign-in email."""
        return _LINK_TOKEN.search(self.sent[-1][2]).group(1)


@pytest.fixture
def outbox(monkeypatch):
    """Capture outgoing email instead of sending it."""
    box = Outbox()
    monkeypatch.setattr(mailer, "send_email", box)
    return box


async def request_link(client, email: str = "owner@green-leaf.example", headers=APP) -> httpx.Response:
    """Ask for a sign-in link, as the app does."""
    return await client.post("/api/auth/start", json={"email": email}, headers=headers)


async def sign_in(client, outbox, email: str = "owner@green-leaf.example") -> httpx.Response:
    """The full flow: request a link, then use its token. The client keeps the session cookie."""
    assert (await request_link(client, email)).status_code == 202
    response = await client.post("/api/auth/verify", json={"token": outbox.last_token()}, headers=APP)
    assert response.status_code == 200, response.text
    return response


async def test_the_email_contains_a_one_time_link(client, outbox):
    """The link points at the app and carries the token in the URL fragment."""
    await request_link(client)
    to, subject, text = outbox.sent[-1]
    assert to == "owner@green-leaf.example" and "sign-in" in subject.lower()
    assert f"{settings.app_base_url}/app/#signin=" in text


async def test_first_sign_in_creates_a_tenant_and_its_owner(client, outbox, owner_db):
    """A new email gets a free-plan tenant in onboarding, with itself as owner."""
    response = await sign_in(client, outbox, "New.Owner@Green-Leaf.example")
    assert response.json() == {"email": "new.owner@green-leaf.example"}
    assert "ec_session" in response.cookies
    row = owner_db.execute(
        "SELECT u.role, t.status, t.plan, t.slug FROM users u JOIN tenants t ON t.id = u.tenant_id "
        "WHERE u.email = 'new.owner@green-leaf.example'").fetchone()
    assert row[:3] == ("owner", "onboarding", "free") and row[3].startswith("t-")


async def test_signing_in_again_reuses_the_account(client, outbox, owner_db):
    """One email, one tenant: a second sign-in doesn't create another."""
    await sign_in(client, outbox)
    await sign_in(client, outbox)
    assert owner_db.execute("SELECT count(*) FROM users WHERE email = 'owner@green-leaf.example'").fetchone()[0] == 1


async def test_a_token_works_once(client, outbox):
    """Using the same link twice fails the second time."""
    await request_link(client)
    token = outbox.last_token()
    assert (await client.post("/api/auth/verify", json={"token": token}, headers=APP)).status_code == 200
    assert (await client.post("/api/auth/verify", json={"token": token}, headers=APP)).status_code == 400


async def test_made_up_tokens_are_rejected(client):
    """A token we never issued is refused."""
    r = await client.post("/api/auth/verify", json={"token": "x" * 43}, headers=APP)
    assert r.status_code == 400


async def test_no_email_enumeration(client, outbox):
    """Known and unknown addresses get exactly the same answer."""
    await sign_in(client, outbox, "known@green-leaf.example")
    known = await request_link(client, "known@green-leaf.example")
    unknown = await request_link(client, "nobody@green-leaf.example")
    assert (known.status_code, known.json()) == (unknown.status_code, unknown.json()) == (202, {"ok": True})


async def test_redis_never_holds_a_usable_token(client, outbox):
    """Only hashes of tokens and session ids are stored."""
    response = await sign_in(client, outbox)
    session_id = response.cookies["ec_session"]
    await request_link(client)
    token = outbox.last_token()
    keys = [k.decode() for k in await limits.client.keys("auth:*")]
    assert keys and not any(token in k or session_id in k for k in keys)


async def test_onboarding_needs_a_session(client):
    """Without signing in, the onboarding API is 401."""
    assert (await client.get("/api/onboarding")).status_code == 401


async def test_logout_ends_the_session_server_side(client, outbox):
    """After logout, even a copy of the old cookie no longer works."""
    session_id = (await sign_in(client, outbox)).cookies["ec_session"]
    assert (await client.get("/api/onboarding")).status_code == 200
    assert (await client.post("/api/auth/logout", headers=APP)).status_code == 204
    client.cookies.set("ec_session", session_id)
    assert (await client.get("/api/onboarding")).status_code == 401


@pytest.mark.parametrize("headers", [{"Origin": "https://evil.example"}, {}])
async def test_cross_site_requests_are_refused(client, headers):
    """State-changing requests from another origin (or without one) are 403 (CSRF)."""
    assert (await request_link(client, headers=headers)).status_code == 403


@pytest.mark.parametrize("email", ["not-an-email", "a@b", "two@@at.example", "x" * 250 + "@a.example"])
async def test_invalid_emails_are_422(client, outbox, email):
    """Obviously invalid addresses are rejected before anything is sent."""
    assert (await request_link(client, email)).status_code == 422
    assert outbox.sent == []


async def test_sign_in_emails_per_address_are_capped(client, outbox):
    """One inbox can't be flooded with sign-in links."""
    codes = [(await request_link(client)).status_code for _ in range(limits.SIGNIN_EMAILS_PER_ADDRESS_HOUR + 1)]
    assert codes[-1] == 429 and set(codes[:-1]) == {202}


async def test_new_accounts_per_client_are_capped(client, outbox):
    """One network can't create unlimited free accounts."""
    for n in range(limits.SIGNUPS_PER_IP_DAY):
        await sign_in(client, outbox, f"owner{n}@green-leaf.example")
    await request_link(client, "one-too-many@green-leaf.example")
    r = await client.post("/api/auth/verify", json={"token": outbox.last_token()}, headers=APP)
    assert r.status_code == 429


async def test_mail_outage_is_reported_and_the_token_dropped(client, outbox):
    """If the email can't be sent, the visitor is told, and no usable token is left behind."""
    outbox.fail = True
    assert (await request_link(client)).status_code == 503
    assert await limits.client.keys("auth:token:*") == []
