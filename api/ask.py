"""Show what retrieval finds for a question, without calling the chat model. For tuning
RAG_MIN_SCORE and checking that the right pages come back.

    python ask.py culpine "What services do you offer?"

Lists every candidate in ranked order with its cosine score; the ones marked * (at or above
RAG_MIN_SCORE, first RAG_TOP_K of them) are what the model would see.
"""
import asyncio
import sys

from crawl import tenant_by_slug

import db
from config import settings
from retrieval import retrieve

PREVIEW_CHARS = 110


async def ask(slug: str, question: str) -> None:
    tenant_id, _ = tenant_by_slug(slug)
    await db.open_pool()
    try:
        everything = await retrieve(tenant_id, question, min_score=-1.0, limit=100)   # show all
    finally:
        await db.close_pool()
    print(f"{everything.candidates} candidates; floor RAG_MIN_SCORE={settings.rag_min_score}, "
          f"top {settings.rag_top_k} above it go to the model ({everything.embed_tokens} tokens)\n")
    sent = 0
    for hit in everything.hits:                       # in fused (RRF) order, as retrieval ranks them
        relevant = hit.score >= settings.rag_min_score and sent < settings.rag_top_k
        sent += relevant
        mark = "*" if relevant else " "
        preview = " ".join(hit.content.split())[:PREVIEW_CHARS]
        kind = " [product]" if hit.product_id else ""
        print(f"{mark} {hit.score:.3f}  {hit.url}{kind}\n          {preview}")


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit('usage: python ask.py <tenant-slug> "question"')
    asyncio.run(ask(sys.argv[1], sys.argv[2]), loop_factory=asyncio.SelectorEventLoop)


if __name__ == "__main__":
    main()
