"""Background worker (roadmap step 7): takes jobs from the queue (jobs.py) and runs them.

    cd api && python worker.py          # keep it running next to the API

Jobs:
  crawl  {tenant_id, crawl_job_id}  index the tenant's website (onboarding and "crawl again")

One worker process is enough for the MVP (see jobs.py on crash recovery). Each job runs with
the API's database role inside the tenant's RLS scope, exactly like a request.
"""
import asyncio
import logging
from uuid import UUID

import redis.asyncio as aioredis

import db
import limits
from config import settings
from crawler import CrawlError, run_crawl
from jobs import Job, RedisQueue
from middleware import configure_logging
from models import TenantConfig

log = logging.getLogger("easychat.worker")

POLL_SECONDS = 5                  # how long one wait for a job blocks (then it loops)
RETRY_PAUSE_SECONDS = 10          # pause after a failed job before it is retried
REDIS_RETRY_SECONDS = 3           # pause before polling again when Redis is unreachable


async def crawl_job(payload: dict) -> None:
    """Crawl one tenant's website into its knowledge base, updating the queued crawl_jobs row."""
    tenant_id, job_id = UUID(payload["tenant_id"]), UUID(payload["crawl_job_id"])
    async with db.tenant_tx(tenant_id) as conn:
        row = await (await conn.execute("SELECT config FROM tenants WHERE id = %s", (tenant_id,))).fetchone()
    knowledge = TenantConfig.model_validate(row["config"]).knowledge if row else None
    if knowledge is None:
        log.warning("crawl job for tenant %s without a website: skipped", tenant_id)
        return
    try:
        _, stats = await run_crawl(tenant_id, knowledge, allow_local=settings.is_dev, job_id=job_id)
    except CrawlError as exc:      # the site itself is the problem (no pages): retrying won't help
        log.warning("crawl for tenant %s failed: %s", tenant_id, exc)
        return
    await limits.record_tokens(tenant_id, stats.embed_tokens)
    log.info("crawl done for tenant %s: %d indexed, %d failed", tenant_id, stats.pages_indexed, stats.pages_failed)


HANDLERS = {"crawl": crawl_job}


async def run_one(queue: RedisQueue, job: Job, raw: str) -> None:
    """Run one job; on failure queue it again (up to jobs.MAX_ATTEMPTS)."""
    handler = HANDLERS.get(job.name)
    if handler is None:
        log.error("unknown job %r dropped", job.name)
        await queue.done(raw)
        return
    try:
        await handler(job.payload)
    except Exception:
        log.exception("job %s (%s) failed", job.id, job.name)
        await asyncio.sleep(RETRY_PAUSE_SECONDS)
        await queue.retry(job, raw)
        return
    await queue.done(raw)


async def poll_once(queue: RedisQueue) -> None:
    """Wait for one job and run it. A Redis outage (restart, laptop sleep, network blip) is
    logged and waited out instead of killing the worker. A job taken just before the outage
    stays in the processing list and is requeued by recover() at the next worker start."""
    try:
        taken = await queue.take(POLL_SECONDS)
        if taken:
            await run_one(queue, *taken)
    except (aioredis.ConnectionError, aioredis.TimeoutError) as exc:
        log.warning("redis unavailable (%s): retrying in %d s", exc, REDIS_RETRY_SECONDS)
        await asyncio.sleep(REDIS_RETRY_SECONDS)


async def main() -> None:
    """Recover a crashed worker's jobs, then process jobs until stopped (Ctrl+C)."""
    configure_logging()
    # Its own client: the shared one times out after 2 s, shorter than a blocking wait for a job.
    client = aioredis.from_url(settings.redis_url, socket_timeout=POLL_SECONDS + 5, socket_connect_timeout=2)
    queue = RedisQueue(client)
    await db.open_pool()
    try:
        recovered = await queue.recover()
        log.info("worker ready%s", f" ({recovered} unfinished jobs requeued)" if recovered else "")
        while True:
            await poll_once(queue)
    finally:
        await db.close_pool()
        await client.aclose()
        await limits.client.aclose()


if __name__ == "__main__":
    try:
        # SelectorEventLoop: psycopg's async driver can't use Windows' default loop (see serve_local.py).
        asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
    except KeyboardInterrupt:
        pass
