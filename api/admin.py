"""Admin v0 (roadmap step 10): the team's console behind sign-in, in the same app as onboarding.

  Inbox (A-01)      GET    /api/admin/conversations?view=&q=&page=   list
                    GET    /api/admin/conversations/{id}             transcript + its requests
                    PUT    /api/admin/conversations/{id}/status      {status: open | resolved}
                    DELETE /api/admin/conversations/{id}             delete the chat and its requests
  Leads (A-04)      GET    /api/admin/requests?kind=&status=&q=&page=
                    PUT    /api/admin/requests/{id}/status           {status: new | contacted | closed}
                    GET    /api/admin/requests/export.csv?kind=      CSV download
  Knowledge (A-05)  GET    /api/admin/kb                             pages, entries, publish state
                    POST   /api/admin/kb/entries, PUT/DELETE /api/admin/kb/entries/{id}
                    PUT    /api/admin/kb/pages/{id}                  {hidden}
                    POST   /api/admin/kb/publish                     queue the worker
                    POST   /api/admin/kb/ask                         test console (published knowledge)
  Team              GET    /api/admin/team, POST /api/admin/team (invite, owner), DELETE /api/admin/team/{id}

Every route needs a session; the tenant comes from it, and every query runs in db.tenant_tx (RLS).
Visitor and model text is returned as plain data: the app renders it as text, never as HTML.
Exports, deletions, publishing and team changes are written to admin_audit.
"""
import csv
import io
import logging
import re
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

import chat_forms
import db
import kb_admin
import limits
import retrieval
from auth import Session, current_session, invite_email, owner_only, same_origin, send_link
from embeddings import EmbeddingError
from grounding import NO_ANSWER_MARKER, knowledge_block
from guards import new_canary
from jobs import RedisQueue
from llm import Usage, complete
from models import EMAIL_PATTERN, TenantConfig, TenantLimits
from prompts import grounded_system_prompt

log = logging.getLogger("easychat.admin")

router = APIRouter(prefix="/api/admin", dependencies=[Depends(same_origin)])
queue = RedisQueue(limits.client)

PAGE_SIZE = 25
EXPORT_MAX_ROWS = 5000
CONSOLE_ANSWER_TOKENS = 300
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


async def audit(session: Session, action: str, detail: dict | None = None) -> None:
    """Record who did something that matters for privacy."""
    async with db.tenant_tx(session.tenant_id) as conn:
        await conn.execute("INSERT INTO admin_audit (tenant_id, user_email, action, detail) VALUES (%s, %s, %s, %s)",
                           (session.tenant_id, session.email, action, Jsonb(detail or {})))


def _like(text: str) -> str:
    """A search term for ILIKE, with its wildcards escaped."""
    return "%" + re.sub(r"([\\%_])", r"\\\1", text.strip()[:100]) + "%"


def _iso(value: datetime | None) -> str | None:
    """A timestamp for JSON."""
    return value.isoformat() if value else None


# --- Inbox --------------------------------------------------------------------------------------

View = Literal["all", "requests", "follow_up", "open", "resolved"]
_VIEWS = {
    "all": "true",
    "requests": "EXISTS (SELECT 1 FROM requests r WHERE r.conversation_id = c.id)",
    "follow_up": "c.status = 'open' AND EXISTS (SELECT 1 FROM requests r WHERE r.conversation_id = c.id "
                 "AND r.status = 'new')",
    "open": "c.status = 'open'",
    "resolved": "c.status = 'resolved'",
}


