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
  done   {}                             reply finished successfully (and was saved)
  error  {"message": "..."}             something failed; the stream then ends (never hangs)
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
from config import settings
from llm import Usage, stream_reply, user_facing_error
from middleware import BodySizeLimitMiddleware, RequestContextMiddleware, configure_logging
from prompts import assistant_system_prompt
from schemas import ChatRequest
from tenancy import SITE_KEY_PATTERN, Tenant, resolve_tenant

configure_logging()
log = logging.getLogger("easychat.api")

# Fire-and-forget bookkeeping tasks, kept referenced until they finish.
_background: set[asyncio.Task] = set()


@asynccontextmanager
async def lifespan(_: FastAPI):
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


def build_prompt(tenant: Tenant, history: list[dict]) -> list[dict]:
    """System prompt first, then the stored conversation (already trimmed to recent turns)."""
    system = assistant_system_prompt(tenant.config.bot_name, tenant.config.company_name)
    return [{"role": "system", "content": system}, *history]


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
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


async def reply_events(tenant: Tenant, turn: conversations.Turn) -> AsyncIterator[str]:
    """Turn the model's text stream into SSE events. Always ends with `done` or `error`."""
    yield sse_event("meta", {"conversation_id": str(turn.conversation_id)})
    usage = Usage()
    reply: list[str] = []
    try:
        async for text in stream_reply(build_prompt(tenant, turn.history), usage):
            reply.append(text)
            yield sse_event("token", {"t": text})

        if not reply:
            yield sse_event("error", {"message": "The model returned an empty reply."})
            return
        await conversations.save_reply(
            tenant, turn.conversation_id, "".join(reply), usage.model, usage.tokens_in, usage.tokens_out
        )
        yield sse_event("done", {})
    except Exception as exc:  # any failure becomes an event so the widget never hangs
        yield sse_event("error", {"message": user_facing_error(exc)})
    finally:
        # Runs even if the visitor disconnects mid-stream (when awaiting here could be cancelled).
        _in_background(limits.record_tokens(tenant.id, usage.total))


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/ready")
async def ready() -> JSONResponse:
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
    tenant = await resolve_tenant(key, origin)
    return tenant.public_config()


@app.post("/chat")
async def chat(body: ChatRequest, request: Request, origin: OriginHeader = None) -> StreamingResponse:
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
