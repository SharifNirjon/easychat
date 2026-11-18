"""Crawl a tenant's site into its knowledge base: discover -> fetch -> extract -> chunk -> embed -> store.

    stats = await run_crawl(tenant_id, knowledge_config, allow_local=settings.is_dev)

Runs as the API's database role inside db.tenant_tx(), so row-level security applies to every
write. Today crawl.py (CLI) calls it; in roadmap step 7 a queue worker will.

Recrawl semantics (a crawl never leaves half-indexed pages behind):
  * a page whose extracted content is unchanged is only marked as seen (no re-embedding);
  * a changed page has its chunks and products replaced in one transaction;
  * a chunk whose exact text was embedded before reuses that vector (no embedding cost);
  * a page that fails to fetch (network/5xx) keeps its previous copy;
  * at the end of a successful crawl, pages this crawl didn't see (removed from the sitemap,
    404, noindex) are deleted. A failed crawl deletes nothing.
"""
import hashlib
import json
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from urllib.parse import urlsplit
from uuid import UUID

import db
from chunker import Chunk, chunk_sections, text_hash
from embeddings import embed_texts, to_pgvector
from extract import Page, SitemapError, parse_html, parse_sitemap
from fetcher import (HTML_TYPES, PAGE_MAX_BYTES, SITEMAP_MAX_BYTES, XML_TYPES, Fetcher, FetchError,
                     check_url, host_key)
from models import KnowledgeConfig
from products import Product, page_kind, products_from_json_ld

log = logging.getLogger("easychat.crawler")

MAX_SITEMAPS = 10              # sitemap files read per crawl (index + children)
GONE_STATUSES = (404, 410)     # the page was removed: let the end-of-crawl cleanup drop it
NON_PAGE_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".zip", ".gz",
                       ".mp3", ".mp4", ".mov", ".webm", ".css", ".js", ".json", ".xml", ".txt",
                       ".woff", ".woff2", ".ttf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}


class CrawlError(RuntimeError):
    """The crawl as a whole failed (e.g. no sitemap could be read). Nothing is deleted."""


@dataclass
class CrawlStats:
    """Counters for one crawl run; also saved to crawl_jobs for progress screens."""
    pages_found: int = 0
    pages_indexed: int = 0
    pages_unchanged: int = 0
    pages_skipped: int = 0
    pages_failed: int = 0
    pages_removed: int = 0
    chunks_embedded: int = 0
    embed_tokens: int = 0


Progress = Callable[[str, str], None]    # (outcome, url) -> None, e.g. for printing


# --- Discovery ----------------------------------------------------------------------------------

def _is_page_url(url: str) -> bool:
    """False for URLs that obviously aren't web pages (images, PDFs, scripts...)."""
    return PurePosixPath(urlsplit(url).path).suffix.lower() not in NON_PAGE_EXTENSIONS


async def discover(fetcher: Fetcher, knowledge: KnowledgeConfig) -> list[str]:
    """Page URLs from the sitemaps (following sitemap indexes) plus the start URLs, deduplicated."""
    found: dict[str, None] = {}

    def add(url: str) -> None:
        """Keep a URL if it is safe and on this crawl's sites; first occurrence wins."""
        try:
            clean = check_url(url, fetcher.allowed_hosts, fetcher.allow_local)
        except FetchError:
            return                                   # off-site or unsafe entries are ignored
        if _is_page_url(clean):
            found.setdefault(clean, None)

    for url in knowledge.start_urls:
        add(str(url))
    pending = [str(url) for url in knowledge.sitemaps]
    read = 0
    errors: list[str] = []
    while pending and read < MAX_SITEMAPS and len(found) < knowledge.max_pages:
        sitemap = pending.pop(0)
        read += 1
        try:
            result = await fetcher.get(sitemap, XML_TYPES, SITEMAP_MAX_BYTES)
            pages, children = parse_sitemap(result.body)
        except (FetchError, SitemapError) as exc:
            log.warning("sitemap %s unreadable: %s", sitemap, exc)
            errors.append(f"{sitemap}: {exc}")
            continue
        for page in pages:
            add(page)
        pending.extend(children)

    if not found:
        raise CrawlError("no pages found" + (f" ({errors[0]})" if errors else ""))
    return list(found)[:knowledge.max_pages]


# --- Storage ------------------------------------------------------------------------------------

async def _start_job(tenant_id: UUID) -> UUID:
    """Create the crawl_jobs row that tracks this run; return its id."""
    async with db.tenant_tx(tenant_id) as conn:
        cur = await conn.execute("INSERT INTO crawl_jobs (tenant_id) VALUES (%s) RETURNING id", (tenant_id,))
        return (await cur.fetchone())["id"]