@router.get("/conversations")
async def conversations(view: View = "all", q: str = Query(default="", max_length=100),
                        page: int = Query(default=1, ge=1, le=1000),
                        session: Session = Depends(current_session)) -> dict:
    """The inbox list, newest first: a preview of the visitor's last message and any requests."""
    where = [_VIEWS[view]]
    params: list = []
    if q.strip():
        where.append("EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id = c.id AND m.content ILIKE %s)")
        params.append(_like(q))
    sql = (
        "SELECT c.id, c.created_at, c.last_message_at, c.user_turns, c.status, count(*) OVER () AS total, "
        "(SELECT m.content FROM messages m WHERE m.conversation_id = c.id AND m.role = 'user' "
        " ORDER BY m.id DESC LIMIT 1) AS preview, "
        "(SELECT coalesce(jsonb_agg(jsonb_build_object('kind', r.kind, 'status', r.status, "
        " 'name', r.payload->>'name', 'order', r.payload->>'order_number') ORDER BY r.created_at), '[]') "
        " FROM requests r WHERE r.conversation_id = c.id) AS requests, "
        "EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id = c.id AND m.guard_flags IS NOT NULL) AS flagged "
        f"FROM conversations c WHERE {' AND '.join(where)} "
        "ORDER BY c.last_message_at DESC LIMIT %s OFFSET %s")
    async with db.tenant_tx(session.tenant_id) as conn:
        rows = await (await conn.execute(sql, (*params, PAGE_SIZE, (page - 1) * PAGE_SIZE))).fetchall()
    return {"total": rows[0]["total"] if rows else 0, "page": page, "page_size": PAGE_SIZE,
            "items": [{"id": str(r["id"]), "created_at": _iso(r["created_at"]),
                       "last_message_at": _iso(r["last_message_at"]), "turns": r["user_turns"], "status": r["status"],
                       "preview": (r["preview"] or "")[:160], "requests": r["requests"], "flagged": r["flagged"]}
                      for r in rows]}


@router.get("/conversations/{conversation_id}")
async def conversation(conversation_id: UUID, session: Session = Depends(current_session)) -> dict:
    """One conversation: its messages and the requests sent from it (with consent details)."""
    async with db.tenant_tx(session.tenant_id) as conn:
        chat = await (await conn.execute("SELECT id, created_at, last_message_at, status FROM conversations "
                                         "WHERE id = %s", (conversation_id,))).fetchone()
        if chat is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        messages = await (await conn.execute(
            "SELECT role, content, created_at, guard_flags FROM messages WHERE conversation_id = %s ORDER BY id",
            (conversation_id,))).fetchall()
        requests = await (await conn.execute(
            "SELECT id, kind, status, payload, consent_text, consented_at, expires_at, emailed_at, webhook_sent_at, "
            "webhook_error FROM requests WHERE conversation_id = %s ORDER BY created_at", (conversation_id,))).fetchall()
    return {"id": str(chat["id"]), "status": chat["status"], "created_at": _iso(chat["created_at"]),
            "last_message_at": _iso(chat["last_message_at"]),
            "messages": [{"role": m["role"], "content": m["content"], "at": _iso(m["created_at"]),
                          "flags": m["guard_flags"] or []} for m in messages],
            "requests": [_request_json(r) for r in requests]}


class StatusIn(BaseModel):
    """A new status."""
    status: str = Field(max_length=20)


@router.put("/conversations/{conversation_id}/status")
async def conversation_status(conversation_id: UUID, body: StatusIn,
                              session: Session = Depends(current_session)) -> dict:
    """Mark a conversation resolved (or open again)."""
    if body.status not in ("open", "resolved"):
        raise HTTPException(status_code=422, detail="Status must be open or resolved")
    async with db.tenant_tx(session.tenant_id) as conn:
        cur = await conn.execute("UPDATE conversations SET status = %s, resolved_at = CASE WHEN %s = 'resolved' "
                                 "THEN now() END WHERE id = %s", (body.status, body.status, conversation_id))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"status": body.status}


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: UUID, session: Session = Depends(current_session)) -> None:
    """Delete a chat, its messages and requests (e.g. a visitor asked by email)."""
    if not await chat_forms.delete_conversation(session.tenant_id, conversation_id):
        raise HTTPException(status_code=404, detail="Conversation not found")
    await audit(session, "conversation.delete", {"conversation_id": str(conversation_id)})


# --- Leads and returns --------------------------------------------------------------------------

Group = Literal["leads", "returns"]
_KINDS = {"leads": ("lead", "handoff"), "returns": ("return",)}


