"""Background jobs: a small Redis queue behind a Queue interface (roadmap step 7).

v4 uses Pub/Sub; locally (and until step 14) this is a Redis list. Callers only use
`queue.enqueue(name, payload)`, so swapping the implementation doesn't touch them.
The worker process (worker.py) takes jobs and runs their handlers.

Reliability, kept simple:
  * A job is moved atomically from the queue to a "processing" list while it runs (BLMOVE), so
    a job whose worker crashed is still in Redis; recover() puts such jobs back when a worker starts.
    That assumes one worker process, which is all local dev and the MVP need.
  * A failed job is retried up to MAX_ATTEMPTS times, then dropped and logged.
Payloads must not hold secrets: they sit in Redis until the job is done.
"""
import json
import logging
import secrets
from dataclasses import asdict, dataclass, field
from typing import Protocol

import redis.asyncio as aioredis

log = logging.getLogger("easychat.jobs")

QUEUE_KEY = "jobs:queue"
PROCESSING_KEY = "jobs:processing"
MAX_ATTEMPTS = 3


@dataclass
class Job:
    """One unit of background work."""
    name: str                       # which handler runs it (see worker.HANDLERS)
    payload: dict
    id: str = field(default_factory=lambda: secrets.token_hex(8))
    attempts: int = 0

    def encode(self) -> str:
        """The job as stored in Redis."""
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def decode(cls, raw: str | bytes) -> "Job":
        """A job read back from Redis."""
        return cls(**json.loads(raw))


class Queue(Protocol):
    """What the API needs from a job queue."""

    async def enqueue(self, name: str, payload: dict) -> str:
        """Add a job; return its id."""


class RedisQueue:
    """FIFO queue on a Redis list: LPUSH to add, BLMOVE from the other end to take."""

    def __init__(self, client: aioredis.Redis):
        """Use an existing Redis client (the API's, from limits.py)."""
        self.client = client

    async def enqueue(self, name: str, payload: dict) -> str:
        """Add a job at the back of the queue; return its id."""
        job = Job(name, payload)
        await self.client.lpush(QUEUE_KEY, job.encode())
        return job.id

    async def take(self, timeout_seconds: float) -> tuple[Job, str] | None:
        """Wait up to `timeout_seconds` for the next job: (job, raw form for done/retry), or None."""
        raw = await self.client.blmove(QUEUE_KEY, PROCESSING_KEY, timeout_seconds, "RIGHT", "LEFT")
        if raw is None:
            return None
        raw = raw.decode() if isinstance(raw, bytes) else raw
        return Job.decode(raw), raw

    async def done(self, raw: str) -> None:
        """The job finished: remove it from the processing list."""
        await self.client.lrem(PROCESSING_KEY, 1, raw)

    async def retry(self, job: Job, raw: str) -> bool:
        """The job failed: queue it again unless it has used all its attempts. True if requeued."""
        await self.client.lrem(PROCESSING_KEY, 1, raw)
        job.attempts += 1
        if job.attempts >= MAX_ATTEMPTS:
            log.error("job %s (%s) failed %d times: dropped", job.id, job.name, job.attempts)
            return False
        await self.client.lpush(QUEUE_KEY, job.encode())
        return True

    async def recover(self) -> int:
        """Put jobs left in the processing list (a crashed worker's) back on the queue."""
        moved = 0
        while await self.client.lmove(PROCESSING_KEY, QUEUE_KEY, "RIGHT", "RIGHT") is not None:
            moved += 1
        return moved