async def _save_progress(tenant_id: UUID, job_id: UUID, stats: CrawlStats, status: str | None = None,
                         error: str | None = None) -> None:
    """Write the current counters (and, at the end, the final status) to the job row."""
    fields = asdict(stats)
    assignments = ", ".join(f"{name} = %({name})s" for name in fields)
    if status:
        assignments += ", status = %(status)s, error = %(error)s, finished_at = now()"
    async with db.tenant_tx(tenant_id) as conn:
        await conn.execute(f"UPDATE crawl_jobs SET {assignments} WHERE id = %(job_id)s",
                           {**fields, "status": status, "error": error and error[:500], "job_id": job_id})


async def _existing_hash(tenant_id: UUID, url: str) -> str | None:
    """Content hash stored for this URL by an earlier crawl, or None if it is new."""
    async with db.tenant_tx(tenant_id) as conn:
        cur = await conn.execute("SELECT content_hash FROM kb_pages WHERE url = %s", (url,))
        row = await cur.fetchone()
        return row["content_hash"] if row else None


async def _mark_seen(tenant_id: UUID, job_id: UUID, url: str) -> None:
    """Record that this crawl saw the page, so the end-of-crawl cleanup keeps it."""
    async with db.tenant_tx(tenant_id) as conn:
        await conn.execute("UPDATE kb_pages SET last_job_id = %s WHERE url = %s", (job_id, url))


async def _known_vectors(tenant_id: UUID, hashes: list[str]) -> dict[str, str]:
    """Vectors already stored for these exact texts (pgvector text format)."""
    async with db.tenant_tx(tenant_id) as conn:
        cur = await conn.execute(
            "SELECT DISTINCT ON (text_hash) text_hash, embedding::text AS vector "
            "FROM kb_chunks WHERE text_hash = ANY(%s)", (hashes,),
        )
        return {row["text_hash"]: row["vector"] for row in await cur.fetchall()}


async def _replace_page(tenant_id: UUID, job_id: UUID, url: str, title: str, kind: str, content_hash: str,
                        products: list[Product], chunks: list[tuple[Chunk, str, str]],
                        product_chunks: list[tuple[str, str, str]]) -> None:
    """Upsert the page and swap in its new products and chunks, all in one transaction.

    chunks: (chunk, text_hash, vector); product_chunks: (summary, text_hash, vector), same order
    as products.
    """
    async with db.tenant_tx(tenant_id) as conn:
        cur = await conn.execute(
            "INSERT INTO kb_pages (tenant_id, url, title, kind, content_hash, last_job_id) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (tenant_id, url) DO UPDATE SET title = EXCLUDED.title, kind = EXCLUDED.kind, "
            "content_hash = EXCLUDED.content_hash, last_job_id = EXCLUDED.last_job_id, fetched_at = now() "
            "RETURNING id",
            (tenant_id, url, title, kind, content_hash, job_id),
        )
        page_id = (await cur.fetchone())["id"]
        await conn.execute("DELETE FROM kb_chunks WHERE page_id = %s", (page_id,))
        await conn.execute("DELETE FROM products WHERE page_id = %s", (page_id,))
        for chunk, digest, vector in chunks:
            await conn.execute(
                "INSERT INTO kb_chunks (tenant_id, page_id, ord, heading, content, text_hash, embedding) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s::vector)",
                (tenant_id, page_id, chunk.ord, chunk.heading, chunk.content, digest, vector),
            )
        for offset, (product, (summary, digest, vector)) in enumerate(zip(products, product_chunks)):
            cur = await conn.execute(
                "INSERT INTO products (tenant_id, page_id, name, url, description, image_url, brand, price, "
                "currency, availability, rating, review_count) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (tenant_id, page_id, product.name, product.url, product.description, product.image_url,
                 product.brand, product.price, product.currency, product.availability, product.rating,
                 product.review_count),
            )
            product_id = (await cur.fetchone())["id"]
            await conn.execute(
                "INSERT INTO kb_chunks (tenant_id, page_id, product_id, ord, heading, content, text_hash, embedding) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector)",
                (tenant_id, page_id, product_id, len(chunks) + offset, product.name[:300], summary, digest, vector),
            )


async def _remove_unseen(tenant_id: UUID, job_id: UUID) -> int:
    """Delete pages this crawl didn't see (their chunks/products cascade); return how many."""
    async with db.tenant_tx(tenant_id) as conn:
        cur = await conn.execute("DELETE FROM kb_pages WHERE last_job_id IS DISTINCT FROM %s", (job_id,))
        return cur.rowcount


# --- One page -----------------------------------------------------------------------------------

