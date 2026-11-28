"""Sign-in by email link, and sessions for the onboarding app (roadmap step 7).

Flow:
  POST /api/auth/start  {email}  -> emails a one-time link; always 202, whether or not the
                                    address has an account (no email enumeration)
  POST /api/auth/verify {token}  -> consumes the token, creates the account on first sign-in,
                                    sets the session cookie
  POST /api/auth/logout          -> ends the session

Why the link opens a page with a "Sign in" button instead of signing in on GET: email security
scanners open links automatically and would use up a one-time token.

Tokens and sessions live in Redis under the SHA-256 of their value: a Redis dump doesn't
contain usable credentials. Sign-in tokens last 15 minutes and work once (GETDEL); sessions
last 7 days and are revocable (logout deletes them). The link carries the token in the URL
fragment (#), which browsers never send to servers or put in Referer headers.

CSRF: every state-changing /api request must come from the app's own origin (same_origin),
on top of the SameSite=Lax cookie.

Team (step 10): an owner invites members (admin.py); an invite is a user row plus a sign-in link.
Every request re-reads the user's row, so a removed member is signed out at once and the role
(owner / member) is always current; owner_only() guards team management.
"""
import hashlib
import json
import re
import secrets
from dataclasses import asdict, dataclass
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator

import db
import limits
import mailer
from branding import placeholder_config
from config import settings
from models import normalize_origin

SIGNIN_TOKEN_TTL_SECONDS = 15 * 60
SESSION_TTL_SECONDS = 7 * 86400
SESSION_COOKIE = "ec_session"
SLUG_ATTEMPTS = 3
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,189}\.[^@\s.]{2,63}$")
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def same_origin(request: Request) -> None:
    """403 unless a state-changing request comes from the onboarding app's own origin."""
    if request.method in _UNSAFE_METHODS:
        origin = request.headers.get("origin")
        if origin is None or normalize_origin(origin) != settings.app_origin:
            raise HTTPException(status_code=403, detail="Cross-site request refused")


router = APIRouter(prefix="/api/auth", dependencies=[Depends(same_origin)])


class EmailIn(BaseModel):
    """A sign-in request."""
    email: str = Field(max_length=254)

    @field_validator("email")
    @classmethod
    def plausible_email(cls, value: str) -> str:
        """Lowercase, trimmed, and shaped like an address (the email itself is the real check)."""
        value = value.strip().lower()
        if not _EMAIL.match(value):
            raise ValueError("enter a valid email address")
        return value


class TokenIn(BaseModel):
    """The token from a sign-in link."""
    token: str = Field(pattern=r"^[A-Za-z0-9_-]{20,100}$")


@dataclass(frozen=True)
class Session:
    """Who is signed in. The tenant always comes from here, never from a request body."""
    user_id: UUID
    tenant_id: UUID
    email: str
    role: str = "owner"           # read from the users row on every request (not stored in Redis)


def _digest(secret: str) -> str:
    """What Redis stores instead of a token or session id."""
    return hashlib.sha256(secret.encode()).hexdigest()


def _token_key(token: str) -> str:
    """Redis key of a sign-in token."""
    return f"auth:token:{_digest(token)}"


def _session_key(session_id: str) -> str:
    """Redis key of a session."""
    return f"auth:session:{_digest(session_id)}"


def signin_email(link: str) -> str:
    """The plain-text sign-in email."""
    return (
        "Hi,\n\n"
        "Use this link to sign in to easychat and set up your website assistant:\n\n"
        f"{link}\n\n"
        f"The link works once and expires in {SIGNIN_TOKEN_TTL_SECONDS // 60} minutes.\n"
        "If you didn't ask for it, you can ignore this email.\n"
    )


def invite_email(company: str, inviter: str, link: str) -> str:
    """The plain-text email inviting a team member (step 10)."""
    return (
        "Hi,\n\n"
        f"{inviter} invited you to the {company} team on easychat, to read the website assistant's chats "
        "and leads.\n\n"
        f"Sign in with this link:\n\n{link}\n\n"
        f"The link works once and expires in {SIGNIN_TOKEN_TTL_SECONDS // 60} minutes. Later, sign in at "
        f"{settings.app_base_url}/app/ with this email address.\n"
        "If you don't know why you got this, you can ignore it.\n"
    )


