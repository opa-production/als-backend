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

from app.workers.runner import Worker


class _Clock:
    """Records what the loop slept for, instead of actually sleeping."""

    def __init__(self) -> None:
        self.waits: list[float] = []


def _install(monkeypatch, clock: _Clock, tick, *, stop_after: int):
    """
    Replace the loop's two moving parts and bound the run.

    `_sleep` is patched with a plain function, not a bound method: assigning
    `clock.sleep` to the class would leave it already bound to the clock, so
    `worker._sleep(5)` would pass 5 as `worker` and never reach `seconds`.

    The tick stops the worker after a fixed number of passes rather than the
    test racing it with `asyncio.sleep(0)`. That also removes the hang the first
    version of this file had: a tick that always reports work and never awaits
    anything real never yields, so a stop set from outside is never seen.
    """
    calls = {"count": 0}

    async def counted_tick(self, client):
        calls["count"] += 1
        if calls["count"] >= stop_after:
            self.stop()
        # A real tick always awaits a query or a download. Without a yield here
        # the loop spins without ever handing control back.
        await asyncio.sleep(0)
        return await tick(self, client)

    async def fake_sleep(self, seconds):
        clock.waits.append(seconds)
        await asyncio.sleep(0)

    monkeypatch.setattr(Worker, "_tick", counted_tick)
    monkeypatch.setattr(Worker, "_sleep", fake_sleep)
    return calls


async def test_a_failing_pass_does_not_kill_the_loop(monkeypatch):
    """
    The property the whole design turns on.

    A database that has gone away, a Supabase blip, a malformed row — any of
    them raising out of `_tick` must be one bad pass, not the end of the worker.
    This is exactly what a local run does with no Postgres, and the loop is
    expected to log and carry on rather than exit.
    """
    clock = _Clock()

    async def failing(self, client):
        raise RuntimeError("the database went away")

    calls = _install(monkeypatch, clock, failing, stop_after=3)

    worker = Worker()
    await asyncio.wait_for(worker.run(), timeout=5)

    assert calls["count"] >= 3, "the loop gave up after the first failure"
    # And it backed off rather than hammering a dead database.
    assert clock.waits and all(wait >= 30 for wait in clock.waits), clock.waits


async def test_an_empty_queue_idles_rather_than_spinning(monkeypatch):
    clock = _Clock()

    async def empty(self, client):
        return False

    _install(monkeypatch, clock, empty, stop_after=2)

    worker = Worker()
    await asyncio.wait_for(worker.run(), timeout=5)

    assert clock.waits, "an empty queue should sleep"
    assert all(wait <= 10 for wait in clock.waits), "idle waits should be short"


async def test_work_found_means_no_sleep(monkeypatch):
    """A full queue is drained back to back, not one document every five seconds."""
    clock = _Clock()

    async def busy(self, client):
        return True

    calls = _install(monkeypatch, clock, busy, stop_after=3)

    worker = Worker()
    await asyncio.wait_for(worker.run(), timeout=5)

    assert calls["count"] >= 3
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