def _request_json(r: dict) -> dict:
    """A request for the app."""
    return {"id": str(r["id"]), "kind": r["kind"], "status": r["status"], "data": r["payload"],
            "consent": r["consent_text"], "consented_at": _iso(r["consented_at"]), "expires_at": _iso(r["expires_at"]),
            "emailed_at": _iso(r.get("emailed_at")), "webhook_sent_at": _iso(r.get("webhook_sent_at")),
            "webhook_error": r.get("webhook_error"), "conversation_id": str(r["conversation_id"])
            if r.get("conversation_id") else None}


async def _requests(tenant_id: UUID, group: Group, status: str | None, q: str, limit: int, offset: int) -> list[dict]:
    """Requests of one group, newest first, filtered."""
    where, params = ["kind = ANY(%s)"], [list(_KINDS[group])]
    if status:
        where.append("status = %s")
        params.append(status)
    if q.strip():
        where.append("payload::text ILIKE %s")
        params.append(_like(q))
    async with db.tenant_tx(tenant_id) as conn:
        cur = await conn.execute(
            "SELECT *, count(*) OVER () AS total FROM requests WHERE " + " AND ".join(where) +
            " ORDER BY created_at DESC LIMIT %s OFFSET %s", (*params, limit, offset))
        return await cur.fetchall()


@router.get("/requests")
async def requests(kind: Group = "leads", status: Literal["new", "contacted", "closed"] | None = None,
                   q: str = Query(default="", max_length=100), page: int = Query(default=1, ge=1, le=1000),
                   session: Session = Depends(current_session)) -> dict:
    """Leads (contact + handoff) or return requests."""
    rows = await _requests(session.tenant_id, kind, status, q, PAGE_SIZE, (page - 1) * PAGE_SIZE)
    return {"total": rows[0]["total"] if rows else 0, "page": page, "page_size": PAGE_SIZE,
            "items": [_request_json(r) for r in rows]}


@router.put("/requests/{request_id}/status")
async def request_status(request_id: UUID, body: StatusIn, session: Session = Depends(current_session)) -> dict:
    """Move a lead along: new -> contacted -> closed."""
    if body.status not in ("new", "contacted", "closed"):
        raise HTTPException(status_code=422, detail="Status must be new, contacted or closed")
    async with db.tenant_tx(session.tenant_id) as conn:
        cur = await conn.execute("UPDATE requests SET status = %s WHERE id = %s", (body.status, request_id))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Request not found")
    return {"status": body.status}


def csv_cell(value: object) -> str:
    """A CSV cell that a spreadsheet won't run as a formula (=, +, -, @ ... get a leading ')."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_START) else text


def _items(items: list[dict]) -> str:
    """Return items in one cell."""
    return "; ".join(f"{i.get('title')} x{i.get('quantity', 1)}" for i in items or [])


@router.get("/requests/export.csv")
async def export(kind: Group = "leads", session: Session = Depends(current_session)) -> Response:
    """Download leads (or returns) as CSV. Logged in the audit trail."""
    await limits.count_export(session.tenant_id)
    rows = await _requests(session.tenant_id, kind, None, "", EXPORT_MAX_ROWS, 0)
    out = io.StringIO()
    writer = csv.writer(out)
    if kind == "leads":
        writer.writerow(["received", "type", "status", "name", "email", "summary", "consent", "consent_given_at"])
        for r in rows:
            p = r["payload"]
            writer.writerow([csv_cell(v) for v in (_iso(r["created_at"]), r["kind"], r["status"], p.get("name"),
                                                   p.get("email"), p.get("summary"), r["consent_text"],
                                                   _iso(r["consented_at"]))])
    else:
        writer.writerow(["received", "status", "order", "customer_email", "items", "reason", "refund_preference",
                         "notes", "consent_given_at"])
        for r in rows:
            p = r["payload"]
            writer.writerow([csv_cell(v) for v in (_iso(r["created_at"]), r["status"], p.get("order_number"),
                                                   p.get("customer_email"), _items(p.get("items")), p.get("reason"),
                                                   p.get("refund_preference"), p.get("notes"),
                                                   _iso(r["consented_at"]))])
    await audit(session, "requests.export", {"kind": kind, "rows": len(rows)})
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    return Response(out.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{kind}-{stamp}.csv"',
                             "Cache-Control": "no-store"})


# --- Knowledge ----------------------------------------------------------------------------------

@router.get("/kb")
async def knowledge(session: Session = Depends(current_session)) -> dict:
    """Crawled pages, the team's entries, and the last publish."""
    entries = await kb_admin.list_entries(session.tenant_id)
    return {"pages": await kb_admin.list_pages(session.tenant_id), "entries": entries,
            "changes": sum(e["changed"] for e in entries), "publish": await kb_admin.publish_state(session.tenant_id)}


