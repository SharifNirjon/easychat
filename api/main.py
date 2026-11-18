"""easychat API — HTTP layer.

Routes:
  GET  /health              -> {"ok": true}             liveness (no dependencies)
  GET  /ready               -> {"ok": true} | 503       readiness (Postgres + Redis)
  GET  /config?key=<pk_..>  -> public widget config for that site key
  POST /chat                -> Server-Sent Events stream of the assistant's reply

/config and /chat resolve the tenant from the public site key and require the browser's
Origin to be in that key's allowed_origins (403 otherwise).

POST /chat body: {"site_key", "conversation_id"?, "message"?}  (see schemas.ChatRequest)
Errors before streaming starts are plain JSON {"detail": ...} with 4xx/5xx status:
  403 origin, 404 key/conversation, 409 nothing to retry, 413 body too large,
  422 invalid body, 429 rate/budget limit (Retry-After header), 503 dependency down.

SSE events sent by /chat:
  meta   {"conversation_id": "<uuid>"}  first event; the widget sends this id with the next turn
  token  {"t": "<text chunk>"}          one per chunk, in order
  block  {"type": "...", ...}           zero or more UI blocks after the text (validated, see blocks.py)
  done   {}                             reply finished successfully (and was saved)
  error  {"message": "..."}             something failed; the stream then ends (never hangs)

Grounding (step 5): if the tenant has a knowledge base, each turn retrieves relevant chunks
(retrieval.py) into the system prompt, and the reply's blocks are built by code (grounding.py):
product cards + sources when it answered, or the tenant's fallback when the model said it
doesn't know. Without a knowledge base the assistant answers as before.

In local/test, a message `/demo <name>` replies with sample blocks without calling the model
(see demo.py).
"""
import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

import conversations
import db
import limits
import retrieval
from blocks import validate_blocks
from config import settings
from demo import DemoReply, demo_reply
from embeddings import EmbeddingError
from grounding import (NO_ANSWER_MARKER, MarkerFilter, fallback_blocks, knowledge_block, product_blocks,
                       sources_block)
from llm import Usage, stream_reply, user_facing_error
from middleware import BodySizeLimitMiddleware, RequestContextMiddleware, configure_logging
from prompts import assistant_system_prompt, grounded_system_prompt
from retrieval import Retrieval
from schemas import ChatRequest
from tenancy import SITE_KEY_PATTERN, Tenant, resolve_tenant

configure_logging()
log = logging.getLogger("easychat.api")

# Fire-and-forget bookkeeping tasks, kept referenced until they finish.
_background: set[asyncio.Task] = set()


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Open the database pool at startup; close it and the Redis client at shutdown."""
    await db.open_pool()
    try:
        yield
    finally:
        await db.close_pool()
        await limits.client.aclose()


app = FastAPI(
    title="easychat api",
    lifespan=lifespan,
    # Interactive docs only in local/test: no need to publish the API surface in production.
    docs_url="/docs" if settings.is_dev else None,
    redoc_url=None,
    openapi_url="/openapi.json" if settings.is_dev else None,
)

# Middleware runs outermost-last-added. The widget sends no cookies or credentials, so allowing
# any origin at the CORS layer is safe; resolve_tenant() then enforces each site key's origins.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
    expose_headers=["Retry-After", "X-Request-ID"],
)
app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)
app.add_middleware(RequestContextMiddleware)

OriginHeader = Annotated[str | None, Header(alias="origin")]


def sse_event(event: str, data: dict) -> str:
    """Format one Server-Sent Event: an event name line, a JSON data line, a blank line."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def build_prompt(tenant: Tenant, history: list[dict], knowledge: Retrieval | None = None) -> list[dict]:
    """System prompt first, then the stored conversation (already trimmed to recent turns).

    With a knowledge base, the system prompt also carries the grounding rules and the retrieved
    chunks; without one (knowledge=None) the assistant behaves as before step 5.
    """
    name, company = tenant.config.bot_name, tenant.config.company_name
    if knowledge is None:
        system = assistant_system_prompt(name, company)
    else:
        system = grounded_system_prompt(name, company, knowledge_block(knowledge.hits), NO_ANSWER_MARKER)
    return [{"role": "system", "content": system}, *history]


async def find_knowledge(tenant: Tenant, turn: conversations.Turn) -> Retrieval | None:
    """Retrieved chunks for this turn, or None if the tenant has no knowledge base yet."""
    if not await retrieval.has_knowledge(tenant.id):
        return None
    found = await retrieval.retrieve(tenant.id, retrieval.search_query(turn.history))
    log.info("retrieval: %d relevant of %d candidates", len(found.hits), found.candidates)
    return found


async def knowledge_blocks(tenant: Tenant, knowledge: Retrieval | None, no_answer: bool) -> list[dict]:
    """Blocks built by code from database rows: fallback, or product cards + sources."""
    if knowledge is None:
        return []
    if no_answer:
        return fallback_blocks(tenant.config.fallback)
    if not knowledge.hits:
        return []          # small talk: nothing was needed and nothing was used
    product_ids = list(dict.fromkeys(h.product_id for h in knowledge.hits if h.product_id))
    rows = await retrieval.products_for(tenant.id, product_ids)
    return product_blocks(rows, tenant.asset_policy(settings.is_dev)) + sources_block(knowledge.hits)


