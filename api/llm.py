"""Thin wrapper around the OpenRouter chat API.

`stream_reply` yields plain text chunks and raises on failure; `user_facing_error`
turns any exception into a safe message for the visitor (no internals, no secrets).
"""
import logging
from collections.abc import AsyncIterator

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from config import settings

log = logging.getLogger("easychat.llm")

# v4 production privacy settings: zero data retention, no training on our data,
# fastest provider first. Only sent when PRIVACY_MODE=strict (free models reject them).
STRICT_PROVIDER_PREFS = {"data_collection": "deny", "zdr": True, "sort": "latency"}

# max_retries=0: a retry would double the wait; fallback models cover failures instead.
# timeout also applies between streamed chunks, so a stalled stream can't hang forever.
_client = AsyncOpenAI(
    base_url=settings.llm_base_url,
    api_key=settings.api_key,
    timeout=settings.timeout_seconds,
    max_retries=0,
)


def _openrouter_options() -> dict:
    """OpenRouter-specific fields that the OpenAI SDK passes through via extra_body."""
    options: dict = {}

    # Reasoning: "off" gives the fastest first token; "low" thinks a little.
    # Reasoning text is excluded so it is never streamed to the visitor.
    if settings.reasoning == "off":
        options["reasoning"] = {"enabled": False}
    else:
        options["reasoning"] = {"effort": "low", "exclude": True}

    # OpenRouter tries these models in order if the first one is down or refuses.
    if settings.fallback_models:
        options["models"] = [settings.chat_model, *settings.fallback_models]

    if settings.privacy_mode == "strict":
        options["provider"] = STRICT_PROVIDER_PREFS

    return options


async def stream_reply(messages: list[dict]) -> AsyncIterator[str]:
    """Stream the model's reply as text chunks. Raises on any API error."""
    response = await _client.chat.completions.create(
        model=settings.chat_model,
        messages=messages,
        stream=True,
        max_tokens=settings.max_tokens,
        extra_body=_openrouter_options(),
    )
    async for chunk in response:
        # Some chunks carry only metadata (usage, finish reason) and no text.
        if chunk.choices and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content


def user_facing_error(exc: Exception) -> str:
    """Log the real error server-side and return a safe message for the visitor."""
    if isinstance(exc, APITimeoutError):
        log.warning("LLM timeout after %ss", settings.timeout_seconds)
        return "The assistant took too long to answer. Please try again."
    if isinstance(exc, APIStatusError):
        log.warning("LLM status %s: %s", exc.status_code, exc.message)
        return "The assistant is unavailable right now. Please try again."
    if isinstance(exc, APIConnectionError):
        log.warning("LLM connection error")
        return "Could not reach the assistant. Please try again."
    log.exception("Unexpected error while streaming a reply", exc_info=exc)
    return "Something went wrong. Please try again."
