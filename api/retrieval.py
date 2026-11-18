"""Find the knowledge-base chunks that answer a question (roadmap step 5).

Hybrid search inside the tenant's own rows (RLS, via db.tenant_tx):
  * vector: cosine similarity between the question's bge-m3 embedding and each chunk's;
  * full-text: Postgres text search, which catches exact names, SKUs and prices that embeddings
    can blur;
  * the two rankings are merged with Reciprocal Rank Fusion (RRF).
Only chunks whose cosine similarity reaches RAG_MIN_SCORE count: below that the honest answer
is "I don't know", whatever the ranking says.
"""
import re
from dataclasses import dataclass
from uuid import UUID

import db
from config import settings
from embeddings import embed_texts, to_pgvector

CANDIDATES = 20                 # taken from each ranking before fusion
RRF_K = 60                      # standard RRF damping constant
MAX_QUERY_WORDS = 20
SHORT_FOLLOW_UP_CHARS = 60      # "how much is it?" -> also search with the previous question
MAX_QUERY_CHARS = 1000

_WORD = re.compile(r"\w{2,}", re.UNICODE)

# One statement: both candidate lists, fused, with the true cosine score for the floor.
_SEARCH_SQL = """
WITH vec AS (
  SELECT id, row_number() OVER (ORDER BY embedding <=> %(q)s::vector) AS rank
  FROM (SELECT id, embedding FROM kb_chunks ORDER BY embedding <=> %(q)s::vector LIMIT %(n)s) v
),
fts AS (
  SELECT id, row_number() OVER (ORDER BY text_rank DESC) AS rank
  FROM (SELECT id, ts_rank_cd(tsv, query) AS text_rank
        FROM kb_chunks, to_tsquery('english', %(words)s) query
        WHERE tsv @@ query
        ORDER BY text_rank DESC LIMIT %(n)s) f
),
fused AS (
  SELECT id, sum(1.0 / (%(k)s + rank)) AS rrf
  FROM (SELECT id, rank FROM vec UNION ALL SELECT id, rank FROM fts) ranked
  GROUP BY id
)
SELECT c.id, c.page_id, c.product_id, c.heading, c.content, p.url, p.title, p.kind,
       1 - (c.embedding <=> %(q)s::vector) AS score, fused.rrf
FROM fused
JOIN kb_chunks c ON c.id = fused.id
JOIN kb_pages p ON p.id = c.page_id
ORDER BY fused.rrf DESC
"""


@dataclass(frozen=True)
class Hit:
    """One relevant chunk, with the page it came from."""
    chunk_id: int
    page_id: int
    product_id: int | None
    heading: str
    content: str
    url: str
    title: str
    score: float          # cosine similarity, 0..1
    kind: str = "page"    # "page" | "article" (kb_pages.kind)


@dataclass(frozen=True)
class Retrieval:
    """The result of one search: relevant hits plus numbers for logs and tuning."""
    hits: list[Hit]       # relevant chunks (score >= floor), best first, at most RAG_TOP_K
    candidates: int       # chunks considered before the floor (for logs and ask.py)
    embed_tokens: int


def search_query(history: list[dict]) -> str:
    """The text to search with: the latest question, plus the one before if it's a short follow-up."""
    user_turns = [m["content"] for m in history if m["role"] == "user"]
    if not user_turns:
        return ""
    query = user_turns[-1]
    if len(query) < SHORT_FOLLOW_UP_CHARS and len(user_turns) > 1:
        query = f"{user_turns[-2]}\n{query}"
    return query[:MAX_QUERY_CHARS]


def _tsquery_words(text: str) -> str:
    """Words OR-ed for to_tsquery. Only \\w characters reach it, so no tsquery syntax can be injected."""
    words = list(dict.fromkeys(w.lower() for w in _WORD.findall(text)))[:MAX_QUERY_WORDS]
    return " | ".join(words)


async def has_knowledge(tenant_id: UUID) -> bool:
    """Whether the tenant has indexed anything (no knowledge base = answer as before)."""
    async with db.tenant_tx(tenant_id) as conn:
        cur = await conn.execute("SELECT EXISTS (SELECT 1 FROM kb_chunks) AS present")
        return (await cur.fetchone())["present"]


async def retrieve(tenant_id: UUID, query: str, min_score: float | None = None,
                   limit: int | None = None) -> Retrieval:
    """Embed the query and return the tenant's relevant chunks. Raises EmbeddingError.

    min_score / limit override RAG_MIN_SCORE / RAG_TOP_K (ask.py uses them to show everything).
    """
    floor = settings.rag_min_score if min_score is None else min_score
    limit = settings.rag_top_k if limit is None else limit
    vectors, tokens = await embed_texts([query])
    params = {"q": to_pgvector(vectors[0]), "words": _tsquery_words(query), "n": CANDIDATES, "k": RRF_K}
    async with db.tenant_tx(tenant_id) as conn:
        # With many tenants in one HNSW index, keep scanning until enough of THIS tenant's rows
        # are found (pgvector >= 0.8), instead of filtering a global top-N down to nothing.
        await conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
        cur = await conn.execute(_SEARCH_SQL, params)
        rows = await cur.fetchall()
    hits = [
        Hit(r["id"], r["page_id"], r["product_id"], r["heading"], r["content"], r["url"], r["title"],
            float(r["score"]), r["kind"])
        for r in rows if float(r["score"]) >= floor
    ]
    return Retrieval(hits=hits[:limit], candidates=len(rows), embed_tokens=tokens)


async def products_for(tenant_id: UUID, product_ids: list[int]) -> list[dict]:
    """Product rows in the given order (ids come from retrieval hits)."""
    if not product_ids:
        return []
    async with db.tenant_tx(tenant_id) as conn:
        cur = await conn.execute(
            "SELECT id, name, url, description, image_url, brand, price, currency, availability, rating, "
            "review_count FROM products WHERE id = ANY(%s)", (product_ids,),
        )
        rows = {row["id"]: row for row in await cur.fetchall()}
    return [rows[i] for i in product_ids if i in rows]
