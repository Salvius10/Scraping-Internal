"""The 12-hour refresh.

Runs `ingest.pipeline.refresh()` every `refresh_hours`, either inside the API
process (the default: `scheduler_enabled`) or standalone:

    python -m app.scheduler            # blocking; runs until interrupted
    python -m app.scheduler --status   # when the next run is due, then exit

The cadence is anchored to the *last recorded refresh*, not to process start,
so restarting the server does not reset the clock -- and does not trigger a
paid run when the feed is already fresh. A feed that is overdue refreshes
straight away.

Two guards stop overlapping runs, each of which would pay for enrichment
twice: a lock within the process, and a freshness check at run time that
skips the job if anything (another process, a manual `--once`) refreshed
within the interval.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from .config import settings
from .db import init_db, session_scope
from .models import IngestRun, utcnow

log = logging.getLogger(__name__)

JOB_ID = "refresh"

# A job firing a few minutes early must not skip a run it was scheduled for.
GRACE = timedelta(minutes=10)

_run_lock = threading.Lock()
_scheduler = None


def interval() -> timedelta:
    return timedelta(hours=settings.refresh_hours)


def last_refresh() -> datetime | None:
    with session_scope() as s:
        return s.scalar(select(func.max(IngestRun.started_at)))


def next_due(last: datetime | None, now: datetime | None = None) -> datetime:
    """When the next refresh should run: now if overdue or never run."""
    now = now or utcnow()
    if last is None:
        return now
    return max(now, last + interval())


def is_fresh(last: datetime | None, now: datetime | None = None) -> bool:
    """True when a refresh ran recently enough that another would be waste."""
    if last is None:
        return False
    now = now or utcnow()
    return now - last < interval() - GRACE


def refresh_job() -> None:
    """One scheduled refresh. Never raises -- the scheduler must survive it."""
    if not _run_lock.acquire(blocking=False):
        log.warning("scheduler: a refresh is already running, skipping")
        return
    try:
        if is_fresh(last_refresh()):
            log.info("scheduler: feed refreshed recently elsewhere, skipping")
            return
        from .ingest.pipeline import refresh

        log.info("scheduler: refresh starting")
        result = refresh()
        e = result.enrich
        log.info(
            "scheduler: refresh done -- %d sources (%d failed), %d new, "
            "%s described, %s enriched ($%.6f), %s merged",
            len(result.runs), sum(1 for r in result.runs if r.error),
            sum(r.items_new for r in result.runs), result.described,
            getattr(e, "updated", 0), getattr(e, "cost_usd", 0.0), result.merged,
        )
    except Exception:  # noqa: BLE001 - log and wait for the next interval
        log.exception("scheduler: refresh failed")
    finally:
        _run_lock.release()


def _add_job(scheduler) -> None:
    first = next_due(last_refresh())
    scheduler.add_job(
        refresh_job,
        trigger="interval",
        hours=settings.refresh_hours,
        next_run_time=first,
        id=JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,                 # a machine asleep for a day runs once
        misfire_grace_time=3600,
    )
    log.info("scheduler: every %dh, next refresh at %s",
             settings.refresh_hours, first.isoformat(timespec="minutes"))


def start() -> None:
    """Start the background scheduler inside the API process. Idempotent."""
    global _scheduler
    if _scheduler is not None:
        return
    from apscheduler.schedulers.background import BackgroundScheduler

    _scheduler = BackgroundScheduler(timezone=timezone.utc, daemon=True)
    _add_job(_scheduler)
    _scheduler.start()


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def next_run_time() -> datetime | None:
    """When the in-process scheduler fires next, or None when it is not running."""
    if _scheduler is None:
        return None
    job = _scheduler.get_job(JOB_ID)
    return job.next_run_time if job else None


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    ap = argparse.ArgumentParser(description="Run the 12h news refresh.")
    ap.add_argument("--status", action="store_true",
                    help="print when the next refresh is due and exit")
    args = ap.parse_args(argv)

    init_db()
    if args.status:
        last = last_refresh()
        print("last refresh  %s" % (last.isoformat(timespec="minutes") if last else "never"))
        print("next due      %s" % next_due(last).isoformat(timespec="minutes"))
        print("interval      %dh" % settings.refresh_hours)
        return 0

    from apscheduler.schedulers.blocking import BlockingScheduler

    scheduler = BlockingScheduler(timezone=timezone.utc)
    _add_job(scheduler)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
