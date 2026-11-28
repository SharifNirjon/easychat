"""Editing the knowledge base from the admin (roadmap step 10, design A-05).

Two kinds of knowledge:
  * crawled pages (crawler.py): the team can hide one. Hiding drops its chunks at once and the
    crawler keeps skipping it; un-hiding clears the stored hash, so the next re-sync indexes it.
  * the team's own entries (kb_entries): edited as drafts, live only after Publish. Publishing
    runs in the worker (job "publish_kb"): every changed entry is chunked and embedded into a
    kb_pages row of kind 'entry' (url "entry:<id>"), so retrieval finds entries exactly like pages;
    deleted entries are removed. Entries have no web page, so answers from them show no source link.

Team-written text still reaches the model only inside the fenced <knowledge> section (as
"Written by the team"): data the assistant answers from, never instructions it follows.
"""
import json
import logging
from dataclasses import dataclass
from uuid import UUID

import db
import limits
from chunker import chunk_sections, text_hash
from embeddings import embed_texts, to_pgvector
from extract import Section

log = logging.getLogger("easychat.kb")

PUBLISH_JOB = "publish_kb"
PUBLISH_STATE_TTL_SECONDS = 7 * 86400
ENTRY_URL_PREFIX = "entry:"
MAX_ENTRIES = 200                     # per tenant (free plan)


# --- Crawled pages ------------------------------------------------------------------------------

async def list_pages(tenant_id: UUID) -> list[dict]:
    """The crawled pages (not entries), with how many chunks each has in the index."""
    async with db.tenant_tx(tenant_id) as conn:
        cur = await conn.execute(
            "SELECT p.id, p.url, p.title, p.kind, p.hidden, p.fetched_at, "
            "(SELECT count(*) FROM kb_chunks c WHERE c.page_id = p.id) AS chunks "
            "FROM kb_pages p WHERE p.kind <> 'entry' ORDER BY p.hidden, p.title, p.url")
        rows = await cur.fetchall()
    return [{**r, "fetched_at": r["fetched_at"].isoformat()} for r in rows]


async def set_hidden(tenant_id: UUID, page_id: int, hidden: bool) -> bool:
    """Hide (drop from answers now) or un-hide (re-indexed at the next re-sync). False if no such page."""
    async with db.tenant_tx(tenant_id) as conn:
        cur = await conn.execute(
            "UPDATE kb_pages SET hidden = %s, content_hash = CASE WHEN %s THEN content_hash ELSE '' END "
            "WHERE id = %s AND kind <> 'entry'", (hidden, hidden, page_id))
        if cur.rowcount == 0:
            return False
        if hidden:
            await conn.execute("DELETE FROM kb_chunks WHERE page_id = %s", (page_id,))
            await conn.execute("DELETE FROM products WHERE page_id = %s", (page_id,))
    return True


# --- The team's entries -------------------------------------------------------------------------

@dataclass(frozen=True)
class EntryIn:
    """What the team edits."""
    section: str
    title: str
    body: str


def _entry_json(row: dict) -> dict:
    """An entry for the app, with whether it has unpublished changes."""
    live = row["published_body"] is not None
    changed = row["deleted"] or not live or (row["title"], row["body"]) != (row["published_title"],
                                                                             row["published_body"])
    return {"id": str(row["id"]), "section": row["section"], "title": row["title"], "body": row["body"],
            "live": live, "changed": changed, "deleted": row["deleted"],
            "updated_at": row["updated_at"].isoformat(),
            "published_at": row["published_at"].isoformat() if row["published_at"] else None}


async def list_entries(tenant_id: UUID) -> list[dict]:
    """Every entry, by section and title (deleted-but-still-live ones included, marked)."""
    async with db.tenant_tx(tenant_id) as conn:
        cur = await conn.execute("SELECT * FROM kb_entries ORDER BY section, title")
        return [_entry_json(r) for r in await cur.fetchall()]


async def create_entry(tenant_id: UUID, entry: EntryIn) -> dict | None:
    """A new draft entry; None when the tenant has MAX_ENTRIES already."""
    async with db.tenant_tx(tenant_id) as conn:
        count = (await (await conn.execute("SELECT count(*) AS n FROM kb_entries")).fetchone())["n"]
        if count >= MAX_ENTRIES:
            return None
        cur = await conn.execute("INSERT INTO kb_entries (tenant_id, section, title, body) VALUES (%s, %s, %s, %s) "
                                 "RETURNING *", (tenant_id, entry.section, entry.title, entry.body))
        return _entry_json(await cur.fetchone())


