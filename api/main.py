"""easychat API — HTTP layer.

Routes:
  GET  /health              -> {"ok": true}             liveness (no dependencies)
  GET  /ready               -> {"ok": true} | 503       readiness (Postgres + Redis)
  GET  /config?key=<pk_..>  -> public widget config for that site key
  POST /chat                -> Server-Sent Events stream of the assistant's reply
  /api/auth/*, /api/onboarding/*  -> self-serve sign-up and onboarding (step 7: auth.py, onboarding.py)
  /api/onboarding/orders/*  -> connect the shop's order system (step 8: order_connections.py)
  /app/                     -> the onboarding web app (app/dist, built from app/), if built

/config and /chat resolve the tenant from the public site key and require the browser's
Origin to be in that key's allowed_origins (403 otherwise). The first /config call from a
tenant's own website (not the onboarding preview) marks the widget installed (step 7).

POST /chat body: {"site_key", "conversation_id"?, "message"? | "form"?}  (see schemas.ChatRequest)
Errors before streaming starts are plain JSON {"detail": ...} with 4xx/5xx status:
  403 origin, 404 key/conversation, 409 nothing to retry, 413 body too large,
  422 invalid body, 429 rate/budget limit (Retry-After header), 503 dependency down.

SSE events sent by /chat:
  meta   {"conversation_id": "<uuid>"}  first event; the widget sends this id with the next turn
  token  {"t": "<text chunk>"}          one per chunk, in order
  replace {"text": "..."}              the output guard blocked the reply: show this text instead of
                                        the tokens so far (then blocks, then done)
  block  {"type": "...", ...}           zero or more UI blocks after the text (validated, see blocks.py)
  forget {}                             the visitor deleted this conversation ("delete my data", step 9):
                                        the widget drops its copy and starts fresh next time
  done   {}                             reply finished successfully (and was saved)
  error  {"message": "..."}             something failed; the stream then ends (never hangs)

Grounding (step 5): if the tenant has a knowledge base, each turn retrieves relevant chunks
(retrieval.py) into the system prompt, and the reply's blocks are built by code (grounding.py):
product cards + sources when it answered, or the tenant's fallback when the model said it
doesn't know. Without a knowledge base the assistant answers as before.

Guardrails (step 6, guards.py): visitor messages are cleaned of hidden characters and checked
for injection patterns (flags add a reminder to the prompt and are stored, never block). Every
model reply streams through guards.OutputGuard; on a violation (prompt leak, order claim,
invented contact details or a foreign link) the stream stops and a `replace` event swaps in a
safe reply. The verdicts are stored in messages.guard_flags.

Order tracking (step 8, order_tracking.py): for tenants with an order system connected, order
questions get a lookup form, submitted forms (`form` instead of `message`) get replies written
by code (code emailed, then the timeline card), and a conversation that verified an order gives
the model that order, fenced, while the output guard allows only that order's numbers.

Chat forms (step 9, chat_forms.py): returns, contact / "talk to a person", transcript by email
and "delete my data" are answered by code too, before order tracking. Requests are stored only
with consent and delivered by the worker (team_requests.py); onboarding settings are in
request_settings.py.

In local/test, a message `/demo <name>` replies with sample blocks without calling the model
(see demo.py).
"""
import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import auth
import chat_forms
import conversations
import db
import limits
import onboarding
import order_connections
import order_tracking
import request_settings
import retrieval
from blocks import validate_blocks
from config import settings
from demo import DemoReply, demo_reply
from embeddings import EmbeddingError
from guards import PROMPT_LEAK, OutputGuard, new_canary, safe_reply
from grounding import (NO_ANSWER_MARKER, MarkerFilter, fallback_blocks, knowledge_block, product_blocks,
                       sources_block)
from llm import Usage, stream_reply, user_facing_error
from middleware import BodySizeLimitMiddleware, RequestContextMiddleware, configure_logging
from models import normalize_origin
from order_tracking import OrderTurn, order_prompt_block
from prompts import (ORDERS_LOOKUP, ORDERS_OFF, ORDERS_VERIFIED, PROMPT_FINGERPRINTS, assistant_system_prompt,
                     grounded_system_prompt, injection_reminder, verified_order_section)
from replies import FormSubmission, ScriptedReply
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
        await order_tracking.drain()          # let running code lookups finish
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
app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes,
                   overrides={onboarding.SITEMAP_UPLOAD_PATH: onboarding.SITEMAP_UPLOAD_MAX_BYTES})
app.add_middleware(RequestContextMiddleware)

app.include_router(auth.router)
app.include_router(onboarding.router)
app.include_router(order_connections.router)
app.include_router(request_settings.router)

