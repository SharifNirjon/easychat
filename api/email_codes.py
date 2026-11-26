"""One-time codes sent by email, checked in the chat (order tracking, step 8; transcript, step 9).

A code proves the visitor can read one inbox. It is:
  * 6 random digits, stored only as a keyed hash (secretbox.code_digest), so a Redis dump
    doesn't reveal it (a plain hash of 6 digits is reversed by trying a million values);
  * bound to one purpose, tenant, conversation and subject (`bound`: an order number, an
    email address...): it opens that one thing, in that one chat;
  * valid CODE_TTL_SECONDS, usable once, cancelled after CODE_ATTEMPTS wrong tries (counted with
    INCR, so parallel guesses all count).

`data` stored with a code is sealed (AES-GCM, same binding), e.g. the address a transcript goes to.
"""
import base64
import json
import re
import secrets
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

import limits
from secretbox import code_digest, open_sealed, same_digest, seal

CODE_DIGITS = 6
CODE_TTL_SECONDS = 10 * 60
CODE_ATTEMPTS = 5

Outcome = Literal["ok", "wrong", "too_many", "missing", "bad_format"]


@dataclass(frozen=True)
class CodeCheck:
    """The result of checking a code: on "ok" and "too_many", what it was bound to."""
    outcome: Outcome
    bound: str | None = None
    data: dict | None = None


def new_code() -> str:
    """Six random digits (leading zeros kept)."""
    return f"{secrets.randbelow(10 ** CODE_DIGITS):0{CODE_DIGITS}d}"


def _pending_key(purpose: str, tenant_id: UUID, conversation_id: UUID) -> str:
    """The code waiting to be entered in this conversation, for this purpose."""
    return f"codes:{purpose}:pending:{tenant_id}:{conversation_id}"


def _attempts_key(purpose: str, tenant_id: UUID, conversation_id: UUID) -> str:
    """Wrong tries against the pending code."""
    return f"codes:{purpose}:attempts:{tenant_id}:{conversation_id}"


def _context(purpose: str, tenant_id: UUID, conversation_id: UUID, bound: str) -> str:
    """Everything a code is bound to."""
    return f"{purpose}:{tenant_id}:{conversation_id}:{bound}"


async def store(purpose: str, tenant_id: UUID, conversation_id: UUID, bound: str, code: str,
                data: dict | None = None) -> None:
    """Remember `code` (hashed) as this conversation's pending code; replaces an earlier one."""
    context = _context(purpose, tenant_id, conversation_id, bound)
    pending = {"bound": bound, "digest": code_digest(code, context)}
    if data is not None:
        pending["sealed"] = base64.b64encode(seal(data, context)).decode()
    async with limits.client.pipeline(transaction=True) as pipe:
        pipe.set(_pending_key(purpose, tenant_id, conversation_id), json.dumps(pending), ex=CODE_TTL_SECONDS)
        pipe.delete(_attempts_key(purpose, tenant_id, conversation_id))
        await pipe.execute()


async def check(purpose: str, tenant_id: UUID, conversation_id: UUID, code: str) -> CodeCheck:
    """Check a code typed in this conversation. A right code is used up; too many wrong ones
    cancel it."""
    code = code.strip()
    if not re.fullmatch(rf"\d{{{CODE_DIGITS}}}", code):
        return CodeCheck("bad_format")
    pending_key = _pending_key(purpose, tenant_id, conversation_id)
    attempts_key = _attempts_key(purpose, tenant_id, conversation_id)
    raw = await limits.client.get(pending_key)
    if raw is None:
        return CodeCheck("missing")
    pending = json.loads(raw)
    bound = pending["bound"]
    context = _context(purpose, tenant_id, conversation_id, bound)
    if not same_digest(code_digest(code, context), pending["digest"]):
        async with limits.client.pipeline(transaction=True) as pipe:
            pipe.incr(attempts_key)
            pipe.expire(attempts_key, CODE_TTL_SECONDS)
            attempts, _ = await pipe.execute()
        if int(attempts) >= CODE_ATTEMPTS:
            await limits.client.delete(pending_key, attempts_key)
            return CodeCheck("too_many", bound)
        return CodeCheck("wrong")
    if not await limits.client.delete(pending_key):        # used at the same moment by another request
        return CodeCheck("missing")
    await limits.client.delete(attempts_key)
    data = open_sealed(base64.b64decode(pending["sealed"]), context) if "sealed" in pending else None
    return CodeCheck("ok", bound, data)


async def forget(purpose: str, tenant_id: UUID, conversation_id: UUID) -> None:
    """Drop this conversation's pending code (e.g. its data was deleted)."""
    await limits.client.delete(_pending_key(purpose, tenant_id, conversation_id),
                               _attempts_key(purpose, tenant_id, conversation_id))