async def update_entry(tenant_id: UUID, entry_id: UUID, entry: EntryIn) -> dict | None:
    """Change an entry's draft (also un-deletes it); None if there's no such entry."""
    async with db.tenant_tx(tenant_id) as conn:
        cur = await conn.execute("UPDATE kb_entries SET section = %s, title = %s, body = %s, deleted = false, "
                                 "updated_at = now() WHERE id = %s RETURNING *",
                                 (entry.section, entry.title, entry.body, entry_id))
        row = await cur.fetchone()
    return _entry_json(row) if row else None


async def delete_entry(tenant_id: UUID, entry_id: UUID) -> bool:
    """A never-published entry goes at once; a live one is marked and goes at the next Publish."""
    async with db.tenant_tx(tenant_id) as conn:
        cur = await conn.execute("DELETE FROM kb_entries WHERE id = %s AND published_body IS NULL", (entry_id,))
        if cur.rowcount:
            return True
        cur = await conn.execute("UPDATE kb_entries SET deleted = true, updated_at = now() WHERE id = %s",
                                 (entry_id,))
        return cur.rowcount > 0


# --- Publishing (worker) ------------------------------------------------------------------------

def _state_key(tenant_id: UUID) -> str:
    """Redis: the latest publish's state, for the app to show."""
    return f"kb:publish:{tenant_id}"


async def set_publish_state(tenant_id: UUID, state: str, detail: str = "") -> None:
    """queued | running | done | failed (with a short, safe detail)."""
    await limits.client.set(_state_key(tenant_id), json.dumps({"state": state, "detail": detail[:200]}),
                            ex=PUBLISH_STATE_TTL_SECONDS)


async def publish_state(tenant_id: UUID) -> dict | None:
    """The latest publish's state, if any."""
    raw = await limits.client.get(_state_key(tenant_id))
    return json.loads(raw) if raw else None


async def _index_entry(conn, tenant_id: UUID, row: dict) -> int:
    """Chunk + embed one entry into its kb_pages row (replacing older chunks); embed tokens used."""
    chunks = chunk_sections([Section(heading=row["title"], text=row["body"])])
    texts = [c.embed_text(row["title"]) for c in chunks]
    vectors, tokens = await embed_texts(texts) if texts else ([], 0)
    url = f"{ENTRY_URL_PREFIX}{row['id']}"
    cur = await conn.execute(
        "INSERT INTO kb_pages (tenant_id, url, title, kind, content_hash) VALUES (%s, %s, %s, 'entry', %s) "
        "ON CONFLICT (tenant_id, url) DO UPDATE SET title = EXCLUDED.title, content_hash = EXCLUDED.content_hash, "
        "fetched_at = now() RETURNING id",
        (tenant_id, url, row["title"][:300], text_hash(row["title"] + "\n" + row["body"])))
    page_id = (await cur.fetchone())["id"]
    await conn.execute("DELETE FROM kb_chunks WHERE page_id = %s", (page_id,))
    for chunk, text, vector in zip(chunks, texts, vectors, strict=True):
        await conn.execute(
            "INSERT INTO kb_chunks (tenant_id, page_id, ord, heading, content, text_hash, embedding) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s::vector)",
            (tenant_id, page_id, chunk.ord, chunk.heading, chunk.content, text_hash(text), to_pgvector(vector)))
    return tokens


async def publish(tenant_id: UUID) -> int:
    """Worker job: make every changed entry live (deleted ones disappear). Each entry is its own
    transaction, so a failure (e.g. the embedding service) leaves the rest consistent and the job
    can simply run again. Returns embedding tokens used."""
    await set_publish_state(tenant_id, "running")
    async with db.tenant_tx(tenant_id) as conn:
        cur = await conn.execute(
            "SELECT * FROM kb_entries WHERE deleted OR published_body IS NULL "
            "OR title IS DISTINCT FROM published_title OR body IS DISTINCT FROM published_body")
        changed = await cur.fetchall()
    tokens = 0
    try:
        for row in changed:
            async with db.tenant_tx(tenant_id) as conn:
                if row["deleted"]:
                    await conn.execute("DELETE FROM kb_pages WHERE url = %s", (f"{ENTRY_URL_PREFIX}{row['id']}",))
                    await conn.execute("DELETE FROM kb_entries WHERE id = %s", (row["id"],))
                    continue
                tokens += await _index_entry(conn, tenant_id, row)
                await conn.execute("UPDATE kb_entries SET published_title = title, published_body = body, "
                                   "published_at = now() WHERE id = %s AND updated_at = %s",
                                   (row["id"], row["updated_at"]))
    except Exception as exc:
        await set_publish_state(tenant_id, "failed", "The knowledge service didn't answer. Try publishing again.")
        log.warning("publish for tenant %s failed: %s", tenant_id, type(exc).__name__)
        raise
    finally:
        await limits.record_tokens(tenant_id, tokens)
    await set_publish_state(tenant_id, "done", f"{len(changed)} change(s) published")
    log.info("published %d knowledge changes for tenant %s", len(changed), tenant_id)
    return tokens
