"""Scheduler (every 12 hours) and Manual Refresh (on demand from the platform).

Both start the same `pipeline.run.run_pipeline()`. The schedule runs inside the
API process (the default: `scheduler_enabled`) or standalone:

    python -m app.scheduler            # blocking; runs until interrupted
    python -m app.scheduler --status   # when the next run is due, then exit

The cadence is anchored to the *last recorded run*, not to process start, so
restarting the server does not reset the clock -- and does not trigger a paid
run when the data is already fresh. An overdue run starts straight away.

Three guards stop overlapping runs, each of which would pay twice: a lock
within the process, a lock file shared by every process on the same database
(`proclock.py`), and a freshness check at run time that skips the scheduled
job if anything (another process, a manual refresh) ran within the interval.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from .config import settings
from .db import init_db, session_scope
from .models import PipelineRun, utcnow
from .proclock import ProcessLock, refresh_lock

log = logging.getLogger(__name__)

JOB_ID = "refresh"

# A job firing a few minutes early must not skip a run it was scheduled for.
GRACE = timedelta(minutes=10)

_run_lock = threading.Lock()
_held: ProcessLock | None = None    # the cross-process lock, while a run holds it
_scheduler = None


def _acquire() -> bool:
    """Take both locks without waiting: this process's, then every process's."""
    global _held
    if not _run_lock.acquire(blocking=False):
        return False
    lock = refresh_lock()
    try:
        got = lock.acquire()
    except OSError:
        log.exception("scheduler: could not open %s", lock.path)
        got = False
    if not got:
        _run_lock.release()
        return False
    _held = lock
    return True


def _release() -> None:
    global _held
    lock, _held = _held, None
    try:
        if lock is not None:
            lock.release()
    finally:
        _run_lock.release()


def interval() -> timedelta:
    return timedelta(hours=settings.refresh_hours)


def last_refresh() -> datetime | None:
    with session_scope() as s:
        return s.scalar(select(func.max(PipelineRun.started_at)))


def next_due(last: datetime | None, now: datetime | None = None) -> datetime:
    """When the next run should start: now if overdue or never run."""
    now = now or utcnow()
    if last is None:
        return now
    return max(now, last + interval())


def is_fresh(last: datetime | None, now: datetime | None = None) -> bool:
    """True when a run started recently enough that another would be waste."""
    if last is None:
        return False
    now = now or utcnow()
    return now - last < interval() - GRACE


def _run(trigger: str):
    from .pipeline.run import run_pipeline
    return run_pipeline(trigger)


def refresh_job() -> None:
    """One scheduled run. Never raises -- the scheduler must survive it."""
    if not _acquire():
        log.warning("scheduler: a run is already in progress, skipping")
        return
    try:
        if is_fresh(last_refresh()):
            log.info("scheduler: ran recently elsewhere, skipping")
            return
        log.info("scheduler: run starting")
        run = _run("scheduled")
        log.info("scheduler: run %s done -- %d fetched, %d new, funding +%d, events +%d, $%.6f%s",
                 run.id, run.fetched, run.admitted, run.funding_new, run.events_new,
                 run.llm_usd, f", error: {run.error}" if run.error else "")
    except Exception:  # noqa: BLE001 - log and wait for the next interval
        log.exception("scheduler: run failed")
    finally:
        _release()


# --- Manual refresh ------------------------------------------------------------------
#
# "Refresh now" on the platform. Same lock as the scheduled job, so a manual run
# and a scheduled one never overlap (and never pay twice). Runs in a background
# thread; the page polls `manual_status()`.

@dataclass
class ManualRun:
    running: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
    summary: str | None = None
    error: str | None = None


_manual = ManualRun()
_manual_guard = threading.Lock()


def manual_status() -> ManualRun:
    with _manual_guard:
        return ManualRun(**_manual.__dict__)


def _cooldown_left(now: datetime) -> timedelta | None:
    """Time left before a refresh may be started by hand again, if any."""
    last = last_refresh()
    wait = timedelta(minutes=settings.manual_refresh_cooldown_minutes)
    if last is not None and now - last < wait:
        return wait - (now - last)
    return None


def _run_manual() -> None:
    summary = error = None
    try:
        from .pipeline.run import describe
        run = _run("manual")
        if run.error:
            error = describe(run)
        else:
            summary = describe(run)
    except Exception as exc:  # noqa: BLE001 - reported on the page, not raised
        log.exception("manual refresh failed")
        error = f"The refresh failed: {type(exc).__name__}: {exc}"[:300]
    finally:
        _release()
        with _manual_guard:
            _manual.running = False
            _manual.finished_at = utcnow()
            _manual.summary, _manual.error = summary, error
        log.info("manual refresh done: %s", error or summary)


def start_manual() -> str | None:
    """Start a manual refresh in the background. Returns why not, or None."""
    now = utcnow()
    left = _cooldown_left(now)
    if left is not None:
        minutes = max(1, round(left.total_seconds() / 60))
        return (f"Refreshed moments ago. Try again in {minutes} "
                f"{'minute' if minutes == 1 else 'minutes'}.")
    if not _acquire():
        return "A refresh is already running. This page updates when it is done."
    with _manual_guard:
        _manual.running, _manual.started_at, _manual.finished_at = True, now, None
        _manual.summary = _manual.error = None
    try:
        threading.Thread(target=_run_manual, daemon=True, name="manual-refresh").start()
    except Exception:
        # No thread means nothing will release the locks; do it here.
        _release()
        with _manual_guard:
            _manual.running = False
        raise
    log.info("manual refresh started")
    return None


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
    log.info("scheduler: every %dh, next run at %s",
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
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="Run the pipeline every refresh_hours.")
    ap.add_argument("--status", action="store_true",
                    help="print when the next run is due and exit")
    args = ap.parse_args(argv)

    init_db()
    if args.status:
        last = last_refresh()
        print("last run  %s" % (last.isoformat(timespec="minutes") if last else "never"))
        print("next due  %s" % next_due(last).isoformat(timespec="minutes"))
        print("interval  %dh" % settings.refresh_hours)
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
