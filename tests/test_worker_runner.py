"""
The worker loop.

Nothing here parses a PDF — `test_extraction.py` does that. What is pinned here
is that the loop *keeps going*, because the way a background worker fails in
production is silently: it hits one bad pass at three in the morning, exits, and
nobody notices until a week of uploads are sitting at `pending` and every
coursework question is being answered "I could not find this in your material".

Two properties, and they pull in opposite directions:

* A failing pass must not kill the loop.
* A stop must not have to wait out the idle sleep, or systemd sends SIGKILL to
  a worker that was only sleeping.
"""

import asyncio

import pytest

from app.workers.runner import Worker


class _Clock:
    """Records what the loop slept for, without actually sleeping."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def sleep(self, worker: Worker, seconds: float) -> None:
        self.waits.append(seconds)
        # Yield, so a concurrent `stop()` gets a turn.
        await asyncio.sleep(0)


async def _run_briefly(worker: Worker, ticks: int) -> None:
    """Let the loop run for a bounded number of passes, then stop it."""
    task = asyncio.create_task(worker.run())
    for _ in range(ticks * 4):
        await asyncio.sleep(0)
    worker.stop()
    await asyncio.wait_for(task, timeout=5)


async def test_a_failing_pass_does_not_kill_the_loop(monkeypatch):
    """
    The property the whole design turns on.

    A database that has gone away, a Supabase blip, a malformed row — any of
    them raising out of `_tick` must be one bad pass, not the end of the worker.
    """
    calls = {"count": 0}
    clock = _Clock()

    async def failing_tick(self, client):
        calls["count"] += 1
        raise RuntimeError("the database went away")

    monkeypatch.setattr(Worker, "_tick", failing_tick)
    monkeypatch.setattr(Worker, "_sleep", clock.sleep)

    worker = Worker()
    await _run_briefly(worker, ticks=3)

    assert calls["count"] > 1, "the loop gave up after the first failure"
    # And it backed off rather than spinning at full speed on a dead database.
    assert all(wait >= 30 for wait in clock.waits), clock.waits


async def test_an_empty_queue_idles_rather_than_spinning(monkeypatch):
    clock = _Clock()

    async def empty_tick(self, client):
        return False

    monkeypatch.setattr(Worker, "_tick", empty_tick)
    monkeypatch.setattr(Worker, "_sleep", clock.sleep)

    worker = Worker()
    await _run_briefly(worker, ticks=2)

    assert clock.waits, "an empty queue should sleep"
    assert all(wait <= 10 for wait in clock.waits), "idle waits should be short"


async def test_work_found_means_no_sleep(monkeypatch):
    """A full queue is drained back to back, not one document every five seconds."""
    clock = _Clock()
    calls = {"count": 0}

    async def busy_tick(self, client):
        calls["count"] += 1
        return True

    monkeypatch.setattr(Worker, "_tick", busy_tick)
    monkeypatch.setattr(Worker, "_sleep", clock.sleep)

    worker = Worker()
    await _run_briefly(worker, ticks=3)

    assert calls["count"] > 1
    assert clock.waits == [], "a worker with work to do should not be sleeping"


async def test_stop_does_not_wait_out_the_idle_sleep():
    """
    `_sleep` waits on the shutdown event, not the clock.

    A plain `asyncio.sleep` would make systemd wait the full interval on every
    restart and eventually SIGKILL a worker that was merely idle.
    """
    worker = Worker()
    worker.stop()

    # Already stopping: the wait must return at once rather than in ten seconds.
    await asyncio.wait_for(worker._sleep(10.0), timeout=0.5)


async def test_stopping_twice_is_harmless():
    """SIGTERM then SIGINT during a slow shutdown is ordinary, not an error."""
    worker = Worker()
    worker.stop()
    worker.stop()
    assert worker._stopping.is_set()


async def test_a_stop_mid_batch_leaves_the_rest_for_the_next_pass(monkeypatch, client):
    """
    Stopping between documents loses nothing: the untouched ones are still
    `pending`, so another pass or another worker takes them.
    """
    import uuid

    from app.workers import extraction

    seen: list[uuid.UUID] = []
    ids = [uuid.uuid4() for _ in range(4)]

    async def claim(session, limit):
        return ids

    async def extract(session, material_id, *, client):
        seen.append(material_id)
        worker.stop()  # stop after the first document
        return "done"

    async def no_requeue(session, older_than_minutes=15):
        return 0

    monkeypatch.setattr(extraction, "claim_batch", claim)
    monkeypatch.setattr(extraction, "extract_material", extract)
    monkeypatch.setattr(extraction, "requeue_stalled", no_requeue)

    worker = Worker()
    await asyncio.wait_for(worker.run(), timeout=5)

    assert len(seen) == 1, "the batch should stop after the document in hand"
