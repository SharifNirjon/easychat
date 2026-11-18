"""Crawl a tenant's site into its knowledge base (the admin path until onboarding, roadmap step 7).

    python crawl.py culpine

Reads what to crawl from the tenant's `knowledge` config (seeds/*.json). Looks the tenant up with
MIGRATION_DATABASE_URL (owner, like seed_tenant.py), then crawls and writes as the API role, so
row-level security applies to every row it writes. Embedding tokens count against the tenant's
daily budget.
"""
import asyncio
import os
import sys
from pathlib import Path
from uuid import UUID

import psycopg
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

import db  # noqa: E402  (config reads the environment at import)
import limits  # noqa: E402
from config import settings  # noqa: E402
from crawler import run_crawl  # noqa: E402
from models import TenantConfig  # noqa: E402


def tenant_by_slug(slug: str) -> tuple[UUID, TenantConfig]:
    """Owner lookup by slug: the API role can only see a tenant once it's scoped to it."""
    url = os.getenv("MIGRATION_DATABASE_URL", "").strip()
    if not url:
        sys.exit("Missing required env var MIGRATION_DATABASE_URL (see api/.env.example)")
    with psycopg.connect(url) as conn:
        row = conn.execute("SELECT id, config FROM tenants WHERE slug = %s", (slug,)).fetchone()
    if row is None:
        sys.exit(f"no tenant with slug {slug!r} (seed it first: python seed_tenant.py seeds/<file>.json)")
    return row[0], TenantConfig.model_validate(row[1])


def print_progress(outcome: str, url: str) -> None:
    print(f"  {outcome:<45} {url}", flush=True)


async def crawl(slug: str) -> None:
    tenant_id, config = tenant_by_slug(slug)
    if config.knowledge is None:
        sys.exit(f"tenant {slug!r} has no `knowledge` config: add sitemaps/start_urls to its seed file")
    await db.open_pool()
    try:
        print(f"crawling for {slug} (max {config.knowledge.max_pages} pages)")
        job_id, stats = await run_crawl(tenant_id, config.knowledge, allow_local=settings.is_dev,
                                        progress=print_progress)
        await limits.record_tokens(tenant_id, stats.embed_tokens)
    finally:
        await db.close_pool()
        await limits.client.aclose()
    print(f"done (job {job_id}): {stats.pages_found} found, {stats.pages_indexed} indexed, "
          f"{stats.pages_unchanged} unchanged, {stats.pages_skipped} skipped, {stats.pages_failed} failed, "
          f"{stats.pages_removed} removed; {stats.chunks_embedded} chunks embedded "
          f"({stats.embed_tokens} tokens)")


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: python crawl.py <tenant-slug>")
    # SelectorEventLoop: psycopg's async driver can't use Windows' default loop (see serve_local.py).
    asyncio.run(crawl(sys.argv[1]), loop_factory=asyncio.SelectorEventLoop)


if __name__ == "__main__":
    main()