# The onboarding app (Preact, built into app/dist by `npm run build` in app/). Same origin as the
# API, so its session cookie needs no cross-site rules. Not built = not served.
APP_DIST = Path(__file__).resolve().parent.parent / "app" / "dist"
if APP_DIST.is_dir():
    app.mount("/app", StaticFiles(directory=APP_DIST, html=True), name="app")


@app.get("/app", include_in_schema=False)
def app_without_slash() -> RedirectResponse:
    """/app -> /app/ (so the app's relative asset URLs resolve)."""
    return RedirectResponse("/app/")

OriginHeader = Annotated[str | None, Header(alias="origin")]


def sse_event(event: str, data: dict) -> str:
    """Format one Server-Sent Event: an event name line, a JSON data line, a blank line."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def orders_mode(orders: OrderTurn) -> str:
    """Which version of prompt rule 3 this turn gets (prompts.ORDERS_*)."""
    if orders.order is not None:
        return ORDERS_VERIFIED
    return ORDERS_LOOKUP if orders.enabled else ORDERS_OFF


def build_prompt(tenant: Tenant, turn: conversations.Turn, canary: str,
                 knowledge: Retrieval | None = None, orders: OrderTurn | None = None) -> list[dict]:
    """System prompt first, then the stored conversation (already trimmed to recent turns).

    With a knowledge base, the system prompt also carries the grounding rules and the retrieved
    chunks; without one (knowledge=None) the assistant behaves as before step 5. If the visitor's
    message matched an injection pattern, the prompt also gets a reminder to keep the rules.
    A verified order (step 8) is appended last, fenced as data.
    """
    orders = orders or OrderTurn()
    name, company, mode = tenant.config.bot_name, tenant.config.company_name, orders_mode(orders)
    flagged = bool(turn.flags)
    if knowledge is None:
        system = assistant_system_prompt(name, company, canary, mode) + (injection_reminder() if flagged else "")
    else:
        system = grounded_system_prompt(name, company, canary, knowledge_block(knowledge.hits), NO_ANSWER_MARKER,
                                        flagged, mode)
    if orders.order is not None:
        system += verified_order_section(order_prompt_block(orders.order))
    return [{"role": "system", "content": system}, *turn.history]


def output_guard(tenant: Tenant, turn: conversations.Turn, knowledge: Retrieval | None, canary: str,
                 orders: OrderTurn | None = None) -> OutputGuard:
    """The guard for one reply. Contact details may come from the retrieved knowledge, the tenant's
    config or the conversation itself; links may only point to the tenant's own sites; with a
    verified order, only that order's numbers may be mentioned."""
    config = tenant.config
    hits = knowledge.hits if knowledge else []
    allowed = [config.company_name, *(f"{h.title}\n{h.url}\n{h.content}" for h in hits),
               *(m["content"] for m in turn.history)]
    urls = [str(config.privacy_url), *(h.url for h in hits)]
    if config.fallback:
        allowed.append(config.fallback.text)
        urls += [str(config.fallback.contact_url)] if config.fallback.contact_url else []
    if config.knowledge:
        urls += [str(u) for u in (*config.knowledge.sitemaps, *config.knowledge.start_urls)]
    allowed += urls
    hosts = {urlsplit(u).hostname for u in urls if urlsplit(u).hostname}
    refs = orders.refs if orders and orders.order is not None else None
    return OutputGuard(canary, PROMPT_FINGERPRINTS, "\n".join(allowed), hosts, refs)


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


async def _visible_text(stream: AsyncIterator[str], marker: MarkerFilter,
                        guard: OutputGuard | None) -> AsyncIterator[str]:
    """The model's text as the visitor may see it: no-answer marker removed, then checked by the
    output guard. Stops, and closes the model stream, at the first guard violation."""
    try:
        async for text in stream:
            visible = marker.feed(text)
            if guard:
                visible = guard.feed(visible)
                if guard.violation:
                    return
            if visible:
                yield visible
        rest = marker.flush()
        if guard:
            rest = guard.feed(rest) + guard.flush()
        if rest:
            yield rest
    finally:
        await stream.aclose()


async def scripted_reply(tenant: Tenant, turn: conversations.Turn, form: FormSubmission | None,
                         ip: str) -> tuple[ScriptedReply | None, OrderTurn]:
    """A reply written by code, if this turn gets one: chat forms first (step 9), then order
    tracking (step 8). Also what order tracking adds to a model turn (the verified order)."""
    reply = await chat_forms.handle(tenant, turn, form, ip)
    if reply is not None:
        return reply, OrderTurn()
    orders = await order_tracking.handle(tenant, turn.conversation_id, turn.history[-1]["content"], form, ip)
    return orders.reply, orders