def client_ip(request: Request) -> str:
    """The visitor's IP for rate limiting.

    Each trusted proxy appends the address it received the request from to X-Forwarded-For,
    so the real client is the Nth entry from the right; anything further left is client-supplied
    and may be forged. With no trusted proxies, use the socket peer.
    """
    hops = settings.trusted_proxy_hops
    if hops > 0:
        forwarded = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
        if len(forwarded) >= hops:
            return forwarded[-hops]
    return request.client.host if request.client else "unknown"


def _in_background(coro) -> None:
    """Run bookkeeping after the response, keeping a reference so it isn't garbage-collected."""
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


def demo_for(tenant: Tenant, turn: conversations.Turn) -> DemoReply | None:
    """Local/test only: `/demo <name>` answers with sample blocks instead of calling the model."""
    if not settings.is_dev:
        return None
    image_host = tenant.config.image_hosts[0] if tenant.config.image_hosts else None
    return demo_reply(turn.history[-1]["content"], image_host)


async def _as_stream(text: str) -> AsyncIterator[str]:
    """Wrap a fixed text (demo replies) as a one-chunk stream, like the model's."""
    yield text


async def reply_events(tenant: Tenant, turn: conversations.Turn) -> AsyncIterator[str]:
    """Turn a reply into SSE events: meta, tokens, validated blocks, then `done` (or `error`)."""
    yield sse_event("meta", {"conversation_id": str(turn.conversation_id)})
    usage = Usage()
    embed_tokens = 0
    reply: list[str] = []
    demo = demo_for(tenant, turn)
    marker = MarkerFilter()       # strips the model's "no answer" signal from the visible text
    try:
        knowledge = None
        if demo:
            text_stream = _as_stream(demo.text)
        else:
            knowledge = await find_knowledge(tenant, turn)
            embed_tokens = knowledge.embed_tokens if knowledge else 0
            text_stream = stream_reply(build_prompt(tenant, turn.history, knowledge), usage)
        async for text in text_stream:
            visible = marker.feed(text)
            if visible:
                reply.append(visible)
                yield sse_event("token", {"t": visible})
        rest = marker.flush()
        if rest:
            reply.append(rest)
            yield sse_event("token", {"t": rest})

        if not "".join(reply).strip():
            if not marker.found:
                yield sse_event("error", {"message": "The model returned an empty reply."})
                return
            # The model sent only the marker: say "I don't know" in the tenant's words.
            fallback = tenant.config.fallback
            reply = [fallback.text if fallback else "Sorry, I don't know the answer to that."]
            yield sse_event("token", {"t": reply[0]})

        raw_blocks = demo.blocks if demo else await knowledge_blocks(tenant, knowledge, marker.found)
        # Every block is validated here, whatever produced it; invalid ones never leave the server.
        blocks = validate_blocks(raw_blocks, tenant.asset_policy(settings.is_dev))
        await conversations.save_reply(
            tenant, turn.conversation_id, "".join(reply).strip(), usage.model, usage.tokens_in,
            usage.tokens_out, blocks,
        )
        for block in blocks:
            yield sse_event("block", block)
        yield sse_event("done", {})
    except EmbeddingError:
        yield sse_event("error", {"message": "The assistant is unavailable right now. Please try again."})
    except Exception as exc:  # any failure becomes an event so the widget never hangs
        yield sse_event("error", {"message": user_facing_error(exc)})
    finally:
        # Runs even if the visitor disconnects mid-stream (when awaiting here could be cancelled).
        _in_background(limits.record_tokens(tenant.id, usage.total + embed_tokens))


@app.get("/health")
def health() -> dict:
    """Liveness: the process is up. Checks nothing else and reveals nothing."""
    return {"ok": True}


@app.get("/ready")
async def ready() -> JSONResponse:
    """Readiness: Postgres and Redis both answer. 503 otherwise (details only in logs)."""
    try:
        await db.ping()
        await limits.ping()
    except Exception as exc:
        log.error("readiness check failed: %s", type(exc).__name__)
        return JSONResponse({"ok": False}, status_code=503)
    return JSONResponse({"ok": True})


@app.get("/config")
async def widget_config(
    origin: OriginHeader = None,
    key: str = Query(pattern=SITE_KEY_PATTERN),
) -> dict:
    """The tenant's public widget config, for an allowed Origin only."""
    tenant = await resolve_tenant(key, origin)
    return tenant.public_config()


@app.post("/chat")
async def chat(body: ChatRequest, request: Request, origin: OriginHeader = None) -> StreamingResponse:
    """Check the tenant, limits and budget, store the visitor's message, then stream the reply."""
    tenant = await resolve_tenant(body.site_key, origin)

    # Cheapest checks first; all of them happen before any model call.
    await limits.check_ip(client_ip(request))
    await limits.check_token_budget(tenant.id, tenant.limits)
    if body.conversation_id is None:
        await limits.count_new_conversation(tenant.id, tenant.limits)

    turn = await conversations.prepare_turn(tenant, body.conversation_id, body.message)
    return StreamingResponse(
        reply_events(tenant, turn),
        media_type="text/event-stream",
        # Stop proxies and browsers from buffering or caching the stream.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