def _content_hash(page: Page, kind: str, products: list[Product]) -> str:
    """Fingerprint of everything indexed from a page: unchanged hash = nothing to redo."""
    payload = json.dumps([page.title, kind, [(s.heading, s.text) for s in page.sections],
                          [asdict(p) for p in products]], sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


async def _vectors_for(tenant_id: UUID, texts: list[str], stats: CrawlStats) -> list[tuple[str, str]]:
    """(text_hash, vector) per text, embedding only texts never embedded before."""
    hashes = [text_hash(t) for t in texts]
    known = await _known_vectors(tenant_id, list(set(hashes))) if hashes else {}
    missing = [i for i, h in enumerate(hashes) if h not in known]
    if missing:
        vectors, tokens = await embed_texts([texts[i] for i in missing])
        stats.embed_tokens += tokens
        stats.chunks_embedded += len(missing)
        for i, vector in zip(missing, vectors):
            known[hashes[i]] = to_pgvector(vector)
    return [(h, known[h]) for h in hashes]


async def crawl_page(fetcher: Fetcher, tenant_id: UUID, job_id: UUID, url: str, indexed: set[str],
                     stats: CrawlStats) -> tuple[str, Page | None]:
    """Fetch and index one page. Returns (outcome, parsed page or None)."""
    if not await fetcher.allowed_by_robots(url):
        stats.pages_skipped += 1
        return "skipped: robots.txt", None
    try:
        result = await fetcher.get(url, HTML_TYPES, PAGE_MAX_BYTES)
    except FetchError as exc:
        if exc.blocked or exc.status in GONE_STATUSES:
            stats.pages_skipped += 1
            return f"skipped: {exc}", None
        stats.pages_failed += 1
        await _mark_seen(tenant_id, job_id, url)          # keep the previous copy, if any
        return f"failed: {exc}", None

    page = parse_html(result.text(), result.url)
    if page.noindex or "noindex" in result.headers.get("x-robots-tag", "").lower():
        stats.pages_skipped += 1
        return "skipped: noindex", page

    # Index under the canonical URL when it's on an allowed site, so duplicates collapse.
    key = result.url
    if page.canonical:
        try:
            key = check_url(page.canonical, fetcher.allowed_hosts, fetcher.allow_local)
        except FetchError:
            pass                                     # off-site canonical: keep the fetched URL
    if key in indexed:
        stats.pages_skipped += 1
        return "skipped: duplicate", page
    indexed.add(key)

    products = products_from_json_ld(page.json_ld, key)
    if not page.sections and not products:
        stats.pages_skipped += 1
        return "skipped: no text (needs JavaScript?)", page

    kind = page_kind(page.json_ld)
    content_hash = _content_hash(page, kind, products)
    if await _existing_hash(tenant_id, key) == content_hash:
        await _mark_seen(tenant_id, job_id, key)
        stats.pages_unchanged += 1
        return "unchanged", page

    chunks = chunk_sections(page.sections)
    chunk_vectors = await _vectors_for(tenant_id, [c.embed_text(page.title) for c in chunks], stats)
    summaries = [p.summary() for p in products]
    product_vectors = await _vectors_for(tenant_id, summaries, stats)
    await _replace_page(
        tenant_id, job_id, key, page.title, kind, content_hash, products,
        [(c, h, v) for c, (h, v) in zip(chunks, chunk_vectors)],
        [(s, h, v) for s, (h, v) in zip(summaries, product_vectors)],
    )
    stats.pages_indexed += 1
    return f"indexed: {len(chunks)} chunks, {len(products)} products", page


# --- The crawl ----------------------------------------------------------------------------------

async def run_crawl(tenant_id: UUID, knowledge: KnowledgeConfig, allow_local: bool,
                    progress: Progress | None = None) -> tuple[UUID, CrawlStats]:
    """Crawl and index. Raises CrawlError (or an embedding/DB error) after recording the failure."""
    stats = CrawlStats()
    job_id = await _start_job(tenant_id)
    sources = [str(u) for u in (*knowledge.sitemaps, *knowledge.start_urls)]
    allowed_hosts = frozenset(host_key(u) for u in sources)
    follow_links = not knowledge.sitemaps       # no sitemap: discover pages by following links
    try:
        async with Fetcher(allowed_hosts, allow_local) as fetcher:
            queue = deque(await discover(fetcher, knowledge))
            seen = set(queue)
            stats.pages_found = len(seen)
            indexed: set[str] = set()
            processed = 0
            while queue and processed < knowledge.max_pages:
                url = queue.popleft()
                processed += 1
                outcome, page = await crawl_page(fetcher, tenant_id, job_id, url, indexed, stats)
                if progress:
                    progress(outcome, url)
                if follow_links and page and not page.noindex:
                    for link in page.links:
                        if len(seen) >= knowledge.max_pages:
                            break
                        try:
                            link = check_url(link, allowed_hosts, allow_local)
                        except FetchError:
                            continue
                        if link not in seen and _is_page_url(link):
                            seen.add(link)
                            queue.append(link)
                    stats.pages_found = len(seen)
                await _save_progress(tenant_id, job_id, stats)
        stats.pages_removed = await _remove_unseen(tenant_id, job_id)
        await _save_progress(tenant_id, job_id, stats, status="done")
    except Exception as exc:
        await _save_progress(tenant_id, job_id, stats, status="failed", error=str(exc) or type(exc).__name__)
        raise
    return job_id, stats