class EntryBody(BaseModel):
    """An entry as the team writes it."""
    section: str = Field(min_length=1, max_length=60)
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=8000)

    def entry(self) -> kb_admin.EntryIn:
        """Trimmed values."""
        return kb_admin.EntryIn(self.section.strip(), self.title.strip(), self.body.strip())


@router.post("/kb/entries", status_code=201)
async def create_entry(body: EntryBody, session: Session = Depends(current_session)) -> dict:
    """A new draft entry."""
    created = await kb_admin.create_entry(session.tenant_id, body.entry())
    if created is None:
        raise HTTPException(status_code=409, detail=f"You can have up to {kb_admin.MAX_ENTRIES} entries")
    return created


@router.put("/kb/entries/{entry_id}")
async def update_entry(entry_id: UUID, body: EntryBody, session: Session = Depends(current_session)) -> dict:
    """Edit an entry's draft."""
    updated = await kb_admin.update_entry(session.tenant_id, entry_id, body.entry())
    if updated is None:
        raise HTTPException(status_code=404, detail="Entry not found")
    return updated


@router.delete("/kb/entries/{entry_id}", status_code=204)
async def delete_entry(entry_id: UUID, session: Session = Depends(current_session)) -> None:
    """Delete a draft now, or a live entry at the next Publish."""
    if not await kb_admin.delete_entry(session.tenant_id, entry_id):
        raise HTTPException(status_code=404, detail="Entry not found")


class HiddenIn(BaseModel):
    """Hide or show a crawled page."""
    hidden: bool


@router.put("/kb/pages/{page_id}")
async def hide_page(page_id: int, body: HiddenIn, session: Session = Depends(current_session)) -> dict:
    """Hide a crawled page from answers (now), or show it again (at the next re-sync)."""
    if not await kb_admin.set_hidden(session.tenant_id, page_id, body.hidden):
        raise HTTPException(status_code=404, detail="Page not found")
    return {"hidden": body.hidden}


@router.post("/kb/publish", status_code=202)
async def publish(session: Session = Depends(current_session)) -> dict:
    """Queue publishing every changed entry (the worker embeds them)."""
    state = await kb_admin.publish_state(session.tenant_id)
    if state and state["state"] in ("queued", "running"):
        return {"publish": state}
    await kb_admin.set_publish_state(session.tenant_id, "queued")
    await queue.enqueue(kb_admin.PUBLISH_JOB, {"tenant_id": str(session.tenant_id)})
    await audit(session, "kb.publish")
    return {"publish": await kb_admin.publish_state(session.tenant_id)}


class AskIn(BaseModel):
    """A test question."""
    question: str = Field(min_length=2, max_length=500)


async def _tenant_config(tenant_id: UUID) -> tuple[TenantConfig, TenantLimits]:
    """The tenant's config and plan caps."""
    async with db.tenant_tx(tenant_id) as conn:
        row = await (await conn.execute("SELECT config, limits FROM tenants WHERE id = %s", (tenant_id,))).fetchone()
    return TenantConfig.model_validate(row["config"]), TenantLimits.model_validate(row["limits"])