async def reply_events(tenant: Tenant, turn: conversations.Turn, form: FormSubmission | None = None,
                       ip: str = "unknown") -> AsyncIterator[str]:
    """Turn a reply into SSE events: meta, tokens, `replace` if the guard blocked the reply,
    validated blocks, then `done` (or `error`). Chat forms and order tracking may answer
    instead of the model."""
    yield sse_event("meta", {"conversation_id": str(turn.conversation_id)})
    usage = Usage()
    embed_tokens = 0
    reply: list[str] = []
    demo = demo_for(tenant, turn)
    marker = MarkerFilter()       # strips the model's "no answer" signal from the visible text
    guard: OutputGuard | None = None   # demo and scripted replies are our own text: nothing to guard
    orders = OrderTurn()
    scripted = None
    try:
        knowledge = None
        if not demo:
            scripted, orders = await scripted_reply(tenant, turn, form, ip)
        if demo or scripted:
            text_stream = _as_stream(demo.text if demo else scripted.text)
        else:
            knowledge = await find_knowledge(tenant, turn)
            embed_tokens = knowledge.embed_tokens if knowledge else 0
            canary = new_canary()
            guard = output_guard(tenant, turn, knowledge, canary, orders)
            text_stream = stream_reply(build_prompt(tenant, turn, canary, knowledge, orders), usage)
        async for visible in _visible_text(text_stream, marker, guard):
            reply.append(visible)
            yield sse_event("token", {"t": visible})

        violation = guard.violation if guard else None
        if violation:
            # Never shown in full, never stored: the visitor gets a safe reply instead.
            log.warning("output guard replaced a reply: %s", violation)
            reply = [safe_reply(violation, tenant.config.company_name, orders.enabled)]
            yield sse_event("replace", {"text": reply[0]})
        elif not "".join(reply).strip():
            if not marker.found:
                yield sse_event("error", {"message": "The model returned an empty reply."})
                return
            # The model sent only the marker: say "I don't know" in the tenant's words.
            fallback = tenant.config.fallback
            reply = [fallback.text if fallback else "Sorry, I don't know the answer to that."]
            yield sse_event("token", {"t": reply[0]})

        if violation:
            raw_blocks = [] if violation == PROMPT_LEAK else fallback_blocks(tenant.config.fallback)
        elif demo or scripted:
            raw_blocks = demo.blocks if demo else scripted.blocks
        else:
            raw_blocks = await knowledge_blocks(tenant, knowledge, marker.found)
        # Every block is validated here, whatever produced it; invalid ones never leave the server.
        blocks = validate_blocks(raw_blocks, tenant.asset_policy(settings.is_dev))
        if scripted and scripted.forget:
            # "Delete my data": the conversation is gone, so there is nothing to store it in.
            for block in blocks:
                yield sse_event("block", block)
            yield sse_event("forget", {})
            yield sse_event("done", {})
            return
        # Order cards and request cards are shown, not stored: the conversation keeps only a note.
        stored_text = scripted.stored_text if scripted and scripted.stored_text else "".join(reply).strip()
        stored_blocks = blocks if not scripted or scripted.store_blocks else None
        await conversations.save_reply(
            tenant, turn.conversation_id, stored_text, usage.model, usage.tokens_in,
            usage.tokens_out, stored_blocks, [violation] if violation else None,
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
    await onboarding.mark_installed(tenant, normalize_origin(origin or ""))
    return tenant.public_config()


@app.post("/chat")
async def chat(body: ChatRequest, request: Request, origin: OriginHeader = None) -> StreamingResponse:
    """Check the tenant, limits and budget, store the visitor's message, then stream the reply."""
    tenant = await resolve_tenant(body.site_key, origin)

    # Cheapest checks first; all of them happen before any model call.
    ip = limits.client_ip(request)
    await limits.check_ip(ip)
    await limits.check_token_budget(tenant.id, tenant.limits)
    if body.conversation_id is None:
        await limits.count_new_conversation(tenant.id, tenant.limits)

    # A submitted form is stored as a short summary: never the details or the code it carried.
    form = FormSubmission(body.form.name, body.form.values) if body.form else None
    message = chat_forms.form_summary(form) if form else body.message
    turn = await conversations.prepare_turn(tenant, body.conversation_id, message)
    return StreamingResponse(
        reply_events(tenant, turn, form, ip),
        media_type="text/event-stream",
        # Stop proxies and browsers from buffering or caching the stream.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
