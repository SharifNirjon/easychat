"""Server-side conversation history. The client sends only its new message; the history the
model sees always comes from here, so a client can't forge earlier "assistant" turns."""
from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException

from db import tenant_tx
from limits import LimitExceeded
from tenancy import Tenant

# Only the most recent messages are sent to the model: keeps cost and latency bounded.
HISTORY_MESSAGES = 10


@dataclass(frozen=True)
class Turn:
    conversation_id: UUID
    history: list[dict]     # [{"role": ..., "content": ...}] oldest first, ends with a user message


async def prepare_turn(tenant: Tenant, conversation_id: UUID | None, message: str | None) -> Turn:
    """Store the visitor's message (if any) and return the history to answer.

    message=None means "retry": answer the existing history, whose last message must be the
    visitor's (e.g. the previous reply failed).
    """
    async with tenant_tx(tenant.id) as conn:
        if conversation_id is None:
            if message is None:
                raise HTTPException(status_code=422, detail="A new conversation needs a message")
            cur = await conn.execute(
                "INSERT INTO conversations (tenant_id, site_key_id) VALUES (%s, %s) RETURNING id, user_turns",
                (tenant.id, tenant.site_key_id),
            )
        else:
            # FOR UPDATE serialises concurrent sends on one conversation. RLS limits this to the tenant.
            cur = await conn.execute(
                "SELECT id, user_turns FROM conversations WHERE id = %s FOR UPDATE", (conversation_id,)
            )
        conversation = await cur.fetchone()
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        cid = conversation["id"]

        if message is not None:
            if conversation["user_turns"] >= tenant.limits.conversation_user_turns:
                raise LimitExceeded("This chat has reached its length limit. Please start a new chat.", retry_after=0)
            await conn.execute(
                "INSERT INTO messages (tenant_id, conversation_id, role, content) VALUES (%s, %s, 'user', %s)",
                (tenant.id, cid, message),
            )
            await conn.execute(
                "UPDATE conversations SET user_turns = user_turns + 1, last_message_at = now() WHERE id = %s",
                (cid,),
            )

        cur = await conn.execute(
            "SELECT role, content FROM ("
            "  SELECT id, role, content FROM messages WHERE conversation_id = %s ORDER BY id DESC LIMIT %s"
            ") recent ORDER BY id",
            (cid, HISTORY_MESSAGES),
        )
        history = [{"role": r["role"], "content": r["content"]} for r in await cur.fetchall()]

    if not history or history[-1]["role"] != "user":
        raise HTTPException(status_code=409, detail="Nothing to reply to")
    return Turn(conversation_id=cid, history=history)


async def save_reply(
    tenant: Tenant, conversation_id: UUID, content: str, model: str | None, tokens_in: int, tokens_out: int
) -> None:
    async with tenant_tx(tenant.id) as conn:
        await conn.execute(
            "INSERT INTO messages (tenant_id, conversation_id, role, content, model, tokens_in, tokens_out) "
            "VALUES (%s, %s, 'assistant', %s, %s, %s, %s)",
            (tenant.id, conversation_id, content, model, tokens_in, tokens_out),
        )
        await conn.execute("UPDATE conversations SET last_message_at = now() WHERE id = %s", (conversation_id,))