async def send_link(email: str, subject: str, text_for_link) -> None:
    """Create a one-time sign-in token for `email` and email the link (text_for_link(link) -> body).
    503 if the email can't be sent (the token is then dropped)."""
    token = secrets.token_urlsafe(32)
    await limits.client.set(_token_key(token), email, ex=SIGNIN_TOKEN_TTL_SECONDS)
    link = f"{settings.app_base_url}/app/#signin={token}"
    try:
        await mailer.send_email(email, subject, text_for_link(link))
    except mailer.MailError as exc:
        await limits.client.delete(_token_key(token))
        raise HTTPException(status_code=503, detail="We couldn't send the email. Please try again.") from exc


@router.post("/start", status_code=202)
async def start(body: EmailIn, request: Request) -> dict:
    """Email a one-time sign-in link. Same answer whether or not the address has an account."""
    await limits.check_signin(body.email, limits.client_ip(request))
    await send_link(body.email, "Your easychat sign-in link", signin_email)
    return {"ok": True}


async def _account_for(email: str, request: Request) -> dict:
    """The user + tenant for this email, creating them on first sign-in (counts sign-up caps)."""
    account = await db.resolve_login(email)
    if account:
        return account
    await limits.count_signup(limits.client_ip(request))
    for _ in range(SLUG_ATTEMPTS):           # the slug is random; retry on the rare collision
        slug = f"t-{secrets.token_hex(6)}"
        account = await db.create_signup(email, slug, "My business",
                                         placeholder_config(f"{settings.app_base_url}/app/"))
        if account:
            return account
    raise HTTPException(status_code=503, detail="Could not create your account. Please try again.")


def _set_session_cookie(response: Response, session_id: str) -> None:
    """HttpOnly (no JavaScript access), SameSite=Lax, Secure outside local dev."""
    response.set_cookie(SESSION_COOKIE, session_id, max_age=SESSION_TTL_SECONDS, httponly=True,
                        samesite="lax", secure=not settings.is_dev, path="/")


@router.post("/verify")
async def verify(body: TokenIn, request: Request, response: Response) -> dict:
    """Use a sign-in token (once) and start a session."""
    email = await limits.client.getdel(_token_key(body.token))
    if email is None:
        raise HTTPException(status_code=400, detail="This sign-in link is invalid or has expired. "
                                                    "Please request a new one.")
    email = email.decode() if isinstance(email, bytes) else email
    account = await _account_for(email, request)
    session = Session(user_id=account["user_id"], tenant_id=account["tenant_id"], email=email)
    async with db.tenant_tx(session.tenant_id) as conn:
        await conn.execute("UPDATE users SET last_login_at = now() WHERE id = %s", (session.user_id,))
    session_id = secrets.token_urlsafe(32)
    stored = json.dumps({k: str(v) for k, v in asdict(session).items() if k != "role"})
    await limits.client.set(_session_key(session_id), stored, ex=SESSION_TTL_SECONDS)
    _set_session_cookie(response, session_id)
    return {"email": email}


async def current_session(ec_session: str | None = Cookie(default=None)) -> Session:
    """FastAPI dependency: the signed-in session, or 401. The user must still exist (a member the
    owner removed is signed out at once); the role comes from the database, never the cookie."""
    if not ec_session or len(ec_session) > 100:
        raise HTTPException(status_code=401, detail="Please sign in")
    raw = await limits.client.get(_session_key(ec_session))
    if raw is None:
        raise HTTPException(status_code=401, detail="Your session has expired. Please sign in again.")
    data = json.loads(raw)
    user_id, tenant_id = UUID(data["user_id"]), UUID(data["tenant_id"])
    async with db.tenant_tx(tenant_id) as conn:
        cur = await conn.execute("SELECT role FROM users WHERE id = %s", (user_id,))
        row = await cur.fetchone()
    if row is None:
        await limits.client.delete(_session_key(ec_session))
        raise HTTPException(status_code=401, detail="You no longer have access to this account.")
    return Session(user_id=user_id, tenant_id=tenant_id, email=data["email"], role=row["role"])


async def owner_only(session: Session = Depends(current_session)) -> Session:
    """FastAPI dependency: the session, if it is the account's owner (403 for members)."""
    if session.role != "owner":
        raise HTTPException(status_code=403, detail="Only the account owner can do this")
    return session


@router.post("/logout", status_code=204)
async def logout(response: Response, ec_session: str | None = Cookie(default=None)) -> None:
    """End the session (server-side too: the cookie alone stops working)."""
    if ec_session and len(ec_session) <= 100:
        await limits.client.delete(_session_key(ec_session))
    response.delete_cookie(SESSION_COOKIE, path="/")
