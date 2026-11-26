"""jobs.py + worker.run_one: the Redis job queue (FIFO, crash recovery, retries) on the test Redis db."""
import pytest

import limits
import worker
from jobs import MAX_ATTEMPTS, PROCESSING_KEY, QUEUE_KEY, RedisQueue


@pytest.fixture
async def queue():
    """A queue on the test Redis db (emptied by conftest.clean_state before each test)."""
    return RedisQueue(limits.client)


async def test_jobs_come_out_in_order(queue):
    """First in, first out; an empty queue returns None after the timeout."""
    await queue.enqueue("crawl", {"n": 1})
    await queue.enqueue("crawl", {"n": 2})
    first, raw = await queue.take(1)
    await queue.done(raw)
    second, raw = await queue.take(1)
    await queue.done(raw)
    assert (first.payload["n"], second.payload["n"]) == (1, 2)
    assert await queue.take(0.1) is None
    assert await limits.client.llen(PROCESSING_KEY) == 0


async def test_a_crashed_workers_job_is_recovered(queue):
    """A job taken but never finished is put back when a worker starts."""
    await queue.enqueue("crawl", {"n": 1})
    await queue.take(1)                                   # the worker "crashes" here
    assert await limits.client.llen(QUEUE_KEY) == 0
    assert await queue.recover() == 1
    job, _ = await queue.take(1)
    assert job.payload == {"n": 1}


async def test_failed_jobs_are_retried_then_dropped(queue, monkeypatch):
    """A failing handler is retried up to MAX_ATTEMPTS times, then the job is dropped."""
    calls = []

    async def always_fails(payload):
        """A handler that never succeeds."""
        calls.append(payload)
        raise RuntimeError("boom")

    monkeypatch.setitem(worker.HANDLERS, "flaky", always_fails)
    monkeypatch.setattr(worker, "RETRY_PAUSE_SECONDS", 0)
    await queue.enqueue("flaky", {"x": 1})
    while (taken := await queue.take(0.1)) is not None:
        await worker.run_one(queue, *taken)
    assert len(calls) == MAX_ATTEMPTS
    assert await limits.client.llen(QUEUE_KEY) == 0 and await limits.client.llen(PROCESSING_KEY) == 0


async def test_unknown_jobs_are_dropped(queue):
    """A job with no handler is removed, not retried forever."""
    await queue.enqueue("no-such-job", {})
    await worker.run_one(queue, *await queue.take(1))
    assert await limits.client.llen(PROCESSING_KEY) == 0 and await limits.client.llen(QUEUE_KEY) == 0


async def test_a_redis_outage_does_not_kill_the_worker(monkeypatch):
    """A Redis timeout while waiting for a job is waited out; the worker keeps polling."""
    class DownQueue:
        """A queue whose Redis connection has dropped."""
        async def take(self, _timeout):
            """Fail like a Redis read timeout."""
            raise worker.aioredis.TimeoutError("Timeout reading from 127.0.0.1:6379")
    monkeypatch.setattr(worker, "REDIS_RETRY_SECONDS", 0)
    await worker.poll_once(DownQueue())          # no exception escapes