@router.post("/kb/ask")
async def ask(body: AskIn, session: Session = Depends(current_session)) -> dict:
    """Test console: what the assistant would find in the PUBLISHED knowledge, and its answer
    (the answer is best effort: shown only to the team, from the same grounded prompt)."""
    config, plan = await _tenant_config(session.tenant_id)
    await limits.check_token_budget(session.tenant_id, plan)
    try:
        found = await retrieval.retrieve(session.tenant_id, body.question)
    except EmbeddingError as exc:
        raise HTTPException(status_code=503, detail="The knowledge search is unavailable right now") from exc
    hits = [{"title": h.title or h.url, "kind": h.kind, "url": None if h.kind == "entry" else h.url,
             "score": round(h.score, 3), "excerpt": h.content[:300]} for h in found.hits]
    usage = Usage()
    answer, error = None, None
    try:
        system = grounded_system_prompt(config.bot_name, config.company_name, new_canary(),
                                        knowledge_block(found.hits), NO_ANSWER_MARKER)
        text = await complete([{"role": "system", "content": system}, {"role": "user", "content": body.question}],
                              usage, CONSOLE_ANSWER_TOKENS)
        answer = text.replace(NO_ANSWER_MARKER, "").strip()
        known = NO_ANSWER_MARKER not in text
    except Exception as exc:  # the model is optional here: the hits are the main result
        log.warning("test console answer failed: %s", type(exc).__name__)
        error, known = "The model didn't answer (the matches above are still what it would use).", bool(hits)
    finally:
        await limits.record_tokens(session.tenant_id, usage.total + found.embed_tokens)
    return {"hits": hits, "answer": answer, "knows": known, "error": error}


# --- Team ---------------------------------------------------------------------------------------

@router.get("/team")
async def team(session: Session = Depends(current_session)) -> dict:
    """Everyone who can sign in to this account."""
    async with db.tenant_tx(session.tenant_id) as conn:
        rows = await (await conn.execute("SELECT id, email, role, created_at, last_login_at FROM users "
                                         "ORDER BY role DESC, created_at")).fetchall()
    _, plan = await _tenant_config(session.tenant_id)
    return {"you": {"email": session.email, "role": session.role}, "max_members": plan.team_members,
            "members": [{"id": str(r["id"]), "email": r["email"], "role": r["role"], "joined": _iso(r["created_at"]),
                         "last_sign_in": _iso(r["last_login_at"])} for r in rows]}


class InviteIn(BaseModel):
    """Who to invite."""
    email: str = Field(max_length=254)


@router.post("/team", status_code=201)
async def invite(body: InviteIn, session: Session = Depends(owner_only)) -> dict:
    """Owner: add a member and email them a sign-in link."""
    email = body.email.strip().lower()
    if not re.fullmatch(EMAIL_PATTERN, email):
        raise HTTPException(status_code=422, detail="Enter a valid email address")
    config, plan = await _tenant_config(session.tenant_id)
    async with db.tenant_tx(session.tenant_id) as conn:
        count = (await (await conn.execute("SELECT count(*) AS n FROM users")).fetchone())["n"]
    if count >= plan.team_members:
        raise HTTPException(status_code=409, detail=f"Your plan allows {plan.team_members} people")
    if await db.resolve_login(email):
        raise HTTPException(status_code=409, detail="That email already has an easychat account")
    await limits.count_invite(session.tenant_id)
    async with db.tenant_tx(session.tenant_id) as conn:
        row = await (await conn.execute("INSERT INTO users (tenant_id, email, role) VALUES (%s, %s, 'member') "
                                        "RETURNING id", (session.tenant_id, email))).fetchone()
    try:
        await send_link(email, f"You're invited to the {config.company_name} team on easychat",
                        lambda link: invite_email(config.company_name, session.email, link))
    except HTTPException:
        async with db.tenant_tx(session.tenant_id) as conn:
            await conn.execute("DELETE FROM users WHERE id = %s", (row["id"],))
        raise
    await audit(session, "team.invite", {"email": email})
    return {"id": str(row["id"]), "email": email, "role": "member"}


@router.delete("/team/{user_id}", status_code=204)
async def remove(user_id: UUID, session: Session = Depends(owner_only)) -> None:
    """Owner: remove a member (signed out at once: sessions re-check the user)."""
    async with db.tenant_tx(session.tenant_id) as conn:
        cur = await conn.execute("DELETE FROM users WHERE id = %s AND role = 'member' RETURNING email", (user_id,))
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="No such member (the owner can't be removed)")
    await audit(session, "team.remove", {"email": row["email"]})
