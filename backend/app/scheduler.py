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
from dataclasses import dataclass
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
            "%s described, %s enriched ($%.6f), %s merged, %s rounds read, "
            "%s VC posts new",
            len(result.runs), sum(1 for r in result.runs if r.error),
            sum(r.items_new for r in result.runs), result.described,
            getattr(e, "updated", 0), getattr(e, "cost_usd", 0.0), result.merged,
            getattr(result.rounds, "extracted", 0), getattr(result.vcs, "new", 0),
        )
    except Exception:  # noqa: BLE001 - log and wait for the next interval
        log.exception("scheduler: refresh failed")
    finally:
        _run_lock.release()


# --- Manual refresh ------------------------------------------------------------------
#
# The "Refresh now" buttons. Same lock as the scheduled job, so a manual run and
# a scheduled one never overlap (and never pay for enrichment twice). Runs in a
# background thread; the page polls `manual_status()`.
#
#   feed    the full refresh: news, funding rounds for Insights, VC firms, pasted
#           websites, events
#   vcs     VC firms and websites pasted on that page: free reads, plus
#           Firecrawl reads that are due
#   events  events organised only: the sources that are due (all paid)
#   linkedin  the LinkedIn accounts that are due (all paid, Apify), then the
#           model reads their new posts

MANUAL_SCOPES = ("feed", "vcs", "events", "linkedin")


@dataclass
class ManualRun:
    scope: str | None = None
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


def _last_vc_read() -> datetime | None:
    from .models import VcRead
    with session_scope() as s:
        return s.scalar(select(func.max(VcRead.started_at)))


def _last_event_read() -> datetime | None:
    from .models import EventRead
    with session_scope() as s:
        return s.scalar(select(func.max(EventRead.started_at)))


def _last_linkedin_read() -> datetime | None:
    from .models import LinkedinRead
    with session_scope() as s:
        return s.scalar(select(func.max(LinkedinRead.started_at)))


def _cooldown_left(scope: str, now: datetime) -> timedelta | None:
    """Time left before this scope may be refreshed by hand again, if any."""
    last = {"feed": last_refresh, "vcs": _last_vc_read,
            "events": _last_event_read, "linkedin": _last_linkedin_read}[scope]()
    wait = timedelta(minutes=settings.manual_refresh_cooldown_minutes)
    if last is not None and now - last < wait:
        return wait - (now - last)
    return None


def _describe_feed(result) -> str:
    new = sum(r.items_new for r in result.runs)
    failed = [r.source for r in result.runs if r.error]
    parts = [f"{new} new {'story' if new == 1 else 'stories'}"]
    rounds = getattr(result.rounds, "extracted", None)
    if rounds is not None:
        parts.append(f"{rounds} funding {'round' if rounds == 1 else 'rounds'} read")
    if result.vcs is not None:
        parts.append(f"{result.vcs.new} new VC firm posts")
    pasted = getattr(result, "pasted", None)
    if pasted is not None and pasted.reads:
        parts.append(f"{pasted.new} new from the websites you added")
    if getattr(result, "events", None) is not None:
        parts.append(f"{result.events.new} new {'event' if result.events.new == 1 else 'events'}")
    linkedin = getattr(result, "linkedin", None)
    if linkedin is not None and linkedin.reads:
        parts.append(f"{linkedin.new} new LinkedIn {'post' if linkedin.new == 1 else 'posts'}")
    text = "Refreshed: " + ", ".join(parts)
    if failed:
        text += f". Could not read {', '.join(failed)}"
    return text + "."


def _describe_vcs(result, pasted=None) -> str:
    text = (f"Refreshed VC firms: {result.new} new "
            f"{'post' if result.new == 1 else 'posts'} from {len(result.reads)} reads")
    if pasted is not None and pasted.reads:
        text += f", {pasted.new} from the websites you added"
    if result.skipped:
        text += (f"; {result.skipped} Firecrawl reads skipped, as they ran in the "
                 f"last {settings.vc_firecrawl_hours}h")
    if result.failed:
        text += f"; {result.failed} failed"
    return text + "."


def _describe_events(result) -> str:
    if not result.reads:
        return (f"Events are up to date: every source was read in the last "
                f"{settings.events_hours}h.")
    text = (f"Refreshed events: {result.new} new "
            f"{'event' if result.new == 1 else 'events'} from {len(result.reads)} reads")
    if result.failed:
        text += f"; {result.failed} failed"
    return text + "."


def _describe_linkedin(result) -> str:
    read = result.classify.classified if result.classify else 0
    if not result.reads:
        text = (f"LinkedIn is up to date: every account was read in the last "
                f"{settings.linkedin_hours}h")
        return text + (f"; {read} waiting posts read." if read else ".")
    text = (f"Refreshed LinkedIn: {result.new} new "
            f"{'post' if result.new == 1 else 'posts'} from {len(result.reads)} "
            f"{'account' if len(result.reads) == 1 else 'accounts'}")
    if result.skipped:
        text += f"; {result.skipped} read in the last {settings.linkedin_hours}h, skipped"
    if result.failed:
        text += f"; {result.failed} failed"
    return text + "."


def _run_manual(scope: str) -> None:
    summary = error = None
    try:
        if scope == "feed":
            from .ingest.pipeline import refresh
            summary = _describe_feed(refresh())
        elif scope == "events":
            from .ingest.events import refresh_events
            summary = _describe_events(refresh_events())
        elif scope == "linkedin":
            from .ingest.linkedin import refresh_linkedin
            summary = _describe_linkedin(refresh_linkedin())
        else:
            from .ingest.pasted_sources import refresh_pasted
            from .ingest.vc_firms import refresh_firms
            summary = _describe_vcs(refresh_firms(), refresh_pasted(section="vcs"))
    except Exception as exc:  # noqa: BLE001 - reported on the page, not raised
        log.exception("manual %s refresh failed", scope)
        error = f"The refresh failed: {type(exc).__name__}: {exc}"[:300]
    finally:
        _run_lock.release()
        with _manual_guard:
            _manual.running = False
            _manual.finished_at = utcnow()
            _manual.summary, _manual.error = summary, error
        log.info("manual %s refresh done: %s", scope, error or summary)


def start_manual(scope: str) -> str | None:
    """Start a manual refresh in the background. Returns why not, or None."""
    if scope not in MANUAL_SCOPES:
        return f"Unknown refresh {scope!r}."
    now = utcnow()
    left = _cooldown_left(scope, now)
    if left is not None:
        minutes = max(1, round(left.total_seconds() / 60))
        return (f"Refreshed moments ago. Try again in {minutes} "
                f"{'minute' if minutes == 1 else 'minutes'}.")
    if not _run_lock.acquire(blocking=False):
        return "A refresh is already running. It will update this page when done."
    with _manual_guard:
        _manual.scope, _manual.running = scope, True
        _manual.started_at, _manual.finished_at = now, None
        _manual.summary = _manual.error = None
    threading.Thread(target=_run_manual, args=(scope,), daemon=True,
                     name=f"manual-{scope}-refresh").start()
    log.info("manual %s refresh started", scope)
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
