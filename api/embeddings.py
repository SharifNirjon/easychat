"""Text embeddings via OpenRouter (baai/bge-m3, 1024 dimensions).

Same privacy rule as chat (llm.py): in PRIVACY_MODE=strict every call carries the zero-data-
retention provider preferences. kb_chunks.embedding is vector(1024), so a model returning any
other size is refused rather than stored.
"""
import logging

from openai import AsyncOpenAI

from config import settings
from llm import STRICT_PROVIDER_PREFS

log = logging.getLogger("easychat.embeddings")

EMBED_DIMENSIONS = 1024
BATCH_SIZE = 32                 # inputs per API call
MAX_INPUT_CHARS = 8000          # far below bge-m3's 8k-token window

_client = AsyncOpenAI(
    base_url=settings.llm_base_url,
    api_key=settings.api_key,
    timeout=settings.timeout_seconds,
    max_retries=1,               # embeddings are idempotent: one retry is cheap and safe
)


class EmbeddingError(RuntimeError):
    """The embedding service failed or returned something unusable."""


def _extra_body() -> dict:
    """OpenRouter provider preferences: zero data retention in strict privacy mode."""
    return {"provider": STRICT_PROVIDER_PREFS} if settings.privacy_mode == "strict" else {}


async def embed_texts(texts: list[str]) -> tuple[list[list[float]], int]:
    """Embed texts in order. Returns (vectors, tokens used). Raises EmbeddingError."""
    vectors: list[list[float]] = []
    tokens = 0
    for start in range(0, len(texts), BATCH_SIZE):
        batch = [text[:MAX_INPUT_CHARS] for text in texts[start:start + BATCH_SIZE]]
        try:
            response = await _client.embeddings.create(
                model=settings.embed_model, input=batch, encoding_format="float", extra_body=_extra_body()
            )
        except Exception as exc:
            log.warning("embedding call failed: %s", type(exc).__name__)
            raise EmbeddingError("embedding service unavailable") from exc
        data = sorted(response.data, key=lambda item: item.index)
        if len(data) != len(batch) or any(len(item.embedding) != EMBED_DIMENSIONS for item in data):
            raise EmbeddingError(f"expected {len(batch)} vectors of {EMBED_DIMENSIONS} dimensions")
        vectors.extend(item.embedding for item in data)
        tokens += response.usage.prompt_tokens if response.usage else sum(len(t) // 4 for t in batch)
    return vectors, tokens


def to_pgvector(vector: list[float]) -> str:
    """pgvector's text format ('[0.1,0.2,...]'), so no extra driver package is needed."""
    return "[" + ",".join(f"{x:.7g}" for x in vector) + "]"
