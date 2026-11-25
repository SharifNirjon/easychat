"""Create the eval tenants and index their fixture websites. Run once, and again after
changing anything in evals/sites/ or evals/seeds/ (unchanged pages are skipped):

    cd api && python -m evals.setup

Two fictional tenants (evals/seeds/):
  * eval-fernwood: the shop every case talks to. Its site (evals/sites/fernwood/) has real
    policy pages, blog articles whose *example* policies differ from the real ones, and a
    reviews page with planted prompt injections.
  * eval-rival: a second tenant whose page holds a secret string. No Fernwood reply may ever
    contain it (row-level security should make that impossible; the evals prove it).

The pages are served from a local HTTP server and indexed by the real crawler, so extraction,
chunking and bge-m3 embeddings are exactly what production does. Needs APP_ENV=local or test
(the crawler only fetches plain-http localhost URLs in dev) and the usual api/.env.
"""
import asyncio
import functools
import os
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psycopg

import db
import limits
import seed_tenant
from config import settings
from crawl import tenant_by_slug
from crawler import run_crawl
from models import TenantSeed

EVALS_DIR = Path(__file__).resolve().parent
SITES_DIR = EVALS_DIR / "sites"
SEEDS = [EVALS_DIR / "seeds" / "fernwood.json", EVALS_DIR / "seeds" / "rival.json"]
SITE_HOST, SITE_PORT = "127.0.0.1", 8799     # must match the URLs in the seeds and sitemaps


class _QuietHandler(SimpleHTTPRequestHandler):
    """Serves evals/sites/ without logging every request."""

    def log_message(self, format, *args):  # noqa: A002 (signature defined by the base class)
        """Silence the default per-request log line."""


def serve_sites() -> ThreadingHTTPServer:
    """Start a background HTTP server for the fixture sites and return it (call .shutdown())."""
    handler = functools.partial(_QuietHandler, directory=str(SITES_DIR))
    server = ThreadingHTTPServer((SITE_HOST, SITE_PORT), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def seed_tenants() -> list[str]:
    """Create or update the eval tenants as the schema owner; return their slugs."""
    url = os.getenv("MIGRATION_DATABASE_URL", "").strip()
    if not url:
        sys.exit("Missing required env var MIGRATION_DATABASE_URL (see api/.env.example)")
    tenants = [TenantSeed.model_validate_json(path.read_text(encoding="utf-8")) for path in SEEDS]
    with psycopg.connect(url, autocommit=True) as conn:
        for tenant in tenants:
            seed_tenant.seed(conn, tenant)
    return [t.slug for t in tenants]


async def index_sites(slugs: list[str]) -> None:
    """Crawl each eval tenant's fixture site with the real crawler."""
    await db.open_pool()
    try:
        for slug in slugs:
            tenant_id, config = tenant_by_slug(slug)
            _, stats = await run_crawl(tenant_id, config.knowledge, allow_local=True)
            print(f"{slug}: {stats.pages_indexed} indexed, {stats.pages_unchanged} unchanged, "
                  f"{stats.pages_skipped} skipped, {stats.pages_failed} failed, {stats.pages_removed} removed")
            if stats.pages_failed or stats.pages_skipped:
                sys.exit(f"{slug}: some fixture pages were not indexed; fix evals/sites/ first")
    finally:
        await db.close_pool()
        await limits.client.aclose()


def main() -> None:
    """Seed the tenants, then serve and index their sites."""
    if not settings.is_dev:
        sys.exit("evals.setup needs APP_ENV=local or test (it crawls plain-http localhost pages)")
    slugs = seed_tenants()
    server = serve_sites()
    try:
        asyncio.run(index_sites(slugs), loop_factory=asyncio.SelectorEventLoop)
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
