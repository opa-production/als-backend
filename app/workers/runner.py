"""
The background worker.

    python -m app.workers.runner

A separate process from the API, deliberately. ARCHITECTURE.md §3 is explicit
about why: PDF parsing is CPU-bound, and a handler that blocks the event loop
stalls *every* other request on that process — the failure that looks like "the
whole API went down" when one endpoint got slow. Even on a thread, a 200-page
document competing with request handlers for the GIL makes every response
slower.

It polls rather than subscribing to anything. A queue would be better at scale
and is a second thing to run, keep alive and back up; `materials` already has
the index this query needs (`ix_materials_extraction`), and a five second delay
between uploading a PDF and being able to ask about it is not a delay anyone
notices. Redis becomes worth it when one box stops keeping up, not before.
"""

from __future__ import annotations

import asyncio
import signal

import httpx
import structlog

from app.core.config import settings
from app.core.logging import configure_logging
from app.db.session import SessionLocal, dispose_engine
from app.services import notifications, referrals, settlement
from app.workers import extraction

log = structlog.get_logger()

#: How long to wait when the queue is empty. Short enough that an upload is
#: readable almost immediately, long enough that an idle box is not running a
#: query every second all night.
IDLE_SECONDS = 5.0

#: How many documents one pass takes. Small: each is a download and a parse, and
#: a long batch is a long time between shutdown checks.
BATCH = 4

#: Backoff after an unexpected failure of the loop itself, so a database that
#: has gone away produces one log line every half minute rather than thousands.
ERROR_BACKOFF = 30.0


class Worker:
    def __init__(self) -> None:
        self._stopping = asyncio.Event()
        #: When the next reminder sweep is allowed to run. Zero means "now", so
        #: a restart notices anything that came due while the process was down.
        self._next_sweep = 0.0

    def stop(self) -> None:
        """Finish the document in hand, then exit."""
        if not self._stopping.is_set():
            log.info("worker_stopping")
            self._stopping.set()

    async def run(self) -> None:
        configure_logging()

        if not settings.storage_configured:
            # Not fatal. The worker idles and says why once, rather than
            # failing every document in the queue with a storage error.
            log.warning(
                "worker_storage_unconfigured",
                detail="SUPABASE_URL / SUPABASE_SERVICE_KEY are not set — "
                "nothing can be downloaded, so nothing will be extracted",
            )

        if not settings.push_configured:
            # Same shape as the storage warning: the sweep still runs and still
            # decides what to send, it just writes it to the log instead of a
            # handset. Said once, so "why did nobody get a reminder" has an
            # answer at the top of the journal.
            log.warning(
                "worker_push_unconfigured",
                detail="PUSH_ENABLED is off — reminders will be logged, not sent",
            )

        log.info(
            "worker_started",
            batch=BATCH,
            idle_seconds=IDLE_SECONDS,
            sweep_seconds=settings.reminder_sweep_seconds,
        )

        # One client for the process, like the API's. A client per document
        # re-does TLS every time and leaks sockets until the process runs out.
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(90.0, connect=10.0),
            limits=httpx.Limits(max_connections=10),
        ) as client:
            while not self._stopping.is_set():
                try:
                    did_work = await self._tick(client)
                except Exception:  # noqa: BLE001 — the loop must outlive one bad pass
                    log.exception("worker_tick_failed")
                    await self._sleep(ERROR_BACKOFF)
                    continue

                if not did_work:
                    await self._sleep(IDLE_SECONDS)

        await dispose_engine()
        log.info("worker_stopped")

    async def _tick(self, client: httpx.AsyncClient) -> bool:
        """One pass. Returns whether there was anything to do."""
        await self._sweep_reminders(client)

        async with SessionLocal() as session:
            await extraction.requeue_stalled(session)
            ids = await extraction.claim_batch(session, BATCH)

        if not ids:
            return False

        for material_id in ids:
            if self._stopping.is_set():
                # Leave the rest `pending`. Another pass or another worker takes
                # them; stopping mid-batch loses nothing.
                break

            # A session per document, so one failure cannot roll back the work
            # already committed for the others in this batch.
            async with SessionLocal() as session:
                await extraction.extract_material(session, material_id, client=client)

        return True

    async def _sweep_reminders(self, client: httpx.AsyncClient) -> None:
        """
        Send anything that has come due, at most once a minute.

        It rides on the extraction loop rather than running as a second process:
        both are "wake up, do a little work, sleep", and a separate service is
        another unit to keep alive, watch and restart for a job that is one
        indexed query most minutes.

        Its own cadence, though. The extraction loop wakes every five seconds,
        and sweeping that often would be sixty times the queries for a
        granularity nobody can perceive.

        Failures are swallowed here so a reminder problem cannot stop documents
        being extracted — the two jobs share a loop, not a fate.
        """
        loop_now = asyncio.get_running_loop().time()
        if loop_now < self._next_sweep:
            return

        self._next_sweep = loop_now + settings.reminder_sweep_seconds

        async with SessionLocal() as session:
            try:
                await notifications.sweep(session, client=client)
                # Rides the same cadence. Referral rewards move on a seven-day
                # hold and a ninety-day expiry, so a minute either way is
                # irrelevant — and a second timer for it would be a second
                # thing to get wrong.
                await referrals.sweep(session)
                await session.commit()
            except Exception:  # noqa: BLE001 — extraction must outlive this
                log.exception("reminder_sweep_failed")
                # So the sweep below starts on a session that is not already in
                # a failed transaction. Without it, one bad reminder query would
                # take the settlement sweep down with it every pass — and that
                # sweep is the only thing standing between a card payment and a
                # student locked out of what they paid for.
                await session.rollback()

            # Payments that were made and never heard about: a card payment
            # whose student closed the tab before the redirect, an M-Pesa
            # callback that was dropped.
            #
            # Its own `try` rather than its own session. The isolation that
            # matters is that a payment provider having a bad minute cannot stop
            # reminders — and reminders have already committed by this line, so
            # a second connection would buy nothing but a second thing to fail
            # to open.
            try:
                await settlement.sweep(session, client=client)
            except Exception:  # noqa: BLE001 — extraction must outlive this
                log.exception("settlement_sweep_failed")

    async def _sleep(self, seconds: float) -> None:
        """
        Sleep, but wake immediately on shutdown.

        A plain `asyncio.sleep` would make systemd wait out the full interval on
        every restart, and eventually send SIGKILL to a worker that was only
        idle.
        """
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except TimeoutError:
            pass


async def main() -> None:
    worker = Worker()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, worker.stop)
        except NotImplementedError:
            # Windows has no signal handlers on the event loop. Ctrl-C still
            # raises KeyboardInterrupt, which is enough for local runs.
            pass

    await worker.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
