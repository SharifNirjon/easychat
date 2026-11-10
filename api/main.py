"""easychat API — HTTP layer.

Routes:
  GET  /health              -> {"ok": true, "model": "<chat model>"}
  GET  /config?site=<id>    -> public widget config for that tenant
  POST /chat                -> Server-Sent Events stream of the assistant's reply

/config and /chat resolve the tenant from the site id and require the browser's Origin
to be in that tenant's allowed_origins (403 otherwise).

SSE events sent by /chat:
  token  {"t": "<text chunk>"}   one per chunk, in order
  done   {}                      reply finished successfully
  error  {"message": "..."}      something failed; the stream then ends (never hangs)
"""
import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import FastAPI, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from config import settings
from llm import stream_reply, user_facing_error
from prompts import assistant_system_prompt
from schemas import ChatRequest
from tenancy import SITE_ID_PATTERN, Tenant, all_allowed_origins, load_tenants, resolve_tenant

# Only the most recent turns are sent to the model: keeps cost and latency bounded.
MAX_HISTORY_TURNS = 10

TENANTS = load_tenants()

app = FastAPI(title="easychat api")

# CORS lets browsers on any tenant's origins call the API at all;
# resolve_tenant() then checks the origin belongs to the specific site.
app.add_middleware(
    CORSMiddleware,
    allow_origins=all_allowed_origins(TENANTS),
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

OriginHeader = Annotated[str | None, Header(alias="origin")]


def sse_event(event: str, data: dict) -> str:
    """Format one Server-Sent Event: an event name line, a JSON data line, a blank line."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def build_prompt(request: ChatRequest, tenant: Tenant) -> list[dict]:
    """System prompt first, then the last N turns of the conversation."""
    system = assistant_system_prompt(tenant.bot_name, tenant.company_name)
    history = [m.model_dump() for m in request.messages[-MAX_HISTORY_TURNS:]]
    return [{"role": "system", "content": system}, *history]


async def reply_events(messages: list[dict]) -> AsyncIterator[str]:
    """Turn the model's text stream into SSE events. Always ends with `done` or `error`."""
    try:
        got_text = False
        async for text in stream_reply(messages):
            got_text = True
            yield sse_event("token", {"t": text})

        if got_text:
            yield sse_event("done", {})
        else:
            yield sse_event("error", {"message": "The model returned an empty reply."})
    except Exception as exc:  # any failure becomes an event so the widget never hangs
        yield sse_event("error", {"message": user_facing_error(exc)})


@app.get("/health")
def health() -> dict:
    return {"ok": True, "model": settings.chat_model}


@app.get("/config")
def widget_config(
    origin: OriginHeader = None,
    site: str = Query(pattern=SITE_ID_PATTERN),
) -> dict:
    tenant = resolve_tenant(TENANTS, site, origin)
    return tenant.public_config()


@app.post("/chat")
async def chat(request: ChatRequest, origin: OriginHeader = None) -> StreamingResponse:
    tenant = resolve_tenant(TENANTS, request.site_id, origin)
    return StreamingResponse(
        reply_events(build_prompt(request, tenant)),
        media_type="text/event-stream",
        # Stop proxies and browsers from buffering or caching the stream.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
